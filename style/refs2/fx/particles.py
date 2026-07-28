#!/usr/bin/env python3
"""
particles.py -- SUPERSEDED, KEPT ONLY AS A RECORD OF THE WRONG MODEL.

This file assumed Node Video's "Snow" was a sprite/particle system of soft gold
bokeh discs on closed orbits. It is not. Snow is a single procedural full-screen
pass -- two magnified copies of a white-noise texture, scrolled at different
rates, multiplied, and raised to a high power. The shipped GLSL was recovered
from the app and is reproduced (in pure Pillow) by render_snow() in
scripts/render_bars.py. See style/refs2/SNOW_SPEC.md for the shader, the
forensics on both reference reels, and a point-by-point list of what the model
below got wrong (fat soft blobs, gold tint, orbiting-in-place, gentle flicker).
Do not use this for new renders.

ORIGINAL DOCSTRING FOLLOWS.

particles.py -- "SNOW" (floating gold bokeh/dust) layer generator.

Renders a seamlessly-looping video of soft, sparse, drifting, flickering
specks on pure black, meant to be composited with `blend=all_mode=screen`
(or lighten) over the graded footage.

Node Video "Snow" parameter mapping:
    Amount 0.70    -> --amount   (particle count = amount * density * area)
    Size   0.10    -> --size     (mean radius in px = size * 22)
    Direction 0x0  -> --dirx/--diry (px drifted per loop; 0 = pure brownian-ish drift)
    Intensity 1.00 -> --intensity (peak brightness multiplier)
    Speed  -1.00   -> --speed    (sign flips drift direction; magnitude scales it)
    Flicker 1.00   -> --flicker  (0 = steady, 1 = full sinusoidal twinkle)
    Color          -> --color    (#RRGGBB)

Everything is periodic in `--duration` seconds, so the output can be
-stream_loop'ed forever with no visible seam.

Usage:
  python particles.py --w 720 --h 405 --fps 30 --duration 6 \
      --color '#C9A227' --out particles.mp4
"""
import argparse
import math
import subprocess
import sys

import numpy as np


def parse_hex(s):
    s = s.lstrip("#")
    return np.array([int(s[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float64) / 255.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--w", type=int, default=720)
    ap.add_argument("--h", type=int, default=405)
    ap.add_argument("--fps", type=float, default=30)
    ap.add_argument("--duration", type=float, default=6.0)
    ap.add_argument("--color", default="#C9A227")
    ap.add_argument("--amount", type=float, default=0.70)
    ap.add_argument("--size", type=float, default=0.10)
    ap.add_argument("--intensity", type=float, default=1.00)
    ap.add_argument("--speed", type=float, default=-1.00)
    ap.add_argument("--flicker", type=float, default=1.00)
    ap.add_argument("--dirx", type=float, default=0.0)
    ap.add_argument("--diry", type=float, default=0.0)
    ap.add_argument("--density", type=float, default=110.0,
                    help="particles per megapixel at amount=1.0")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="particles.mp4")
    a = ap.parse_args()

    rng = np.random.default_rng(a.seed)
    W, H, FPS = a.w, a.h, a.fps
    nframes = int(round(a.duration * FPS))
    col = parse_hex(a.color)

    n = max(1, int(a.amount * a.density * (W * H) / 1e6))

    # --- per-particle constants -------------------------------------------
    px0 = rng.uniform(0, W, n)
    py0 = rng.uniform(0, H, n)
    # radius: lognormal-ish spread around size*22 px
    rad = np.clip(a.size * 22.0 * np.exp(rng.normal(0, 0.45, n)), 0.6, 40.0)
    # brightness: small specks bright, big ones dim (bokeh look)
    amp = a.intensity * rng.uniform(0.35, 1.0, n) * (1.4 / (1.0 + rad * 0.35))
    amp = np.clip(amp, 0, 1.2)
    # each particle drifts along its own slow closed ellipse (period = loop)
    orb_r = rng.uniform(4, 26, n) * abs(a.speed)
    orb_ph = rng.uniform(0, 2 * math.pi, n)
    orb_asp = rng.uniform(0.3, 1.0, n)
    # flicker: 1-3 cycles per loop so it stays periodic
    fl_k = rng.integers(1, 4, n).astype(float)
    fl_ph = rng.uniform(0, 2 * math.pi, n)
    # global drift (direction) in px over one loop
    dx_tot, dy_tot = a.dirx * np.sign(a.speed or 1), a.diry * np.sign(a.speed or 1)

    sign = 1.0 if a.speed >= 0 else -1.0

    # --- render ------------------------------------------------------------
    # libx264 needs even dimensions; pad on the way out and crop back.
    ff = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-",
         "-vf", f"pad=ceil(iw/2)*2:ceil(ih/2)*2,crop={W - W % 2}:{H - H % 2}:0:0",
         "-an", "-c:v", "libx264", "-crf", "16", "-preset", "medium",
         "-pix_fmt", "yuv420p", a.out],
        stdin=subprocess.PIPE)

    yy = np.arange(H)[:, None]
    xx = np.arange(W)[None, :]

    for f in range(nframes):
        t = f / nframes                      # 0..1 over the loop
        ang = 2 * math.pi * t * sign
        cx = px0 + orb_r * np.cos(orb_ph + ang) + dx_tot * t
        cy = py0 + orb_r * orb_asp * np.sin(orb_ph + ang) + dy_tot * t
        # only wrap when there is a global drift; otherwise orbits are closed
        # and unwrapped coords keep the loop seam-free.
        if dx_tot:
            cx = np.mod(cx, W)
        if dy_tot:
            cy = np.mod(cy, H)

        tw = 1.0 - a.flicker * 0.5 * (1.0 - np.cos(2 * math.pi * (fl_k * t) + fl_ph))
        # tw in [1-flicker, 1]
        bri = amp * np.clip(tw, 0.0, 1.5)

        acc = np.zeros((H, W), dtype=np.float32)
        for i in range(n):
            r = rad[i]
            b = bri[i]
            if b <= 0.004:
                continue
            R = int(math.ceil(r * 3.0)) + 1
            x0, x1 = int(cx[i]) - R, int(cx[i]) + R + 1
            y0, y1 = int(cy[i]) - R, int(cy[i]) + R + 1
            sx0, sx1 = max(0, x0), min(W, x1)
            sy0, sy1 = max(0, y0), min(H, y1)
            if sx0 >= sx1 or sy0 >= sy1:
                continue
            dx = xx[:, sx0:sx1] - cx[i]
            dy = yy[sy0:sy1, :] - cy[i]
            g = np.exp(-(dx * dx) / (2 * r * r)) * np.exp(-(dy * dy) / (2 * r * r))
            acc[sy0:sy1, sx0:sx1] += (b * g).astype(np.float32)

        acc = np.clip(acc, 0, 1)
        frame = (acc[:, :, None] * col[None, None, :] * 255.0)
        ff.stdin.write(np.clip(frame, 0, 255).astype(np.uint8).tobytes())

    ff.stdin.close()
    rc = ff.wait()
    if rc:
        sys.exit(rc)
    print(f"wrote {a.out}  ({n} particles, {nframes} frames, loop {a.duration}s)")


if __name__ == "__main__":
    main()
