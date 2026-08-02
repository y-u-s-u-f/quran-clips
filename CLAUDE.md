# CLAUDE.md — writing code in quran-clips

To *produce a reel*, invoke the `make-post` skill
(`.claude/skills/make-post/SKILL.md`).

## Codebase map

Ten standalone scripts under `pipeline/`; each owns one stage, reads/writes
plain files. No shared util module — `fetch.py` and `transcribe.py` each
carry their own `.env` reader so every script stays independently runnable.

- **`fetch.py`** — URL/local -> `sources/<id>/`. yt-dlp (proxy pool,
  `web_embedded` retry, `ensure_aac`, stub gate). Caps at `MAX_FPS` 30 at
  intake. No third-party imports.
- **`transcribe.py`** — `source.*` -> `whisper.json` + `.srt`. Backend
  subprocess against `tools/asr-venv` via `_SNIPPETS` (strings so they run
  under an interpreter that cannot import this package). Contract:
  `{"words":[{"w","t0","t1","p"}], "segments", "backend", "model"}`.
- **`quran.py`** — offline mushaf; ONLY source of Arabic/translations
  (`assets/quran/`). `ayah()` returns stored bytes; `search()` is IDF +
  contiguity. Normalisation tables are frozen (byte-identical to legacy on
  all 6236 ayat). `ar.muyassar` is for `tafsir()` / post captions only.
- **`align.py`** — config -> `<reel>.align.json`. CTC forced alignment
  (MMS / `tools/align-venv`) of known mushaf text — WHEN only, never WHAT.
  Omit `trim:` -> align whole source, write measured window back (line edit;
  `safe_dump` eats comments). Head measured via `rms_envelope` (Haram reverb
  floors `silencedetect`); tail keeps `TAIL_PAD`. Whisper needed only for
  verse discovery and ibtidāʾ repeats (`find_repeats` needs Whisper to have
  split the two utterances). Auto-trim is only sound when the source is
  roughly the reel.
- **`crop.py`** — authoring-time `crop:` + `x_offset:` for `bars`/`hz`
  (`default` uses YuNet at render). Shells out to `claude -p` (local auth);
  arithmetic decides the window (`targets()` equal-gap rule). Defensive
  parse, no silent defaults. Refuses only on no face / off-frame caption;
  `FACE_Y_BAND` is a printed note. `--annotate` is the primary check
  (invariant 4: model never consulted at render time).
- **`generate.py`** — YAML -> render. `load_config` / `align_words` /
  groups / silences / `suppress`/`nudge` (nudge last) /
  `print_verification`. Dispatches on `style:` with plan dict
  (`cfg/src/info/arabic/english/verses/tmp/out`).
- **`publish.py`** — mp4 -> Instagram + Facebook. Tags from the file
  (`--surah/--ayat/--reciter` if missing). Caption from `tafsir()` + ayat +
  hashtags. Cover at `COVER_MS` 1550. Credentials in `.env`; no pixels
  touched.
- **`render_default.py`** — Arabic + English over dimmed footage (Pillow;
  no libass). Canvas short side in [720, 1080].
- **`render_bars.py`** — vertical pills. `layout` / `draw_layers` /
  `schedule` / `build_graph`. Constants measured from refs (120pt / 54px
  pill / 0.45 W ink cap). Provenance in `legacy/templates/bars.yaml`.
- **`render_hz.py`** — 1920x1080; config `crop` only at render.
  `layout()` centres the median phrase block. Grade gradients at 48x27
  (that resolution IS the softness).
- **`fx.py`** — bars effects. Ported from `legacy/qc/`; do not re-derive.
- **`docs/asr-and-alignment.md`** — measured ASR comparison; read before a
  model swap. Whisper kept only because it is autoregressive (ibtidāʾ).
- **`OPTIMIZATIONS.md`** — render cost profile + rejected ideas; read
  before a performance change. Update in the same change.
- **`legacy/`** — archived; never run, edit, or import from pipeline.
- **`sources/<id>/`** configs tracked, media not; **`reels/`** untracked;
  **`assets/`** fonts + editions.

## Invariants

1. **No Arabic typed by a model** — including in source code. Slice from
   `quran.py` by word index; configs carry counts. In `.py`, `\uXXXX` or
   verify codepoint-by-codepoint against legacy.
2. **`render_bars.py` + `fx.py` byte-identity** with
   `tests/golden/bars-filtergraph.txt`. After any edit:

       tools/render-venv/bin/python tests/graph_parity.py

   Intended look change: PSNR a known reel first (~45dB floor), then
   `--bless`.
3. **Interpreter split.** Render under `tools/render-venv` (RAQM Pillow;
   hard-exit without it). Whisper: `asr-venv`. Align: `align-venv`. Only
   `./install.sh` builds them. `align.py` imports `generate` for helpers
   only — nothing that pulls Pillow.
4. **Nothing outside committed files may affect a rendered pixel.** Style
   constants in renderers; `.env` is machine config only.
5. **Loud validation at config time**: unknown key = error; group sums
   partition the span; `signature` required. Extend `DEFAULTS` +
   `config_schema()` together.

## How to write changes

- **Keep code concise, optimized, and minimal.** Comments and docs: CURRENT
  or planned behavior only — never previous behavior.
- **Minimize.** Smallest diff that solves it. No abstractions for
  hypotheticals. New key needs a concrete reel; new file needs a reason the
  existing ones can't hold it.
- **No new dependencies** without an owner decision.
- **Prefer porting.** Search `legacy/` first; keep its numbers.
- **Comments explain WHY** (measurement/incident), never what the next line
  does. Phrase as current rationale, not a changelog.
- **Bars: layout / draw / graph stay separate.**
- **ffmpeg:** bound every `-loop 1` with `-t`; keep `THREAD_QUEUE_SIZE`;
  loudnorm floats stay floats (`-14.0`); `heat` needs `perlin` (>= 7.1).
- **Verify:** end-to-end on a cached source, decode-check, verification
  block, golden parity for bars; for `quran.py`, whole-mushaf vs legacy.
- **Docs travel with code:** `DEFAULTS`, `config_schema()`,
  `pipeline/README.md`, make-post skill.
