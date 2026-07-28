#!/usr/bin/env python3
"""
render_bars.py  (Agent C / renderer -- "bars" style)

Sibling of render_text.py for the vertical 1080x1920 "bars" style
(templates/bars.yaml). Selected per-clip with `style: bars` in clip.yaml;
without that key nothing here is reached and the original landscape style is
untouched.

Produces, into <clip>/work/overlays/:

  bar1.png / text1.png / bar2.png ... : per-phrase caption overlays, TWO layers
      per phrase on the full 1080x1920 RGBA canvas so build_bars.py can animate
      them independently (the pill wipes, the text always cross-fades):
        barN  = the hard-edged opaque rounded pill(s) alone.
        textN = one or two CENTRE-anchored lines of Arabic (AMoshref-Thulth,
                RTL, shaped) plus their single hard drop shadow.
      textN is composited OVER barN. No english and no glow baked in -- the bar
      glow and the tight white-text halo are produced by the Glow / Glow Scan
      passes in build_bars.py, exactly as the reference reels were made.
  snow_*.mp4 : the seamlessly-looping "Snow" layer for those FX -- a Pillow
      evaluation of Node Video's own procedural noise shader, not a particle
      system. See style/refs2/SNOW_SPEC.md.

Also derives the bar colour from the clip's own GRADED footage
(H = the band's hue, L = 1.78 x mean L, S = 2.2 x mean S, clamped) -- see
style/refs2/STYLE2_SPEC.md C2 and derive_bar_color below. Overridable per-clip
with `bar_color: "#RRGGBB"` in clip.yaml.

Run with /opt/homebrew/bin/python3 (its Pillow has RAQM).
"""
import colorsys
import math
import os
import subprocess
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, features

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_text import ROOT, load_yaml, hexrgb, truetype  # noqa: E402

if not features.check("raqm"):
    sys.exit("FATAL: Pillow RAQM (HarfBuzz+FriBiDi) not available. "
             "Use /opt/homebrew/bin/python3.")

FFMPEG = "/opt/homebrew/bin/ffmpeg"

# Same house-style normalisation as render_text.build: drop the tajweed
# ANNOTATION marks. U+06DF is additionally absent from AM_Thulth's cmap and
# would silently vanish (this font renders missing codepoints as ZERO WIDTH,
# not as tofu), so stripping it keeps widths honest.
_STRIP_MARKS = {"۟", "ۭ"}

# Combining marks, for the tashkeel-stripped "letter body" measurement.
_TASHKEEL = set(
    "ًٌٍَُِّْٕٓٔ"
    "ٰۖۗۘۙۚۛۜ۟۠ۡ"
    "ۣ۪ۭۢۤۧۨ۫۬"
)


def norm_ar(s):
    return "".join(c for c in s if c not in _STRIP_MARKS)


def strip_tashkeel(s):
    return "".join(c for c in s if c not in _TASHKEEL)


# ----------------------------------------------------------------------------
# measurement
# ----------------------------------------------------------------------------
_PROBE = ImageDraw.Draw(Image.new("L", (1, 1)))


def bbox_ls(text, font):
    """Ink bbox with the pen at (0,0) ON THE BASELINE -> (l, top, r, bot),
    top negative (above baseline), bot positive (below)."""
    return _PROBE.textbbox((0, 0), text, font=font, anchor="ls",
                           direction="rtl", language="ar")


# ----------------------------------------------------------------------------
# bar colour
# ----------------------------------------------------------------------------
def grade_chain(style):
    g = style["grade"]
    return f"eq={g['eq']},colorbalance={g['colorbalance']},vignette={g['vignette']}"


def band_source_chain(clip, style):
    """[0:v] -> the graded 1080x608 footage band (no pad, no captions)."""
    band = style["band"]
    vb = clip.get("video_bg") or {}
    vc = vb.get("crop") or {}
    if vc:
        pre = f"crop={vc['w']}:{vc['h']}:{vc['x']}:{vc['y']}," \
              f"scale={band['width']}:{band['height']}:flags=lanczos"
    else:
        pre = (f"scale={band['width']}:{band['height']}:flags=lanczos:"
               "force_original_aspect_ratio=increase,"
               f"crop={band['width']}:{band['height']}")
    return pre + "," + grade_chain(style)


