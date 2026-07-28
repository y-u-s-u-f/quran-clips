#!/usr/bin/env python3
"""
render_text.py  (Agent C / renderer)

Reads a clip.yaml + the shared style.yaml and produces, into
<clip>/work/overlays/, the transparent-PNG overlays that build_render.py
composites over the trimmed footage:

  phrase1.png / phrase2.png ... : per-phrase text overlays (full 1920x1080
      RGBA canvas) = Arabic (Uthmanic Hafs, RTL, shaped) + English small-caps
      centred below + ayah-end medallion (Arabic-Indic numerals) when a full
      ayah completes on that phrase. Soft drop shadow, no box/outline.
  reciter_overlay.png : the reciter portrait treated (feathered / colour-
      matched) to play the "reciter side" of the reference landscape balance.
  grade_overlay.png   : slight dim + soft vignette + subtle dark gradient
      behind the text region (all as one black-with-alpha darkening layer).

Native-landscape 1920x1080 output, per STYLE_SPEC.md authoritative
fractions_of_frame.  Requires Pillow with RAQM (HarfBuzz+FriBiDi).

Run with tools/render-venv/bin/python (a --system-site-packages venv over
/opt/homebrew/bin/python3, so its Pillow still has RAQM, plus PyYAML).
"""
import os, sys, math, re, itertools

from PIL import Image, ImageDraw, ImageFilter

try:
    import yaml
except ImportError:                                       # pragma: no cover
    sys.exit("FATAL: PyYAML not available. Use tools/render-venv/bin/python "
             "(create it with: /opt/homebrew/bin/python3 -m venv "
             "--system-site-packages tools/render-venv && "
             "tools/render-venv/bin/pip install -r requirements/render.txt). "
             "Do NOT pip-install into /opt/homebrew/bin/python3.")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# ----------------------------------------------------------------------------
# hard requirements
# ----------------------------------------------------------------------------
# importing qc.fonts is what enforces RAQM (it exits if Pillow lacks it)
sys.path.insert(0, ROOT)
from qc.arabic import norm_ar  # noqa: E402
from qc.fonts import RAQM, fit_pt, truetype  # noqa: E402,F401

