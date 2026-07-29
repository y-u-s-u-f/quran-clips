---
name: make-post
description: >-
  Make a Quran recitation clip/reel from a YouTube URL for the Quran clipping
  account. Trigger on "make a post from <youtube url>", "make a clip from this
  recitation", "turn this into a reel". Drives the scripted ~/quran-clips
  pipeline: `qc source add` -> `qc propose` -> `qc author` -> `qc check` ->
  `qc render` -> `qc export`. TWO styles: `bars` (vertical 9:16, Arabic-only
  Thuluth captions on coloured pills — the default of the CLI) and `default`
  (landscape 1920x1080 with English), selected with `--style`.
---

# make-post — Quran clip pipeline

The pipeline is SCRIPTED. Your job is to run the commands, read what they print,
and make the few judgement calls the tools hand up. Do not re-derive by hand
anything a command already does.

Repo root `~/quran-clips`. All commands below are run from there.
`./bin/qc` picks `tools/render-venv` automatically; scripts under `scripts/`
must be invoked with `tools/render-venv/bin/python` explicitly.
`./bin/qc` with no arguments prints the authoritative usage — read it if a flag
here looks stale.

## Styles
- `bars` — 1080x1920, letterboxed footage band, Arabic-only captions on pills.
  Params `templates/bars.yaml`. This is the CLI default for `--style`.
- `default` — 1920x1080 landscape, Arabic + English. Params `templates/style.yaml`.
- The style is fixed per clip by the `style:` key in clip.yaml, and it also keys
  the cached crop, so pass the same `--style` to every command in a run.
- A bare "make a post from <url>" means: pick the style the user last used /
  asked for; if nothing indicates otherwise, `bars`. Do not stall on a question.

## End-to-end sequence

```
./bin/qc source add <youtube-url>            # yt-dlp -> sources/<id>.mp4 + subs + sources/meta/<id>.yaml
./bin/qc locate <video_id>                   # which surah/ayat is recited (uses the auto-captions)
./bin/qc crop <video_id> --style bars --write # solve the 16:9 framing ONCE per source, cache it
./bin/qc propose <video_id> --style bars      # ranked candidate windows; show these to the user
./bin/qc author <video_id> 9:128 <start> <end> --style bars -o clips/<name>
./bin/qc check clips/<name>                   # config-time assertions, ~1s. MUST pass before rendering
./bin/qc render clips/<name>                  # -> clips/<name>/output/final.mp4
./bin/qc check --output clips/<name>          # geometry / letterbox purity / loudness of the finished file
./bin/qc export clips/<name>                  # -> reels/RECITER-SURAH-a-b.mp4 + Finder tags
tools/render-venv/bin/python scripts/status.py post <clip-or-reel-name>   # after it is actually posted
```

What each one does:
- **`source add`** downloads ≤1080p video + Arabic auto-captions into `sources/`
  (the durable cache, shared across clips). `--no-video` for audio/meta only.
  Re-running is cheap; never copy a source into a clip folder.
- **`locate`** matches the auto-captions against the mushaf and prints the ayah
  range. `-v` for detail. `./bin/qc ayah 9:128-129` prints the mushaf text.
- **`crop`** solves the reciter/caption framing from sampled frames and caches it
  in `sources/meta/<id>.yaml`, keyed by style. `--side left|right`,
  `--face X,Y[,W]`, `--exclude X,Y,W,H` (for an intermittent banner),
  `--annotate P` / `--sheet P` to inspect, `--write` to cache. `qc author`
  refuses to reuse a crop solved for the OTHER style — solve it per style.
  Needs `tools/author-venv`.
- **`propose`** ranks clip-worthy windows and asks a model (in English, on ayah
  NUMBERS only) whether each stands alone. `-n N`, `--range MM:SS-MM:SS` or
  `--verses S:A-B` to skip proposing and score a chosen window, `--no-judge`,
  `--json`. Give the user the top candidates and let them pick.
- **`author`** aligns the window against the mushaf, measures the envelope, sets
  the head, splits the cards at real waqf gaps, breaks the lines, and writes
  `clip.yaml` + `tags.yaml`. `-o DIR`, `--like CLIP` (carry reciter/crop/url off
  a shipped clip), `-n` to print without writing. It prints warnings on stderr —
  `! restart detected` (ibtidāʾ) and `! Pn->Pn+1 is NOT a true waqf` — pass those
  up to the user; they are judgement calls, not bugs.
- **`check`** is the QA stage. Eleven assertions: schema (unknown key = error),
  phrase/cut ordering, glyph coverage, Arabic == mushaf text, no card spanning
  two ayat, bars caption geometry, no hard-cut changeover, English present/absent
  per style, fx names, and that the clip opens on the first word of the ayah it
  claims. Never render over a failing check.
