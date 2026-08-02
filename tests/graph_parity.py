"""tests/graph_parity.py -- the golden parity gate for render_bars + fx.

    tools/render-venv/bin/python tests/graph_parity.py
    tools/render-venv/bin/python tests/graph_parity.py --bless

CLAUDE.md invariant 2: a look change is an owner decision and never a
refactor side-effect. This rebuilds the at-tawbah-128-128 graph -- the useful
case because it exercises a wipe card, crossfade cards, a two-line card and a
single-line card in one graph -- and diffs it filter by filter against
tests/golden/bars-filtergraph.txt.

Only the FILTERGRAPH is fixtured, not the argv: the graph text carries every
number that decides a pixel (geometry, sigmas, gains, fade times, each
caption layer's ink box and enable window), while the argv is just the input
paths, which differ per machine.

    NO ARABIC IS TYPED HERE. The caption text is read out of
    legacy/clips/at-tawbah-128-128/clip.yaml, the archived recipe. legacy/ is
    reference material: this READS a data file from it and never imports or
    runs any of its code.

The recipe (from OPTIMIZATIONS.md, recovered from the golden):
x_offset -216, dur 23.720, crop 48,30,1280x720, tint (191,140,54) recovered
from the golden's glow scalars via rr/0.35*255, every switch on, no
signature, and the golden's own loudnorm chain so the audio leg is fixed
rather than measured.
"""
import argparse
import difflib
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Drop any cached bytecode for pipeline/ BEFORE importing it. CPython
# validates a .pyc against (source mtime IN WHOLE SECONDS, source size), so
# an edit that changes neither -- a constant tweaked from 0.35 to 0.36, then
# tweaked back within the same second -- loads the stale .pyc and this gate
# reports on code that is not on disk. Caught exactly that way while checking
# that the gate could fail at all. A check that can pass spuriously is worse
# than no check, and the cache is gitignored, so just remove it.
shutil.rmtree(os.path.join(ROOT, "pipeline", "__pycache__"), ignore_errors=True)
sys.dont_write_bytecode = True

sys.path.insert(0, os.path.join(ROOT, "pipeline"))

try:
    import yaml
except ImportError:
    sys.exit("PyYAML not available -- run with tools/render-venv/bin/python")

import render_bars as RB  # noqa: E402

CLIP = os.path.join(ROOT, "legacy", "clips", "at-tawbah-128-128", "clip.yaml")
FIXTURE = os.path.join(HERE, "golden", "bars-filtergraph.txt")

DUR = 23.720
X_OFFSET = -216
CROP = {"x": 48, "y": 30, "w": 1280, "h": 720}
TINT_RGB = (191, 140, 54)

# The golden's own audio leg, so nothing here has to decode a media file:
# loudnorm's numbers are measured off the source and would otherwise make
# the fixture depend on footage that is not tracked.
LOUDNORM = ("loudnorm=I=-14.0:TP=-1.0:LRA=11.0:measured_I=-8.81:"
            "measured_TP=0.19:measured_LRA=3.80:measured_thresh=-18.94:"
            "offset=0.63:linear=true:print_format=summary")
AFADE = "afade=t=in:st=0:d=0.3,afade=t=out:st=23.220:d=0.5"


def phrases_from_clip():
    """The archived recipe's cards, in this pipeline's phrase shape.

    ar1/ar2 are the two caption lines as the legacy config spelled them, so
    the split is carried over as `line_split` (words on line 1) rather than
    left to the auto balancer -- the fixture must pin the layout it recorded,
    not re-derive it."""
    doc = yaml.safe_load(open(CLIP, encoding="utf-8"))
    out = []
    for p in doc["phrases"]:
        if "ar" in p:
            text, split = p["ar"], None
        else:
            text = p["ar1"] + " " + p["ar2"]
            split = len(p["ar1"].split())
        ph = {"text": text, "start": float(p["t0"]), "end": float(p["t1"])}
        if split:
            ph["line_split"] = split
        out.append(ph)
    return out


def build():
    """-> the filtergraph string for the fixture clip."""
    phrases = phrases_from_clip()
    on = {n: True for n in RB.SWITCHES}
    lay = RB.layout(phrases, X_OFFSET, 0)
    tmp = tempfile.mkdtemp(prefix="graph-parity-")
    rep = RB.draw_layers(lay, RB.predraw_color(TINT_RGB),
                         os.path.join(tmp, "overlays"))
    sched, _cuts = RB.schedule(phrases, DUR)
    fc, _argv = RB.build_graph(
        "SOURCE", DUR, CROP, rep, sched, [c / 255.0 for c in TINT_RGB], on,
        "SNOW", "SCRIM", LOUDNORM, AFADE, None, ["HEATX", "HEATY"])
    return fc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bless", action="store_true",
                    help="record the current graph as the fixture. An OWNER "
                         "decision: it declares the look change intended.")
    a = ap.parse_args(argv)

    got = build()
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)

    if a.bless or not os.path.exists(FIXTURE):
        why = "blessed" if os.path.exists(FIXTURE) else "recorded (was missing)"
        with open(FIXTURE, "w", encoding="utf-8") as fh:
            fh.write(got + "\n")
        print("%s %s (%d chars)"
              % (why, os.path.relpath(FIXTURE, ROOT), len(got)))
        return 0

    want = open(FIXTURE, encoding="utf-8").read().rstrip("\n")
    if got == want:
        print("OK  filtergraph matches %s (%d chars)"
              % (os.path.relpath(FIXTURE, ROOT), len(got)))
        return 0

    # One filter per line so the diff points at the stage that moved; the
    # emitted graph is a single ';'-joined line and would diff as "everything".
    print("FAIL  the emitted filtergraph no longer matches the fixture.\n"
          "      If the change was intended, re-render a reel, check the "
          "PSNR, then --bless.\n", file=sys.stderr)
    for ln in difflib.unified_diff(want.split(";"), got.split(";"),
                                   "fixture", "emitted", lineterm=""):
        print(ln, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
