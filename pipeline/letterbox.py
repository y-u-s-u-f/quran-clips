#!/usr/bin/env python3
"""Letterbox a finished 1920x1080 reel onto a 1080x1920 portrait canvas.

Black above and below and nothing else touched: the picture is scaled by the
single factor that fills the width, then centred. One x264 pass at `preset
veryfast` -- the source is already a crf-18 encode of the final pixels, so
the slow preset buys nothing a second time; audio is stream-copied.
"""
import os
import subprocess

W, H = 1080, 1920


def letterbox(path, out=None):
    """`path` -> portrait mp4, replacing `path` unless `out` is given."""
    dst = out or path
    tmp = "%s.portrait.mp4" % os.path.splitext(path)[0]
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", path,
         "-vf", "scale=%d:-2,pad=%d:%d:0:(oh-ih)/2:black" % (W, W, H),
         "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", "-c:a", "copy",
         "-movflags", "+faststart", tmp], check=True)
    os.replace(tmp, dst)
    return dst


if __name__ == "__main__":
    import sys
    if len(sys.argv) not in (2, 3):
        sys.exit("usage: letterbox.py <reel.mp4> [out.mp4]")
    print(letterbox(*sys.argv[1:]))
