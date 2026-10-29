"""tests/gpu_parity.py -- tools/render_gpu's frames against their goldens.

    tools/render-venv/bin/python tests/gpu_parity.py
    tools/render-venv/bin/python tests/gpu_parity.py --ffmpeg
    tools/render-venv/bin/python tests/gpu_parity.py --bless [CASE ...]

Every style's graph (tests/graph_parity.py pins the bars one as text) runs
on tools/render_gpu (pipeline/gpu/render_gpu.swift). This pins the PIXELS:
over the first WINDOW seconds of the graph_parity recipe (at-tawbah-128-128:
for bars the wipe card and the first crossfade, every switch on; for the
text styles its first two cards with an English line each, the second
wrapping to two lines and stroking the Allah macron) it builds

    bars       landscape and --vertical, each again with heat off
    vertical   the native 1080x1920 canvas off a 9:16 crop
    horizontal landscape, --vertical (composed at 1080x608), and with no
               crop (the cover-scale path) both ways: at the source's own
               size, and --vertical, whose 1081-wide scale forces swscale's
               full-chroma RGBA

and three of them again off a BT.601 or a full-range source (CASES),
renders each to raw yuv420p frames and compares every frame's md5 with
tests/golden/gpu-<case>.framemd5 -- exact, no tolerance -- and the colour
tags render_gpu reports with the header's. Every frame of every golden is
identical to ffmpeg 9.0.2's own render of the same graph, and the tags to
its last frame's: `--ffmpeg` checks exactly that, against a live render.

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


# the footage's tags: the fetched sources' BT.709, then the tags a source can
# arrive with that change the decode (full range) or the matrix (BT.601)
SOURCES = {
    "bt709": ["-colorspace", "bt709", "-color_primaries", "bt709",
              "-color_trc", "bt709", "-color_range", "tv"],
    "bt601": ["-vf", "setparams=colorspace=smpte170m:color_primaries="
              "smpte170m:color_trc=smpte170m:range=tv"],
    "pc": ["-vf", "scale=out_range=pc,setparams=colorspace=bt709:"
           "color_primaries=bt709:color_trc=bt709:range=pc"],
}


def source(tmp, tags="bt709"):
    path = os.path.join(tmp, "source-%s.mp4" % tags)
    if not os.path.exists(path):
        subprocess.run([FFMPEG, "-v", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30000/1001",
                        "-f", "lavfi", "-i", "sine=f=440:r=48000",
                        "-t", str(WINDOW + 1), "-c:v", "libx264", "-crf", "18",
                        "-pix_fmt", "yuv420p"] + SOURCES[tags]
                       + ["-c:a", "aac", path], check=True)
    return path


def phrases():
    return [dict(p, end=min(p["end"], WINDOW - 0.5))
            for p in GP.phrases_from_clip() if p["start"] < WINDOW - 1]


def bars_graph(src, portrait, tmp, heat=True):
    ph = phrases()
    geo = RB.geometry(portrait)
    lay = RB.layout(ph, GP.X_OFFSET, 0, geo)
    rep = RB.draw_layers(lay, RB.predraw_color(GP.TINT_RGB),
                         os.path.join(tmp, "overlays"))
    sched, _ = RB.schedule(ph, WINDOW)
    on = RB.switches({"heat": heat})
    snow = RB.snow_layer(GP.TINT_RGB, geo["W"], geo["H"])
    maps = RB.heat_layers(WINDOW) if heat else None
    fc, argv = RB.build_graph(
        src, WINDOW, GP.CROP, rep, sched, [c / 255.0 for c in GP.TINT_RGB],
        on, snow, RB.scrim_plate(tmp, geo), maps, portrait=portrait)
    media = {"snow": snow}
    if heat:
        media.update(heatx=maps[0], heaty=maps[1])
    return fc, argv, media


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


# (name, source tags, graph)
CASES = [
    ("bars-portrait", "bt709", lambda s, t: bars_graph(s, True, t)),
    ("bars-landscape", "bt709", lambda s, t: bars_graph(s, False, t)),
    ("vertical", "bt709", lambda s, t: text_graph(s, "vertical", False,
                                                  PORTRAIT_CROP, 0, 0.30, t)),
    ("horizontal-landscape", "bt709", lambda s, t: text_graph(
        s, "horizontal", False, GP.CROP, GP.X_OFFSET, None, t)),
    ("horizontal-portrait", "bt709", lambda s, t: text_graph(
        s, "horizontal", True, GP.CROP, GP.X_OFFSET, None, t)),
    ("horizontal-cover-scaled", "bt709", lambda s, t: text_graph(
        s, "horizontal", False, None, GP.X_OFFSET, None, t)),
    ("horizontal-portrait-cover-scaled", "bt709", lambda s, t: text_graph(
        s, "horizontal", True, None, GP.X_OFFSET, None, t)),
    # with heat off the band ends in RGB: fade and pad run on gbrp, and the
    # source's BT.709 survives to the RGB -> YUV conversion
    ("bars-portrait-no-heat", "bt709",
     lambda s, t: bars_graph(s, True, t, heat=False)),
    ("bars-landscape-no-heat", "bt709",
     lambda s, t: bars_graph(s, False, t, heat=False)),
    ("horizontal-landscape-bt601", "bt601", lambda s, t: text_graph(
        s, "horizontal", False, GP.CROP, GP.X_OFFSET, None, t)),
    # a full-range source decodes as such, and the RGB -> YUV conversion
    # and pad's black land on its range
    ("vertical-pc", "pc", lambda s, t: text_graph(s, "vertical", False,
                                                  PORTRAIT_CROP, 0, 0.30, t)),
    ("bars-portrait-pc", "pc", lambda s, t: bars_graph(s, True, t)),
]

# showinfo's names for the code points the cases deliver
TAG_CODES = {"color_range": {"tv": 0, "unknown": 0, "pc": 1},
             "colorspace": {"bt709": 1, "unknown": 2, "smpte170m": 6,
                            "bt2020nc": 9},
             "color_primaries": {"bt709": 1, "unknown": 2, "smpte170m": 6,
                                 "bt2020": 9},
             "color_trc": {"bt709": 1, "unknown": 2, "smpte170m": 6,
                           "smpte2084": 16, "arib-std-b67": 18}}


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()[:16]


def frame_md5s(raw, size):
    w, h = map(int, size.split("x"))
    n = w * h * 3 // 2
    md5s = []
    with open(raw, "rb") as f:
        for b in iter(lambda: f.read(n), b""):
            md5s.append(hashlib.md5(b).hexdigest())
    os.remove(raw)
    return md5s


def render_frames(fc, argv, tmp):
    """-> (size "WxH", [md5 per frame], colour tags) of render_gpu's raw
    [vout]."""
    plan, raw = os.path.join(tmp, "plan.json"), os.path.join(tmp, "gpu.yuv")
    with open(plan, "w") as f:
        json.dump({"in_argv": argv, "fc": fc, "dur": WINDOW}, f)
    r = subprocess.run([GPU_BIN, plan, raw, "--raw"], check=True,
                       capture_output=True, text=True)
    head = next(ln for ln in r.stderr.splitlines() if "] yuv420p " in ln)
    size = head.split("] yuv420p ")[1].split(",")[0]
    tags = json.loads(r.stdout)
    tags.pop("sar")
    return size, frame_md5s(raw, size), tags


def ffmpeg_frames(fc, argv, size, tmp):
    """-> ([md5 per frame], colour tags) of ffmpeg's own render of [vout]:
    the frames the delivery encode reads, its last frame's tags."""
    raw = os.path.join(tmp, "ffmpeg.yuv")
    r = subprocess.run([FFMPEG, "-v", "info", "-y"] + argv + [
        "-filter_complex", fc + ";[vout]showinfo[shown]", "-map", "[shown]",
        "-t", "%.3f" % WINDOW, "-f", "rawvideo", raw],
        check=True, capture_output=True, text=True)
    last = [ln for ln in r.stderr.splitlines() if "color_range:" in ln][-1]
    names = dict(kv.split(":") for kv in last.split("] ")[1].split())
    tags = {k: TAG_CODES[k][names[n]] for k, n in
            [("color_range", "color_range"), ("colorspace", "color_space"),
             ("color_primaries", "color_primaries"), ("color_trc", "color_trc")]}
    return frame_md5s(raw, size), tags


