"""pipeline/fx.py -- the bars FX stack: filtergraph builder + effects + snow.

Ported VERBATIM from legacy/qc/ffgraph.py, legacy/qc/fx/ and the snow shader
in legacy/scripts/render_bars.py. Nothing here is a re-derivation: the
sigma/knee/tint algebra, the Rec.601 luma mixer, the perlin centre/sd ->
slope conversion and the literal `split=2`s are the same expressions in the
same order, so with all stages enabled the emitted filtergraph is
byte-identical to the legacy one (verified against the frozen golden
fixture). The measurement notes justifying every number live in
legacy/templates/bars.yaml and legacy/style/refs2/FX_RECIPE.md.

Contents:
  Graph            a small chain/label builder for -filter_complex
  Ctx              attribute bag the effects read canvas/band facts from
  Glow ... Heat    the six switchable band stages
  render_snow      the Node Video "Snow" procedural shader, evaluated with
                   Pillow into a seamlessly-looping mp4
"""
import math
import os
import random
import subprocess
import sys

from PIL import Image, ImageChops

FFMPEG = os.environ.get("QC_FFMPEG") or "ffmpeg"


# ---------------------------------------------------------------------------
# filtergraph builder
# ---------------------------------------------------------------------------

class Ref(str):
    """An input pad reference such as "0:v"."""

    @property
    def v(self):
        return Ref(str(self).split(":")[0] + ":v")

    @property
    def a(self):
        return Ref(str(self).split(":")[0] + ":a")


class _Chain(object):
    __slots__ = ("ins", "filters", "outs")

    def __init__(self, ins, filters, outs):
        self.ins, self.filters, self.outs = ins, filters, outs


class _Tap(object):
    """A split whose degree is not known until every consumer has asked."""
    __slots__ = ("src", "outs")

    def __init__(self, src):
        self.src, self.outs = src, []


def _labels(x):
    if x is None:
        return []
    if isinstance(x, str):
        return [x]
    return [str(i) for i in x]


def _joined(f):
    return f if isinstance(f, str) else ",".join(f)


class Graph(object):
    """Deliberately NOT a DAG library: chains are recorded in declaration
    order and joined with ';', so a ported graph emits a byte-identical
    string. input() owns the -i argv so indices can never drift out of sync
    with the [N:v] labels; tap() defers a split's degree to the number of
    consumers that actually asked."""

    # ffmpeg's per-input packet queue defaults to 8. A bars clip feeds ONE
    # filtergraph from many inputs (source + a bar/text pair per phrase +
    # snow + scrim), and past roughly a dozen the graph DEADLOCKS: one
    # input's queue fills, blocking the demuxer in tq_send, while the filter
    # and encoder threads sit in tq_receive on a DIFFERENT input -- 0% CPU
    # forever, no moov atom. Costs memory only; changes no output byte.
    THREAD_QUEUE_SIZE = 4096

    def __init__(self):
        self._nodes = []
        self._argv = []
        self._taps = {}
        self._ninputs = 0

    def input(self, path, **kw):
        """Record an ffmpeg input; kwargs become the pre -i options in the
        order given. Returns the video pad ref; use .a for audio."""
        idx = self._ninputs
        self._ninputs += 1
        for k, val in kw.items():
            self._argv += ["-" + k, str(val)]
        self._argv += ["-thread_queue_size", str(self.THREAD_QUEUE_SIZE),
                       "-i", path]
        return Ref("%d:v" % idx)

    def chain(self, ins, filters, outs):
        """Append "[in]...filters...[out]...". `ins=None` declares a source
        chain (color=, perlin=, ...)."""
        outs = _labels(outs)
        self._nodes.append(_Chain(_labels(ins), _joined(filters), outs))
        return outs[0] if len(outs) == 1 else tuple(outs)

    def tap(self, label, name=None):
        """A branch off `label`; the split's degree grows per call. A tap
        with a single consumer emits no split at all."""
        t = self._taps.get(label)
        if t is None:
            t = _Tap(label)
            self._taps[label] = t
            self._nodes.append(t)
        out = name or "%s_t%d" % (label, len(t.outs) + 1)
        t.outs.append(out)
        return out

    def render(self):
        """-> (filter_complex, input argv)."""
        alias = {}
        for n in self._nodes:
            if isinstance(n, _Tap) and len(n.outs) == 1:
                alias[n.outs[0]] = n.src

        def res(lbl):
            for _ in range(len(alias) + 1):
                if lbl not in alias:
                    break
                lbl = alias[lbl]
            return lbl

        parts = []
        for n in self._nodes:
            if isinstance(n, _Tap):
                if len(n.outs) < 2:
                    continue
                parts.append("[%s]split=%d%s"
                             % (res(n.src), len(n.outs),
                                "".join("[%s]" % o for o in n.outs)))
            else:
                parts.append("%s%s%s"
                             % ("".join("[%s]" % res(i) for i in n.ins),
                                n.filters,
                                "".join("[%s]" % o for o in n.outs)))
        return ";".join(parts), list(self._argv)


