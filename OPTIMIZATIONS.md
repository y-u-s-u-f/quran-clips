# Pipeline render cost profile

Read the rejected list before proposing anything. Numbers:
`sources/gt9y-QGgMsA/hadid-16-16-bars.yaml` (27.5s, 8 cards, 4P+4E) unless
noted; also `sources/hajri-23-taraweeh/hujurat-4-5-*.yaml` (26.2s, 5 cards,
1920x1080 source) for the current per-style comparison.

## Current cost

| | wall | CPU |
|---|---|---|
| `bars`, full fx, heat maps cached | **~75s** | ~560s |
| `bars`, first run on a machine (heat bake) | ~460s | ~1200s |
| `horizontal` | ~11s | ~107s |
| `vertical` | ~12s | ~121s |

`bars` is 3.16x the pixels of the other two styles per frame and carries the
whole FX stack, which is where the gap comes from. The perlin heat maps are
cached in `tools/cache/heat/`, outside `/tmp` so a sweep cannot bill the bake
twice, and any map at least as long as the reel is reused verbatim — perlin
is a deterministic field over (x, y, t) where `-t` only decides where to stop
reading, so a long map's opening frames are bit-identical to a short bake
(framemd5, and a whole reel re-rendered off a 60s map instead of a 30s one
hashes the same). Only a reel longer than every cached map pays perlin, at
~9.3s of wall per second of map per axis. Pin `fx: {heat: false}` for timing
previews (~27% cheaper, and never judge LOOK without the full stack).

`generate.py --vertical` adds one x264 pass (`veryfast`, crf 18, audio
stream-copied) over the finished file: **~3s wall / ~11s CPU** on a 21.2s
1920x1080 reel.

## Techniques in place

Changing these needs PSNR; bars graph edits also need
`tests/graph_parity.py --bless`.

- **Ink-cropped + `enable`-gated overlays** (bars + text captions/signature).
  Bit-identical vs full canvas. Text style isolated: 4.08→1.64s wall,
  1618→633 MB. Signature: 13.1→10.1s CPU, 371→220 MB.
- **Wipe mask stays full-canvas**, then cropped to the bar box (sweep in
  canvas columns; feather is part of the look).
- **`derive_bar_color`: `fps=1` first** — 8.57→1.89s CPU. Pin `bar_color:`
  to skip (vignette dither can move one LSB).
- **`trim_media`: `veryfast`/crf 10** — 50.39 dB vs lossless. crf 14 is
  *worse* (49.46); size ≠ fidelity across presets.
- **`heat` perlin maps pre-baked** (`heat_layers`, longest-map reuse) — 47.1 dB.
- **`fx.blur` half/quarter-res** (`HALF_RES_SIGMA` 10, `QUARTER_RES_SIGMA`
  40). Bar glow band-sliced (+3σ). Scan/textglow via `blur`: −58% CPU at
  55.6/54.9 dB. Heat supersample = 2 (48.8 dB). Glow plates band-sized
  (bit-identical). Snow bake atomic + shared cache.
- **bars carries no letterbox plumbing** — with the picture filling the
  canvas there is no `pad`, no crop back to a band, and no full-frame
  overlay putting it back; the FX chain runs on the captioned frame.
- **One text renderer, no render-time detection** — `vertical` from a
  landscape source composites its `crop` inside the single graph instead of
  writing a re-encoded 9:16 intermediate first, so that whole decode+encode
  pass is gone. No OpenCV/ONNX at render time either: the framing is a
  committed number.

    tools/render-venv/bin/python tests/graph_parity.py

## Rejected

- **`-thread_queue_size 4096`**: 4096→64 moves RSS 24 MB; md5 unchanged.
- **`scrim_plate` pixel loop**: 0.10s.
- **`align_words` skeleton precompute**: ms at ~50×60.
- **Final encode `slow`→`medium`**: 1.4% CPU for a deliverable change.
- **`heat` lutrgb before 2× upscale**: *worse* (28.4→29.2s CPU).
- **Audio analysis passes**: 0.10s together.
- **`wrap_english` balance**: <1s/reel.
- **`align.py` memory**: already windowed; long auto-trim is a correctness
  hazard anyway.
- **Letterbox pass at `slow`**: 54.97→58.32 dB vs the padded source for
  10.9→17.5s CPU. Both are far above the 45 dB floor, and the input is already
  a crf-18 `slow` encode of the final pixels — the dB buys fidelity to a
  finished encode, not to the render.
- **Delete `trim_media`**: ~3.1s wall; seek into long source is cheaper to
  decode than the intermediate. Not worth the A/V-sync surface.

## Open

**`heat` supersample 2→1** — look decision; no cheap refactor left.
**bars at 1920x1080** — the FX stack now covers 3.16x the pixels per frame.
Nothing here is wrong, but the band-era sigmas were tuned when a blur covered
608 rows; whether any of them can drop a resolution tier at this size is
unmeasured.
