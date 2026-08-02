"""pipeline/crop.py -- solve a reel's 16:9 framing at authoring time.

    tools/render-venv/bin/python pipeline/crop.py sources/<id>/<reel>.yaml
        [--frames 4] [--annotate out.png] [--write] [--force]
        [--dry-run] [--measurements FILE]

Samples frames, asks a vision model where the reciter is, computes the crop
with arithmetic, prints; `--write` edits `crop:` + `x_offset:` into the
config. `bars`/`hz` only (`default` uses YuNet at render).

Authoring only (invariant 4): model never consulted at render. Model returns
head/crown/shoulders; never a crop. Bad answer = wrong box on `--annotate`.

Horizontal (`targets()`, legacy equal-gap): caption = fixed-width column
(centre ~0.302); reciter = HEAD box including headwear, not body. Body is
containment only (clamped to BODY_FROM_FACE face widths of head centre);
`outer` is air beyond the HEAD — overhang is printed. Caption yields if
holding him puts text on his face.

Vertical: face at FACE_Y_FRAC; FACE_Y_BAND is printed scatter, not a refusal;
crown/MAX_HEADROOM wins when they conflict. `_reconcile()` ties crown_y /
face_cy to the head box before vertical().

Refuses: no face, off-frame caption. `--annotate` is the primary check —
geometry guards only check the model's numbers against each other.
"""
import argparse
import datetime
import json
import os
import re
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import generate  # noqa: E402  (config/path helpers only -- no Pillow)


# --- machine config (.env) -------------------------------------------------
# Copied, not imported. Every pipeline script carries its own reader so it
# stays runnable on its own; see CLAUDE.md's codebase map.

def _dotenv():
    """KEY=value pairs from ROOT/.env; process environment wins on conflict."""
    path = os.path.join(ROOT, ".env")
    out = {}
    if os.path.exists(path):
        for raw in open(path, encoding="utf-8"):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            k, sep, v = line.partition("=")
            if sep:
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                elif " #" in v:
                    v = v.split(" #", 1)[0].rstrip()
                out[k.strip()] = v
    return out


_ENV = None


def envvar(name, default=None):
    global _ENV
    if os.environ.get(name):
        return os.environ[name]
    if _ENV is None:
        _ENV = _dotenv()
    return _ENV.get(name) or default


# --- the owner's numbers ---------------------------------------------------

CAPTION_W = {"bars": 0.45,     # render_bars.TEXT["max_line_width_frac"]
             "hz": 0.504}      # 2 x render_hz CAPTION_HALF_W
CANVAS_W = {"bars": 1080, "hz": 1920}
MIN_GAP = 0.02
FACE_Y_FRAC = 0.275            # measured face centre in shipped windows
FACE_Y_BAND = (0.24, 0.34)     # shipped scatter; printed, never a refusal
# (model noise on this number is bigger than the band -- vqxYwdR4RvQ same
# four frames: 0.243 / 0.275 / 0.231). --annotate is the check.
MAX_HEADROOM = 0.08
FACE_IN_HEAD = (0.50, 0.85)    # face_cy below box top, in head heights
# Fixtures: upright 0.500-0.560 (median 0.528), bowed al-Ansari 0.759.
# Low edge = tightest real reading (keeps want off MAX_HEADROOM). High edge
# stays clear of 0.759 so bowed framing still prints outside the band.
MIN_CONF = 0.4
BODY_FROM_FACE = 2.6           # legacy crop.py:89, from HEAD centre
HEAD_FROM_FACE = 2.0           # legacy crop.py:90
# Fixtures reach 0.593-1.163 head widths a side; congregation hit 3.0.

# --- the model -------------------------------------------------------------
# `claude -p` over local auth; no API key. Prompt/schema/guards are the
# contract the arithmetic depends on.

MODEL = "sonnet"
FRAME_W = 1280                 # ~1.2k tokens/frame; 4K buys nothing here
EST_TOKENS_PER_FRAME = 1200
CTX = 1000000
GRID = 1000.0                  # prompt asks for 0-1000, not fractions
TIMEOUT = 180.0                # ~7.5s for 4 frames measured; headroom
RETRIES = 3                    # transient CLI / malformed parse; never silent
PASSES = 1
PROMPT = """You are framing a Qur'an recitation video for a vertical social \
media reel. I am showing you %d frames sampled from one continuous shot of the \
same recitation, in time order. Report, PER FRAME and in that same order, where \
the reciter is.

Each frame is %d by %d pixels, but give every coordinate NORMALISED TO A \
0-1000 GRID over the frame, as a whole number: x is 0 at the left edge and \
1000 at the right edge, y is 0 at the top edge and 1000 at the bottom edge, so \
a width of 1000 is the whole frame width and a height of 500 is half the frame \
height.

The reciter is the man leading the recitation: nearest the camera, largest in \
frame, usually the only one speaking. Worshippers praying or seated behind him \
are NOT the reciter and must never widen any of his boxes.

For each frame report:

  reciter_present  false if he is out of frame, cut away from, or completely \
occluded. The other fields are then ignored, so any value will do.
  face_visible     true ONLY if you can actually SEE facial features in this \
frame -- an eye, the brow, the nose, the mouth, the line of the cheek or jaw. \
It is false when his head is turned away from the camera, when it is bowed far \
enough that the cloth hides everything, when a ghutrah or shemagh is drawn \
across his face, when a mic or its windscreen covers it, and whenever the shot \
is the back or the top of his head. Answer for THIS frame and answer honestly: \
"false" costs nothing here, it only routes the shot to a human, whereas a \
"true" you cannot support makes every other number below a guess about a face \
that is not there. If you are unsure, answer false. Still fill in head, \
crown_y and the rest from the head's outline as best you can -- they are used \
to show a person the shot, not to crop it.
  head             his head's bounding box INCLUDING all headwear -- keffiyeh, \
ghutrah, shemagh, turban, imamah, taqiyah, hair. The box a hat-wearing head \
fills: from the top of the cloth to the bottom of the chin or beard, and only \
as wide as the head and its cloth. It must NOT reach down to his shoulders or \
out over his robe, and it must NOT include a microphone, a windscreen or a \
boom arm, even when one stands directly in front of his face. The head ends \
where the cloth ends. cx and cy are its CENTRE.
  crown_y          the y of the very topmost pixel of that head, cloth \
included -- the same value as head cy minus half of head h.
  face_cy          the vertical centre of the FACE itself, halfway between the \
eyebrows and the chin. On a reciter who bows his head this sits well below the \
centre of the head box. Report both honestly; the difference matters.
  body_left        the leftmost x of HIS shoulders and torso, robe included.
  body_right       the rightmost x of the same. Not his outstretched hands, not \
a person beside him, not a lectern or a rail.
  facing           WHICH WAY HIS NOSE POINTS ON THE SCREEN, not his own left \
and right: "left" if his face points toward the left edge of the picture \
(toward x=0), "right" if it points toward the right edge (toward x=1), \
"frontal" if he is square to the camera and you can see both of his cheeks. A \
man photographed from his own right side is facing "left" here.
  posture          "upright", "bowed" (rukuu', or reciting with the head \
lowered over a mushaf), or "prostrate" (sujuud).
  headroom_frac    how much empty space should sit above his crown in the final \
crop -- the ONLY fraction here, given as a fraction of the crop's height. \
Reference reels run 0.00 to 0.08.
  obstructions     GRAPHICS BURNED INTO THE VIDEO, and nothing else: channel \
logos, station watermarks, social-media handles, lower-thirds and name plates, \
hard subtitles, tickers, timestamps, URLs. Things that are part of the picture \
file rather than part of the room. They are usually crisp, flat, brightly \
outlined, and identical in every frame. Check all four corners and the whole \
bottom strip before answering: a broadcast of a mosque normally carries two to \
four of them, a logo in one top corner and a name plate or a row of social \
handles along the bottom. Box each one.
                   A microphone, a mic boom or stand, a pillar, a lamp, a \
chandelier, a mushaf stand, a curtain, a rail, a doorway, a dark window, a \
piece of carved wall or a person is NOT an obstruction, however much of the \
frame it crosses, and however rectangular it looks. If it is part of the room \
it is not a graphic. Report an empty list when there are no graphics. An \
earlier version of this tool read the furniture as graphics and could then find \
no legal crop at all, so an invented box is far worse than a missed one.
  confidence       0 to 1, how sure you are of THIS frame's boxes.

Answer with the JSON object the schema describes, and nothing else."""

