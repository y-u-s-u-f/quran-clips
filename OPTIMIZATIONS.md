# Pipeline render cost profile

Read the rejected list before proposing anything. Numbers:
`sources/hajri-23-taraweeh/hujurat-4-5-*.yaml` (26.2s, 5 cards, 1920x1080
source) -- the one source with all three styles cut from it, so the rows below
are one reel three ways.

## Current cost

| | wall | CPU |
|---|---|---|
| `bars`, full fx, heat + snow cached | **~75s** | ~552s |
| `bars`, first run on a machine | ~850s | ~1830s |
| `horizontal` | ~12s | ~105s |
| `vertical` | ~14s | ~119s |

ffmpeg 9.0.1, 14 cores. MEASURE THESE COOLED AND ALONE. Rendered back to back
they read 10-25% high -- a bars render is 550s of CPU across 14 cores, and the
next one starts on a hot machine -- which reads as a regression that is not
there. These are the medians of alternating runs with the machine idle between
them.

The first-run row is one 60s perlin bake per axis (1237s CPU together) plus
the snow bake (~12s / ~45s) plus the render, paid once per machine and never
per reel. The two axis bakes run CONCURRENTLY (`perlin` is single-threaded;
1.93x measured on a 4s-span pair), so the wall for the pair is one bake's,
~380s, not the 761s two sequential bakes cost.

