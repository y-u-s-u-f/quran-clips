"""pipeline/render_bars.py -- the `bars` style renderer, full port.

Vertical 1080x1920: the footage letterboxed into a centred 16:9 band on pure
black, graded down to the reference's luma, white Thuluth on equal-height
colour pills, a right-to-left wipe on the first card and strictly sequential
crossfades after it, and the band FX chain -- glow, bar glow, text glow,
scan, procedural snow, heat shimmer -- confined strictly to the band (any
spill into the letterbox instantly reads as wrong).

Ported from legacy/scripts/render_bars.py + legacy/scripts/build_bars.py +
legacy/qc/timeline.py, with the style numbers from legacy/templates/bars.yaml
(pixel forensics of the account's two reference reels). The effects
themselves live in pipeline/fx.py.

THE GOLDEN BYTE-IDENTITY CONTRACT IS SUSPENDED as of the 2026-08-01 speed
work: the emitted filtergraph is deliberately no longer byte-identical to
legacy/tests/golden/at-tawbah-128-128/filtergraph.txt. Four changes did it,
all owner-approved, none of them a look decision:
  * heat's two perlin maps are pre-baked and fed in as inputs (heat_layers);
  * wide gaussians run at half linear size (fx.blur) and bar glow blurs a
    band-sized slice instead of the full 1920-tall canvas;
  * heat's supersample went 3 -> 2;
  * caption layers are cropped to their own ink and placed, instead of being
    full-canvas plates overlaid at 0:0 three times each (trim_to_ink), and
    every one of those overlays is gated to the card's own window.
Measured against the pre-change render of sources/gt9y-QGgMsA/waqiah-83-87:
PSNR 47.1dB for the bake, 53.1dB for the blur work, 48.8dB for the
supersample, i.e. below the 8-bit quantiser in each case; wall time 372s ->
150s on a 4P+4E machine. Until a replacement fixture exists, the guarantee
that a look change is never a refactor side-effect rests on re-measuring
PSNR against the previous render, NOT on diffing the graph text.

Per-reel switches, via the config's `fx:` map (e.g. `fx: {heat: false}` --
heat is ~27% of the remaining render time, measured 118s -> 87s on a 27.5s
8-card reel, and is the one to drop for a fast preview): grade, scrim, glow,
barglow, textglow, scan, snow, heat.

Called by generate.py with the resolved plan; not a standalone CLI.
"""
import colorsys
import json
import math
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FONT_DIR = os.path.join(ROOT, "assets", "fonts")
sys.path.insert(0, HERE)

import fx as FX  # noqa: E402

FFMPEG = os.environ.get("QC_FFMPEG") or "ffmpeg"

if not features.check("raqm"):
    sys.exit("FATAL: Pillow lacks RAQM (HarfBuzz+FriBiDi) -- Arabic would be "
             "laid out unjoined, left to right. Use tools/render-venv/bin/python.")

# ---------------------------------------------------------------------------
# Style constants -- legacy/templates/bars.yaml, derived from pixel
# measurements of the two reference reels (720x1280 refs x1.5 -> 1080).
# ---------------------------------------------------------------------------
CANVAS_W, CANVAS_H = 1080, 1920
FPS = 30

# 16:9 footage band, centred vertically, pure #000000 above and below.
BAND_W, BAND_H, BAND_Y = 1080, 608, 656

# Step 0 of the look: bring the band to mean luma 0.15-0.32 before any glow.
GRADE_EQ = "brightness=-0.30:contrast=1.30:saturation=0.75:gamma=0.72"
GRADE_CB = "rm=0.08:gm=0.02:bm=-0.10"
GRADE_VIGNETTE = "a=PI/8"       # refs are NOT radially vignetted; the
                                # directional darkening is the scrim below
# The edge scrim: a LEFT ramp plus a soft top/bottom feather where the
# picture meets the letterbox -- and nothing on the right. Not a radial
# shape, so it is a generated multiply plate.
SCRIM = {"left_min": 0.20, "left_to_px": 180,
         "edge_min": 0.15, "edge_to_px": 190}

TEXT = {
    "font": os.path.join(FONT_DIR, "AM_Thulth_Regular_0.1.ttf"),
    "color": (255, 255, 255),
    "nominal_pt": 96,
    "min_pt": 60,
    # Per-line ink cap; bar = ink + 2*pad and the widest ref bar is 0.503 W.
    "max_line_width_frac": 0.45,
    # Bar centre lines as fractions of canvas height (measured rows).
    "line1_center_y_frac": 0.4637,
    "line2_center_y_frac": 0.5592,
    "single_center_y_frac": 0.5111,
}
# ONE drop shadow on the glyphs -- a solid offset copy of the silhouette,
# pure black, fully opaque, ZERO blur, pushed straight DOWN. It rides OVER
# the pill, darkening the bar where a glyph overhangs it (as measured).
TEXT_SHADOW = {"color": (0, 0, 0), "opacity": 1.0,
               "dx_frac": 0.0, "dy_frac": 0.003, "blur_px": 0}

# The pill is a highlight band struck THROUGH the letter bodies, not a
# background box: 44px tall against ~125px of glyph ink.
BAR = {"height_px": 44, "pad_x_px": 27,
       # Baseline-anchored: this face's tashkeel overshoot the em box by
       # ~0.23em, so neither the ink bbox nor font metrics can place the bar.
       "baseline_below_center_px": 6,
       # The auto/config colour is the bar as it must READ IN THE FINISHED
       # FRAME; the FX screen passes lift the pill by a measured near-constant
       # factor, so it is DRAWN darker to land on target (predraw_color).
       "fx_screen_lift": 0.179, "fx_sat_retain": 0.88}
