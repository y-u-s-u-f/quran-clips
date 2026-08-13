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
the docs. Nothing below is applied; every number is measured on this machine
unless marked as read off the source. Ordered by leverage inside each list.

The layout numbers come from `tools/render-venv/bin/python` against
`Albertus MT Lt Regular` at 33pt and the committed `en.sahih` edition, which
is what a render actually shapes.

## Efficiency and optimization

| # | Change | Impact |
|---|---|---|
| 1 | `render_text.wrap_english`: replace the exhaustive split search with a two-stage bottleneck DP | 21.5s -> under 1ms on one 33-word card, and removes an unbounded case (below) |
| 2 | Memoise `font.getlength` per (font, char) in `en_width` | folds into 1; also cuts `backing_ellipse`, which re-tokenises and re-measures every line of every card |
| 3 | `render_bars.layout`: `bbox_ls` is called twice per line building `raw`, and `phrase_lines` is recomputed per phrase after the `all_lines` pass | 362 -> ~180 shaping calls, 0.131s -> ~0.06s on 3 phrases. Pure memoisation, so bit-identical |
| 4 | Move the snow cache from `dirname(tmp_dir)` to `tools/cache/snow` | the shader pass is re-paid after every `/tmp` sweep, for the same reason `HEAT_CACHE` already lives in `tools/` |
| 5 | `fetch.py`: skip the `-J` metadata call when `source.mp4` is usable and `captions.srt` exists | a re-fetch of a cached source stops costing a network round trip, and stops being able to fail the bot check for nothing |
| 6 | `align.py`: accept several configs per invocation | the torch import and the ~1.2GB MMS load are paid once per config; a source with three reels pays them three times |
| 7 | `transcribe.interpreter()`: stop probing candidates with `python -c "import <backend>"` | up to three interpreter starts, each importing mlx/faster-whisper, before any audio is read |
| 8 | `quran.from_flat`: replace the 114-step scan with a flat lookup | `search(boost=...)` is 2.04x `search()` because `from_flat` runs per vote (3.7ms vs 7.5ms per call) |
| 9 | `publish.py`: fold `clamp_cover`'s duration probe into `probe()` | one `ffprobe` instead of two on the same file |

**1 is the headline and it is not only a speed number.** `wrap_english`
enumerates `C(words-1, lines-1)` splits and measures every line of each one.
That is fine on an authored card and unbounded on a long one:

| card English | words | lines | candidate splits | measured |
|---|---|---|---|---|
| authored, typical | 10 | 2 | 9 | 2.6ms |
| 33:56 as ONE card | 34 | 5 | 40 920 | 21.5s |
| 2:164 as ONE card | 84 | 11 | 2.4e12 | does not return |
| 2:282 as ONE card | 256 | 32 | 7.3e39 | does not return |

A config with no `groups:` is the documented draft path
(`generate.auto_group`), and it splits a verse's translation across however
many cards the pauses produced -- one card for a verse the reciter did not
pause inside. So a draft render of a long ayah hangs with no output and no
message. `--print-schema` offers that path, so this is reachable from the
documented workflow, not only from a hand-written config.

The replacement has to reproduce the current answer, not merely a good one.
The objective is lexicographic (fewest lines, then narrowest widest line,
then smallest spread), and a single-value DP ties differently -- measured, 1
mismatch in 440 real translations. Two bottleneck DPs give it exactly:
minimise the widest line at the fixed line count, then maximise the narrowest
line subject to that bound. Both are O(n^2 x lines), and the same 440 cases
run in 0.0006s each against 1.4-7.7s for the current code.

## Bugs

