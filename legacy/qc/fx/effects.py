"""
qc/fx/effects.py -- the switchable stages of the "bars" render.

Every filter string and every derived scalar below was moved VERBATIM out of
scripts/build_bars.py. Nothing here is a re-derivation: the sigma/knee/tint
algebra, the Rec.601 luma mixer, the perlin centre/sd -> slope conversion and
the literal `split=2`s are the same expressions in the same order, so with all
stages enabled the emitted filtergraph is byte-identical to the pre-registry
one. The measurement notes that justify the numbers live in
templates/bars.yaml and style/refs2/FX_RECIPE.md; only the notes about WHERE a
stage hooks into the graph are repeated here.
"""
from .base import Effect, LUMA


# --------------------------------------------------------------------------
# pre-composite stages -- switches only; they are wired at their own sites
# (render_bars.band_source_chain for the grade, build_bars for the scrim)
# rather than in the band chain, so they carry no hooks.
# --------------------------------------------------------------------------
class Grade(Effect):
    """Step 0 of FX_RECIPE: eq + colorbalance + vignette on the footage,
    before the captions. The biggest single contributor to the look -- and it
    also feeds the bar-colour derivation, so disabling it moves the pill hue
    as well as the picture."""
    name = "grade"
    band = False

    @classmethod
    def config(cls, style):
        return style.get("grade") or {}


class Scrim(Effect):
    """The generated left/edge multiply plate (render_bars.scrim_layer),
    multiplied into the graded band before the pad. Disabling it drops the
    plate's ffmpeg input entirely."""
    name = "scrim"
    band = False

    @classmethod
    def config(cls, style):
        return (style.get("grade") or {}).get("scrim") or {}


# --------------------------------------------------------------------------
# band chain
# --------------------------------------------------------------------------
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
        g.chain(g1, [LUMA, KNEE, f"gblur=sigma={sig}:steps=3",
                     f"colorchannelmixer=rr={gr}:gg={gg}:bb={gb}"], "glow")
        return g.chain([band, "glow"], "blend=all_mode=screen:shortest=1", "s1")


class BarGlow(Effect):
    """The pill's own glow. It rides the BAR layer alone -- keyed off the
    composite it is at the mercy of scene brightness (measured: 3.4x the gain
    moved the excess at d=5 by 1 DN). Two gaussians, near + far, because the
    refs' tail is exponential rather than gaussian (STYLE2_SPEC B.6).

    Hooks: a black plate, then one WHITE silhouette of each phrase's pill
    accumulated onto it (white so the glow's amplitude does not follow the
    clip-derived pill colour), then the two-gaussian pass in the band chain."""
    name = "barglow"
    plate_rank = 1
    layer = "bar"

    def plate(self, g, ctx):
        g.chain(None,
                f"color=c=black:s={ctx.W}x{ctx.H}:r={ctx.fps}:d={ctx.dur:.3f}",
                "bq0")

    def per_phrase(self, g, ctx, i, lbl):
        g.chain(f"x{lbl}", "split=2", [f"x{lbl}c", f"x{lbl}g"])
        g.chain(f"x{lbl}g", "alphaextract", f"q{i + 1}")
        g.chain(None,
                f"color=c=white:s={ctx.W}x{ctx.H}:r={ctx.fps}:d={ctx.dur:.3f}",
                f"qw{i + 1}")
        g.chain([f"qw{i + 1}", f"q{i + 1}"], "alphamerge", f"qm{i + 1}")
        g.chain([f"bq{i}", f"qm{i + 1}"],
                "overlay=0:0:format=auto:shortest=1", f"bq{i + 1}")
        return f"{lbl}c"

    def apply(self, g, band, ctx):
        c = self.cfg
        bgsn = round(float(c["sigma_near_px"]), 2)
        bgsf = round(float(c["sigma_far_px"]), 2)
        bgn = [round(v * float(c["gain"]) * float(c["weight_near"]), 4)
               for v in ctx.tint]
        bgf = [round(v * float(c["gain"]) * float(c["weight_far"]), 4)
               for v in ctx.tint]
        g.chain(f"bq{ctx.nphrases}", ["format=gbrp", "split=2"],
                ["bqn", "bqf"])
        g.chain("bqn", [f"gblur=sigma={bgsn}:steps=3",
                        f"colorchannelmixer=rr={bgn[0]}:gg={bgn[1]}:bb={bgn[2]}"],
                "bgn")
        g.chain("bqf", [f"gblur=sigma={bgsf}:steps=3",
                        f"colorchannelmixer=rr={bgf[0]}:gg={bgf[1]}:bb={bgf[2]}"],
                "bgf")
        g.chain(["bgn", "bgf"], ["blend=all_mode=addition:shortest=1",
                                 f"crop={ctx.BW}:{ctx.BH}:0:{ctx.BY}"],
                "barglow")
        return g.chain([band, "barglow"],
                       "blend=all_mode=screen:shortest=1", "s1a")


