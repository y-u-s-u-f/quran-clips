#!/usr/bin/env python3
"""Letterbox a finished 1920x1080 reel onto a 1080x1920 portrait canvas.

Black above and below and nothing else touched: the picture is scaled by the
single factor that fills the width, then centred. One x264 pass at `preset
veryfast` -- the source is already a crf-18 encode of the final pixels, so
the slow preset buys nothing a second time; audio is stream-copied.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate import FFMPEG, FFPROBE  # noqa: E402

W, H = 1080, 1920


def letterbox(path, out=None):
    """`path` -> portrait mp4, replacing `path` unless `out` is given.
    Refuses a file that is already portrait: a second pass would letterbox
    the letterbox down to a postage stamp."""
    size = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0:s=x", path],
        capture_output=True, text=True, check=True).stdout.strip()
    w, h = (int(v) for v in size.split("x"))
    if h > w:
        sys.exit("%s is already %s portrait -- nothing to letterbox"
                 % (path, size))
    dst = out or path
    tmp = "%s.portrait.mp4" % os.path.splitext(path)[0]
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-i", path,
         "-vf", "scale=%d:-2,pad=%d:%d:0:(oh-ih)/2:black" % (W, W, H),
         "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", "-c:a", "copy",
         "-movflags", "+faststart", tmp], check=True)
    os.replace(tmp, dst)
    return dst


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit("usage: letterbox.py <reel.mp4> [out.mp4]")
    print(letterbox(*sys.argv[1:]))