| # | Where | Trigger and symptom | Mitigation |
|---|---|---|---|
| 1 | `render_text.wrap_english` | any card whose English needs more than ~6 lines; no output, no message | the DP above; independently, cap the balanced search and fall back to greedy |
| 2 | `generate.auto_group` | two cards with equal text and equal times: `arabic.index(ph)` matches by value, so one `english` slot stays `None` and `_fill_gaps` raises `TypeError: 'NoneType' object does not support item assignment` | carry the index through `by_verse` instead of looking the dict back up |
| 3 | `render_text.schedule` | no sequential guard: at every changeover the outgoing card is still fading (to +0.47s) while the incoming one is fully opaque (from +0.21s), in the SAME block position, so the two Arabic lines strike over each other for ~0.26s | port `render_bars.schedule`'s shrink: serialise both fades into the gap the recitation leaves, floored at `min_fade_s` |
| 4 | `generate.load_config` | invariant 5 says loud validation at config time. `groups` with no `n_words` raises a bare `KeyError`, `nudge` with no `group` the same, `bar_color: 'ZZZZZZ'` a bare `ValueError`, `trim: 12.0` is accepted and fails at `list(12.0)`, and `crop: {x, y}` is accepted and refused only after `trim_media` has re-encoded the window | validate shape in `load_config` beside the unknown-key check |
| 5 | `publish.reel_facts` | `--surah` without `--ayat` gives `ValueError: invalid literal for int() with base 10: 'None'` | require the pair in `argparse`, or name the missing flag |
| 6 | `render_bars.phrase_lines` | `line_split` on a one-word group returns `['<word>', '']`, and the empty line still gets a pill `2 x pad_x` wide | refuse `line_split` when it cannot split, at config time |
| 7 | `transcribe.transcribe` | the backend writes `out_json` directly, so a crash mid-write leaves a partial `whisper.json` that the next run reads and skips | write to a temp file and `os.replace` |
| 8 | `letterbox.letterbox` | run standalone twice it letterboxes the letterbox; AGENTS.md documents this and the code has no guard | probe and refuse when the input is already 1080x1920 |
| 9 | `fetch.fetch_local` | keeps the source's own extension, but `generate.resolve_paths` and `transcribe.find_source` only look for seven names, so a local `.avi`/`.m4v` fetches and is then invisible | share one extension list, or always land on `source.mp4` |
| 10 | `fetch.fetch_local` | never runs `ensure_aac`, so a local mp4 carrying Opus hits the deadlock `ensure_aac` exists to prevent | probe the local file and transcode into the source folder |
| 11 | `publish.main` | Instagram publishes first; a Facebook failure leaves the reel live on Instagram, unmarked in Finder and with nothing recording the partial post | tag after each platform succeeds, and report which one went out |
| 12 | `publish.api` | no retry, so a transient Graph 5xx after the bytes have landed kills the publish | retry 5xx and rate-limit codes with a backoff |
| 13 | `generate.run_config` | `_fill_gaps` runs AFTER `clip_to_speech` and re-extends every clipped caption by up to `MAX_HOLD`: a caption clipped to a rest at 10.00s ends at 13.00s. `MAX_HOLD` is also `detect_silences`'s `min_len`, so the shortest detectable rest is exactly cancelled | decide which is intended. If the frame should go clean, clip after filling; if the hold is intended, the two docstrings should say so |

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
- **`warn_heat_bake(cfg, dur, tmp)`** never uses `tmp`.

## Code quality

- **Duplication between the two renderers.** `measure_loudness` and
  `loudnorm_filter` are byte-identical in `render_text.py` and
  `render_bars.py`; `fit_pt`, `trim_to_ink`, `_PROBE`, `AUDIO`, `ENCODE`,
  `VIDEO_FADE_IN_S/OUT_S` and `THREAD_QUEUE_SIZE` are duplicated too. The
  no-shared-module rule is about the standalone CLI stages; both of these are
  imported by `generate.py` and neither is runnable alone, so a
  `render_common.py` does not weaken it. `tests/graph_parity.py` is the guard.
- **`norm_ar` is defined in both renderers with different strip sets.** Same
  name, different behaviour, one import away from each other.
- **Invariant 1 is broken in two files.** `generate._ARABIC_DIGITS` and
  `verse_end_marker`'s `"۝"` are typed Arabic, as are
  `render_bars.STRIP_MARKS` and `TASHKEEL`. `render_text.DISPLAY_STRIP_CHARS`
  and `publish.caption` do it right with escapes. The digit table also splits
  its literal mid-string for no reason.
- **`.env` readers.** `_dotenv`/`envvar` appear in `fetch.py`,
  `transcribe.py` and `crop.py`; `publish.load_env` is a fourth shape that
  handles neither `export ` nor a trailing ` #` comment, so the four are not
  equivalent to each other.
- **`fx.Heat.apply` defaults `supersample` to 3** while `FX_CFG` sets 2 and
  this file records 2 as the measured value, so the default is dead and
  points the wrong way.
- **`fx._snow_tile` calls `__import__("random")` inline**, and
  `fx.TextGlow.plate`'s comment block drops to column 0 mid-docstring.
- **`render_bars.heat_map_paths` assigns a lambda to `paths`**, carrying a
  `# noqa: E731` that a `def` would not need.
- **`render_text.render` re-checks `info["width"]`** after
  `generate.run_config` has already refused an input without a video stream.
- **`quran._offsets()` caches by writing `_offsets`/`_counts` into the loaded
  JSON dict**, and `surah_name_ar` indexes `surahs[n-1]` while `_offsets`
  keys off `s["number"]`.
- **`.env.example` names `pipeline/render_default.py`**, which does not
  exist; the file is `render_text.py`. `install.sh`'s header numbers its
  steps 1, 2, 3, 3b, 5, 6.