def golden(name):
    return os.path.join(GP.GOLDEN, "gpu-%s.framemd5" % name)


def read_golden(path):
    lines = open(path).read().splitlines()
    head = json.loads(lines[0].lstrip("# "))
    return head, [ln.split()[1] for ln in lines[1:]]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bless", nargs="*", metavar="CASE",
                    help="record the current frames (of CASE..., default "
                         "every case) as the goldens. An OWNER decision: it "
                         "declares the look change intended.")
    ap.add_argument("--ffmpeg", action="store_true",
                    help="compare against ffmpeg's own render of each graph "
                         "instead of the goldens (minutes): how a golden is "
                         "checked before --bless")
    a = ap.parse_args(argv)
    names = [c[0] for c in CASES]
    for n in a.bless or []:
        if n not in names:
            sys.exit("unknown case %s (cases: %s)" % (n, ", ".join(names)))
    if not os.access(GPU_BIN, os.X_OK):
        sys.exit("tools/render_gpu missing: run ./install.sh")
    ok = True
    with tempfile.TemporaryDirectory(prefix="gpu-parity-") as tmp:
        for name, tags_key, build in CASES:
            src = source(tmp, tags_key)
            fc, args, media = build(src, tmp)
            inputs = {"source": sha(src)}
            inputs.update({k: sha(p) for k, p in media.items()})
            size, md5s, tags = render_frames(fc, args, tmp)
            path, rel = golden(name), os.path.relpath(golden(name), GP.ROOT)
            if a.ffmpeg:
                want, want_tags = ffmpeg_frames(fc, args, size, tmp)
                head = {"size": size, "inputs": inputs, "tags": want_tags}
                rel = "ffmpeg's render"
            elif a.bless is not None and (not a.bless or name in a.bless):
                with open(path, "w") as f:
                    f.write("# " + json.dumps({"size": size, "inputs": inputs,
                                               "tags": tags},
                                              sort_keys=True) + "\n")
                    f.writelines("%d %s\n" % kv for kv in enumerate(md5s))
                print("blessed %s (%d frames)" % (rel, len(md5s)))
                continue
            elif not os.path.exists(path):
                ok = False
                print("FAIL  %s: no golden %s; check the stills, then run with "
                      "--bless" % (name, rel), file=sys.stderr)
                continue
            else:
                head, want = read_golden(path)
            moved = [k for k in head["inputs"]
                     if head["inputs"][k] != inputs.get(k)]
            bad = [i for i in range(max(len(want), len(md5s)))
                   if i >= len(want) or i >= len(md5s) or want[i] != md5s[i]]
            # a golden whose header has no tags pins the frames only
            want_tags = head.get("tags", tags)
            if not bad and size == head["size"] and tags == want_tags:
                print("OK    %s: %d frames %s, every frame identical"
                      % (name, len(md5s), size))
                continue
            ok = False
            print("FAIL  %s: %s, %d of %d frames differ (first: frame %d) "
                  "against %s" % (name, size, len(bad), len(want), bad[0]
                                  if bad else -1, rel), file=sys.stderr)
            if tags != want_tags:
                print("      colour tags %s, want %s" % (tags, want_tags),
                      file=sys.stderr)
            if moved:
                print("      the fixture's input(s) changed, not necessarily "
                      "the renderer: %s" % ", ".join(moved), file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
