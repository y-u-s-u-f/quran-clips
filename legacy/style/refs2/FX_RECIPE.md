# FX_RECIPE — reproducing the Node Video effect stack in ffmpeg

Target look: the Arabic Quran-recitation reel style in
`ig_DNon3t8J9je.mp4` (gold) and `ig_DNrnY9b2EHe.mp4` (rose/maroon).
Tutorial pipeline = flat build (letterboxed footage + text on rounded colour
bars) → four stacked Node Video effects: **Glow**, **Glow Scan**, **Snow**,
**Heat Wave**.

Everything below was built and rendered on this machine. Test renders are in
`fx/` (paths at the bottom).

---

## A. Capability report

```
ffmpeg version 8.1.2  (homebrew)
configuration: --enable-gpl --enable-libx264 --enable-libx265 --enable-libvpx
               --enable-libsvtav1 --enable-libvmaf --enable-videotoolbox
               --enable-neon --enable-shared --enable-version3
libavfilter 11.14.102
```

**frei0r: NOT built in.** `ffmpeg -filters | grep frei0r` → nothing.
It was **not** installed, and installing it is **not** recommended: homebrew's
`frei0r` would require rebuilding ffmpeg with `--enable-frei0r` (the bottled
ffmpeg has no plugin loader for it), and every frei0r filter we would want
(`glow`, `distort0r`, `nervous`) is reproducible with native filters at equal
or better quality. Not worth a source build.

Relevant native filters present (all confirmed):

| need | available |
|---|---|
| threshold / luma key | `lutrgb`, `lutyuv`, `lut`, `curves`, `colorlevels`, `lumakey`, `colorkey`, `geq`, `pseudocolor` |
| luma extraction | `colorchannelmixer` (Rec.601 matrix into all 3 channels) |
| blur | `gblur` (sigma + steps), `boxblur`, `avgblur`, `smartblur`, `unsharp` |
| blend | `blend` (screen, lighten, add, softlight, …), `tblend`, `overlay`, `maskedmerge` |
| colorise | `colorchannelmixer`, `colorize`, `colorbalance`, `hue`, `selectivecolor`, `colortemperature`, `monochrome`, `vibrance` |
| displacement | `displace` (3-input xmap/ymap), `remap`, `rgbashift`, `chromashift`, `perspective`, `shufflepixels`, `scroll` |
| generated sources | **`perlin`** (octaves/persistence/xscale/yscale/tscale/seed), `noise`, `cellauto`, `mandelbrot`, `life`, `gradients`, `nullsrc` |
| grading | `eq`, `curves`, `vignette`, `exposure`, `lut3d` |

The single most useful discovery: **`perlin`** is a real fractal-noise *source*
with `octaves`, `persistence`, `xscale`, `yscale`, `tscale` — it maps almost
one-to-one onto the Heat Wave parameters.

### Gotchas hit while testing
* `crop` on a `yuv420p` stream **silently rounds to even dimensions**. Put
  `format=gbrp` *before* `crop` or a 405-px band becomes 404 and `blend` fails
  with "input link parameters do not match".
* `libx264` refuses odd dimensions — the particle generator pads/crops on write.

---

## What measurement of the references established (before any filtergraph)

These constrain the recipe more than the tutorial numbers do.

1. **The FX are confined to the footage rectangle.** The letterbox bars are
   *bit-exact black*, and the band edge goes `0 → 18` in **two rows**
   (measured row means at y=436..438 and y=841..843 in both refs). There is
   **zero** glow bleed and **zero** particle spill into the bars. So the stack
   must be applied to the footage band, then padded — not to the whole 720×1280
   canvas.
2. **The base under the FX is heavily graded down.** Band luma means:
   gold ref **0.319**, rose ref **0.149–0.173**; fraction of band above 0.40
   luma = **0.27 / 0.12**; above 0.90 = **1.4% / 0.3%**.
   This is why "Threshold range 0.20–0.40" is sane in Node and why the same
   number applied to an ungraded flat produces a solid yellow wash — I hit
   exactly that on the first render. **The glow numbers only work on a dark,
   contrasty, vignetted base.** Target band mean luma ≈ 0.15–0.32.
3. **The halo is subtle.** Vertical profile above the rose caption bar:
   `y=600 → 7`, `620 → 11`, `635 → 19`, bar `→ 101`. Added glow at the edge of
   a mid-bright object is only ≈ **+10–15/255**, decaying over ~30–40 px.
   The *bright* halo you notice is on the **white text** (luma 1.0) — that is
   the Glow Scan, not the main glow.
4. **Speck colour**: reference specks peak around `(102,79,81)` on rose —
   tinted, ~40 % brightness, 1–4 px, soft-edged, ~25–35 visible per band.
5. Reference tint samples: gold bar ≈ `#BCA770` (post-glow), rose bar ≈
   `#836569` (post-glow). Working tints used: **gold `#C9A227`**, **rose `#B0576B`**.

---

## B. GLOW — tested filtergraph

