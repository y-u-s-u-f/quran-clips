"""pipeline/crop.py -- solve a reel's framing at authoring time.

    tools/render-venv/bin/python pipeline/crop.py sources/<id>/<reel>.yaml
        [--frames 4] [--annotate out.png] [--write] [--force]

Samples frames from the reel's trim window, asks a vision model (any
OpenAI-compatible API, set in .env) where the reciter's head is, then picks
the window with arithmetic:

    bars, horizontal  1920x1080  -> crop: + x_offset:
    vertical          1080x1920  -> crop: + face_bottom:

Column styles: the caption goes on the side he FACES (facing right on screen
-> he sits left, caption right). Frontal -> whichever side frames better. The
gaps edge|reciter, reciter|caption, caption|edge come out roughly equal, and
the caption is centred in the free space beside him.
Vertical: he is centred; the caption hangs under his head box (`face_bottom`).
Up/down, every style: head centre near FACE_Y, crown kept in frame, never
below the middle.

Authoring only (invariant 4): no model at render time. `--annotate` is the
check -- the numbers can agree with each other and still be a box drawn
round the wrong thing.
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import generate  # noqa: E402  (config/path helpers only -- no Pillow)

ASPECT = {"bars": (16, 9), "horizontal": (16, 9), "vertical": (9, 16)}
CANVAS_W = {"bars": 1920, "horizontal": 1920, "vertical": 1080}
# Caption column width as a fraction of the window. Hard-coded, not imported:
# both renderers pull in Pillow, which this script must not need. horizontal
# is the wider of its two lines (render_text AR 0.51 / EN 0.55).
CAPTION_W = {"bars": 0.45, "horizontal": 0.55, "vertical": None}
FACE_Y = 0.275          # measured face centre in shipped windows
HEADROOM = 0.04         # air kept above his crown
MIN_SCALE = 0.5
# Cost of zooming in, per unit of lost scale, against 1.0 per window-width he
# sits off his mark. Vertical weights resolution far higher: a 9:16 window of
# a 16:9 source has already lost two thirds of the pixels (hajri-23-taraweeh:
# the column weight bought dead-centre with a 2.5x upscale over a 608x1080
# window that cleared both logos with him at 0.66).
ZOOM_COST = {"bars": 0.5 / 3, "horizontal": 0.5 / 3, "vertical": 1.5}
MAX_FACE_BOTTOM = 0.75  # vertical: below this there is no room to caption

FRAME_W = 1280          # ~1.2k tokens/frame; 4K buys nothing here
GRID = 1000.0           # 0-1000 grid: 0..1 or pixels mis-scale (vqxYwdR4RvQ)
TIMEOUT = 180
RETRIES = 3
PROMPT = """These are %d frames, in time order, from one continuous shot of a \
Qur'an recitation video. I am cropping it for a social media reel and need to \
know where the reciter is.

Give every coordinate on a 0-1000 grid over the frame, as whole numbers: x is \
0 at the left edge and 1000 at the right edge, y is 0 at the top and 1000 at \
the bottom.

The reciter is the man leading the recitation: nearest the camera, largest in \
frame. People praying or sitting behind him are not the reciter.

For EACH frame, in order:
  present       false if he is out of frame or completely hidden (the other \
fields are then ignored).
  face_visible  true ONLY if you can actually see his facial features (an eye, \
the nose, the mouth, the line of the cheek or jaw). False if his head is \
turned away, bowed so the cloth hides his face, covered, or only the back or \
top of it shows. If unsure, false.
  head          [left, top, right, bottom] of his head INCLUDING all headwear \
(ghutrah, shemagh, turban, cap, hair): from the top of the cloth to the bottom \
of the chin or beard, only as wide as the head and its cloth. Not his \
shoulders, not a microphone in front of him.
  facing        which way his NOSE POINTS ON THE SCREEN: "left" (toward x=0), \
"right" (toward x=1000), or "frontal" if he is square to the camera. A man \
filmed from his own right side is facing "left" here.

Once, for the whole shot:
  graphics      [left, top, right, bottom] of every graphic BURNED INTO THE \
VIDEO: channel logos, watermarks, social handles, name plates, subtitles, \
tickers. They are crisp, flat and identical in every frame; check all four \
corners and the bottom strip. Anything in the room is NOT a graphic: \
microphones, pillars, lamps, rails, walls, windows, people. Empty list if none.

