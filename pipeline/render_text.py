"""pipeline/render_text.py -- the `vertical` and `horizontal` styles.

Arabic + English over graded footage, at one of two canvases:

    vertical    1080x1920   the reel shape; type sits BELOW the reciter
    horizontal  1920x1080   the account's landscape look; type sits BESIDE him

ONE look on two canvases, verified against
reels/BADR-AL-TURKI-AHZAB-56-56.mp4: the footage reframed by the config's
`crop` and pushed down under ONE black alpha plate (flat dim + optional soft
vignette + a backing gradient behind the caption block), warm-white Uthmanic
Hafs on a single line with the ayah medallion inline, tracked ALL-CAPS Albertus
beneath it, one shared soft drop shadow for both scripts, and 0.45s dissolves
between cards. The two shapes share every one of those: they differ only in
canvas and in where the block sits.

WHERE THE TYPE GOES is the one thing the two shapes do not share, and neither
half of it is decided here at render time -- pipeline/crop.py measures the
reciter at AUTHORING time and writes numbers into the config (invariant 4: a
reel re-renders identically on a machine with no vision model at all):

  * horizontal -- the block is centred vertically and its COLUMN sits opposite
    the reciter, via the generic `x_offset` px knob (`cx = W // 2 + x_offset`),
    which crop.py solves with the equal-gap rule. No reciter -> no x_offset ->
    dead centre.
  * vertical -- the block is centred horizontally and its TOP sits just below
    his chin, via `face_bottom` (the fraction of the canvas height at which
    crop.py measured his head box to end). No face_bottom -> dead centre.

`x_offset` / `y_offset` are px nudges on top of both, as in the bars style.

Called by generate.py with the resolved plan; not a standalone CLI.
"""
import itertools
import json
import math
import os
import re
import subprocess
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, features

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FONT_DIR = os.path.join(ROOT, "assets", "fonts")

FFMPEG = os.environ.get("QC_FFMPEG") or "ffmpeg"

if not features.check("raqm"):
    sys.exit("FATAL: Pillow lacks RAQM (HarfBuzz+FriBiDi) -- Arabic would be "
             "laid out unjoined, left to right. Use tools/render-venv/bin/python.")

# ---------------------------------------------------------------------------
# Style constants -- legacy/templates/style.yaml, derived from pixel
# measurements of the three landscape reference reels. A constant that cannot
# hold on both canvases is keyed by orientation and carries its reason.
# ---------------------------------------------------------------------------
CANVAS = {"vertical": (1080, 1920), "horizontal": (1920, 1080)}
FPS = 30

ARABIC_FONTS = {
    "uthmanic_hafs": os.path.join(FONT_DIR, "uthmanic_hafs_v22.ttf"),
    "thuluth": os.path.join(FONT_DIR, "AM_Thulth_Regular_0.1.ttf"),
}
# `stroke_macron` -- this font has no U+0100 and renders it as .notdef, so
# ALLAH -> ALLĀH needs its bar stroked as a rectangle over a plain A. Measured
# per font against a private-use codepoint: Albertus true, Gentium false.
ENGLISH_FONTS = {
    "albertus": {"path": os.path.join(FONT_DIR, "Albertus MT Lt Regular.ttf"),
                 "stroke_macron": True},
    "gentium": {"path": os.path.join(FONT_DIR, "GentiumPlus-Regular.ttf"),
                "stroke_macron": False},
}

ARABIC = {"color": (247, 245, 239),      # #F7F5EF, warm white
          "nominal_pt": 72,              # design target, not a fill target
          "min_pt": 40}

# Of frame WIDTH; the Arabic is a single line and is never wrapped. Landscape
# 0.51 is the reference reels' own margin (they never bring text within ~0.05 W
# of the edge) and it also has to leave the reciter his half of the frame.
# Portrait's block is centred with nothing beside it, so it may use the frame.
# The two land within a few px of each other in absolute ink -- 0.86 x 1080 =
# 929, 0.51 x 1920 = 979 -- which is why ONE nominal point size serves both.
AR_WIDTH_FRAC = {"horizontal": 0.51, "vertical": 0.86}
EN_WIDTH_FRAC = {"horizontal": 0.55, "vertical": 0.82}

ENGLISH = {"color": (245, 243, 237),     # #F5F3ED
           "alpha": 240,                 # a hair behind the Arabic
           # ALL-CAPS at ONE size: Albertus has no true small-caps
           # (style.yaml `smallcaps: false`).
           "cap_pt": 29,
           "tracking_pct": 3,            # of the point size, per character
           "line_height_mult": 1.62,
           # Of the frame's SHORT side, not its height: the pair's spacing
           # belongs to the TYPE, which is one size on both canvases, so a
           # height fraction would loosen it by 78% in portrait.
           "gap_below_arabic_frac": 0.02}

