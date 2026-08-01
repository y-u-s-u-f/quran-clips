---
name: make-post
description: >-
  Make a Quran recitation clip/reel for the Quran clipping account, from a
  YouTube URL or a local video. Trigger on "make a post from <url>", "make a
  clip from this recitation", "turn this into a reel". Drives the pipeline/
  scripts: fetch.py -> transcribe.py -> quran.py -> a per-reel YAML config ->
  generate.py. TWO styles: `bars` (vertical 9:16, Arabic-only Thuluth on
  coloured pills, full FX stack) and `default` (Arabic + English over dimmed
  footage), selected with `style:` in the config.
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
python3 pipeline/transcribe.py sources/<id>                      # ONLY if span unknown
python3 pipeline/quran.py --search "<arabic from whisper.srt>"   # if span unknown
python3 pipeline/quran.py <s>:<a>-<b>                            # read the span
# write sources/<id>/<reel-name>.yaml                            # see below
tools/align-venv/bin/python pipeline/align.py sources/<id>/<reel-name>.yaml
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
style: bars                       # or default
signature: "TilawatQuraniyyah"    # REQUIRED; null = burn nothing
surah: 78
ayah_start: 31
ayah_end: 34
trim: [16.4, 48.0]                # seconds of source; pick from whisper.srt
groups:                           # one entry per caption card
  - n_words: 3                    # MUST sum to the span's word count
    english: "..."                # default style only
    line_split: 2                 # bars: words on line 1 (omit = auto)
```

**No Arabic ever goes in a config.** The caption text is sliced from the
committed mushaf by `n_words` — you choose WHERE the cards split, never
what they say. Count words with `python3 pipeline/quran.py <s>:<a>` (the
displayed words are the count; waqf signs don't count). A wrong sum fails
loudly before anything renders.

Judgement calls the scripts hand up to you:

- **Leave `trim:` out and let `align.py` measure it.** It derives the window
  from where the first and last mushaf word actually land and writes it into
  the config. Only set `trim:` by hand when the reel is a deliberate excerpt
  of a longer recitation of the same span, or when the source holds a second
  take you want to exclude — the aligner has no way to know which take you
  meant.
- **Card splits are semantic.** Split at waqf/breath points; never let a
  card span two ayat (put a card boundary at every ayah boundary); avoid a
  card opening on a connective like بِكُمْ.
- **Repeated phrases** (ibtidāʾ restarts): `align.py` handles these itself
  when `whisper.json` exists — Whisper transcribes both utterances, so two
  consecutive identical segments give the restart away, and it pulls the
  phrase's opening word back onto the first one. It PRINTS the correction;
  read that line. Uncorrected only if you skipped `transcribe.py`. To
  override, `nudge: [{group: i, start: -X}]` — measured, not eyeballed, and
  the indices are 0-BASED (card 8 in the printed block is `group: 7`).
- **`suppress`** windows for anything that is not recitation (du'a,
  audience, talk).

## Caption line balance (bars)

The account's standard, in priority order:

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
   word is exactly the case for one wide pill.

## Styles

- `bars` — 1080x1920, graded letterbox band, Thuluth pills, wipe-in first
  card, sequential crossfades, and the FX chain (glow, barglow, textglow,
  scan, snow, heat). `fx: {heat: false}` halves render time for a timing/
  layout check — never judge the LOOK without the full stack. Bar colour is
  auto-derived from the clip's own graded footage; override with
  `bar_color: "#RRGGBB"` only per-reel.
- `default` — the landscape/vertical Arabic + English look. Base the
  `english:` on Saheeh International (`quran.py <s>:<a>` prints both
  editions); cut it to match the cards, editing only at clause boundaries,
  and verify against BOTH editions (see above). Output resolution is
  clamped: a low-quality source upscales to 720p minimum so captions are
  drawn at delivery resolution; a high-quality source caps at 1080p. Bars
  is always 1080x1920.
- Subtitles are CENTERED by default in both styles; `x_offset`/`y_offset`
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
