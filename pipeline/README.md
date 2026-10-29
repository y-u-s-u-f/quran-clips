# pipeline/ — Qur'an reel pipeline

Each stage is a script that reads and writes plain files; every one is independently runnable. The workflow and its fixes: `.agents/skills/make-post/SKILL.md`.

```
Source (YouTube / local)
  └─ fetch.py ──► sources/<id>/source.*  (+ captions.srt, or captions.none)
transcribe.py (Whisper) ──► whisper.json + whisper.srt
     finds the span, and lets align.py repair an ibtidāʾ restart
Identify verses (quran.py --search, or name the span in the config)
Write sources/<id>/<reel>.yaml
align.py ──► <reel>.align.json (+ writes trim:, and the span if omitted)
crop.py (every style) ──► crop: + x_offset:/face_bottom: in the config
generate.py ──► reels/<reel>.mp4  (tags: title, artist, Quran s:a-b)
publish.py ──► Instagram + Facebook
```

## Files

```
sources/<id>/   source.*, captions.srt|captions.none, whisper.*,
                <reel>.yaml, <reel>.align.json, crop.json
reels/          output only
pipeline/       fetch transcribe quran align crop generate letterbox publish
                + render_text render_bars fx render_common (imported by generate)
                + gpu/render_gpu.swift (every style's GPU renderer -> tools/render_gpu)
assets/         fonts + mushaf/translation editions
tests/          graph_parity.py (bars goldens), gpu_parity.py (render_gpu's
                frames, every style), input_parity.py (odd sources against
                live ffmpeg), wrap_parity.py (English wrap)
```

## Config

`tools/render-venv/bin/python pipeline/generate.py --print-schema` is the schema; the make-post skill has an annotated example. Rules: Uthmani by word index (no model-typed Arabic); unknown key = error, inside `groups`/`nudge` entries too; group sums must partition the span. `--verify-only` prints the verification block (every card vs Saheeh + Taqi) and stops — a split check costs seconds instead of a render.

## Stages

**align.py** — `trim:` head is measured (0.12s before the first word); hand-set trim is reported, never moved; tail keeps 0.30s. With no `trim:` it aligns the whole source and writes the window back; with no span it identifies one from Whisper and writes it back too. Both only when the source is roughly the reel. Takes several configs at once (`sources/<id>/*.yaml`), sharing the model load. Without `whisper.json` an ibtidāʾ restart goes unrepaired.

Reusing a sidecar requires both its recorded trim and its exact stored mushaf words to match. `align.stale_note` re-aligns a named span with changed or missing words without `--force`, even at the same word count; `generate.load_forced_alignment` refuses it and asks for re-alignment.

**crop.py** (every style) — framing from an OpenAI-compatible vision API (`QC_VISION_*` in `.env`), cached in `crop.json`. Column styles (bars, horizontal): caption on the side he faces, edge|reciter, reciter|caption and caption|edge roughly equal, `x_offset`. `vertical` centres him and reports `face_bottom`, the fraction of the canvas height his head box ends at. Zooms only to clear burned-in graphics or reach that layout. Refuses to write on no face / no room for the caption; an EMPTY shot is a centred window and no anchor key. Check `--annotate` every time. Authoring only — no model at render time.

## Styles

All 30fps and fixed-size; a weak source is upscaled, never delivered small.

- **`vertical`** — 1080×1920, `render_text.py`. Arabic + English over a graded plate, centred horizontally, block top just under his chin (`face_bottom`).
- **`horizontal`** — 1920×1080, the SAME renderer and the same look; block centred vertically, column opposite him (`x_offset`).
- **`bars`** — 1920×1080, Thuluth on pills (213pt / 96px / 0.45 W ink), wipe then sequential crossfades, full FX (`fx.py`). Never burns a signature. Golden: `tests/graph_parity.py`. `fx: {heat: false}` for timing previews.

Every style renders on `tools/render_gpu` (macOS + Metal only), which runs the style's own ffmpeg filtergraph string as its input spec, an exact port of each filter, and encodes VideoToolbox H.264 at `render_common.VT_QUALITY`, keyed by style. ffmpeg renders no frame; `tests/gpu_parity.py` pins the frames. render_gpu never reads the source itself: `generate.trim_media` re-encodes it to an 8-bit H.264 mp4 first, the whole of it when the config has no `trim:`, so any source fetch takes (MKV, WebM, VP9, AV1, 10-bit HDR) renders.

`generate.py --vertical` delivers a 1920×1080 style as 1080×1920, black above and below, inside the render's own encode. The picture is 1080×608, about a third of the frame, so bars' Arabic arrives on the phone at 120pt nominal instead of 213; the native `vertical` style avoids that trade. Both `horizontal` and `bars` compose their whole look at 1080×608 (`render_text.geometry`, `render_bars.geometry`), which is cheaper and looks the same at the size it is shown (`OPTIMIZATIONS.md`). `letterbox.py` does the same to an already-finished file (and refuses a portrait one).

Design rationale lives in each renderer's module docstring.

## Publishing

```sh
python3 pipeline/publish.py reels/<name>.mp4
python3 pipeline/publish.py reels/<name>.mp4 --caption-only
python3 pipeline/publish.py reels/<name>.mp4 --dry
python3 pipeline/publish.py reels/<name>.mp4 --draft
```

Cover at 1.55s (`COVER_MS`). Caption from `tafsir()` + ayat + `#reciter | #surah` (from mp4 tags; `--surah` + `--ayat` together, `--reciter` override them). `--ig-only` / `--fb-only` are exclusive. Credentials in `.env`. Posts are independent (no Graph link). The reel is tagged green in Finder once at least one platform posted; `--draft` leaves it untagged. A failed permalink lookup only warns and prints the published media ID: it cannot turn a successful post into a failed one. The temporary Facebook cover is cleaned up even after extraction fails; cleanup errors warn without interrupting Instagram publishing.

`upload_instagram` creates and uploads a container, then Facebook runs while Instagram transcodes, then `publish_instagram` polls and publishes it. Failures are collected per platform so either can still succeed, with one Finder-tag attempt after the flow and an explicit partial-post report on failure. `--draft` uploads a Facebook draft and leaves the ready Instagram container unpublished; neither is a live post. `--dry` copies the caption and `--caption-only` prints it, both without uploads or network requests. Consecutive ayat sharing a tafsir paragraph are grouped with `groupby` so the paragraph appears once.

## Environments

`./install.sh` / `INSTALL.md`. Render: `tools/render-venv` (RAQM Pillow). Whisper: `asr-venv`. Align: `align-venv` (`ctc-forced-aligner` from git — PyPI name is unrelated). GPU render: `tools/render_gpu`, swiftc-built from `pipeline/gpu/render_gpu.swift` (macOS; a stale or missing binary is an error, not a fallback). Machine config in `.env` only. `docs/asr-and-alignment.md` before an ASR swap; `OPTIMIZATIONS.md` before a performance change.