```text
[band]split=2[base][g1];
[g1]colorchannelmixer=rr=0.299:rg=0.587:rb=0.114:
                      gr=0.299:gg=0.587:gb=0.114:
                      br=0.299:bg=0.587:bb=0.114,
    lutrgb=r='clip((val-51)*5.0,0,255)':
           g='clip((val-51)*5.0,0,255)':
           b='clip((val-51)*5.0,0,255)',
    gblur=sigma=20:steps=3,
    colorchannelmixer=rr=0.2758:gg=0.2223:bb=0.0536[glow];
[base][glow]blend=all_mode=screen:shortest=1[s1]
```

(`0.2758 0.2223 0.0536` = `#C9A227` normalised × GLOW_GAIN 0.35.)

### Parameter mapping

| Node setting | ffmpeg | derivation |
|---|---|---|
| Threshold range **0.20 – 0.40** | `lutrgb` soft knee `clip((val-51)*5.0,0,255)` | lo = 0.20·255 = **51**, hi = 0.40·255 = **102**, slope = 255/(102−51) = **5.0**. Below 0.20 → 0, above 0.40 → full, linear ramp between. |
| **Scattering 7.00** | `gblur=sigma=20:steps=3` | σ = scatter × **2.86** × (band_h/405). Derived by sweeping σ ∈ {8,16,25,40} and matching the reference's ~30–40 px halo decay. `steps=3` approximates a true gaussian. |
| **Intensity 1.5** | mixer gain **0.35** (not 1.5) | Honest note: Node normalises the glow layer internally; a full-range 0–255 mask screened at 1.5 blows the frame to solid yellow (I rendered it — see the first sweep). Swept 0.35/0.6/0.9/1.5; **0.35** reproduces the reference's warm haze, 0.6 is a heavier "cinematic bloom" option. |
| **Glow Color** (eyedropper gold) | `colorchannelmixer=rr/gg/bb = tint × gain` | mask is greyscale, so a diagonal mixer both tints and scales it. |
| **Blend: Lighten** | `blend=all_mode=screen` | **Deliberate deviation.** `all_mode=lighten` = `max(base,glow)`, which replaces all shadow detail with flat tinted mush — I rendered it and it destroys the image (`cmp_lighten.png` during testing). `screen` is the standard bloom composite and matches the references. |
| Preserve Alpha: Off | n/a | we work on an opaque band. |

Luma extraction uses the Rec.601 matrix loaded into **all three** output
channels of `colorchannelmixer`, which turns the RGB frame into a grey RGB
frame — so the subsequent per-channel `lutrgb` acts as a true luma threshold.

---

## C. GLOW SCAN — tested filtergraph

```text
[s1]split=2[s1a][sc0];
[sc0]colorchannelmixer=rr=0.299:rg=0.587:rb=0.114:
                       gr=0.299:gg=0.587:gb=0.114:
                       br=0.299:bg=0.587:bb=0.114,
     lutrgb=r='clip((val-230)*10.2,0,255)':
            g='clip((val-230)*10.2,0,255)':
            b='clip((val-230)*10.2,0,255)',
     gblur=sigma=6.72:steps=3,
     lutrgb=r='clip(val*3,0,255)':g='clip(val*3,0,255)':b='clip(val*3,0,255)',
     colorchannelmixer=rr=1.1031:gg=0.8894:bb=0.2145[scan];
[s1a][scan]blend=all_mode=screen:shortest=1[s2]
```

### How it differs from the main glow, and why

| | Glow | Glow Scan |
|---|---|---|
| threshold | soft knee 0.20→0.40 (`-51 × 5.0`) | near-hard at 0.90 (`-230 × 10.2`) — only the white text and specular highlights survive (1.4 % / 0.3 % of the band) |
| σ | 20 | **6.72** — tight |
| post-blur | none | `lutrgb val*3` — flat-tops the blurred mask. This is what "**Blend Mode: Solid Color**" means: the bloom reads as a solid slab of tint with a soft rim, not a smooth falloff |
| gain | 0.35 | **1.40** — much hotter, but over a tiny area, so it lifts to near-white right at the glyph edge and stays gold further out — exactly the halo seen around the reference's white Arabic text |
| input | the band | the **output of the main glow** (effects are serial in Node) |

| Node setting | ffmpeg |
|---|---|
| Channel: Luminance | the Rec.601 `colorchannelmixer` |
| Threshold **0.90** | knee at `0.90·255 = 230`, slope `255/(255−230) = 10.2` |
| Scattering **6.00** / Radius **0.70** | σ = `scatter × 1.20 × radius × 2 × (band_h/405)` = **6.72**. Swept scatter ∈ {3,6,10} against the reference glyph halo (`fx/cmp_scan2.png`); the reference sits between 3 and 6 → default **4.0** in the script (σ ≈ 6.7). |
| Intensity 1.0 / Solid Color | gain 1.40 + the `val*3` flat-top |
| Smooth: Off | no extra blur pass |

---

## D. SNOW / particles

### Options evaluated

* **(i) ffmpeg `noise` filter** — rejected. Produces per-pixel static that
  changes every frame; there is no notion of a particle that persists, drifts
  and flickers. `cellauto`/`life`/`mandelbrot` are structurally wrong.
* **(iii) `geq`** — rejected. You can express a handful of moving gaussians in
  a `geq` expression, but ~25 particles means a ~25-term expression evaluated
  per pixel per channel; it is unreadable, unparameterisable, and slow.