def hms(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def derive_bar_color(clip_dir, clip, style):
    """Sample the clip's own GRADED band and apply the spec's auto rule:
        H_bar = hue of the luminance^2-weighted dominant colour (unchanged)
        L_bar = clamp(1.78 * L_mean, 0.34, 0.48)
        S_bar = clamp(2.2  * S_mean, 0.22, 0.60)
    Returns (hex, stats-dict)."""
    a = style["bar"]["auto"]
    tile = int(a["sample_tile"])
    seg = clip["segment"]
    ss = hms(seg["start"])
    dur = round(hms(seg["end"]) - ss, 3)
    src = os.path.join(clip_dir, "work", "source.mp4")
    tile_png = os.path.join(clip_dir, "work", "bar_color_sample.png")
    vf = (band_source_chain(clip, style) +
          f",fps={a['sample_fps']},scale=48:27,tile={tile}x{tile}")
    cmd = [FFMPEG, "-v", "error", "-y", "-ss", f"{ss:.3f}", "-t", f"{dur:.3f}",
           "-i", src, "-vf", vf, "-frames:v", "1", tile_png]
    if subprocess.run(cmd).returncode != 0:
        sys.exit("ffmpeg bar-colour sampling failed")

    px = [q for q in Image.open(tile_png).convert("RGB").get_flattened_data()
          if sum(q)]                # skip unfilled tile cells
    if not px:
        sys.exit("bar-colour sampling produced an empty tile")
    # H / S / L of the band's MEAN RGB -- i.e. exactly the "mean RGB" row of
    # STYLE2_SPEC C2, not a per-pixel average of H/S/L (they differ a lot in S)
    # and not the luminance^2-weighted "dominant colour" (which is what the
    # spec's hue rule nominally reads, but on a near-neutral scene its hue is
    # pure noise -- on this clip it lands on lime green). Checked against both
    # refs: predicted vs measured bar is H 43.2/42.9, L 0.464/0.465,
    # S 0.552/0.561 (gold) and H 348.7/348.3, L 0.344/0.380, S 0.287/0.237
    # (rose).
    n = len(px)
    mr = sum(q[0] for q in px) / n
    mg = sum(q[1] for q in px) / n
    mb = sum(q[2] for q in px) / n
    dh, l_mean, s_mean = colorsys.rgb_to_hls(mr / 255.0, mg / 255.0, mb / 255.0)
    # Low-chroma fallback for the SATURATION term only. Both refs are strongly
    # tinted scenes; this clip is white marble + white thobes, so its mean RGB
    # is nearly neutral (63,60,56 -> S 0.057) and the rule would clamp the bar
    # to S 0.22, which renders as a grey-tan box rather than a colour. Below
    # the threshold we fall back to the mean of the per-pixel saturations,
    # which still describes "how colourful the clip is" when the colours cancel
    # in the mean. Both refs stay above the threshold and are unaffected
    # (gold S 0.251, rose S 0.130).
    if s_mean < float(a["saturation_lowchroma_below"]):
        s_mean = sum(colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)[2]
                     for r, g, b in px) / n
    l_bar = min(float(a["lightness_max"]),
                max(float(a["lightness_min"]), float(a["lightness_gain"]) * l_mean))
    s_bar = min(float(a["saturation_max"]),
                max(float(a["saturation_min"]), float(a["saturation_gain"]) * s_mean))
    r, g, b = colorsys.hls_to_rgb(dh, l_bar, s_bar)
    hexc = "#%02X%02X%02X" % tuple(int(round(v * 255)) for v in (r, g, b))
    return hexc, {"L_mean": round(l_mean, 4), "S_mean": round(s_mean, 4),
                  "H_deg": round(dh * 360, 1), "L_bar": round(l_bar, 3),
                  "S_bar": round(s_bar, 3), "n_px": n}