# ---------------------------------------------------------------------------
# effects
# ---------------------------------------------------------------------------

# Rec.601 luma as a 3x3 channel mixer, so it can sit inside an RGB chain
# without a format change. Shared by glow, textglow and scan.
LUMA = ("colorchannelmixer=rr=0.299:rg=0.587:rb=0.114:"
        "gr=0.299:gg=0.587:gb=0.114:br=0.299:bg=0.587:bb=0.114")


class Ctx(object):
    """Plain attribute bag: canvas/band geometry plus the clip-derived values
    every effect draws on (tint, phrase count, the band source label...)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class Effect(object):
    """An effect owns its parameters (`cfg`), its filter strings, and where
    it hooks in: `plate` (a black accumulator declared before the phrase
    loop), `per_phrase` (a branch off one caption layer inside the loop),
    `apply` (the band-chain stage, ordered by BAND_ORDER)."""
    name = None
    plate_rank = None    # not None => emits an accumulator plate, at this rank
    layer = None         # caption layer it branches off ("bar" / "text")

    def __init__(self, cfg):
        self.cfg = cfg

    def plate(self, g, ctx):
        pass

    def per_phrase(self, g, ctx, i, lbl):
        return lbl

    def apply(self, g, band, ctx):
        return band


# A gaussian is a low-pass: everything a wide blur keeps survives a coarsening
# of the sampling grid, so blurring at reduced linear size and scaling back
# costs 1/n^2 of the pixels for a difference below the 8-bit quantiser. The
# same trade is made in render_text, whose two grade gradients are built at
# 48x27 and bilinearly upscaled -- "that low resolution IS the softness of the
# look".
#
# Quarter threshold: at 1/n the kernel still spans sigma/n samples, and sigma
# 40 keeps that at 10. Half threshold (10) is a measurement that brings in scan
# (sigma 10.09) and textglow (sigma 14): 7.2s -> 3.0s CPU each per 10s of
# 1080x608 (-58%), at 55.6 dB and 54.9 dB against real footage.
HALF_RES_SIGMA = 10.0
QUARTER_RES_SIGMA = 40.0


def blur(sigma, w, h, steps=3):
    """gblur at `sigma`, evaluated at reduced linear size when that is safe.

    The restore is written as an EXPLICIT size rather than `iw*n`: `iw/n`
    truncates, so on any input whose dimensions are not a multiple of n the
    round trip would land a pixel short and the blend downstream would fail
    on a size mismatch."""
    if sigma < HALF_RES_SIGMA:
        return [f"gblur=sigma={sigma}:steps={steps}"]
    n = 4 if sigma >= QUARTER_RES_SIGMA else 2
    return [f"scale={w // n}:{h // n}:flags=bilinear",
            f"gblur=sigma={round(sigma / n, 2)}:steps={steps}",
            f"scale={w}:{h}:flags=bilinear"]


class Glow(Effect):
    """The WIDE scene bloom, keyed off the composited band."""
    name = "glow"

    def apply(self, g, band, ctx):
        c = self.cfg
        sig = round(float(c["scatter"]) * 2.86 * ctx.BH / 405.0, 2)
        lo, hi = round(float(c["lo"]) * 255), round(float(c["hi"]) * 255)
        knee = round(255.0 / (hi - lo), 6)
        gr, gg, gb = [round(v * float(c["gain"]), 4) for v in ctx.tint]
        KNEE = (f"lutrgb=r='clip((val-{lo})*{knee},0,255)':"
                f"g='clip((val-{lo})*{knee},0,255)':"
                f"b='clip((val-{lo})*{knee},0,255)'")
        g1 = g.tap(ctx.band_src, "g1")
        g.chain(g1, [LUMA, KNEE] + blur(sig, ctx.BW, ctx.BH) +
                [f"colorchannelmixer=rr={gr}:gg={gg}:bb={gb}"], "glow")
        return g.chain([band, "glow"], "blend=all_mode=screen:shortest=1", "s1")


class BarGlow(Effect):
    """The pill's own glow, riding the BAR layer alone -- keyed off the
    composite it is at the mercy of scene brightness. Two gaussians, near +
    far, because the refs' tail is exponential rather than gaussian. The
    plate accumulates a WHITE silhouette of each phrase's pill, so the glow's
    amplitude does not follow the clip-derived pill colour."""
    name = "barglow"
    plate_rank = 1
    layer = "bar"

    def slice_rows(self, ctx):
        """(y0, height) of the only canvas rows that can reach the band.

        The plate is black wherever the pills are not and the pills live in
        the band, so 3 sigma of the FAR gaussian either side already holds
        every pixel whose blur can land inside it; past that the source is
        black, and gblur's edge replication of black is what more black would
        have contributed anyway. Accumulating at this height rather than the
        full 1920 makes the plate and every overlay onto it 40% smaller, and
        the slice is exact, so nothing downstream can tell."""
        mrg = 2 * int(math.ceil(1.5 * round(float(self.cfg["sigma_far_px"]), 2)))
        y0 = max(0, ctx.BY - mrg)
        return y0, min(ctx.H - y0, ctx.BH + 2 * mrg)

    def plate(self, g, ctx):
        _y0, hh = self.slice_rows(ctx)
        g.chain(None,
                f"color=c=black:s={ctx.W}x{hh}:r={ctx.fps}:d={ctx.dur:.3f}",
                "bq0")

    def per_phrase(self, g, ctx, i, lbl):
        # The caption layer arrives cropped to its own ink, so the white
        # silhouette is struck at that size and PLACED on the accumulator --
        # which is itself only the slice, hence the y0 shift. A pill that
        # fell outside the slice is clipped by the overlay, which is the
        # right answer: outside the slice is outside the blur's reach.
        y0, _hh = self.slice_rows(ctx)
        x, y, w, h = ctx.boxes[i]["bar"]
        y = y - y0
        g.chain(f"x{lbl}", "split=2", [f"x{lbl}c", f"x{lbl}g"])
        g.chain(f"x{lbl}g", "alphaextract", f"q{i + 1}")
        g.chain(None,
                f"color=c=white:s={w}x{h}:r={ctx.fps}:d={ctx.dur:.3f}",
                f"qw{i + 1}")
        g.chain([f"qw{i + 1}", f"q{i + 1}"], "alphamerge", f"qm{i + 1}")
        g.chain([f"bq{i}", f"qm{i + 1}"],
                f"overlay={x}:{y}:format=auto:shortest=1"
                f":enable='{ctx.gates[i]}'", f"bq{i + 1}")
        return f"{lbl}c"

    def apply(self, g, band, ctx):
        c = self.cfg
        bgsn = round(float(c["sigma_near_px"]), 2)
        bgsf = round(float(c["sigma_far_px"]), 2)
        bgn = [round(v * float(c["gain"]) * float(c["weight_near"]), 4)
               for v in ctx.tint]
        bgf = [round(v * float(c["gain"]) * float(c["weight_far"]), 4)
               for v in ctx.tint]
        # The plate arrives already sliced (see slice_rows), so there is
        # nothing left to crop off the top before blurring.
        y0, hh = self.slice_rows(ctx)
        g.chain(f"bq{ctx.nphrases}", ["format=gbrp", "split=2"],
                ["bqn", "bqf"])
        g.chain("bqn", blur(bgsn, ctx.W, hh) +
                [f"colorchannelmixer=rr={bgn[0]}:gg={bgn[1]}:bb={bgn[2]}"],
                "bgn")
        g.chain("bqf", blur(bgsf, ctx.W, hh) +
                [f"colorchannelmixer=rr={bgf[0]}:gg={bgf[1]}:bb={bgf[2]}"],
                "bgf")
        g.chain(["bgn", "bgf"], ["blend=all_mode=addition:shortest=1",
                                 f"crop={ctx.BW}:{ctx.BH}:0:{ctx.BY - y0}"],
                "barglow")
        return g.chain([band, "barglow"],
                       "blend=all_mode=screen:shortest=1", "s1a")


class TextGlow(Effect):
    """The tight halo, riding the TEXT layer alone with its own near-white
    tint -- the refs' halo is warm neutral, notably NOT the bar hue."""
    name = "textglow"
    plate_rank = 0
    layer = "text"

    def plate(self, g, ctx):
        # Band-sized, not canvas-sized: this plate is cropped to the band
        # before the blur, so glyph ink outside the band never contributes.
        # Accumulating at band size is the same pixels for a third of the
        # plate and a third of every overlay onto it.
        g.chain(None,
                f"color=c=black:s={ctx.BW}x{ctx.BH}:r={ctx.fps}:d={ctx.dur:.3f}",
                "tg0")

    def per_phrase(self, g, ctx, i, lbl):
        x, y, _w, _h = ctx.boxes[i]["text"]
        g.chain(f"x{lbl}", "split=2", [f"x{lbl}c", f"x{lbl}g"])
        g.chain([f"tg{i}", f"x{lbl}g"],
                f"overlay={x}:{y - ctx.BY}:format=auto:shortest=1"
                f":enable='{ctx.gates[i]}'", f"tg{i + 1}")
        return f"{lbl}c"

    def apply(self, g, band, ctx):
        c = self.cfg
        tgsig = round(float(c["sigma_px"]), 2)
        tgr, tgg, tgb = [round(v / 255.0 * float(c["gain"]), 4)
                         for v in ctx.hexrgb(c["tint"])]
        g.chain(f"tg{ctx.nphrases}",
                ["format=gbrp", LUMA] + blur(tgsig, ctx.BW, ctx.BH) +
                [f"colorchannelmixer=rr={tgr}:gg={tgg}:bb={tgb}"], "tglow")
        return g.chain([band, "tglow"],
                       "blend=all_mode=screen:shortest=1", "s1b")