Reply with only this JSON:
{"frames": [{"present": true, "face_visible": true, "head": [0, 0, 0, 0], \
"facing": "frontal"}], "graphics": [[0, 0, 0, 0]]}"""


def even(v):
    return int(round(v / 2.0)) * 2


# --- the model -------------------------------------------------------------

def sample_times(t0, t1, n):
    """Inset 6% from both ends: that is where a fade, a bow or a cut lives."""
    lo, hi = t0 + 0.06 * (t1 - t0), t0 + 0.94 * (t1 - t0)
    return [(lo + hi) / 2.0] if n == 1 else \
        [lo + (hi - lo) * i / (n - 1.0) for i in range(n)]


def extract(src, times, tmp, W):
    os.makedirs(tmp, exist_ok=True)
    out = []
    for i, t in enumerate(times):
        p = os.path.join(tmp, "crop-%02d.jpg" % i)
        subprocess.run([generate.FFMPEG, "-y", "-v", "error", "-ss", "%.3f" % t,
                        "-i", src, "-frames:v", "1", "-vf",
                        "scale=%d:-2:flags=lanczos" % min(FRAME_W, W),
                        "-q:v", "3", p], check=True)
        out.append(p)
    return out


def ask(paths, model):
    base = generate.envvar("QC_VISION_BASE_URL", "https://api.openai.com/v1")
    key = generate.envvar("QC_VISION_API_KEY")
    if not key:
        raise SystemExit("set QC_VISION_API_KEY in %s (see .env.example)"
                         % os.path.join(ROOT, ".env"))
    content = [{"type": "text", "text": PROMPT % len(paths)}]
    for p in paths:
        b64 = base64.b64encode(open(p, "rb").read()).decode()
        content.append({"type": "image_url", "image_url":
                        {"url": "data:image/jpeg;base64," + b64}})
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps({"model": model, "messages": [
            {"role": "user", "content": content}]}).encode(),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    err = None
    for attempt in range(RETRIES):
        if attempt:
            print("      %s, retrying" % err, file=sys.stderr)
            time.sleep(2.0)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                reply = json.load(r)
            return parse(reply["choices"][0]["message"]["content"], len(paths))
        except urllib.error.HTTPError as e:
            err = "HTTP %d: %s" % (e.code, e.read()[:400].decode(errors="replace"))
            if e.code in (400, 401, 403, 404):     # retrying will not fix these
                break
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError,
                TypeError, ValueError) as e:
            err = "%s: %s" % (type(e).__name__, e)
    raise SystemExit("vision API failed (%s): %s" % (base, err))


def parse(text, n):
    """The reply's JSON object, or ValueError (ask() retries)."""
    s = text or ""
    d = json.loads(s[s.find("{"):s.rfind("}") + 1])

    def box(b):
        return isinstance(b, list) and len(b) == 4 and \
            all(isinstance(v, (int, float)) for v in b)
    frames = d.get("frames")
    if not isinstance(frames, list) or len(frames) != n:
        raise ValueError("expected %d frames: %s" % (n, s[:300]))
    for f in frames:
        if f.get("present") and not (box(f.get("head")) and
                                     f.get("facing") in ("left", "right",
                                                         "frontal")):
            raise ValueError("bad frame %r" % f)
    if not all(box(g) for g in d.get("graphics", [])):
        raise ValueError("bad graphics %r" % d.get("graphics"))
    return {"frames": frames, "graphics": d.get("graphics", [])}


def measure(answer, W, H):
    """Median head box in source px, plus the votes. None = no reciter."""
    seen = [f for f in answer["frames"] if f.get("present")]
    if len(seen) * 2 <= len(answer["frames"]):
        return None
    scale = (W, H, W, H)
    facing = [f["facing"] for f in seen]
    cx = [(f["head"][0] + f["head"][2]) / 2.0 / GRID for f in seen]
    return {
        "head": [statistics.median(f["head"][i] for f in seen) / GRID * scale[i]
                 for i in range(4)],
        "facing": max(set(facing), key=facing.count),
        # Majority: one frame claiming a face in a shot with none is the
        # hallucination this outvotes (gt9y-QGgMsA: shoulder boxed at 0.98).
        "face": sum(1 for f in seen if f.get("face_visible")) * 2 > len(seen),
        "spread": max(cx) - min(cx),
        "graphics": [[g[i] / GRID * scale[i] for i in range(4)]
                     for g in answer["graphics"]],
        "n": len(seen), "n_frames": len(answer["frames"]),
    }


