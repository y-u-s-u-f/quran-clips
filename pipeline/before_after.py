#!/usr/bin/env python3
"""A finished reel beside its source: reels/<reel>-before-after.mp4.

    tools/render-venv/bin/python pipeline/before_after.py sources/<id>/<reel>.yaml

1080x1920 white canvas, "ORIGINAL" (the source over the config's trim window)
above "EDITED" (the finished reel's picture, never re-rendered), each 960x540;
the reel's audio is stream-copied. Takes a landscape reel or a `--vertical`
one (its 1080x608 band is cropped back out of the padding); native `vertical`
has no 16:9 picture to place and is refused.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate import FFMPEG, FFPROBE, load_config, resolve_paths  # noqa: E402

W, H = 1080, 1920
PANEL = (60, 390), (60, 1070)          # top-left of each 960x540 panel
LABEL_Y = 330, 1012                    # label centres, 60px above each panel
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def labels(path):
    # ffmpeg here has no drawtext, so the labels are one still under the panels.
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT, 64)
    for text, y in zip(("ORIGINAL", "EDITED"), LABEL_Y):
        d.text((W // 2, y), text, font=f, fill=(0, 0, 0), anchor="mm")
    im.save(path)


def before_after(config):
    cfg = resolve_paths(load_config(config), config)
    if cfg["style"] == "vertical":
        sys.exit("native `vertical` has no 16:9 picture for the EDITED panel")
    reel = cfg["output"]
    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "csv=p=0:s=x", reel],
        capture_output=True, text=True, check=True).stdout.split()
    w, h = (int(v) for v in probe[0].split("x"))
    dur = probe[1]
    # A --vertical reel is the 1080x608 picture padded to 1080x1920
    # (render_common.PORTRAIT_PAD); a landscape one is all picture.
    band = "crop=iw:608," if h > w else ""
    out = os.path.splitext(reel)[0] + "-before-after.mp4"
    start = (cfg["trim"] or [0])[0]
    with tempfile.TemporaryDirectory() as tmp:
        bg = os.path.join(tmp, "bg.png")
        labels(bg)
        (ox, oy), (ex, ey) = PANEL
        subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-loop", "1", "-i", bg,
             "-ss", str(start), "-t", dur, "-i", cfg["input"], "-i", reel,
             "-filter_complex",
             "[1:v]fps=30,scale=960:540,setsar=1[o];"
             "[2:v]%sscale=960:540,setsar=1[e];"
             "[0:v][o]overlay=%d:%d[a];[a][e]overlay=%d:%d,format=yuv420p[v]"
             % (band, ox, oy, ex, ey),
             "-map", "[v]", "-map", "2:a", "-t", dur, "-r", "30",
             "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
             "-c:a", "copy", "-movflags", "+faststart", out], check=True)
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: before_after.py <config.yaml>")
    print(before_after(sys.argv[1]))