class Scan(Effect):
    """Glow Scan: a second, harder-keyed bloom off the band."""
    name = "scan"

    def apply(self, g, band, ctx):
        c = self.cfg
        ssig = round(float(c["scatter"]) * 1.20 * float(c["radius"]) * 2
                     * ctx.BH / 405.0, 2)
        sth = round(float(c["threshold"]) * 255)
        sknee = round(255.0 / (255 - sth), 6)
        sr, sg, sb = [round(v * float(c["gain"]), 4) for v in ctx.tint]
        SKNEE = (f"lutrgb=r='clip((val-{sth})*{sknee},0,255)':"
                 f"g='clip((val-{sth})*{sknee},0,255)':"
                 f"b='clip((val-{sth})*{sknee},0,255)'")
        sc0 = g.tap(ctx.band_src, "sc0")
        g.chain(sc0, [LUMA, SKNEE] + blur(ssig, ctx.BW, ctx.BH) +
                ["lutrgb=r='clip(val*3,0,255)':g='clip(val*3,0,255)':"
                 "b='clip(val*3,0,255)'",
                 f"colorchannelmixer=rr={sr}:gg={sg}:bb={sb}"], "scan")
        return g.chain([band, "scan"],
                       "blend=all_mode=screen:shortest=1", "s2")