class TextGlow(Effect):
    """The tight halo. It rides the TEXT layer alone and carries its own
    near-white tint -- the refs' halo is warm neutral, notably NOT the bar hue.

    Hooks: a black plate, each phrase's text layer accumulated onto it, then
    the single blur pass in the band chain."""
    name = "textglow"
    plate_rank = 0
    layer = "text"

    def plate(self, g, ctx):
        g.chain(None,
                f"color=c=black:s={ctx.W}x{ctx.H}:r={ctx.fps}:d={ctx.dur:.3f}",
                "tg0")

    def per_phrase(self, g, ctx, i, lbl):
        g.chain(f"x{lbl}", "split=2", [f"x{lbl}c", f"x{lbl}g"])
        g.chain([f"tg{i}", f"x{lbl}g"],
                "overlay=0:0:format=auto:shortest=1", f"tg{i + 1}")
        return f"{lbl}c"

    def apply(self, g, band, ctx):
        c = self.cfg
        tgsig = round(float(c["sigma_px"]), 2)
        tgr, tgg, tgb = [round(v / 255.0 * float(c["gain"]), 4)
                         for v in ctx.hexrgb(c["tint"], (255, 244, 224))]
        g.chain(f"tg{ctx.nphrases}",
                ["format=gbrp", f"crop={ctx.BW}:{ctx.BH}:0:{ctx.BY}", LUMA,
                 f"gblur=sigma={tgsig}:steps=3",
                 f"colorchannelmixer=rr={tgr}:gg={tgg}:bb={tgb}"], "tglow")
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
        g.chain(sc0, [LUMA, SKNEE, f"gblur=sigma={ssig}:steps=3",
                      "lutrgb=r='clip(val*3,0,255)':g='clip(val*3,0,255)':"
                      "b='clip(val*3,0,255)'",
                      f"colorchannelmixer=rr={sr}:gg={sg}:bb={sb}"], "scan")
        return g.chain([band, "scan"],
                       "blend=all_mode=screen:shortest=1", "s2")


class Snow(Effect):
    """Node Video's procedural two-layer noise shader, pre-rendered to a
    seamless loop by render_bars.snow_layer and screened over the band."""
    name = "snow"

    def apply(self, g, band, ctx):
        g.chain(ctx.snow_in, ["format=gbrp",
                              f"scale={ctx.BW}:{ctx.BH}:flags=neighbor",
                              "setsar=1"], "pt")
        return g.chain([band, "pt"],
                       "blend=all_mode=screen:shortest=1", "bandfx0")


class Heat(Effect):
    """HEAT WAVE -- the 4th Node effect. Runs LAST, over the composited band,
    so the footage, the pills and the glyphs are displaced by one shared perlin
    field (Node runs over a flattened export, so the warp inherently hits the
    text). Measured in both refs at 0.96 px RMS @720 against a 0.10 px noise
    floor -- 1.44 px at this canvas. See FX_RECIPE.md "Heat Wave - motion
    re-test".

    It is the most expensive stage in the graph (~49% of render time: two
    supersampled perlin maps plus a 3x up/down scale around `displace`);
    `enable: false` for a fast preview."""
    name = "heat"

    def apply(self, g, band, ctx):
        ht = self.cfg
        S = int(ht.get("supersample", 3))
        rms = float(ht["rms_frac_w"]) * ctx.W       # RMS displacement in px
        ctr = float(ht.get("perlin_centre", 130.26))
        sdp = float(ht.get("perlin_sd", 18.4))
        k = round(rms * S / sdp, 5)                 # px -> 8-bit map slope
        SW, SH = ctx.BW * S, ctx.BH * S
        psrc = (f"perlin=size={ctx.BW}x{ctx.BH}:rate={ctx.fps}"
                f":octaves={int(ht.get('octaves', 6))}"
                f":persistence={ht.get('persistence', 0.6)}"
                f":xscale={ht['xscale']}:yscale={ht['xscale']}"
                f":tscale={ht['tscale']}:random_mode=seed")
        pmap = (f"scroll=vertical={ht['scroll_v']},"
                f"scale={SW}:{SH}:flags=bicubic,format=gbrp,"
                f"lutrgb=r='128+(val-{ctr})*{k}':g='128+(val-{ctr})*{k}':"
                f"b='128+(val-{ctr})*{k}'")
        g.chain(None, [f"{psrc}:seed={int(ht.get('seed_x', 11))}", pmap], "xm")
        g.chain(None, [f"{psrc}:seed={int(ht.get('seed_y', 77))}", pmap], "ym")
        # neighbour up / area down: the upscale must not soften, the downscale
        # must average -- that is what turns integer `displace` into 1/S px.
        g.chain(band, f"scale={SW}:{SH}:flags=neighbor", "bigb")
        return g.chain(["bigb", "xm", "ym"],
                       ["displace=edge=smear",
                        f"scale={ctx.BW}:{ctx.BH}:flags=area"], "bandfx")


ALL = (Grade, Scrim, Glow, BarGlow, TextGlow, Scan, Snow, Heat)
