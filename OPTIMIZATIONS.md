# Deferred pipeline optimizations

Three measured CPU/memory wins that were left out of the byte-identical
optimization pass, because each one either moves a pixel or breaks a frozen
fixture. All three are owner decisions, not refactors.

Numbers below are from `sources/wawassayna/ahqaf-15-bars.yaml` (23.3s reel,
4 cards, 1080p/25fps source, 699 output frames) on a 14-core Apple silicon
machine, with `ffmpeg` 8.1.2. Reference profile of that reel:

| run | wall | CPU | peak ffmpeg RSS |
|---|---|---|---|
| bars, full fx | 161.8s | 500.8s | 3553 MB |
| bars, `heat: false` | 73.3s | 392.7s | 3472 MB |
| bars, all fx off | 24.3s | 140.0s | 2927 MB |
| bars, all fx off, 1 card | 21.1s | 117.5s | 2090 MB |

So `heat` is 55% of wall but only 22% of CPU; the other five effects are 50%
of CPU; and ~2 GB of RSS is present before any effect runs at all.

---

## 1. Bars caption layers at full canvas (largest win)

`draw_layers` emits two 1080x1920 RGBA PNGs per phrase
(`pipeline/render_bars.py`), fed as `-loop 1` inputs and composited over the
whole clip with no `enable` gate, then overlaid a second and third time onto
full-canvas black plates for barglow and textglow (`pipeline/fx.py`). The ink
is about 680x270 px, 11x smaller than the canvas.

Measured by differencing the 1-card and 4-card no-fx runs above:
**7.7s CPU and ~280 MB RSS per caption card**, linear in card count. An
8-card reel spends ~60s CPU and ~2.2 GB RSS compositing transparent pixels.

**Fix.** Crop each PNG to its ink bbox and overlay at `(x, y)`; add
`enable='between(t,...)'` to the composite overlays, as `render_default.py`
already does. `render_default.trim_to_ink()` is the same idea and is the
model to copy, including its outward even-coordinate snap: `overlay` blends
in yuv420, so an odd edge lands the overlay's 2x2 chroma/alpha blocks on a
different grid.

**Why deferred.** The output is pixel-identical, but the emitted filtergraph
string changes, and `render_bars.py` + `fx.py` are under a byte-identity
contract with `legacy/tests/golden/at-tawbah-128-128/filtergraph.txt`
(CLAUDE.md invariant #2). Re-blessing that fixture is an owner decision.

**Verifying it.** Render `ahqaf-15-bars` before and after and compare md5
(baseline `8ca19813288c46790cb0ada3a5d43f8d`); only regenerate the golden
once the md5 matches. Note the intricate parts: the wipe mask is a
full-canvas plate that must be cropped to match its bar layer, and the
barglow plate's `gblur sigma=90` currently runs on the full 1920-tall plate
before being cropped to the band, so shrinking the plate is *not* free (ffmpeg's
gblur is an IIR approximation and its boundary behaviour would change).

---

## 2. `derive_bar_color` grades every frame to sample 25 of them

`fps=1` sits *after* the lanczos scale and the full grade in
`pipeline/render_bars.py`, so 24 of every 25 frames are scaled and graded and
then discarded. Moving `fps=1` to the front of the chain:
**8.57s -> 1.89s CPU (-78%)** on this clip. The pass scales with source
resolution and clip length.

**Why deferred.** `vignette` dithers per frame, so feeding it 24 frames
instead of 581 advances its PRNG differently. Measured: the auto-derived pill
moves `#808B23` -> `#818B23`. One LSB on one channel, but it is a look
change.

**Options.** Accept the 1 LSB; or add `dither=0` to the sampling chain only
(which also differs from today, but is at least deterministic); or set
`bar_color:` in the config, which skips the pass entirely and is free.

---

## 3. `trim_media` encodes a throwaway intermediate at `-preset slow -crf 16`

`pipeline/generate.py` cuts the trim window into an intermediate that the
renderer re-encodes at crf 18 minutes later. On a 20s 1080p cut:

| settings | CPU | intermediate size |
|---|---|---|
| `slow` / crf 16 (current) | 37.1s | 12.0 MB |
| `veryfast` / crf 14 | 11.0s | 14.8 MB |
| `ultrafast` / crf 12 | 6.6s | 69.8 MB |

That is more CPU than the entire all-fx-off render. `veryfast`/crf 14 is
**-70% CPU** and produces a *larger*, i.e. higher-fidelity, intermediate, so
this is arguably a quality gain rather than a loss.

**Why deferred.** Different intermediate compression shifts the final pixels.

**Bigger version.** Delete the pass entirely and push `-ss`/`-t` into the
render inputs; `Graph.input` already accepts both. That removes an encode
*and* a decode, but it is a multi-file change (the bar-colour sample,
loudnorm and wav extract all currently receive the pre-trimmed file) and
needs A/V sync verified across the seek.

---

## Checked and deliberately not proposed

- **`-thread_queue_size 4096`** (`pipeline/fx.py`). Looked like the memory
  hog; it is not. 4096 -> 64 moved peak RSS by 24 MB (3635 -> 3611) and the
  output md5 was unchanged. Leave it alone.
- **`scrim_plate`'s pure-Python nested pixel loop** (`render_bars.py`): 0.10s.
- **`align_words`' Needleman-Wunsch** (`generate.py`) recomputes
  `quran.skeleton` inside the O(n*m) inner loop. Milliseconds at real reel
  sizes (~50x60 cells); worth precomputing only if reels get much longer.
- **`heat`**: no way to make it cheaper without changing the look. It already
  has a switch and the README already names it as the preview drop.

---

## Golden parity gate

There is no in-repo script for the check CLAUDE.md invariant #2 requires
after any edit to `render_bars.py` or `fx.py`. One was written and used to
verify the byte-identical pass; it rebuilds the at-tawbah-128-128 graph
(phrases from `legacy/clips/at-tawbah-128-128/clip.yaml`, `x_offset=-216`,
`dur=23.720`, crop `48,30,1280x720`, tint `(191,140,54)` recovered from the
golden's glow scalars via `rr/0.35*255`, all switches on, `sig_path=None`)
and diffs it against the fixture. Worth committing before attempting #1.