class Snow(Effect):
    """The pre-rendered snow loop (render_snow below), screened over the
    band."""
    name = "snow"

    def apply(self, g, band, ctx):
        g.chain(ctx.snow_in, ["format=gbrp",
                              f"scale={ctx.BW}:{ctx.BH}:flags=neighbor",
                              "setsar=1"], "pt")
        return g.chain([band, "pt"],
                       "blend=all_mode=screen:shortest=1", "bandfx0")


def heat_perlin_src(cfg, bw, bh, fps, seed):
    """The bare `perlin` source for one displacement map.

    Shared by the graph and by the pre-bake in render_bars.heat_layers, so a
    baked map cannot drift from what a live one would have produced: change a
    parameter here and the cache tag changes with it."""
    return (f"perlin=size={bw}x{bh}:rate={fps}"
            f":octaves={int(cfg['octaves'])}"
            f":persistence={cfg['persistence']}"
            f":xscale={cfg['xscale']}:yscale={cfg['xscale']}"
            f":tscale={cfg['tscale']}:random_mode=seed:seed={int(seed)}")


class Heat(Effect):
    """HEAT WAVE -- runs LAST, over the composited band, so footage, pills
    and glyphs are displaced by one shared perlin field. Measured in both
    refs at 0.96 px RMS @720 against a 0.10 px noise floor.

    The two perlin fields are the x- and y-displacement maps `displace`
    takes. They are PRE-BAKED (render_bars.heat_layers) and arrive here as
    inputs: `perlin` is single-threaded and cost 66s per map per reel
    (real 66.2s vs user 65.2s -- one core of eight), 132s of a 372s render,
    while depending on nothing but its own constants. Baked once per machine
    it is reused by every reel forever. What stays live is scroll/scale/lut,
    which is where the remaining cost of this stage is: the 3x supersample
    around integer-pixel `displace`. `fx: {heat: false}` for a fast preview."""
    name = "heat"

    def apply(self, g, band, ctx):
        ht = self.cfg
        S = int(ht["supersample"])                  # 2 is the measured value
        rms = float(ht["rms_frac_w"]) * ctx.W       # RMS displacement in px
        ctr = float(ht["perlin_centre"])
        sdp = float(ht["perlin_sd"])
        k = round(rms * S / sdp, 5)                 # px -> 8-bit map slope
        SW, SH = ctx.BW * S, ctx.BH * S
        pmap = (f"scroll=vertical={ht['scroll_v']},"
                f"scale={SW}:{SH}:flags=bicubic,format=gbrp,"
                f"lutrgb=r='128+(val-{ctr})*{k}':g='128+(val-{ctr})*{k}':"
                f"b='128+(val-{ctr})*{k}'")
        g.chain(ctx.heat_x_in, pmap, "xm")
        g.chain(ctx.heat_y_in, pmap, "ym")
        # neighbour up / area down: the upscale must not soften, the
        # downscale must average -- what turns integer `displace` into 1/S px.
        g.chain(band, f"scale={SW}:{SH}:flags=neighbor", "bigb")
        return g.chain(["bigb", "xm", "ym"],
                       ["displace=edge=smear",
                        f"scale={ctx.BW}:{ctx.BH}:flags=area"], "bandfx")