# ONE drop shadow, composited from the MERGED ink alpha of both scripts (they
# are drawn into the same layer), so an English line never casts a second
# shadow onto the Arabic. blur 3.0 = style.yaml's `blur_px: 5` x 0.6 -- the
# radius that ships.
SHADOW = {"color": (0, 0, 0), "alpha": 153,     # style.yaml opacity 0.60
          "offset_px": 2, "blur_px": 3.0}

# The grade: a single black RGBA plate whose alpha is dim + vignette + a
# backing gradient under the caption block, clamped so the darkest pixel still
# reads as footage rather than a box.
GRADE = {"cap": 190,
         "vignette": {"rx_frac": 0.62, "ry_frac": 0.62,
                      "a_center": 0, "a_edge": 126, "ease": 2.2}}
# The backing follows the block, so it is shaped like the block: a column in
# landscape, a band in portrait.
BACKING = {"horizontal": {"dy_frac": 0.04, "rx_frac": 0.32, "ry_frac": 0.26,
                          "a_center": 130, "a_edge": 0, "ease": 1.45},
           "vertical": {"dy_frac": 0.06, "rx_frac": 0.60, "ry_frac": 0.15,
                        "a_center": 130, "a_edge": 0, "ease": 1.45}}

# With no measured reciter the block is centred on the frame; `y_offset` (and
# `x_offset`) are the per-clip nudges.
BLOCK_CENTER_Y_FRAC = 0.50
# Portrait: air between his chin and the Arabic's ink top, as a fraction of the
# short side. The brief is "a few pixels below his face"; below ~0.03 the
# tashkeel of a tall phrase starts touching the underside of his jaw.
FACE_GAP_FRAC = 0.035
# ...and the block's ink may not run past this fraction of the height, which is
# where the signature's own band begins (SIGNATURE_Y_FRAC 0.945).
BLOCK_BOTTOM_FRAC = 0.90

# Cards dissolve in fixed position: the incoming fade-in straddles the
# outgoing fade-out. No slide, no scale, no per-word reveal.
CROSSFADE_S = 0.45

VIDEO_FADE_IN_S, VIDEO_FADE_OUT_S = 0.3, 0.5
AUDIO = {"lufs": -14.0, "tp": -1.0, "lra": 11.0,
         "fade_in_s": 0.3, "fade_out_s": 0.5}
ENCODE = {"crf": 18, "preset": "slow", "audio_bitrate": "192k"}

# ffmpeg's default 8-packet input queue deadlocks a many-input filtergraph
# (one looped PNG per card, plus the grade plate and the signature).
THREAD_QUEUE_SIZE = 4096

# Signature: the SIZE is of the short side (28px on either canvas); the
# POSITION is of the height.
SIGNATURE_SIZE_FRAC = 0.026
SIGNATURE_Y_FRAC = 0.945
SIGNATURE_ALPHA = 0.87

# Tajweed ANNOTATION marks (reading aids, neither letters nor harakat) that
# these fonts cannot mark-attach -- U+06DF SMALL HIGH ROUNDED ZERO comes out as
# a stray U+25CC dotted ring, U+06ED SMALL LOW MEEM as a floating symbol --
# plus U+0640 TATWEEL, which breaks dagger-alif anchoring in PIL/Raqm (the tiny
# alif detaches and lands at the far end of the word). Display only; the
# aligner never sees display text and no letter or pronunciation diacritic is
# lost. Escapes, not literals -- invariant 1: no Arabic is retyped in source.
DISPLAY_STRIP_CHARS = {"\u06DF", "\u06ED", "\u0640"}


def norm_ar(s):
    return "".join(c for c in s if c not in DISPLAY_STRIP_CHARS)


def truetype(path, pt):
    return ImageFont.truetype(path, pt, layout_engine=ImageFont.Layout.RAQM)


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------
_PROBE = ImageDraw.Draw(Image.new("L", (1, 1)))


def ar_bbox(text, font, xy=(0, 0)):
    """Ink bbox of one shaped Arabic line CENTRED on xy -> (l, top, r, bot)."""
    return _PROBE.textbbox(xy, text, font=font, anchor="mm",
                           direction="rtl", language="ar")


def fit_pt(nominal_pt, min_pt, max_width, widest):
    """One shared point size for every phrase (a caption swap never changes the
    type size), shrunk only far enough that the WIDEST line fits. Never scaled
    UP, never below min_pt."""
    return max(int(min_pt), int(nominal_pt * min(1.0, max_width / widest)))


