"""pipeline/render_hz.py -- the `hz` style renderer.

Native landscape 1920x1080, the account's original look: the footage reframed
by the config's `crop` and pushed down under ONE black alpha plate (flat dim +
soft vignette + a backing gradient behind the caption column), warm-white
Uthmanic Hafs on a single line with the ayah medallion inline, tracked ALL-CAPS
Albertus centred beneath it, one shared soft drop shadow for both scripts, and
0.45s dissolves between cards.

Ported from legacy/scripts/render_text.py + legacy/scripts/build_render.py,
with the style numbers from legacy/templates/style.yaml (pixel forensics of the
account's three landscape reference reels). The reference render this was
verified against is reels/BADR-AL-TURKI-AHZAB-56-56.mp4.

Two things this style does NOT do, deliberately:
  * no face detection -- the reframe is the config's `crop` (source pixels),
    solved once at authoring time, so a render is reproducible;
  * no per-phrase type size -- one shrink-to-fit point size for the whole reel,
    so a caption swap never changes the Arabic's weight.

The caption COLUMN is off-centre (the type sits opposite the reciter), which is
expressed with the generic `x_offset` px knob, exactly as the bars style does
it: `cx = W // 2 + x_offset`. Vertically the block is auto-centred on the frame
and `y_offset` nudges it (style.yaml's per-clip "vertical_nudge +/- 0.04 H").

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
# measurements of the three landscape reference reels. Native landscape: this
# style never letterboxes and never re-crops to 9:16.
# ---------------------------------------------------------------------------
CANVAS_W, CANVAS_H = 1920, 1080
FPS = 30

AR_FONT = os.path.join(FONT_DIR, "uthmanic_hafs_v22.ttf")
EN_FONT = os.path.join(FONT_DIR, "Albertus MT Lt Regular.ttf")

ARABIC = {"color": (247, 245, 239),      # #F7F5EF, warm white
          "nominal_pt": 72,              # design target, not a fill target
          "min_pt": 40,
          # of frame WIDTH; single line, never wrapped. The refs never bring
          # text within ~0.05 W of the frame edge.
          "max_line_width_frac": 0.51}

ENGLISH = {"color": (245, 243, 237),     # #F5F3ED
           "alpha": 240,                 # a hair behind the Arabic
           # ALL-CAPS at ONE size: Albertus has no true small-caps and the
           # faked petite caps were dropped (style.yaml `smallcaps: false`).
           "cap_pt": 29,
           "tracking_pct": 3,            # of the point size, per character
           "max_line_width_frac": 0.55,
           "line_height_px": int(29 * 1.62),
           "gap_below_arabic_frac": 0.02}

# ONE drop shadow, composited from the MERGED ink alpha of both scripts (they
# are drawn into the same layer), so an English line never casts a second
# shadow onto the Arabic. blur 3.0 = style.yaml's `blur_px: 5` x the 0.6 the
# legacy renderer multiplied it by before handing it to GaussianBlur -- the
# template number was never the radius that shipped.
SHADOW = {"color": (0, 0, 0), "alpha": 153,     # style.yaml opacity 0.60
          "offset_px": 2, "blur_px": 3.0}

# The grade: a single black RGBA plate whose alpha is dim + vignette + a
# backing gradient under the caption column, clamped so the darkest pixel
# still reads as footage rather than a box.
GRADE = {"base": 30, "cap": 190,
         "vignette": {"rx_frac": 0.62, "ry_frac": 0.62,
                      "a_center": 0, "a_edge": 126, "ease": 2.2},
         "backing": {"dy_frac": 0.04, "rx_frac": 0.32, "ry_frac": 0.26,
                     "a_center": 130, "a_edge": 0, "ease": 1.45}}

# The caption block is centred on the frame; per-clip nudges are `y_offset`.
BLOCK_CENTER_Y_FRAC = 0.50

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

# Signature: house geometry, shared with the other two styles.
SIGNATURE_SIZE_FRAC = 0.026
SIGNATURE_Y_FRAC = 0.945
SIGNATURE_ALPHA = 0.87

# Tajweed ANNOTATION marks (reading aids, neither letters nor harakat) that
# this font cannot mark-attach -- U+06DF SMALL HIGH ROUNDED ZERO comes out as a
# stray U+25CC dotted ring, U+06ED SMALL LOW MEEM as a floating symbol -- plus
# U+0640 TATWEEL, which breaks dagger-alif anchoring in PIL/Raqm (the tiny alif
# detaches and lands at the far end of the word). Display only; the aligner
# never sees display text and no letter or pronunciation diacritic is lost.
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
# Albertus has no U+0100 (LATIN CAPITAL A WITH MACRON) and renders tofu for it,
# so ALLAH -> ALLĀH is drawn as a plain "A" with the bar stroked as a rectangle.
_NON_LETTER = re.compile("[^A-Za-z\\u0100-\\u017F]")


def en_tokens(text):
    """-> [(char, macron?)] for one line, upper-cased, spacing and punctuation
    passed through. `macron` marks the second A of ALLAH (letter index 3)."""
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
                up = ch.upper()
                toks.append((up, macron and li == 3 and up == "A"))
                li += 1
            else:
                toks.append((ch, False))
    return toks


def en_width(toks, font, tracking_px):
    return sum(font.getlength(ch) + tracking_px for ch, _ in toks)


def wrap_english(text, font, tracking_px, max_w):
    """Wrap into the FEWEST lines that fit max_w, then BALANCE the split
    (min-raggedness): among all splits with that line count whose every line
    fits, pick the one minimising the widest line (tie-break: min spread).
    Greedy first-fit alone leaves orphan last lines (a lone 'ME.')."""
    if not text.strip():
        return []
    words = text.split(" ")

    def width_of(ws):
        return en_width(en_tokens(" ".join(ws)), font, tracking_px)

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
            cb = font.getbbox("A")
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

def layout(phrases, english, x_offset, y_offset):
    """WHERE the Arabic anchor and every English baseline goes.

    The vertical anchor is SOLVED, not configured: `AR_CY` anchors the ARABIC
    line, but the block extends below it by an amount that depends on the ink
    height, the fitted point size and -- most of all -- how many lines the
    English wraps to, so a fixed anchor does not centre the block. The anchor
    is a pure translation of the whole block, so one measurement solves it
    exactly. Phrase blocks differ in height (a two-line English hangs ~0.02 H
    lower than a one-line one), so no anchor centres them all: the MEDIAN
    phrase is centred, exactly as the legacy solver did.
    """
    W, H = CANVAS_W, CANVAS_H
    cx = W // 2 + int(x_offset)

    texts = [norm_ar(p["text"]) for p in phrases]
    max_ar_w = ARABIC["max_line_width_frac"] * W
    probe = truetype(AR_FONT, ARABIC["nominal_pt"])
    raw = [ar_bbox(t, probe) for t in texts]
    widest = max([1.0] + [b[2] - b[0] for b in raw])
    pt = fit_pt(ARABIC["nominal_pt"], ARABIC["min_pt"], max_ar_w, widest)
    ar_font = truetype(AR_FONT, pt)

    en_font = truetype(EN_FONT, ENGLISH["cap_pt"])
    tracking = ENGLISH["tracking_pct"] / 100.0 * ENGLISH["cap_pt"]
    max_en_w = ENGLISH["max_line_width_frac"] * W
    en_lines = [wrap_english(e["text"], en_font, tracking, max_en_w)
                for e in english]
    cap_bb = en_font.getbbox("H")
    cap_h = cap_bb[3] - cap_bb[1]
    gap = int(ENGLISH["gap_below_arabic_frac"] * H)
    line_h = ENGLISH["line_height_px"]

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
    mids = sorted(sum(bounds(*rows(text, lines, trial))) / 2.0
                  for text, lines in zip(texts, en_lines))
    # anchor -> block-centre offset, measured on the median phrase
    drop = mids[len(mids) // 2] - trial
    ar_cy = int(round(BLOCK_CENTER_Y_FRAC * H - drop)) + int(y_offset)

    out = {"pt": pt, "ar_font": ar_font, "en_font": en_font, "cx": cx,
           "ar_cy": ar_cy, "tracking": tracking, "widest": widest,
           "max_ar_w": max_ar_w, "phrases": []}
    for i, (text, lines) in enumerate(zip(texts, en_lines), start=1):
        bb, base = rows(text, lines, ar_cy)
        top, bot = bounds(bb, base)
        out["phrases"].append({"i": i, "text": text, "ar_bbox": bb,
                               "english": base, "top": top, "bottom": bot})
    return out


# ---------------------------------------------------------------------------
# drawing
# ---------------------------------------------------------------------------

def with_shadow(ink):
    """The ink plus ONE soft drop shadow struck from its merged alpha: a solid
    black layer through that alpha, pushed down-right and blurred, ink over it
    -- a lift off the footage, not an outline ring."""
    W, H = CANVAS_W, CANVAS_H
    off = SHADOW["offset_px"]
    solid = Image.new("RGBA", (W, H), SHADOW["color"] + (SHADOW["alpha"],))
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sh.paste(solid, (0, 0), ink.getchannel("A"))
    sh = sh.transform((W, H), Image.AFFINE, (1, 0, -off, 0, 1, -off))
    sh = sh.filter(ImageFilter.GaussianBlur(SHADOW["blur_px"]))
    return Image.alpha_composite(sh, ink)


def draw_layers(lay, out_dir):
    """One full-canvas RGBA card per phrase: Arabic + English + the shared
    shadow. -> [{"path", "ar_w", "en_lines"}]."""
    os.makedirs(out_dir, exist_ok=True)
    report = []
    for p in lay["phrases"]:
        ink = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        ImageDraw.Draw(ink).text(
            (lay["cx"], lay["ar_cy"]), p["text"], font=lay["ar_font"],
            fill=ARABIC["color"] + (255,), anchor="mm",
            direction="rtl", language="ar")
        for ln, y in p["english"]:
            toks = en_tokens(ln)
            w = en_width(toks, lay["en_font"], lay["tracking"])
            draw_en_line(ink, lay["cx"] - w / 2.0, y, toks, lay["en_font"],
                         lay["tracking"], ENGLISH["color"] + (ENGLISH["alpha"],))
        path = os.path.join(out_dir, "phrase%d.png" % p["i"])
        with_shadow(ink).save(path)
        bb = p["ar_bbox"]
        report.append({"path": path, "ar_w": bb[2] - bb[0],
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


def grade_plate(lay, tmp):
    """The single black plate that seats the type: flat dim, plus a vignette,
    plus a backing gradient centred just below the Arabic anchor (live footage
    runs brighter than the account's moody grade, and warm-white type over a
    sunlit thobe needs the backing to read at all). Capped so the darkest
    pixel is still footage."""
    W, H = CANVAS_W, CANVAS_H
    v, t = GRADE["vignette"], GRADE["backing"]
    combo = Image.new("L", (W, H), GRADE["base"])
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


def signature_card(text, tmp, offset=0):
    """`offset` moves the line vertically only (+ lower / - higher); the
    signature is ALWAYS horizontally centered -- no x knob on purpose."""
    W, H = CANVAS_W, CANVAS_H
    font = ImageFont.truetype(EN_FONT, max(12, int(H * SIGNATURE_SIZE_FRAC)))
    bbox = _PROBE.textbbox((0, 0), text, font=font)
    ink = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ink).text(
        (W // 2 - (bbox[2] - bbox[0]) // 2 - bbox[0],
         int(H * SIGNATURE_Y_FRAC) - (bbox[3] - bbox[1]) // 2 - bbox[1]
         + int(offset)),
        text, font=font, fill=(255, 255, 255, 255))
    card = with_shadow(ink)
    card.putalpha(card.getchannel("A").point(
        lambda a: int(round(a * SIGNATURE_ALPHA))))
    path = os.path.join(tmp, "signature.png")
    card.save(path)
    return path


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

def source_chain(crop):
    """[0:v] -> exactly 1920x1080 of footage. `crop` is the authored window in
    SOURCE pixels (pipeline/crop.py solves it); without one the footage is
    cover-scaled, never distorted."""
    if crop:
        return ("crop=%d:%d:%d:%d,scale=%d:%d:flags=lanczos"
                % (crop["w"], crop["h"], crop["x"], crop["y"],
                   CANVAS_W, CANVAS_H))
    return ("scale=%d:%d:flags=lanczos:force_original_aspect_ratio=increase,"
            "crop=%d:%d" % (CANVAS_W, CANVAS_H, CANVAS_W, CANVAS_H))


def build_graph(src, dur, crop, grade_png, card_pngs, sched, ln, afade,
                sig_png=None):
    """-> (filter_complex, input argv). Inputs: [0]=source, [1]=grade plate,
    [2..]=one card per phrase, then the signature (last, so dropping it cannot
    shift any other index).

    Every looped PNG input is bounded by the output's `-t`: a `-loop 1` input
    never EOFs, and overlay's default eof_action would leave the graph with no
    end at all -- the encode runs forever into a file with no moov atom.
    """
    tqs = ["-thread_queue_size", str(THREAD_QUEUE_SIZE)]
    argv = ["-ss", "0.000", "-t", "%.3f" % dur] + tqs + ["-i", src]
    for png in [grade_png] + list(card_pngs) + ([sig_png] if sig_png else []):
        argv += ["-framerate", str(FPS), "-loop", "1"] + tqs + ["-i", png]

    parts = ["[0:v]%s,setsar=1,fps=%d,format=rgba[bg];"
             "[1:v]format=rgba[grd];[bg][grd]overlay=0:0:format=auto[b0]"
             % (source_chain(crop), FPS)]
    base = "b0"
    for i, (t_in, d_in, t_out, d_out) in enumerate(sched):
        parts.append(";[%d:v]format=rgba,"
                     "fade=t=in:st=%.3f:d=%s:alpha=1,"
                     "fade=t=out:st=%.3f:d=%s:alpha=1[c%d]"
                     % (i + 2, t_in, d_in, t_out, d_out, i + 1))
        parts.append(";[%s][c%d]overlay=0:0:format=auto[b%d]"
                     % (base, i + 1, i + 1))
        base = "b%d" % (i + 1)
    if sig_png:
        sidx = 2 + len(card_pngs)
        parts.append(";[%d:v]format=rgba[sg];[%s][sg]overlay=0:0:format=auto[sig]"
                     % (sidx, base))
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
    dur = info["duration"]
    if not info["width"]:
        raise SystemExit("hz style needs footage; it has no still-photo mode")

    crop = cfg.get("crop")
    if crop and not all(k in crop for k in ("x", "y", "w", "h")):
        raise SystemExit("crop must carry x, y, w, h")

    lay = layout(phrases, english, cfg["x_offset"], cfg["y_offset"])
    print("      Arabic pt=%d (nominal %d, widest raw %dpx, cap %dpx)"
          % (lay["pt"], ARABIC["nominal_pt"], lay["widest"], lay["max_ar_w"]))
    print("      caption column x=%d (%.3f W), Arabic anchor y=%d (%.3f H)"
          % (lay["cx"], lay["cx"] / float(CANVAS_W),
             lay["ar_cy"], lay["ar_cy"] / float(CANVAS_H)))
    rep = draw_layers(lay, os.path.join(tmp, "overlays"))
    for r, p in zip(rep, lay["phrases"]):
        print("      phrase%d: ar_w=%dpx en_lines=%d block %d..%d (centre %.3f H)"
              % (p["i"], r["ar_w"], r["en_lines"], p["top"], p["bottom"],
                 (p["top"] + p["bottom"]) / 2.0 / CANVAS_H))

    grade_png = grade_plate(lay, tmp)
    sig_png = (signature_card(cfg["signature"], tmp, cfg["signature_offset"])
               if cfg["signature"] else None)
    sched = schedule(phrases, dur)

    print("      loudnorm pass 1...")
    ln = loudnorm_filter(measure_loudness(src, dur))
    afade = ("afade=t=in:st=0:d=%s,afade=t=out:st=%.3f:d=%s"
             % (AUDIO["fade_in_s"], dur - AUDIO["fade_out_s"],
                AUDIO["fade_out_s"]))

    fc, in_argv = build_graph(src, dur, crop, grade_png,
                              [r["path"] for r in rep], sched, ln, afade,
                              sig_png)

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
    print("      %s | %dx%d | crop %s"
          % (os.path.relpath(out, ROOT), CANVAS_W, CANVAS_H,
             ("%dx%d+%d+%d" % (crop["w"], crop["h"], crop["x"], crop["y"]))
             if crop else "cover-scaled"))
