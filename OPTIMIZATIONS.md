# Pipeline render cost profile

Read the rejected list before proposing anything. Numbers:
`sources/hajri-23-taraweeh/hujurat-4-5-*.yaml` (26.2s, 5 cards, 1920x1080
source) -- the one source with all three styles cut from it, so the rows below
are one reel three ways. `sources/gt9y-QGgMsA/hadid-16-16-bars.yaml` (27.5s, 8
cards, 4P+4E) where noted; its media is not on this machine, which is why the
`bars` row is measured on hujurat now.

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
them; each style was rendered the same way at HEAD and the two agree within
the run-to-run spread, in both directions. They must: the two trees emit the
same filtergraph and the same argv apart from the snow input's path, and the
streams they produce are byte-identical (`framemd5` over video and audio).

The first-run row is one 60s perlin bake per axis (761s wall / 1237s CPU
together) plus the snow bake (~12s / ~45s) plus the render, paid once per
machine and never per reel.

`bars` is 3.16x the pixels of the other two styles per frame and carries the
whole FX stack, which is where the gap comes from. Both baked layers live
under `tools/cache/` -- perlin heat maps in `heat/`, the snow loop in `snow/`
-- outside `/tmp` so a sweep cannot bill either bake twice, and any heat map
at least as long as the reel is reused verbatim: perlin is a deterministic
field over (x, y, t) where `-t` only decides where to stop reading, so a long
map's opening frames are bit-identical to a short bake (framemd5, and a whole
reel re-rendered off a 60s map instead of a 30s one hashes the same). Only a
reel longer than every cached map pays perlin, at ~6.3s of wall per second of
map per axis -- `warn_heat_bake` quotes 9.3, so its ETA over-states. Pin
`fx: {heat: false}` for timing previews (~27% cheaper, and never judge LOOK
without the full stack).

`generate.py --vertical` adds one x264 pass (`veryfast`, crf 18, audio
stream-copied) over the finished file: **~1.6s wall / ~13s CPU** on the 26.2s
1920x1080 bars reel.

## Techniques in place

Changing one of these needs the guard it was verified with: PSNR for anything
that moves a pixel, `tests/graph_parity.py --bless` for a bars graph edit,
`tests/wrap_parity.py` for the English wrap.

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
- **The balanced English wrap is SOLVED, not enumerated** (`wrap_english`).
  Two bottleneck passes over one width table -- minimise the widest line at
  the first-fit line count, then maximise the narrowest -- give the split the
  enumeration gave, because the optimum is exactly the splits whose every line
  falls in the resulting band and walking it from the left takes the earliest
  break. Identical on 36 336 real cards, 305.5s → 2.2s over that corpus. The
  enumeration was `C(words-1, lines-1)` and did not return AT ALL past about
  six lines, which the documented draft path reaches on its own: `auto_group`
  puts a whole verse on one card when the reciter did not pause inside it, and
  2:164 as one card is 2.4e12 candidates.
- **`font.getlength` cached per (font, character)** — every English width in
  render_text is a sum of per-character advances over an alphabet of a few
  dozen glyphs, re-measured for every line of every card by the wrap, by
  `draw_en_line` and again by `backing_ellipse`.
- **`render_bars.layout` splits each phrase once** — `phrase_lines` shapes
  every candidate split to measure it, and it was called twice per phrase,
  with the width bbox measured twice on top: 362 → 180 shaping calls.

The last three are NOT why a render is fast. `layout` costs the hujurat reels
1.8ms (bars), 4.3ms (horizontal) and 3.8ms (vertical), down from 2.4, 9.2 and
11.5 -- single-digit milliseconds against 12 to 75 seconds of ffmpeg, and the
table above cannot see them. What the wrap solve actually buys is that a card
the enumeration could not finish now returns at all; the rest is the cost of a
draft loop and of anything that calls layout in bulk.

      tools/render-venv/bin/python tests/graph_parity.py
      tools/render-venv/bin/python tests/wrap_parity.py

## Start-up cost, outside the render

What the stages around the render pay before any pixel moves.

- **`fetch.py`** — a source that is already complete (a usable `source.mp4`
  and a `captions.srt`) never calls yt-dlp at all, so a re-fetch costs no
  network round trip and cannot fail the bot check for nothing.
- **`transcribe.py`** — the interpreter probe asks
  `importlib.util.find_spec` instead of importing the backend: 0.02s per
  candidate against 0.9s warm and 3.1s cold for `import mlx_whisper`, and it
  paid that for every candidate before the snippet that transcribes paid it
  again.
- **`align.py`** — takes several configs per invocation, so the 7.1s of
  start-up (1.0s torch, 4.9s the aligner package, 1.3s the MMS weights) is
  paid once for every reel cut from one source rather than once per reel.
- **`publish.py`** — one `ffprobe` for the tags, the frame size and the
  duration `clamp_cover` needs.
- **`quran.from_flat`** — bisect over the surah offsets, not a 114-step scan.
  It runs once per candidate ayah inside `search(boost=...)`, which is why
  that was 2.04x `search()` (3.7ms vs 7.5ms per call).

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
**bars at 1920x1080** — the FX stack now covers 3.16x the pixels per frame.
Nothing here is wrong, but the band-era sigmas were tuned when a blur covered
608 rows; whether any of them can drop a resolution tier at this size is
unmeasured.

# Audit, 2026-08-13