_BOX = {"type": "object", "additionalProperties": False,
        "properties": {k: {"type": "number"} for k in ("x", "y", "w", "h")},
        "required": ["x", "y", "w", "h"]}

# strict: true demands additionalProperties false and every property listed in
# required, at every level -- an optional key is a schema error, not an
# omission.
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"frames": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "reciter_present": {"type": "boolean"},
            "face_visible": {"type": "boolean"},
            "head": {"type": "object", "additionalProperties": False,
                     "properties": {k: {"type": "number"}
                                    for k in ("cx", "cy", "w", "h")},
                     "required": ["cx", "cy", "w", "h"]},
            "crown_y": {"type": "number"},
            "face_cy": {"type": "number"},
            "body_left": {"type": "number"},
            "body_right": {"type": "number"},
            "facing": {"type": "string", "enum": ["left", "right", "frontal"]},
            "posture": {"type": "string",
                        "enum": ["upright", "bowed", "prostrate"]},
            "headroom_frac": {"type": "number"},
            "obstructions": {"type": "array", "items": _BOX},
            "confidence": {"type": "number"},
        },
        "required": ["reciter_present", "face_visible", "head", "crown_y",
                     "face_cy", "body_left", "body_right", "facing", "posture",
                     "headroom_frac", "obstructions", "confidence"],
    }}},
    "required": ["frames"],
}


# --- sampling --------------------------------------------------------------

def sample_times(t0, t1, n):
    """n timestamps spread across [t0, t1], inset from both ends.

    Inset because the first and last seconds of a reel's window are where a
    fade, a bow or a camera change lives, and a frame taken there answers about
    a moment the reel barely shows."""
    lo, hi = t0 + 0.06 * (t1 - t0), t0 + 0.94 * (t1 - t0)
    if n == 1:
        return [(lo + hi) / 2.0]
    return [lo + (hi - lo) * i / (n - 1.0) for i in range(n)]


def extract(src, times, tmp, W, H):
    """One JPEG per timestamp, scaled to FRAME_W wide. -> ([paths], (w, h))"""
    ff = envvar("QC_FFMPEG", "ffmpeg")
    os.makedirs(tmp, exist_ok=True)
    fw = min(FRAME_W, W)
    fh = int(round(H * fw / float(W) / 2.0)) * 2
    out = []
    for i, t in enumerate(times):
        p = os.path.join(tmp, "crop-%02d.jpg" % i)
        subprocess.run(
            [ff, "-y", "-v", "error", "-ss", "%.3f" % t, "-i", src,
             "-frames:v", "1", "-vf",
             "scale=%d:%d:flags=lanczos" % (fw, fh), "-q:v", "3", p],
            check=True)
        out.append(p)
    return out, (fw, fh)


def to_fractions(parsed):
    """0-1000 grid -> frame fractions. The model answers in that grid; asking
    for 0..1 or pixels mis-scales (measured on vqxYwdR4RvQ)."""
    for f in parsed.get("frames") or []:
        if not isinstance(f, dict):
            continue
        for k in ("body_left", "body_right", "crown_y", "face_cy"):
            if isinstance(f.get(k), (int, float)):
                f[k] = f[k] / GRID
        h = f.get("head")
        if isinstance(h, dict):
            for k in ("cx", "cy", "w", "h"):
                if isinstance(h.get(k), (int, float)):
                    h[k] = h[k] / GRID
        for b in f.get("obstructions") or []:
            if isinstance(b, dict):
                for k in ("x", "y", "w", "h"):
                    if isinstance(b.get(k), (int, float)):
                        b[k] = b[k] / GRID
    return parsed


# --- the model call --------------------------------------------------------

