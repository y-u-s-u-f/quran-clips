"""tests/input_parity.py -- the sources a reel can start from, against live ffmpeg.

    tools/render-venv/bin/python tests/input_parity.py [CASE ...]

tests/gpu_parity.py pins every style off one well-behaved source. This runs
the same graphs (its builders, its WINDOW) off the sources that took
render_gpu down a different path, and compares every frame and the colour
tags with ffmpeg 9.0.2's own render of the graph -- exact, no tolerance:

    cover      no crop, --vertical: 1280x720 and 1920x800 cover-scale to an
               odd width, so swscale's packed RGBA forces full chroma
    odd crop   a hand-written crop of odd w/h, which vf_crop rounds down
    sar        1920x1080 at 16:15: coded size, not AVFoundation's display one
    offset     a container start of 0.2s (`-output_ts_offset`), which
               ffmpeg's input takes to 0; bars ends a frame early on it
    hlg        BT.2020 HLG 10-bit HEVC, through generate.trim_media

then runs generate.run_config end to end with no `trim:` off the sources
AVFoundation cannot read (H.264 in MKV, VP9 in WebM, AV1 in mp4): each must
render, and the delivered mp4's colr box must carry the SPS's tags. One
no-`trim:` config also runs --verify-only, whose verification block must be
the full render's.

Nothing is golden here, so nothing is blessed: a failure is render_gpu
leaving ffmpeg's arithmetic.
"""
import contextlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gpu_parity as P  # noqa: E402  (also puts pipeline/ on the path)
import generate as G  # noqa: E402
from generate import FFMPEG, FFPROBE  # noqa: E402
from render_common import GPU_BIN  # noqa: E402

BT709 = ["-colorspace", "bt709", "-color_primaries", "bt709",
         "-color_trc", "bt709", "-color_range", "tv"]
# every tag set in the stream itself, so all three survive into the delivery
TAGGED = ("setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:"
          "range=tv")
X264 = ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p"]


def make(tmp, name, size, video, dur=P.WINDOW + 1, vf=None):
    path = os.path.join(tmp, name)
    if os.path.exists(path):
        return path
    subprocess.run([FFMPEG, "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=%s:r=30000/1001" % size,
                    "-f", "lavfi", "-i", "sine=f=440:r=48000", "-t", str(dur)]
                   + (["-vf", vf] if vf else []) + video
                   + ["-c:a", "libopus" if name.endswith(".webm") else "aac",
                      path], check=True)
    return path


def offset(tmp):
    src = make(tmp, "base.mp4", "1920x1080", X264 + BT709)
    path = os.path.join(tmp, "offset.mp4")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", src, "-c", "copy",
                    "-output_ts_offset", "0.2", path], check=True)
    return path


X265_HLG = (["-c:v", "libx265", "-preset", "ultrafast",
             "-x265-params", "log-level=error"],
            "format=yuv420p10le,setparams=colorspace=bt2020nc:"
            "color_primaries=bt2020:color_trc=arib-std-b67:range=tv")


def hlg(tmp):
    src = make(tmp, "hlg10.mp4", "1920x1080", X265_HLG[0], vf=X265_HLG[1])
    return G.trim_media(src, os.path.join(tmp, "hlg-cut.mp4"), 0.0, None)


ODD_CROP = {"x": 41, "y": 21, "w": 1101, "h": 619}
ODD_PORTRAIT = {"x": 40, "y": 20, "w": 405, "h": 719}


def text(style, portrait, crop, face_bottom=None):
    x = 0 if style == "vertical" else P.GP.X_OFFSET
    return lambda s, t: P.text_graph(s, style, portrait, crop, x,
                                     face_bottom, t)


