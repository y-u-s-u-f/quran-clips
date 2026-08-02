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
crop.py (bars/hz) ──► crop: + x_offset: in the config
generate.py ──► reels/<reel>.mp4  (tags: title, artist, Quran s:a-b)
publish.py ──► Instagram + Facebook
```

## Quickstart

```sh
./install.sh
python3 pipeline/fetch.py "https://www.youtube.com/watch?v=..."
# transcribe + quran.py search ONLY if you don't know the verses
tools/align-venv/bin/python  pipeline/align.py    sources/<id>/<reel>.yaml
tools/render-venv/bin/python pipeline/crop.py     sources/<id>/<reel>.yaml --write
tools/render-venv/bin/python pipeline/generate.py sources/<id>/<reel>.yaml
python3 pipeline/publish.py reels/<reel>.mp4
```

## Layout

```
sources/<id>/   source.*, captions.srt?, whisper.*, <reel>.yaml, <reel>.align.json
reels/          output only
pipeline/       fetch transcribe quran align crop generate publish + render_*
assets/         fonts + mushaf/translation editions
legacy/         archived; do not run/edit/import
```

## Config

`generate.py --print-schema` is authoritative. Shape:

```yaml
style: bars                       # bars | default | hz
signature: null                   # REQUIRED; null = burn nothing
surah: 78
ayah_start: 31
ayah_end: 40
reciter: "..."                    # Arabic spelling for the hashtag / mp4 artist
trim: [15.0, 55.0]                # omit -> align.py measures (source ≈ reel only)
groups:                           # n_words must sum to the span's word count
  - n_words: 4
    english: "..."                # default + hz; bars is Arabic-only
    line_split: 2                 # bars only; omit = auto
x_offset: 0
y_offset: 0
# optional: suppress, nudge, verse_numbers, bar_color, crop, fx, ...
```

Rules: Uthmani by word index (no model-typed Arabic); unknown key = error;
`signature` required. Verification block: every card vs Saheeh + Taqi.

`trim:` head is measured (0.12s before first word); hand-set trim is reported,
never moved; tail keeps 0.30s. Auto-trim only when the source is roughly the reel.

`crop.py` (bars/hz): equal-gap framing from a vision model; refuses only on no
face / off-frame caption; `--annotate` is required. Authoring only.

## Styles

- **`default`** — Arabic + English over dimmed footage; optional 9:16 YuNet
  re-crop / still background. Canvas short side in [720, 1080]. Pillow only.
- **`bars`** — 1080×1920 letterbox band, Thuluth on pills (120pt / 54px /
  0.45 W ink), wipe then sequential crossfades, full FX (`fx.py`). Golden:
  `tests/graph_parity.py`. `fx: {heat: false}` for timing previews.
- **`hz`** — 1920×1080; config `crop` only; type opposite the reciter via
  `x_offset`. Verified on `sources/YkXjYyKwHJ4/ahzab-56-56.yaml`.

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