# Auto-derivation of the pill colour from the clip's own GRADED band.
BAR_AUTO = {"lightness_gain": 1.78, "lightness_min": 0.34, "lightness_max": 0.48,
            "saturation_gain": 2.2, "saturation_min": 0.22, "saturation_max": 0.60,
            "saturation_lowchroma_below": 0.12,
            "sample_fps": 1, "sample_tile": 5}

# The first card WIPES (directional RTL sweep of the pill; its text
# cross-fades over the same window, which is what the refs show); later cards
# crossfade. Captions are STRICTLY SEQUENTIAL: caption N's fade-out reaches
# alpha 0 at or before caption N+1's fade-in leaves 0, both fades fitted into
# the gap the recitation actually leaves. A wipe-out is EXEMPT from the
# shrink (anchor `end`): it keeps its full duration, mirrors its wipe-in, and
# eats the outgoing caption's tail instead of snapping.
TRANSITIONS = {"first": "wipe", "rest": "crossfade", "wipe_target": "bar",
               "sequential": True, "min_fade_s": 0.20, "crossfade_s": 0.95,
               "wipe_in_s": 1.02, "wipe_out_s": 1.02,
               "wipe_out_anchor": "end", "wipe_feather_px": 20}

# Band FX parameters -- see pipeline/fx.py for what each one is and
# legacy/style/refs2/FX_RECIPE.md for the measurements behind the numbers.
FX_CFG = {
    "glow": {"lo": 0.20, "hi": 0.40, "scatter": 10.0, "gain": 0.35},
    "barglow": {"sigma_near_px": 21, "weight_near": 0.22,
                "sigma_far_px": 90, "weight_far": 0.42, "gain": 1.20},
    "textglow": {"sigma_px": 14, "gain": 0.45, "tint": "#FFF4E0"},
    "scan": {"threshold": 0.90, "scatter": 4.0, "radius": 0.70, "gain": 1.40},
    "snow": {"amount": 0.70, "size": 0.10, "speed": -1.0, "flicker": 1.0,
             "intensity": 1.0, "gain": 7.0, "texel_scale": 72.0,
             "tint_mix": 1.00, "sway_texels": 3.5, "seed": 7, "loop_s": 8},
    "heat": {"rms_frac_w": 0.00133, "xscale": 2.0, "tscale": 0.5,
             "scroll_v": -0.004, "octaves": 6, "persistence": 0.6,
             "supersample": 2, "perlin_centre": 130.26, "perlin_sd": 18.4,
             "seed_x": 11, "seed_y": 77},
}
SWITCHES = ("grade", "scrim") + FX.BAND_ORDER

VIDEO_FADE_IN_S, VIDEO_FADE_OUT_S = 0.3, 0.5
AUDIO = {"lufs": -14.0, "tp": -1.0, "lra": 11.0,
         "fade_in_s": 0.3, "fade_out_s": 0.5}
ENCODE = {"crf": 18, "preset": "slow", "audio_bitrate": "192k"}

# Signature: same house geometry as the default style, landing in the bottom
# letterbox (0.945 H = 1814px, well below the band's end at 1264).
SIGNATURE_FONT = os.path.join(FONT_DIR, "Albertus MT Lt Regular.ttf")
SIGNATURE_SIZE_FRAC = 0.026
SIGNATURE_Y_FRAC = 0.945
SIGNATURE_ALPHA = 0.87

# House normalisation of the caption text (see legacy/qc/arabic.py):
# U+06DF renders ZERO WIDTH in AM_Thulth (silent width corruption) and
# U+06ED reads as a stray floating meem.
STRIP_MARKS = {"۟", "ۭ"}
# Combining marks, for the tashkeel-stripped "letter body" band the pill must
# sit inside -- the marks overshoot the em box, so the full ink bbox would
# place it wrong.
TASHKEEL = set("ًٌٍَُِّْٓ"
               "ٰٕٔۖۗۘۙۚۛ"
               "ۣۜ۟۠ۡۢۤۧۨ"
               "۪ۭ۫۬")


def norm_ar(s):
    return "".join(c for c in s if c not in STRIP_MARKS)


def strip_tashkeel(s):
    return "".join(c for c in s if c not in TASHKEEL)


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def switches(clip_fx):
    """name -> bool for every switchable stage, template default (all on)
    merged with the reel config's `fx:` overrides. Unknown name = error --
    a typo'd one would silently do nothing."""
    on = {n: True for n in SWITCHES}
    for k, v in (clip_fx or {}).items():
        if k not in on:
            raise SystemExit("unknown fx switch %r (known: %s)"
                             % (k, ", ".join(SWITCHES)))
        on[k] = bool(v)
    return on


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------
_PROBE = ImageDraw.Draw(Image.new("L", (1, 1)))


def truetype(pt):
    return ImageFont.truetype(TEXT["font"], pt,
                              layout_engine=ImageFont.Layout.RAQM)


def bbox_ls(text, font):
    """Ink bbox with the pen at (0,0) ON THE BASELINE -> (l, top, r, bot),
    top negative (above baseline), bot positive (below)."""
    return _PROBE.textbbox((0, 0), text, font=font, anchor="ls",
                           direction="rtl", language="ar")


def fit_pt(nominal_pt, min_pt, max_width, widest):
    """One shared point size for every phrase (a caption swap never changes
    the type size), shrunk only far enough that the WIDEST line fits. Never
    scaled UP, never below min_pt."""
    return max(int(min_pt), int(nominal_pt * min(1.0, max_width / widest)))


# ---------------------------------------------------------------------------
# grade + bar colour
# ---------------------------------------------------------------------------

def band_source_chain(crop, grade_on=True):
    """[0:v] -> the graded 1080x608 footage band (no pad, no captions).
    The grade also feeds the bar-colour derivation, so turning it off moves
    the pill hue as well as the picture."""
    if crop:
        pre = ("crop=%d:%d:%d:%d,scale=%d:%d:flags=lanczos"
               % (crop["w"], crop["h"], crop["x"], crop["y"], BAND_W, BAND_H))
    else:
        pre = ("scale=%d:%d:flags=lanczos:force_original_aspect_ratio=increase,"
               "crop=%d:%d" % (BAND_W, BAND_H, BAND_W, BAND_H))
    if not grade_on:
        return pre
    return "%s,eq=%s,colorbalance=%s,vignette=%s" % (
        pre, GRADE_EQ, GRADE_CB, GRADE_VIGNETTE)