def predraw_color(hexc, style):
    """The spec's bar colour was measured on FINISHED reels, i.e. AFTER the
    Glow / Glow Scan passes screen over the pill. Screening lifts the pill by a
    near-constant factor, so the pill has to be DRAWN darker (and a touch more
    saturated) to land on the target. Measured on this band by sweeping the
    drawn lightness and reading the composited interior back:
        drawn 0.22/0.28/0.35 -> composited 0.354/0.409/0.472
    which is screen(drawn, 0.179) to within 0.006 across the sweep."""
    b = style["bar"]
    lift = float(b["fx_screen_lift"])
    keep = float(b["fx_sat_retain"])
    r, g, bl = [c / 255.0 for c in hexrgb(hexc, (185, 147, 52))]
    h_, l_, s_ = colorsys.rgb_to_hls(r, g, bl)
    l_ = max(0.0, 1.0 - (1.0 - l_) / (1.0 - lift))
    s_ = min(1.0, s_ / keep)
    r, g, bl = colorsys.hls_to_rgb(h_, l_, s_)
    return "#%02X%02X%02X" % tuple(int(round(v * 255)) for v in (r, g, bl))


def bar_color_of(clip_dir, clip, style):
    """clip.yaml `bar_color` > bars.yaml `bar.color` > auto-derivation.
    Returns the TARGET (as-seen-in-the-finished-frame) colour."""
    for src, val in (("clip.yaml", clip.get("bar_color")),
                     ("bars.yaml", style["bar"].get("color"))):
        if val and str(val).startswith("#"):
            return str(val).upper(), {"source": src}
    hexc, stats = derive_bar_color(clip_dir, clip, style)
    stats["source"] = "auto"
    return hexc, stats


# ----------------------------------------------------------------------------
# drawing
# ----------------------------------------------------------------------------
_SS = 4     # pill supersampling factor (Pillow's rounded_rectangle has no AA)


def draw_pill(canvas, x0, y0, x1, y1, rgb):
    """Hard-edged full pill (radius = height/2), antialiased in 2-3px like the
    references -- drawn supersampled into a tile and pasted as a mask."""
    w, h = int(round(x1 - x0)), int(round(y1 - y0))
    tile = Image.new("L", (w * _SS, h * _SS), 0)
    ImageDraw.Draw(tile).rounded_rectangle(
        [0, 0, w * _SS - 1, h * _SS - 1], radius=h * _SS / 2.0, fill=255)
    mask = tile.resize((w, h), Image.LANCZOS)
    canvas.paste(Image.new("RGBA", (w, h), rgb + (255,)),
                 (int(round(x0)), int(round(y0))), mask)


def with_shadow(ink, cfg, W, H):
    """The glyph layer plus its ONE drop shadow, composited (shadow beneath the
    ink). Deliberately NOT the default style's soft shadow: the copy is solid,
    fully opaque, un-blurred -- so its boundary is exactly as sharp as the
    glyph's own antialiased edge -- and it is displaced straight DOWN.
    z-order: this whole layer rides OVER the pill, so the shadow darkens the bar
    where a glyph overhangs it, as measured in the gold reference."""
    dx = int(round(float(cfg["dx_frac"]) * W))
    dy = int(round(float(cfg["dy_frac"]) * H))
    op = float(cfg["opacity"])
    blur = float(cfg["blur_px"])
    alpha = ink.getchannel("A")
    if op < 1.0:
        alpha = alpha.point(lambda v: int(round(v * op)))
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sh.paste(Image.new("RGBA", (W, H), hexrgb(cfg["color"], (0, 0, 0)) + (255,)),
             (0, 0), alpha)
    # NEAREST: an integer translate must not resample the silhouette soft.
    sh = sh.transform((W, H), Image.AFFINE, (1, 0, -dx, 0, 1, -dy),
                      resample=Image.NEAREST)
    if blur > 0:
        sh = sh.filter(ImageFilter.GaussianBlur(blur))
    return Image.alpha_composite(sh, ink)


