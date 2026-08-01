# Deferred pipeline optimizations

All three items in the first edition of this file are **DONE** as of
2026-08-01, together with three further wins that were not in it. What
remains open is at the bottom.

Numbers below are from `sources/wawassayna/ahqaf-15-bars.yaml` (23.3s reel,
4 cards, 1080p/25fps source, 699 output frames) on a 14-core Apple silicon
machine, with `ffmpeg` 8.1.2, unless a line says otherwise. Reference profile
of that reel as it stood before any of this:

| run | wall | CPU | peak ffmpeg RSS |
|---|---|---|---|
| bars, full fx | 161.8s | 500.8s | 3553 MB |
| bars, `heat: false` | 73.3s | 392.7s | 3472 MB |
| bars, all fx off | 24.3s | 140.0s | 2927 MB |
| bars, all fx off, 1 card | 21.1s | 117.5s | 2090 MB |

So `heat` was 55% of wall but only 22% of CPU; the other five effects were
50% of CPU; and ~2 GB of RSS was present before any effect ran at all.

---

## Done

### 1. Bars caption layers at full canvas — DONE, bit-identical

`draw_layers` emitted two 1080x1920 RGBA PNGs per phrase, fed as `-loop 1`
inputs and composited over the whole clip with no `enable` gate, then
overlaid a second and third time onto full-canvas black plates for barglow
and textglow. The ink is about 680x270 px, 11x smaller than the canvas.
Measured by differencing the 1-card and 4-card no-fx runs above: **7.7s CPU
and ~280 MB RSS per caption card**, linear in card count.

Now `render_bars.trim_to_ink` crops each PNG to its ink bbox (outward
even-coordinate snap, ported from `render_default.trim_to_ink`), every
consumer places it at `(x, y)`, and all three overlays carry
`enable='between(t,...)'` widened by one frame either side of the card's own
alpha window.

The wipe mask stays a **full-canvas** plate and is `crop`ped to the bar
layer's box. Its sweep expression is in canvas columns and the feather blur's
boundaries are part of the look; a crop is exact, so the mask the layer sees
is unchanged. Only the first card wipes, so the full-canvas plates cost this
buys back are one card's worth.

**Verified:** `sources/gt9y-QGgMsA/hadid-16-16-bars.yaml` (8 cards), with the
pill colour pinned so #2 could not contaminate the measurement, rendered
**PSNR y:inf u:inf v:inf** against the pre-change render. Bit-identical.

### 2. `derive_bar_color` grades every frame to sample 25 — DONE, 1 LSB

`fps=1` now runs first instead of after the lanczos scale and the grade.
**8.57s -> 1.89s CPU (-78%)**, scaling with source resolution and length.

As predicted, `vignette` dithers per frame, so a different frame count
advances its PRNG differently: the auto-derived pill on hadid-16-16 moved
`#8B6E23 -> #8B6D23`. One LSB on one channel of a colour that is a heuristic
to begin with. Pin `bar_color:` in the config to skip the pass entirely.

### 3. `trim_media` encodes a throwaway intermediate at `slow`/crf 16 — DONE

Now `veryfast`/crf 10. Measured against a **lossless** cut of the same 27.5s
1080p window, which is the comparison the first edition of this file did not
make:

| settings | CPU | size | PSNR vs lossless |
|---|---|---|---|
| `slow` / crf 16 (was) | 71.0s | 6.4 MB | 50.14 dB |
| `veryfast` / crf 14 (proposed) | 20.5s | 7.4 MB | **49.46 dB** |
| `veryfast` / crf 10 (now) | 25.1s | 14.8 MB | **50.39 dB** |

**The `veryfast`/crf 14 this file originally proposed is worse**, and the
reasoning that recommended it — "produces a *larger*, i.e. higher-fidelity,
intermediate" — is wrong: file size does not track fidelity across presets.
crf 10 is -65% CPU and genuinely better than what it replaced, and the extra
8 MB lives in `/tmp` for the duration of one render.

### Also done, and not in the first edition

- **`heat`'s two perlin maps are pre-baked and cached** (`heat_layers`).
  This file said "no way to make it cheaper without changing the look",
  reading `heat` as 55% of wall / 22% of CPU as inherent. It is not: `perlin`
  is single-threaded and cost 66s per map per reel while depending on nothing
  but its own constants. Baked in 30s buckets to a machine-level cache, they
  are computed once ever. 47.1 dB.