def derive_bar_color(src, dur, crop, tmp, grade_on=True):
    """Sample the clip's own GRADED band and apply the measured rule:
        H = hue of the band's mean RGB
        L = clamp(1.78 * L_mean, 0.34, 0.48)
        S = clamp(2.2  * S_mean, 0.22, 0.60)
    with a low-chroma fallback for S: a near-neutral scene's mean RGB has a
    meaningless saturation (colours cancel in the mean), so below the
    threshold the mean of the per-pixel saturations is used instead.

    `fps=1` runs FIRST: it used to sit after the lanczos scale and the grade,
    so 24 of every 25 frames were scaled and graded and then discarded
    (8.57s -> 1.89s CPU, -78%, on a 23s 1080p clip; it scales with source
    resolution and length). This moves the pill by up to one LSB on one
    channel -- `vignette` dithers per frame, so feeding it 25 frames instead
    of 700 advances its PRNG differently -- which is a look change, but a
    sub-quantiser one on a colour that is a heuristic to begin with. Pin
    `bar_color:` in the config to bypass the pass entirely."""
    a = BAR_AUTO
    tile = int(a["sample_tile"])
    tile_png = os.path.join(tmp, "bar_color_sample.png")
    vf = ("fps=%s," % a["sample_fps"] + band_source_chain(crop, grade_on)
          + ",scale=48:27,tile=%dx%d" % (tile, tile))
    run([FFMPEG, "-v", "error", "-y", "-t", "%.3f" % dur, "-i", src,
         "-vf", vf, "-frames:v", "1", tile_png])

    px = [q for q in Image.open(tile_png).convert("RGB").getdata() if sum(q)]
    if not px:
        raise SystemExit("bar-colour sampling produced an empty tile")
    n = len(px)
    mr = sum(q[0] for q in px) / n
    mg = sum(q[1] for q in px) / n
    mb = sum(q[2] for q in px) / n
    dh, l_mean, s_mean = colorsys.rgb_to_hls(mr / 255.0, mg / 255.0, mb / 255.0)
    if s_mean < a["saturation_lowchroma_below"]:
        s_mean = sum(colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)[2]
                     for r, g, b in px) / n
    l_bar = min(a["lightness_max"], max(a["lightness_min"],
                                        a["lightness_gain"] * l_mean))
    s_bar = min(a["saturation_max"], max(a["saturation_min"],
                                         a["saturation_gain"] * s_mean))
    r, g, b = colorsys.hls_to_rgb(dh, l_bar, s_bar)
    rgb = tuple(int(round(v * 255)) for v in (r, g, b))
    print("      bar colour auto: #%02X%02X%02X (H %.1f L %.3f S %.3f, %d px)"
          % (rgb + (dh * 360, l_bar, s_bar, n)))
    return rgb


def predraw_color(target_rgb):
    """The target colour is measured on FINISHED reels, i.e. AFTER the glow
    passes screen over the pill. Screening lifts the pill by a near-constant
    factor (measured: screen(drawn, 0.179) to within 0.006 across a sweep),
    so the pill is DRAWN darker (and a touch more saturated) to land on
    target."""
    lift = BAR["fx_screen_lift"]
    keep = BAR["fx_sat_retain"]
    r, g, b = [c / 255.0 for c in target_rgb]
    h_, l_, s_ = colorsys.rgb_to_hls(r, g, b)
    l_ = max(0.0, 1.0 - (1.0 - l_) / (1.0 - lift))
    s_ = min(1.0, s_ / keep)
    r, g, b = colorsys.hls_to_rgb(h_, l_, s_)
    return tuple(int(round(v * 255)) for v in (r, g, b))


def hexrgb(value):
    v = str(value).lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


# ---------------------------------------------------------------------------
# caption layout + drawing
# ---------------------------------------------------------------------------

def phrase_lines(ph, font, max_w):
    """A phrase's caption line(s), house-normalised.

    `line_split` from the config (words on line 1) wins; otherwise a single
    line when it fits the width cap, else the two-line split whose line
    widths are closest to equal -- the account's standard is the two lines of
    a card within ~30px of each other."""
    words = norm_ar(ph["text"]).split()
    split = ph.get("line_split")
    if split:
        k = max(1, min(len(words) - 1, int(split)))
        return [" ".join(words[:k]), " ".join(words[k:])]
    one = " ".join(words)
    l, _, r, _ = bbox_ls(one, font)
    if r - l <= max_w or len(words) < 2:
        return [one]
    best, best_diff = 1, None
    for k in range(1, len(words)):
        w1 = bbox_ls(" ".join(words[:k]), font)
        w2 = bbox_ls(" ".join(words[k:]), font)
        diff = abs((w1[2] - w1[0]) - (w2[2] - w2[0]))
        if best_diff is None or diff < best_diff:
            best, best_diff = k, diff
    return [" ".join(words[:best]), " ".join(words[best:])]


