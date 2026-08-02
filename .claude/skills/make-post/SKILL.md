---
name: make-post
description: >-
  Make a Quran recitation clip/reel for the Quran clipping account, from a
  YouTube URL or a local video. Trigger on "make a post from <url>", "make a
  clip from this recitation", "turn this into a reel". Drives the pipeline/
  scripts: fetch.py -> transcribe.py -> quran.py -> a per-reel YAML config ->
  generate.py. THREE styles: `bars` (vertical 9:16, Arabic-only Thuluth on
  coloured pills, full FX stack), `hz` (native 1920x1080 landscape, Arabic +
  English in an off-centre column over graded footage) and `default` (Arabic +
  English over dimmed footage), selected with `style:` in the config.
---

# make-post — Quran reel pipeline

The pipeline is SCRIPTED. Your job is to run the scripts, read what they
print, author the one artifact that is yours — the per-reel YAML config —
and make the few judgement calls listed here. Do not re-derive by hand
anything a script already does.

Repo root `~/quran-clips`, all commands run from there.
`pipeline/README.md` is the pipeline reference, `INSTALL.md` the setup
guide. **On an unfamiliar machine run `./install.sh --check` FIRST** — one
second, and it names exactly what is missing and which stage it blocks.

## The sequence

```
python3 pipeline/fetch.py <url-or-path> [--name SLUG] [--proxy] [--timestamps A-B]
# read sources/<id>/captions.srt FIRST if it exists            # cheapest span finder
python3 pipeline/transcribe.py sources/<id>                      # ONLY if span still unknown
python3 pipeline/quran.py --search "<arabic from whisper.srt>"   # if span unknown
python3 pipeline/quran.py <s>:<a>-<b>                            # read the span
# write sources/<id>/<reel-name>.yaml                            # see below
tools/align-venv/bin/python pipeline/align.py sources/<id>/<reel-name>.yaml
tools/render-venv/bin/python pipeline/crop.py sources/<id>/<reel-name>.yaml --write   # bars + hz
tools/render-venv/bin/python pipeline/generate.py sources/<id>/<reel-name>.yaml
ffmpeg -v error -i reels/<reel-name>.mp4 -f null -               # decode gate
```

If you already know which verses the clip is, SKIP transcribe.py entirely:
name `surah`/`ayah_start`/`ayah_end` in the config and `align.py` times the
reel straight off the mushaf text. Whisper exists only to answer "which
verses is this?".