- **Wide gaussians at half linear size** (`fx.blur`, sigma >= 20), and the
  bar glow blurs a band-sized slice plus a 3-sigma margin rather than the
  full 1920-tall plate. This file warned the slice was "not free" because
  gblur is an IIR approximation whose boundary behaviour would change; tested
  in isolation on a synthetic plate it is **bit-identical**, because the
  plate is black outside the band and 3 sigma of margin is past anything that
  can reach it. 53.1 dB for the pair.
- **`heat` supersample 3 -> 2**. 48.8 dB.

### Where that leaves it

Two reels, measured on a 4P+4E machine. They are DIFFERENT clips, so read
each column down, never across:

`sources/gt9y-QGgMsA/waqiah-83-87-bars.yaml` — 26.4s, 5 cards:

| | wall |
|---|---|
| before any of this | 372s |
| + baked heat maps | 253s |
| + half-res gaussians, band-sliced bar glow | 185s |
| + heat supersample 3 -> 2 | 150s |

`sources/gt9y-QGgMsA/hadid-16-16-bars.yaml` — 27.5s, 8 cards, picked up
from where that left off:

| | wall | CPU | peak RSS |
|---|---|---|---|
| after heat bake + blurs + supersample | 170.7s | 724.2s | 4685 MB |
| after items 1-3 | 117.7s | 525.8s | 2517 MB |
| + quarter-res blurs, shrunk glow plates | **95.9s** | **459.1s** | **2212 MB** |
| (for reference: items 1-3 with `fx: {heat: false}`) | 86.5s | 377.0s | 2346 MB |

PSNR of the second row against the first is 52.93 dB (min 51.05), all of it
item 3's intermediate plus item 2's one LSB — item 1 contributes nothing,
having been measured at inf on its own. The third row is 53.23 dB against
the second, all of it the quarter-res step; the plate shrink measured inf.
Note the fourth row: `heat` is ~27% of the render now, not the ~55% of wall
it was before the maps were baked, so the docs' old "heat halves the render
time" was out of date and has been corrected.

### Where the remaining 95.9s goes

Measured by switching stages off, on the same reel:

| | wall | share |
|---|---|---|
| floor — trim, audio, caption PNGs, source decode, final encode | 35.2s | 37% |
| glow + barglow + textglow + scan + snow | ~30s | 31% |
| heat | ~31s | 32% |

Inside the floor: `trim_media` is 3.1s, the snow bake 2.8s (and cached after
the first render of a reel). Neither is worth chasing — see below.

---

## Checked and deliberately not proposed

- **`-thread_queue_size 4096`** (`pipeline/fx.py`). Looked like the memory
  hog; it is not. 4096 -> 64 moved peak RSS by 24 MB (3635 -> 3611) and the
  output md5 was unchanged. Leave it alone.
- **`scrim_plate`'s pure-Python nested pixel loop** (`render_bars.py`): 0.10s.
- **`align_words`' Needleman-Wunsch** (`generate.py`) recomputes
  `quran.skeleton` inside the O(n*m) inner loop. Milliseconds at real reel
  sizes (~50x60 cells); worth precomputing only if reels get much longer.

---

## Still open

### Push `-ss`/`-t` into the render inputs and delete `trim_media` — NOT WORTH IT

This was billed as the bigger version of item 3. Measured after item 3
shipped, it is not: the trim now costs **3.1s wall** (20.8s CPU) of a 95.9s
render, and seeking into the 33-minute original is actually *cheaper* to
decode than the intermediate (0.40s vs 0.63s for the same window, because
crf 10 is a higher bitrate than the source at that point). Deleting it buys
~4s for a five-call change that needs A/V sync verified at every seek. Left
undone deliberately; revisit only if the trim's cost grows.

### A golden parity gate that lives in this repo

CLAUDE.md invariant #2 is **suspended**: the graph is deliberately no longer
byte-identical to `legacy/tests/golden/at-tawbah-128-128/filtergraph.txt`,
and the golden lives under read-only `legacy/`, so there is nothing to
re-bless. Until a fixture exists outside `legacy/`, the rule is enforced by
re-rendering a known reel after any `render_bars.py`/`fx.py` edit and PSNR-ing
it against the previous render (below ~45 dB is a look change and needs a
decision).

The script that verified the original byte-identical pass was written and
used but never committed. It rebuilds the at-tawbah-128-128 graph (phrases
from `legacy/clips/at-tawbah-128-128/clip.yaml`, `x_offset=-216`,
`dur=23.720`, crop `48,30,1280x720`, tint `(191,140,54)` recovered from the
golden's glow scalars via `rr/0.35*255`, all switches on, `sig_path=None`)
and diffs it against the fixture. Committing a version of it that records a
*current* graph as the fixture is what restores the invariant.
