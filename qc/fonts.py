"""
qc.fonts -- the RAQM requirement, font loading, and the shrink-to-fit rule.

Everything Arabic in this pipeline depends on Pillow being built against RAQM
(HarfBuzz for shaping + FriBiDi for bidi). Without it Pillow lays glyphs out
left to right in codepoint order with no joining, which does not fail -- it
quietly produces wrong-looking Arabic. Hence the hard exit at import time.
"""
import sys

from PIL import ImageFont, features

_HINT = ("FATAL: Pillow RAQM (HarfBuzz+FriBiDi) not available. "
         "Use tools/render-venv/bin/python.")


def require_raqm():
    if not features.check("raqm"):
        sys.exit(_HINT)


require_raqm()

RAQM = ImageFont.Layout.RAQM


def truetype(path, pt):
    return ImageFont.truetype(path, pt, layout_engine=RAQM)


def fit_pt(nominal_pt, min_pt, max_width, widest):
    """Shrink-to-fit: one shared point size for every phrase in a clip (so a
    caption swap never changes the type size), scaled down only far enough that
    the WIDEST measured line fits `max_width`. Never scaled UP -- `nominal_pt`
    is a design target, not a fill target -- and never below `min_pt`.

    `widest` is the widest line's ink width measured at `nominal_pt`. The two
    styles measure it with different anchors (see qc/__init__ on why those stay
    apart), so measurement is the caller's job; only the rule lives here.
    """
    return max(int(min_pt), int(nominal_pt * min(1.0, max_width / widest)))
