# AGENTS.md — writing code in quran-clips

To *produce a reel*, invoke the `make-post` skill
(`.agents/skills/make-post/SKILL.md`).

## Codebase map

Twelve files under `pipeline/`. Eight are runnable scripts that read/write
plain files: `fetch`, `transcribe`, `quran`, `align`, `crop`, `generate`
(the stages), plus the standalone `letterbox` and `publish`. Four are modules
only the render imports: `render_text`, `render_bars`, `fx`, `render_common`.
`generate.py` doubles as the shared base: at import it needs only the stdlib
and `quran.py` (PyYAML loads inside `load_config`, the renderers inside
`run_config`), so every script imports it under any interpreter. It owns the
one `.env` reader (`envvar`), `FFMPEG`/`FFPROBE` (resolved once) and
`SOURCE_NAMES`.

- **`fetch.py`** — URL/local -> `sources/<id>/`. yt-dlp, no third-party
  imports. Player-client ladder `web_embedded, tv_simply, android_vr`
  (`web_embedded` is the only one reaching 1080p without a PO token, measured
  2026-09); a client offering under `MIN_HEIGHT` 720p is skipped, a pinned
  `--client` takes what it offers. One player-API request per fresh fetch
  (`--load-info-json`). Resolution beats fps; over `MAX_FPS` 30 is re-encoded
  to 30 (`cap_fps`). `ensure_aac`, stub gate. `--timestamps` records
  `captions.none`. Proxy credentials reach yt-dlp via its environment, never
  argv. A complete source — usable media plus `captions.srt` or
  `captions.none` — never reaches the network on a re-fetch. Measurements
  are in the file.
- **`transcribe.py`** — `source.*` -> `whisper.json` + `.srt`. Backend
  subprocess against `tools/asr-venv` via `_SNIPPETS` (strings so they run
  under an interpreter that cannot import this package). Contract:
  `{"words":[{"w","t0","t1","p"}], "segments", "backend", "model"}`.
- **`quran.py`** — offline mushaf; ONLY source of Arabic/translations
  (`assets/quran/`). `ayah()` returns stored bytes; `search()` is IDF +
  contiguity. Normalisation tables are frozen: a change must leave
  `normalize()` byte-identical over all 6236 ayat. `ar.muyassar` is for
  `tafsir()` / post captions only.
- **`align.py`** — config -> `<reel>.align.json`. CTC forced alignment
  (MMS / `tools/align-venv`) of known mushaf text — WHEN only, never WHAT.
  Omit `trim:` -> align whole source, write the measured window back; omit
  the span -> identify it from Whisper and write it back beside `trim:`
  (`write_back`, a line edit; `safe_dump` eats comments). Both are only
  sound when the source is roughly the reel — on a long source, name the
  span and hand-set `trim:`. Head measured via `rms_envelope` (Haram reverb
  floors `silencedetect`); tail keeps `TAIL_PAD`. Whisper is needed for span
  discovery and for ibtidāʾ repeat repair (`find_repeats` needs Whisper to
  have split the two utterances, and only scans the trim window). Takes
  several configs per run, which is how the reels cut from one source share
  the model load; a failed config is reported and the batch continues (exit
  non-zero).
- **`crop.py`** — authoring-time framing for EVERY style: `crop:` plus
  `x_offset:` (bars, horizontal) or `face_bottom:` (vertical). Shells out to
  the `claude` CLI (`claude -p`, local auth); arithmetic decides the window.
  Column styles use `targets()`'s equal-gap rule with the caption column at
  `CAPTION_W` (bars 0.45; horizontal 0.55, the wider of render_text's Arabic
  and English caps); `vertical` has no column beside him, so he is centred
  (`fx_target` 0.5) and the answer is where his head box ENDS, which is what
  the caption hangs under. `COST_W` weights the trade per style — vertical
  weights resolution over centring, because a 9:16 window out of a 16:9
  source has already thrown away two thirds of the pixels (measured on
  hajri-23-taraweeh: the column weights bought dead-centre with a 2.5x
  upscale). Answers cached in `crop.json`, keyed by model, a hash of
  prompt/schema/`FRAME_W`, the source and the frame timestamps. Defensive
  parse, no silent defaults. Refuses on no face / off-frame caption / no
  room under the chin; an EMPTY shot (no reciter in most frames) is a
  centred window and no anchor key, not a refusal. `FACE_Y_BAND` is a
  printed note. `--annotate` is the primary check (invariant 4: model never
  consulted at render time).
- **`generate.py`** — YAML -> render. `load_config` (+ `check_shapes`) /
  `resolve_span` / `align_words` / groups / silences / `suppress`/`nudge`
  (nudge last) / `print_verification`. Dispatches on `style:` with plan dict
  (`cfg/src/info/arabic/english/tmp/out/portrait/meta`). `--vertical` sets
  `portrait`, which the renderer folds into its own graph (`PORTRAIT_PAD`);
  `meta` (title/comment/artist) is written by the delivery encode, so the
  finished file needs no remux. `--verify-only` stops after the verification
  block, off a 32px-wide cut of the window — seconds, not a render.
- **`letterbox.py`** — standalone: a finished landscape mp4 -> 1080x1920,
  black above and below, one x264 pass (crf 18, `veryfast`), audio
  stream-copied (`letterbox.py <reel.mp4> [out.mp4]`, replacing in place).
  Refuses portrait input. `generate.py --vertical` does NOT call it — the
  same scale/pad runs inside the render's single encode, one lossy
  generation cheaper.