# ----------------------------------------------------------------------------
# YAML
# ----------------------------------------------------------------------------
# Every config in this repo (clips/*/clip.yaml, clips/*/tags.yaml,
# templates/*.yaml) is read through here. This used to be a ~75-line
# indentation-based hand parser that failed silently on tabs, single quotes,
# block scalars and duplicate keys; it is now a thin yaml.safe_load wrapper.
#
# PyYAML uses YAML 1.1 resolution, which differs from the old parser in two
# ways that mattered to this repo's data (both fixed in the data, not here):
#   * bare off/on/yes/no resolve to booleans -- quote them to keep a string;
#   * bare `none` is NOT a null (only null/~/Null/NULL are), so any key that
#     wants None must be spelled `null`.
# Bare sexagesimals (12:30) also resolve to integers, so every time-like
# value (segment.start / segment.end) must stay quoted.
def load_yaml(path):
    """Parse a YAML file into plain Python data. Returns None for an empty
    file, matching the previous parser."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ----------------------------------------------------------------------------
# style selection
# ----------------------------------------------------------------------------
# clip.yaml `style:` picks which template drives the render. Absent => the
# original landscape style, so every pre-existing clip is untouched.
STYLE_TEMPLATES = {
    "default": "templates/style.yaml",   # 1920x1080 landscape (this module)
    "bars":    "templates/bars.yaml",    # 1080x1920 pill-bars (render_bars.py)
}

def style_of(clip):
    return str(clip.get("style") or "default").lower()

def template_path(clip):
    name = style_of(clip)
    if name not in STYLE_TEMPLATES:
        sys.exit(f"unknown style '{name}' in clip.yaml "
                 f"(known: {', '.join(sorted(STYLE_TEMPLATES))})")
    return os.path.join(ROOT, STYLE_TEMPLATES[name])


# ----------------------------------------------------------------------------
# geometry / style params (1920x1080 native landscape; frame fractions)
# ----------------------------------------------------------------------------
W, H = 1920, 1080

AR_FONT_PATH = os.path.join(ROOT, "assets/fonts/uthmanic_hafs_v22.ttf")
EN_FONT_PATH = os.path.join(ROOT, "assets/fonts/Albertus MT Lt Regular.ttf")

def hexrgb(s, default=(247, 245, 239)):
    if not s or not str(s).startswith("#"):
        return default
    s = s.lstrip("#")
    return tuple(int(s[i:i+2], 16) for i in (0, 2, 4))

def draw_rtl(draw, xy, text, font, fill, anchor="mm"):
    draw.text(xy, text, font=font, fill=fill, anchor=anchor,
              direction="rtl", language="ar")

def bbox_rtl(draw, xy, text, font, anchor="mm"):
    return draw.textbbox(xy, text, font=font, anchor=anchor,
                         direction="rtl", language="ar")


# ---- English small-caps engine -------------------------------------------
# Albertus lacks true OpenType small-caps: word-initial letter at full cap
# height, remaining letters as *petite* caps (smaller uppercase). Manual.
def smallcaps_tokens(text, cap_font, petite_font):
    """Return list of (char, font) preserving spaces/punct at full size."""
    toks = []
    prev_was_letter = False
    at_word_start = True
    i = 0
    # handle the ALLAH -> ALLĀH macron substitution word-wise
    words = re.split(r"(\s+)", text)
    for w in words:
        if w.isspace() or w == "":
            for ch in w:
                toks.append((ch, cap_font, False))
            at_word_start = True
            continue
        # strip leading punctuation/quotes to find the alpha core for ALLAH test
        core = re.sub(r"[^A-Za-zĀ-ſ]", "", w).lower()
        macron = core == "allah"
        first_letter_seen = False
        li = 0  # index among letters of this word
        for ch in w:
            if ch.isalpha():
                up = ch.upper()
                mac = False
                if macron and li == 3 and up == "A":
                    # A L L A H  -> A L L Ā H : the 2nd 'a' takes a macron.
                    # Albertus has no U+0100 glyph (renders tofu), so we draw
                    # "A" and stroke the macron bar ourselves (mac flag).
                    mac = True
                font = cap_font if not first_letter_seen else petite_font
                toks.append((up, font, mac))
                first_letter_seen = True
                li += 1
            else:
                toks.append((ch, cap_font, False))
    return toks

def measure_smallcaps(toks, tracking_px):
    total = 0
    for ch, font, mac in toks:
        total += font.getlength(ch) + tracking_px
    return total

def draw_smallcaps_line(layer, x0, baseline_y, toks, fill, tracking_px):
    d = ImageDraw.Draw(layer)
    x = x0
    for ch, font, mac in toks:
        d.text((x, baseline_y), ch, font=font, fill=fill, anchor="ls")
        adv = font.getlength(ch)
        if mac:
            # macron bar over the cap: centred, ~55% of glyph width, above cap
            cb = font.getbbox("A")
            cap_h = cb[3] - cb[1]
            bar_w = adv * 0.55
            bx = x + (adv - bar_w) / 2.0
            by = baseline_y - cap_h - max(2, cap_h * 0.14)
            th = max(1, int(round(cap_h * 0.08)))
            d.rectangle([bx, by, bx + bar_w, by + th], fill=fill)
        x += adv + tracking_px

def wrap_smallcaps(text, cap_font, petite_font, tracking_px, max_w):
    """Wrap into the FEWEST lines that fit max_w, then BALANCE the split
    (min-raggedness): among all splits with that line count whose every line
    fits, pick the one minimising the widest line (tie-break: min spread).
    Prevents orphan last lines (e.g. a lone 'ME.') that greedy first-fit
    produces."""
    words = text.split(" ")

    def width_of(ws):
        toks = smallcaps_tokens(" ".join(ws), cap_font, petite_font)
        return measure_smallcaps(toks, tracking_px)

    # greedy first-fit gives the minimal feasible line count
    greedy, cur = [], []
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
        return [" ".join(l) for l in greedy]

    best, best_key = None, None
    for cuts in itertools.combinations(range(1, len(words)), n - 1):
        bounds = (0,) + cuts + (len(words),)
        cand = [words[bounds[i]:bounds[i + 1]] for i in range(n)]
        ws = [width_of(l) for l in cand]
        if max(ws) > max_w:
            continue
        key = (max(ws), max(ws) - min(ws))
        if best_key is None or key < best_key:
            best_key, best = key, cand
    if best is None:          # no balanced split fits; keep greedy
        best = greedy
    return [" ".join(l) for l in best]


# ---- low-res gradient helpers (no numpy) ----------------------------------
def radial_alpha(w, h, cx, cy, rx, ry, a_center, a_edge, ease=1.6, lo=48, hi=27):
    """Build an 'L' alpha image (a_center..a_edge over an ellipse) at low res
    then upscale. a in 0..255."""
    small = Image.new("L", (lo, hi), 0)
    px = small.load()
    for j in range(hi):
        for i in range(lo):
            fx = (i + 0.5) / lo * w
            fy = (j + 0.5) / hi * h
            d = math.sqrt(((fx - cx) / rx) ** 2 + ((fy - cy) / ry) ** 2)
            d = min(1.0, d)
            t = d ** ease
            val = a_center + (a_edge - a_center) * t
            px[i, j] = max(0, min(255, int(round(val))))
    return small.resize((w, h), Image.BILINEAR)


# ----------------------------------------------------------------------------
# main build
# ----------------------------------------------------------------------------
def build(clip_dir):
    clip = load_yaml(os.path.join(clip_dir, "clip.yaml"))
    if style_of(clip) != "default":       # non-default styles have their own
        import render_bars                # overlay generator
        return render_bars.build(clip_dir)
    style = load_yaml(template_path(clip))

    # Drop the small tajweed annotation marks (see qc.arabic for which and why;
    # learned on al-ahzab-70-71).
    for _ph in clip.get("phrases", []):
        if isinstance(_ph, dict) and _ph.get("ar"):
            _ph["ar"] = norm_ar(_ph["ar"])

    ar_color = hexrgb(style["arabic"]["color"])
    en_color = hexrgb(style["english"]["color"])
    frac = style["arabic"]["fractions_of_frame"]
    enfrac = style["english"]["fractions_of_frame"]

    max_line_w = float(frac["max_line_width"]) * W          # 0.55 W
    gap_below  = float(enfrac["gap_below_arabic"]) * H       # 0.02 H
    tracking_pct = float(style["english"].get("tracking_pct", 3)) / 100.0

    sh = style["shadow"]
    sh_off = int(sh["offset_px"]); sh_blur = float(sh["blur_px"])
    sh_col = hexrgb(sh["color"], (0, 0, 0))
    sh_alpha = int(round(float(sh["opacity"]) * 255))

    # --- text side: LEFT (reciter is on the RIGHT of the scene image). The
    #     scene's clear area is the UPPER-left wall, so the block sits higher
    #     than mid-frame (config-driven per clip via clip.text.*). ---
    txt_cfg = clip.get("text", {}) or {}
    cx_frac = float(txt_cfg.get("center_x_frac", 0.30))
    cy_frac = float(txt_cfg.get("center_y_frac", 0.34))
    TEXT_CX = int(cx_frac * W)
    AR_CY   = int(cy_frac * H)  # arabic vertical centre

    # --- Arabic font size: nominal design target (config-driven), auto-shrunk
    #     so the WIDEST phrase stays within max_line_width (single line, no
    #     wrap); both phrases share one size for consistent swaps. ---
    ar_render = style["arabic"].get("render", {}) or {}
    NOMINAL_AR_PT = int(ar_render.get("nominal_pt", 84))

    # --- ayah medallion: a phrase shows the end-of-verse rosette (Arabic-Indic
    #     numerals; Uthmanic Hafs auto-encloses them in the decorative circle)
    #     ONLY when it completes a full ayah. A phrase completes its ayah when
    #     the NEXT phrase moves to a different ayah, or (for the final phrase)
    #     when it ends the clip's last ayah. Mid-ayah phrases show no number. A
    #     leading U+06DD is NOT used (it would add a spurious 2nd rosette). ---
    _phrases = clip["phrases"]
    ayah_range = clip.get("ayah_range", [None, None])
    last_ayah = ayah_range[-1] if isinstance(ayah_range, list) else None

    def arabic_indic(n):
        table = {"0": "٠", "1": "١", "2": "٢", "3": "٣", "4": "٤",
                 "5": "٥", "6": "٦", "7": "٧", "8": "٨", "9": "٩"}
        return "".join(table[c] for c in str(n))

    def med_suffix(i):  # i is 0-based; "" if this phrase shows no medallion
        ph = _phrases[i]
        ay = ph.get("ayah")
        if ay is None:
            return ""
        if i == len(_phrases) - 1:
            completes = (ay == last_ayah)
        else:
            completes = (_phrases[i + 1].get("ayah") != ay)
        return (" " + arabic_indic(ay)) if completes else ""

    probe = Image.new("RGBA", (10, 10))
    pd = ImageDraw.Draw(probe)
    widest = 1.0
    for i, ph in enumerate(_phrases):
        f = truetype(AR_FONT_PATH, NOMINAL_AR_PT)
        bb = bbox_rtl(pd, (0, 0), ph["ar"] + med_suffix(i), f)
        widest = max(widest, bb[2] - bb[0])
    AR_PT = fit_pt(NOMINAL_AR_PT, 40, max_line_w, widest)
    ar_font = truetype(AR_FONT_PATH, AR_PT)

    # English fonts. Uniform ALL-CAPS at one size when smallcaps is off
    # (petite size == cap size); otherwise word-initial cap + petite caps.
    smallcaps = bool(style["english"].get("smallcaps", True))
    en_render = style["english"].get("render", {}) or {}
    EN_CAP_PT = int(en_render.get("cap_pt", 33))
    EN_PET_PT = int(en_render.get("petite_pt", 26)) if smallcaps else EN_CAP_PT
    cap_font = truetype(EN_FONT_PATH, EN_CAP_PT)
    pet_font = truetype(EN_FONT_PATH, EN_PET_PT)
    tracking_px = tracking_pct * EN_CAP_PT
    # English line-width cap (QA: <=0.50 W so line ends clear the mic/reciter)
    max_en_w = float(enfrac.get("max_line_width", 0.50)) * W

    out_dir = os.path.join(clip_dir, "work", "overlays")
    os.makedirs(out_dir, exist_ok=True)

    report = {"AR_PT": AR_PT, "TEXT_CX": TEXT_CX, "AR_CY": AR_CY,
              "phrases": []}

    # ----- per-phrase text overlays -----
    for idx, ph in enumerate(clip["phrases"], start=1):
        ink = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(ink)

        # ayah medallion whenever this phrase completes a full ayah (see
        # med_suffix above). Medallion = the Arabic-Indic numerals alone
        # (Uthmanic Hafs auto-encloses them in the decorative rosette).
        suffix = med_suffix(idx - 1)
        show_med = bool(suffix)
        ar_text = ph["ar"] + suffix

        draw_rtl(d, (TEXT_CX, AR_CY), ar_text, ar_font, ar_color + (255,))
        arb = bbox_rtl(d, (TEXT_CX, AR_CY), ar_text, ar_font)
        ar_bottom = arb[3]

        # English small-caps, centred on TEXT_CX, below Arabic
        en_lines = wrap_smallcaps(ph["en"], cap_font, pet_font, tracking_px, max_en_w)
        line_h = int(EN_CAP_PT * 1.62)
        cap_h = cap_font.getbbox("H")[3] - cap_font.getbbox("H")[1]
        y = ar_bottom + int(gap_below) + cap_h
        for ln in en_lines:
            toks = smallcaps_tokens(ln, cap_font, pet_font)
            wln = measure_smallcaps(toks, tracking_px)
            x0 = TEXT_CX - wln / 2.0
            draw_smallcaps_line(ink, x0, y, toks, en_color + (240,), tracking_px)
            y += line_h

        # soft drop shadow from the ink alpha
        alpha = ink.split()[3]
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        solid = Image.new("RGBA", (W, H), sh_col + (sh_alpha,))
        shadow.paste(solid, (0, 0), alpha)
        shadow = shadow.transform(
            (W, H), Image.AFFINE, (1, 0, -sh_off, 0, 1, -sh_off))
        shadow = shadow.filter(ImageFilter.GaussianBlur(sh_blur * 0.6))

        final = Image.alpha_composite(shadow, ink)
        p = os.path.join(out_dir, f"phrase{idx}.png")
        final.save(p)
        report["phrases"].append(
            {"file": p, "ar_w": arb[2]-arb[0], "en_lines": en_lines,
             "medallion": show_med})

    # ----- (reciter is baked into the scene image; no portrait overlay) -----
    from PIL import ImageChops

    # ----- grade overlay: slight dim + vignette + text-region dark gradient --
    # The new scene image is a bright, mostly-white wall on the text side, so
    # the warm-white text needs a stronger dark backing than the old footage.
    grd_cfg = style.get("color_treatment", {}) or {}
    grade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    # uniform slight dim -> seat white text; live footage runs brighter than
    # the account's moody grade, so a touch more than the still pipeline used
    # (QA al-ahzab-70-71: render luma 107 vs refs ~90-95).
    base_dim = 30  # ~12%
    # vignette: 0 in centre -> darker at corners (also aids the upper-left text)
    vig = radial_alpha(W, H, W/2, H/2, rx=W*0.62, ry=H*0.62,
                       a_center=0, a_edge=126, ease=2.2)
    # text backing gradient (behind the UPPER-left text block) — centred on the
    # text block, stronger centre so white text reads over the white wall.
    tb = radial_alpha(W, H, TEXT_CX, int((cy_frac + 0.04) * H),
                      rx=W*0.32, ry=H*0.26,
                      a_center=130, a_edge=0, ease=1.45)
    combo = Image.new("L", (W, H), base_dim)
    combo = ImageChops.add(combo, vig)
    combo = ImageChops.add(combo, tb)
    combo = combo.point(lambda v: min(v, 190))  # cap darkening
    grade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    grade.putalpha(combo)  # black rgba with alpha=combo
    # ensure rgb is black
    grade = Image.merge("RGBA", (Image.new("L", (W, H), 0),
                                 Image.new("L", (W, H), 0),
                                 Image.new("L", (W, H), 0), combo))
    grade.save(os.path.join(out_dir, "grade_overlay.png"))

    print("render_text.py OK")
    print(f"  Arabic pt={AR_PT} (nominal {NOMINAL_AR_PT}, widest raw "
          f"{int(widest)}px, cap {int(max_line_w)}px)")
    for r in report["phrases"]:
        print(f"  {os.path.basename(r['file'])}: ar_w={int(r['ar_w'])}px "
              f"en_lines={len(r['en_lines'])} medallion={r['medallion']}")
    print(f"  overlays -> {out_dir}")
    return out_dir


if __name__ == "__main__":
    clip_dir = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.join(ROOT, "clips", "at-tawbah-51-51")
    build(clip_dir)