def layout(phrases, x_offset, y_offset):
    """WHERE every pill and baseline goes -- no drawing, no ffmpeg.
    draw_layers() consumes exactly these numbers, so the two can never
    disagree."""
    W, H = CANVAS_W, CANVAS_H
    cx = W // 2 + int(x_offset)          # centered by default
    max_w = TEXT["max_line_width_frac"] * W
    bar_h = BAR["height_px"]
    pad_x = BAR["pad_x_px"]
    base_off = BAR["baseline_below_center_px"]

    probe_font = truetype(TEXT["nominal_pt"])
    all_lines = [ln for ph in phrases
                 for ln in phrase_lines(ph, probe_font, max_w)]
    raw = [bbox_ls(ln, probe_font)[2] - bbox_ls(ln, probe_font)[0]
           for ln in all_lines]
    widest = max(1.0, max(raw))
    pt = fit_pt(TEXT["nominal_pt"], TEXT["min_pt"], max_w, widest)
    font = truetype(pt)

    y_line = [TEXT["line1_center_y_frac"] * H + y_offset,
              TEXT["line2_center_y_frac"] * H + y_offset]
    y_single = TEXT["single_center_y_frac"] * H + y_offset

    out = {"pt": pt, "font": font, "cx": cx, "widest": widest,
           "max_w": max_w, "phrases": []}
    for idx, ph in enumerate(phrases, start=1):
        lines = phrase_lines(ph, probe_font, max_w)
        centers = [y_single] if len(lines) == 1 else y_line[:len(lines)]
        rows = []
        for n, (ln, cyb) in enumerate(zip(lines, centers), start=1):
            l, top, r, bot = bbox_ls(ln, font)
            baseline = cyb + base_off
            # The pill must sit inside the tashkeel-stripped letter-body band.
            _, sb_top, _, sb_bot = bbox_ls(strip_tashkeel(ln), font)
            y0 = baseline - bar_h / 2.0 - base_off
            y0 = min(max(y0, baseline + sb_top), baseline + sb_bot - bar_h)
            rows.append({"n": n, "text": ln, "baseline": baseline,
                         "x0": cx - (r - l) / 2.0 - pad_x,
                         "x1": cx + (r - l) / 2.0 + pad_x,
                         "y0": y0, "y1": y0 + bar_h,
                         "pen_x": cx - (r - l) / 2.0 - l})
        out["phrases"].append({"i": idx, "lines": rows})
    return out


_SS = 4     # pill supersampling (Pillow's rounded_rectangle has no AA)


def draw_pill(canvas, x0, y0, x1, y1, rgb):
    """Hard-edged full pill (radius = height/2), antialiased in 2-3px like
    the references -- drawn supersampled into a tile, pasted as a mask."""
    w, h = int(round(x1 - x0)), int(round(y1 - y0))
    tile = Image.new("L", (w * _SS, h * _SS), 0)
    ImageDraw.Draw(tile).rounded_rectangle(
        [0, 0, w * _SS - 1, h * _SS - 1], radius=h * _SS / 2.0, fill=255)
    mask = tile.resize((w, h), Image.LANCZOS)
    canvas.paste(Image.new("RGBA", (w, h), rgb + (255,)),
                 (int(round(x0)), int(round(y0))), mask)


def with_shadow(ink, cfg, W, H):
    """The glyph layer plus its ONE drop shadow (beneath the ink): a solid,
    fully-opaque, un-blurred copy displaced straight DOWN -- its boundary
    exactly as sharp as the glyph's own antialiased edge."""
    dx = int(round(cfg["dx_frac"] * W))
    dy = int(round(cfg["dy_frac"] * H))
    alpha = ink.getchannel("A")
    if cfg["opacity"] < 1.0:
        alpha = alpha.point(lambda v: int(round(v * cfg["opacity"])))
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sh.paste(Image.new("RGBA", (W, H), cfg["color"] + (255,)), (0, 0), alpha)
    # NEAREST: an integer translate must not resample the silhouette soft.
    sh = sh.transform((W, H), Image.AFFINE, (1, 0, -dx, 0, 1, -dy),
                      resample=Image.NEAREST)
    if cfg["blur_px"] > 0:
        sh = sh.filter(ImageFilter.GaussianBlur(cfg["blur_px"]))
    return Image.alpha_composite(sh, ink)


def trim_to_ink(card):
    """A full-canvas card cropped to its non-transparent pixels -> (img, x, y).

    The same idea as render_default.trim_to_ink, and ported from it including
    the outward even-coordinate snap: `overlay` blends in yuv420 by default,
    so on an odd edge the overlay's 2x2 chroma/alpha blocks would straddle a
    different grid than the full-canvas card's and the result would shift.

    Worth it here because bars pays for a caption card THREE times -- the
    composite, the barglow accumulator and the textglow accumulator -- and
    the ink is ~680x270 against a 1080x1920 canvas, 11x smaller. Measured by
    differencing a 1-card against a 4-card render: 7.7s CPU and ~280 MB RSS
    per card, linear in card count, spent compositing (0,0,0,0)."""
    box = card.getbbox()
    if box is None:                       # an empty card composites to nothing
        return card, 0, 0
    x0, y0 = box[0] - box[0] % 2, box[1] - box[1] % 2
    x1 = min(card.width, box[2] + box[2] % 2)
    y1 = min(card.height, box[3] + box[3] % 2)
    return card.crop((x0, y0, x1, y1)), x0, y0


def draw_layers(lay, bar_rgb_drawn, out_dir):
    """TWO layers per phrase, so the compositor can animate them
    independently (the pill wipes, the text always cross-fades):
        barN.png  = the hard-edged opaque pill(s) alone
        textN.png = the Thuluth glyphs + their single hard drop shadow
    Drawn at full canvas and saved cropped to their own ink; the box each one
    came from travels with it, since every consumer now has to place it.
    Returns [{"bar", "text", "x0", "x1", "lines", "bar_box", "text_box"}]."""
    os.makedirs(out_dir, exist_ok=True)
    report = []
    for p in lay["phrases"]:
        bar_img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        ink = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        d = ImageDraw.Draw(ink)
        px0, px1 = CANVAS_W, 0
        for ln in p["lines"]:
            draw_pill(bar_img, ln["x0"], ln["y0"], ln["x1"], ln["y1"],
                      bar_rgb_drawn)
            d.text((ln["pen_x"], ln["baseline"]), ln["text"], font=lay["font"],
                   fill=TEXT["color"] + (255,), anchor="ls",
                   direction="rtl", language="ar")
            px0, px1 = min(px0, ln["x0"]), max(px1, ln["x1"])
        bp = os.path.join(out_dir, "bar%d.png" % p["i"])
        tp = os.path.join(out_dir, "text%d.png" % p["i"])
        bcrop, bx, by = trim_to_ink(bar_img)
        tcrop, tx, ty = trim_to_ink(
            with_shadow(ink, TEXT_SHADOW, CANVAS_W, CANVAS_H))
        bcrop.save(bp)
        tcrop.save(tp)
        report.append({"bar": bp, "text": tp, "x0": px0, "x1": px1,
                       "lines": len(p["lines"]),
                       "bar_box": (bx, by, bcrop.width, bcrop.height),
                       "text_box": (tx, ty, tcrop.width, tcrop.height)})
    return report