BAND_CLASSES = {c.name: c for c in (Glow, BarGlow, TextGlow, Scan, Snow, Heat)}
BAND_ORDER = ("glow", "barglow", "textglow", "scan", "snow", "heat")


def fx_chain(fx_cfg, enables):
    """The enabled band effects, in chain order. `enables` maps name -> bool
    (template default merged with the clip's `fx:` overrides); an unknown
    name there is the caller's error to have raised already."""
    return [BAND_CLASSES[n](fx_cfg[n]) for n in BAND_ORDER if enables[n]]


def plates(chain):
    """The effects that emit an accumulator plate, in FROZEN rank order --
    plate order predates the band ordering and is part of the byte-for-byte
    filtergraph contract."""
    return sorted((e for e in chain if e.plate_rank is not None),
                  key=lambda e: e.plate_rank)


def by_layer(chain):
    return {e.layer: e for e in chain if e.layer}


# ---------------------------------------------------------------------------
# snow layer
#
# NOT a particle system. Node Video's "Snow" is a single full-screen
# procedural pass; the compiled GLSL was recovered from the app
# (com.shallwaystudio.nodevideo 8.8.0) and reads, in full:
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
# `noise` is a bundled 256x256 white-noise texture, bilinear + repeat (a
# seeded RNG tile is statistically identical, so ours is generated rather
# than shipping an extracted app asset). Two heavily magnified copies scroll
# at slightly different scales and rates; their product raised to a high
# power leaves only the rare coincidences of two near-white samples -- the
# "snowflakes". Seamless loop: each layer's travel over one loop is exactly
# its own tile height, the sway is a whole sinusoid, so frame N == frame 0.
# Calibration against the reference reels is in legacy/style/refs2/SNOW_SPEC.md.
# ---------------------------------------------------------------------------
SNOW_REF_H = 405.0          # band height the forensics were measured at
SNOW_NX = 256               # noise tile width, texels (as shipped by the app)


