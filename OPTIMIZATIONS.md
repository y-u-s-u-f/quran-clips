# Pipeline render cost profile

Read the rejected list before proposing anything. Numbers:
`sources/gt9y-QGgMsA/hadid-16-16-bars.yaml` (27.5s, 8 cards, 4P+4E) unless
noted; also `sources/wawassayna/ahqaf-15-bars.yaml` (23.3s, 4 cards).

## Current cost (bars, full fx)

| | wall | CPU | peak RSS |
|---|---|---|---|
| full fx | **~96s** | **~460s** | **~2.2 GB** |
| `fx: {heat: false}` | ~87s | ~377s | ~2.3 GB |

~96s split: floor ~35s (37%), screen FX ~30s (31%), heat ~31s (32%).
`trim_media` ~3.1s, snow bake ~2.8s (cached) — neither worth chasing.
Heat is the preview drop (`fx: {heat: false}`); never judge LOOK without
the full stack. Lighter bars (ahqaf-15, heat cached): ~33s / ~229s CPU.
`hz` over same source: ~8s / ~72s CPU.

## Techniques in place

Changing these needs PSNR; bars graph edits also need
`tests/graph_parity.py --bless`.

- **Ink-cropped + `enable`-gated overlays** (bars + hz captions/signature).
  Bit-identical vs full canvas. hz isolated: 4.08→1.64s wall, 1618→633 MB.
  Signature: 13.1→10.1s CPU, 371→220 MB.
- **Wipe mask stays full-canvas**, then cropped to the bar box (sweep in
  canvas columns; feather is part of the look).
- **`derive_bar_color`: `fps=1` first** — 8.57→1.89s CPU. Pin `bar_color:`
  to skip (vignette dither can move one LSB).
- **`trim_media` / default 9:16 intermediate: `veryfast`/crf 10** — 50.39 dB
  vs lossless. crf 14 is *worse* (49.46); size ≠ fidelity across presets.
- **`heat` perlin maps pre-baked** (`heat_layers`, 30s buckets) — 47.1 dB.
- **`fx.blur` half/quarter-res** (`HALF_RES_SIGMA` 10, `QUARTER_RES_SIGMA`
  40). Bar glow band-sliced (+3σ). Scan/textglow via `blur`: −58% CPU at
  55.6/54.9 dB. Heat supersample = 2 (48.8 dB). Glow plates band-sized
  (bit-identical). Snow bake atomic + shared cache.

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
- **Delete `trim_media`**: ~3.1s wall; seek into long source is cheaper to
  decode than the intermediate. Not worth the A/V-sync surface.

## Open

**`heat` supersample 2→1** — look decision; no cheap refactor left.
