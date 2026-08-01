# CLAUDE.md — writing code in quran-clips

To *produce a reel* rather than change code, invoke the `make-post` skill
(`.claude/skills/make-post/SKILL.md`) and follow it.

## Codebase map

Qur'an recitation video -> subtitled reel. Seven standalone scripts under
`pipeline/`; each is runnable on its own, reads and writes plain files, and
owns one stage. There is deliberately NO shared util module — `fetch.py`
and `transcribe.py` each carry their own ~15-line `.env` reader so every
script stays independently runnable; do not "deduplicate" that.

- **`pipeline/fetch.py`** — source intake -> `sources/<id>/`. YouTube via
  yt-dlp (proxy-pool escalation, `web_embedded` retry, `ensure_aac`,
  stub-download gate in `usable()`), local files via symlink. Everything is
  held to `MAX_FPS` (30) at intake -- the format string prefers a `<=30fps`
  rendition and a faster local file is re-encoded down once, rather than
  paying for the surplus frames in every later decode. No third-party imports.
- **`pipeline/transcribe.py`** — `sources/<id>/source.*` -> `whisper.json`
  + `whisper.srt`. Backend (mlx/faster) selected per machine, run as a
  SUBPROCESS with inline source snippets (`_SNIPPETS`) against
  `tools/asr-venv` — the snippets are strings precisely so they run under an
  interpreter that cannot import this package. The word contract is
  `{"words": [{"w","t0","t1","p"}], "segments": [...], "backend", "model"}`;
  everything downstream consumes only that.
- **`pipeline/quran.py`** — the offline mushaf and the ONLY source of
  Arabic text and translations (`assets/quran/`: uthmani, en.sahih,
  en.taqi, en.wbw). `ayah()` returns stored bytes verbatim; `normalize()`/
  `tokens()`/`skeleton()` produce throwaway matching skeletons; `search()`
  is IDF + contiguity scoring for lossy transcript text. Ported from
  `legacy/qc/quran.py` and verified byte-identical on all 6236 ayat — treat
  its normalisation tables as frozen.