def ask(paths, dims, cwd):
    """One `claude -p` call. -> (parsed, envelope). Paths named in the prompt;
    CLI reads the JPEGs off disk."""
    listing = "\n".join("  frame %d: %s" % (i, p) for i, p in enumerate(paths))
    prompt = (PROMPT % (len(paths), dims[0], dims[1])
              + "\n\nThe frames, in the same time order, are these image "
                "files on disk -- read each one before answering:\n" + listing)
    cli = envvar("QC_CLAUDE", "claude")
    cmd = [cli, "-p", prompt, "--model", MODEL, "--output-format", "json",
           "--allowedTools", "Read", "--strict-mcp-config",
           "--json-schema", json.dumps(SCHEMA)]
    # Read only; no MCP; schema cuts common malformations; parse() still walks.
    last_err = None
    for attempt in range(RETRIES):
        if attempt:
            time.sleep(2.0)
        try:
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True,
                                   text=True, timeout=TIMEOUT)
        except FileNotFoundError:
            # Not a transient failure -- retrying won't install the CLI.
            raise SystemExit(
                "%r is not on PATH. Install the Claude Code CLI and sign in "
                "(`claude auth login`), or point QC_CLAUDE at its binary in "
                "%s." % (cli, os.path.join(ROOT, ".env")))
        except subprocess.TimeoutExpired:
            last_err = "claude -p timed out after %.0fs" % TIMEOUT
        else:
            if proc.returncode != 0:
                last_err = ("claude -p exited %d: %s" % (
                    proc.returncode, (proc.stderr or proc.stdout)[:400]))
            else:
                try:
                    envelope = json.loads(proc.stdout)
                except ValueError as exc:
                    last_err = ("claude -p did not print its JSON envelope "
                                "(%s): %s" % (exc, proc.stdout[:400]))
                else:
                    if envelope.get("is_error"):
                        last_err = ("claude -p reported an error: %s"
                                    % json.dumps(envelope)[:400])
                    else:
                        try:
                            parsed = parse(envelope.get("result"))
                        except ValueError as exc:
                            last_err = str(exc)
                        else:
                            return parsed, envelope
        print("      %s (attempt %d/%d)%s"
              % (last_err, attempt + 1, RETRIES,
                 "" if attempt == RETRIES - 1 else ", retrying"),
              file=sys.stderr)
    raise SystemExit("claude -p failed after %d attempts: %s"
                     % (RETRIES, last_err))


