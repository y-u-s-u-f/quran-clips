"""tests/gpu_parity.py -- tools/bars_gpu against the ffmpeg graph it ports.

    tools/render-venv/bin/python tests/gpu_parity.py

The bars style renders on the GPU (pipeline/gpu/bars_gpu.swift) and keeps
the ffmpeg filtergraph as its definition: tests/graph_parity.py pins the
graph, this pins the GPU to the graph. It builds the graph_parity recipe
(at-tawbah-128-128, every switch on) over the first WINDOW seconds -- the
wipe card and the first crossfade -- landscape and portrait, runs it through
both renderers to lossless frames and asserts PSNR-Y >= FLOOR on each.

The footage is synthesized (testsrc2 at 30000/1001 in an mp4 with B-frames,
so the edit list and the fps mapping are exercised), because the golden's
source is not tracked. Snow and heat come from render_bars' caches, baked on
first use.

GPU output is pinned per machine, not per byte across machines: Metal's
float maths is the GPU's. Every reel measured so far is bit-identical to
ffmpeg on an M1; a new machine is checked by running this.
"""
import hashlib
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import graph_parity as GP  # noqa: E402  (also puts pipeline/ on the path)
import render_bars as RB  # noqa: E402
from render_common import FFMPEG  # noqa: E402

WINDOW = 9.0
FLOOR = 45.0


def source(tmp):
    path = os.path.join(tmp, "source.mp4")
    subprocess.run([FFMPEG, "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30000/1001",
                    "-f", "lavfi", "-i", "sine=f=440:r=48000",
                    "-t", str(WINDOW + 1), "-c:v", "libx264", "-crf", "18",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", path], check=True)
    return path


def graph(src, portrait, tmp):
    phrases = [dict(p, end=min(p["end"], WINDOW - 0.5))
               for p in GP.phrases_from_clip() if p["start"] < WINDOW - 1]
    geo = RB.geometry(portrait)
    lay = RB.layout(phrases, GP.X_OFFSET, 0, geo)
    rep = RB.draw_layers(lay, RB.predraw_color(GP.TINT_RGB),
                         os.path.join(tmp, "overlays"))
    sched, _ = RB.schedule(phrases, WINDOW)
    on = {n: True for n in RB.SWITCHES}
    return RB.build_graph(
        src, WINDOW, GP.CROP, rep, sched, [c / 255.0 for c in GP.TINT_RGB],
        on, RB.snow_layer(GP.TINT_RGB, geo["W"], geo["H"]),
        RB.scrim_plate(tmp, geo), GP.LOUDNORM, RB.audio_fades(WINDOW),
        RB.heat_layers(WINDOW), portrait=portrait)


def check(src, portrait, tmp):
    name = "portrait" if portrait else "landscape"
    fc, argv = graph(src, portrait, tmp)
    ff, gpu = os.path.join(tmp, "ff.yuv"), os.path.join(tmp, "gpu.yuv")
    video = ";".join(c for c in fc.split(";") if not c.startswith("[0:a]"))
    subprocess.run([FFMPEG, "-v", "error", "-y"] + argv +
                   ["-filter_complex", video, "-map", "[vout]",
                    "-t", "%.3f" % WINDOW, "-r", "30",
                    "-f", "rawvideo", "-pix_fmt", "yuv420p", ff], check=True)
    plan = os.path.join(tmp, "plan.json")
    with open(plan, "w") as f:
        RB.json.dump({"in_argv": argv, "fc": fc, "dur": WINDOW}, f)
    subprocess.run([RB.GPU_BIN, plan, gpu, "--raw"], check=True,
                   stderr=subprocess.DEVNULL)
    size = "1080x1920" if portrait else "1920x1080"
    same = [hashlib.sha256(open(p, "rb").read()).digest() for p in (ff, gpu)]
    r = subprocess.run([FFMPEG, "-hide_banner", "-f", "rawvideo", "-pix_fmt",
                        "yuv420p", "-s", size, "-i", ff, "-f", "rawvideo",
                        "-pix_fmt", "yuv420p", "-s", size, "-i", gpu,
                        "-lavfi", "psnr", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"PSNR y:(\S+) .*min:(\S+)", r.stderr)
    frames = os.path.getsize(ff) // (3 * 1080 * 1920 // 2)
    if os.path.getsize(ff) != os.path.getsize(gpu) or not m:
        print("FAIL  %s: ffmpeg %d bytes, gpu %d bytes"
              % (name, os.path.getsize(ff), os.path.getsize(gpu)))
        return False
    y, worst = float(m.group(1)), float(m.group(2))
    ok = y >= FLOOR and worst >= FLOOR
    print("%s  %s: %d frames, %s (PSNR-Y %s, worst frame %s; floor %.0f)"
          % ("OK " if ok else "FAIL", name, frames,
             "bit-identical" if same[0] == same[1] else "differs",
             m.group(1), m.group(2), FLOOR))
    return ok


def main():
    if not os.access(RB.GPU_BIN, os.X_OK):
        sys.exit("tools/bars_gpu missing: run ./install.sh")
    with tempfile.TemporaryDirectory(prefix="gpu-parity-") as tmp:
        src = source(tmp)
        ok = [check(src, portrait, tmp) for portrait in (True, False)]
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