- **`pipeline/align.py`** — reel config -> `<reel>.align.json`. CTC forced
  alignment (Meta's MMS via `ctc-forced-aligner`, in `tools/align-venv`) of
  the KNOWN mushaf text onto the audio: it never chooses WHAT was said, only
  WHEN, so it cannot hallucinate and its onsets beat Whisper's. Omitting
  `trim:` makes it align the whole source and write the measured window back
  into the YAML (`write_trim`, a line edit — `safe_dump` would eat the
  configs' hand-written comments). Whisper is needed ONLY to discover the
  verse span; name the span and transcribe.py is skippable entirely.
  Ibtidāʾ restarts (a phrase recited twice) have only one reference word to
  claim them, so alignment times the second utterance — `find_repeats`/
  `apply_repeats` recover the first from whisper.json's consecutive
  identical segments, the one thing the transcript knows that the mushaf
  cannot. The scores do NOT reveal a restart; a held madd scores as low.
- **`pipeline/generate.py`** — per-reel YAML -> render. Owns everything
  style-independent: `load_config` (DEFAULTS is the schema; unknown key =
  error; `signature` required), `resolve_paths` (input/whisper/output
  default beside the config / into `reels/`), `align_words` (Needleman-
  Wunsch of normalised mushaf words onto transcript words; unmatched words
  interpolated; result strictly monotonic), grouping (`apply_groups`
  enforces the n_words-sum invariant), silences, `suppress`/`nudge`
  (nudge applied LAST so nothing can undo a hand fix),
  `print_verification` (dual-translation block). Dispatches on `style:` to
  a renderer's `render(plan)`; the `plan` dict
  (`cfg/src/info/wav/arabic/english/verses/tmp/out`) is the whole
  interface between generate and the renderers.
- **`pipeline/render_default.py`** — Arabic + English over dimmed footage,
  all Pillow (no libass anywhere — it mis-rasterises Quranic fonts).
  `output_size()` clamps the canvas short side into [720, 1080].
  Full-frame RGBA card PNGs overlaid with fade-alpha by one ffmpeg call in
  `composite()`.
- **`pipeline/render_bars.py`** — the vertical pill style. Split into
  `layout()` (pure numbers), `draw_layers()` (consumes exactly those
  numbers — the two must never disagree), `schedule()` (sequential
  transitions with gap-fitting), and `build_graph()` (the filtergraph).
  Every constant at the top of the file is a measured value from reference
  reels; the provenance notes live in `legacy/templates/bars.yaml` and
  `legacy/style/refs2/`.
- **`pipeline/fx.py`** — the bars effects: `Graph` (declaration-order
  filtergraph builder with deferred `tap()` splits), the six `Effect`
  classes with their three hooks (plate / per_phrase / apply), and the
  snow shader. Ported verbatim from `legacy/qc/`; the scalar algebra is not
  to be re-derived.
- **`legacy/`** — the two previous implementations, archived intact with
  specs, tests and golden fixtures. Reference material only: never run it,
  never edit it, never import from it in pipeline code.
- **`sources/<id>/`** per-source workspace (configs tracked, media not);
  **`reels/`** flat output, never tracked; **`assets/`** fonts + editions.

## The load-bearing invariants

1. **No Arabic is ever typed by a model — including in source code.**
   Caption text is sliced from `quran.py` by word index; configs carry
   counts, not text. In .py files, write Arabic as `\uXXXX` escapes or
   verify retyped literals codepoint-by-codepoint against the legacy file
   (this was done for every regex/table in `quran.py`, `render_bars.py`).
2. **`render_bars.py` + `fx.py` are under a byte-identity contract** with
   `legacy/tests/golden/at-tawbah-128-128/filtergraph.txt`. After ANY edit
   to either, rebuild the graph for that clip (phrases from its clip.yaml
   with ar1/ar2 -> text+line_split and t0/t1 -> start/end;
   `layout(..., x_offset=-216)`; `schedule(phrases, 23.720)`; tint
   recovered from the golden's glow scalars via rr/0.35*255; the golden's
   own `[0:a]` chain as `ln`; crop 48,30,1280x720; all switches on;
   sig_path None) and diff against the golden — it must match exactly.
   A look change is an owner decision, never a refactor side-effect.
3. **Interpreter split.** generate/render run ONLY under
   `tools/render-venv/bin/python` (RAQM Pillow — without RAQM Arabic
   renders unjoined, silently; the renderers hard-exit on it); whisper runs
   under `tools/asr-venv`, `align.py` under `tools/align-venv` (torch).
   Never pip-install anything into the render interpreter; `./install.sh`
   is the only sanctioned venv builder. `align.py` imports `generate` only
   for config/path/verse helpers — nothing that pulls in Pillow.
4. **Nothing outside committed files may affect a rendered pixel.** Style
   values are constants in the renderer files; `.env` is machine config
   (binaries, ASR, proxies) only.
5. **Validation fails loudly at config time**: unknown key = error, group
   sums must partition the span exactly, `signature` must be present.
   Extend `DEFAULTS` + `config_schema()` together when adding a key.

## How to write changes here

- **Minimize.** The smallest diff that fully solves the problem; no new
  abstractions, registries, or option surfaces for hypothetical needs. A
  new config key needs a concrete reel that needs it. A new file needs a
  reason the existing six can't hold the code.
- **No new dependencies.** The pipeline is stdlib + Pillow + PyYAML (+ the
  isolated ASR/opencv venvs). Adding anything else is an owner decision to
  propose, not make.
- **Prefer porting to inventing.** Almost every behaviour here was
  measured or learned from an incident, and the legacy tree holds the
  reference implementation and the write-up. Search `legacy/` before
  writing something "new"; carry logic over verbatim and keep its numbers.
- **Comments explain WHY, with the measurement or incident** ("opus in mp4
  deadlocks ffmpeg at some seek points", "0.96 px RMS @720"), never what
  the next line does. Match the existing register; module docstrings carry
  the design rationale.
- **Keep layout/drawing/graph separation** (bars): numbers in `layout()`,
  pixels in `draw_layers()`, filters in `build_graph()`. New behaviour goes
  in the layer that owns it.
- **ffmpeg discipline**: bound every `-loop 1` graph with `-t` (looped
  inputs never EOF); keep `THREAD_QUEUE_SIZE` (>12 inputs deadlocks);
  loudnorm floats must stay floats (`-14.0` formats differently from
  `-14`); `heat` needs ffmpeg's `perlin` (>= 7.1).
- **Verify like the repo does**: run the touched script end-to-end on the
  cached source (`sources/iBAYTGTcMt8/` has committed test configs),
  decode-check the output (`ffmpeg -v error -i ... -f null -`), read the
  verification block, and for bars edits run the golden parity check. For
  `quran.py` changes, diff behaviour against `legacy/qc/quran.py` across
  the whole mushaf, not on samples.
- **Docs travel with code**: a schema change updates `DEFAULTS`,
  `config_schema()`, `pipeline/README.md`, and the make-post skill in the
  same change.
