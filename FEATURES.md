# Recommended features

Proposals only. Each one names what it touches, what it costs, and which
invariant it has to hold. Ordered by leverage on the authoring loop, which is
where the pipeline's real cost is -- the render is minutes, the caption split
is the part that needs a person.

Items marked *(todo.md)* are already on the owner's list; the entry here is
the concrete shape, not a new idea.

---

## 1. A committed verse-split library *(todo.md)*

**What.** `assets/splits/<surah>.yaml`, one reviewed `groups:` block per
ayah: `n_words`, `english`, and `line_split` for the bars style. A reel
config then names a span and inherits the split:

```yaml
surah: 33
ayah_start: 56
groups: from-library        # or omit entirely once the span resolves
```

**Why this is the biggest one.** Every other stage is already automatic:
`fetch` downloads, `align` times, `crop` frames, `generate` renders. The one
step that still needs a human is splitting a verse into cards and writing
each card's English -- and `print_verification` exists precisely because only
a reader can judge it. Doing that once per ayah, in a tracked file, converts
the pipeline's slowest step into a lookup, and every later reel over the same
ayat inherits a split that has already been checked against both editions.

**Touches.** `generate.apply_groups` gains a resolver; `config_schema()` and
`DEFAULTS` gain the key; `pipeline/README.md` and `SKILL.md` travel with it.
A `--emit-splits` on `generate.py` would seed the library from the configs
already in `sources/`.

**Invariants.** 1 holds -- the library carries word COUNTS and English, never
Arabic; the Arabic is still sliced out of `quran.ayah()` by index. 5 holds
better than today: the sum check runs against the library at load.

**Cost.** The resolver is small. The library is the work, and it is
incremental: a surah at a time, starting with the ones already cut.

---

## 2. One driver command, with a resume point *(todo.md)*

**What.** `pipeline/make.py <config> [--from align] [--to generate]` running
align, crop, generate and optionally publish, skipping stages whose output is
already current, and printing what it skipped and why.

**Why.** `SKILL.md` is five commands under three interpreters. That is
correct and it is also the reason a re-run after a one-line config edit means
remembering which stages the edit invalidated. Every stage already knows how
to answer that question (`trim_note`, `crop.json`'s cache key,
`check_align_window`); nothing collects the answers.

**Touches.** A new script that imports the stages as modules, plus an
interpreter map, since align cannot run under the render venv (invariant 3).

**Invariants.** 3 is the constraint: the driver has to `subprocess` into the
right venv per stage rather than import across them.

---

## 3. Preview and verify-only modes

**What.** Two flags on `generate.py`:

- `--verify-only` -- resolve the span, align, group, print the verification
  block, stop. No render.
- `--preview` -- short side 640, `veryfast`, fx off, for a timing or split
  check in seconds rather than a minute (or 75s on bars).

**Why.** The verification block is the artefact the authoring loop turns on,
and today the only way to see it is to pay a full render. `fx: {heat: false}`
is the existing escape hatch and it is bars-only.

**Touches.** `run_config` returns before the renderer for the first; the
renderers take a scale factor and an encode override for the second.

**Invariants.** 2 -- a preview must not be blessable, so `--preview` should
refuse to write over a reel path that already exists, and the golden must be
built from the full-size path.

---

## 4. Configurable transitions *(todo.md)*

**What.** Per-config `transition: {crossfade: 0.45, fade_in: 0.3,
fade_out: 0.5}` for the text styles, validated like every other key.

**Why.** The owner's note is "we kinda merge the images together when
transitioning between text frames". That is a real defect, not a preference
(OPTIMIZATIONS.md, audit bug 3): `render_text.schedule` has no sequential
guard, so two cards are drawn over each other at every changeover. Fix the
guard first, then expose the durations -- exposing them over the bug just
makes the overlap adjustable.