def phrase_lines(ph):
    """clip.yaml carries `ar` (single line) or `ar1`/`ar2` (two lines)."""
    if ph.get("ar1"):
        return [norm_ar(ph["ar1"])] + ([norm_ar(ph["ar2"])] if ph.get("ar2") else [])
    return [norm_ar(ph["ar"])]


# ----------------------------------------------------------------------------
# left scrim
# ----------------------------------------------------------------------------
def scrim_layer(clip_dir, style):
    """The refs' edge falloff is ASYMMETRIC (spec E.3): normalised luminance is
    0.16 at the left edge recovering by x=120@720, 0.23-0.33 just inside the
    band's top/bottom edges, and 0.81-0.94 -- i.e. essentially none -- on the
    right. A radial `vignette=a=...` cannot make that shape (measured on a
    white probe: at the strength needed for the left edge it takes the right
    edge down with it), so the directional part is an explicit plate:
    left ramp x factor, vertical feather y factor, multiplied into the GRADED
    band by build_bars.py."""
    band, cfg = style["band"], style["grade"]["scrim"]
    lo, to = float(cfg["left_min"]), float(cfg["left_to_px"])
    eo, et = float(cfg["edge_min"]), float(cfg["edge_to_px"])
    w, h = int(band["width"]), int(band["height"])
    path = os.path.join(clip_dir, "work",
                        f"scrim_{w}x{h}_{lo}_{int(to)}_{eo}_{int(et)}.png")
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


# ----------------------------------------------------------------------------
# snow layer
#
# This is NOT a particle system. Node Video's "Snow" (Asset Store > Nature >
# Snow, shader `NodeVideo/AssetStore/snow`) is a single full-screen procedural
# pass. The app is a Unity build; the compiled GLSL was recovered from
# com.shallwaystudio.nodevideo 8.8.0 and reads, in full:
#
#     p     = rotate(uv*viewport_ratio - 0.5, -direction*PI/180)
#     scale = size*9.5 + 0.5
#     expo  = 50 - 49*amount
#     q     = p/scale + 0.5
#     sway  = sin(0.1*t*speed)
#     uv1   = (q.x - 0.5000*sway, q.y + 0.1000*t*speed)*0.6 - 0.003*t*flicker
#     uv2   = (q.x + 0.1111*sway, q.y + 0.0714*t*speed)*0.9 + 0.002*t*flicker
#     v     = pow(texture(noise, uv1).r * texture(noise, uv2).b, expo)
#     out   = src + color.rgb * v * intensity * 18
#
# `noise` is a bundled 256x256 RGBA8 white-noise texture, bilinear + repeat
# (verified: uniform histogram, zero neighbour and zero inter-channel
# correlation -- so a seeded RNG tile is statistically identical and we
# generate ours rather than shipping an extracted app asset). Two heavily
# magnified, bilinearly interpolated copies of it scroll at slightly different
# scales and rates; their product raised to a high power leaves only the rare
# coincidences of two near-white samples -- which is what the "snowflakes" are.
#
# Everything below is C-speed Pillow: two Image.transform() bilinear samples,
# one ImageChops.multiply, one point() LUT for the pow+gain+tint. No numpy.
#
# Calibration against the two reference reels (see style/refs2/SNOW_SPEC.md):
# the specks are neutral WHITE (residual RGB 96.1/96.4/93.4 gold, 92.7/92.5/92.5
# rose), normalised radial profile 1.00 / 0.40 / 0.09 / 0.04 at r = 0..3 px on
# the 720x405 band, ~24 blobs/band above 40 DN, and the whole field RISES at a
# coherent 99 px/s (phase correlation of the isolated layer). Fitted:
# texel_px = 7.2 px on the 405-band (texel_scale 72 x Size 0.10) and
# expo = 15.7 (= Amount 0.70, unchanged). Only the output gain is free -- Node's
# literal x18 is an additive-HDR figure and we screen-composite instead.
# Verified on the finished layer: apparent rise 3.0 px/frame (refs 2.9),
# 22.5 blobs/band over 40 DN (refs 24), 12.1 over 60 DN (refs 12),
# radial profile 1.00/0.45/0.13/0.04 (refs 1.00/0.40/0.09/0.04).
#
# Seamless loop: each layer's vertical travel over one loop is exactly its own
# noise-tile height in texels, the sway is a whole sinusoid, and the horizontal
# offsets are constant -- so frame N is bit-identical to frame 0.
# ----------------------------------------------------------------------------
SNOW_REF_H = 405.0          # band height the forensics were measured at
SNOW_NX = 256               # noise tile width, texels (as shipped by the app)


