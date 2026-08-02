# quran-clips

Turn a Qur'an recitation into a subtitled reel:

```sh
python3 pipeline/fetch.py "https://www.youtube.com/watch?v=..."
# write sources/<id>/<reel>.yaml  (verse span, card splits)
tools/align-venv/bin/python  pipeline/align.py    sources/<id>/<reel>.yaml
tools/render-venv/bin/python pipeline/crop.py     sources/<id>/<reel>.yaml --write
tools/render-venv/bin/python pipeline/generate.py sources/<id>/<reel>.yaml
python3 pipeline/publish.py reels/<reel>.mp4
```

`align.py` times known mushaf text onto audio (WHEN only). Whisper is only
for verse discovery — name the span and skip it. Caption Arabic is sliced
from committed Uthmani by word index; bad group sums fail before a frame is
drawn. `publish.py` builds the caption from tags on the mp4 itself.

## Styles

Three, all 30fps, all sized once and for all — a weak source is upscaled
rather than delivered small.

- **`vertical`** — 1080×1920. Arabic + English over graded footage, centred
  horizontally, hung just below the reciter's chin (`face_bottom`).
- **`horizontal`** — 1920×1080. The same look and the same renderer
  (`render_text.py`); the type sits in the column opposite him (`x_offset`).
- **`bars`** — 1920×1080, Thuluth on pills, wipe + sequential crossfades, full
  FX. Geometry is measured from refs — edit via `tests/graph_parity.py`, not
  by eye. Burns no signature, ever. See `OPTIMIZATIONS.md` for cost.

`vertical` and `horizontal` are one renderer (`render_text.py`) and one look;
only the canvas and the block's anchor differ.

Demos in `docs/`. Details: `pipeline/README.md`.

## Layout

```
pipeline/    workflow scripts + renderers (+ fx.py)
sources/<id>/  source media, whisper.*, per-reel *.yaml + *.align.json
reels/       output (untracked)
assets/      fonts + mushaf/translations
tests/       bars filtergraph golden
legacy/      archived; do not run/edit/import
```

## Config

Every key has a default; an unknown key is an error.
`generate.py --print-schema` is authoritative:

```yaml
style: vertical
signature: null             # omit or null = burn nothing; bars never burns
surah: 78
ayah_start: 31
ayah_end: 34
trim: [16.4, 48.0]          # omit -> align.py measures (source ≈ reel)
groups:
  - n_words: 3              # must sum to the span's word count
    english: "..."
x_offset: 0
y_offset: 0
```

`crop.py` authors the framing for every style: `crop:` plus `x_offset:`
(bars, horizontal) or `face_bottom:` (vertical). It is authoring-only — no
model runs at render time, so a reel re-renders identically anywhere.
Generation prints a verification block (card Arabic + Saheeh + Taqi) — check
both editions.

## Setup

```sh
./install.sh            # or ./install.sh --check
```

Three interpreters: `tools/render-venv` (RAQM Pillow), `asr-venv`,
`align-venv`. Only `./install.sh` builds them. `.env` is machine config
(binaries, ASR, proxies, Meta credentials) — never pixels. Full guide:
`INSTALL.md`. Agent drive: `.claude/skills/make-post/SKILL.md`.

Media and renders stay local; configs are committed. Nothing here
redistributes anyone's footage.
