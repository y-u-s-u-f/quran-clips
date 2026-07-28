"""
Pin the scalars qc/fx derives from templates/bars.yaml.

WHY THESE AND NOT MEASUREMENTS OF THE OUTPUT. Every number in templates/bars.yaml
was fitted once against the two reference reels, and the fitting is written up in
the template and in style/refs2/FX_RECIPE.md. Those numbers are now INPUTS. The
old QA pass re-measured glow falloff, snow blob counts and heat RMS off a
rendered frame and compared them back to the constants that produced them -- a
tautology that cost minutes per run and could only ever fail because of encoder
noise.

What can actually rot is the ALGEBRA between the template value and the filter
string: `scatter * 2.86 * BH/405`, the two knee slopes, the tint triples, the
perlin px -> 8-bit-map conversion. That is code, it has no test elsewhere, and
the goldens only cover it for the four clips whose full filtergraph is frozen
(and only while every effect is enabled). These assertions are the same facts
in milliseconds, and they are the values that produced the approved render.

    tools/render-venv/bin/python -m unittest discover -s tests
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "scripts"), ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import render_text                                    # noqa: E402
import qc.ffgraph                                     # noqa: E402
import qc.fx                                          # noqa: E402
from qc.fx import effects                             # noqa: E402

STYLE = render_text.load_yaml(os.path.join(ROOT, "templates", "bars.yaml"))

# The approved render's canvas, and the bar colour at-tawbah-128-128's own
# footage derives (#BF8C36) -- so every tint triple below is literally the one
# in tests/golden/at-tawbah-128-128/filtergraph.txt.
BAND_H = 608
TINT = [191 / 255.0, 140 / 255.0, 54 / 255.0]


def ctx(**kw):
    d = dict(W=1080, H=1920, BW=1080, BH=BAND_H, BY=656, fps=30, dur=23.720,
             tint=TINT, nphrases=4, band_src="band", snow_in="9:v",
             hexrgb=render_text.hexrgb)
    d.update(kw)
    return qc.fx.Ctx(**d)


def emit(cls, **kw):
    """Run one effect's band stage on an empty graph -> its filter_complex."""
    g = qc.ffgraph.Graph()
    g.chain(None, "color=c=black", "band")
    eff = cls(STYLE, {})
    eff.apply(g, "band", ctx(**kw))
    return g.render()[0]


class DerivedScalars(unittest.TestCase):

    def test_glow_sigma_scales_with_the_band(self):
        # scatter 10.0 * 2.86 * 608/405
        self.assertIn("gblur=sigma=42.94:steps=3", emit(effects.Glow))

    def test_glow_knee_is_the_lo_hi_ramp(self):
        # lo 0.20 -> 51, hi 0.40 -> 102, slope 255/(102-51)
        self.assertIn("lutrgb=r='clip((val-51)*5.0,0,255)'", emit(effects.Glow))

    def test_glow_tint_is_the_bar_colour_times_gain(self):
        # #BF8C36 / 255 * gain 0.35
        self.assertIn("colorchannelmixer=rr=0.2622:gg=0.1922:bb=0.0741",
                      emit(effects.Glow))

    def test_scan_tint_is_the_bar_colour_times_its_own_gain(self):
        # gain 1.40 -- this pass is allowed to run past 1.0
        self.assertIn("colorchannelmixer=rr=1.0486:gg=0.7686:bb=0.2965",
                      emit(effects.Scan))

    def test_scan_sigma_is_the_radius_doubled(self):
        # scatter 4.0 * 1.20 * radius 0.70 * 2 * 608/405
        self.assertIn("gblur=sigma=10.09:steps=3", emit(effects.Scan))

    def test_scan_knee_is_keyed_off_the_threshold(self):
        # threshold 0.90 -> 230 (rounded), slope 255/(255-230) = 10.2
        self.assertIn("lutrgb=r='clip((val-230)*10.2,0,255)'",
                      emit(effects.Scan))

    def test_barglow_near_and_far_weights(self):
        # #BF8C36 / 255 * gain 1.20 * weight_near 0.22 / weight_far 0.42
        fc = emit(effects.BarGlow)
        self.assertIn("gblur=sigma=21.0:steps=3", fc)
        self.assertIn("colorchannelmixer=rr=0.1977:gg=0.1449:bb=0.0559", fc)
        self.assertIn("gblur=sigma=90.0:steps=3", fc)
        self.assertIn("colorchannelmixer=rr=0.3775:gg=0.2767:bb=0.1067", fc)

    def test_textglow_tint_is_warm_white_not_the_bar_hue(self):
        # #FFF4E0 * gain 0.45 -- deliberately NOT ctx.tint
        fc = emit(effects.TextGlow)
        self.assertIn("gblur=sigma=14.0:steps=3", fc)
        self.assertIn("colorchannelmixer=rr=0.45:gg=0.4306:bb=0.3953", fc)

    def test_heat_map_slope_converts_px_to_8bit(self):
        # rms 0.00133*1080 = 1.4364 px, supersample 3, perlin_sd 18.4
        self.assertIn("lutrgb=r='128+(val-130.26)*0.2342'", emit(effects.Heat))

    def test_heat_supersamples_by_three(self):
        fc = emit(effects.Heat)
        self.assertIn("scale=3240:1824:flags=neighbor", fc)
        self.assertIn("scale=1080:608:flags=area", fc)
        self.assertIn("perlin=size=1080x608:rate=30:octaves=6:persistence=0.6"
                      ":xscale=2.0:yscale=2.0:tscale=0.5:random_mode=seed"
                      ":seed=11", fc)

    def test_every_effect_screens_over_the_band(self):
        for cls in (effects.Glow, effects.BarGlow, effects.TextGlow,
                    effects.Scan, effects.Snow):
            self.assertIn("blend=all_mode=screen:shortest=1", emit(cls),
                          cls.name)

    def test_luma_mixer_is_rec601(self):
        self.assertEqual(
            qc.fx.LUMA,
            "colorchannelmixer=rr=0.299:rg=0.587:rb=0.114:"
            "gr=0.299:gg=0.587:gb=0.114:br=0.299:bg=0.587:bb=0.114")


class Registry(unittest.TestCase):

    def test_fx_order_lists_every_band_effect(self):
        self.assertEqual(list(STYLE["fx"]["order"]), list(qc.fx.BAND_FX))

    def test_unknown_effect_in_a_clip_is_a_hard_error(self):
        with self.assertRaises(SystemExit) as e:
            qc.fx.switches(STYLE, {"fx": {"heatwave": False}})
        self.assertIn("heatwave", str(e.exception))

    def test_disabling_one_effect_drops_it_from_the_chain(self):
        on = [e.name for e in qc.fx.fx_chain(STYLE, {})]
        off = [e.name for e in qc.fx.fx_chain(STYLE, {"fx": {"scan": False}})]
        self.assertIn("scan", on)
        self.assertNotIn("scan", off)
        self.assertEqual(len(on) - 1, len(off))


if __name__ == "__main__":
    unittest.main()