def scrim_plate(tmp):
    """Left ramp x vertical feather multiply plate for the band."""
    lo, to = SCRIM["left_min"], SCRIM["left_to_px"]
    eo, et = SCRIM["edge_min"], SCRIM["edge_to_px"]
    w, h = BAND_W, BAND_H
    path = os.path.join(tmp, "scrim_%dx%d.png" % (w, h))
    if not os.path.exists(path):
        img = Image.new("L", (w, h))
        px = img.load()
        colx = [min(1.0, lo + (1.0 - lo) * x / to) for x in range(w)]
        for y in range(h):
            fy = min(1.0, eo + (1.0 - eo) * min(y, h - 1 - y) / et)
            for x in range(w):
                px[x, y] = int(round(255.0 * colx[x] * fy))
        img.convert("RGB").save(path)
    return path


def heat_layers(tmp, dur):
    """The two perlin displacement maps heat needs, baked to mp4 and cached.

    Cached OUTSIDE the reel's tmp, one level up, because unlike snow these
    depend on nothing about the reel: snow is tinted by the clip's own bar
    colour, while these are pure geometry from FX_CFG constants. Baked once
    on a machine, every later reel reuses them. That matters because the bake
    is the expensive part -- `perlin` is single-threaded and runs at ~0.4x
    realtime, 66s per map for a 26s reel.

    Cached in 30s buckets: a bake is reusable by any reel no longer than it,
    so most reels hit the same 30s pair and a long one bakes its own. crf 8
    on flat low-frequency noise, matching the snow loop's setting -- the maps
    are read back through a 3x bicubic upscale, which is far softer than the
    quantiser.
    """
    c = FX_CFG["heat"]
    span = int(math.ceil(max(dur, 1.0) / 30.0) * 30)
    cache = os.path.dirname(os.path.normpath(tmp)) or tmp
    os.makedirs(cache, exist_ok=True)
    # Keyed on exactly what determines the baked BYTES -- the perlin source's
    # own arguments plus geometry and length. Not on supersample, scroll_v,
    # rms_frac_w or the perlin_centre/sd pair: those live in the graph's
    # scroll/scale/lut, downstream of the file, and folding them in here would
    # throw away a valid 164s bake every time one of them is tuned.
    tag = "%dx%d_%d_%ds_o%s_p%s_x%s_t%s" % (
        BAND_W, BAND_H, FPS, span, c.get("octaves", 6),
        c.get("persistence", 0.6), c["xscale"], c["tscale"])
    out = []
    for axis, seed in (("x", int(c.get("seed_x", 11))),
                       ("y", int(c.get("seed_y", 77)))):
        path = os.path.join(cache, "heat%s_%s_s%d.mp4" % (axis, tag, seed))
        if not os.path.exists(path):
            print("      heat map %s: baking %ds of perlin (once per machine, "
                  "reused by every reel)" % (axis, span))
            src = FX.heat_perlin_src(c, BAND_W, BAND_H, FPS, seed)
            # Bake to a pid-tagged temp and rename only on success: the cache
            # is keyed by name alone, so a file that exists must be complete.
            # Deleting on a bad returncode is not enough -- a bake killed from
            # outside (^C, a timeout, the OOM killer) never reaches that line,
            # and the truncated map it leaves behind then loads as a valid
            # short input and fails the render on every later run.
            # The pid goes BEFORE the extension: ffmpeg picks its muxer off
            # the suffix, and a file ending .part has no format to infer.
            _b, _e = os.path.splitext(path)
            part = "%s.%d.part%s" % (_b, os.getpid(), _e)
            try:
                r = subprocess.run(
                    [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", src,
                     "-t", str(span), "-an", "-c:v", "libx264", "-crf", "8",
                     "-preset", "medium", "-pix_fmt", "yuv420p", part])
                if r.returncode:
                    sys.exit("ffmpeg heat-map bake failed")
                os.replace(part, path)
            finally:
                if os.path.exists(part):
                    os.remove(part)
        out.append(path)
    return out


def snow_layer(tmp, tint_rgb):
    """The seamlessly-looping snow mp4, cached in tmp by parameter tag.
    Expensive to evaluate (one shader pass per frame), so it is only built
    when the effect is on."""
    c = dict(FX_CFG["snow"])
    tag = "%dx%d_%02X%02X%02X_%s" % (
        BAND_W, BAND_H, *tint_rgb,
        "_".join(str(c[k]) for k in sorted(c)))
    path = os.path.join(tmp, "snow_%s.mp4" % tag)
    if not os.path.exists(path):
        fld, nf = FX.render_snow(path, BAND_W, BAND_H, FPS, c, tint_rgb)
        print("      snow layer: shader pass, texel %.1fpx expo %.1f "
              "gain %.1f, %d frames" % (fld.texel, fld.expo, fld.gain, nf))
    return path


# ---------------------------------------------------------------------------
# transition schedule
# ---------------------------------------------------------------------------