def _snow_tile(nx, ny, seed):
    rnd = random.Random(seed)
    return Image.frombytes("L", (nx, ny),
                           bytes(rnd.randrange(256) for _ in range(nx * ny)))


def _snow_wrapped(base, nx, ny, need_w, need_h):
    """Tile `base` so an Image.transform sample window never runs off it."""
    im = Image.new("L", (nx + need_w, ny + need_h))
    for oy in range(0, im.height, ny):
        for ox in range(0, im.width, nx):
            im.paste(base, (ox, oy))
    return im


class SnowField(object):
    """One loop of the decompiled shader, evaluated with Pillow -- two
    Image.transform bilinear samples, one multiply, one point() LUT for the
    pow+gain. No numpy."""

    def __init__(self, w, h, fps, c):
        self.w, self.h = w, h
        self.nframes = max(1, int(round(c["loop_s"] * fps)))
        loop = c["loop_s"]
        texel = max(1.0, c["size"] * c["texel_scale"] * (h / SNOW_REF_H))
        self.ppt = (texel, texel * 2.0 / 3.0)          # 0.6 vs 0.9 uv scaling
        smag = abs(c["speed"])
        rate = (0.6 * 0.1000 * SNOW_NX * smag, 0.9 * 0.0714 * SNOW_NX * smag)
        cross = (-0.003 * SNOW_NX * c["flicker"], 0.002 * SNOW_NX * c["flicker"])
        # Node's Speed is negative by default and the field rises; the
        # shader's v axis is GL-oriented, so a negative Speed scrolls UP.
        up = 1.0 if c["speed"] < 0 else -1.0
        self.ny, self.trav, self.src = [], [], []
        for k in (0, 1):
            need = int(math.ceil(h / self.ppt[k])) + 3
            n = max(need, int(round((rate[k] + cross[k]) * loop)))
            self.ny.append(n)
            self.trav.append(up * n)                   # exactly one tile/loop
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


def render_snow(out_path, w, h, fps, cfg, tint_rgb):
    """Evaluate one seamless loop to `out_path` (mp4). `tint_rgb` is the bar
    colour; `tint_mix` blends the specks from pure white toward it."""
    fld = SnowField(w, h, fps, cfg)
    ch = [v / 255.0 for v in tint_rgb]
    m = max(1e-6, max(ch))
    k = cfg["tint_mix"]
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