def _snow_cfg(cfg):
    """Template values + defaults, so an older bars.yaml still parses."""
    def g(k, d):
        return float(cfg[k]) if k in cfg else d
    return dict(
        amount=g("amount", 0.70),       # Node Amount  -> pow exponent
        size=g("size", 0.10),           # Node Size    -> noise magnification
        speed=g("speed", -1.0),         # Node Speed   -> scroll (<0 = rising)
        flicker=g("flicker", 1.0),      # Node Flicker -> layer cross-drift
        intensity=g("intensity", 1.0),  # Node Intensity
        gain=g("gain", 6.0),            # our screen-composite equivalent of x18
        tint_mix=g("tint_mix", 0.10),   # 0 = pure white, as measured
        sway_texels=g("sway_texels", 3.5),
        texel_scale=g("texel_scale", 72.0),   # texel_px = size * this @405-band
        loop_s=g("loop_s", 8.0),
        seed=int(g("seed", 7)),
    )


def _snow_tile(nx, ny, seed):
    rnd = __import__("random").Random(seed)
    return Image.frombytes("L", (nx, ny),
                           bytes(rnd.randrange(256) for _ in range(nx * ny)))


def _snow_wrapped(base, nx, ny, need_w, need_h):
    """Tile `base` so an Image.transform sample window never runs off it."""
    im = Image.new("L", (nx + need_w, ny + need_h))
    for oy in range(0, im.height, ny):
        for ox in range(0, im.width, nx):
            im.paste(base, (ox, oy))
    return im


class _SnowField(object):
    """One loop of the decompiled shader, evaluated with Pillow."""

    def __init__(self, w, h, fps, c):
        self.w, self.h = w, h
        self.nframes = max(1, int(round(c["loop_s"] * fps)))
        loop = c["loop_s"]
        texel = max(1.0, c["size"] * c["texel_scale"] * (h / SNOW_REF_H))
        self.ppt = (texel, texel * 2.0 / 3.0)          # 0.6 vs 0.9 uv scaling
        # shader scroll rates, texels/s, plus the flicker cross-drift
        smag = abs(c["speed"])
        rate = (0.6 * 0.1000 * SNOW_NX * smag, 0.9 * 0.0714 * SNOW_NX * smag)
        cross = (-0.003 * SNOW_NX * c["flicker"], 0.002 * SNOW_NX * c["flicker"])
        # Node's Speed is negative by default and the field rises; the shader's
        # v axis is GL-oriented, so a negative Speed scrolls the pattern UP.
        up = 1.0 if c["speed"] < 0 else -1.0
        self.ny, self.trav, self.src = [], [], []
        for k in (0, 1):
            need = int(math.ceil(h / self.ppt[k])) + 3
            n = max(need, int(round((rate[k] + cross[k]) * loop)))
            self.ny.append(n)
            self.trav.append(up * n)                   # exactly one tile / loop
            self.src.append(_snow_wrapped(
                _snow_tile(SNOW_NX, n, c["seed"] + k), SNOW_NX, n,
                int(math.ceil(w / self.ppt[k])) + 3, need))
        self.sway = c["sway_texels"]
        expo = max(1.0, 50.0 - 49.0 * c["amount"])
        gain = c["gain"] * c["intensity"]
        self.lut = [min(255, int(255.0 * gain * ((i / 255.0) ** expo) + 0.5))
                    for i in range(256)]
        self.expo, self.gain, self.texel = expo, gain, texel

    def frame(self, f):
        t = (f % self.nframes) / float(self.nframes)
        sw = self.sway * math.sin(2 * math.pi * t)
        lay = []
        for k in (0, 1):
            p = self.ppt[k]
            oy = (self.trav[k] * t) % self.ny[k]
            ox = ((sw if k == 0 else -sw / 3.0)) % SNOW_NX
            lay.append(self.src[k].transform(
                (self.w, self.h), Image.AFFINE,
                (1.0 / p, 0.0, ox, 0.0, 1.0 / p, oy), resample=Image.BILINEAR))
        return ImageChops.multiply(lay[0], lay[1]).point(self.lut)


