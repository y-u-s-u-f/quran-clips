# quran-clips

Turn a Qur'an recitation video — a YouTube URL or a local file — into a
subtitled reel:

```sh
python3 pipeline/fetch.py "https://www.youtube.com/watch?v=..."
python3 pipeline/transcribe.py sources/<id>
# write sources/<id>/<reel>.yaml  (the per-reel recipe)
tools/render-venv/bin/python pipeline/generate.py sources/<id>/<reel>.yaml
# -> reels/<reel>.mp4
```

The pipeline downloads the source, transcribes it with word-level Whisper,
aligns the known-correct mushaf text against those timings, and renders
Pillow-drawn captions composited by ffmpeg. Every step is a script with an
assertion behind it: caption Arabic is *sliced from the committed Uthmani
text by word index* — no model ever retypes an Arabic word — and a config
whose card splits don't cover the verse range exactly fails loudly before a
frame is drawn.

## Two styles

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
off reference reels; the emitted filtergraph is verified byte-identical to
the frozen golden fixture of the previous implementation, so every measured
behaviour carried over. Demo clips of both styles are in `docs/`.

## Layout

```
pipeline/       the six scripts (see pipeline/README.md):
                fetch.py  transcribe.py  quran.py  generate.py
                render_default.py  render_bars.py  (+ fx.py, the bars FX)
sources/<id>/   one folder per source: source.mp4, captions.srt (if any),
                whisper.json / whisper.srt, and the per-reel *.yaml configs
reels/          generated reels, flat — output only
assets/         fonts + the committed mushaf and BOTH English editions
                (Saheeh International, Mufti Taqi Usmani) + word glosses
legacy/         the first- and second-generation implementations, archived
                intact with their specs, tests and golden fixtures
```

## The reel config

One YAML per reel, kept in the source's own folder. `signature` is the only
required key (a string burned bottom-centre, or an explicit `null`);
`generate.py --print-schema` prints the authoritative schema. The heart of
it:

```yaml
style: bars                       # bars | default
signature: "TilawatQuraniyyah"    # required; always horizontally centered
signature_offset: 0               # px, vertical only: + lower / - higher
surah: 78
ayah_start: 31
ayah_end: 34
trim: [16.4, 48.0]                # seconds of the source
groups:                           # caption cards, in Arabic word order
  - n_words: 3                    # must sum exactly to the span's word count
    english: "..."                # default style; bars is Arabic-only
x_offset: 0                       # subtitles are centered by default;
y_offset: 0                       # these px offsets are the only knobs
```

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
madd really ends, whether a card split reads as connected meaning — are
documented there for a human or agent to decide.

## What is not in the repo

Downloaded footage, transcripts and rendered reels stay local (`sources/*`
media, `reels/`); the authored reel configs and fetched metadata
are committed. Two vendored text editions sit under `assets/quran/` plus
word-by-word glosses; the fonts are AM Thuluth, KFGQPC Uthmanic Hafs and
Albertus. Nothing here downloads or redistributes anyone's footage — the
pipeline points yt-dlp at a URL you choose, and what you may do with the
result is between you and whoever recorded it.
