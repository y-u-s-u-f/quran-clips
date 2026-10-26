"""tests/gpu_parity.py -- tools/render_gpu's frames against their goldens.

    tools/render-venv/bin/python tests/gpu_parity.py
    tools/render-venv/bin/python tests/gpu_parity.py --bless

Every style's graph (tests/graph_parity.py pins the bars one as text) runs
on tools/render_gpu (pipeline/gpu/render_gpu.swift). This pins the PIXELS:
over the first WINDOW seconds of the graph_parity recipe (at-tawbah-128-128:
for bars the wipe card and the first crossfade, every switch on; for the
text styles its first two cards with an English line each, the second
wrapping to two lines and stroking the Allah macron) it builds

    bars       landscape and --vertical
    vertical   the native 1080x1920 canvas off a 9:16 crop
    horizontal landscape, --vertical (composed at 1080x608), and with no
               crop (the cover-scale path, at the source's own size)

renders each to raw yuv420p frames and compares every frame's md5 with
tests/golden/gpu-<case>.framemd5 -- exact, no tolerance. Every frame of
every golden is identical to ffmpeg 9.0.2's own render of the same graph.

The footage is synthesized (testsrc2 at 30000/1001 in an mp4 with B-frames,
so the edit list and the fps mapping are exercised, tagged BT.709 like the
fetched sources, so the matrix the RGB -> YUV conversion negotiates is
exercised), because the golden's source is not tracked. Snow and heat come
from render_bars' caches, baked on first use. Each golden's header carries
the hash of every media input, so a changed input (a new ffmpeg encoding
the fixture differently, a re-baked map) is reported as that, not as a
pixel drift.

The goldens are pinned on an Apple M1: Metal's float maths belongs to the
GPU it runs on, so another Mac may differ in the last bit. `--bless` is an
owner decision, as for graph_parity: render a known reel, PSNR it against
the previous render and look at stills first.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import graph_parity as GP  # noqa: E402  (also puts pipeline/ on the path)
import generate as G  # noqa: E402
import render_bars as RB  # noqa: E402
import render_text as RT  # noqa: E402
from render_common import FFMPEG, GPU_BIN  # noqa: E402

WINDOW = 9.0
ENGLISH = ["There has certainly come to you a Messenger",
           "Grievous to him is what you suffer, and by Allah he is "
           "concerned over you"]
PORTRAIT_CROP = {"x": 656, "y": 0, "w": 608, "h": 1080}


def source(tmp):
    path = os.path.join(tmp, "source.mp4")
    subprocess.run([FFMPEG, "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30000/1001",
                    "-f", "lavfi", "-i", "sine=f=440:r=48000",
                    "-t", str(WINDOW + 1), "-c:v", "libx264", "-crf", "18",
                    "-pix_fmt", "yuv420p", "-colorspace", "bt709",
                    "-color_primaries", "bt709", "-color_trc", "bt709",
                    "-color_range", "tv", "-c:a", "aac", path], check=True)
    return path


def phrases():
    return [dict(p, end=min(p["end"], WINDOW - 0.5))
            for p in GP.phrases_from_clip() if p["start"] < WINDOW - 1]


def bars_graph(src, portrait, tmp):
    ph = phrases()
    geo = RB.geometry(portrait)
    lay = RB.layout(ph, GP.X_OFFSET, 0, geo)
    rep = RB.draw_layers(lay, RB.predraw_color(GP.TINT_RGB),
                         os.path.join(tmp, "overlays"))
    sched, _ = RB.schedule(ph, WINDOW)
    on = {n: True for n in RB.SWITCHES}
    snow = RB.snow_layer(GP.TINT_RGB, geo["W"], geo["H"])
    heat = RB.heat_layers(WINDOW)
    fc, argv = RB.build_graph(
        src, WINDOW, GP.CROP, rep, sched, [c / 255.0 for c in GP.TINT_RGB],
        on, snow, RB.scrim_plate(tmp, geo), heat, portrait=portrait)
    return fc, argv, {"snow": snow, "heatx": heat[0], "heaty": heat[1]}


def text_graph(src, style, portrait, crop, x_offset, face_bottom, tmp):
    """render_text.render's graph, minus the config and the measurements."""
    cfg = dict(G.DEFAULTS, style=style, x_offset=x_offset,
               signature="TilawatQuraniyyah")
    ph = phrases()
    english = [{"text": e} for e in ENGLISH[:len(ph)]]
    geo = RT.geometry(style, portrait)
    W, H = geo["W"], geo["H"]
    lay = RT.layout(style, ph, english, cfg, face_bottom, geo)
    sub = os.path.join(tmp, "text")
    os.makedirs(sub, exist_ok=True)
    rep = RT.draw_layers(lay, W, H, os.path.join(sub, "overlays"))
    grade = RT.grade_plate(lay, W, H, cfg["vignette"], cfg["dim"], sub)
    sig = RT.signature_card(cfg["signature"],
                            RT.ENGLISH_FONTS[cfg["english_font"]]["path"],
                            W, H, sub, 0, geo["k"])
    sched = RT.schedule(ph, WINDOW, cfg["caption_transition"])
    fc, argv = RT.build_graph(src, WINDOW, crop, W, H, grade, rep, sched, sig,
                              geo["k"] != 1)
    return fc, argv, {}


