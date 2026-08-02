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

- **`default`** — Arabic + English over dimmed footage; optional 9:16 re-crop /
  still background. Canvas short side in [720, 1080].
- **`bars`** — 1080×1920 letterbox band, Thuluth on pills, wipe + sequential
  crossfades, band FX. Geometry is measured from refs — edit via
  `tests/graph_parity.py`, not by eye. See `OPTIMIZATIONS.md` for cost.
- **`hz`** — 1920×1080; authored `crop`; type opposite the reciter.

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

`signature` required (`null` = burn nothing). `generate.py --print-schema`
is authoritative:

```yaml
style: bars
signature: null
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

`crop.py` authors `crop:` / `x_offset:` for bars/hz. Generation prints a
verification block (card Arabic + Saheeh + Taqi) — check both editions.

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