- **`fetch.py`** downloads into `sources/<id>/` (YouTube) or symlinks a local
  file. Everything is held to 30fps at intake: a `<=30fps` rendition is
  preferred on download, and a faster local file is re-encoded down once
  instead of being symlinked.
  It refuses a stub download (size + probeable duration), keeps
  an existing `source.mp4` untouched, and guarantees AAC audio (Opus in an
  MP4 deadlocks ffmpeg at some seek points). Captions land in
  `captions.srt` when YouTube has them.
  **When `captions.srt` exists, read it before running `transcribe.py`** —
  on a long source it answers "which verses are in here, and roughly where"
  in seconds instead of minutes. It arrives in rolling-window form (each
  block repeats the previous block's last line), so dedupe first: keep each
  block's last line, drop it if it equals the previous one. A 33-minute
  tahajjud recording collapses from 4405 lines to a ~440-line time-indexed
  map of every surah in it. Its timings are loose by a second or two — good
  enough to CHOOSE a span, never good enough to `trim:` one.
- **`transcribe.py`** writes word-level `whisper.json` + readable
  `whisper.srt`. Read `whisper.srt` to find where things are; trust the
  mushaf, not the transcript, for what is SAID — ASR drops and invents
  words during melismas.
- **`quran.py`** is the only source of Arabic text and translations.
  `--search` locates lossy transcript text in the mushaf; `s:a-b` prints
  the exact span; `--words` gives per-word English glosses.
- **`align.py`** times the reel: it takes the config's KNOWN mushaf text and
  CTC-forced-aligns it onto the audio (Meta's MMS). It cannot invent words,
  only place them, so its onsets are tighter than Whisper's. Writes
  `<reel>.align.json`, which `generate.py` then prefers. With no `trim:` in
  the config it aligns the whole source, derives the window from where the
  first and last word actually land, and writes `trim:` back into the YAML.
  That auto-trim only works when the source is ROUGHLY THE REEL — see
  "Trim" below for the long-source case, where it silently misfires.
- **`crop.py`** frames the reciter, for `bars` and `hz` only (`default`
  frames itself at render time, so skip it there). It shows sampled frames to
  a vision model, and from the head box it gets back it solves the three
  EQUAL gaps across the frame — edge, caption column, his head, edge — then
  writes `crop:` and `x_offset:` into the config. Run it BEFORE `generate.py`;
  once written the numbers are the config's, and re-running a render never
  calls out. This is an ESTIMATE, not an exact optimum — the reference reels
  it was calibrated against were never consistent with each other, so "fits
  the dimensions and looks good" is the bar. It REFUSES to write only when
  there is no face in the shot at all, or the caption anchor falls off the
  frame; a face landing outside the shipped reels' own 0.24-0.34 spread is
  now just a printed note (a bowed reciter often does this, harmlessly) —
  check `--annotate`, don't assume it needs a hand-set `crop:`. Shells out to
  the local `claude` CLI (`claude -p`), so it rides your existing Claude Code
  auth and needs no API key of its own; cached, so it is paid once and never
  at render time.
  **ALWAYS check the solve with `--annotate out.png` and LOOK at it** — see
  "When crop.py cannot see a face" below for the failure its guards miss.
- **`generate.py`** validates the config (unknown key = error), takes word
  timing from `<reel>.align.json` (else falls back to matching mushaf words
  onto the transcript), builds the captions, and renders. Read its report:
  the timing line, the VERIFICATION BLOCK (below), and, for bars, the
  per-card bar widths.

## Verify every card's English — against BOTH translations

`generate.py` prints a verification block before rendering: each card's
Arabic beside its authored English, then every verse in the two committed
editions — Saheeh International (`[sahih]`) and Mufti Taqi Usmani
(`[taqi]`). **You MUST check every card against both.** The test: the
card's English must cover the same clause as its Arabic words. One edition
can paraphrase in a way that hides a mis-split; where the two AGREE on
clause order, a card whose English contradicts them is mis-split — fix the
`groups` (or the card's `english:`), re-run, re-check. `python3
pipeline/quran.py <s>:<a>-<b>` prints both editions any time. Do not accept
a render whose cards you have not verified this way.

## The reel config — what you author

Schema: `tools/render-venv/bin/python pipeline/generate.py --print-schema`.
The minimum that makes a good reel:

```yaml
style: bars                       # or default | hz
signature: null                   # REQUIRED key; null = burn nothing
surah: 78
ayah_start: 31
ayah_end: 34
trim: [16.4, 48.0]                # seconds of source; see "Trim" below
groups:                           # one entry per caption card
  - n_words: 3                    # MUST sum to the span's word count
    english: "..."                # default + hz styles
    line_split: 2                 # bars: words on line 1 (omit = auto)
```

**`signature: null` is the default** — reels carry no watermark unless a
handle is asked for by name. The key is still REQUIRED on every config; it
is never silently defaulted. Do not copy a handle out of an older config.

**No Arabic ever goes in a config.** The caption text is sliced from the
committed mushaf by `n_words` — you choose WHERE the cards split, never
what they say. A wrong sum fails loudly before anything renders.

Count the words with the same function `generate.py` will:

```
tools/render-venv/bin/python -c "import sys; sys.path.insert(0,'pipeline'); import generate; \
  w=generate.spoken_words(generate.fetch_verses(57,16,16)); \
  print(len(w)); [print(i,x) for i,x in enumerate(w)]"
```

Do NOT count with `quran.ayah(s,a)['ar'].split()` — that counts marks that
are not spoken words and overshoots (57:16 reads 30 that way and is 28).
`ayah()` returns a dict, not a string. Printing the words INDEXED is worth
the extra line: card boundaries are then chosen by index, not by re-reading.

Judgement calls the scripts hand up to you:

- **Card splits are semantic.** Split at waqf/breath points; never let a
  card span two ayat (put a card boundary at every ayah boundary); avoid a
  card opening on a connective like بِكُمْ.
- **`suppress`** windows for anything that is not recitation (du'a,
  audience, talk).

## Trim: open ON the recitation, and measure the window

**The reel must start with the first word, not before it.** Dead air at the
head is where people scroll — even one second loses them. `align.py` now
MEASURES this, so it is no longer yours to do by hand:

- **Auto-trim** (no `trim:` in the config) cuts 0.12s before the first
  word's onset, clamped so it never reaches back past the start of the waqf
  that onset comes out of — on a short breath it takes what air there is
  rather than opening on the tail of the previous word. It prints what it
  did: `head: 120ms before the first word (waqf gap 0.50s)`.
- **A hand-set `trim:`** is never moved — it is yours — but the head gap is
  reported every run, and anything over 0.25s is called out with the trim
  start you should have used. Read that line.

The tail is deliberately not measured: a held final word wants its decay,
so it keeps a flat 0.30s.

**Leave `trim:` out and let `align.py` measure it ONLY when the source is
roughly the reel.** It aligns the whole source, so on a long recording —
a full tahajjud or taraweeh running several surahs end to end — the mushaf
text has nothing to anchor it against 30+ minutes of similar audio, and it
misfires QUIETLY: it still returns the right number of words and still
writes valid JSON. On a 33-minute source, widening a first-pass window by
16s moved the final word 18s downstream into a later ayah, and the only
tell was its score falling from -0.14 to -0.53.

So on any source materially longer than the reel:

1. Pick the rough span from `captions.srt` (or `whisper.srt`).
2. Set a bounded `trim:` by hand and run `align.py`.
3. **Read the score column, not just the word count.** A trailing word
   scoring far worse than its neighbours has been captured downstream.
   Re-bound the window and re-run rather than accepting it. (A low score on
   a long held madd mid-phrase is normal and means nothing.)

Also hand-set `trim:` when the reel is a deliberate excerpt of a longer
recitation of the same span, or when the source holds a second take you
want to exclude — the aligner cannot know which take you meant.

### Measuring an edge when `silencedetect` finds nothing

In a Haram recording there is no true silence: hall reverb never drops
below about -35dB and the ambience floor sits near -25dB, so
`silencedetect` returns zero hits at any usable threshold. Dump the audio
and read the envelope instead:

```
ffmpeg -v error -ss T -t N -i source.mp4 -ac 1 -ar 16000 -f s16le w.raw
# then RMS per 1600 samples (0.1s), printed as 20*log10(rms/32768)
```

Waqf pauses show up as 0.4-0.9s dips to -27…-34dB against speech at -11dB.
Relative minima are obvious by eye even when no absolute threshold
separates them. Onsets found this way agreed with a bounded CTC align to
within 0.16s — and disagreed with Whisper's segment start by ~1s, because
Whisper pulls an onset back into the previous word's tail. Cut a tail
inside the dip that follows the last word, before the next ayah starts.

## Repeated phrases (ibtidāʾ restarts)

A restarted phrase gets **its own card.** One caption must not sit across a
break the reciter himself made — cut the repeated phrase out of the card
that carries its completion:

```yaml
  - n_words: 2        # wa-la yakunu     <- said twice; its own card
  - n_words: 5        # ka-lladhina utu l-kitaba min qablu
```

This needs no schema escape and does not touch the partition invariant: the
phrase's words are simply their own group, and that card holds through both
utterances because both are saying the same words. Cutting it out usually
improves the rest of the split too — the completion gets to read as one
clause instead of being cut short.

Timing is handled for you, two ways over:

- `align.py` corrects the onset when `whisper.json` exists — Whisper
  transcribes both utterances, so two consecutive identical segments give
  the restart away, and it pulls the phrase's opening word back onto the
  first one. It PRINTS the correction; read that line.
- **Whisper does not always split them.** It can collapse a restart into
  ONE abnormally long word instead of two segments, and then `find_repeats`
  sees nothing and prints nothing. The tell is duration: in 57:16 the word
  `yakunu` came back spanning 3.1s against 0.7-1.2s for its neighbours.
  Scan the align output for a word several times longer than its
  neighbours before assuming there was no restart.
- A collapsed restart may still come out RIGHT: CTC alignment placed the
  phrase's first word on the FIRST utterance and let the second word span
  the gap and the restart, which is exactly what is wanted. **Check the
  card's start time against the envelope before reaching for `nudge:`.**

To override, `nudge: [{group: i, start: -X}]` — measured, not eyeballed,
and the indices are 0-BASED (card 8 in the printed block is `group: 7`).

## When crop.py cannot see a face

`crop.py`'s guards are consistency checks on the vision model's own
numbers. **None of them asks whether there is a face in the shot at all**,
and the model will always return a head box for something. If the reciter's
head is draped, hooded or turned away, it can box his SHOULDER and the
whole solve comes back looking clean — high confidence, zero spread across
frames, face landing at exactly 0.275. It happened on a source whose real
crown was at 0.05 H: the window it wanted to write had its top edge 130px
BELOW his crown and would have decapitated him in every frame.

So: **a confident, self-consistent solve is not evidence that it found a
face.** `--annotate` and look at the picture. The `posture is 'bowed'`
warning is about a DIFFERENT failure (a bowed reciter whose face sits low
in a tall head) and is easy to misread as "the number is fine".

A bowed, covered or turned-away reciter is a hand solve. Measure over
several frames spanning the reel's window rather than eyeballing one:

- crown y (take the highest he ever reaches), his body's near edge,
  where the congregation starts, where burned-in graphics start;
- choose the window to drop the graphics and the congregation together;
- the arithmetic in `targets()` is still right — put his head centre at
  **0.771 of the window** and keep the crown in frame with headroom.

For `x_offset`, the usual rule is to match the caption column's outer
margin to the reciter's. When his body runs off the window edge that margin
is zero and the rule cannot be used: centre the column in the free space
beside him instead. Remember the canvas differs — the same fraction is
`(f - 0.5) * 1080` for bars and `(f - 0.5) * 1920` for hz.

## Caption line balance (bars)

The account's standard, in priority order:

**Budget first: the type is 120pt and the ink cap is 486px, so plan on
2–3 words a line.** The cap is a frame fraction and did not grow with the
2026-08-01 resize, so a card that fitted at 96pt often will not now — a
three-word line that inked 420px inks 524px. `generate.py` reports the pt it
actually used; if it is below the nominal, one line in the reel is over the
cap and is dragging the whole reel's type down with it.

1. **The two lines of a card should be close in width** — aim within
   ~30px; over ~60px is visible. `generate.py` prints each card's bar
   width; the auto line-break already balances by width, so intervene only
   when a card still reads lopsided.
2. **Fix it by moving the CARD boundary first** (re-cut `n_words` across
   neighbouring groups), **the line break second** (`line_split`). There is
   no kashida lever in this pipeline — the config carries no Arabic to
   stretch.
3. **Single-line cards: avoid, except where the card closes on a waqf or
   the end of the ayah** — there they are encouraged, and a long held final
   word is exactly the case for one wide pill. At 120pt that pill is much
   harder to earn: two words usually fit on one line, three rarely do.
   `hadid-16-16-bars` lost its ayah-closing single line to the resize and
   the config says so — that is the pattern, not a bug to fix.
4. **`line_split` cannot beat the width cap.** Asking for one line only
   gets one if the words fit: a `line_split` whose line rasters past the cap
   is re-wrapped, silently, back to the automatic break. `generate.py`
   prints the cap — `Arabic pt=120 (nominal 120, widest raw NNNpx, cap
   486px)`. So the wide single pill an ayah-closing card would like is not
   always available.
5. **One over-cap line shrinks the WHOLE reel.** `fit_pt` scales the type
   so the widest line fits, so a single stubborn card takes every other
   card's size down with it (hadid-16-16 renders at 117, not 120, for one
   484px line). Before accepting that, try re-cutting the card boundary — but
   accept it rather than break a clause: 3pt is invisible, a phrase cut in
   the wrong place is not.
6. **Some cards simply will not balance — take the closer break and move
   on.** A four-word card whose 2nd word is short and 4th is long gives 394
   vs 229 at 3/1 and 208 vs 421 at 2/2. When the card is a whole ayah with a
   whole ayah either side, no boundary can be re-cut; when the words also
   overflow the cap, one line is out too. Pick the closer of the two, write
   down in the config why the alternatives were rejected, and stop. Do not
   keep re-rendering a card that has no better state.

## Styles

- `bars` — 1080x1920, graded letterbox band, Thuluth pills, wipe-in first
  card, sequential crossfades, and the FX chain (glow, barglow, textglow,
  scan, snow, heat). `fx: {heat: false}` cuts ~27% off the render (118s →
  87s on a 27.5s 8-card reel) for a timing/layout check — never judge the
  LOOK without the full stack. Bar colour is
  auto-derived from the clip's own graded footage; override with
  `bar_color: "#RRGGBB"` only per-reel.
- `hz` — native landscape 1920x1080, the account's original look: the
  footage reframed by `crop: {x,y,w,h}` (source px) and graded down, warm-
  white Uthmanic Hafs on ONE line with the ayah medallion inline, tracked
  ALL-CAPS English balanced onto 1-2 lines beneath it, 0.45s dissolves.
  The type sits in a COLUMN OPPOSITE the reciter, set with `x_offset`: pick
  the column centre so its outer margin matches the reciter's (the reference
  reel uses 0.31 W, i.e. `x_offset: -365`). The block is vertically centred
  automatically; `y_offset` is the per-clip nudge. There is no face
  detection — the crop is authored, so the render is reproducible.
- `default` — the landscape/vertical Arabic + English look. Output
  resolution is clamped: a low-quality source upscales to 720p minimum so
  captions are drawn at delivery resolution; a high-quality source caps at
  1080p. Bars is always 1080x1920, hz always 1920x1080.
- For `default` and `hz`, base the `english:` on Saheeh International
  (`quran.py <s>:<a>` prints both editions); cut it to match the cards,
  editing only at clause boundaries, and verify against BOTH editions (see
  above).
- Subtitles are CENTERED by default in every style; `x_offset`/`y_offset`
  (px) are the only placement knobs. The signature is ALWAYS horizontally
  centered — `signature_offset` (px, + lower / - higher) is its only knob.

## Egress: downloads on a cloud host

`fetch.py` is the ONLY script that leaves the machine. On a cloud host
YouTube bot-checks the datacentre IP, so configure the pool in `.env` and
pass `--proxy`:

    QC_PROXY_STATIC=user:pass@host:port,...      # static residential tier
    QC_PROXY_DATACENTER=user:pass@host:port,...  # fallback tier

Escalation is fixed: static residential -> datacentre -> fail. A rotating
proxy cannot download (the signed googlevideo URL embeds the resolving exit
IP), the bot check burns per exit IP (move endpoints, don't retry one
harder), and `--timestamps` + an authenticated proxy don't mix (the range
fetch runs through a child ffmpeg that cannot CONNECT-tunnel) — download
full and use `trim` instead.

## Hard rules

- **No model ever retypes Arabic.** Uthmani codepoints corrupt on
  retyping. Arabic comes from `pipeline/quran.py`, and the config schema is
  designed so you never need to write any.
- **`signature` is required on every config** — a string, or an explicit
  `null`. Never silently defaulted.
- **NEVER pip-install into the render interpreter.** Whisper and opencv
  live in their own venvs; a careless resolve once destroyed the
  RAQM-enabled Pillow that shapes Arabic. `./install.sh` rebuilds venvs
  correctly.
- **Never accept a download because the file exists** — fetch.py already
  gates on size + probeable duration; do not bypass it.
- **Decode-check the output** (`ffmpeg -v error -i ... -f null -`) before
  calling a render done: container metadata survives a corrupted bitstream.
- **Do not `open` produced artefacts.** Build the file, report the path.
- **`reels/` is both the output dir and the library.** Rendering the same
  config overwrites its own reel — that is fine. Never delete or overwrite
  a DIFFERENT reel, and never rename one without explicit intent: a posted
  reel's filename is its identity.
- The legacy implementations under `legacy/` are ARCHIVED references —
  never run them as part of this flow.