* **(ii) pre-rendered Python layer — CHOSEN.** Full control over amount, size,
  drift, flicker, colour; renders 180 frames of 720×404 in **0.9 s**; cached
  per (size, colour, amount, size) and `-stream_loop -1`'d, so it costs nothing
  on re-renders. Made seamlessly loopable by putting every particle on a closed
  elliptical orbit whose period is exactly the loop length.

### Generator — `fx/particles.py` (full source)

```python
#!/usr/bin/env python3
"""
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
```

### Generate + composite

```bash
python particles.py --w 720 --h 404 --fps 30 --duration 6 \
    --color '#C9A227' --amount 0.70 --size 0.10 --speed -1.0 --flicker 1.0 \
    --out particles_gold.mp4
```

```text
[1:v]format=gbrp,scale=720:405:flags=neighbor,setsar=1[pt];
[s2][pt]blend=all_mode=screen:shortest=1[bandfx]
```

`--amount 0.70 × --density 110/Mpx × 0.29 Mpx` → **22 particles** over the band,
mean radius `0.10 × 22 = 2.2 px` (visible diameter ~5 px) — matching the
reference's small, sparse, soft specks. Input is `-stream_loop -1`'d so a 6 s
layer covers any clip length.

---

## E. HEAT WAVE — tested, and the honest verdict

It **works**, and `perlin` maps onto it almost exactly:

```text
perlin=size=720x404:rate=30:octaves=6:persistence=0.6:
       xscale=0.33:yscale=0.33:tscale=0.5:random_mode=seed:seed=11,
  scroll=vertical=-0.004,scale=720:405,format=gbrp,
  lutrgb=r='128+(val-157)*0.0678':g='...':b='...'[xm];
perlin=... :seed=77, ...same... [ym];
[band][xm][ym]displace=edge=smear[warped]
```

| Node setting | ffmpeg |
|---|---|
| **Octaves 6** | `perlin:octaves=6` |
| **Scale 0.33** | `xscale=0.33:yscale=0.33` |
| **Speed 0.50** | `tscale=0.5` |
| **Frequency 0.50 / Offset 0.09** | folded into `persistence=0.6` and the seeds |
| **Direction 0 × −125** | `scroll=vertical=-0.004` (upward drift of the noise field) |
| **Amplitude 0.02** | `lutrgb=128+(val-157)*k`, `k = amp_px/29.5`. This build's perlin spans 127–186 (centre 157, σ 8.2), so `k=0.068` → **±2 px**. |
| **Keep Edge: On** | `displace=edge=smear` |

### Verdict: ~~**DROP IT.**~~ **SUPERSEDED — see "Heat Wave — motion re-test" at the
### bottom of this file.** The measurements below were made from still frames and
### from an integer-pixel `displace` with a mis-centred perlin normalisation. Heat
### Wave *is* present in both reference reels, it *does* read in motion, and it
### ships. The section below is kept only for the record.

* **Invisible at the specified amplitude.** Rendered ±2 px, ±4 px, ±12 px and
  compared against ±0 (`fx/cmp_heat.png`, left = off, middle = 2 px, right =
  12 px). At 2 px the still is indistinguishable; in motion it is a faint
  breathing you have to be told to look for.
* **It is expensive.** Same 4 s clip, same encoder settings:
  off = **257 KB**, ±2 px = **457 KB** (+78 %), ±4 px = 645 KB, ±12 px = 1.18 MB.
  PSNR vs. off is only 22.6 dB at ±2 px — i.e. it perturbs *every* pixel every
  frame, which is precisely what a video codec hates. On the full pipeline the
  gold render went 617 KB → 1023 KB (+66 %) for no visible gain.
  After Instagram's re-encode that extra bitrate becomes *worse* artefacts
  elsewhere. **Net negative.**
* **It can actively damage the frame.** Above ~8 px it visibly tears the
  rounded caption bar and the Arabic glyph edges (right panel of
  `cmp_heat.png`) — the one thing in the design that must stay crisp. The
  references have razor-clean bar edges.

It is wired into `nodefx.sh` behind `HEAT=1` for completeness. Leave it off.
If you ever do want it, apply it to the **footage only, before the text layer
is composited**, and keep `HEAT_AMP_PX ≤ 2`.

---

## F. Combined pipeline — `fx/nodefx.sh`

```
nodefx.sh <in.mp4> <out.mp4> <#RRGGBB> [band_y] [band_h]
```

Single tint parameter drives glow, glow scan and particles. `band_y`/`band_h`
describe the letterboxed footage rectangle so the FX stay inside it (pass
`0 <height>` to treat the whole frame as the band). Env overrides:
`GLOW_LO GLOW_HI GLOW_SCATTER GLOW_GAIN SCAN_THR SCAN_SCATTER SCAN_RADIUS
SCAN_GAIN SNOW_AMOUNT SNOW_SIZE SNOW_SPEED SNOW_FLICKER HEAT HEAT_AMP_PX`.