`bars` carries the whole FX stack over the full 1920x1080 frame, which is
where the gap comes from. Both baked layers live under `tools/cache/` --
perlin heat maps in `heat/`, the snow loop in `snow/` -- outside `/tmp` so a
sweep cannot bill either bake twice, and any heat map at least as long as the
reel is reused verbatim: perlin is a deterministic field over (x, y, t) where
`-t` only decides where to stop reading, so a long map's opening frames are
bit-identical to a short bake (framemd5, and a whole reel re-rendered off a
60s map instead of a 30s one hashes the same). Only a reel longer than every
cached map pays perlin, at ~6.3s of wall per second of map
(`render_bars.HEAT_BAKE_RATE`, which the bake's printed ETA uses). Pin
`fx: {heat: false}` for timing previews (~27% cheaper, and never judge LOOK
without the full stack).

`generate.py --vertical` adds NO pass: the scale/pad runs inside the style's
own graph (`render_common.PORTRAIT_PAD`), so the portrait file is the same
single encode. Measured on the 26.2s hujurat `horizontal` reel against a
landscape render followed by `letterbox.py` and a tag remux: 7.9s against
11.5s wall, 70s against 115s CPU -- the final x264 is also cheaper because two
thirds of the portrait canvas is black -- at 51.0 dB against the letterboxed
file, one lossy generation closer to the render. The mp4 tags are written by
the delivery encode too, so the finished file is never rewritten.

## Techniques in place

Changing one of these needs the guard it was verified with: PSNR for anything
that moves a pixel, `tests/graph_parity.py --bless` for a bars graph edit,
`tests/wrap_parity.py` for the English wrap.

- **Ink-cropped + `enable`-gated overlays** (bars + text captions/signature).
  Bit-identical vs full canvas. Text style isolated: 4.08→1.64s wall,
  1618→633 MB. Signature: 13.1→10.1s CPU, 371→220 MB.
- **Grade plate and signature are single-frame inputs** (text styles). Only
  the cards are looped, because only they fade; overlay's
  `eof_action=repeat` holds the other two for the whole reel -- same pixels,
  no per-frame PNG decode.
- **Wipe mask stays full-canvas**, then cropped to the bar box (sweep in
  canvas columns; feather is part of the look).
- **`derive_bar_color`: `fps=1` first** — 8.57→1.89s CPU. Pin `bar_color:`
  to skip (vignette dither can move one LSB).
- **`trim_media`: `veryfast`/crf 10** — 50.39 dB vs lossless. crf 14 is
  *worse* (49.46); size ≠ fidelity across presets.
- **`heat` perlin maps pre-baked** (`heat_layers`, longest-map reuse) — 47.1 dB.
- **`fx.blur` half/quarter-res** (`HALF_RES_SIGMA` 10, `QUARTER_RES_SIGMA`
  40). Bar glow band-sliced (+3σ). Scan/textglow via `blur`: −58% CPU at
  55.6/54.9 dB. Heat supersample = 2 (48.8 dB). Snow bake atomic + shared
  cache.
- **bars carries no letterbox plumbing** — with the picture filling the
  canvas there is no `pad`, no crop back to a band, and no full-frame
  overlay putting it back; the FX chain runs on the captioned frame.
- **One text renderer, no render-time detection** — `vertical` from a
  landscape source composites its `crop` inside the single graph instead of
  writing a re-encoded 9:16 intermediate first. No OpenCV/ONNX at render
  time either: the framing is a committed number.
- **The balanced English wrap is SOLVED, not enumerated** (`wrap_english`).
  Two bottleneck passes over one width table -- minimise the widest line at
  the first-fit line count, then maximise the narrowest -- give the split
  enumeration gives, because the optimum is exactly the splits whose every
  line falls in the resulting band and walking it from the left takes the
  earliest break. Identical on 36 336 real cards, 2.2s against enumeration's
  305.5s over that corpus. Enumeration is `C(words-1, lines-1)` and does not
  return AT ALL past about six lines, which `auto_group` reaches on its own
  when the reciter does not pause inside a verse: 2:164 as one card is
  2.4e12 candidates.
- **`font.getlength` cached per (font, character)** — every English width in
  render_text is a sum of per-character advances over an alphabet of a few
  dozen glyphs, re-measured for every line of every card by the wrap, by
  `draw_en_line` and again by `backing_ellipse`.
- **`render_bars.layout` splits each phrase once** — `phrase_lines` shapes
  every candidate split to measure it: 180 shaping calls where asking twice
  per phrase costs 362.

The last three are NOT why a render is fast. `layout` costs the hujurat reels
1.8ms (bars), 4.3ms (horizontal) and 3.8ms (vertical) -- single-digit
milliseconds against 12 to 75 seconds of ffmpeg, and the table above cannot
see them. What the wrap solve buys is that every card returns at all; the
rest is the cost of a draft loop and of anything that calls layout in bulk.

      tools/render-venv/bin/python tests/graph_parity.py
      tools/render-venv/bin/python tests/wrap_parity.py

## Start-up cost, outside the render

What the stages around the render pay before any pixel moves.

- **`fetch.py`** — a source that is already complete (a usable `source.mp4`
  plus `captions.srt` OR the `captions.none` sentinel) never calls yt-dlp at
  all, so a re-fetch costs no network round trip and cannot fail the bot
  check for nothing. Most recitations have no Arabic auto-captions, which is
  why the sentinel exists. A fresh fetch makes ONE player-API request: its
  JSON is handed back (`--load-info-json`) for the media and the captions.
- **`crop.py`** — the cache is read BEFORE any frame is extracted, so a
  cached geometry re-solve runs no ffmpeg at all: 0.13s against 0.65s for a
  4-frame solve. A cached `--annotate` extracts only the one frame it draws.
- **`generate.py --verify-only`** — the verification block without the
  render, off a 32px-wide cut of the window: ~1.7s wall / ~6s CPU against a
  12s `vertical` render, and the block is the only thing a split check reads.
- **`transcribe.py`** — the interpreter probe asks
  `importlib.util.find_spec` instead of importing the backend: 0.02s per
  candidate against 0.9s warm and 3.1s cold for `import mlx_whisper`.
- **`align.py`** — takes several configs per invocation, so the 7.1s of
  start-up (1.0s torch, 4.9s the aligner package, 1.3s the MMS weights) is
  paid once for every reel cut from one source rather than once per reel.
- **`publish.py`** — one `ffprobe` for the tags, the frame size and the
  duration `clamp_cover` needs.

## Rejected

- **`-thread_queue_size 4096`**: 4096→64 moves RSS 24 MB; md5 unchanged.
- **`scrim_plate` pixel loop**: 0.10s.
- **`align_words` skeleton precompute**: ms at ~50×60.
- **Final encode `slow`→`medium`**: 1.4% CPU for a deliverable change.
- **`heat` lutrgb before 2× upscale**: *worse* (28.4→29.2s CPU).
- **Audio analysis passes**: 0.10s together.
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
**bars FX sigmas at 1920x1080** — they were tuned when a blur covered a
1080x608 band; whether any of them can drop a resolution tier on the full
frame is unmeasured.