def render_snow(out_path, w, h, fps, cfg, tint):
    c = _snow_cfg(cfg)
    fld = _SnowField(w, h, fps, c)

    # colour: the references measure NEUTRAL WHITE specks in both the gold and
    # the rose reel, so tint_mix is a whisper by default.
    ch = [v / 255.0 for v in hexrgb(tint, (255, 255, 255))]
    m = max(1e-6, max(ch))
    k = c["tint_mix"]
    mul = [1.0 - k + k * (v / m) for v in ch]
    chan_lut = [[min(255, int(i * mv + 0.5)) for i in range(256)] for mv in mul]

    pw, ph_ = w - w % 2, h - h % 2
    ff = subprocess.Popen(
        [FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
         "-vf", f"crop={pw}:{ph_}:0:0", "-an", "-c:v", "libx264", "-crf", "8",
         "-preset", "medium", "-pix_fmt", "yuv420p", out_path],
        stdin=subprocess.PIPE)
    for f in range(fld.nframes):
        v = fld.frame(f)
        rgb = Image.merge("RGB", tuple(v.point(l) for l in chan_lut))
        ff.stdin.write(rgb.tobytes())
    ff.stdin.close()
    if ff.wait():
        sys.exit("ffmpeg snow encode failed")
    return fld, fld.nframes


def snow_layer(clip_dir, style, tint):
    band, cfg = style["band"], style["fx"]["snow"]
    c = _snow_cfg(cfg)
    tag = "{}x{}_{}_{}".format(
        band["width"], band["height"], tint.lstrip("#"),
        "_".join(str(c[k]) for k in sorted(c)))
    path = os.path.join(clip_dir, "work", f"snow_{tag}.mp4")
    if not os.path.exists(path):
        fld, nf = render_snow(path, band["width"], band["height"],
                              int(style["meta"]["fps"]), cfg, tint)
        print(f"  snow layer: shader pass, texel {fld.texel:.1f}px "
              f"expo {fld.expo:.1f} gain {fld.gain:.1f}, {nf} frames "
              f"-> {os.path.basename(path)}")
    return path