**Touches.** `render_text.schedule` (port `render_bars.schedule`'s shrink),
`DEFAULTS`, `config_schema()`.

**Invariants.** 4 -- the numbers stay in the config, which is committed.

---

## 5. A word-by-word style

**What.** A style that shows one Arabic word at a time with its English
gloss beneath, timed off the alignment that already exists per word.

**Why.** `assets/quran/en.wbw.json` is committed and `quran.words()` reads
it, and nothing in the pipeline uses either -- it is reachable only from
`quran.py --words`. Meanwhile `align.py` produces exactly one span per
display word. The data for this style is already on disk and already timed;
what is missing is a renderer.

**Touches.** A new `render_words.py` beside the other two, dispatched from
`run_config`. It can reuse the text style's grade plate, shadow and fonts.

**Invariants.** 1 -- slice by index from `quran.ayah()`, as `spoken_words`
already does. `quran.words()` returns `None` when the gloss table and the
mushaf disagree on word count (true for 37:130), so the style must refuse
that span loudly rather than render an off-by-one.

---

## 6. More Arabic and English fonts *(todo.md)*

**What.** Add Lifta and any others to `ARABIC_FONTS` / `ENGLISH_FONTS`.

**Why.** Both tables are already dicts keyed by config name, and
`arabic_font`/`english_font` are already config keys. This is a data change.

**Touches.** The font file under `assets/fonts/`, one table entry, and for an
English face the `stroke_macron` measurement (does it have U+0100, or does
ALLĀH need a drawn bar). `FONT_NEEDS_AYAH_MARK` needs the same one-off check
per Arabic face: get it wrong and the ayah ornament renders as two
concentric rings.

**Invariants.** 4 -- the font ships in `assets/`, so a render stays
reproducible from the repo.

---

## 7. A published-reel ledger

**What.** `pipeline/catalog.py`: read every mp4 in `reels/`, print span,
reciter, style, duration and published state as a table; `--json` for
machines.

**Why.** "The mp4 is the record" is already the design -- `publish.py`
rebuilds a whole caption from four tags. The only index over those records is
a green Finder label, which is macOS-only, invisible from a terminal, and
lost the moment a file is copied. A ledger makes "what have I posted, and
what is rendered but not posted" answerable, and it needs no new state: the
tags are already there.

**Touches.** One new script. No change to any existing stage.

---

## 8. A pre-publish quality gate

**What.** `pipeline/check.py <reel.mp4>`: decode the whole file, then assert
duration against the config, exact canvas size for the style, 30fps,
measured loudness within a band of -14 LUFS, a non-black cover frame at
`COVER_MS`, and audio present and AAC. `publish.py` runs it unless
`--no-check`.

**Why.** Publishing is the one irreversible stage, and the checks it does
today are a landscape warning and a cover-offset clamp. Every failure mode
this would catch has a precedent in the code's own comments: a missing moov
atom from an unbounded `-loop 1`, a fade-in cover frame, an Opus track.

**Touches.** One new script plus a call in `publish.main`.

---

## 9. Batch render and batch align

**What.** `generate.py --all` / `align.py <config>...` over several configs,
rendering the ones whose output is missing or older than their config.

**Why.** `align.py` reloads the 1.2GB MMS model per invocation, so a source
with three reels pays it three times (OPTIMIZATIONS.md, audit optimization
6). Batching is the fix for that and the natural shape for an overnight
re-render after a style change.

**Touches.** `align.main` takes `nargs="+"`; `generate` gains a discovery
walk over `sources/*/*.yaml`.

---

## 10. English and gloss search in `quran.py`

**What.** `quran.py --search-en "conferred blessing"` over `en.sahih` and
`en.taqi`, and over the word-by-word glosses.

**Why.** `search()` today takes Arabic tokens, which is right for matching a
transcript and wrong for authoring, where the thing you remember is the
English. The editions are already loaded and the IDF machinery already
exists; this is a second index over text that is already in memory.

**Touches.** `quran.py` only, and it stays offline.

---

## 11. Per-config audio controls *(todo.md, "Automatic Volume")*

**What.** `audio: {lufs: -14.0, fade_in: 0.3, fade_out: 0.5, hpf: null}` in
the config, plus an intake loudness report from `fetch.py` naming a source
that is clipped or far off the target.

**Why.** Two-pass loudnorm at -14 LUFS already runs in both renderers, so
levels are consistent; what is not exposed is the target, the fades, or any
way to see that a source arrived clipped before the render normalises it and
keeps the distortion. A high-pass would also be the honest place to deal with
Haram hall rumble, which `align.py` already documents as the reason
`silencedetect` is unusable on these sources.

**Touches.** `AUDIO` in both renderers becomes config-merged (see the
`render_common.py` note in OPTIMIZATIONS.md -- do this after that merge, not
twice), `DEFAULTS`, `config_schema()`.

**Invariants.** 4 -- in the config, not `.env`.

---

## 12. Caption sidecar export

**What.** Write `reels/<name>.srt` (and `.ass`) of the final cards beside the
mp4, from the same `arabic`/`english` lists the renderer burns.

**Why.** The card list is fully resolved by the time `render()` is called and
is thrown away after it. A sidecar costs nothing to produce, makes the burned
captions diffable between renders, and is what a platform that accepts real
captions would want.

**Touches.** One writer in `generate.run_config`. `transcribe.write_srt`
already has the timestamp formatter.

---

## Not recommended

- **Scheduled publishing.** Facebook can schedule an unpublished reel;
  Instagram's Graph API cannot, so the feature would work on one platform and
  silently not on the other. `--draft` already leaves both ready to post by
  hand.
- **A vision model at render time.** Invariant 4. `crop.py` writes numbers
  into the config for exactly this reason.
- **Replacing Whisper outright** *(todo.md names Tarteel and
  wav2vec2-quran-phonetics)*. Worth measuring, but it is an evaluation that
  belongs in `docs/asr-and-alignment.md` and not a feature: Whisper is kept
  only because it is autoregressive and so transcribes an ibtidāʾ repeat
  twice, which is what `align.find_repeats` needs. A model that scores better
  on word error rate but emits one utterance would remove that correction
  without anything saying so.
