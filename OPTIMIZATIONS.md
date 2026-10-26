# Pipeline render cost profile

Read the rejected list before proposing anything. Numbers: `sources/hajri-23-taraweeh/hujurat-4-5-*.yaml` (26.2s, 5 cards, 1920x1080 source) -- the one source with all three styles cut from it, so the rows below are one reel three ways.

## Current cost

| | wall | CPU |
|---|---|---|
| `bars` (GPU), heat + snow cached | **~41s** | ~19s |
| `bars --vertical` (GPU), heat + snow cached | **~18s** | ~16s |
| `horizontal` (GPU) | **~6.0s** | ~3.3s |
| `horizontal --vertical` (GPU) | **~5.9s** | ~3.2s |
| `vertical` (GPU) | **~6.2s** | ~3.3s |

Apple M1 (4 performance + 4 efficiency cores, 16 GB), ffmpeg 9.0.2, every render paying its own `trim_media` cut. MEASURE THESE COOLED AND ALONE: medians of runs with 60s idle before each. The renderer is GPU-bound, so it does not have the scheduler spread a CPU render has here (an ffmpeg bars render spread +-20% run to run on this machine: the scheduler puts its threads on efficiency cores whenever the desktop wants a performance one). The same graphs run through ffmpeg 9.0.2 + x264 crf 18 / faster, before render_gpu replaced it, measured on this machine: bars 164s / 851s CPU, bars --vertical 61s / 314s, horizontal 12.4s / 70s, horizontal --vertical 7.4s / 39s, vertical 13.3s / 77s.

The bars rows are medians of 2 alternating GPU/ffmpeg runs with 60s idle before each, snow cached (2026-10). Longer reels scale with duration: `fatihah-1-1-7-bars --vertical` (52.5s, the longest config) 33.0s / 20.1s CPU on the GPU against 127.6s / 638s through ffmpeg, `shuara-26-105-111-bars --vertical` (41.6s) 27.6s / 13.6s against 102.1s / 498s. The GPU renderer is GPU-bound (~19ms of GPU per portrait frame, ~44ms landscape, CPU mostly idle), so its numbers do not have the ffmpeg path's scheduler spread.

The text rows are medians of 2 alternating GPU/ffmpeg runs with 60s idle before each (2026-10; CPU is user time). `ahzab-56-56` (30.0s, `YkXjYyKwHJ4`): horizontal 6.7s / 3.7s against 13.3s / 79s, `--vertical` 6.6s / 3.6s against 7.9s / 43s, vertical (hand-set 9:16 crop) 6.7s / 3.7s against 13.6s / 78s. About 4s of a text render is `tools/render_gpu` itself, of which the GPU is busy ~2.7s; the rest is the stages around it (loudnorm measure, layout, the mux).

A first run on a machine adds one 60s perlin bake per axis plus the snow bake. The two axis bakes run CONCURRENTLY (`perlin` is single-threaded; 1.93x measured on a 4s-span pair), so the pair costs one bake's wall: 8.4s per map-second at 1920x1080 here (4s span: 33.7s wall, 56s CPU), ~8.5min for 60s. Paid once per machine, never per reel; a portrait render reads the same maps. The snow loop is baked per bar colour and canvas: 11.0s at 1920x1080, 4.7s at 1080x608.

`bars` carries the whole FX stack over the full frame, which is where the gap comes from -- and why `--vertical` composes it at the 1080x608 the portrait file shows rather than at 1920x1080 scaled down. Both baked layers live under `tools/cache/` -- perlin heat maps in `heat/`, the snow loop in `snow/` -- outside `/tmp` so a sweep cannot bill either bake twice, and any heat map at least as long as the reel is reused verbatim: perlin is a deterministic field over (x, y, t) where `-t` only decides where to stop reading, so a long map's opening frames are bit-identical to a short bake (framemd5, and a whole reel re-rendered off a 60s map instead of a 30s one hashes the same). Only a reel longer than every cached map pays perlin, at ~6.3s of wall per second of map on the 14-core machine (`render_bars.HEAT_BAKE_RATE`, which the bake's printed ETA uses; 8.4s on the M1). Pin `fx: {heat: false}` for timing previews (~27% cheaper, and never judge LOOK without the full stack).