```bash
#!/bin/bash
# nodefx.sh -- reproduce the Node Video "Glow + Glow Scan + Snow + Heat Wave"
# stack in ffmpeg.  See FX_RECIPE.md for the parameter mapping.
#
#   nodefx <in.mp4> <out.mp4> <#RRGGBB> [band_y] [band_h]
#
# band_y/band_h describe the letterboxed footage rectangle; the FX are confined
# to it (the reference reels show ZERO glow/particle bleed into the black bars).
# Pass 0 and the full height to treat the whole frame as the band.

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-$HERE/venv/bin/python}"
FF="${FF:-ffmpeg}"

IN="$1"; OUT="$2"; TINT="${3:-#C9A227}"

W=$(ffprobe -v error -select_streams v:0 -show_entries stream=width  -of csv=p=0 "$IN")
H=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$IN")
FPS=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of csv=p=0 "$IN" | awk -F/ '{printf "%.4f", $1/$2}')
BY="${4:-0}"; BH="${5:-$H}"

# ---- tunables (defaults = the tutorial's settings, remapped -- see recipe) ---
GLOW_LO=${GLOW_LO:-0.20}       # Threshold range low knee
GLOW_HI=${GLOW_HI:-0.40}       # Threshold range high knee
GLOW_SCATTER=${GLOW_SCATTER:-7.0}   # Scattering
GLOW_GAIN=${GLOW_GAIN:-0.35}   # Intensity (Node 1.5 -> ~0.35 here; see recipe)
SCAN_THR=${SCAN_THR:-0.90}     # Glow Scan Threshold
SCAN_SCATTER=${SCAN_SCATTER:-4.0}
SCAN_RADIUS=${SCAN_RADIUS:-0.70}
SCAN_GAIN=${SCAN_GAIN:-1.40}
SNOW_AMOUNT=${SNOW_AMOUNT:-0.70}
SNOW_SIZE=${SNOW_SIZE:-0.10}
SNOW_SPEED=${SNOW_SPEED:--1.0}
SNOW_FLICKER=${SNOW_FLICKER:-1.0}
HEAT=${HEAT:-0}                # 1 to enable Heat Wave (see recipe: negligible)
HEAT_AMP_PX=${HEAT_AMP_PX:-2}

# Scattering -> gaussian sigma, scaled to the band height (tuned at BH=405).
SIG=$($PY   -c "print(round($GLOW_SCATTER * 2.86 * $BH/405.0, 2))")
SSIG=$($PY  -c "print(round($SCAN_SCATTER * 1.20 * $SCAN_RADIUS * 2 * $BH/405.0, 2))")
LO=$($PY -c "print(round($GLOW_LO*255))")
HI=$($PY -c "print(round($GLOW_HI*255))")
KN=$($PY -c "print(round(255.0/($HI-$LO), 6))")
STH=$($PY -c "print(round($SCAN_THR*255))")
SKN=$($PY -c "print(round(255.0/(255-$STH), 6))")

read -r TR TG TB <<<"$($PY - "$TINT" <<'EOF'
import sys
s = sys.argv[1].lstrip('#')
print(*[int(s[i:i+2], 16)/255 for i in (0, 2, 4)])
EOF
)"
GR=$($PY -c "print(round($TR*$GLOW_GAIN,4))"); GG=$($PY -c "print(round($TG*$GLOW_GAIN,4))"); GB=$($PY -c "print(round($TB*$GLOW_GAIN,4))")
SR=$($PY -c "print(round($TR*$SCAN_GAIN,4))"); SG=$($PY -c "print(round($TG*$SCAN_GAIN,4))"); SB=$($PY -c "print(round($TB*$SCAN_GAIN,4))")

# ---- SNOW layer (pre-rendered, seamless 6 s loop) --------------------------
PBH=$(( BH - BH % 2 )); PBW=$(( W - W % 2 ))
PART="$HERE/.parts_${PBW}x${PBH}_$(echo "$TINT" | tr -d '#')_${SNOW_AMOUNT}_${SNOW_SIZE}.mp4"
[ -f "$PART" ] || $PY "$HERE/particles.py" --w "$PBW" --h "$PBH" --fps "$FPS" \
    --duration 6 --color "$TINT" --amount "$SNOW_AMOUNT" --size "$SNOW_SIZE" \
    --speed "$SNOW_SPEED" --flicker "$SNOW_FLICKER" --out "$PART"

LUMA="colorchannelmixer=rr=0.299:rg=0.587:rb=0.114:gr=0.299:gg=0.587:gb=0.114:br=0.299:bg=0.587:bb=0.114"
KNEE="lutrgb=r='clip((val-${LO})*${KN},0,255)':g='clip((val-${LO})*${KN},0,255)':b='clip((val-${LO})*${KN},0,255)'"
SKNEE="lutrgb=r='clip((val-${STH})*${SKN},0,255)':g='clip((val-${STH})*${SKN},0,255)':b='clip((val-${STH})*${SKN},0,255)'"

# ---- HEAT WAVE (optional; see recipe -- visually negligible, costs bitrate) --
if [ "$HEAT" = "1" ]; then
  # perlin output here spans ~127..186 (centre 157); remap to 128 +- amp px.
  K=$($PY -c "print(round($HEAT_AMP_PX/29.5,4))")
  PMAP="scroll=vertical=-0.004,scale=${W}:${BH},format=gbrp,lutrgb=r='128+(val-157)*${K}':g='128+(val-157)*${K}':b='128+(val-157)*${K}'"
  PSRC="perlin=size=${PBW}x${PBH}:rate=${FPS}:octaves=6:persistence=0.6:xscale=0.33:yscale=0.33:tscale=0.5:random_mode=seed"
  HEATG="
${PSRC}:seed=11,${PMAP}[xm];
${PSRC}:seed=77,${PMAP}[ym];
[band][xm][ym]displace=edge=smear[warped];
"
  WARP="[warped]"
else
  HEATG=""
  WARP="[band]"
fi

FC="
[0:v]split=2[full][pre];
[pre]format=gbrp,crop=${W}:${BH}:0:${BY}[band];
${HEATG}
${WARP}split=3[base][g1][sc0];
[g1]${LUMA},${KNEE},gblur=sigma=${SIG}:steps=3,
    colorchannelmixer=rr=${GR}:gg=${GG}:bb=${GB}[glow];
[base][glow]blend=all_mode=screen:shortest=1[s1];
[sc0]${LUMA},${SKNEE},gblur=sigma=${SSIG}:steps=3,
    lutrgb=r='clip(val*3,0,255)':g='clip(val*3,0,255)':b='clip(val*3,0,255)',
    colorchannelmixer=rr=${SR}:gg=${SG}:bb=${SB}[scan];
[s1][scan]blend=all_mode=screen:shortest=1[s2];
[1:v]format=gbrp,scale=${W}:${BH}:flags=neighbor,setsar=1[pt];
[s2][pt]blend=all_mode=screen:shortest=1[bandfx];
[full][bandfx]overlay=0:${BY}:shortest=1,format=yuv420p[v]
"

$FF -v error -y -i "$IN" -stream_loop -1 -i "$PART" \
    -filter_complex "$FC" -map "[v]" -map 0:a? -shortest \
    -c:v libx264 -crf 18 -preset medium -pix_fmt yuv420p -c:a copy "$OUT"
echo "wrote $OUT"
```