def schedule(phrases, dur, kinds=None):
    """One (kind, t_in, d_in, t_out, d_out) per phrase, plus the indices
    whose changeover had no room left to fade at all (hard cuts).

    The magic offsets were tuned on the reference reels: the first card is up
    a hair (0.05s) before its own first word; the last starts leaving 0.15s
    before its last word ends so it is gone into the tail fade. Sequential:
    every changeover is serialised into the recitation's own gap -- both
    fades shrink together and proportionally, never below min_fade_s, and
    the outgoing card is gone half a frame before the incoming one starts.
    A wipe-out (anchor `end`) is exempt from the shrink: it keeps its full
    duration, mirroring its wipe-in, and starts BEFORE its t1 -- losing a few
    frames of tail beats a 0.2s snap-out."""
    tr = TRANSITIONS
    n_ph = len(phrases)
    if kinds is None:
        kinds = [str(tr["first"] if i == 0 else tr["rest"]).lower()
                 for i in range(n_ph)]
    xf = float(tr["crossfade_s"])
    wipe_in_s, wipe_out_s = float(tr["wipe_in_s"]), float(tr["wipe_out_s"])
    minf = float(tr["min_fade_s"])

    def snap(t):
        """On the frame grid, so a fade-out's last frame and the next
        fade-in's first frame cannot straddle one frame through rounding."""
        return round(t * FPS) / float(FPS)

    sched = []
    for i, ph in enumerate(phrases):
        d_in = wipe_in_s if kinds[i] == "wipe" else xf
        d_out = wipe_out_s if kinds[i] == "wipe" else xf
        t_in = max(0.0, ph["start"] - 0.05) if i == 0 else ph["start"] - d_in
        t_out = (min(dur - d_out, ph["end"] - 0.15)
                 if i == n_ph - 1 else ph["end"])
        sched.append([kinds[i], t_in, d_in, t_out, d_out])

    cuts = []
    guard = 0.5 / FPS
    for i in range(n_ph - 1):
        t0n, t1c = float(phrases[i + 1]["start"]), float(phrases[i]["end"])
        gap = t0n - t1c
        d_out, d_in = sched[i][4], sched[i + 1][2]
        nom_out = wipe_out_s if kinds[i] == "wipe" else xf
        anch = kinds[i] == "wipe" and \
            str(tr["wipe_out_anchor"]).lower() == "end"
        if d_out + d_in + guard > gap:            # shrink both, in ratio
            k = max(0.0, gap - guard) / (d_out + d_in)
            d_out, d_in = max(minf, d_out * k), max(minf, d_in * k)
        if anch:                                  # ...but not the wipe-out
            d_out = nom_out
        t_in = snap(t0n - d_in)                   # up on the word onset
        end = t_in - guard                        # outgoing gone by here
        slack = end - d_out - snap(t1c)
        if slack > 0 and not anch:                # spare gap: fade slower
            d_out = min(nom_out, d_out + slack)
        t_out = end - d_out
        floor = sched[i][1] + sched[i][2]         # fully up only by here
        if t_out < floor:                         # -> cannot fade at all
            d_out = max(1.0 / FPS, end - floor)
            t_out = end - d_out
            cuts.append(i + 1)
        sched[i + 1][2], sched[i + 1][1] = d_in, t_in
        sched[i][4], sched[i][3] = d_out, t_out
    return [tuple(x) for x in sched], cuts


# ---------------------------------------------------------------------------
# audio (two-pass loudnorm + fades)
# ---------------------------------------------------------------------------

def measure_loudness(src, dur):
    # -vn: without it the null muxer still pulls the video stream, so pass 1
    # decodes the whole clip's picture to measure its audio (measured 2.07s ->
    # 0.60s CPU on a 23s 1080p source; the JSON is byte-identical).
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
# the filtergraph (a faithful port of legacy build_bars.build)
# ---------------------------------------------------------------------------