# (name, source, graph)
CASES = [
    ("cover-portrait-1280x720",
     lambda t: make(t, "s720.mp4", "1280x720", X264 + BT709),
     text("horizontal", True, None)),
    ("cover-portrait-1920x800",
     lambda t: make(t, "s800.mp4", "1920x800", X264 + BT709),
     text("horizontal", True, None)),
    ("odd-crop-horizontal",
     lambda t: make(t, "s720.mp4", "1280x720", X264 + BT709),
     text("horizontal", False, ODD_CROP)),
    ("odd-crop-vertical",
     lambda t: make(t, "s720.mp4", "1280x720", X264 + BT709),
     text("vertical", False, ODD_PORTRAIT, 0.30)),
    ("sar-bars-portrait",
     lambda t: make(t, "sar.mp4", "1920x1080", X264 + BT709,
                    vf="setsar=16/15"),
     lambda s, t: P.bars_graph(s, True, t)),
    ("sar-horizontal-cover",
     lambda t: make(t, "sar.mp4", "1920x1080", X264 + BT709,
                    vf="setsar=16/15"),
     text("horizontal", False, None)),
    ("offset-bars-landscape", offset,
     lambda s, t: P.bars_graph(s, False, t)),
    ("offset-horizontal", offset,
     text("horizontal", False, P.GP.CROP)),
    ("hlg-horizontal", hlg, text("horizontal", False, P.GP.CROP)),
    ("hlg-bars-portrait", hlg, lambda s, t: P.bars_graph(s, True, t)),
]

# (name, file, encoder argv, -vf): sources only ffmpeg decodes here, and a
# 10-bit HDR one render_gpu refuses uncut
E2E = [
    ("h264-mkv", "src.mkv", X264, TAGGED),
    ("vp9-webm", "src.webm", ["-c:v", "libvpx-vp9", "-deadline", "realtime",
                              "-pix_fmt", "yuv420p"], TAGGED),
    ("av1-mp4", "src-av1.mp4", ["-c:v", "libsvtav1", "-preset", "12",
                                "-pix_fmt", "yuv420p"], TAGGED),
    ("hevc-hlg-10bit", "src-hlg.mp4") + X265_HLG,
]
E2E_DUR = 2.0


def colr(path):
    """The mp4's nclx colr box as (primaries, transfer, matrix, full range)."""
    data = open(path, "rb").read()
    i = data.find(b"colrnclx")
    if i < 0:
        return None
    p, t, m, f = struct.unpack(">HHHB", data[i + 8:i + 15])
    return p, t, m, f >> 7


def sps_tags(path, tmp):
    """The same four read from the H.264 SPS alone, with no container."""
    es = os.path.join(tmp, "es.h264")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", path, "-map", "0:v",
                    "-c", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "h264",
                    es], check=True)
    s = json.loads(subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=color_range,color_space,color_primaries,color_transfer",
         "-of", "json", es], check=True, capture_output=True,
        text=True).stdout)["streams"][0]
    c = P.TAG_CODES
    return (c["color_primaries"][s.get("color_primaries", "unknown")],
            c["color_trc"][s.get("color_transfer", "unknown")],
            c["colorspace"][s.get("color_space", "unknown")],
            c["color_range"][s.get("color_range", "unknown")])