Whole-repo read of the eleven `pipeline/` scripts, `tests/`, `install.sh` and
the docs. The efficiency findings are applied and are recorded above; what is
left below is not. Every number is measured on this machine unless marked as
read off the source. Ordered by leverage inside each list.

## Bugs

| # | Where | Trigger and symptom | Mitigation |
|---|---|---|---|
| 1 | `generate.auto_group` | two cards with equal text and equal times: `arabic.index(ph)` matches by value, so one `english` slot stays `None` and `_fill_gaps` raises `TypeError: 'NoneType' object does not support item assignment` | carry the index through `by_verse` instead of looking the dict back up |
| 2 | `render_text.schedule` | no sequential guard: at every changeover the outgoing card is still fading (to +0.47s) while the incoming one is fully opaque (from +0.21s), in the SAME block position, so the two Arabic lines strike over each other for ~0.26s | port `render_bars.schedule`'s shrink: serialise both fades into the gap the recitation leaves, floored at `min_fade_s` |
| 3 | `generate.load_config` | invariant 5 says loud validation at config time. `groups` with no `n_words` raises a bare `KeyError`, `nudge` with no `group` the same, `bar_color: 'ZZZZZZ'` a bare `ValueError`, `trim: 12.0` is accepted and fails at `list(12.0)`, and `crop: {x, y}` is accepted and refused only after `trim_media` has re-encoded the window | validate shape in `load_config` beside the unknown-key check |
| 4 | `publish.reel_facts` | `--surah` without `--ayat` gives `ValueError: invalid literal for int() with base 10: 'None'` | require the pair in `argparse`, or name the missing flag |
| 5 | `render_bars.phrase_lines` | `line_split` on a one-word group returns `['<word>', '']`, and the empty line still gets a pill `2 x pad_x` wide | refuse `line_split` when it cannot split, at config time |
| 6 | `transcribe.transcribe` | the backend writes `out_json` directly, so a crash mid-write leaves a partial `whisper.json` that the next run reads and skips | write to a temp file and `os.replace` |
| 7 | `letterbox.letterbox` | run standalone twice it letterboxes the letterbox; AGENTS.md documents this and the code has no guard | probe and refuse when the input is already 1080x1920 |
| 8 | `fetch.fetch_local` | keeps the source's own extension, but `generate.resolve_paths` and `transcribe.find_source` only look for seven names, so a local `.avi`/`.m4v` fetches and is then invisible | share one extension list, or always land on `source.mp4` |
| 9 | `fetch.fetch_local` | never runs `ensure_aac`, so a local mp4 carrying Opus hits the deadlock `ensure_aac` exists to prevent | probe the local file and transcode into the source folder |
| 10 | `publish.main` | Instagram publishes first; a Facebook failure leaves the reel live on Instagram, unmarked in Finder and with nothing recording the partial post | tag after each platform succeeds, and report which one went out |
| 11 | `publish.api` | no retry, so a transient Graph 5xx after the bytes have landed kills the publish | retry 5xx and rate-limit codes with a backoff |
| 12 | `generate.run_config` | `_fill_gaps` runs AFTER `clip_to_speech` and re-extends every clipped caption by up to `MAX_HOLD`: a caption clipped to a rest at 10.00s ends at 13.00s. `MAX_HOLD` is also `detect_silences`'s `min_len`, so the shortest detectable rest is exactly cancelled | decide which is intended. If the frame should go clean, clip after filling; if the hold is intended, the two docstrings should say so |

## UX and quality of life

- **A verify-only pass.** `print_verification` is the block the whole
  authoring loop turns on, and the only way to see it is to pay a render.
  `generate.py --verify-only` would stop after it.
- **A preview render.** `fx: {heat: false}` is bars-only. A `--preview`
  (short side 640, `veryfast`, fx off) would make a timing or split check
  seconds rather than a minute.
- **One driver.** The skill runs five commands by hand. A `--from`/`--to`
  driver over align, crop, generate and publish would make the workflow one
  command and give it a resume point.
- **Line buffering.** `generate.py` reconfigures stdout; `align.py`,
  `crop.py`, `transcribe.py` and `publish.py` do not, so their progress
  arrives all at once through a pipe or a log.
- **Render progress.** The final ffmpeg call prints nothing for its whole
  run. `-progress pipe:1` would give a percentage on the stage that
  dominates wall time.
- **Publish progress.** The Instagram container poll is silent for up to
  five minutes.
- **`crop.py --annotate` shows one frame** (the middle one), while `report`
  warns about spread across all of them. A contact sheet of every sampled
  frame would show what the warning is about.

## Code quality

- **`THREAD_QUEUE_SIZE` is 4096 in both `render_text.py` and `fx.Graph`.**
  `render_common.py` holds every other constant the two renderers share; this
  one sits outside it because `fx.Graph` owns the argv it goes into, and
  nothing else in that port reaches out of the file.
- **`norm_ar` is defined in both renderers with different strip sets.** Same
  name, different behaviour, one import away from each other.
- **`.env` readers.** `_dotenv`/`envvar` appear in `fetch.py`,
  `transcribe.py` and `crop.py`; `publish.load_env` is a fourth shape that
  handles neither `export ` nor a trailing ` #` comment, so the four are not
  equivalent to each other.
- **`quran.surah_name_ar` indexes `surahs[n-1]`** while `_offsets` keys off
  `s["number"]`; the two disagree on any edition whose array is not in surah
  order.