### Order of operations

`crop to band → (heat wave) → glow → glow scan → snow → overlay back onto the
letterboxed canvas`. Snow goes **last** so a warp can never smear the specks,
and glow scan reads the glow's output because Node applies effects serially.

### Step 0 you must not skip — the grade

The FX numbers assume the graded base described above. The grade used for the
test flats (this is *not* one of the four effects; it belongs to the flat build):

```text
# gold
scale=720:405,
eq=brightness=-0.10:contrast=1.30:saturation=0.75:gamma=0.72,
colorbalance=rm=0.08:gm=0.02:bm=-0.10,
vignette=a=PI/2.6,
pad=720:1280:0:437:black
# rose
eq=brightness=-0.14:contrast=1.35:saturation=0.60:gamma=0.62,
colorbalance=rm=0.05:gm=-0.02:bm=0.02, vignette=a=PI/2.3
```

Verify with: band mean luma **0.15–0.32**, fraction above 0.40 luma **0.12–0.27**.
If your flat is brighter than that, the glow will wash the frame yellow.

---

## Honest verdict — which effects actually matter

| effect | importance | why |
|---|---|---|
| **The grade** (not one of the four) | **essential — biggest single contributor** | dark, low-sat, vignetted, warm-shifted. Without it none of the FX numbers behave, and with it alone you are already ~60 % of the way to the look. |
| **Glow** | **high** | supplies the warm haze that makes the whole frame read as one graded image and softly lifts the caption bars. Clearly present in both refs. |
| **Glow Scan** | **high — the signature** | the bright tight halo on the white Arabic text. This is the detail the eye reads as "premium". Drop it and the text looks flat and pasted-on. Cheap (tiny mask area). |
| **Snow / particles** | **medium — cheap and worth it** | small, sparse gold specks. Individually barely noticeable in a still, but they are unmistakably in both references and they stop the dark areas from looking like dead black. ~1 s to generate, cached, near-zero encode cost. |
| **Heat Wave** | **KEEP — present in both refs, measured** | ~~negligible~~ **superseded.** The bar-edge displacement measurement (final section) finds 0.96 px RMS sub-pixel wander in both references against a 0.10 px noise floor — 9× the floor, and spatially coherent. It ships. |

Confinement to the footage band matters more than any individual effect
setting: bleeding glow or specks into the letterbox bars is the one thing that
would instantly read as "not the same style".

---

## Test renders

| file | what |
|---|---|
| `fx/fx_gold_final.mp4` | **best render** — 10 s of `sources/YkXjYyKwHJ4.mp4` (from 05:00), gold `#C9A227`, glow + glow scan + snow, heat wave off |
| `fx/fx_rose_final.mp4` | same segment, rose `#B0576B` |
| `fx/fx_gold_heat.mp4` | identical to the gold render but `HEAT=1` — for the A/B that shows it is not worth 66 % more bitrate |
| `fx/flat.mp4`, `fx/flat_rose.mp4` | the graded, letterboxed, captioned **inputs** (before FX) |
| `fx/FINAL_CMP.png` | ref-gold │ ours-gold │ ref-rose │ ours-rose |
| `fx/cmp_scan2.png` | glow-scan scatter sweep (ref vs 3 / 6 / 10) zoomed on the glyphs |
| `fx/cmp_heat.png` | heat wave off / ±2 px / ±12 px |
| `fx/nodefx.sh`, `fx/particles.py` | the pipeline |

