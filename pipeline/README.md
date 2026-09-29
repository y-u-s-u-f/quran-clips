# pipeline/ — Qur'an reel pipeline

Each stage is a script that reads and writes plain files; every one is
independently runnable. The workflow and its fixes:
`.agents/skills/make-post/SKILL.md`.

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
assets/         fonts + mushaf/translation editions
tests/          graph_parity.py (bars goldens), wrap_parity.py (English wrap)
```

## Config

`tools/render-venv/bin/python pipeline/generate.py --print-schema` is the
schema; the make-post skill has an annotated example. Rules: Uthmani by word index (no
model-typed Arabic); unknown key = error, inside `groups`/`nudge` entries too;
group sums must partition the span. `--verify-only` prints the verification
block (every card vs Saheeh + Taqi) and stops — a split check costs seconds
instead of a render.

## Stages

**align.py** — `trim:` head is measured (0.12s before the first word);
hand-set trim is reported, never moved; tail keeps 0.30s. With no `trim:` it
aligns the whole source and writes the window back; with no span it
identifies one from Whisper and writes it back too. Both only when the source
is roughly the reel. Takes several configs at once (`sources/<id>/*.yaml`),
sharing the model load. Without `whisper.json` an ibtidāʾ restart goes
unrepaired.

**crop.py** (every style) — framing from the `claude` CLI, cached in
`crop.json`. Column styles (bars, horizontal) get the equal-gap rule and
`x_offset`; `vertical` centres him and reports `face_bottom`, the fraction of
the canvas height his head box ends at. Refuses on no face / off-frame
caption / no room under his chin; an EMPTY shot is a centred window and no
anchor key. Check `--annotate` every time. Authoring only — no model at
render time.

## Styles

All 30fps and fixed-size; a weak source is upscaled, never delivered small.

- **`vertical`** — 1080×1920, `render_text.py`. Arabic + English over a graded
  plate, centred horizontally, block top just under his chin (`face_bottom`).
- **`horizontal`** — 1920×1080, the SAME renderer and the same look; block
  centred vertically, column opposite him (`x_offset`).
- **`bars`** — 1920×1080, Thuluth on pills (213pt / 96px / 0.45 W ink), wipe
  then sequential crossfades, full FX (`fx.py`). Never burns a signature.
  Golden: `tests/graph_parity.py`. `fx: {heat: false}` for timing previews.

`generate.py --vertical` delivers a 1920×1080 style as 1080×1920, black above
and below, inside the render's own encode. The picture is 1080×608, about a
third of the frame, so bars' 213pt Arabic arrives on the phone nearer
120pt-equivalent; the native `vertical` style avoids that trade.
`letterbox.py` does the same to an already-finished file (and refuses a
portrait one).

Design rationale lives in each renderer's module docstring.

## Publishing

```sh
python3 pipeline/publish.py reels/<name>.mp4
python3 pipeline/publish.py reels/<name>.mp4 --caption-only
python3 pipeline/publish.py reels/<name>.mp4 --draft
```

Cover at 1.55s (`COVER_MS`). Caption from `tafsir()` + ayat + `#reciter | #surah`
(from mp4 tags; `--surah` + `--ayat` together, `--reciter` override them).
`--ig-only` / `--fb-only` are exclusive. Credentials in `.env`. Posts are
independent (no Graph link). The reel is tagged green in Finder once at least
one platform posted; `--draft` leaves it untagged.

## Environments

`./install.sh` / `INSTALL.md`. Render: `tools/render-venv` (RAQM Pillow).
Whisper: `asr-venv`. Align: `align-venv` (`ctc-forced-aligner` from git — PyPI
name is unrelated). Machine config in `.env` only. `docs/asr-and-alignment.md`
before an ASR swap; `OPTIMIZATIONS.md` before a performance change.