- **`render`** writes `output/final.mp4` (~6 min for 24s of bars). `--preview`
  writes `output/preview.mp4` instead: half canvas, no heat/snow/glow/scan,
  ≤30s — use it for TIMING and LAYOUT only, never to judge the look.
  `./bin/qc frames <clip> --at 0,6,12,18 [-o DIR]` writes full-fidelity stills.
  (preview + frames are implemented for `bars` only.)
- **`export`** stream-copy remuxes to `reels/RECITER-SURAH-a0-a1.mp4`, embeds
  title/artist/date/source metadata, applies the tags.yaml keywords and a red
  "Not Posted" Finder marker, and drops an alias in the clip folder. Idempotent;
  refuses a preview render. `reels/` is the permanent library — once posted, the
  clip folder is disposable.
- **`status.py`** `list` / `post <name>` / `unpost <name>` / `sync` flips
  tags.yaml + the Finder markers on both the clip folder and the reel.

Fixing a clip: edit `clips/<name>/clip.yaml` (segment start/end, `segment.cuts`,
the card split, `bar_color`), re-run `qc check`, re-render. Per-clip problems get
per-clip fixes; only an account-wide shift touches `templates/*.yaml`, and
`bar.auto.*` is reference-validated — never hand-tune it.

## Regression harness
`scripts/golden.py` freezes, for four golden clips (bars, default, 1-cut and
2-cut splice), the exact ffmpeg argv + filtergraph, the md5 of every intermediate
layer, and the md5 of the final encode. Any change to render code must leave them
byte-identical.

```
tools/render-venv/bin/python scripts/golden.py check --all        # tier 1: env + argv + filtergraph
tools/render-venv/bin/python scripts/golden.py check --layers <clip>  # tier 2: + regenerate layers
tools/render-venv/bin/python scripts/golden.py check --full <clip>    # tier 3: + full render md5
tools/render-venv/bin/python scripts/golden.py bless <clip> [--full]  # re-record an INTENDED change
tools/render-venv/bin/python scripts/golden.py env                    # ffmpeg/python build report
tools/render-venv/bin/python -m unittest discover -s tests            # fx scalar pins, milliseconds
```
Tiers 2 and 3 never write into a clip's `output/`. Byte-identity is coupled to
the ffmpeg build; the checker gates on `ffmpeg -version` first, so an upgrade
reports "golden invalid" rather than a fake regression.

## Hard rules
- **NEVER pip-install into `/opt/homebrew/bin/python3`.** A past install there
  destroyed the RAQM-enabled Pillow that does Arabic shaping, and it is not
  reproducible from PyPI. Use the venvs: `tools/render-venv` (rendering + all
  `scripts/`; `--system-site-packages`, PyYAML only), `tools/asr-venv`
  (mlx-whisper), `tools/author-venv` (opencv/numpy for `qc crop`). `tools/` is
  gitignored, so a fresh clone recreates them from `requirements/`.
- **NEVER `golden.py bless` to silence a failing golden.** A failure means
  something broke. Diagnose it, or stop and report. Bless only re-records a
  change you deliberately made and understand.
- **No model ever retypes Arabic.** Uthmani codepoints (madda alif, small high
  marks, superscript alif) corrupt on retyping, and so does any summarising
  fetch. Arabic comes from `qc.quran` / `qc ayah`, copied byte-for-byte. LLM
  judges return indices and numbers only — the line-breaker answers with a
  single integer, the coherence judge with ayah numbers.
- **A text card must never span two ayat.** `qc author` cuts at every ayah
  boundary unconditionally, before the energy envelope gets a say, and
  `qc check`'s `ayah-span` assertion fails any clip that straddles one. A card
  may show part of an ayah or a whole ayah — never words from two.
- **Do not `open` produced artefacts.** Build the file, report the path.
- **Do not touch `reels/` or `clips/at-tawbah-128-128/output/final.mp4`** without
  explicit intent — the first is the permanent library, the second is a golden.
- ffmpeg here is a SLIM build: no drawtext/subtitles/libass. All text is Pillow
  PNGs composited with `overlay` + `fade:alpha=1`. Don't reach for drawtext.

## Reference material (read only when a question actually needs it)
`style/STYLE_SPEC.md` + `templates/style.yaml` (default style);
`style/refs2/STYLE2_SPEC.md`, `FX_RECIPE.md`, `SNOW_SPEC.md` + `templates/bars.yaml`
(bars). Style references are ONLY the account's own IG posts: `style/refs/` for
default, `style/refs2/` for bars — never mix them, and never use the iCloud
`Clips/` folder. On any conflict, the reels beat the written spec.