`nodefx.sh` expects `$PY` to be a python with numpy (set `PY=/path/to/python`);
it only uses python for arithmetic and to run the particle generator.

---

# Heat Wave — motion re-test

Section E above judged Heat Wave from **still frames** and bitrate. Shimmer is a
temporal effect, so that test could not see it. This section redoes it properly:
a sub-pixel displacement measurement on the references, an amplitude ladder
rendered and viewed in motion, and a corrected parameter mapping. **Conclusion:
Heat Wave is in the references, it ships, and section E's mapping had two real
bugs.**

Artefacts: `fx/heat/` (A/B videos, x-t slices, the test harness `heatfx.sh`, and
the measurement code `edge2.py` / `rd.py` / `run3.py`).

---

## 1. The measurement — sub-pixel wander of the caption-bar edges

The caption bars are the most sensitive probe available: machine-drawn, straight,
high-contrast, and known to be geometrically static in the composition. If they
wander, something is warping the whole flattened frame.

**Estimator.** For a horizontal bar edge, take an 11-row window and solve, per
column and per frame, the 3-parameter least-squares fit

```
I(y,t) − T(y)  ≈  −d·T′(y)  +  a·T(y)  +  b
```

where `T` is the time-averaged column profile. `d` is the sub-pixel displacement;
`a`,`b` absorb glow flicker and exposure drift so brightness changes are not read
as motion. Displacements are then split into
`d(x,t) = global(t) + static(x) + residual(x,t)`:
`global` = rigid shift of the whole edge, `residual` = the spatially-varying
(non-rigid) part that only a warp field can produce.

Every figure below uses **identical geometry** (130-column window, 165 frames) so
the numbers are directly comparable.

> Gotcha that cost an hour: `ffmpeg -vf crop=W:H` on a `yuv420p` stream silently
> rounds an **odd** H down, so the raw byte stream desynchronises by one row per
> frame. Put `format=gray` **before** `crop`.

### Results

| clip | RMS total (px) | rigid part | non-rigid part | non-rigid / rigid |
|---|---|---|---|---|
| **noise floor** — our reel, heat OFF, CRF 18 | **0.103** | 0.010 | 0.102 | — |
| same, re-encoded to the refs' 455 kbps | 0.106 | 0.013 | 0.105 | — |
| our reel, heat ON ±2 px (old mapping) | 0.965 | 0.820 | 0.508 | 0.62 |
| **`ig_DNrnY9b2EHe` (rose)** — 12 windows | **0.99** | 0.66 | 0.73 | 1.11 |
| **`ig_DNon3t8J9je` (gold)** — 4 windows | **0.79** | 0.55 | 0.65 | 1.18 |
| **both refs, mean of 16 windows** | **0.956** | 0.635 | 0.715 | 1.13 |

### Verdict on Q1 — yes, it is there

* The refs measure **0.956 px RMS**, **9.3× the method's own noise floor** of
  0.103 px. The floor was established on our own captioned reel with a moving
  subject, at CRF 18 *and* re-encoded to the refs' exact bitrate — compression
  moved it by 0.003 px, so codec noise cannot explain the refs.
* It is **not camera shake.** A rigid shift would put all the variance in the
  global term. 56 % of it (0.715 of 0.956) is *non-rigid* — the straight bar edge
  becomes slightly wavy, and the waviness changes shape over time.
* It is **not codec jitter.** The displacement is spatially coherent: the two
  caption bars, 123 px apart, wander together with **r = 0.733 (rose) / 0.725
  (gold)**. Quantisation noise gives r ≈ 0. Our known-heat control gives r = 0.756;
  our heat-off control gives no significant correlation.
* Temporally it is smooth and slow: **97 % of the residual power is below 2 Hz**,
  autocorrelation half-life 11–16 frames (0.4–0.5 s). Codec noise is broadband;
  this is not.

So the tutorial and the reference creator are doing the same thing. The refs'
displacement field is **0.96 px RMS at 720×1280 = 0.00133 × frame width**, peak
(1st/99th pct) **≈ ±2.2 px = 0.0031 × frame width**.

### Amplitude calibration

Rendering our own pipeline at known amplitudes and measuring it back:

| commanded amplitude | measured RMS (130-col window) |
|---|---|
| 0 | 0.103 |
| ±0.5 px | 0.307 |
| ±1 px | 0.546 |
| ±2 px | 0.965 |
| ±3 px | 1.385 |
| ±4 px | 1.860 |

Linear, slope 0.438 px measured per px commanded. Inverting the refs' 0.956 gives
the numbers above.

---

## 2. Two bugs in section E's mapping

### (a) `displace` is **integer-pixel only**

Proved directly: feed a ramp x-map spanning 124→132 over 256 px and the output
sampling position steps in whole pixels at each map level boundary. At ±2 px the
warp field therefore takes only the values −2,−1,0,1,2 — a piecewise-constant
tile field, not a smooth warp. That is what section E saw as "tearing" at ≥8 px;
it is present at *every* amplitude, it is just coarser when there are more levels.

**Fix — supersample around the displace.** Upscale the band by `S` with
`flags=neighbor` (exact replication, no softening), displace by `amp × S`, and
downscale with `flags=area` (a proper box average). `S = 3` gives ⅓-px steps.