def frames(path):
    return int(subprocess.run(
        [FFPROBE, "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path],
        check=True, capture_output=True, text=True).stdout.strip())


def run(path, tmp, **kw):
    """generate.run_config's stdout, its scratch under `tmp`."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        G.run_config(path, **kw)
    return out.getvalue()


def block(log):
    """The verification block, from the captions it read the window into to
    the last reference translation."""
    lines = log.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.startswith("[5/6]"))
    j = max(j for j, ln in enumerate(lines) if re.match(r" +\d+:\d+ \[", ln))
    return lines[i:j + 1]


def e2e(tmp):
    ok = True
    words = G.spoken_words(G.fetch_verses(112, 1, 1))
    whisper = os.path.join(tmp, "whisper.json")
    with open(whisper, "w") as f:
        json.dump({"words": [{"w": w, "t0": 0.1 + i * 0.15,
                              "t1": 0.2 + i * 0.15, "p": 1.0}
                             for i, w in enumerate(words)],
                   "backend": "test", "model": "test"}, f)
    resolve = G.resolve_paths

    def paths(cfg, config_path, output_override=None):
        cfg = resolve(cfg, config_path, output_override)
        cfg["tmp_dir"] = os.path.join(tmp, "run-" + os.path.basename(
            config_path))
        return cfg
    G.resolve_paths = paths
    try:
        for name, file, video, vf in E2E:
            src = make(tmp, file, "1280x720", video, dur=E2E_DUR, vf=vf)
            cfg = os.path.join(tmp, name + ".yaml")
            out = os.path.join(tmp, name + "-out.mp4")
            with open(cfg, "w") as f:
                json.dump({"input": src, "whisper": whisper,
                           "style": "horizontal", "surah": 112,
                           "ayah_start": 1, "ayah_end": 1, "signature": "",
                           "groups": [{"n_words": len(words),
                                       "english": "Say, He is Allah, One"}],
                           "output": out}, f)
            try:
                log = run(cfg, tmp)
                proxy = (run(cfg, tmp, verify_only=True)
                         if name == "h264-mkv" else None)
            except (Exception, SystemExit) as e:
                ok = False
                print("FAIL  e2e %s: %r" % (name, e), file=sys.stderr)
                continue
            n, box, sps = frames(out), colr(out), sps_tags(out, tmp)
            want = round(E2E_DUR * 30)
            bad = []
            if n != want:
                bad.append("%d frames, want %d" % (n, want))
            # every source is fully tagged, and movenc writes colr whenever
            # all three are specified
            if box != sps:
                bad.append("colr %s, SPS %s" % (box, sps))
            if proxy is not None and block(proxy) != block(log):
                bad.append("--verify-only's verification block is not the "
                           "render's")
            if bad:
                ok = False
                print("FAIL  e2e %s: %s" % (name, "; ".join(bad)),
                      file=sys.stderr)
            else:
                print("OK    e2e %s: %d frames, colr %s = SPS%s"
                      % (name, n, box, ", --verify-only block identical"
                         if proxy is not None else ""))
    finally:
        G.resolve_paths = resolve
    return ok


def main(argv=None):
    only = set(sys.argv[1:] if argv is None else argv)
    known = {c[0] for c in CASES} | {"e2e"}
    if only - known:
        sys.exit("unknown case(s) %s (cases: %s)"
                 % (", ".join(sorted(only - known)), ", ".join(sorted(known))))
    if not os.access(GPU_BIN, os.X_OK):
        sys.exit("tools/render_gpu missing: run ./install.sh")
    ok = True
    with tempfile.TemporaryDirectory(prefix="input-parity-") as tmp:
        G.TRIM_CACHE = os.path.join(tmp, "trim")
        for name, fixture, build in CASES:
            if only and name not in only:
                continue
            src = fixture(tmp)
            fc, args, _ = build(src, tmp)
            try:
                size, md5s, tags = P.render_frames(fc, args, tmp)
            except subprocess.CalledProcessError as e:
                ok = False
                print("FAIL  %s: render_gpu: %s" % (name, e.stderr.strip()),
                      file=sys.stderr)
                continue
            want, want_tags = P.ffmpeg_frames(fc, args, size, tmp)
            bad = [i for i in range(max(len(want), len(md5s)))
                   if i >= len(want) or i >= len(md5s) or want[i] != md5s[i]]
            if not bad and tags == want_tags:
                print("OK    %s: %d frames %s, every frame identical, tags %s"
                      % (name, len(md5s), size, tags))
                continue
            ok = False
            print("FAIL  %s: %s, %d vs ffmpeg's %d frames, %d differ (first: "
                  "%d); tags %s, ffmpeg's %s"
                  % (name, size, len(md5s), len(want), len(bad),
                     bad[0] if bad else -1, tags, want_tags), file=sys.stderr)
        if not only or "e2e" in only:
            ok = e2e(tmp) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
