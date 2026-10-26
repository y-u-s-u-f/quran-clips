"""tests/gpu_parity.py -- tools/render_gpu against the ffmpeg graphs it ports.

    tools/render-venv/bin/python tests/gpu_parity.py

Every style renders on the GPU (pipeline/gpu/render_gpu.swift) and keeps
its ffmpeg filtergraph as the definition: tests/graph_parity.py pins the bars
graph, this pins the GPU to each style's graph. Over the first WINDOW seconds
of the graph_parity recipe (at-tawbah-128-128: for bars the wipe card and
the first crossfade, every switch on; for the text styles its first two
cards with an English line each, the second wrapping to two lines and
stroking the Allah macron) it builds

    bars       landscape and --vertical
    vertical   the native 1080x1920 canvas off a 9:16 crop
    horizontal landscape, --vertical (composed at 1080x608), and with no
               crop (the cover-scale path, at the source's own size)

runs each through both renderers to lossless frames and asserts PSNR-Y >=
FLOOR on each.

The footage is synthesized (testsrc2 at 30000/1001 in an mp4 with B-frames,
so the edit list and the fps mapping are exercised, tagged BT.709 like the
fetched sources, so the matrix the RGB -> YUV conversion negotiates is
exercised), because the golden's source is not tracked. Snow and heat come
from render_bars' caches, baked on first use.

GPU output is pinned per machine, not per byte across machines: Metal's
float maths is the GPU's. Every reel measured so far is bit-identical to
ffmpeg on an M1; a new machine is checked by running this.
"""
import hashlib
import json
import os
import re
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
FLOOR = 45.0
ENGLISH = ["There has certainly come to you a Messenger",
           "Grievous to him is what you suffer, and by Allah he is "
           "concerned over you"]


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
    return RB.build_graph(
        src, WINDOW, GP.CROP, rep, sched, [c / 255.0 for c in GP.TINT_RGB],
        on, RB.snow_layer(GP.TINT_RGB, geo["W"], geo["H"]),
        RB.scrim_plate(tmp, geo), GP.LOUDNORM, RB.audio_fades(WINDOW),
        RB.heat_layers(WINDOW), portrait=portrait)


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
    return RT.build_graph(src, WINDOW, crop, W, H, grade, rep, sched,
                          GP.LOUDNORM, RT.audio_fades(WINDOW), sig,
                          geo["k"] != 1)


def check(name, fc, argv, tmp):
    ff, gpu = os.path.join(tmp, "ff.yuv"), os.path.join(tmp, "gpu.yuv")
    video = ";".join(c for c in fc.split(";") if not c.startswith("[0:a]"))
    subprocess.run([FFMPEG, "-v", "error", "-y"] + argv +
                   ["-filter_complex", video, "-map", "[vout]",
                    "-t", "%.3f" % WINDOW, "-r", "30",
                    "-f", "rawvideo", "-pix_fmt", "yuv420p", ff], check=True)
    plan = os.path.join(tmp, "plan.json")
    with open(plan, "w") as f:
        json.dump({"in_argv": argv, "fc": fc, "dur": WINDOW}, f)
    r = subprocess.run([GPU_BIN, plan, gpu, "--raw"], check=True,
                       capture_output=True, text=True)
    size = re.search(r"\] yuv420p (\d+x\d+),", r.stderr).group(1)
    same = [hashlib.sha256(open(p, "rb").read()).digest() for p in (ff, gpu)]
    r = subprocess.run([FFMPEG, "-hide_banner", "-f", "rawvideo", "-pix_fmt",
                        "yuv420p", "-s", size, "-i", ff, "-f", "rawvideo",
                        "-pix_fmt", "yuv420p", "-s", size, "-i", gpu,
                        "-lavfi", "psnr", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"PSNR y:(\S+) .*min:(\S+)", r.stderr)
    w, h = map(int, size.split("x"))
    frames = os.path.getsize(ff) // (w * h * 3 // 2)
    if os.path.getsize(ff) != os.path.getsize(gpu) or not m:
        print("FAIL  %s: ffmpeg %d bytes, gpu %d bytes"
              % (name, os.path.getsize(ff), os.path.getsize(gpu)))
        return False
    y, worst = float(m.group(1)), float(m.group(2))
    ok = y >= FLOOR and worst >= FLOOR
    print("%s  %s: %d frames %s, %s (PSNR-Y %s, worst frame %s; floor %.0f)"
          % ("OK " if ok else "FAIL", name, frames, size,
             "bit-identical" if same[0] == same[1] else "differs",
             m.group(1), m.group(2), FLOOR))
    return ok


def main():
    if not os.access(GPU_BIN, os.X_OK):
        sys.exit("tools/render_gpu missing: run ./install.sh")
    portrait_crop = {"x": 656, "y": 0, "w": 608, "h": 1080}
    cases = [
        ("bars portrait", lambda s, t: bars_graph(s, True, t)),
        ("bars landscape", lambda s, t: bars_graph(s, False, t)),
        ("vertical", lambda s, t: text_graph(s, "vertical", False,
                                             portrait_crop, 0, 0.30, t)),
        ("horizontal landscape", lambda s, t: text_graph(
            s, "horizontal", False, GP.CROP, GP.X_OFFSET, None, t)),
        ("horizontal portrait", lambda s, t: text_graph(
            s, "horizontal", True, GP.CROP, GP.X_OFFSET, None, t)),
        ("horizontal cover-scaled", lambda s, t: text_graph(
            s, "horizontal", False, None, GP.X_OFFSET, None, t)),
    ]
    with tempfile.TemporaryDirectory(prefix="gpu-parity-") as tmp:
        src = source(tmp)
        ok = [check(name, *build(src, tmp), tmp) for name, build in cases]
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