def parse(text):
    """JSON matching SCHEMA, or ValueError (ask() retries). Envelope is JSON;
    `result` is not -- strip fence, take first {...}, walk SCHEMA."""
    s = (text or "").strip()
    fence = re.search(r"```(?:[a-zA-Z]*)\s*\n(.*?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    start = s.find("{")
    if start == -1:
        raise ValueError("model output has no JSON object at all: %s"
                         % s[:400])
    s = _first_json_object(s[start:])
    try:
        out = json.loads(s)
    except ValueError as exc:
        raise ValueError("model output did not parse as JSON (%s): %s"
                         % (exc, s[:400]))
    problems = _validate(SCHEMA, out, "response")
    if problems:
        raise ValueError("model output failed schema validation: %s (%s)"
                         % ("; ".join(problems[:6]), s[:200]))
    return out


def _first_json_object(s):
    """First balanced {...}; drops trailing prose after the object."""
    depth = 0
    for i, ch in enumerate(s):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[:i + 1]
    raise ValueError("no balanced JSON object found: %s" % s[:400])


def _validate(schema, value, path):
    """Required fields + types. Extra keys ignored (consumers read by name)."""
    t = schema.get("type")
    problems = []
    if t == "object":
        if not isinstance(value, dict):
            return ["%s: expected an object, got %r" % (path, value)]
        for req in schema.get("required", []):
            if req not in value:
                problems.append("%s.%s: missing" % (path, req))
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                problems += _validate(props[k], v, "%s.%s" % (path, k))
    elif t == "array":
        if not isinstance(value, list):
            return ["%s: expected an array, got %r" % (path, value)]
        items = schema.get("items")
        if items:
            for i, item in enumerate(value):
                problems += _validate(items, item, "%s[%d]" % (path, i))
    elif t == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            problems.append("%s: expected a number, got %r" % (path, value))
    elif t == "boolean":
        if not isinstance(value, bool):
            problems.append("%s: expected a boolean, got %r" % (path, value))
    elif t == "string":
        if not isinstance(value, str):
            problems.append("%s: expected a string, got %r" % (path, value))
        elif "enum" in schema and value not in schema["enum"]:
            problems.append("%s: %r not one of %s"
                            % (path, value, schema["enum"]))
    return problems


# --- medianing -------------------------------------------------------------

_NUM = ("crown_y", "face_cy", "body_left", "body_right", "headroom_frac")


def usable(f):
    """Is this frame a vote? A dropped frame is louder than a wrong one."""
    if not isinstance(f, dict) or not f.get("reciter_present"):
        return False
    if float(f.get("confidence") or 0.0) < MIN_CONF:
        return False
    h = f.get("head") or {}
    if not all(isinstance(h.get(k), (int, float))
               for k in ("cx", "cy", "w", "h")):
        return False
    return all(isinstance(f.get(k), (int, float)) for k in _NUM)


def median(frames):
    """Median every number across the usable frames. -> measurements dict.

    Median-of-N is the whole defence against an imprecise box: a VLM's head cx
    is good to a few percent and 3% of frame width is visible, but the errors
    are not correlated between frames the way a systematic bias would be."""
    keep = [f for f in frames if usable(f)]
    if not keep:
        raise SystemExit(
            "no usable frame: the model saw no reciter (or was under %.2f "
            "confident) in all %d. Check the sampled frames, or the trim window."
            % (MIN_CONF, len(frames)))
    med = {k: statistics.median([float(f[k]) for f in keep]) for k in _NUM}
    for k in ("cx", "cy", "w", "h"):
        med["head_" + k] = statistics.median([float(f["head"][k]) for f in keep])
    med["facing"] = _vote([f.get("facing") for f in keep], "frontal")
    med["posture"] = _vote([f.get("posture") for f in keep], "upright")
    # Majority, not any(): one pass claiming a face in a shot that has none is
    # exactly the hallucination this field exists to outvote. A frame that
    # omitted the key counts as "no face" -- absent evidence is not evidence.
    med["face_seen"] = sum(1 for f in keep if f.get("face_visible") is True)
    med["face_visible"] = med["face_seen"] * 2 > len(keep)
    med["confidence"] = statistics.median([float(f["confidence"]) for f in keep])
    med["obstructions"] = _boxes([f.get("obstructions") or [] for f in keep])
    med["n"], med["n_frames"] = len(keep), len(frames)
    # The spread is the fixed-camera test: this whole file assumes one window
    # frames the whole reel, which is false if the shot cuts or the camera
    # moves. Reported, never silently averaged away.
    med["spread_cx"] = _spread([float(f["head"]["cx"]) for f in keep])
    med["spread_cy"] = _spread([float(f["head"]["cy"]) for f in keep])
    return _reconcile(med)


def _reconcile(m):
    """Tie crown_y / face_cy to the head box; keep raws for report()."""
    top = m["head_cy"] - m["head_h"] / 2.0
    lo = top + FACE_IN_HEAD[0] * m["head_h"]
    hi = top + FACE_IN_HEAD[1] * m["head_h"]
    m["crown_reported"], m["crown_y"] = m["crown_y"], top
    m["face_reported"] = m["face_cy"]
    m["face_cy"] = min(max(m["face_cy"], lo), hi)
    return m


def _vote(values, fallback):
    values = [v for v in values if v]
    if not values:
        return fallback
    return max(set(values), key=values.count)


def _spread(v):
    return (max(v) - min(v)) if len(v) > 1 else 0.0


def _boxes(per_frame, min_share=0.5):
    """Boxes in >= half the frames, unioned. One-frame hits are dropped."""
    clusters = []
    for boxes in per_frame:
        for b in boxes:
            if not all(isinstance(b.get(k), (int, float))
                       for k in ("x", "y", "w", "h")):
                continue
            for c in clusters:
                if _overlaps(c["box"], b):
                    c["box"] = _union(c["box"], b)
                    c["n"] += 1
                    break
            else:
                clusters.append({"box": dict(b), "n": 1})
    need = max(1, int(round(min_share * len(per_frame))))
    return [c["box"] for c in clusters if c["n"] >= need]


def _overlaps(a, b):
    return (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and
            a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])


def _union(a, b):
    x0, y0 = min(a["x"], b["x"]), min(a["y"], b["y"])
    x1 = max(a["x"] + a["w"], b["x"] + b["w"])
    y1 = max(a["y"] + a["h"], b["y"] + b["h"])
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


# --- the geometry (no model involved) --------------------------------------

def targets(style, side, block):
    """Equal-gap centres (fractions of the output window). `block` = head
    width / window. Same rule for bars and hz."""
    cap_w = CAPTION_W[style]
    gaps = 1.0 - block - cap_w
    outer = gaps / 3.0
    if outer < MIN_GAP:
        outer, inner = 0.0, max(gaps / 2.0, MIN_GAP)
    else:
        inner = outer
    fx = outer + block / 2.0
    cap = outer + block + inner + cap_w / 2.0
    if side == "left":                    # caption LEFT -> reciter RIGHT
        return 1.0 - fx, 1.0 - cap, outer, inner
    return fx, cap, outer, inner


def caption_side(m):
    """Caption on the side he faces; square-on -> opposite his head."""
    if m["facing"] in ("left", "right"):
        return m["facing"]
    return "left" if m["head_cx"] > 0.5 else "right"


def vertical(H, h, m):
    """-> (y, lo, hi, ok). Obstruction search may only move y inside [lo, hi].
    Crown wins when the band and crown disagree (bowed reciter)."""
    crown = m["crown_y"] * H
    face = m["face_cy"] * H
    lo = max(face - FACE_Y_BAND[1] * h, crown - MAX_HEADROOM * h, 0.0)
    hi = min(face - FACE_Y_BAND[0] * h, crown, float(H - h))
    want = face - FACE_Y_FRAC * h
    ok = lo <= hi
    if not ok:
        lo = max(crown - MAX_HEADROOM * h, 0.0)
        hi = max(min(crown, float(H - h)), lo)
    lo, hi = max(0.0, lo), min(float(H - h), hi)
    hi = max(hi, lo)
    y = int(round(min(max(want, lo), hi) / 2.0)) * 2
    return max(0, min(y, (H - h) // 2 * 2)), lo, hi, ok


def body_edges(W, m):
    """Silhouette in source px, clamped to BODY_FROM_FACE of head centre.
    Never used by targets() -- containment only."""
    head_cx = m["head_cx"] * W
    reach = (BODY_FROM_FACE / HEAD_FROM_FACE) * m["head_w"] * W
    raw_l, raw_r = m["body_left"] * W, m["body_right"] * W
    left = max(raw_l, head_cx - reach, 0.0)
    right = min(raw_r, head_cx + reach, float(W))
    return left, right, (left > raw_l + 0.5 or right < raw_r - 0.5)


def horizontal(W, w, m, side, cap_cx, cap_w):
    """-> (lo, hi, c_lo, c_hi, contained). Caption clearance first, then
    contain the body (yields when holding him puts text on his face)."""
    bl, br, _ = body_edges(W, m)
    lo, hi = 0.0, float(W - w)
    head_l = (m["head_cx"] - m["head_w"] / 2.0) * W
    head_r = (m["head_cx"] + m["head_w"] / 2.0) * W
    if side == "left":                # caption LEFT: his head must stay right
        hi = min(hi, head_l - (cap_cx + cap_w / 2.0) * w)
    else:                             # caption RIGHT: his head must stay left
        lo = max(lo, head_r - (cap_cx - cap_w / 2.0) * w)
    lo = max(0.0, min(lo, float(W - w)))
    hi = max(lo, min(hi, float(W - w)))
    c_lo, c_hi = br - w, bl           # contain right edge / contain left edge
    if max(lo, c_lo) <= min(hi, c_hi):
        return max(lo, c_lo), min(hi, c_hi), c_lo, c_hi, True
    return lo, hi, c_lo, c_hi, False


def place(W, H, m, style, side, scale):
    """The window at one zoom level, before obstruction avoidance."""
    base_w = min(float(W), H * 16.0 / 9.0)
    w = int(round(base_w * scale / 2.0)) * 2
    h = int(round(w * 9.0 / 16.0 / 2.0)) * 2
    if w > W or h > H or w < 160:
        return None
    head_w = m["head_w"] * W
    body_l, body_r, clamped = body_edges(W, m)
    block = min(head_w / float(w), 1.0)
    fx_t, cap_cx, outer, inner = targets(style, side, block)
    x_lo, x_hi, c_lo, c_hi, contained = horizontal(W, w, m, side, cap_cx,
                                                   CAPTION_W[style])
    # Even both ends before clamping, so rounding x to an even px inside the
    # band cannot land 1 px back outside it and re-open the cut. The band is
    # then the ONLY thing bounding x, so it carries the frame limit too.
    lim = float((W - w) // 2 * 2)
    x_lo = min(max(0.0, 2.0 * int(-(-x_lo // 2.0))), lim)
    x_hi = min(max(x_lo, 2.0 * int(x_hi // 2.0)), lim)
    gap_x = (m["head_cx"] * W - fx_t * w)      # what the gap rule alone wants
    x = int(round(min(max(gap_x, x_lo), x_hi) / 2.0)) * 2
    y, y_lo, y_hi, y_ok = vertical(H, h, m)
    s = {"x": x, "y": y, "w": w, "h": h, "scale": scale, "y_lo": y_lo,
         "y_hi": y_hi, "block": block,
         "body_frac": max(body_r - body_l, 0.0) / float(w),
         "body_l": body_l, "body_r": body_r, "body_clamped": clamped,
         "x_lo": x_lo, "x_hi": x_hi, "c_lo": c_lo, "c_hi": c_hi,
         "contained": contained, "gap_x": gap_x,
         "fx_target": fx_t, "caption_cx": cap_cx, "outer": outer,
         "inner": inner, "y_ok": y_ok}
    return shift(s, W, H, m, 0, 0)


def shift(sol, W, H, m, dx, dy):
    """The same window moved, with its errors recomputed.

    x is confined to the band horizontal() returned and y to vertical()'s --
    see there for why."""
    s = dict(sol)
    x = min(max(sol["x"] + dx, sol["x_lo"]), sol["x_hi"])
    s["x"] = int(min(max(round(x / 2.0) * 2, sol["x_lo"]), sol["x_hi"]))
    y = min(max(sol["y"] + dy, sol["y_lo"]), sol["y_hi"])
    s["y"] = max(0, min(int(round(y / 2.0)) * 2, (H - sol["h"]) // 2 * 2))
    s["fx"] = (m["head_cx"] * W - s["x"]) / float(s["w"])
    s["fy"] = (m["face_cy"] * H - s["y"]) / float(s["h"])
    s["headroom"] = (m["crown_y"] * H - s["y"]) / float(s["h"])
    # Signed, in window widths: negative = this edge cuts through him.
    s["margin_l"] = (sol["body_l"] - s["x"]) / float(s["w"])
    s["margin_r"] = (s["x"] + s["w"] - sol["body_r"]) / float(s["w"])
    return s


def cost(s):
    return (3.0 * abs(s["fx"] - s["fx_target"])
            + 2.0 * abs(s["fy"] - FACE_Y_FRAC) + 0.5 * (1.0 - s["scale"]))


def hits(s, boxes, W, H, pad=6):
    """The first obstruction box the window overlaps, or None."""
    for b in boxes:
        bx, by = b["x"] * W, b["y"] * H
        bw, bh = b["w"] * W, b["h"] * H
        if (s["x"] < bx + bw + pad and bx - pad < s["x"] + s["w"] and
                s["y"] < by + bh + pad and by - pad < s["y"] + s["h"]):
            return b
    return None


def solve(W, H, m, style, side):
    """Largest 16:9 window that frames him and clears graphics. Zoom is last
    resort. Containment is inside horizontal()'s band per zoom -- never a
    term between zoom levels (tier/cost both break a shipped reel)."""
    boxes = m["obstructions"]
    best, best_forced = None, None
    for i in range(26):
        s = place(W, H, m, style, side, 1.0 - 0.02 * i)
        if s is None:
            continue
        step = max(8, s["w"] // 100)
        for dx in _offsets(int(0.16 * s["w"]), step):
            for dy in _offsets(int(0.05 * s["h"]), step):
                c = shift(s, W, H, m, dx, dy)
                c["cost"] = cost(c)
                c["blocked_by"] = hits(c, boxes, W, H)
                c["rank"] = (0 if c["blocked_by"] is None else 1, c["cost"])
                # 0.16 gate on the gap rule; containment can pin past it.
                if best_forced is None or c["rank"] < best_forced["rank"]:
                    best_forced = c
                if abs(c["fx"] - c["fx_target"]) > 0.16:
                    continue
                if best is None or c["rank"] < best["rank"]:
                    best = c
        if best is not None and best["rank"][0] == 0 and \
                s["scale"] > 0.92 and \
                abs(best["fx"] - best["fx_target"]) < 0.02:
            break
    if best is None:
        best = best_forced
    if best is None:
        raise SystemExit("no 16:9 window fits inside %dx%d" % (W, H))
    return best


def _offsets(limit, step):
    """0, +step, -step, +2*step ... -- nearest-first so ties keep the ideal."""
    out = [0]
    k = step
    while k <= limit:
        out += [k, -k]
        k += step
    return out


def x_offset_px(sol, style):
    """The caption anchor as generate.py's generic `x_offset`, in canvas px.

    Not a new config key: bars already draws its pills at BAND_W/2 + x_offset,
    so the anchor IS this number. The bars golden uses -216 on a 1080 canvas,
    which is 0.30 W."""
    return int(round((sol["caption_cx"] - 0.5) * CANVAS_W[style]))


# --- reporting -------------------------------------------------------------

def report(m, sol, style, W, H):
    """Print the solve, and every reason it might be wrong. -> [problems]"""
    bad = []
    print("head    : cx %.3f cy %.3f  %.3f x %.3f W/H  (median of %d/%d frames, "
          "confidence %.2f)" % (m["head_cx"], m["head_cy"], m["head_w"],
                                m["head_h"], m["n"], m["n_frames"],
                                m["confidence"]))
    print("          face visible in %d/%d frames%s"
          % (m.get("face_seen", 0), m["n"],
             "" if m.get("face_visible", True) else "  <- NO FACE, see below"))
    print("          facing %s, posture %s; body %.3f..%.3f W; crown %.3f H"
          % (m["facing"], m["posture"], m["body_left"], m["body_right"],
             m["crown_y"]))
    print("          head spread across frames: cx %.3f W, cy %.3f H"
          % (m["spread_cx"], m["spread_cy"]))
    was = (m["face_reported"] - m["crown_reported"]) / m["head_h"]
    now = (m["face_cy"] - m["crown_y"]) / m["head_h"]
    if abs(was - now) > 0.005:
        print("          reconciled to the head box: crown %.3f -> %.3f H, "
              "face %.3f -> %.3f H; the face was reported %.2f head heights "
              "below the crown and the pixels say %.2f-%.2f, so %.2f is used."
              % (m["crown_reported"], m["crown_y"], m["face_reported"],
                 m["face_cy"], was, FACE_IN_HEAD[0], FACE_IN_HEAD[1], now))
    if max(m["spread_cx"], m["spread_cy"]) > 0.06:
        print("! the head moves more than 6%% of frame between samples -- this "
              "shot cuts or the camera pans, and ONE crop is wrong for it. "
              "Check the sampled frames before writing.", file=sys.stderr)
    if m["posture"] != "upright":
        print("! posture is %r. The 0.275 face rule is a PROXY for headroom and "
              "it fails on a reciter who bows -- check the top of his head is "
              "in frame on --annotate, not just the number below."
              % m["posture"], file=sys.stderr)
    if sol["body_clamped"]:
        print("! body came back reaching further than %.1f face widths from "
              "the centre of his head (%.3f..%.3f W measured, %.3f..%.3f W "
              "used). That is the 9Yci0oWB2fE failure: the model is measuring "
              "the congregation, not the reciter -- check --annotate."
              % (BODY_FROM_FACE, m["body_left"], m["body_right"],
                 sol["body_l"] / float(W), sol["body_r"] / float(W)),
              file=sys.stderr)

    print("crop    : {x: %d, y: %d, w: %d, h: %d}   (%.2fx zoom of %dx%d)"
          % (sol["x"], sol["y"], sol["w"], sol["h"], W / float(sol["w"]), W, H))
    print("head    : x %.3f of window (target %.3f), y %.3f  [block %.3f W, "
          "body %.3f W]" % (sol["fx"], sol["fx_target"], sol["fy"],
                            sol["block"], sol["body_frac"]))
    print("gaps    : outer %.3f | caption %.3f | inner %.3f | block %.3f | "
          "outer %.3f" % (sol["outer"], CAPTION_W[style], sol["inner"],
                          sol["block"], sol["outer"]))
    print("face    : %.3f of window height (rule %.3f, band %.2f-%.2f); "
          "headroom %.3f (model asked %.3f)"
          % (sol["fy"], FACE_Y_FRAC, FACE_Y_BAND[0], FACE_Y_BAND[1],
             sol["headroom"], m["headroom_frac"]))
    # Before the band check, because the band is a statement ABOUT a face and
    # is meaningless when there is not one. Every other guard here compares the
    # model's numbers with each other, so all of them pass on a shot where it
    # boxed something that is not his head: on gt9y-QGgMsA (head fully draped,
    # turned away) it boxed his shoulder and returned confidence 0.98, zero
    # spread across 12 frames and a face at exactly 0.275 -- a window whose top
    # edge sat 130px BELOW his real crown. Nothing downstream could have caught
    # that; only "is there a face at all" can.
    if not m.get("face_visible", True):
        bad.append("no face is visible in %d of %d frames"
                   % (m["n"] - m.get("face_seen", 0), m["n"]))
        print("! NO FACE IS VISIBLE in this shot (%d of %d frames). Every "
              "number above is then a box drawn round something that is not "
              "his face, and it will still look self-consistent -- confident, "
              "steady between frames, landing inside the band. Do not trust "
              "it and do not re-run for a better answer. HAND-SOLVE the crop: "
              "measure his crown, his body edges, where the congregation and "
              "any burned-in graphics start over several frames, put his head "
              "centre at %.3f of the window, and write crop:/x_offset: "
              "yourself with the reasoning in a comment."
              % (m["n"] - m.get("face_seen", 0), m["n"], sol["fx_target"]),
              file=sys.stderr)
    if not sol["y_ok"]:
        # FACE_Y_BAND miss: printed only; config still written.
        print("! face lands at %.3f of the window, outside the %.2f-%.2f the "
              "shipped reels span. Not fatal -- check --annotate before "
              "trusting it, especially on a bowed reciter (see the al-Ansari "
              "clips), but the config is written regardless."
              % (sol["fy"], FACE_Y_BAND[0], FACE_Y_BAND[1]), file=sys.stderr)
    # `outer` is air beyond the HEAD — can look healthy while cutting his back.
    print("body    : %.3f..%.3f of window; margin left %+.3f, right %+.3f "
          "(%.3f W of frame, clamped to %.1f face widths of head centre)"
          % ((sol["body_l"] - sol["x"]) / float(sol["w"]),
             (sol["body_r"] - sol["x"]) / float(sol["w"]),
             sol["margin_l"], sol["margin_r"], sol["body_frac"] * sol["w"] / W,
             BODY_FROM_FACE))
    edge, want = None, None
    # The bound also has to be INSIDE the frame, or the frame edge is what
    # actually moved the window and containment would be taking the credit.
    if sol["contained"]:
        if sol["gap_x"] < sol["c_lo"] - 1.0 and sol["c_lo"] > 0.0:
            edge, want = "right", sol["c_lo"]
        elif sol["gap_x"] > sol["c_hi"] + 1.0 and sol["c_hi"] < W - sol["w"]:
            edge, want = "left", sol["c_hi"]
    if edge:
        print("          the %s edge is set by CONTAINMENT, not the gap rule: "
              "equal gaps wanted x=%d, his silhouette needs x=%d, so the "
              "window moved %d px."
              % (edge, int(round(sol["gap_x"])), int(round(want)),
                 abs(int(round(want - sol["gap_x"])))))
    if min(sol["margin_l"], sol["margin_r"]) < 0.0:
        print("! this window CUTS HIM: %s edge, %d px of silhouette outside "
              "the crop. At this zoom no window holds him that also keeps the "
              "caption column off his head, so he bleeds off that edge -- the "
              "two-gap branch of the rule, which the shipped BADR-AL-TURKI and "
              "ABDULLAH-AL-JUHANY reels both do. Confirm on --annotate that it "
              "reads as framing and not as an accident."
              % ("left" if sol["margin_l"] < sol["margin_r"] else "right",
                 int(round(abs(min(sol["margin_l"], sol["margin_r"]))
                           * sol["w"]))), file=sys.stderr)
    if abs(sol["fx"] - sol["fx_target"]) > 0.02 and not edge:
        print("! the source cannot pan far enough: he wants to be at %.3f of "
              "the window and can only reach %.3f. A tighter window would get "
              "there at the cost of resolution."
              % (sol["fx_target"], sol["fx"]), file=sys.stderr)

    cap = sol["caption_cx"]
    print("caption : centre %.3f of window -> x_offset %d px on the %d-wide %s "
          "canvas" % (cap, x_offset_px(sol, style), CANVAS_W[style], style))
    if not 0.0 < cap < 1.0:
        bad.append("caption centre %.3f is off the frame" % cap)
        print("! caption centre %.3f is OFF THE FRAME. This is the -0.105 "
              "failure in sources/9Yci0oWB2fE/meta.yaml -- the block width is "
              "wrong, not the formula." % cap, file=sys.stderr)
    if m["obstructions"]:
        print("graphics: %d burned-in box(es) seen in most frames:"
              % len(m["obstructions"]))
        for b in m["obstructions"]:
            print("          x %.3f..%.3f  y %.3f..%.3f"
                  % (b["x"], b["x"] + b["w"], b["y"], b["y"] + b["h"]))
    if sol.get("blocked_by"):
        b = sol["blocked_by"]
        print("! no window clears the graphic at x %.3f..%.3f y %.3f..%.3f; "
              "this is the best window IGNORING it. Either it is furniture the "
              "model mislabelled (check --annotate), or accept the logo, or "
              "hand-solve." % (b["x"], b["x"] + b["w"], b["y"], b["y"] + b["h"]),
              file=sys.stderr)
    return bad


def annotate(frame_path, sol, m, style, out_path):
    """Draw the solve over a real frame. Pillow, because the pipeline has it."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(frame_path).convert("RGB")
    # The frame was scaled for the model; the solve is in source px.
    W, H = im.size
    sx, sy = W / float(m["_W"]), H / float(m["_H"])
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.load_default(size=max(11, W // 80))
    except TypeError:                     # Pillow < 10.1: fixed-size bitmap
        font = ImageFont.load_default()

    def box(x0, y0, x1, y1, colour, width=2):
        d.rectangle([x0 * sx, y0 * sy, x1 * sx, y1 * sy], outline=colour,
                    width=width)

    for b in m["obstructions"]:           # red: burned-in graphics
        box(b["x"] * m["_W"], b["y"] * m["_H"], (b["x"] + b["w"]) * m["_W"],
            (b["y"] + b["h"]) * m["_H"], (255, 40, 40))
    # cyan: the body extent the solver CONTAINED (clamped), dim cyan: what the
    # model actually said. Two lines because when they differ the clamp fired,
    # and that is the congregation case you want to see rather than read.
    for f in (m["body_left"], m["body_right"]):
        d.line([f * W, 0, f * W, H], fill=(0, 110, 110), width=1)
    for px in (sol["body_l"], sol["body_r"]):
        d.line([px * sx, 0, px * sx, H], fill=(0, 230, 230), width=2)
    hx0 = (m["head_cx"] - m["head_w"] / 2.0) * W    # yellow: head box
    hx1 = (m["head_cx"] + m["head_w"] / 2.0) * W
    d.rectangle([hx0, (m["head_cy"] - m["head_h"] / 2.0) * H, hx1,
                 (m["head_cy"] + m["head_h"] / 2.0) * H],
                outline=(255, 220, 0), width=2)
    cy = m["crown_y"] * H
    d.line([0, cy, W, cy], fill=(255, 220, 0), width=1)

    x, y = sol["x"] * sx, sol["y"] * sy               # green: the window
    w, h = sol["w"] * sx, sol["h"] * sy
    d.rectangle([x, y, x + w, y + h], outline=(0, 255, 0), width=3)
    d.line([x, y + FACE_Y_FRAC * h, x + w, y + FACE_Y_FRAC * h],
           fill=(0, 255, 0), width=1)
    cw = CAPTION_W[style]                             # blue: caption column
    c0 = x + (sol["caption_cx"] - cw / 2.0) * w
    d.rectangle([c0, y + 0.30 * h, c0 + cw * w, y + 0.70 * h],
                outline=(80, 160, 255), width=2)

    # The three gaps, labelled, along the top of the window -- the whole point
    # of the rule is that they are equal, and that is checkable by eye only if
    # they are drawn.
    if sol["caption_cx"] < 0.5:
        edges = [(0.0, sol["outer"], "outer %.3f" % sol["outer"]),
                 (sol["outer"] + cw, sol["outer"] + cw + sol["inner"],
                  "inner %.3f" % sol["inner"]),
                 (1.0 - sol["outer"], 1.0, "outer %.3f" % sol["outer"])]
    else:
        edges = [(0.0, sol["outer"], "outer %.3f" % sol["outer"]),
                 (sol["outer"] + sol["block"],
                  sol["outer"] + sol["block"] + sol["inner"],
                  "inner %.3f" % sol["inner"]),
                 (1.0 - sol["outer"], 1.0, "outer %.3f" % sol["outer"])]
    for a, b, label in edges:
        d.line([x + a * w, y + 0.08 * h, x + b * w, y + 0.08 * h],
               fill=(255, 0, 255), width=3)
        d.text((x + a * w + 3, y + 0.09 * h), label, fill=(255, 0, 255),
               font=font)
    d.text((x + 6, y + 6), "crop %d,%d %dx%d   face %.3f H   caption %.3f W"
           % (sol["x"], sol["y"], sol["w"], sol["h"], sol["fy"],
              sol["caption_cx"]), fill=(0, 255, 0), font=font)
    im.save(out_path)
    return out_path


# --- the cache -------------------------------------------------------------

def cache_path(config_path):
    return os.path.join(os.path.dirname(os.path.abspath(config_path)),
                        "crop.json")


def cache_key(times):
    """Keyed by the frame timestamps, so re-running to tune the GEOMETRY costs
    nothing and only a change of frames pays again."""
    return "%s|%s" % (MODEL, ",".join("%.2f" % t for t in times))


def cache_read(path, key):
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path, encoding="utf-8")).get(key)
    except ValueError:
        return None


def cache_write(path, key, entry):
    data = {}
    if os.path.exists(path):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except ValueError:
            data = {}
    data[key] = entry
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write("\n")


# --- writing the config ----------------------------------------------------

MARK = "# framing solved by pipeline/crop.py"


def block_lines(sol, style, source, when, m):
    """The provenance comment plus the two keys, as YAML lines.

    The comment names the model and the date because that is the only record
    of WHERE these numbers came from once they are four integers in a config
    -- and because a re-solve six months from now will be a different model."""
    return [
        "%s on %s,\n" % (MARK, when),
        "# %s, median of %d/%d sampled frames.\n"
        % (source, m["n"], m["n_frames"]),
        "# Head box %.3f of the window, %s caption column %.3f -> equal %.3f\n"
        % (sol["block"], style, CAPTION_W[style], sol["outer"]),
        "# gaps; face centre at %.3f of the window height. Re-solve with\n"
        % sol["fy"],
        "# `pipeline/crop.py <this-file> --force`; edit these two by hand and\n",
        "# they stay edited (crop.py then refuses without --force).\n",
        "crop: {x: %d, y: %d, w: %d, h: %d}\n"
        % (sol["x"], sol["y"], sol["w"], sol["h"]),
        "x_offset: %d\n" % x_offset_px(sol, style),
    ]


def write_config(path, lines_to_write, force):
    """Put `crop:` and `x_offset:` into the config as a LINE EDIT.

    yaml.safe_dump would round-trip the file and eat every hand-written comment
    in it, and those comments are where each reel's reasoning lives -- the same
    reason align.py's write_trim edits lines."""
    text = open(path, encoding="utf-8").read()
    if not text.endswith("\n"):
        text += "\n"
    lines = text.splitlines(keepends=True)

    # Our own previous block: the marker, its comment lines, and the keys.
    at = next((i for i, l in enumerate(lines) if l.startswith(MARK)), None)
    if at is not None:
        end = at
        while end < len(lines) and (
                lines[end].lstrip().startswith("#")
                or re.match(r"^(crop|x_offset)\s*:", lines[end])):
            end += 1
        del lines[at:end]

    stray = [i for i, l in enumerate(lines)
             if re.match(r"^(crop|x_offset)\s*:", l)]
    if stray and not force:
        raise SystemExit(
            "%s already has a hand-written %s (line %d). It was put there by a "
            "person and this solve would silently replace it -- pass --force if "
            "that is what you want."
            % (path, lines[stray[0]].split(":")[0], stray[0] + 1))
    for i in reversed(stray):
        del lines[i]

    # Under the verse span / trim, where a reader looks for the source facts.
    keys = r"^(style|signature|surah|ayah_start|ayah_end|trim)\s*:"
    after = [i for i, l in enumerate(lines) if re.match(keys, l)]
    at = max(after) + 1 if after else len(lines)
    while at < len(lines) and not lines[at].strip():
        at += 1
    lines[at:at] = ["\n"] + lines_to_write
    open(path, "w", encoding="utf-8").write("".join(lines))


# --- entry point -----------------------------------------------------------

def run(config_path, frames=4, annotate_path=None, write=False, force=False,
        dry_run=False, measurements=None):
    cfg = generate.load_config(config_path)
    style = cfg["style"]
    if style not in CAPTION_W:
        raise SystemExit(
            "crop.py solves `bars` and `hz` only; this config is `%s`.\n"
            "The default style re-crops at RENDER time from its own YuNet pass "
            "(render_default.py detect_subject/vertical_crop_filter) and must "
            "not be given a crop here." % style)
    cfg = generate.resolve_paths(cfg, config_path)
    src = cfg["input"]
    info = generate.get_video_info(src)
    W, H = info["width"], info["height"]
    if not W:
        raise SystemExit("%s has no video stream" % src)

    # Frames come from the reel's OWN window: how the reciter sits during these
    # 30 seconds is the question, not how he sits across a 20-minute upload.
    t0, t1 = 0.0, info["duration"]
    if cfg.get("trim"):
        t0 = float(cfg["trim"][0])
        t1 = float(cfg["trim"][1]) if len(cfg["trim"]) > 1 and \
            cfg["trim"][1] is not None else info["duration"]
    cap = max(1, (CTX - 8192) // EST_TOKENS_PER_FRAME)
    if frames > cap:
        print("      --frames %d would not fit %s's %d-token context at ~%d "
              "tokens a frame; using %d." % (frames, MODEL, CTX,
                                             EST_TOKENS_PER_FRAME, cap))
        frames = cap
    times = sample_times(t0, t1, frames)
    print("%s  %dx%d  %s, %d frames over %.1f-%.1fs"
          % (os.path.basename(config_path), W, H, style, len(times), t0, t1))

    tmp_dir = os.path.join(cfg["tmp_dir"], "crop")
    paths, dims = extract(src, times, tmp_dir, W, H)
    key = cache_key(times)
    entry = None
    if measurements:
        # Already in fractions: a hand-written regression fixture is written in
        # the geometry's own units, not in some frame's pixels.
        entry = {"parsed": json.load(open(measurements, encoding="utf-8")),
                 "usage": {}, "source": measurements, "grid": False}
    elif not force:
        entry = cache_read(cache_path(config_path), key)
        if entry:
            print("      cached solve (%s) -- --force re-queries"
                  % entry.get("when", "?"))
    if entry is None:
        if dry_run:
            raise SystemExit(
                "--dry-run and nothing cached for these frames. Pass "
                "--measurements FILE with a {\"frames\": [...]} object, or drop "
                "--dry-run to ask the model.")
        pooled, usage, cost_usd = [], {}, 0.0
        for p in range(PASSES):
            parsed, envelope = ask(paths, dims, tmp_dir)
            pooled.extend(parsed["frames"])
            cost_usd += float(envelope.get("total_cost_usd") or 0.0)
            for k, v in (envelope.get("usage") or {}).items():
                if isinstance(v, (int, float)):
                    usage[k] = usage.get(k, 0) + v
            print("      pass %d/%d: %d frame reading(s)"
                  % (p + 1, PASSES, len(parsed["frames"])))
        entry = {"parsed": {"frames": pooled}, "usage": usage,
                 "cost_usd": cost_usd, "model": MODEL,
                 "when": datetime.date.today().isoformat(), "times": times,
                 "frame_size": list(dims), "grid": True, "passes": PASSES}
        cache_write(cache_path(config_path), key, entry)
    if entry.get("grid"):
        to_fractions(entry["parsed"])
    usage = entry.get("usage") or {}
    if usage:
        # Printed so the cost of a solve is checked against a bill: one 4-frame
        # pass measures $0.09-0.13 total_cost_usd (Claude Code reports it in
        # the --output-format json envelope).
        print("usage   : %s  ->  $%.5f"
              % (json.dumps({k: v for k, v in usage.items()
                             if isinstance(v, (int, float))}),
                 entry.get("cost_usd") or 0.0))

    m = median(entry["parsed"]["frames"])
    m["_W"], m["_H"] = W, H
    side = caption_side(m)
    sol = solve(W, H, m, style, side)
    print("caption : side %s (he faces %s)" % (side, m["facing"]))
    bad = report(m, sol, style, W, H)

    if annotate_path:
        print("annotate: %s" % annotate(paths[len(paths) // 2], sol, m, style,
                                        annotate_path))
    if not write:
        print("(dry run -- pass --write to put crop: and x_offset: in %s)"
              % os.path.basename(config_path))
        return sol
    if bad:
        raise SystemExit("NOT written: %s. Fix it or hand-solve; a wrong crop "
                         "in a config outlives the session that made it."
                         % "; ".join(bad))
    source = ("measurements from %s" % os.path.basename(measurements)
              if measurements
              else "%s via claude code CLI" % entry.get("model", MODEL))
    when = entry.get("when") or datetime.date.today().isoformat()
    write_config(config_path, block_lines(sol, style, source, when, m), force)
    print("wrote   : %s" % os.path.relpath(config_path, ROOT))
    return sol


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Solve a bars/hz reel's 16:9 crop with a vision model.")
    p.add_argument("config", help="sources/<id>/<reel>.yaml")
    p.add_argument("--frames", type=int, default=4,
                   help="frames to sample and show the model (default 4)")
    p.add_argument("--annotate", metavar="PNG",
                   help="write a labelled debug frame")
    p.add_argument("--write", action="store_true",
                   help="edit crop: and x_offset: into the config")
    p.add_argument("--force", action="store_true",
                   help="re-query the model, and overwrite a hand-written crop")
    p.add_argument("--dry-run", action="store_true",
                   help="never call the model: use --measurements or the cache")
    p.add_argument("--measurements", metavar="JSON",
                   help='offline input: {"frames": [...]}, in FRACTIONS of'
                        ' the frame (not the model\'s 0-1000 grid)')
    a = p.parse_args(argv)
    run(a.config, frames=a.frames, annotate_path=a.annotate, write=a.write,
        force=a.force, dry_run=a.dry_run, measurements=a.measurements)


if __name__ == "__main__":
    main()