def build_graph(src, dur, crop, rep, sched, tint, on, snow_path, scrim_path,
                ln, afade, sig_path=None, heat_paths=None):
    """-> (filter_complex, input argv). Inputs: [0]=source, [1..2n]=bar,text
    per phrase, then snow, then scrim (last of the legacy set, so dropping it
    cannot shift any other index), then the signature, then the two heat
    maps. The heat pair goes LAST for the same reason scrim went last: every
    index before it keeps the number it had before the maps were pre-baked."""
    W, H = CANVAS_W, CANVAS_H
    tr = TRANSITIONS
    nphrases = len(rep)
    phrase_pngs = [p[k] for p in rep for k in ("bar", "text")]

    chain = FX.fx_chain(FX_CFG, on)
    fx_layer = FX.by_layer(chain)

    ph_base = 1
    g_ = FX.Graph()
    vin = g_.input(src, ss="0.000", t="%.3f" % dur)
    ph_in = [g_.input(png, framerate=FPS, loop=1) for png in phrase_pngs]
    snow_in = g_.input(snow_path, stream_loop=-1) if snow_path else None
    scrim_in = (g_.input(scrim_path, framerate=FPS, loop=1)
                if on["scrim"] else None)
    sig_in = (g_.input(sig_path, framerate=FPS, loop=1) if sig_path else None)
    # stream_loop so a reel longer than its cached bucket still runs out to
    # the end; the bucket is sized so this normally never wraps.
    heat_in = [g_.input(p, stream_loop=-1) for p in (heat_paths or [])]

    # Every caption layer is now an ink-sized tile that has to be PLACED, and
    # every consumer of one (the composite here, both glow accumulators in
    # fx.py) needs the same box and the same window. `gate` is the layer's
    # own alpha window widened by a frame each side, so the enable can only
    # ever switch on frames the layer was already fully transparent for.
    boxes = [{"bar": r["bar_box"], "text": r["text_box"]} for r in rep]
    gates = ["between(t,%.3f,%.3f)"
             % (max(0.0, t_in - 1.0 / FPS), t_out + d_out + 1.0 / FPS)
             for _k, t_in, _di, t_out, d_out in sched]

    ctx = FX.Ctx(W=W, H=H, BW=BAND_W, BH=BAND_H, BY=BAND_Y, fps=FPS, dur=dur,
                 tint=tint, nphrases=nphrases, band_src="band",
                 snow_in=snow_in, hexrgb=hexrgb, boxes=boxes, gates=gates,
                 heat_x_in=heat_in[0] if heat_in else None,
                 heat_y_in=heat_in[1] if heat_in else None)

    wtgt = str(tr["wipe_target"]).lower()
    g_.chain(vin, [band_source_chain(crop, on["grade"]), "setsar=1",
                   "fps=%d" % FPS, "format=gbrp"], "bnd")
    if on["scrim"]:
        g_.chain(scrim_in, "format=gbrp", "scr")
        g_.chain(["bnd", "scr"],
                 ["blend=all_mode=multiply:shortest=1", "format=rgba",
                  "pad=%d:%d:0:%d:color=black" % (W, H, BAND_Y)], "bg")
    else:
        g_.chain("bnd", ["format=rgba",
                         "pad=%d:%d:0:%d:color=black" % (W, H, BAND_Y)], "bg")
    # The caption layers are accumulated a second time onto black plates, to
    # key the barglow / textglow passes off; `d=` keeps those branches finite.
    for eff in FX.plates(chain):
        eff.plate(g_, ctx)
    base = "bg"
    for i, (kind, t_in, d_in, t_out, d_out) in enumerate(sched):
        masks = {}
        if kind == "wipe":
            x0, x1 = rep[i]["x0"], rep[i]["x1"]
            feather = float(tr["wipe_feather_px"])
            # front travel: the full bar span plus enough margin for the
            # feathered front to clear both ends completely.
            trav = (x1 - x0) + 2 * feather + 40
            a = t_in + d_in                       # entry complete
            # A full-frame white plate slid over black: entering, its LEFT
            # edge is the front (right edge pinned); leaving, its RIGHT edge
            # is the front, dragged off to the left. min(0,...) keeps the
            # plate flush during the steady phase. (drawbox is unusable: its
            # `t` is box thickness, not time.)
            fi = f"{x1:.1f}-{trav:.1f}*(t-{t_in:.3f})/{d_in:.3f}"
            fo = f"{x1:.1f}-{trav:.1f}*(t-{t_out:.3f})/{d_out:.3f}"
            xe = f"if(lt(t,{a:.3f}),max(0,{fi}),min(0,{fo}-{W}))"
            g_.chain(None, f"color=c=black:s={W}x{H}:r={FPS}", f"kb{i + 1}")
            g_.chain(None, f"color=c=white:s={W}x{H}:r={FPS}", f"kw{i + 1}")
            g_.chain([f"kb{i + 1}", f"kw{i + 1}"],
                     [f"overlay=x='{xe}':y=0:shortest=1",
                      f"gblur=sigma={feather / 4.0:.2f}:steps=2",
                      "format=gray"],
                     f"m{i + 1}")
            if wtgt == "all":
                g_.chain(f"m{i + 1}", "split=2",
                         [f"mbar{i + 1}", f"mtext{i + 1}"])
                masks = {"bar": f"mbar{i + 1}", "text": f"mtext{i + 1}"}
            else:
                # `bar`: the pill alone rides the sweep; the text dissolves.
                masks = {"bar": f"m{i + 1}"}
        # bar first, text over it -- so the shadow (in the text layer) lands
        # ON the pill, as measured in the references.
        for j, name in enumerate(("bar", "text")):
            sidx = ph_base + 2 * i + j
            lbl = f"{name}{i + 1}"
            ox, oy, ow, oh = boxes[i][name]
            if name in masks:
                # The mask stays a full-canvas plate -- its sweep expression
                # is in canvas columns and the feather blur's boundaries are
                # part of the look -- and is CROPPED to the layer it gates.
                # A crop is exact, so the mask the layer sees is unchanged.
                g_.chain(masks[name], f"crop={ow}:{oh}:{ox}:{oy}", f"mc{lbl}")
                g_.chain(ph_in[sidx - ph_base], ["format=rgba", "split=2"],
                         [f"p{lbl}", f"pa{lbl}"])
                g_.chain(f"pa{lbl}", "alphaextract", f"a{lbl}")
                g_.chain([f"a{lbl}", f"mc{lbl}"],
                         "blend=all_mode=multiply:shortest=1", f"am{lbl}")
                g_.chain([f"p{lbl}", f"am{lbl}"], "alphamerge", f"x{lbl}")
            else:
                g_.chain(ph_in[sidx - ph_base],
                         ["format=rgba",
                          f"fade=t=in:st={t_in:.3f}:d={d_in:.3f}:alpha=1",
                          f"fade=t=out:st={t_out:.3f}:d={d_out:.3f}:alpha=1"],
                         f"x{lbl}")
            # the layer's glow effect, if enabled, takes its own branch off
            # this layer here and hands back the label to composite instead
            eff = fx_layer.get(name)
            if eff is not None:
                lbl = eff.per_phrase(g_, ctx, i, lbl)
            nxt = f"c{i + 1}{name[0]}"
            g_.chain([base, f"x{lbl}"],
                     f"overlay={ox}:{oy}:format=auto:shortest=1"
                     f":enable='{gates[i]}'", nxt)
            base = nxt

    # FX: applied to the captioned flat but cropped to the band and pasted
    # back, so nothing can bleed into the letterbox. Every branch off the
    # band is a tap, so the split's degree is whatever the enabled consumers
    # add up to.
    full, pre = g_.chain(base, "split=2", ["full", "pre"])
    g_.chain(pre, ["format=gbrp", "crop=%d:%d:0:%d" % (BAND_W, BAND_H, BAND_Y)],
             "band")
    band_lbl = g_.tap("band", "fxbase")
    for eff in chain:
        band_lbl = eff.apply(g_, band_lbl, ctx)
    fades = [f"fade=t=in:st=0:d={VIDEO_FADE_IN_S}",
             f"fade=t=out:st={dur - VIDEO_FADE_OUT_S:.3f}:"
             f"d={VIDEO_FADE_OUT_S}", "format=yuv420p"]
    if sig_in is None:
        # one chain, exactly as the legacy build emits it
        g_.chain([full, band_lbl],
                 [f"overlay=0:{BAND_Y}:shortest=1"] + fades, "vout")
    else:
        g_.chain([full, band_lbl], f"overlay=0:{BAND_Y}:shortest=1", "flat")
        g_.chain(sig_in, "format=rgba", "sg")
        g_.chain(["flat", "sg"], "overlay=0:0:format=auto:shortest=1", "flats")
        g_.chain("flats", fades, "vout")
    g_.chain(vin.a, [f for f in (ln, afade) if f], "aout")
    return g_.render()