`generate.py --vertical` adds NO pass: the scale/pad runs inside the style's own graph (`render_common.PORTRAIT_PAD`; bars only pads), so the portrait file is the same single encode. Measured on the 26.2s hujurat `horizontal` reel against a landscape render followed by `letterbox.py` and a tag remux: 7.9s against 11.5s wall, 70s against 115s CPU -- the final x264 is also cheaper because two thirds of the portrait canvas is black -- at 51.0 dB against the letterboxed file, one lossy generation closer to the render. The mp4 tags are written by the delivery encode too, so the finished file is never rewritten.

## Techniques in place

- **Every style renders on the GPU** (`tools/render_gpu`, `pipeline/gpu/render_gpu.swift`): the style's own graph interpreted in Metal, exact ports of each ffmpeg filter. Text styles: every delivered frame of hujurat-4-5 vertical / horizontal / horizontal `--vertical` (786 each) and ahzab-56-56 horizontal / `--vertical` / vertical (900 each) is framemd5-identical to the ffmpeg graph's at `-r 30`; VideoToolbox at `render_common.VT_QUALITY` 0.76 reads 45.9-52.0 dB PSNR-Y against the lossless frames where x264 reads 45.1-51.5 (0.1-0.9 dB above it on all six, files 28-75% larger: VideoToolbox spends bits on a mostly static picture less well than x264). 1.2x less wall on `horizontal --vertical`, ~2x on the full-canvas reels, 12-23x less CPU. Bars: every frame of all four measured reels (hujurat-4-5 portrait + landscape, fatihah-1-1-7, shuara-26-105-111) is bit-identical to the ffmpeg graph's before encode; picture encoded at `VT_QUALITY` 0.68, delivered PSNR-Y against the lossless frames 45.9-51.0 dB where x264 crf 18 / faster reads 46.0-51.4, within 0.35 dB either way. 3.3-4.0x less wall, 20-45x less CPU. Inside it: gblur's recursive line passes on a transposed buffer (each SIMD group walks memory its threads share), the running value in a register and loads issued 8 ahead, 32-thread groups so few lines still spread over every core -- 36.8 -> 16.5s of GPU on hujurat portrait, bit-identical. Barriers only before a dispatch that touches a buffer bound since the last one (15.1 -> 14.9s, within noise; removing every barrier measured no faster, so the remaining cost is the ~276 kernels per frame themselves).

Changing one of these needs the guard it was verified with: `tests/gpu_parity.py` (exact frames) for anything in the renderer, PSNR + stills before `--bless` for anything that moves a pixel, `tests/graph_parity.py --bless` for a bars graph edit, `tests/wrap_parity.py` for the English wrap.