# ---------------------------------------------------------------------------
# the English engine: ALL-CAPS, tracked, hand-stroked macron
# ---------------------------------------------------------------------------
_NON_LETTER = re.compile("[^A-Za-z\\u0100-\\u017F]")


def en_tokens(text, stroke_macron=True, caps=True):
    """-> [(char, stroke?)] for one line, spacing and punctuation passed
    through, upper-cased unless `caps` is off (config `english_caps`).

    `stroke` marks the second a of Allah (letter index 3) on a font that cannot
    draw the macron -- Albertus has neither U+0100 nor U+0101 and renders both
    as .notdef. A font that HAS the glyph gets the real character instead, in
    the case the line is being set in, and nothing is stroked."""
    toks = []
    for w in re.split(r"(\s+)", text):
        if not w or w.isspace():
            toks.extend((ch, False) for ch in w)
            continue
        # strip leading quotes/punctuation to find the alpha core to test
        macron = _NON_LETTER.sub("", w).lower() == "allah"
        li = 0
        for ch in w:
            if ch.isalpha():
                out = ch.upper() if caps else ch
                mark = macron and li == 3 and out.lower() == "a"
                if mark and not stroke_macron:
                    out = "\u0100" if out.isupper() else "\u0101"
                    mark = False
                toks.append((out, mark))
                li += 1
            else:
                toks.append((ch, False))
    return toks


def en_width(toks, font, tracking_px):
    return sum(font.getlength(ch) + tracking_px for ch, _ in toks)


def wrap_english(text, font, tracking_px, max_w, stroke_macron=True,
                 caps=True):
    """Wrap into the FEWEST lines that fit max_w, then BALANCE the split
    (min-raggedness): among all splits with that line count whose every line
    fits, pick the one minimising the widest line (tie-break: min spread).
    Greedy first-fit alone leaves orphan last lines (a lone 'ME.')."""
    if not text.strip():
        return []
    words = text.split(" ")

    def width_of(ws):
        return en_width(en_tokens(" ".join(ws), stroke_macron, caps), font,
                        tracking_px)

    greedy, cur = [], []                 # greedy = the minimal feasible count
    for wd in words:
        if not cur or width_of(cur + [wd]) <= max_w:
            cur.append(wd)
        else:
            greedy.append(cur)
            cur = [wd]
    if cur:
        greedy.append(cur)
    n = len(greedy)
    if n <= 1:
        return [" ".join(ln) for ln in greedy]

    best, best_key = None, None
    for cuts in itertools.combinations(range(1, len(words)), n - 1):
        bounds = (0,) + cuts + (len(words),)
        cand = [words[bounds[i]:bounds[i + 1]] for i in range(n)]
        ws = [width_of(ln) for ln in cand]
        if max(ws) > max_w:
            continue
        key = (max(ws), max(ws) - min(ws))
        if best_key is None or key < best_key:
            best_key, best = key, cand
    if best is None:                     # no balanced split fits; keep greedy
        best = greedy
    return [" ".join(ln) for ln in best]


def draw_en_line(layer, x0, baseline, toks, font, tracking_px, fill):
    """One tracked line, drawn a glyph at a time because Pillow has no letter-
    spacing. `x0` is the pen, not the centre."""
    d = ImageDraw.Draw(layer)
    x = x0
    for ch, mac in toks:
        d.text((x, baseline), ch, font=font, fill=fill, anchor="ls")
        adv = font.getlength(ch)
        if mac:
            # the glyph actually being set, not a hardcoded "A": with
            # `english_caps: false` the bar has to sit on an x-height "a".
            cb = font.getbbox(ch)
            cap_h = cb[3] - cb[1]
            bar_w = adv * 0.55           # centred, ~55% of the glyph, above cap
            bx = x + (adv - bar_w) / 2.0
            by = baseline - cap_h - max(2, cap_h * 0.14)
            th = max(1, int(round(cap_h * 0.08)))
            d.rectangle([bx, by, bx + bar_w, by + th], fill=fill)
        x += adv + tracking_px


# ---------------------------------------------------------------------------
# layout -- pure numbers; draw_layers() consumes exactly these
# ---------------------------------------------------------------------------