# ---------------------------------------------------------------------------
# the style entry point
# ---------------------------------------------------------------------------

def render(plan):
    cfg, info = plan["cfg"], plan["info"]
    src, tmp = plan["src"], plan["tmp"]
    phrases = plan["arabic"]
    dur = info["duration"]
    if not info["width"]:
        raise SystemExit("bars style needs footage; it has no still-photo mode")

    crop = cfg.get("crop")
    if crop and not all(k in crop for k in ("x", "y", "w", "h")):
        raise SystemExit("crop must carry x, y, w, h")
    on = switches(cfg.get("fx"))

    # pill colour: config override, else derived from the graded band itself.
    # `target` is as-seen-in-the-finished-frame; the pill is DRAWN darker so
    # the glow screen passes land it on target.
    if cfg.get("bar_color"):
        target_rgb = hexrgb(cfg["bar_color"])
        print("      bar colour: %s (config)" % cfg["bar_color"])
    else:
        target_rgb = derive_bar_color(src, dur, crop, tmp, on["grade"])
    drawn_rgb = predraw_color(target_rgb)
    tint = [c / 255.0 for c in target_rgb]

    lay = layout(phrases, cfg["x_offset"], cfg["y_offset"])
    print("      Arabic pt=%d (nominal %d, widest raw %dpx, cap %dpx)"
          % (lay["pt"], TEXT["nominal_pt"], lay["widest"], lay["max_w"]))
    rep = draw_layers(lay, drawn_rgb, os.path.join(tmp, "overlays"))
    for r, p in zip(rep, lay["phrases"]):
        print("      bar%d+text%d: %d line(s), bars x %.0f..%.0f (w %.0f)"
              % (p["i"], p["i"], r["lines"], r["x0"], r["x1"],
                 r["x1"] - r["x0"]))

    snow_path = snow_layer(tmp, target_rgb) if on["snow"] else None
    heat_paths = heat_layers(tmp, dur) if on["heat"] else None
    scrim = scrim_plate(tmp) if on["scrim"] else None
    sig_path = (signature_card(cfg["signature"], tmp, cfg["signature_offset"])
                if cfg["signature"] else None)

    sched, cuts = schedule(phrases, dur)
    if cuts:
        print("      ! HARD CUT at changeover(s): %s"
              % ", ".join("P%d->P%d" % (c, c + 1) for c in cuts))

    print("      loudnorm pass 1...")
    ln = loudnorm_filter(measure_loudness(src, dur))
    afade = ("afade=t=in:st=0:d=%s,afade=t=out:st=%.3f:d=%s"
             % (AUDIO["fade_in_s"], dur - AUDIO["fade_out_s"],
                AUDIO["fade_out_s"]))

    fc, in_argv = build_graph(src, dur, crop, rep, sched, tint, on,
                              snow_path, scrim, ln, afade, sig_path,
                              heat_paths)

    out = plan["out"]
    cmd = [FFMPEG, "-y", "-hide_banner"] + in_argv
    cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "[aout]",
            "-t", "%.3f" % dur,
            "-c:v", "libx264", "-crf", str(ENCODE["crf"]),
            "-preset", ENCODE["preset"], "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-c:a", "aac", "-b:a", ENCODE["audio_bitrate"],
            "-movflags", "+faststart", out]
    print("      fx: " + " ".join(("+" if on[n] else "-") + n
                                  for n in SWITCHES))
    print("      " + " | ".join(
        "P%d %s in@%.2f+%.2f out@%.2f+%.2f" % (i + 1, k, ti, di, to, do)
        for i, (k, ti, di, to, do) in enumerate(sched)))
    if subprocess.run(cmd).returncode != 0:
        raise SystemExit("ffmpeg render failed")
    print("      %s | band %dx%d@y%d | bar #%02X%02X%02X (drawn #%02X%02X%02X)"
          % ((os.path.relpath(out, ROOT), BAND_W, BAND_H, BAND_Y)
             + target_rgb + drawn_rgb))


def signature_card(text, tmp, offset=0):
    """`offset` moves the line vertically only (+ lower / - higher); the
    signature is ALWAYS horizontally centered -- no x knob on purpose."""
    sig_size = max(12, int(CANVAS_H * SIGNATURE_SIZE_FRAC))
    font = ImageFont.truetype(SIGNATURE_FONT, sig_size)
    bbox = _PROBE.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    card = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    ImageDraw.Draw(card).text(
        (CANVAS_W // 2 - w // 2 - bbox[0],
         int(CANVAS_H * SIGNATURE_Y_FRAC) - h // 2 - bbox[1] + int(offset)),
        text, font=font,
        fill=(255, 255, 255, int(255 * SIGNATURE_ALPHA)))
    path = os.path.join(tmp, "signature.png")
    card.save(path)
    return path