- **Ink-cropped + `enable`-gated overlays** (bars + text captions/signature). Bit-identical vs full canvas. Text style isolated: 4.08→1.64s wall, 1618→633 MB. Signature: 13.1→10.1s CPU, 371→220 MB.
- **Grade plate and signature are single-frame inputs** (text styles). Only the cards are looped, because only they fade; overlay's `eof_action=repeat` holds the other two for the whole reel -- same pixels, no per-frame PNG decode.
- **`bars --vertical` composes at 1080x608** (`render_bars.geometry`): every px constant times 1080/1920, then a pad -- no 1920x1080 composite scaled down. hujurat-4-5: 222s -> 78s wall, 1006 -> 366s CPU (2.8x), PSNR-Y 41.0 dB whole frame / 46.3 dB on the caption-free top 100 rows against the 1920x1080 composite; fatihah-1-1-7 411 -> 148s (38.7 / 47.2 dB), shuara-26-105-111 362 -> 131s (36.2 / 47.4 dB). The dB gap is type and pill edges, a touch softer; the owner approved the look on stills. Kept as measured: heat supersample 2 (1 at this size is 36.6 dB, visibly different), the pill colour sampled at 1920x1080 (sampling the small picture moves it, #AD7047 -> #AE7247), the line split measured at 1920 (a portrait file breaks where the landscape one does), and the 1920x1080 heat maps scaled in-graph (perlin is normalised to the frame, so it is the same field; a native 1080x608 bake read 41.03 against 41.05 dB at the same render cost).
- **`horizontal --vertical` composes at 1080x608** (`render_text.geometry`): every px constant times 1080/1920, English wrapped on the 1920 fonts, then a pad. hujurat-4-5 through ffmpeg: 10.9 -> 7.6s wall, 54.6 -> 40.0s CPU; against the 1920x1080 composite scaled down, PSNR-Y 33.6 dB whole frame / 56.4 dB on the caption-free top 100 rows (ahzab-56-56 31.0 / 55.5). The gap is type position and size rounding (Arabic 40pt against 40.5 effective); stills compare the same.
- **Wipe mask over the bar box** plus 16 sigma of the feather blur each side, not the canvas -- framemd5-identical, landscape and portrait.
- **PNG plates held by the `loop` filter**, not `-loop 1` (which re-decodes the PNG every frame) -- framemd5-identical. With the wipe mask: 6 alternating pairs of the hujurat landscape encode alone, 216 -> 184s wall, 1001 -> 897s CPU (every pair lower, by 30-204s CPU).
- **`trim_media` cached** under `tools/cache/trim`, keyed on the source and ffmpeg binary (path, size, mtime) and the whole argv; the cut is deterministic (same md5 across runs and against an uncached cut). A re-render whose window has not moved saves the cut: 5.4-6.6s wall, ~32s CPU on the 26.2s hujurat window -- a quarter of a text-style render.
- **`derive_bar_color`: `fps=1` first** — 8.57→1.89s CPU. Pin `bar_color:` to skip (vignette dither can move one LSB).
- **`trim_media`: `veryfast`/crf 10** — 50.39 dB vs lossless. crf 14 is *worse* (49.46); size ≠ fidelity across presets.
- **`heat` perlin maps pre-baked** (`heat_layers`, longest-map reuse) — 47.1 dB.
- **`fx.blur` half/quarter-res** (`HALF_RES_SIGMA` 10, `QUARTER_RES_SIGMA` 40). Bar glow band-sliced (+3σ). Scan/textglow via `blur`: −58% CPU at 55.6/54.9 dB. Heat supersample = 2 (48.8 dB). Snow bake atomic + shared cache.
- **bars carries no letterbox plumbing** — with the picture filling the canvas there is no `pad`, no crop back to a band, and no full-frame overlay putting it back; the FX chain runs on the captioned frame.
- **One text renderer, no render-time detection** — `vertical` from a landscape source composites its `crop` inside the single graph instead of writing a re-encoded 9:16 intermediate first. No OpenCV/ONNX at render time either: the framing is a committed number.
- **The balanced English wrap is SOLVED, not enumerated** (`wrap_english`). Two bottleneck passes over one width table -- minimise the widest line at the first-fit line count, then maximise the narrowest -- give the split enumeration gives, because the optimum is exactly the splits whose every line falls in the resulting band and walking it from the left takes the earliest break. Identical on 36 336 real cards, 2.2s against enumeration's 305.5s over that corpus. Enumeration is `C(words-1, lines-1)` and does not return AT ALL past about six lines, which `auto_group` reaches on its own when the reciter does not pause inside a verse: 2:164 as one card is 2.4e12 candidates.
- **`font.getlength` cached per (font, character)** — every English width in render_text is a sum of per-character advances over an alphabet of a few dozen glyphs, re-measured for every line of every card by the wrap, by `draw_en_line` and again by `backing_ellipse`.
- **`render_bars.layout` splits each phrase once** — `phrase_lines` shapes every candidate split to measure it: 180 shaping calls where asking twice per phrase costs 362.

The last three are NOT why a render is fast. `layout` costs the hujurat reels 1.8ms (bars), 4.3ms (horizontal) and 3.8ms (vertical) -- single-digit milliseconds against 8 to 72 seconds of ffmpeg, and the table above cannot see them. What the wrap solve buys is that every card returns at all; the rest is the cost of a draft loop and of anything that calls layout in bulk.

      tools/render-venv/bin/python tests/graph_parity.py
      tools/render-venv/bin/python tests/wrap_parity.py

## Start-up cost, outside the render

What the stages around the render pay before any pixel moves.

- **`fetch.py`** — a source that is already complete (a usable `source.mp4` plus `captions.srt` OR the `captions.none` sentinel) never calls yt-dlp at all, so a re-fetch costs no network round trip and cannot fail the bot check for nothing. Most recitations have no Arabic auto-captions, which is why the sentinel exists. A fresh fetch makes ONE player-API request: its JSON is handed back (`--load-info-json`) for the media and the captions.
- **`crop.py`** — the cache is read BEFORE any frame is extracted, so a cached geometry re-solve runs no ffmpeg at all: 0.13s against 0.65s for a 4-frame solve. A cached `--annotate` extracts only the one frame it draws.
- **`generate.py --verify-only`** — the verification block without the render, off a 32px-wide cut of the window: ~1.7s wall / ~6s CPU against an 8s `vertical` render, and the block is the only thing a split check reads.
- **`transcribe.py`** — the interpreter probe asks `importlib.util.find_spec` instead of importing the backend: 0.02s per candidate against 0.9s warm and 3.1s cold for `import mlx_whisper`.
- **`align.py`** — takes several configs per invocation, so the 7.1s of start-up (1.0s torch, 4.9s the aligner package, 1.3s the MMS weights) is paid once for every reel cut from one source rather than once per reel.
- **`publish.py`** — one `ffprobe` for the tags, the frame size and the duration `clamp_cover` needs.

## Rejected

- **render_gpu: 16-wide gblur load batches** instead of 8: 16.5 -> 16.9s. **4-wide blend/lut** threads: 15.1 -> 14.7s, kept (no division in the blend either: `x/255` as `(x*32897)>>23`, exact over 0..65025).

- **`scrim_plate` pixel loop**: 0.10s.
- **`align_words` skeleton precompute**: ms at ~50×60.
- **`heat` lutrgb before 2× upscale**: *worse* (28.4→29.2s CPU).
- **Audio analysis passes**: 0.10s together.
- **`align.py` memory**: already windowed; long auto-trim is a correctness hazard anyway.
- **Letterbox pass at `slow`**: 54.97→58.32 dB vs the padded source for 10.9→17.5s CPU. Both are far above the 45 dB floor, and the input is already a crf-18 encode of the final pixels — the dB buys fidelity to a finished encode, not to the render.
- **Scan's tint folded into its final `lutrgb`**: not identical in general -- `colorchannelmixer` rounds `lrintf` in float (half to even), `lutrgb`'s `round()` is double, half away from zero, so identity depends on the tint; it was worth ~6% of the glow+scan subset.
- **Snow bake in a fork pool**: byte-identical and 8.8 -> 4.2s wall, but +65% CPU, once per bar colour (4.7s serial at the portrait canvas), and `fork` after importing Pillow on macOS is the hazard CPython moved its default off.
- **Native 1080x608 heat bake for portrait**: the same field as the 1920 maps scaled (see above), no closer to the landscape look, same render cost, plus a ~3min bake and 117 MB.
- **Delete `trim_media`**: ~3.1s wall; seek into long source is cheaper to decode than the intermediate. Not worth the A/V-sync surface.

## Open

**render_gpu kernel fusion** -- the GPU renderer spends its ~19ms/frame of bars across ~276 small dispatches, none above ~10% of the total; fusing per-pixel chains (mixer -> lut -> scale, the blend ladder) is the next lever, each fusion checked bit-exact with `--tap` and `tests/gpu_parity.py`.

**render_gpu: the portrait pad's black rows** -- a `--vertical` text render pads rgba to 1080x1920 and then converts all of it to yuv420p (`k_rgb2y`, `k_rgb2uv` and their scalers are ~1.5s of the 2.7s GPU on hujurat), though two thirds of the frame is one constant colour. Converting only the band and filling the rest is unmeasured; it must stay framemd5-identical.

**`heat` supersample 2→1** — look decision; no cheap refactor left. **bars FX sigmas at 1920x1080 (landscape)** — they were tuned when a blur covered a 1080x608 band, which is the canvas `--vertical` composes on again; whether any can drop a resolution tier on the landscape frame is unmeasured.
