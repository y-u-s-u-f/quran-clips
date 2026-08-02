# pipeline/ — Qur'an reel pipeline

Each script reads/writes plain files; every stage is independently runnable.

```
Source (YouTube / local)
  └─ fetch.py ──► sources/<id>/source.*  (+ captions.srt if any)
Whisper (transcribe.py) -- ONLY to discover the verse span
  └─ whisper.json + whisper.srt
Identify verses (quran.py search, or name the span in the config)
Write sources/<id>/<reel>.yaml
align.py ──► <reel>.align.json (+ writes trim: if omitted)
crop.py (every style) ──► crop: + x_offset:/face_bottom: in the config
generate.py ──► reels/<reel>.mp4  (tags: title, artist, Quran s:a-b)
publish.py ──► Instagram + Facebook
```

## Quickstart

```sh
./install.sh
python3 pipeline/fetch.py "https://www.youtube.com/watch?v=..."
# transcribe + quran.py search ONLY if you don't know the verses
tools/align-venv/bin/python  pipeline/align.py    sources/<id>/<reel>.yaml
tools/render-venv/bin/python pipeline/crop.py     sources/<id>/<reel>.yaml --write --annotate /tmp/c.png
tools/render-venv/bin/python pipeline/generate.py sources/<id>/<reel>.yaml
python3 pipeline/publish.py reels/<reel>.mp4
```

## Layout

```
sources/<id>/   source.*, captions.srt?, whisper.*, <reel>.yaml, <reel>.align.json, crop.json
reels/          output only
pipeline/       fetch transcribe quran align crop generate publish + render_*
assets/         fonts + mushaf/translation editions
legacy/         archived; do not run/edit/import
```

## Config

`generate.py --print-schema` is authoritative. Shape:

```yaml
style: vertical                   # vertical | horizontal | bars
signature: null                   # omit/null = burn nothing; bars never burns
surah: 78
ayah_start: 31
ayah_end: 40
reciter: "..."                    # Arabic spelling for the hashtag / mp4 artist
trim: [15.0, 55.0]                # omit -> align.py measures (source ≈ reel only)
groups:                           # n_words must sum to the span's word count
  - n_words: 4
    english: "..."                # vertical + horizontal; bars is Arabic-only
    line_split: 2                 # bars only; omit = auto
crop: {x: 0, y: 0, w: 1920, h: 1080}   # crop.py writes these three
x_offset: -384                    # bars + horizontal: the caption column
face_bottom: 0.371                # vertical: where his head box ends
y_offset: 0                       # px nudge on whichever anchor applies
# optional: suppress, nudge, verse_numbers, arabic_font, english_font,
#   arabic_scale, english_scale, english_caps, vignette, dim, bar_color, fx
```

Rules: Uthmani by word index (no model-typed Arabic); unknown key = error;
group sums must partition the span. Verification block: every card vs
Saheeh + Taqi.

`trim:` head is measured (0.12s before first word); hand-set trim is reported,
never moved; tail keeps 0.30s. Auto-trim only when the source is roughly the reel.

`crop.py` (every style): framing from a vision model, cached in `crop.json`.
Column styles (bars, horizontal) get the equal-gap rule and `x_offset`;
`vertical` centres him and reports `face_bottom`, the fraction of the canvas
height his head box ends at. Refuses on no face / off-frame caption / no room
under his chin; an EMPTY shot is a centred window and no anchor key.
`--annotate` is required. Authoring only — no model at render time.

## Styles

All 30fps and fixed-size; a weak source is upscaled, never delivered small.

- **`vertical`** — 1080×1920, `render_text.py`. Arabic + English over a graded
  plate, centred horizontally, block top just under his chin (`face_bottom`).
- **`horizontal`** — 1920×1080, the SAME renderer and the same look; block
  centred vertically, column opposite him (`x_offset`).
- **`bars`** — 1920×1080, Thuluth on pills (213pt / 96px / 0.45 W ink), wipe
  then sequential crossfades, full FX (`fx.py`). Never burns a signature.
  Golden: `tests/graph_parity.py`. `fx: {heat: false}` for timing previews.

Design rationale lives in each renderer's module docstring.

## Publishing

```sh
python3 pipeline/publish.py reels/<name>.mp4
python3 pipeline/publish.py reels/<name>.mp4 --caption-only
python3 pipeline/publish.py reels/<name>.mp4 --draft
```

Cover at 1.55s (`COVER_MS`). Caption from `tafsir()` + ayat + `#reciter | #surah`
(from mp4 tags). Credentials in `.env`. Posts are independent (no Graph link).

## Environments

`./install.sh` / `INSTALL.md`. Render: `tools/render-venv` (RAQM Pillow).
Whisper: `asr-venv`. Align: `align-venv` (`ctc-forced-aligner` from git — PyPI
name is unrelated). Machine config in `.env` only; never affects pixels.
`docs/asr-and-alignment.md` before an ASR swap; `OPTIMIZATIONS.md` before a
performance change.
