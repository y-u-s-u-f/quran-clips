"""pipeline/render_default.py -- the `default` style renderer.

The first-generation look, ported from legacy/quran_reel_pipeline.py: the
footage dimmed under centred Arabic with an English line beneath, soft
drop-shadowed type, 0.4s caption fades, optional still-photo background,
optional face-centred 9:16 re-crop of a horizontal source.

Every piece of text is a transparent PNG drawn with Pillow (FreeType+RAQM)
and composited by ffmpeg `overlay` -- including the English and the
signature, which the legacy script burned through libass. libass has a
confirmed rasterization bug with Quranic Arabic fonts (HarfBuzz shapes
correctly, libass renders disconnected placeholder boxes), and slim ffmpeg
builds ship without the `subtitles` filter entirely; PIL is a different code
path that renders the same files correctly, so nothing here touches libass.

Called by generate.py with the resolved plan; not a standalone CLI.
"""
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FONT_DIR = os.path.join(ROOT, "assets", "fonts")

FFMPEG = os.environ.get("QC_FFMPEG") or "ffmpeg"

if not features.check("raqm"):
    sys.exit("FATAL: Pillow lacks RAQM (HarfBuzz+FriBiDi) -- Arabic would be "
             "laid out unjoined, left to right. Use tools/render-venv/bin/python.")

ARABIC_FONTS = {
    "uthmanic_hafs": os.path.join(FONT_DIR, "uthmanic_hafs_v22.ttf"),
    "thuluth": os.path.join(FONT_DIR, "AM_Thulth_Regular_0.1.ttf"),
}
ENGLISH_FONTS = {
    "albertus": os.path.join(FONT_DIR, "Albertus MT Lt Regular.ttf"),
    "gentium": os.path.join(FONT_DIR, "GentiumPlus-Regular.ttf"),
}

# Tajweed ANNOTATION marks (reading aids, not letters or vowels) that these
# fonts cannot mark-attach (UthmanicHafs emits a stray U+25CC ring for U+06DF)
# -- plus TATWEEL, which breaks dagger-alif anchoring in PIL/Raqm (the tiny
# alif of ٱلشَّيْطَـٰنُ detaches and lands at the far end of the word). Stripped
# for DISPLAY ONLY; the aligner never sees display text and no letter or
# pronunciation diacritic is lost.
DISPLAY_STRIP_CHARS = {"۟", "ۭ", "ـ"}

# Fixed house style, deliberately NOT config-exposed: tuned once, verified.
AR_SHADOW = {"opacity": 0.78, "blur": 6.5, "offset": 2, "color": (0, 0, 0)}
EN_SHADOW = {"opacity": 0.70, "blur": 3.0, "offset": 1, "color": (0, 0, 0)}

# English size as a fraction of the FITTED Arabic size. Locked house ratio --
# deriving English from the fitted Arabic (not from frame height) keeps the
# pair's relative weight constant even when a wide phrase shrinks the Arabic.
EN_AR_SIZE_RATIO = 0.72

# Caption fade in/out, seconds. A hard cut between recitation cards reads as
# a jolt against sustained melodic recitation. Alpha reaches 0 at both window
# edges, so two consecutive overlays can never both be opaque on a shared
# boundary frame.
FADE = 0.4

# The signature: horizontally centred inside the bottom 10% of the frame,
# matched against the account's reference frame. Only the TEXT is per-reel
# config (and required there); geometry is house style.
SIGNATURE_SIZE_FRAC = 0.026   # of frame height
SIGNATURE_Y_FRAC = 0.945      # center of the line
SIGNATURE_ALPHA = 0.87        # ~87% opaque white

# YuNet face detector (ONNX via cv2.FaceDetectorYN), used to center a
# vertical crop on the reciter. Haar cascades are dead on OpenCV 5.x.
FACE_MODEL = os.path.join(ROOT, "tools", "models",
                          "face_detection_yunet_2023mar.onnx")
FACE_MODEL_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
                  "face_detection_yunet/face_detection_yunet_2023mar.onnx")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def strip_display_marks(text):
    return "".join(c for c in text if c not in DISPLAY_STRIP_CHARS)


# ---------- subject detection (vision) --------------------------------------