CASES = [
    ("bars-portrait", lambda s, t: bars_graph(s, True, t)),
    ("bars-landscape", lambda s, t: bars_graph(s, False, t)),
    ("vertical", lambda s, t: text_graph(s, "vertical", False,
                                         PORTRAIT_CROP, 0, 0.30, t)),
    ("horizontal-landscape", lambda s, t: text_graph(
        s, "horizontal", False, GP.CROP, GP.X_OFFSET, None, t)),
    ("horizontal-portrait", lambda s, t: text_graph(
        s, "horizontal", True, GP.CROP, GP.X_OFFSET, None, t)),
    ("horizontal-cover-scaled", lambda s, t: text_graph(
        s, "horizontal", False, None, GP.X_OFFSET, None, t)),
]


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()[:16]


def render_frames(fc, argv, tmp):
    """-> (size "WxH", [md5 per frame]) of render_gpu's raw [vout]."""
    plan, raw = os.path.join(tmp, "plan.json"), os.path.join(tmp, "gpu.yuv")
    with open(plan, "w") as f:
        json.dump({"in_argv": argv, "fc": fc, "dur": WINDOW}, f)
    r = subprocess.run([GPU_BIN, plan, raw, "--raw"], check=True,
                       capture_output=True, text=True)
    head = next(ln for ln in r.stderr.splitlines() if "] yuv420p " in ln)
    size = head.split("] yuv420p ")[1].split(",")[0]
    w, h = map(int, size.split("x"))
    n = w * h * 3 // 2
    md5s = []
    with open(raw, "rb") as f:
        for b in iter(lambda: f.read(n), b""):
            md5s.append(hashlib.md5(b).hexdigest())
    os.remove(raw)
    return size, md5s


def golden(name):
    return os.path.join(GP.GOLDEN, "gpu-%s.framemd5" % name)


def read_golden(path):
    lines = open(path).read().splitlines()
    head = json.loads(lines[0].lstrip("# "))
    return head, [ln.split()[1] for ln in lines[1:]]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bless", action="store_true",
                    help="record the current frames as the goldens. An OWNER "
                         "decision: it declares the look change intended.")
    a = ap.parse_args(argv)
    if not os.access(GPU_BIN, os.X_OK):
        sys.exit("tools/render_gpu missing: run ./install.sh")
    ok = True
    with tempfile.TemporaryDirectory(prefix="gpu-parity-") as tmp:
        src = source(tmp)
        for name, build in CASES:
            fc, args, media = build(src, tmp)
            inputs = {"source": sha(src)}
            inputs.update({k: sha(p) for k, p in media.items()})
            size, md5s = render_frames(fc, args, tmp)
            path, rel = golden(name), os.path.relpath(golden(name), GP.ROOT)
            if a.bless:
                with open(path, "w") as f:
                    f.write("# " + json.dumps({"size": size, "inputs": inputs},
                                              sort_keys=True) + "\n")
                    f.writelines("%d %s\n" % kv for kv in enumerate(md5s))
                print("blessed %s (%d frames)" % (rel, len(md5s)))
                continue
            if not os.path.exists(path):
                ok = False
                print("FAIL  %s: no golden %s; check the stills, then run with "
                      "--bless" % (name, rel), file=sys.stderr)
                continue
            head, want = read_golden(path)
            moved = [k for k in head["inputs"]
                     if head["inputs"][k] != inputs.get(k)]
            bad = [i for i in range(max(len(want), len(md5s)))
                   if i >= len(want) or i >= len(md5s) or want[i] != md5s[i]]
            if not bad and size == head["size"]:
                print("OK    %s: %d frames %s, every frame identical"
                      % (name, len(md5s), size))
                continue
            ok = False
            print("FAIL  %s: %s, %d of %d frames differ (first: frame %d)"
                  % (name, size, len(bad), len(want), bad[0]
                     if bad else -1), file=sys.stderr)
            if moved:
                print("      the fixture's input(s) changed, not necessarily "
                      "the renderer: %s" % ", ".join(moved), file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
