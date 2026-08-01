# Original pipeline scripts

The **actual** first-generation implementation, not a reconstruction. Recovered
intact from `~/.hermes/tools/transcription/` and verified to run: all three files
parse, and `quran_reel_pipeline.py --print-schema` still prints its full config
reference.

Uploaded 2026-07-31.

| file | LOC | role |
|---|---|---|
| `quran_reel_pipeline.py` | 1186 | the whole pipeline: fetch, align, group, layout, render |
| `verse_lookup.py` | 133 | mushaf text + word counts for a verse range |
| `transcribe.py` | 90 | Whisper wrapper, word-level timings |
| `quran-simple-clean.json` | 2.8 MB | the mushaf corpus these read (**not uploaded**, it is a public dataset) |

## Running it

```sh
source ~/.hermes/hermes-agent/venv/bin/activate
cd ~/.hermes/tools/transcription
python3 quran_reel_pipeline.py --print-schema          # full config reference
python3 quran_reel_pipeline.py configs/<name>.json     # render one reel
```

Only `input` and `output` are required in a config; everything else has a default.
Run with `--print-schema` rather than reading the source to learn the keys, since
that output is generated from the code and cannot drift.

## Architecture, in the order the pipeline runs

1. **Verse span** — `surah` / `ayah_start` / `ayah_end`, or omitted to auto-detect
   by ASR. Auto-detection is only reliable for roughly 1-4 ayah clips; anything
   longer should state the span, which is also why the span-measurement discipline
   in `REFERENCE.md` exists.
2. **Text** — `verse_lookup.py` returns the Uthmani text and the authoritative
   spoken word count. `groups[].n_words` must sum to it exactly, which is the
   invariant that makes a mis-split fail loudly instead of silently shifting
   captions.
3. **Alignment** — `transcribe.py` (Whisper) gives word-level timings, matched
   against the known-correct text rather than trusted as a transcript.
4. **Layout** — the width-limited font fit. `shared_font_size` starts at
   `height * 0.09 * arabic_scale` and shrinks until the **widest** caption fits
   `max_ar_width`. English is locked to 0.72 of the fitted Arabic size.
5. **Render** — captions are drawn to PNG with PIL and composited by ffmpeg.
   Deliberate: libass cannot shape Quranic Arabic (it produces disconnected
   glyph boxes), so text is never handed to a subtitle filter.

## Two implementation details worth knowing

**Verse ornaments are font-dependent, and the two fonts behave oppositely.**
`uthmanic_hafs` wants bare digits; `scheherazade` wants U+06DD. Getting it wrong
renders two concentric rings rather than one ornament.

**`-loop 1` image inputs never EOF.** A still-background render needs an explicit
`-t`; `-shortest` does not terminate it, which will hang an encode indefinitely.

## Relationship to the current package

Superseded by `y-u-s-u-f/quran-clips`, which is better verified (an 11-assertion
pre-render gate, golden regression fixtures, decode-verified output) and better
factored. These scripts are archived because the measured constants in
`REFERENCE.md` were derived with them, and because a few schema keys here
(`background_image`, `trim`, `nudge`, `suppress`, `signature`) have no direct
equivalent in the new package.