def detect_subject(video_path, info, tmp, samples=5):
    """Median YuNet face box over sampled frames, or None on a weak signal.

    Sampling several frames and taking the median rejects per-frame jitter
    and the occasional false positive on patterned fabric. Fewer than half
    the samples detecting -> None, and callers fall back to geometric
    centering rather than trusting a weak signal."""
    try:
        import cv2
    except ImportError:
        print("      opencv not installed -- skipping subject detection")
        return None
    if not os.path.exists(FACE_MODEL):
        os.makedirs(os.path.dirname(FACE_MODEL), exist_ok=True)
        print("      fetching face model -> %s" % os.path.basename(FACE_MODEL))
        try:
            run(["curl", "-sL", "-o", FACE_MODEL, FACE_MODEL_URL])
        except subprocess.CalledProcessError:
            print("      face model download failed -- skipping detection")
            return None

    detector = cv2.FaceDetectorYN.create(FACE_MODEL, "",
                                         (info["width"], info["height"]),
                                         score_threshold=0.6)
    tmp_png = os.path.join(tmp, "_probe.png")
    boxes = []
    for k in range(samples):
        t = info["duration"] * (k + 1) / (samples + 1)
        run([FFMPEG, "-y", "-v", "error", "-ss", "%.2f" % t, "-i", video_path,
             "-frames:v", "1", "-update", "1", tmp_png])
        frame = cv2.imread(tmp_png)
        if frame is None:
            continue
        _, faces = detector.detect(frame)
        if faces is not None and len(faces):
            # Largest detection = the foreground reciter, not an audience head.
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])[:4]
            boxes.append((int(x), int(y), int(w), int(h)))
    if len(boxes) * 2 < samples:
        print("      subject detection weak (%d/%d frames) -- ignoring"
              % (len(boxes), samples))
        return None
    med = lambda vals: sorted(vals)[len(vals) // 2]  # noqa: E731
    x, y = med([b[0] for b in boxes]), med([b[1] for b in boxes])
    w, h = med([b[2] for b in boxes]), med([b[3] for b in boxes])
    return {"cx": x + w // 2, "cy": y + h // 2, "w": w, "h": h, "hits": len(boxes)}


def vertical_crop_filter(info, subject, target_w=1080, target_h=1920):
    """Crop a horizontal source to 9:16 centered on the subject's face
    (geometric center when detection failed). A stage shot rarely has the
    reciter dead center; a naive center crop leaves him off to one side."""
    crop_w = int(info["height"] * target_w / target_h) // 2 * 2
    center = subject["cx"] if subject else info["width"] // 2
    x = max(0, min(info["width"] - crop_w, center - crop_w // 2))
    return ("crop=%d:%d:%d:0,scale=%d:%d:flags=lanczos"
            % (crop_w, info["height"], x, target_w, target_h)), x


def output_size(w, h):
    """The output canvas for a moving-footage reel: aspect preserved, even
    dimensions, and the SHORT side clamped into [720, 1080].

    A low-quality source does not get to mean a low-quality output -- the
    floor upscales anything under 720p to 720p (lanczos), so captions and
    signature are always drawn at delivery resolution instead of being
    upscaled by the platform afterwards. The cap keeps a 4K source from
    producing a needlessly heavy encode: 1080p is the delivery ceiling."""
    short = min(w, h)
    if short < 720:
        s = 720.0 / short
    elif short > 1080:
        s = 1080.0 / short
    else:
        s = 1.0
    return int(round(w * s / 2)) * 2, int(round(h * s / 2)) * 2


# ---------- type ------------------------------------------------------------

_PROBE = ImageDraw.Draw(Image.new("RGBA", (10, 10)))


def shared_font_size(texts, font_path, base, max_width, floor=20):
    """One size fitting every caption on a single line. Per-caption
    auto-shrink would make the Arabic visibly change size between captions,
    so the widest phrase sets the size for the whole reel."""
    size = base
    while size > floor:
        font = ImageFont.truetype(font_path, size)
        if max(_PROBE.textbbox((0, 0), t, font=font)[2] for t in texts) <= max_width:
            return size
        size = int(size * 0.94)
    return size


def _shadow_pad(cfg):
    return int(cfg["blur"]) + cfg["offset"] + 6


def _draw_with_shadow(ink, w, h, cfg):
    """Solid layer through the glyph alpha, offset + blurred, ink over it --
    a subtle lift off the footage rather than an outline ring."""
    solid = Image.new("RGBA", (w, h),
                      cfg["color"] + (int(cfg["opacity"] * 255),))
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow.paste(solid, (0, 0), ink.split()[3])
    off = -cfg["offset"]
    shadow = shadow.transform((w, h), Image.AFFINE, (1, 0, off, 0, 1, off))
    shadow = shadow.filter(ImageFilter.GaussianBlur(cfg["blur"] * 0.6))
    return Image.alpha_composite(shadow, ink)


def render_text_png(text, font_path, size, shadow_cfg):
    """One line to a tight transparent PNG. -> (image, w, h)"""
    font = ImageFont.truetype(font_path, size)
    bbox = _PROBE.textbbox((0, 0), text, font=font)
    pad = _shadow_pad(shadow_cfg)
    w = (bbox[2] - bbox[0]) + pad * 2
    h = (bbox[3] - bbox[1]) + pad * 2
    ink = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(ink).text((pad - bbox[0], pad - bbox[1]), text, font=font,
                             fill=(255, 255, 255, 255))
    return _draw_with_shadow(ink, w, h, shadow_cfg), w, h


def wrap_by_width(text, font, max_width, max_chars):
    """Greedy wrap by MEASURED pixel width (max_chars as a readability cap),
    breaking only between words."""
    lines, cur = [], []
    for w in text.split():
        cand = " ".join(cur + [w])
        if cur and (len(cand) > max_chars or
                    _PROBE.textbbox((0, 0), cand, font=font)[2] > max_width):
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    return lines


def assert_fits(kind, i, png_w, center_x, frame_w):
    """A caption PNG centred on center_x must land inside the frame."""
    left = center_x - png_w // 2
    if left < 0 or left + png_w > frame_w:
        raise SystemExit(
            "%s of card %d is %dpx wide and overflows the %dpx frame -- "
            "lower text_width or arabic_scale" % (kind, i + 1, png_w, frame_w))


def trim_to_ink(card):
    """A full-frame card cropped to its non-transparent pixels. -> (img, x, y)

    The compositor pays for the whole overlay every frame a card is up, and a
    card's ink covers a fraction of the frame; what is cropped away is
    (0,0,0,0), which `overlay` composites to nothing. The box is snapped
    OUTWARD to even coordinates because overlay blends in yuv420 by default:
    on an odd edge the overlay's 2x2 chroma/alpha blocks would straddle a
    different grid than the full-frame card's and the result would shift.
    """
    box = card.getbbox()
    if box is None:                       # an empty card composites to nothing
        return card, 0, 0
    x0, y0 = box[0] - box[0] % 2, box[1] - box[1] % 2
    x1 = min(card.width, box[2] + box[2] % 2)
    y1 = min(card.height, box[3] + box[3] % 2)
    return card.crop((x0, y0, x1, y1)), x0, y0


# ---------- compositing -----------------------------------------------------

def composite(video, overlays, out_path, dim, duration,
              still_image=None, size=None, scale_to=None):
    """Dim the source, fade each caption PNG in/out over its own window,
    overlay it, then the signature (if any) for the whole duration.

    Each overlay gets `fade=in`+`fade=out:alpha=1` on its own PNG input
    rather than a shared binary `enable` gate, because `enable` cannot ramp
    alpha; the overlay is still bounded to its window so an invisible input
    never costs compositing time outside it.

    PITFALLS carried from the legacy script, both measured:
      * ffmpeg's between() is INCLUSIVE on both ends while caption ends can
        equal the next caption's start -- the eps pullback keeps two cards
        off any shared boundary frame, and the fades make it doubly safe.
      * a `-loop 1` image input NEVER ends and overlay's default eof_action
        leaves the graph with no EOF: the encode runs forever and writes an
        ever-growing file with no moov atom. The explicit `-t` bounds it;
        `-shortest` does NOT help because no input is ever short.
    """
    eps = 0.01
    if still_image:
        w, h = size or (1080, 1920)
        cmd = [FFMPEG, "-y", "-loop", "1", "-framerate", "25",
               "-i", still_image, "-i", video]
        audio_map, base = "1:a", 2
        bg = ("[0:v]scale=%d:%d:force_original_aspect_ratio=increase:"
              "flags=lanczos,crop=%d:%d,setsar=1,format=rgba,"
              "colorchannelmixer=rr=%s:gg=%s:bb=%s[bg]"
              % (w, h, w, h, dim, dim, dim))
    else:
        cmd = [FFMPEG, "-y", "-i", video]
        audio_map, base = "0:a", 1
        pre = ("scale=%d:%d:flags=lanczos," % scale_to) if scale_to else ""
        bg = ("[0:v]%scolorchannelmixer=rr=%s:gg=%s:bb=%s[bg]"
              % (pre, dim, dim, dim))

    for ov in overlays:
        cmd += ["-loop", "1", "-i", ov["path"]]

    parts = [bg]
    label = "bg"
    for i, ov in enumerate(overlays):
        if ov.get("full"):            # the signature: no fade, whole duration
            parts.append("[%d:v]format=rgba[fx%d]" % (i + base, i))
            parts.append("[%s][fx%d]overlay=x=%d:y=%d[ov%d]"
                         % (label, i, ov["x"], ov["y"], i))
        else:
            end = max(ov["start"], ov["end"] - eps)
            # Fade can't exceed half the window or in/out would overlap and
            # the card would never reach full opacity.
            fade = min(FADE, max(0.0, (end - ov["start"]) / 2))
            parts.append(
                "[%d:v]format=rgba,"
                "fade=t=in:st=%.3f:d=%.3f:alpha=1,"
                "fade=t=out:st=%.3f:d=%.3f:alpha=1[fx%d]"
                % (i + base, ov["start"], fade, max(0.0, end - fade), fade, i))
            parts.append(
                "[%s][fx%d]overlay=x=%d:y=%d:enable='between(t,%.3f,%.3f)'[ov%d]"
                % (label, i, ov["x"], ov["y"], ov["start"], end, i))
        label = "ov%d" % i
    parts.append("[%s]format=yuv420p[vout]" % label)

    run(cmd + ["-filter_complex", ";".join(parts), "-map", "[vout]",
               "-map", audio_map, "-t", "%.3f" % duration,
               "-c:v", "libx264", "-crf", "18", "-preset", "medium",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", out_path])


# ---------- the style entry point -------------------------------------------

def render(plan):
    cfg, info = plan["cfg"], dict(plan["info"])
    arabic, english = plan["arabic"], plan["english"]
    tmp, source = plan["tmp"], plan["src"]

    if cfg["arabic_font"] not in ARABIC_FONTS:
        raise SystemExit("arabic_font must be one of %s"
                         % sorted(ARABIC_FONTS))
    if cfg["english_font"] not in ENGLISH_FONTS:
        raise SystemExit("english_font must be one of %s"
                         % sorted(ENGLISH_FONTS))
    ar_font_path = ARABIC_FONTS[cfg["arabic_font"]]
    en_font_path = ENGLISH_FONTS[cfg["english_font"]]

    still = cfg.get("background_image")
    if still:
        info["width"], info["height"] = cfg["frame_size"]
        print("      still background -> %dx%d: %s"
              % (info["width"], info["height"], os.path.basename(still)))

    orientation = cfg["orientation"]
    if orientation == "auto":
        orientation = ("vertical" if info["height"] >= info["width"]
                       else "horizontal")
        print("      orientation: %s (from source aspect)" % orientation)

    # A vertical reel from a HORIZONTAL source is re-cropped from the
    # original, not letterboxed: crop first, then carry on with the new
    # geometry, since every pixel coordinate downstream shifts.
    if not still and orientation == "vertical" and info["width"] > info["height"]:
        subject = (detect_subject(source, info, tmp)
                   if cfg["detect_subject"] else None)
        if subject:
            print("      face at x=%d y=%d (%dx%dpx, %d frames)"
                  % (subject["cx"], subject["cy"], subject["w"],
                     subject["h"], subject["hits"]))
        print("      cropping horizontal source to 9:16 on the subject...")
        vf, crop_x = vertical_crop_filter(info, subject)
        cropped = os.path.join(tmp, "cropped.mp4")
        run([FFMPEG, "-y", "-v", "error", "-i", source, "-vf", vf,
             "-c:v", "libx264", "-preset", "slow", "-crf", "16",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", cropped])
        source = cropped
        import generate
        info = {**generate.get_video_info(source),
                "duration": info["duration"]}
        print("      cropped at x=%d -> %dx%d"
              % (crop_x, info["width"], info["height"]))

    width, height = info["width"], info["height"]
    scale_to = None
    if not still:
        width, height = output_size(info["width"], info["height"])
        if (width, height) != (info["width"], info["height"]):
            scale_to = (width, height)
            print("      output canvas: %dx%d (source %dx%d; short side "
                  "clamped into 720-1080)"
                  % (width, height, info["width"], info["height"]))
    # Subtitles are CENTERED by default; the offsets are the only knobs.
    center_x = width // 2 + int(cfg["x_offset"])
    margin = int(width * (1.0 - cfg["text_width"]) / 2)
    max_ar_width = 2 * min(center_x - margin, (width - margin) - center_x)

    texts = [strip_display_marks(p["text"]) for p in arabic]
    ar_size = shared_font_size(texts, ar_font_path,
                               int(height * 0.09 * cfg["arabic_scale"]),
                               max_ar_width)
    en_size = max(12, int(ar_size * EN_AR_SIZE_RATIO * cfg["english_scale"]))
    print("      arabic: %s @ %dpx | english: %s @ %dpx (ratio %.2f)"
          % (cfg["arabic_font"], ar_size, cfg["english_font"], en_size,
             en_size / ar_size))

    # Render Arabic lines first so real PNG heights are known, then center
    # the Arabic + gap + English stack as ONE block on the frame's vertical
    # center (shifted by y_offset). The tallest Arabic PNG fixes every
    # caption's baseline, so text never jumps between captions.
    ar_pngs = [render_text_png(t, ar_font_path, ar_size, AR_SHADOW)
               for t in texts]
    ar_h = max(h for _, _, h in ar_pngs)

    en_font = ImageFont.truetype(en_font_path, en_size)
    en_wrapped = [wrap_by_width(p["text"], en_font, max_ar_width,
                                cfg["english_max_chars"]) if p["text"] else []
                  for p in english]
    en_line_h = int(en_size * 1.25)
    en_h = max((len(ls) for ls in en_wrapped), default=0) * en_line_h

    pad = _shadow_pad(AR_SHADOW)
    gap = int(height * cfg["line_gap"]) - pad   # PNGs carry shadow padding
    block_h = ar_h + gap + en_h
    block_top = (height - block_h) // 2 + int(cfg["y_offset"])
    print("      block: top=%d height=%d" % (block_top, block_h))

    png_dir = os.path.join(tmp, "cards")
    os.makedirs(png_dir, exist_ok=True)
    overlays = []
    for i, ((ar_img, aw, ah), lines, ph) in enumerate(
            zip(ar_pngs, en_wrapped, arabic)):
        card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        assert_fits("arabic", i, aw, center_x, width)
        card.paste(ar_img, (center_x - aw // 2, block_top + (ar_h - ah) // 2),
                   ar_img)
        y = block_top + ar_h + gap
        for line in lines:
            img, lw, lh = render_text_png(line, en_font_path, en_size,
                                          EN_SHADOW)
            assert_fits("english", i, lw, center_x, width)
            card.paste(img, (center_x - lw // 2,
                             y + (en_line_h - lh) // 2), img)
            y += en_line_h
        cropped, ox, oy = trim_to_ink(card)
        path = os.path.join(png_dir, "%03d.png" % i)
        cropped.save(path)
        overlays.append({"path": path, "start": ph["start"], "end": ph["end"],
                         "x": ox, "y": oy})

    if cfg["signature"]:
        sig_size = max(12, int(height * SIGNATURE_SIZE_FRAC))
        sig_img, sw, sh = render_text_png(cfg["signature"], en_font_path,
                                          sig_size, EN_SHADOW)
        alpha = sig_img.getchannel("A").point(
            lambda v: int(v * SIGNATURE_ALPHA))
        sig_img.putalpha(alpha)
        card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        # ALWAYS horizontally centered; only the vertical offset is a knob.
        card.paste(sig_img, (width // 2 - sw // 2,
                             int(height * SIGNATURE_Y_FRAC) - sh // 2
                             + int(cfg["signature_offset"])), sig_img)
        cropped, ox, oy = trim_to_ink(card)
        sig_path = os.path.join(png_dir, "signature.png")
        cropped.save(sig_path)
        overlays.append({"path": sig_path, "x": ox, "y": oy, "full": True})

    composite(source, overlays, plan["out"], cfg["dim"], info["duration"],
              still_image=still, size=(width, height), scale_to=scale_to)
