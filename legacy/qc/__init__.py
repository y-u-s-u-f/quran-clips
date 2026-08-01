"""
qc -- shared internals of the quran-clips render pipeline.

The two style implementations (the default 1920x1080 landscape style, driven by
scripts/render_text.py + scripts/build_render.py, and the vertical 1080x1920
"bars" style, driven by scripts/render_bars.py + scripts/build_bars.py) grew as
siblings and independently re-derived the same handful of primitives: time
parsing, the transition schedule, the loudnorm/afade strings, the Arabic
house-style normalisation, the RAQM guard, the shrink-to-fit rule.

Those primitives live here, once. Everything in this package is
behaviour-preserving with respect to the goldens in tests/golden/ -- the
filtergraph strings and the generated PNGs must stay byte-identical, so the
functions below reproduce the original expressions character for character
(including the float formatting) rather than "cleaning them up".

What is deliberately NOT here (yet): filtergraph construction and the Pillow
drawing/measurement code. The two styles use different RTL measurement
conventions (render_text.bbox_rtl anchors "mm", render_bars.bbox_ls anchors
"ls") and unifying those is output-affecting.

Run everything with tools/render-venv/bin/python.
"""