# ----------------------------------------------------------------------------
# main build
# ----------------------------------------------------------------------------
def build(clip_dir):
    clip = load_yaml(os.path.join(clip_dir, "clip.yaml"))
    style = load_yaml(os.path.join(ROOT, "templates/bars.yaml"))

    W = int(style["canvas"]["width"])
    H = int(style["canvas"]["height"])
    tcfg, bcfg = style["text"], style["bar"]
    scfg = tcfg["shadow"]

    font_path = os.path.join(ROOT, tcfg["font"])
    ink_color = hexrgb(tcfg["color"], (255, 255, 255))
    cx = int(round(float(tcfg["center_x_frac"]) * W))
    max_w = float(tcfg["max_line_width_frac"]) * W
    bar_h = int(bcfg["height_px"])
    pad_x = float(bcfg["pad_x_px"])
    base_off = float(bcfg["baseline_below_center_px"])

    bar_hex, cstats = bar_color_of(clip_dir, clip, style)   # as-seen target
    draw_hex = predraw_color(bar_hex, style)                # what we paint
    bar_rgb = hexrgb(draw_hex, (185, 147, 52))

    phrases = clip["phrases"]
    all_lines = [ln for ph in phrases for ln in phrase_lines(ph)]

    # One shared point size for every phrase (consistent swaps), shrunk only so
    # the WIDEST line stays inside max_line_width.
    nominal = int(tcfg["nominal_pt"])
    probe_font = truetype(font_path, nominal)
    widest = max(1.0, max(bbox_ls(ln, probe_font)[2] - bbox_ls(ln, probe_font)[0]
                          for ln in all_lines))
    pt = max(int(tcfg["min_pt"]), int(nominal * min(1.0, max_w / widest)))
    font = truetype(font_path, pt)

    # Bar centre lines (canvas px).
    y_line = [float(tcfg["line1_center_y_frac"]) * H,
              float(tcfg["line2_center_y_frac"]) * H]
    y_single = float(tcfg["single_center_y_frac"]) * H

    out_dir = os.path.join(clip_dir, "work", "overlays")
    os.makedirs(out_dir, exist_ok=True)

    report = {"pt": pt, "bar_hex": bar_hex, "bar_draw_hex": draw_hex,
              "bar_stats": cstats,
              "center_x": cx, "phrases": []}

    for idx, ph in enumerate(phrases, start=1):
        lines = phrase_lines(ph)
        centers = [y_single] if len(lines) == 1 else y_line[:len(lines)]
        # two canvases, not one: build_bars.py animates them separately.
        bar_img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ink_img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(ink_img)
        px0, px1 = W, 0

        for ln, cyb in zip(lines, centers):
            l, top, r, bot = bbox_ls(ln, font)
            baseline = cyb + base_off
            # tashkeel-stripped letter-body band; the pill must sit inside it
            # (never derived from the full ink bbox or from font metrics --
            # this face's marks overshoot the em box by ~0.23em).
            _, sb_top, _, sb_bot = bbox_ls(strip_tashkeel(ln), font)
            y0 = baseline - bar_h / 2.0 - base_off
            y0 = min(max(y0, baseline + sb_top), baseline + sb_bot - bar_h)
            x0 = cx - (r - l) / 2.0 - pad_x
            x1 = cx + (r - l) / 2.0 + pad_x
            draw_pill(bar_img, x0, y0, x1, y0 + bar_h, bar_rgb)
            d.text((cx - (r - l) / 2.0 - l, baseline), ln, font=font,
                   fill=ink_color + (255,), anchor="ls",
                   direction="rtl", language="ar")
            px0, px1 = min(px0, x0), max(px1, x1)

        bp = os.path.join(out_dir, f"bar{idx}.png")
        tp = os.path.join(out_dir, f"text{idx}.png")
        bar_img.save(bp)
        with_shadow(ink_img, scfg, W, H).save(tp)
        report["phrases"].append({"bar": bp, "text": tp, "lines": len(lines),
                                  "x0": px0, "x1": px1})

    report["snow"] = snow_layer(clip_dir, style, bar_hex)
    report["scrim"] = scrim_layer(clip_dir, style)

    print("render_bars.py OK")
    print(f"  bar {bar_hex} target / {draw_hex} drawn ({cstats})")
    print(f"  Arabic pt={pt} (nominal {nominal}, widest raw {int(widest)}px, "
          f"cap {int(max_w)}px), centre x={cx}")
    print(f"  text shadow {scfg['color']} a{scfg['opacity']} blur {scfg['blur_px']}px "
          f"offset +{int(round(float(scfg['dx_frac']) * W))}x "
          f"+{int(round(float(scfg['dy_frac']) * H))}y px")
    for i, r in enumerate(report["phrases"], 1):
        print(f"  bar{i}.png+text{i}.png: {r['lines']} line(s) "
              f"bars x {r['x0']:.0f}..{r['x1']:.0f} (w {r['x1']-r['x0']:.0f})")
    return report


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1
          else os.path.join(ROOT, "clips", "at-tawbah-128-128"))
