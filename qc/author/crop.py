"""qc.author.crop -- solve the framing ONCE per source video.

These are fixed-camera recitation videos: the reciter does not move between one
clip and the next, so the 16:9 crop window that frames him correctly is a
property of the SOURCE, not of the clip. Every Badr al-Turki clip in `clips/`
already reuses one identical `video_bg.crop` block, hand-solved once by reading
SKILL.md:143-176 and eyeballing keyframes. This module does that solve from the
pixels and caches it in `sources/meta/<id>.yaml`, so every later clip from the
same video inherits it for free.

Three measurements, all taken over ~40 frames sampled across the whole video:

  face      YuNet (cv2.FaceDetectorYN) per frame; the reciter is the LARGEST
            face (he is nearest the camera), and we keep the MEDIAN box. The
            median is what makes this robust: one frame where he bows, is
            occluded by the mic, or where an attendee crosses cannot move it.
            The spread is reported -- a large spread means the camera is NOT
            fixed and the whole per-source premise is wrong for that video.
  silhouette high temporal variance = the thing that moves = the reciter. The
            motion blob containing the face gives his body width, which is the
            `w_r` the equal-margin rule needs; a face-width multiple is the
            fallback.
  overlays  near-ZERO temporal variance + HIGH spatial gradient near a frame
            edge = burned-in channel logo / handle / lower-third. Zero variance
            alone is not enough (a wall is static too); it is the combination
            of "never changes" and "has hard edges in it" that says "graphic".
            These become `exclude_boxes`, and the solver refuses any window
            that touches one -- the automated version of the manual "view
            keyframes, find an overlay-free window" loop.

The composition rules are the owner's, not invented here:

  horizontal  (SKILL.md:168-176) equal outer margins: with the reciter's output
              width `w_r`, `A = (0.49 - w_r)/3` and
              `caption_center = (A + w_r + 1)/2`. Caption goes on the side the
              reciter FACES (SKILL.md:156-157), which we read off YuNet's nose
              vs eye-centre landmarks. For `--style bars` the caption anchor is
              instead FIXED at 0.30 W and the only constraint is that the
              reciter stays clear of 0.60 W (SKILL.md:194-198).
  vertical    owner's rule: "the middle of the face should be in half between
              centered and the top" -> face centre at ~0.275 of output height.
              Explicitly rough, so it is a soft cost with a 0.22-0.33 warning
              band, never a hard failure.

Requires opencv (tools/author-venv) -- see requirements/author.txt. If the
detector finds nothing, `--sheet` writes a labelled contact grid of samples and
`--face X,Y` takes the centre by hand; that is one manual step per SOURCE, which
still amortises over every clip cut from it.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCES = os.path.join(ROOT, "sources")
META = os.path.join(SOURCES, "meta")
YUNET = os.path.join(ROOT, "tools", "models", "face_detection_yunet_2023mar.onnx")

# --- the owner's numbers ---------------------------------------------------
FACE_Y_FRAC = 0.275          # "half between centered and the top"
FACE_Y_WARN = (0.22, 0.33)   # outside this we warn, we do not fail
CAPTION_HALF_W = 0.252       # widest caption half-width, default style
BARS_CENTER_X = 0.30         # bars caption anchor is fixed
BARS_CLEAR_X = 0.60          # ...so the reciter must stay clear of 0.60 W
BODY_FROM_FACE = 2.6         # fallback seated-shoulder width / face width
HEAD_FROM_FACE = 2.0         # head incl. keffiyeh / face width (bars clearance)

# --- overlay detection tuning ----------------------------------------------
# STATIC_STD is deliberately TIGHT. A burned-in graphic is the same pixels every
# frame, so its temporal std is pure codec noise (~0.5-1.5 grey levels); a real
# wall drifts more than that over 20 minutes of changing light. Loosening this
# to 3.0 starts swallowing the architecture and the solver then finds no legal
# window at all.
STATIC_STD = 1.5             # 0-255 grey levels; below this = never changes
EDGE_GRAD = 40.0             # blurred |Sobel| of the median frame
EDGE_BAND = (0.22, 0.22, 0.22, 0.30)  # left, right, top, bottom band widths
MIN_SIDE = 12                # px; kills the 1-2 px static edges of pillars
MIN_FILL = 0.20              # component area / bbox area; graphics are blocky


def _cv2():
    try:
        import cv2
        import numpy
        return cv2, numpy
    except ImportError:
        raise SystemExit(
            "qc crop needs opencv+numpy, which are NOT in render-venv.\n"
            "  python3.14 -m venv tools/author-venv\n"
            "  tools/author-venv/bin/pip install -r requirements/author.txt\n"
            "then run:  tools/author-venv/bin/python -m qc crop <video_id>\n"
            "(NEVER pip-install into /opt/homebrew/bin/python3.)")


# --- sampling --------------------------------------------------------------

def sample(src, n=40, lo=0.03, hi=0.97):
    """n frames spread across the video. Returns (times, [BGR frames])."""
    cv2, _ = _cv2()
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise SystemExit("cannot open %s" % src)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    dur = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / fps
    times, frames = [], []
    for i in range(n):
        t = dur * (lo + (hi - lo) * (i / max(1, n - 1)))
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, f = cap.read()
        if ok and f is not None:
            times.append(t)
            frames.append(f)
    cap.release()
    if not frames:
        raise SystemExit("read no frames from %s" % src)
    return times, frames


# --- face ------------------------------------------------------------------

def detect_faces(frames, score=0.5):
    """Per frame: the LARGEST face, as (x, y, w, h, faces_dx) or None.

    Largest, not highest-scoring: in a Haram taraweeh frame YuNet happily finds
    six faces and the row of attendees behind the imam often scores higher than
    he does. He is the one nearest the camera, so he is the biggest.

    faces_dx is (nose_x - eye_centre_x) / face_w: negative = he is turned to
    screen LEFT, positive = screen RIGHT, ~0 = square to camera.
    """
    cv2, _ = _cv2()
    if not os.path.exists(YUNET):
        raise SystemExit(
            "missing %s\n  curl -sSL -o %s \\\n    https://media.githubusercontent.com"
            "/media/opencv/opencv_zoo/main/models/face_detection_yunet/"
            "face_detection_yunet_2023mar.onnx" % (YUNET, YUNET))
    h, w = frames[0].shape[:2]
    det = cv2.FaceDetectorYN.create(YUNET, "", (w, h), score, 0.3, 5000)
    out = []
    for f in frames:
        _, faces = det.detect(f)
        if faces is None or not len(faces):
            out.append(None)
            continue
        b = max(faces, key=lambda r: r[2] * r[3])
        eye_cx = (b[4] + b[6]) / 2.0
        out.append((float(b[0]), float(b[1]), float(b[2]), float(b[3]),
                    float((b[8] - eye_cx) / max(1.0, b[2]))))
    return out


def median_face(dets):
    """Median box over the frames that saw a face, plus the spread.

    Spread is reported as the 10-90 percentile range of the centre, in source
    px. Small = fixed camera and the per-source cache is sound. Large = the
    video cuts between angles or the camera moves, and no single crop is right.
    """
    _, np = _cv2()
    got = [d for d in dets if d]
    if not got:
        return None
    a = np.array(got, dtype=float)
    cx, cy = a[:, 0] + a[:, 2] / 2, a[:, 1] + a[:, 3] / 2
    def spread(v):
        return float(np.percentile(v, 90) - np.percentile(v, 10))
    return {
        "cx": float(np.median(cx)), "cy": float(np.median(cy)),
        "w": float(np.median(a[:, 2])), "h": float(np.median(a[:, 3])),
        "faces_dx": float(np.median(a[:, 4])),
        "n": len(got), "n_frames": len(dets),
        "spread_cx": spread(cx), "spread_cy": spread(cy),
        "spread_w": spread(a[:, 2]),
    }


def facing(face, W):
    """Which way is he turned? -> 'left' | 'right' | 'frontal'."""
    if face is None:
        return "frontal"
    d = face["faces_dx"]
    if d < -0.06:
        return "left"
    if d > 0.06:
        return "right"
    return "frontal"


# --- temporal variance: silhouette + burned-in overlays --------------------

def variance(frames):
    """(std map, median frame) over the sample stack, in grey."""
    cv2, np = _cv2()
    g = np.stack([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
                  for f in frames])
    return g.std(axis=0), np.median(g, axis=0).astype(np.uint8), g


def inliers(dets, face, W):
    """Indices of frames shot from the DOMINANT camera angle.

    A 20-minute upload often contains more than one prayer and more than one
    camera. The median face already comes from the biggest cluster; these are
    the frames that agree with it, and they are the only ones the silhouette
    measurement may use (mixing angles turns "what moves" into "everything").
    """
    tol = max(0.02 * W, 1.2 * face["w"])
    out = []
    for i, d in enumerate(dets):
        if d and abs(d[0] + d[2] / 2 - face["cx"]) < tol \
                and abs(d[1] + d[3] / 2 - face["cy"]) < tol:
            out.append(i)
    return out


def silhouette(std, face):
    """Horizontal extent of the moving blob that contains the face.

    The reciter is the only large thing in frame that moves, so thresholding
    the temporal std and taking the component under his face measures his body
    width directly -- much better than guessing shoulders from face width. The
    result is clamped to a sane multiple of the face so a frame-wide motion
    blob (a passing attendee merging into him) cannot blow it up.
    """
    cv2, np = _cv2()
    if face is None:
        return None
    H, W = std.shape
    thr = max(4.0, float(np.percentile(std, 60)))
    m = (std > thr).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE,
                         cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25)))
    n, lab = cv2.connectedComponents(m)
    x, y = int(round(face["cx"])), int(round(face["cy"]))
    x, y = min(max(x, 0), W - 1), min(max(y, 0), H - 1)
    if lab[y, x] == 0:
        return None
    ys, xs = np.where(lab == lab[y, x])
    x0, x1 = float(xs.min()), float(xs.max())
    lo = face["cx"] - 2.4 * face["w"], face["cx"] + 2.4 * face["w"]
    x0 = max(x0, lo[0])
    x1 = min(x1, lo[1])
    if x1 - x0 < 1.4 * face["w"]:
        return None
    return (x0, x1)


def overlay_boxes(std, med, min_area_frac=3e-4):
    """Burned-in graphics: static + textured + near a frame edge.

    KNOWN LIMIT, stated because it matters: this finds overlays that are up for
    the WHOLE video. An INTERMITTENT lower-third (YkXjYyKwHJ4's prayer-name
    banner, up for one prayer only) has HIGH temporal variance and is invisible
    to this test. Every variance-based rescue for it -- per-group statics,
    bimodality -- also flags the static background of any video that cuts
    cameras, which is worse than missing the banner. So it stays the human's
    job, and `run()` says so out loud.
    """
    cv2, np = _cv2()
    H, W = std.shape
    band = np.zeros((H, W), np.uint8)
    l, r, t, b = EDGE_BAND
    band[:, :int(l * W)] = 1
    band[:, int((1 - r) * W):] = 1
    band[:int(t * H), :] = 1
    band[int((1 - b) * H):, :] = 1

    def textured(frame):
        gx = cv2.Sobel(frame, cv2.CV_32F, 1, 0, 3)
        gy = cv2.Sobel(frame, cv2.CV_32F, 0, 1, 3)
        return cv2.GaussianBlur(np.abs(gx) + np.abs(gy), (0, 0), 7) > EDGE_GRAD

    m = (std < STATIC_STD) & textured(med) & (band > 0)
    mm = cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_CLOSE,
                          cv2.getStructuringElement(cv2.MORPH_RECT, (41, 15)))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mm, 8)
    boxes = []
    min_area = min_area_frac * W * H
    for i in range(1, n):
        x, y, w, h, a = stats[i]
        if a < min_area or w * h > 0.25 * W * H:
            continue
        if min(w, h) < MIN_SIDE or a < MIN_FILL * w * h:
            continue        # a 2 px static line down a pillar is not a logo
        boxes.append([int(x), int(y), int(w), int(h)])
    return _merge(boxes)


def _merge(boxes, pad=24):
    """Union overlapping/adjacent boxes until stable."""
    out = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                a, b = out[i], out[j]
                if (a[0] - pad < b[0] + b[2] and b[0] - pad < a[0] + a[2] and
                        a[1] - pad < b[1] + b[3] and b[1] - pad < a[1] + a[3]):
                    x0 = min(a[0], b[0]); y0 = min(a[1], b[1])
                    x1 = max(a[0] + a[2], b[0] + b[2])
                    y1 = max(a[1] + a[3], b[1] + b[3])
                    out[i] = [x0, y0, x1 - x0, y1 - y0]
                    del out[j]
                    changed = True
                    break
            if changed:
                break
    return sorted(out, key=lambda b: (-b[2] * b[3]))


# --- the solve -------------------------------------------------------------

def _hits(x, y, w, h, boxes, pad=6):
    for bx, by, bw, bh in boxes:
        if (x < bx + bw + pad and bx - pad < x + w and
                y < by + bh + pad and by - pad < y + h):
            return True
    return False


def targets(style, side, w_r, head_hw):
    """Where the face centre must land horizontally, and where the caption goes.

    Both are fractions of the OUTPUT frame. `side` is the caption side.
    """
    if style == "bars":
        # Caption anchor is fixed; the only rule is clearance. Put the
        # reciter's head edge exactly on the clearance line, which is also what
        # maximises the space left on his own side.
        if side == "left":
            return BARS_CLEAR_X + head_hw, BARS_CENTER_X
        return (1 - BARS_CLEAR_X) - head_hw, 1 - BARS_CENTER_X
    A = (0.49 - w_r) / 3.0
    if A <= 0.01:
        A = 0.01
    if side == "right":                       # reciter LEFT
        return A + w_r / 2.0, (A + w_r + 1) / 2.0
    return 1.0 - (A + w_r / 2.0), 1.0 - (A + w_r + 1) / 2.0


def solve(W, H, face, sil, boxes, style="bars", side="left",
          scales=None, step=8):
    """Largest 16:9 window that frames him by the rules and misses every overlay.

    Zoom is a last resort, not a knob: we start at the biggest 16:9 window the
    source allows and only shrink because a bigger one cannot both place the
    reciter AND clear a burned-in logo -- exactly the reasoning written by hand
    into al-ahzab-70-71's clip.yaml.
    """
    base_w = min(W, H * 16.0 / 9.0)
    if scales is None:
        scales = [1.0 - 0.02 * i for i in range(26)]
    best = None
    for s in scales:
        w = int(round(base_w * s / 2) * 2)
        h = int(round(w * 9.0 / 16.0 / 2) * 2)
        if w > W or h > H or w < 160:
            continue
        w_r = (sil[1] - sil[0]) / w if sil else BODY_FROM_FACE * face["w"] / w
        head_hw = HEAD_FROM_FACE * face["w"] / (2.0 * w)
        fx_t, cap_cx = targets(style, side, w_r, head_hw)
        for x in range(0, W - w + 1, step):
            fx = (face["cx"] - x) / w
            ex = abs(fx - fx_t)
            if ex > 0.16:
                continue
            for y in range(0, H - h + 1, step):
                fy = (face["cy"] - y) / h
                ey = abs(fy - FACE_Y_FRAC)
                if ey > 0.10:
                    continue
                if _hits(x, y, w, h, boxes):
                    continue
                cost = 3.0 * ex + 2.0 * ey + 0.5 * (1.0 - s)
                if best is None or cost < best["cost"]:
                    best = {"cost": cost, "x": x, "y": y, "w": w, "h": h,
                            "scale": s, "w_r": w_r, "fx": fx, "fy": fy,
                            "fx_target": fx_t, "caption_cx": cap_cx,
                            "head_left": fx - head_hw * 1.0,
                            "head_right": fx + head_hw * 1.0}
        if best is not None and s > 0.92:
            break                      # a near-full-size window already works
    return best


# --- reporting -------------------------------------------------------------

def annotate(frame, sol, face, sil, boxes, out_path, style="bars"):
    """Draw the solve onto a real frame so the owner can eyeball it in one look."""
    cv2, _ = _cv2()
    im = frame.copy()
    for bx, by, bw, bh in boxes:                                  # red: excluded
        cv2.rectangle(im, (bx, by), (bx + bw, by + bh), (0, 0, 255), 2)
    if sil:                                                       # cyan: body
        cv2.line(im, (int(sil[0]), 0), (int(sil[0]), im.shape[0]), (255, 255, 0), 1)
        cv2.line(im, (int(sil[1]), 0), (int(sil[1]), im.shape[0]), (255, 255, 0), 1)
    if face:                                                      # yellow: face
        x = int(face["cx"] - face["w"] / 2); y = int(face["cy"] - face["h"] / 2)
        cv2.rectangle(im, (x, y), (int(x + face["w"]), int(y + face["h"])),
                      (0, 255, 255), 2)
        cv2.drawMarker(im, (int(face["cx"]), int(face["cy"])), (0, 255, 255),
                       cv2.MARKER_CROSS, 22, 2)
    if sol:
        x, y, w, h = sol["x"], sol["y"], sol["w"], sol["h"]
        cv2.rectangle(im, (x, y), (x + w, y + h), (0, 255, 0), 3)   # green: crop
        hw = 0.25 if style == "bars" else CAPTION_HALF_W            # blue: caption
        cx = sol["caption_cx"]
        cv2.rectangle(im, (int(x + (cx - hw) * w), int(y + 0.30 * h)),
                      (int(x + (cx + hw) * w), int(y + 0.70 * h)), (255, 128, 0), 2)
        cv2.line(im, (x, int(y + FACE_Y_FRAC * h)), (x + w, int(y + FACE_Y_FRAC * h)),
                 (0, 255, 0), 1)
    cv2.imwrite(out_path, im)
    return out_path


def contact_sheet(times, frames, out_path, cols=5):
    """Fallback when the detector finds nothing: a labelled grid to read x,y off."""
    cv2, np = _cv2()
    th = 220
    tw = int(th * frames[0].shape[1] / frames[0].shape[0])
    rows = (len(frames) + cols - 1) // cols
    sheet = np.zeros((rows * th, cols * tw, 3), np.uint8)
    for i, f in enumerate(frames):
        r, c = divmod(i, cols)
        t = cv2.resize(f, (tw, th))
        cv2.putText(t, "%d  %.0fs" % (i, times[i]), (6, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
        sheet[r * th:(r + 1) * th, c * tw:(c + 1) * tw] = t
    cv2.imwrite(out_path, sheet)
    return out_path


# --- the cache -------------------------------------------------------------

MARK = "# --- framing"


def write_framing(vid, fr):
    """Append/replace a `framing:` block in sources/meta/<id>.yaml.

    Kept in the SAME file as the yt-dlp metadata because it is the same kind of
    thing: expensive to derive, cheap to store, and true of the video forever.
    Everything above the marker is left byte-for-byte alone.
    """
    path = os.path.join(META, "%s.yaml" % vid)
    head = ""
    if os.path.exists(path):
        head = open(path, encoding="utf-8").read()
        if MARK in head:
            head = head[:head.index(MARK)]
    if head and not head.endswith("\n"):
        head += "\n"
    L = [MARK + ": solved by `qc crop` over %d sampled frames." % fr["frames"],
         "# Fixed camera -> one crop per SOURCE; clips inherit it. Hand-tunable.",
         "framing:",
         "  style: %s" % fr["style"],
         "  crop: {x: %d, y: %d, w: %d, h: %d}" % tuple(fr["crop"]),
         "  text_center_x_frac: %.3f" % fr["text_center_x_frac"],
         "  caption_side: %s" % fr["caption_side"],
         "  faces: %s" % fr["faces"],
         "  face_px: {cx: %d, cy: %d, w: %d, h: %d}  # median over %d/%d frames"
         % (fr["face"][0], fr["face"][1], fr["face"][2], fr["face"][3],
            fr["face_n"], fr["frames"]),
         "  face_frac: {x: %.3f, y: %.3f}   # y rule: ~%.3f" % (
             fr["face_frac"][0], fr["face_frac"][1], FACE_Y_FRAC),
         "  reciter_w_frac: %.3f" % fr["reciter_w_frac"],
         "  camera_spread_px: {cx: %.0f, cy: %.0f}" % tuple(fr["spread"]),
         "  exclude_boxes:" if fr["exclude_boxes"] else "  exclude_boxes: []"]
    for b in fr["exclude_boxes"]:
        L.append("    - {x: %d, y: %d, w: %d, h: %d}" % tuple(b))
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(L) + "\n")
    return path


def read_framing(vid):
    """Read back the framing block. Returns {} if this source has not been solved."""
    path = os.path.join(META, "%s.yaml" % vid)
    if not os.path.exists(path):
        return {}
    txt = open(path, encoding="utf-8").read()
    if "framing:" not in txt:
        return {}
    out, boxes = {}, []
    for line in txt[txt.index("framing:"):].splitlines()[1:]:
        s = line.strip()
        if s.startswith("- {"):
            boxes.append(_flow(s[2:]))
            continue
        if not s or s.startswith("#") or ":" not in s or not line.startswith("  "):
            continue
        k, _, v = s.partition(":")
        v = v.split("#")[0].strip()
        out[k.strip()] = _flow(v) if v.startswith("{") else _scalar(v)
    out["exclude_boxes"] = boxes
    return out


def _scalar(v):
    if v in ("[]", ""):
        return []
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v.strip('"')


def _flow(v):
    v = v.strip().strip("{}")
    d = {}
    for part in v.split(","):
        if ":" in part:
            k, _, x = part.partition(":")
            d[k.strip()] = _scalar(x.strip())
    return d


# --- entry point -----------------------------------------------------------

def run(vid, style="bars", frames=40, side=None, face_xy=None, extra_boxes=None,
        annotate_path=None, sheet_path=None, write=False):
    cv2, np = _cv2()
    src = os.path.join(SOURCES, "%s.mp4" % vid)
    if not os.path.exists(src):
        raise SystemExit("no %s -- run `qc source add` first" % src)
    times, fr = sample(src, n=frames)
    H, W = fr[0].shape[:2]
    if len(fr) < 24:
        # Overlay detection is a variance test, so it is only as good as the
        # sample: with a dozen frames plenty of ordinary background has not yet
        # had a chance to change, gets called an overlay, and boxes the solver
        # into a needlessly tight window.
        print("! only %d frames -- burned-in detection needs >=24 to separate a "
              "logo from a wall; boxes below will over-report." % len(fr),
              file=sys.stderr)
    print("%s  %dx%d  %d frames sampled over %.0fs" % (vid, W, H, len(fr), times[-1]))

    std, med, grey = variance(fr)
    boxes = overlay_boxes(std, med)
    # --exclude is the escape hatch for the intermittent case above: tell the
    # tool once where the banner lives and the cache keeps it forever.
    boxes = _merge(boxes + [list(b) for b in (extra_boxes or [])])
    print("exclude_boxes (always-on burned-in graphics, %d):" % len(boxes))
    for b in boxes:
        print("  x%-5d y%-5d %dx%d   (x %.2f-%.2f W, y %.2f-%.2f H)" % (
            b[0], b[1], b[2], b[3], b[0] / W, (b[0] + b[2]) / W,
            b[1] / H, (b[1] + b[3]) / H))
    print("  (an INTERMITTENT lower-third is not detectable by variance -- if "
          "this channel flashes a prayer-name banner, check for it by eye.)")

    if face_xy:
        fx, fy = face_xy[0], face_xy[1]
        # Third value is the face WIDTH; without it we assume 0.09 W, which is
        # right for a head-on face and too wide for a profile (only half the
        # face is visible). It only feeds the bars clearance and the fallback
        # body width, so a wrong guess shifts framing rather than breaking it.
        fw = face_xy[2] if len(face_xy) > 2 else W * 0.09
        face = {"cx": float(fx), "cy": float(fy), "w": float(fw), "h": float(fw),
                "faces_dx": 0.0, "n": 0, "n_frames": len(fr),
                "spread_cx": 0.0, "spread_cy": 0.0, "spread_w": 0.0}
        print("face    : HAND-ENTERED centre %d,%d, width %d" % (fx, fy, fw))
        keep = list(range(len(fr)))
    else:
        dets = detect_faces(fr)
        face = median_face(dets)
        if face is None:
            p = contact_sheet(times, fr, sheet_path or
                              os.path.join(SOURCES, "%s.sheet.png" % vid))
            raise SystemExit(
                "no face found in any of %d frames.\nContact sheet: %s\n"
                "Read the reciter's face centre off it and re-run with "
                "--face X,Y (source px). One manual step per SOURCE." % (len(fr), p))
        print("face    : median %.0f,%.0f  %.0fx%.0f  seen in %d/%d frames"
              % (face["cx"], face["cy"], face["w"], face["h"],
                 face["n"], face["n_frames"]))
        sp = max(face["spread_cx"], face["spread_cy"])
        keep = inliers(dets, face, W)
        print("          camera spread p10-p90: cx %.0f px, cy %.0f px (%.1f%% of W)"
              "; %d/%d frames on the dominant angle"
              % (face["spread_cx"], face["spread_cy"], 100.0 * sp / W,
                 len(keep), len(fr)))
        if sp > 0.06 * W:
            print("! camera is NOT fixed (spread %.0f px > 6%% of width): this video "
                  "cuts angles or moves, so ONE crop per source is wrong for it -- "
                  "verify per segment (SKILL.md:163-167)." % sp, file=sys.stderr)

    # Silhouette from the dominant angle only: across a cut, "what moved" is
    # the entire frame.
    sil = silhouette(variance([fr[i] for i in keep])[0] if len(keep) > 3 else std,
                     face)
    if sil:
        print("reciter : motion silhouette source px %.0f..%.0f (%.1f face widths)"
              % (sil[0], sil[1], (sil[1] - sil[0]) / face["w"]))
    else:
        print("reciter : no clean motion blob; falling back to %.1fx face width"
              % BODY_FROM_FACE)

    faces = facing(face, W)
    if side is None:
        # Caption goes on the side he FACES (SKILL.md:156). Square to camera
        # tells us nothing, so fall back to the style's default hand.
        side = {"left": "left", "right": "right"}.get(faces, "left")
    print("facing  : %s (nose vs eye-centre %+.2f face widths) -> captions %s"
          % (faces, face["faces_dx"], side))

    sol = solve(W, H, face, sil, boxes, style=style, side=side)
    if sol is None:
        raise SystemExit(
            "no 16:9 window satisfies the rules AND clears the burned-in overlays.\n"
            "Re-run with --side %s, or accept a visible logo (SKILL.md:159-162) by "
            "hand-editing the framing block." % ("right" if side == "left" else "left"))

    print("crop    : {x: %d, y: %d, w: %d, h: %d}   (%.2fx zoom)"
          % (sol["x"], sol["y"], sol["w"], sol["h"], W / float(sol["w"])))
    print("face    : x %.3f W (target %.3f), y %.3f H (owner's rule %.3f)"
          % (sol["fx"], sol["fx_target"], sol["fy"], FACE_Y_FRAC))
    if not (FACE_Y_WARN[0] <= sol["fy"] <= FACE_Y_WARN[1]):
        print("! face centre at %.3f H is outside the %.2f-%.2f band -- the source "
              "cannot give that headroom here; eyeball the annotated frame."
              % (sol["fy"], FACE_Y_WARN[0], FACE_Y_WARN[1]), file=sys.stderr)
    print("reciter : w_r %.3f of output; head spans %.3f..%.3f W"
          % (sol["w_r"], sol["head_left"], sol["head_right"]))
    print("text    : center_x_frac %.3f  (%s style)" % (sol["caption_cx"], style))
    if style == "bars":
        clear = sol["head_left"] if side == "left" else 1 - sol["head_right"]
        print("          bars clearance: head edge at %.3f W vs required %.2f"
              % (clear if side == "left" else 1 - clear, BARS_CLEAR_X))

    if annotate_path:
        mid = fr[len(fr) // 2]
        print("annotate: %s" % annotate(mid, sol, face, sil, boxes,
                                        annotate_path, style=style))
    if sheet_path:
        print("sheet   : %s" % contact_sheet(times, fr, sheet_path))

    rec = {
        "style": style, "frames": len(fr),
        "crop": (sol["x"], sol["y"], sol["w"], sol["h"]),
        "text_center_x_frac": round(sol["caption_cx"], 3),
        "caption_side": side, "faces": faces,
        "face": (round(face["cx"]), round(face["cy"]),
                 round(face["w"]), round(face["h"])),
        "face_n": face["n"],
        "face_frac": (sol["fx"], sol["fy"]),
        "reciter_w_frac": sol["w_r"],
        "spread": (face["spread_cx"], face["spread_cy"]),
        "exclude_boxes": boxes,
    }
    if write:
        print("wrote   : %s" % os.path.relpath(write_framing(vid, rec), ROOT))
    else:
        print("(dry run -- pass --write to cache this in sources/meta/%s.yaml)" % vid)
    return rec
