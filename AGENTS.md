# CLAUDE.md — writing code in quran-clips

To *produce a reel*, invoke the `make-post` skill
(`.claude/skills/make-post/SKILL.md`).

## Codebase map

Eleven standalone scripts under `pipeline/`; each owns one stage, reads/writes
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
- **`crop.py`** — authoring-time framing for EVERY style: `crop:` plus
  `x_offset:` (bars, horizontal) or `face_bottom:` (vertical). Shells out to
  `claude -p` (local auth); arithmetic decides the window. Column styles use
  `targets()`'s equal-gap rule; `vertical` has no column beside him, so he is
  centred (`fx_target` 0.5) and the answer is where his head box ENDS, which
  is what the caption hangs under. `COST_W` weights the trade per style —
  vertical weights resolution over centring, because a 9:16 window out of a
  16:9 source has already thrown away two thirds of the pixels (measured on
  hajri-23-taraweeh: the column weights bought dead-centre with a 2.5x
  upscale). Defensive parse, no silent defaults. Refuses on no face /
  off-frame caption / no room under the chin; an EMPTY shot (no reciter in
  most frames) is a centred window and no anchor key, not a refusal.
  `FACE_Y_BAND` is a printed note. `--annotate` is the primary check
  (invariant 4: model never consulted at render time).
- **`generate.py`** — YAML -> render. `load_config` / `align_words` /
  groups / silences / `suppress`/`nudge` (nudge last) /
  `print_verification`. Dispatches on `style:` with plan dict
  (`cfg/src/info/arabic/english/verses/tmp/out`).
- **`letterbox.py`** — finished 1920x1080 mp4 -> 1080x1920, black above and
  below. One x264 pass (crf 18, `veryfast`), audio stream-copied, and no
  decision of its own past the scale/pad. `bars` is the style that needs it:
  the 16:9 picture IS its canvas. `generate.py --vertical` calls it after the
  render and BEFORE tagging, so the tags survive. Standalone
  (`letterbox.py <reel.mp4> [out.mp4]`, replacing in place) it is not
  idempotent — a second pass letterboxes the letterbox to a postage stamp;
  under `--vertical` that cannot happen because generate renders 1920x1080
  fresh each time.
- **`publish.py`** — mp4 -> Instagram + Facebook. Tags from the file
  (`--surah/--ayat/--reciter` if missing). Caption from `tafsir()` + ayat +
  hashtags. Cover at `COVER_MS` 1550. Credentials in `.env`; no pixels
  touched.
- **`render_text.py`** — the `vertical` (1080x1920) and `horizontal`
  (1920x1080) styles: ONE look on two canvases, Pillow only (no libass).
  `layout()` SOLVES the anchor — landscape centres the median phrase's block,
  portrait puts its TOP under `face_bottom` — then lifts the whole block if
  the deepest card would reach the signature's band. Grade gradients at 48x27
  (that resolution IS the softness). `english_caps`, `vignette`, `dim`,
  fonts and scales are the only look knobs; the rest are constants here.
  A constant that cannot hold on both canvases is keyed by style.
- **`render_bars.py`** — 1920x1080 pills; the 16:9 picture IS the canvas, no
  letterbox. `layout` / `draw_layers` / `schedule` / `build_graph`.
  Constants measured from refs and carried to a 1920-wide picture (213pt /
  96px pill / 0.45 W ink cap); provenance in `legacy/templates/bars.yaml`.
  NEVER burns a signature — the only place for one is over the picture, which
  is not this look.
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
   partition the span; `face_bottom` only on `vertical`. Extend `DEFAULTS` +
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
