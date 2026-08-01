# quran-clips

Turn a Qur'an recitation video — a YouTube URL or a local file — into a
subtitled reel:

```sh
python3 pipeline/fetch.py "https://www.youtube.com/watch?v=..."
# write sources/<id>/<reel>.yaml  (the per-reel recipe: verse span, card splits)
tools/align-venv/bin/python  pipeline/align.py    sources/<id>/<reel>.yaml
tools/render-venv/bin/python pipeline/crop.py     sources/<id>/<reel>.yaml --write
tools/render-venv/bin/python pipeline/generate.py sources/<id>/<reel>.yaml
# -> reels/<reel>.mp4
```

The timing comes from `align.py`: CTC forced alignment (Meta's MMS) of the
*known* mushaf text onto the audio. It never chooses WHAT was said, only
WHEN, so it cannot hallucinate a word, and its onsets land on the real
attack instead of inheriting the previous word's tail. Whisper
(`pipeline/transcribe.py`) is still here, but its only remaining job is
answering "which verses is this?" — name the span in the config and the
transcribe step drops out of the sequence. `crop.py` frames the reciter for
`bars` and `hz`; `default` frames itself at render time and skips it.

Every step is a script with an assertion behind it: caption Arabic is
*sliced from the committed Uthmani text by word index* — no model ever
retypes an Arabic word — and a config whose card splits don't cover the
verse range exactly fails loudly before a frame is drawn.

## Three styles

**`default`** — Arabic + English over the dimmed footage: centred type with
soft shadows, the ayah ornament, an English line from Saheeh International,
0.4s fades. Handles landscape and vertical, a face-centred 9:16 re-crop of
a horizontal source, and a still-photo background over an audio-only input.
Output is never below 720p (a low-quality source is upscaled so captions
are drawn at delivery resolution) and never above 1080p.

**`bars`** — vertical 1080x1920: the footage graded down and letterboxed
into a centred 16:9 band on pure black, white Thuluth on equal-height
colour pills (auto-derived from the clip's own graded footage), a
right-to-left wipe on the first card, strictly sequential crossfades, and a
band-confined FX chain — glow, bar glow, text glow, scan, procedural snow,
heat shimmer. The geometry and effects are pixel-forensics work measured
off reference reels, so they are not to be re-derived: `tests/graph_parity.py`
diffs the emitted filtergraph against a committed fixture byte for byte
after any edit to `render_bars.py` or `fx.py`, because a look change is an
owner decision and never a refactor side-effect. It is also no longer slow:
the 2026-08-01 speed work took a bars render from 372s of wall to ~96s for
a 27.5s reel, with every step of it PSNR'd against the render before it
(`OPTIMIZATIONS.md` has the measurements).

**`hz`** — native landscape 1920x1080, the account's original look: the
footage reframed by the config's `crop` and pushed down under one black
plate, warm-white Uthmanic Hafs on a single line with the ayah medallion
inline, tracked ALL-CAPS Albertus beneath it, 0.45s dissolves. The type
sits in a column *opposite* the reciter (`x_offset`), and there is no face
detection at render time — the crop is authored, so the render is
reproducible.

Demo clips of `bars` and `default` are in `docs/`.

## Layout

```
pipeline/       six workflow scripts + three style renderers
                (see pipeline/README.md):
                fetch.py  transcribe.py  quran.py  align.py  crop.py
                generate.py  render_default.py  render_bars.py
                render_hz.py  (+ fx.py, the bars FX)
sources/<id>/   one folder per source: source.mp4, captions.srt (if any),
                whisper.json / whisper.srt, and the per-reel *.yaml configs
                plus their *.align.json word timings
reels/          generated reels, flat — output only
assets/         fonts + the committed mushaf and BOTH English editions
                (Saheeh International, Mufti Taqi Usmani) + word glosses
tests/          graph_parity.py + the bars filtergraph fixture it diffs
legacy/         the first- and second-generation implementations, archived
                intact with their specs, tests and golden fixtures
```

## The reel config

One YAML per reel, kept in the source's own folder. `signature` is required
on every config — a string burned bottom-centre, or an explicit `null`;
`generate.py --print-schema` prints the authoritative schema. The heart of
it:

```yaml
style: bars                       # bars | default | hz
signature: null                   # REQUIRED key; null = burn nothing
surah: 78
ayah_start: 31
ayah_end: 34
trim: [16.4, 48.0]                # seconds of the source; omit it and
                                  # align.py measures the window and writes
                                  # it back here — but only when the source
                                  # is roughly the reel
groups:                           # caption cards, in Arabic word order
  - n_words: 3                    # must sum exactly to the span's word count
    english: "..."                # default + hz; bars is Arabic-only
x_offset: 0                       # subtitles are centered by default;
y_offset: 0                       # these px offsets are the only knobs
```

`crop.py` writes `crop:` and `x_offset:` into the config for `bars` and
`hz`: it samples frames from the reel's own window, asks a vision model
where the reciter's head and silhouette are, and solves the framing rule —
three equal gaps across the frame, edge | caption column | his head | edge.
That is authoring, never rendering: the answer becomes reviewed integers in
a tracked config, so re-rendering never calls out and the same config
renders identically anywhere.

Generation ends with a verification block: every card's Arabic beside its
authored English, then each verse in **both** committed editions — Saheeh
International and Mufti Taqi Usmani. Two translations on purpose: one
rendering can paraphrase in a way that hides a mis-split, and where the two
agree on clause order, a card whose English contradicts them is wrong.
Check every card against both before accepting a render.

## Setting up

```sh
./install.sh            # everything: tools check, venvs, face model, .env
./install.sh --check    # audit only
```

There are **three interpreters**, and which one you use matters:
`tools/render-venv` (Pillow *with RAQM* — without it Arabic renders
unjoined, silently) runs `generate.py`, `crop.py` and the renderers;
`tools/asr-venv` runs Whisper; `tools/align-venv` (torch) runs `align.py`.
The split exists so neither Whisper's nor torch's dependency tree can ever
replace the RAQM Pillow — it has happened. `./install.sh` is the only
sanctioned way to build them; never pip-install into the render venv.

`INSTALL.md` is the full guide. Machine config lives in `.env` (gitignored;
created from `.env.example`): the ASR backend/model, pinned binaries, and
the proxy pool for cloud hosts where YouTube bot-checks datacentre IPs —
`fetch.py --proxy` escalates static residential → datacentre → fail, one
sticky exit per fetch. Nothing in `.env` affects a rendered pixel.

## Driving it from Claude Code

`.claude/skills/make-post/SKILL.md` is the operating manual — ask for
"make a post from <url>" and the agent runs the sequence, authors the reel
config (choosing card boundaries by word count, never typing Arabic), reads
the verification block against both translations, and decode-checks the
output. The judgement calls the scripts can't make — where a held final
madd really ends, whether a card split reads as connected meaning, whether
`crop.py` actually found a face — are documented there for a human or agent
to decide.

## What is not in the repo

Downloaded footage, transcripts and rendered reels stay local (`sources/*`
media, `reels/`); the authored reel configs and fetched metadata
are committed. Two vendored text editions sit under `assets/quran/` plus
word-by-word glosses; the fonts are AM Thuluth, KFGQPC Uthmanic Hafs and
Albertus. Nothing here downloads or redistributes anyone's footage — the
pipeline points yt-dlp at a URL you choose, and what you may do with the
result is between you and whoever recorded it.