- **`publish.py`** — mp4 -> Instagram + Facebook. Tags from the file
  (`--surah` + `--ayat` together, `--reciter`, if missing). Caption from
  `tafsir()` + ayat + hashtags. Cover at `COVER_MS` 1550. `--ig-only` /
  `--fb-only` are exclusive. Credentials in `.env`; no pixels touched. Tagged
  green in Finder once at least one platform posted (`--draft` never); a
  partial post says which platform went out.
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
  96px pill / 0.45 W ink cap); the measurement notes are in git history
  (`legacy/templates/bars.yaml` at `9bc669b`). NEVER burns a signature — the
  only place for one is over the picture, which is not this look.
- **`fx.py`** — bars effects, ported verbatim from the first-generation
  pipeline (git history at `9bc669b`); do not re-derive.
- **`render_common.py`** — what both styles deliver and build with: crf 18 /
  `slow` / AAC 192k, -14 LUFS two-pass loudnorm, `FADE_IN_S`/`FADE_OUT_S`
  (one pair for picture and sound), the mp4 tags, `PORTRAIT_PAD`,
  `THREAD_QUEUE_SIZE`, `source_chain`, `norm_ar`, `PROBE`, `fit_pt` and
  `trim_to_ink`. The LOOK stays in the renderer that owns it.
- **`tests/`** — `graph_parity.py` (bars filtergraph + input argv goldens,
  built from `golden/at-tawbah-128-128.yaml`) and `wrap_parity.py` (the
  English wrap against its enumerated definition).
- **`docs/asr-and-alignment.md`** — measured ASR comparison; read before a
  model swap. Whisper kept only because it is autoregressive (ibtidāʾ).
- **`OPTIMIZATIONS.md`** — render cost profile + rejected ideas; read
  before a performance change. Update in the same change.
- **`todo.md`** — the open list.
- **`sources/<id>/`** media untracked; configs tracked for two example
  sources (`hajri-23-taraweeh`, `YkXjYyKwHJ4`). **`reels/`** untracked;
  **`assets/`** fonts + editions.

## Invariants

1. **No Arabic typed by a model** — including in source code. Slice from
   `quran.py` by word index; configs carry counts. In `.py`, `\uXXXX`
   escapes; a change to `quran.py`'s tables is checked codepoint-by-codepoint
   against the previous commit's (`git show HEAD:pipeline/quran.py`) over all
   6236 ayat.
2. **`render_bars.py` + `fx.py` byte-identity** with
   `tests/golden/bars-filtergraph.txt` and `tests/golden/bars-argv.txt`.
   After any edit:

       tools/render-venv/bin/python tests/graph_parity.py

   Intended look change: PSNR a known reel first (~45dB floor), then
   `--bless`.
3. **Interpreter split.** Render under `tools/render-venv` (RAQM Pillow;
   hard-exit without it). Whisper: `asr-venv`. Align: `align-venv`. Only
   `./install.sh` builds them. `generate.py` stays importable without Pillow
   or PyYAML at module level — every other script imports it.
4. **Nothing outside committed files may affect a rendered pixel.** Style
   constants in renderers; `.env` is machine config only.
5. **Loud validation at config time**: unknown key = error; group sums
   partition the span; `face_bottom` only on `vertical`; value shapes
   (`groups`/`nudge` entries, fonts, `trim`, `crop`) checked in
   `check_shapes`. Extend `DEFAULTS` + `config_schema()` together.

## How to write changes

- **Keep code concise, optimized, and minimal.** Comments and docs: CURRENT
  or planned behavior only — never previous behavior.
- **Minimize.** Smallest diff that solves it. No abstractions for
  hypotheticals. New key needs a concrete reel; new file needs a reason the
  existing ones can't hold it.
- **No new dependencies** without an owner decision.
- **Keep measured numbers.** A constant with a measurement behind it changes
  only with a new measurement.
- **Comments explain WHY** (measurement/incident), never what the next line
  does. Phrase as current rationale, not a changelog.
- **One owner per fact.** State it once, point to it elsewhere.
- **Bars: layout / draw / graph stay separate.**
- **ffmpeg:** bound every `-loop 1` with `-t`; keep `THREAD_QUEUE_SIZE`;
  loudnorm floats stay floats (`-14.0`); `heat` needs `perlin` (>= 7.1).
- **Verify:** end-to-end on a cached source, decode-check, verification
  block, golden parity for bars, `tests/wrap_parity.py` for the English wrap;
  for `quran.py`, whole-mushaf against the previous commit.
- **Docs travel with code:** `DEFAULTS`, `config_schema()`,
  `pipeline/README.md`, `.agents/skills/make-post/SKILL.md`.

## The skill

Two files drive this repo: `AGENTS.md` and
`.agents/skills/make-post/SKILL.md`. Edit those files, never a copy.

- `make-post` is the ONLY workflow for producing a reel. A general
  video-editing, captioning or reel-teardown skill does not know this
  pipeline's mushaf slicing, CTC alignment or golden filtergraph, and
  substituting one silently produces a different reel.
- Keep repo knowledge in the repo. Nothing here belongs in private memory
  or a skill store outside it — a fact kept anywhere else will drift from
  the one kept here.
- `crop.py`'s `claude` subprocess is a tool on PATH, like `ffmpeg`.