# --- the geometry ----------------------------------------------------------

def size(W, H, style, scale):
    aw, ah = ASPECT[style]
    w = even(min(W, H * aw / float(ah)) * scale)
    return w, min(even(w * ah / float(aw)), H // 2 * 2)


def ideal_x(m, style, side, w):
    """Where the window's left edge puts him on his mark."""
    l, _, r, _ = m["head"]
    cap = CAPTION_W[style]
    if cap is None:
        return (l + r) / 2.0 - w / 2.0
    g = max((1.0 - (r - l) / w - cap) / 3.0, 0.0)
    return l - g * w if side == "right" else r - (1.0 - g) * w


def frame(m, style, side, x, y, w, h):
    """A candidate window, its caption anchor, and whether it works."""
    l, t, r, b = m["head"]
    s = {"x": x, "y": y, "w": w, "h": h, "side": side,
         "hl": (l - x) / float(w), "hr": (r - x) / float(w),
         "ht": (t - y) / float(h), "hb": (b - y) / float(h)}
    fits = s["hl"] >= 0 and s["hr"] <= 1 and s["ht"] >= 0
    cap = CAPTION_W[style]
    if cap is None:
        fits = fits and s["hb"] < MAX_FACE_BOTTOM
    else:
        free = (s["hl"], 0.0) if side == "left" else (1.0 - s["hr"], s["hr"])
        s["cap"] = free[1] + free[0] / 2.0
        fits = fits and free[0] >= cap
    s["fits"] = fits
    s["blocked"] = next((g for g in m["graphics"]
                         if x < g[2] + 6 and g[0] - 6 < x + w and
                         y < g[3] + 6 and g[1] - 6 < y + h), None)
    return s


def solve(W, H, m, style, side):
    """Largest, best-placed window that fits and clears every graphic."""
    _, t, _, b = m["head"]
    cy = (t + b) / 2.0
    best = None
    for i in range(int(round((1.0 - MIN_SCALE) / 0.02)) + 1):
        scale = 1.0 - 0.02 * i
        w, h = size(W, H, style, scale)
        y = max(min(cy - FACE_Y * h, t - HEADROOM * h), cy - 0.5 * h)
        y = min(max(even(y), 0), (H - h) // 2 * 2)
        want = ideal_x(m, style, side, w)
        for x in range(0, W - w + 1, 2):
            s = frame(m, style, side, x, y, w, h)
            s["scale"] = scale
            s["cost"] = abs(x - want) / w + ZOOM_COST[style] * (1.0 - scale)
            s["rank"] = (not s["fits"], s["blocked"] is not None, s["cost"])
            if best is None or s["rank"] < best["rank"]:
                best = s
    return best


def centred(W, H, style):
    """No reciter: the centred window, and no anchor key -> centred caption."""
    w, h = size(W, H, style, 1.0)
    return {"x": (W - w) // 4 * 2, "y": (H - h) // 4 * 2, "w": w, "h": h}


def x_offset(s, style):
    return int(round((s["cap"] - 0.5) * CANVAS_W[style]))


# --- report / annotate -----------------------------------------------------

def report(m, s, style):
    """Print the solve. -> [reasons not to write it]"""
    l, t, r, b = m["head"]
    print("head    : %d,%d -> %d,%d px, facing %s (median of %d/%d frames)"
          % (l, t, r, b, m["facing"], m["n"], m["n_frames"]))
    print("crop    : {x: %d, y: %d, w: %d, h: %d}   (%.2fx zoom)"
          % (s["x"], s["y"], s["w"], s["h"], 1.0 / s["scale"]))
    print("head    : %.3f-%.3f of window width, %.3f-%.3f of its height"
          % (s["hl"], s["hr"], s["ht"], s["hb"]))
    if CAPTION_W[style] is None:
        print("caption : under his head -> face_bottom %.3f" % s["hb"])
    else:
        cap = CAPTION_W[style]
        if s["side"] == "right":
            gaps = (s["hl"], s["cap"] - cap / 2 - s["hr"], 1 - s["cap"] - cap / 2)
        else:
            gaps = (s["cap"] - cap / 2, s["hl"] - s["cap"] - cap / 2, 1 - s["hr"])
        print("caption : %s, centre %.3f -> x_offset %d   gaps %.3f | %.3f | %.3f"
              % (s["side"], s["cap"], x_offset(s, style), *gaps))
    bad = []
    if not m["face"]:
        bad.append("no face visible in most frames -- the head box may be a "
                   "shoulder or the back of his head; hand-solve")
    if not s["fits"]:
        bad.append("no window holds his head with room for the caption")
    if s["blocked"]:
        g = s["blocked"]
        print("! window overlaps a burned-in graphic at %d,%d -> %d,%d px "
              "(furniture mislabelled? check --annotate)" % tuple(g))
    if m["spread"] > 0.06:
        print("! his head moves %.0f%% of the frame between samples: the shot "
              "cuts or pans, and one crop is wrong for it"
              % (100 * m["spread"]))
    for why in bad:
        print("! " + why)
    return bad


def annotate(path, s, m, style, W, out):
    from PIL import Image, ImageDraw
    im = Image.open(path).convert("RGB")
    k = im.size[0] / float(W)
    d = ImageDraw.Draw(im)

    def rect(x0, y0, x1, y1, colour, width=2):
        d.rectangle([x0 * k, y0 * k, x1 * k, y1 * k], outline=colour,
                    width=width)
    for g in m["graphics"]:
        rect(*g, colour=(255, 40, 40))
    rect(*m["head"], colour=(255, 220, 0))
    x, y, w, h = s["x"], s["y"], s["w"], s["h"]
    rect(x, y, x + w, y + h, (0, 255, 0), 3)
    cap = CAPTION_W[style]
    if cap is None:
        rect(x + 0.07 * w, y + s["hb"] * h, x + 0.93 * w, y + 0.9 * h,
             (80, 160, 255))
    else:
        c0 = x + (s["cap"] - cap / 2.0) * w
        rect(c0, y + 0.3 * h, c0 + cap * w, y + 0.7 * h, (80, 160, 255))
    im.save(out)
    print("annotate: %s" % out)


# --- the config ------------------------------------------------------------

MARK = "# framing solved by pipeline/crop.py"
KEYS = r"^(crop|x_offset|face_bottom)\s*:"


def block_lines(s, style, model, when, m):
    head = ["%s on %s, %s,\n" % (MARK, when, model)]
    if m is None:
        head.append("# no reciter in the sampled frames: centred window.\n")
        anchor = []
    else:
        head.append("# median of %d/%d sampled frames.\n" % (m["n"], m["n_frames"]))
        anchor = ["face_bottom: %.3f\n" % s["hb"]] if CAPTION_W[style] is None \
            else ["x_offset: %d\n" % x_offset(s, style)]
    return head + [
        "# Re-solve with `pipeline/crop.py <this-file> --write --force`. To\n",
        "# hand-set the keys, delete these comments: crop.py then refuses to\n",
        "# overwrite them without --force.\n",
        "crop: {x: %d, y: %d, w: %d, h: %d}\n" % (s["x"], s["y"], s["w"], s["h"]),
    ] + anchor


def write_config(path, new, force):
    """A line edit: yaml.safe_dump would eat every hand-written comment."""
    text = open(path, encoding="utf-8").read()
    lines = (text if text.endswith("\n") else text + "\n").splitlines(True)
    at = next((i for i, l in enumerate(lines) if l.startswith(MARK)), None)
    if at is not None:      # our previous block: marker, comments, keys
        end = i = at + 1
        while i < len(lines) and (lines[i].lstrip().startswith("#")
                                  or re.match(KEYS, lines[i])):
            if re.match(KEYS, lines[i]):
                end = i + 1
            i += 1
        del lines[at:end]
        if 0 < at < len(lines) and not lines[at].strip() \
                and not lines[at - 1].strip():
            del lines[at]
    stray = [i for i, l in enumerate(lines) if re.match(KEYS, l)]
    if stray and not force:
        raise SystemExit("%s line %d: hand-written `%s` -- pass --force to "
                         "replace it" % (path, stray[0] + 1,
                                         lines[stray[0]].split(":")[0]))
    for i in reversed(stray):
        del lines[i]
    # Under the verse span / trim, one blank line either side.
    after = [i for i, l in enumerate(lines) if re.match(
        r"^(style|signature|surah|ayah_start|ayah_end|trim)\s*:", l)]
    at = max(after) + 1 if after else len(lines)
    while at < len(lines) and not lines[at].strip():
        at += 1
    while at >= 2 and not lines[at - 1].strip() and not lines[at - 2].strip():
        del lines[at - 1]
        at -= 1
    lines[at:at] = (["\n"] if at and lines[at - 1].strip() else []) + new \
        + (["\n"] if at < len(lines) else [])
    open(path, "w", encoding="utf-8").write("".join(lines))


# --- entry point -----------------------------------------------------------

def run(config_path, frames=4, annotate_path=None, write=False, force=False):
    cfg = generate.resolve_paths(generate.load_config(config_path), config_path)
    style = cfg["style"]
    src = cfg["input"]
    info = generate.get_video_info(src)
    W, H = info["width"], info["height"]
    if not W:
        raise SystemExit("%s has no video stream" % src)
    t0, t1 = generate.trim_window(cfg)
    times = sample_times(t0, t1 if t1 is not None else info["duration"], frames)
    model = generate.envvar("QC_VISION_MODEL")
    if not model:
        raise SystemExit("set QC_VISION_MODEL in %s (see .env.example)"
                         % os.path.join(ROOT, ".env"))
    print("%s  %dx%d  %s, %d frames, %s"
          % (os.path.basename(config_path), W, H, style, len(times), model))

    # Keyed by everything that shapes the answer, so re-running to tune the
    # geometry is free and a new question never gets an old answer.
    cache_file = os.path.join(os.path.dirname(os.path.abspath(config_path)),
                              "crop.json")
    cache = json.load(open(cache_file)) if os.path.exists(cache_file) else {}
    key = "%s|%s|%s|%s" % (
        model, hashlib.sha256((PROMPT + str(FRAME_W)).encode()).hexdigest()[:12],
        os.path.relpath(src, ROOT), ",".join("%.2f" % t for t in times))
    tmp = os.path.join(cfg["tmp_dir"], "crop")
    paths = None
    entry = None if force else cache.get(key)
    if entry:
        print("      cached answer (%s) -- --force re-asks" % entry["when"])
    else:
        paths = extract(src, times, tmp, W)
        entry = {"answer": ask(paths, model),
                 "when": datetime.date.today().isoformat()}
        cache[key] = entry
        with open(cache_file, "w") as f:
            json.dump(cache, f, indent=1, sort_keys=True)
            f.write("\n")

    m = measure(entry["answer"], W, H)
    if m is None:
        s, bad = centred(W, H, style), []
        print("head    : no reciter in most frames -> centred window, no anchor")
        print("crop    : {x: %d, y: %d, w: %d, h: %d}"
              % (s["x"], s["y"], s["w"], s["h"]))
    else:
        if CAPTION_W[style] is None:
            sides = [None]
        elif m["facing"] == "frontal":
            sides = ["left", "right"]
        else:
            sides = [m["facing"]]       # caption on the side he faces
        s = min((solve(W, H, m, style, side) for side in sides),
                key=lambda c: c["rank"])
        bad = report(m, s, style)
        if annotate_path:
            mid = times[len(times) // 2]
            p = paths[len(paths) // 2] if paths else \
                extract(src, [mid], tmp, W)[0]
            annotate(p, s, m, style, W, annotate_path)

    if not write:
        print("(not written -- pass --write)")
    elif bad:
        raise SystemExit("NOT written: " + "; ".join(bad))
    else:
        write_config(config_path, block_lines(s, style, model, entry["when"], m),
                     force)
        print("wrote   : %s" % os.path.relpath(config_path, ROOT))
    return s


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Solve a reel's crop and caption anchor with a vision model.")
    p.add_argument("config", help="sources/<id>/<reel>.yaml")
    p.add_argument("--frames", type=int, default=4,
                   help="frames to sample and show the model (default 4)")
    p.add_argument("--annotate", metavar="PNG", help="write a labelled frame")
    p.add_argument("--write", action="store_true",
                   help="edit crop: and the style's anchor key into the config")
    p.add_argument("--force", action="store_true",
                   help="re-ask the model, and overwrite a hand-written crop")
    a = p.parse_args(argv)
    run(a.config, a.frames, a.annotate, a.write, a.force)


if __name__ == "__main__":
    main()