Cost of the fix at 720, same amplitude, CRF 18: **1 023 KB → 1 064 KB (+4 %)** and
+20 % encode time. It is essentially free and it is the difference between a
smooth shimmer and a 1-px judder on the crisp bar edge (see
`fx/heat/xt_recommended.png`, panel 3).

### (b) The perlin normalisation was mis-centred — it shifted the picture

Section E used `lutrgb=128+(val−157)*k`. The actual perlin output, measured over
90 frames at both seeds, is **mean 130.26, sd 18.4** (at `xscale=2`; mean 135, sd
22 at `xscale=0.33`) — not centred on 157. The consequence is a **constant DC
displacement**, not a shimmer:

```
edge y, heat off                585.841
edge y, heat on, centre 157     587.096   ->  +1.255 px permanent shift
edge y, heat on, centre 130.26  585.975   ->  +0.134 px
```

The whole footage band was being nudged 1.26 px down-right for the entire clip.

**Fix:** centre on the measured mean and scale by the measured sd, so amplitude is
expressed as an RMS in pixels and is exact:

```
K = RMS_px * S / 18.4        (18.4 = perlin sd at xscale 2.0)
lutrgb = 128 + (val - 130.26) * K
```

### (c) `Scale 0.33` is too coarse to be a heat wave

`perlin`'s `xscale`/`yscale` are in **normalised frame coordinates**, so the field
is *resolution-independent* — identical statistics at 720×404 and 1080×608 for the
same `xscale`. No rescaling is needed when moving to the 1080 canvas.

At `xscale=0.33` the field is nearly constant across the frame: the whole band
translates as a unit (non-rigid/rigid ratio **0.49**), which reads as camera
wobble, not shimmer. The references are at **1.13**. Sweeping:

| xscale | non-rigid / rigid | CRF-18 size (10 s, 720) |
|---|---|---|
| 0.33 (tutorial-literal) | 0.49 | 1 064 KB |
| 1.0 | 0.64 | 1 213 KB |
| **2.0** | **0.88** | 1 342 KB |
| 4.0 | 0.99 | 1 567 KB |
| *references* | *1.13* | — |

`xscale = 2.0` is the recommendation: it reproduces the refs' balance of local
warp vs. bulk drift at a bitrate that is still sane. `4.0` matches marginally
better for another 17 %.

### What survived unchanged

`tscale = 0.5` is right. The refs' rigid-component autocorrelation decays to 0.5
in ~14 frames; raw perlin at `tscale=0.5` decays to 0.5 in ~16 frames. Node's
"Speed 0.50" maps one-to-one after all. `octaves=6`, `persistence=0.6`,
`scroll=vertical=-0.004` (Direction 0 × −125) and `edge=smear` (Keep Edge On) all
stand.

---

## 3. The amplitude window — where it reads, where it breaks

Ladder rendered with the supersampled displace and viewed as y-t slices through a
column that cuts both caption bars (`fx/heat/xt_amplitude_ladder.png`, panels
off / 1 / 2 / 3 / 4 / 6 / 8 in old commanded-px units; true RMS in the table):

| true RMS @720 | in motion, judged on the bars and glyphs |
|---|---|
| 0 | dead straight lines in the x-t slice |
| **0.75 px** | **floor** — visible in the slice, only just perceptible live |
| **1.0–1.5 px** | **the window.** Smooth organic breathing on the pill edges and glyph strokes. Reads as heat haze. The references sit here (0.96). |
| 2.2 px | pronounced; still organic, starting to draw attention |
| 3.0 px | large excursions — the caption visibly swims |
| **4.5 px** | **ceiling exceeded** — edges go ragged, high-frequency shiver |
| 6.0 px | badly broken |

Note that section E's "it tears the caption bar above ~8 px" is **not** what
limits it. With the supersampled displace the bar edges stay clean in stills even
at the top of the ladder (`fx/heat/cmp_amp_tearing.png` — off / 3 / 6 / 8, and the
pill outline survives all four). What limits it is *temporal*: past ~3 px RMS the
caption stops shimmering and starts swimming.

Because the effect is applied to the flattened frame, the text and bars shimmer
with the footage — which is the point. The hard glyph edges are where the effect
is actually legible; on the soft dark footage alone it would be near-invisible.
That is why section E, looking mostly at the footage in stills, missed it.

---

## 4. Where in the chain, and what it costs

The tutorial applies the four effects serially: Glow → Glow Scan → Snow →
**Heat Wave**. So Heat Wave is **last**, over the fully composited band. Tested
both ways at the recommended settings, 10 s at 720, CRF 18:

| ordering | size | notes |
|---|---|---|
| heat **before** glow (what `nodefx.sh` does today) | 1 201 KB | glow/scan recomputed from the warped image; the snow specks are never warped |
| heat **after** everything (Node's order) | **1 186 KB** | −1.3 % |

The hoped-for saving from moving it earlier does **not** exist — the two orderings
are within noise of each other, and the later position is the faithful one.
Use **last**.

Bitrate cost at CRF 18, 10 s, full pipeline:

| canvas | heat off | heat on (recommended) | delta |
|---|---|---|---|
| 720×1280, band 720×405 | 617 KB | 1 183 KB | +92 % |
| 1080×1920, band 1080×608 | 1 156 KB | ~2 040 KB | +76 % |

That cost is real and unavoidable: the effect perturbs every pixel every frame.
It is the price of the effect, not an argument against it.

Encode time at 1080 with `S=3`: ~9× realtime on this machine (88 s for 10 s).
`S=2` halves that and still gives ½-px steps if render time becomes a problem.

---

## 5. FINAL CONFIGURATION — 1080×1920 canvas, 1080×608 band at y=656

Amplitude is specified as an **RMS displacement in pixels**, which is what the
measurement returns and what scales cleanly:

```
RMS displacement = 0.00133 x frame width
   720 wide -> 0.96 px   (measured value of both references)
  1080 wide -> 1.44 px   <-- USE THIS
peak (1st/99th pct) = 2.3 x RMS = +-3.3 px @1080 = 0.0031 x frame width
```

Placed **after** the snow blend, i.e. as the last thing that touches the band:

```text
# constants for 1080x608, S=3, RMS 1.44 px
#   K   = 1.44 * 3 / 18.4 = 0.2348      (18.4 = measured perlin sd at xscale 2.0)
#   CTR = 130.26                        (measured perlin mean at xscale 2.0)

PSRC = perlin=size=1080x608:rate=30:octaves=6:persistence=0.6:
       xscale=2.0:yscale=2.0:tscale=0.5:random_mode=seed
PMAP = scroll=vertical=-0.004,
       scale=3240:1824:flags=bicubic,format=gbrp,
       lutrgb=r='128+(val-130.26)*0.2348':
              g='128+(val-130.26)*0.2348':
              b='128+(val-130.26)*0.2348'

  ...;[s2][pt]blend=all_mode=screen:shortest=1[bandfx0];
  PSRC:seed=11,PMAP[xm];
  PSRC:seed=77,PMAP[ym];
  [bandfx0]scale=3240:1824:flags=neighbor[bigb];
  [bigb][xm][ym]displace=edge=smear,scale=1080:608:flags=area[bandfx];
  [full][bandfx]overlay=0:656:shortest=1,format=yuv420p[v]
```

`flags=neighbor` on the way up and `flags=area` on the way down are load-bearing:
neighbour replication keeps the upscale lossless, area averaging is what turns the
integer displacement into a ⅓-px one. Bicubic on the *map* upscale is deliberate —
the map should be smooth.

### `templates/bars.yaml` — replace the `fx:` note and add the block

```yaml
# style/refs2/fx/nodefx.sh, confined strictly to the footage band.
# Heat Wave IS in both references (0.96 px RMS bar-edge wander @720 vs a
# 0.10 px noise floor) and is applied LAST, to footage + bars + text together.
fx:
  glow: {...unchanged...}
  scan: {...unchanged...}
  snow: {...unchanged...}
  heat:
    enable: true
    rms_frac_w: 0.00133         # RMS displacement / frame width -> 1.44 px @1080
    xscale: 2.0                 # normalised, resolution-independent; do NOT rescale
    tscale: 0.5                 # Node "Speed 0.50"
    scroll_v: -0.004            # Node "Direction 0 x -125"
    octaves: 6
    persistence: 0.6
    supersample: 3              # displace is integer-px; 3 -> 1/3 px steps
    perlin_centre: 130.26       # measured, at xscale 2.0
    perlin_sd: 18.4             # measured, at xscale 2.0
    seed_x: 11
    seed_y: 77
    order: last                 # after glow + scan + snow, per Node's serial order
```

`build_bars.py` needs the block inserted between the snow blend
(`[s2][pt]blend=...`) and the `[full][bandfx]overlay=` line — the band crop there
already contains the bars and text, so nothing further is required to make the
captions shimmer with the footage.

### Deliverables

| file | what |
|---|---|
| `fx/heat/AB_heat_off_on.mp4` | 10 s, heat off │ heat on at the recommended setting, side by side |
| `fx/heat/AB_heat_off_on_slow2x.mp4` | the same at 2× slow motion |
| `fx/heat/AB_heat_caption_zoom.mp4` | 3× zoom on the caption block only, off above / on below |
| `fx/heat/AB_heat_caption_zoom_slow2x.mp4` | the same at 2× slow motion |
| `fx/heat/xt_amplitude_ladder.png` | y-t slice through both caption bars at 7 amplitudes |
| `fx/heat/xt_recommended.png` | y-t slice: off / recommended / integer-displace / tutorial xscale |
| `fx/heat/xt_edge_displacement.png` | the measured displacement field itself, refs vs controls |
| `fx/heat/cmp_amp_tearing.png` | stills at 0 / 3 / 6 / 8 — the pill survives all of them |
| `fx/heat/heatfx.sh` | the parameterised test harness (`HEAT AMP SS XSCALE TSCALE SCROLL ORDER`) |
| `fx/heat/edge2.py`, `rd.py`, `run3.py` | the sub-pixel edge-displacement measurement |
| `fx/heat/render_1080_heat_off.mp4` / `_on.mp4` | the recommended config rendered on the real 1080x1920 canvas, band 1080x608 @ y=656 |
| `fx/heat/AB_1080_caption_zoom.mp4` (+ `_slow2x`) | 1080 canvas, 2x zoom on the caption block, off above / on below |