def layout(orientation, phrases, english, cfg, face_bottom):
    """WHERE the Arabic anchor and every English baseline goes.

    The vertical anchor is SOLVED, not configured: `ar_cy` anchors the ARABIC
    line, but the block extends below it by an amount that depends on the ink
    height, the fitted point size and -- most of all -- how many lines the
    English wraps to, so no fixed anchor puts the block where it is wanted.
    The anchor is a pure translation of the whole block, so ONE measurement at
    a trial anchor solves it exactly. Phrase blocks differ in height (a
    two-line English hangs ~0.02 H lower than a one-line one), so the MEDIAN
    phrase is the one placed, as the legacy solver does.

    What it is solved AGAINST is the difference between the two shapes:
    landscape centres the block's MIDDLE on the frame; portrait puts its TOP
    below the reciter's chin, which is the number `face_bottom` carries.
    """
    W, H = CANVAS[orientation]
    short = min(W, H)
    cx = W // 2 + int(cfg["x_offset"])

    ar_path = ARABIC_FONTS[cfg["arabic_font"]]
    en_spec = ENGLISH_FONTS[cfg["english_font"]]
    stroke = en_spec["stroke_macron"]

    texts = [norm_ar(p["text"]) for p in phrases]
    max_ar_w = AR_WIDTH_FRAC[orientation] * W
    nominal = ARABIC["nominal_pt"] * cfg["arabic_scale"]
    probe = truetype(ar_path, max(1, int(nominal)))
    raw = [ar_bbox(t, probe) for t in texts]
    widest = max([1.0] + [b[2] - b[0] for b in raw])
    pt = fit_pt(nominal, ARABIC["min_pt"] * cfg["arabic_scale"], max_ar_w,
                widest)
    ar_font = truetype(ar_path, pt)

    cap_pt = max(12, int(round(ENGLISH["cap_pt"] * cfg["english_scale"])))
    en_font = truetype(en_spec["path"], cap_pt)
    tracking = ENGLISH["tracking_pct"] / 100.0 * cap_pt
    max_en_w = EN_WIDTH_FRAC[orientation] * W
    caps = cfg["english_caps"]
    en_lines = [wrap_english(e["text"], en_font, tracking, max_en_w, stroke,
                             caps)
                for e in english]
    cap_bb = en_font.getbbox("H")
    cap_h = cap_bb[3] - cap_bb[1]
    gap = int(ENGLISH["gap_below_arabic_frac"] * short)
    line_h = int(cap_pt * ENGLISH["line_height_mult"])

    def rows(text, lines, ar_cy):
        """(arabic bbox, [(line, baseline_y)]) for one phrase at one anchor."""
        bb = ar_bbox(text, ar_font, (cx, ar_cy))
        y0 = bb[3] + gap + cap_h
        return bb, [(ln, y0 + k * line_h) for k, ln in enumerate(lines)]

    def bounds(bb, base):
        """The block's (top, bottom) in px. Measured on the INK: the shadow is
        a 2px offset inside a 3.0px blur, i.e. a near-symmetric halo, so
        including it moves the solved anchor by ~1px -- and `y_offset` is the
        knob for that kind of nudge anyway."""
        bot = bb[3]
        for ln, y in base:
            bot = max(bot, _PROBE.textbbox((cx, y), ln, font=en_font,
                                           anchor="ls")[3])
        return bb[1], bot

    trial = 0.5 * H
    spans = [bounds(*rows(text, lines, trial))
             for text, lines in zip(texts, en_lines)]
    if orientation == "vertical" and face_bottom is not None:
        # anchor -> block-TOP offset (near-constant across phrases: they share
        # a point size, and only the English line count moves the bottom)
        rise = trial - sorted(t for t, _ in spans)[len(spans) // 2]
        want = float(face_bottom) * H + FACE_GAP_FRAC * short
        placed_on = "chin %.3f H + %dpx" % (face_bottom, int(FACE_GAP_FRAC * short))
        ar_cy = want + rise
    else:
        # anchor -> block-CENTRE offset, measured on the median phrase
        drop = sorted((t + b) / 2.0 for t, b in spans)[len(spans) // 2] - trial
        want = BLOCK_CENTER_Y_FRAC * H
        placed_on = "frame centre %.2f H" % BLOCK_CENTER_Y_FRAC
        ar_cy = want - drop

    # The deepest phrase decides whether the block clears the signature: a
    # two-line English under a low chin is the case that runs off the frame,
    # and silently drawing type into the signature's band is worse than saying
    # so and lifting the whole block.
    deepest = max(b for _, b in spans) + (ar_cy - trial)
    overrun = deepest - BLOCK_BOTTOM_FRAC * H
    if overrun > 0:
        ar_cy -= overrun
        placed_on += ", lifted %dpx to clear %.2f H" % (overrun,
                                                        BLOCK_BOTTOM_FRAC)
    ar_cy = int(round(ar_cy)) + int(cfg["y_offset"])

    out = {"pt": pt, "cap_pt": cap_pt, "ar_font": ar_font, "en_font": en_font,
           "cx": cx, "ar_cy": ar_cy, "tracking": tracking, "widest": widest,
           "max_ar_w": max_ar_w, "stroke_macron": stroke, "caps": caps,
           "placed_on": placed_on, "overrun": max(0.0, overrun), "phrases": []}
    for i, (text, lines) in enumerate(zip(texts, en_lines), start=1):
        bb, base = rows(text, lines, ar_cy)
        top, bot = bounds(bb, base)
        out["phrases"].append({"i": i, "text": text, "ar_bbox": bb,
                               "english": base, "top": top, "bottom": bot})
    return out


# ---------------------------------------------------------------------------
# drawing
# ---------------------------------------------------------------------------

def with_shadow(ink, W, H):
    """The ink plus ONE soft drop shadow struck from its merged alpha: a solid
    black layer through that alpha, pushed down-right and blurred, ink over it
    -- a lift off the footage, not an outline ring."""
    off = SHADOW["offset_px"]
    solid = Image.new("RGBA", (W, H), SHADOW["color"] + (SHADOW["alpha"],))
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sh.paste(solid, (0, 0), ink.getchannel("A"))
    sh = sh.transform((W, H), Image.AFFINE, (1, 0, -off, 0, 1, -off))
    sh = sh.filter(ImageFilter.GaussianBlur(SHADOW["blur_px"]))
    return Image.alpha_composite(sh, ink)


def trim_to_ink(card):
    """A full-canvas card cropped to its non-transparent pixels -> (img, x, y).

    The compositor pays for the whole overlay every frame a card is up, and a
    card's ink -- one Arabic line plus one or two English lines -- runs 6-7% of
    the canvas on real cards; what is cropped away is (0,0,0,0), which
    `overlay` composites to nothing. The box is snapped OUTWARD to even
    coordinates because `overlay` blends in yuv420 by default: on an odd edge
    the overlay's 2x2 chroma/alpha blocks would straddle a different grid than
    the full-canvas card's and the result would shift.

    Worth 33.8s -> 10.5s CPU and 1618 -> 633 MB peak RSS on the caption stage
    of a 6-card 1080p reel at 11%, measured against full-canvas overlays with
    the same gates build_graph adds."""
    box = card.getbbox()
    if box is None:                       # an empty card composites to nothing
        return card, 0, 0
    x0, y0 = box[0] - box[0] % 2, box[1] - box[1] % 2
    x1 = min(card.width, box[2] + box[2] % 2)
    y1 = min(card.height, box[3] + box[3] % 2)
    return card.crop((x0, y0, x1, y1)), x0, y0


def draw_layers(lay, W, H, out_dir):
    """One RGBA card per phrase: Arabic + English + the shared shadow, drawn at
    full canvas and saved cropped to its own ink. The box it came from travels
    with it, since the compositor now has to place it.
    -> [{"path", "box", "ar_w", "en_lines"}]."""
    os.makedirs(out_dir, exist_ok=True)
    report = []
    for p in lay["phrases"]:
        ink = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(ink).text(
            (lay["cx"], lay["ar_cy"]), p["text"], font=lay["ar_font"],
            fill=ARABIC["color"] + (255,), anchor="mm",
            direction="rtl", language="ar")
        for ln, y in p["english"]:
            toks = en_tokens(ln, lay["stroke_macron"], lay["caps"])
            w = en_width(toks, lay["en_font"], lay["tracking"])
            draw_en_line(ink, lay["cx"] - w / 2.0, y, toks, lay["en_font"],
                         lay["tracking"], ENGLISH["color"] + (ENGLISH["alpha"],))
        path = os.path.join(out_dir, "phrase%d.png" % p["i"])
        card, x0, y0 = trim_to_ink(with_shadow(ink, W, H))
        card.save(path)
        bb = p["ar_bbox"]
        report.append({"path": path, "box": (x0, y0),
                       "ar_w": bb[2] - bb[0],
                       "en_lines": len(p["english"])})
    return report


def radial_alpha(w, h, cx, cy, rx, ry, a_center, a_edge, ease, lo=48, hi=27):
    """An 'L' alpha ramp (a_center -> a_edge over an ellipse), built at 48x27
    and bilinearly upscaled.

    The low resolution IS the softness of this look: a full-resolution ramp
    with the same maths reads as a hard-edged gradient, because the upscale's
    bilinear interpolation is what rounds the ellipse's shoulder off. Do not
    "fix" lo/hi.
    """
    small = Image.new("L", (lo, hi), 0)
    px = small.load()
    for j in range(hi):
        for i in range(lo):
            fx = (i + 0.5) / lo * w
            fy = (j + 0.5) / hi * h
            d = min(1.0, math.sqrt(((fx - cx) / rx) ** 2 + ((fy - cy) / ry) ** 2))
            val = a_center + (a_edge - a_center) * d ** ease
            px[i, j] = max(0, min(255, int(round(val))))
    return small.resize((w, h), Image.BILINEAR)


def grade_plate(lay, orientation, W, H, vignette, dim, tmp):
    """The single black plate that seats the type: flat dim, optionally a
    vignette, plus a backing gradient centred just below the Arabic anchor
    (live footage runs brighter than the account's moody grade, and warm-white
    type over a sunlit thobe needs the backing to read at all). Capped so the
    darkest pixel is still footage."""
    v, t = GRADE["vignette"], BACKING[orientation]
    combo = Image.new("L", (W, H), max(0, min(255, int(round(dim * 255)))))
    if vignette:
        combo = ImageChops.add(combo, radial_alpha(
            W, H, W / 2.0, H / 2.0, W * v["rx_frac"], H * v["ry_frac"],
            v["a_center"], v["a_edge"], v["ease"]))
    combo = ImageChops.add(combo, radial_alpha(
        W, H, lay["cx"], lay["ar_cy"] + t["dy_frac"] * H,
        W * t["rx_frac"], H * t["ry_frac"],
        t["a_center"], t["a_edge"], t["ease"]))
    combo = combo.point(lambda a: min(a, GRADE["cap"]))
    black = Image.new("L", (W, H), 0)
    path = os.path.join(tmp, "grade.png")
    Image.merge("RGBA", (black, black, black, combo)).save(path)
    return path


def signature_card(text, font_path, W, H, tmp, offset=0):
    """`offset` moves the line vertically only (+ lower / - higher); the
    signature is ALWAYS horizontally centered -- no x knob on purpose.

    -> (path, x, y), cropped to its own ink like the caption cards. It is the
    one overlay up for the WHOLE reel, so a full-canvas plate for ~200x27px of
    type would cost the most of any of them: 13.1s -> 10.1s CPU and 371 -> 220
    MB peak RSS on a 23s reel."""
    font = ImageFont.truetype(font_path,
                              max(12, int(min(W, H) * SIGNATURE_SIZE_FRAC)))
    bbox = _PROBE.textbbox((0, 0), text, font=font)
    ink = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ink).text(
        (W // 2 - (bbox[2] - bbox[0]) // 2 - bbox[0],
         int(H * SIGNATURE_Y_FRAC) - (bbox[3] - bbox[1]) // 2 - bbox[1]
         + int(offset)),
        text, font=font, fill=(255, 255, 255, 255))
    card = with_shadow(ink, W, H)
    card.putalpha(card.getchannel("A").point(
        lambda a: int(round(a * SIGNATURE_ALPHA))))
    card, x0, y0 = trim_to_ink(card)
    path = os.path.join(tmp, "signature.png")
    card.save(path)
    return path, x0, y0


# ---------------------------------------------------------------------------
# transition schedule
# ---------------------------------------------------------------------------

def schedule(phrases, dur):
    """One (t_in, d_in, t_out, d_out) per card. Every card fades for the same
    0.45s and a later card comes up early enough that its fade-in straddles the
    outgoing card's fade-out -> a fixed-position dissolve, not a blink.

    The offsets were tuned once on the reference reels (legacy/qc/timeline.py):
      0.05  the FIRST card is up a hair before its own first word, so the frame
            is never empty on the word onset;
      0.24  a later card comes up early enough to straddle the outgoing fade;
      0.02  a non-last card holds a beat past its last word;
      0.15  the LAST card starts leaving before its last word ends, so it is
            gone INTO the tail fade rather than cut off by it.
    """
    n = len(phrases)
    xf = CROSSFADE_S
    out = []
    for i, ph in enumerate(phrases):
        t_in = max(0.0, ph["start"] - 0.05) if i == 0 else ph["start"] - 0.24
        t_out = (min(dur - xf, ph["end"] - 0.15) if i == n - 1
                 else ph["end"] + 0.02)
        out.append((t_in, xf, t_out, xf))
    return out


# ---------------------------------------------------------------------------
# audio (two-pass loudnorm + fades)
# ---------------------------------------------------------------------------

def measure_loudness(src, dur):
    # -vn: without it the null muxer still pulls the video stream, so pass 1
    # decodes the whole clip's picture to measure its audio.
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-t", "%.3f" % dur, "-i", src,
           "-vn", "-af", "loudnorm=I=%s:TP=%s:LRA=%s:print_format=json"
           % (AUDIO["lufs"], AUDIO["tp"], AUDIO["lra"]),
           "-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = p.stderr
    start, end = out.rfind("{"), out.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError("loudnorm pass-1 produced no JSON:\n" + out[-2000:])
    return json.loads(out[start:end + 1])


def loudnorm_filter(st):
    return ("loudnorm=I=%s:TP=%s:LRA=%s:measured_I=%s:measured_TP=%s:"
            "measured_LRA=%s:measured_thresh=%s:offset=%s:linear=true:"
            "print_format=summary"
            % (AUDIO["lufs"], AUDIO["tp"], AUDIO["lra"], st["input_i"],
               st["input_tp"], st["input_lra"], st["input_thresh"],
               st["target_offset"]))


# ---------------------------------------------------------------------------
# the filtergraph
# ---------------------------------------------------------------------------

def reframe(orientation, info, crop):
    """The source -> canvas decision, made once and printed. -> (crop, note)

    A source already on the target's side of square is cover-scaled -- written
    on directly, nothing to solve. One on the WRONG side needs a reviewed
    window, not a blind centre crop that would saw the reciter in half, so it
    is refused and sent to pipeline/crop.py, which is where the caption anchor
    has to come from anyway.
    """
    if crop:
        if not all(k in crop for k in ("x", "y", "w", "h")):
            raise SystemExit("crop must carry x, y, w, h")
        return crop, "crop %dx%d+%d+%d" % (crop["w"], crop["h"],
                                           crop["x"], crop["y"])
    portrait = info["height"] >= info["width"]
    if (orientation == "vertical") != portrait:
        raise SystemExit(
            "this source is %dx%d and the reel is %s, so it needs a reviewed "
            "`crop:` -- a blind centre crop would cut the reciter. Solve it "
            "with\n    tools/render-venv/bin/python pipeline/crop.py "
            "<config> --write --annotate /tmp/crop.png\nwhich writes `crop:` "
            "and `%s:` together."
            % (info["width"], info["height"], orientation,
               "face_bottom" if orientation == "vertical" else "x_offset"))
    return None, "cover-scaled (source already %s)" % (
        "portrait" if portrait else "landscape")


def source_chain(crop, W, H):
    """[0:v] -> exactly WxH of footage. `crop` is the authored window in SOURCE
    pixels (pipeline/crop.py solves it); without one the footage is
    cover-scaled, never distorted."""
    if crop:
        return ("crop=%d:%d:%d:%d,scale=%d:%d:flags=lanczos"
                % (crop["w"], crop["h"], crop["x"], crop["y"], W, H))
    return ("scale=%d:%d:flags=lanczos:force_original_aspect_ratio=increase,"
            "crop=%d:%d" % (W, H, W, H))


def build_graph(src, dur, crop, W, H, grade_png, cards, sched, ln, afade,
                sig=None):
    """-> (filter_complex, input argv). Inputs: [0]=source, [1]=grade plate,
    [2..]=one card per phrase, then the signature (last, so dropping it cannot
    shift any other index).

    The grade plate is genuinely full-canvas; every other overlay arrives
    cropped to its own ink and is PLACED at the box it came from. Each card is
    also gated to its own alpha window widened by a frame either side, so the
    enable can only ever switch on frames the card was already fully
    transparent for.

    Every looped PNG input is bounded by the output's `-t`: a `-loop 1` input
    never EOFs, and overlay's default eof_action would leave the graph with no
    end at all -- the encode runs forever into a file with no moov atom.
    """
    tqs = ["-thread_queue_size", str(THREAD_QUEUE_SIZE)]
    argv = ["-ss", "0.000", "-t", "%.3f" % dur] + tqs + ["-i", src]
    for png in ([grade_png] + [c["path"] for c in cards]
                + ([sig[0]] if sig else [])):
        argv += ["-framerate", str(FPS), "-loop", "1"] + tqs + ["-i", png]

    parts = ["[0:v]%s,setsar=1,fps=%d,format=rgba[bg];"
             "[1:v]format=rgba[grd];[bg][grd]overlay=0:0:format=auto[b0]"
             % (source_chain(crop, W, H), FPS)]
    base = "b0"
    for i, ((t_in, d_in, t_out, d_out), c) in enumerate(zip(sched, cards)):
        parts.append(";[%d:v]format=rgba,"
                     "fade=t=in:st=%.3f:d=%s:alpha=1,"
                     "fade=t=out:st=%.3f:d=%s:alpha=1[c%d]"
                     % (i + 2, t_in, d_in, t_out, d_out, i + 1))
        parts.append(";[%s][c%d]overlay=%d:%d:format=auto"
                     ":enable='between(t,%.3f,%.3f)'[b%d]"
                     % (base, i + 1, c["box"][0], c["box"][1],
                        max(0.0, t_in - 1.0 / FPS), t_out + d_out + 1.0 / FPS,
                        i + 1))
        base = "b%d" % (i + 1)
    if sig:
        sidx = 2 + len(cards)
        parts.append(";[%d:v]format=rgba[sg];[%s][sg]overlay=%d:%d:"
                     "format=auto[sig]" % (sidx, base, sig[1], sig[2]))
        base = "sig"
    # whole-frame fade from/to black, mirroring the audio fades (all 3 refs)
    parts.append(";[%s]fade=t=in:st=0:d=%s,fade=t=out:st=%.3f:d=%s,"
                 "format=yuv420p[vout]"
                 % (base, VIDEO_FADE_IN_S, dur - VIDEO_FADE_OUT_S,
                    VIDEO_FADE_OUT_S))
    parts.append(";[0:a]%s,%s[aout]" % (ln, afade))
    return "".join(parts), argv


# ---------------------------------------------------------------------------
# the style entry point
# ---------------------------------------------------------------------------

def render(plan):
    cfg, info = plan["cfg"], plan["info"]
    src, tmp = plan["src"], plan["tmp"]
    phrases, english = plan["arabic"], plan["english"]
    orientation = cfg["style"]
    W, H = CANVAS[orientation]
    dur = info["duration"]
    if not info["width"]:
        raise SystemExit("this style needs footage: the input has no video "
                         "stream and there is no still-photo mode")

    crop, note = reframe(orientation, info, cfg.get("crop"))
    face_bottom = cfg.get("face_bottom")
    lay = layout(orientation, phrases, english, cfg, face_bottom)

    print("      %dx%d | %s" % (W, H, note))
    print("      arabic: %s @ %dpt (nominal %d, widest raw %dpx, cap %dpx) | "
          "english: %s @ %dpt"
          % (cfg["arabic_font"], lay["pt"],
             int(ARABIC["nominal_pt"] * cfg["arabic_scale"]), lay["widest"],
             lay["max_ar_w"], cfg["english_font"], lay["cap_pt"]))
    print("      block x=%d (%.3f W), Arabic anchor y=%d (%.3f H) -- on %s"
          % (lay["cx"], lay["cx"] / float(W), lay["ar_cy"],
             lay["ar_cy"] / float(H), lay["placed_on"]))
    if orientation == "vertical" and face_bottom is None:
        print("      no `face_bottom:` -- the block is centred on the frame "
              "and may land on the reciter. crop.py --write measures it.")
    if lay["overrun"] > 0:
        print("! the caption block ran %dpx past %.2f H and the whole block "
              "was lifted to clear the signature. His chin is low in this "
              "window, or a card's English wraps to more lines than the rest "
              "-- check the render before accepting it."
              % (lay["overrun"], BLOCK_BOTTOM_FRAC), file=sys.stderr)

    rep = draw_layers(lay, W, H, os.path.join(tmp, "overlays"))
    for r, p in zip(rep, lay["phrases"]):
        print("      phrase%d: ar_w=%dpx en_lines=%d block %d..%d "
              "(%.3f..%.3f H)"
              % (p["i"], r["ar_w"], r["en_lines"], p["top"], p["bottom"],
                 p["top"] / float(H), p["bottom"] / float(H)))

    grade_png = grade_plate(lay, orientation, W, H, cfg["vignette"],
                            cfg["dim"], tmp)
    sig = (signature_card(cfg["signature"],
                          ENGLISH_FONTS[cfg["english_font"]]["path"], W, H,
                          tmp, cfg["signature_offset"])
           if cfg["signature"] else None)
    sched = schedule(phrases, dur)

    print("      loudnorm pass 1...")
    ln = loudnorm_filter(measure_loudness(src, dur))
    afade = ("afade=t=in:st=0:d=%s,afade=t=out:st=%.3f:d=%s"
             % (AUDIO["fade_in_s"], dur - AUDIO["fade_out_s"],
                AUDIO["fade_out_s"]))

    fc, in_argv = build_graph(src, dur, crop, W, H, grade_png, rep, sched, ln,
                              afade, sig)

    out = plan["out"]
    cmd = [FFMPEG, "-y", "-hide_banner"] + in_argv
    cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "[aout]",
            "-t", "%.3f" % dur,
            "-c:v", "libx264", "-crf", str(ENCODE["crf"]),
            "-preset", ENCODE["preset"], "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-c:a", "aac", "-b:a", ENCODE["audio_bitrate"],
            "-movflags", "+faststart", out]
    print("      " + " | ".join(
        "P%d in@%.2f out@%.2f" % (i + 1, ti, to)
        for i, (ti, _, to, _) in enumerate(sched)))
    if subprocess.run(cmd).returncode != 0:
        raise SystemExit("ffmpeg render failed")
    print("      %s | %dx%d | %s" % (os.path.relpath(out, ROOT), W, H, note))
