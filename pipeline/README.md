# pipeline/ — Qur'an reel pipeline

Turn a recitation video (YouTube or local) into a subtitled reel. Four
workflow scripts plus two style renderers; each step reads and writes plain
files in the repo tree, so every stage can be run, inspected and re-run on
its own.

```
Source
├── Local file / hand-made folder
│   └── (no captions) ──────────────► Whisper
│
└── YouTube URL
    └── fetch.py (yt-dlp, optional proxy, optional timestamps)
        ├── captions available ─────► captions.srt (verse identification
        │                             can start from these)
        └── no captions ────────────► Whisper

Whisper (transcribe.py, word-level)
    └── whisper.json + whisper.srt

Identify verses + timestamps
    └── quran.py search over the transcript (or captions), or the span
        stated in the reel config

Write the reel config YAML (by hand / by the agent)
    ├── timestamps         trim window; per-card word groups
    ├── verse groups       n_words splits + English per card
    └── config             style, signature, offsets, fonts, ...

generate.py
    ├── validates the config (unknown key = error; groups must cover the
    │   verse range's word count exactly)
    ├── aligns the mushaf words against whisper.json for word-level timing
    ├── builds/corrects captions (silences, suppress, nudge, verse numbers)
    └── dispatches on style:
        ├── render_default.py   landscape/vertical, Arabic + English
        └── render_bars.py      1080x1920 letterbox band, Arabic-only pills
```

## Directory layout

```
sources/<id>/                one folder per source video
    source.mp4               downloaded (fetch.py), symlinked local file, or
                             hand-placed
    captions.srt             YouTube Arabic auto-captions, when they exist
    whisper.json             word-level timings   (transcribe.py)
    whisper.srt              readable transcript  (transcribe.py)
    <reel-name>.yaml         one config per reel cut from this source
    meta.yaml                fetched video metadata (legacy sources carry it)

reels/<reel-name>.mp4        generated output only, flat, no subfolders

pipeline/                    this package
    fetch.py  transcribe.py  quran.py  generate.py
    render_default.py  render_bars.py

assets/                      fonts + the committed mushaf/translation editions
legacy/                      the first- and second-generation implementations,
                             archived intact (see legacy/PIPELINE-README.md)
```

## Quickstart

```sh
# 0. one-time: interpreters (see "Environments" below)

# 1. get a source                      -> sources/<id>/source.mp4 (+captions)
python3 pipeline/fetch.py "https://www.youtube.com/watch?v=..."
python3 pipeline/fetch.py ~/Videos/recitation.mp4 --name my-reciter

# 2. transcribe it                     -> whisper.json + whisper.srt
python3 pipeline/transcribe.py sources/<id>

# 3. find the verses (if you don't already know them)
python3 pipeline/quran.py --search "نص من التلاوة"
python3 pipeline/quran.py 78:31-40          # read the span + translation

# 4. write sources/<id>/<reel-name>.yaml    (schema below)

# 5. render                             -> reels/<reel-name>.mp4
tools/render-venv/bin/python pipeline/generate.py sources/<id>/<reel-name>.yaml
```

## The reel config

`tools/render-venv/bin/python pipeline/generate.py --print-schema` prints the
authoritative schema (generated from the code, cannot drift). The shape:

```yaml
style: bars                       # bars | default
signature: "TilawatQuraniyyah"    # REQUIRED on every reel; null = burn nothing

surah: 78                         # omit all three to auto-detect from the
ayah_start: 31                    # transcript (reliable for short spans only)
ayah_end: 40

trim: [15.0, 55.0]                # seconds of the source this reel covers

groups:                           # caption cards, in Arabic word order
  - n_words: 4                    # must sum EXACTLY to the span's word count
    english: "Indeed, for the righteous is attainment"
    line_split: 2                 # bars only: words on line 1 (omit = auto)
  - n_words: 3
    english: "gardens and grapevines"

x_offset: 0                       # subtitle offset in px from CENTERED
y_offset: 0                       # (+x right, +y down); centered by default
signature_offset: 0               # px, vertical only (+ lower / - higher);
                                  # the signature is ALWAYS horizontally
                                  # centered

# corrections, all optional:
suppress: [[33, 58]]              # leave these second-windows uncaptioned
nudge: [{group: 3, start: -1.8}]  # shift one card's start/end, applied last
verse_numbers: true               # ayah ornament (default: on for `default`,
                                  # off for bars)

# default style: orientation, detect_subject, background_image + frame_size,
#   arabic_font (uthmanic_hafs|thuluth), english_font, arabic_scale,
#   english_scale, line_gap, dim, text_width, english_max_chars
# bars style:    bar_color ("#RRGGBB", omit for auto-derivation from the
#   graded footage), crop {x,y,w,h}
# plumbing:      input / whisper / output, only to override the defaults
#   (source.* and whisper.json beside the config; reels/<config-name>.mp4)
```

Generation ends with a **verification block**: every card's Arabic beside
its authored English, then each verse in BOTH committed editions — Saheeh
International and Mufti Taqi Usmani. Check every card against both before
accepting a render: one rendering can paraphrase in a way that hides a
mis-split, and where the two editions agree on clause order, a card whose
English contradicts them is wrong.

Output resolution: the `default` style's canvas is clamped so its short
side sits in [720, 1080] — a low-quality source upscales to 720p minimum
(captions draw at delivery resolution), a high-quality source caps at
1080p. `bars` is always 1080x1920.

Rules the pipeline enforces rather than trusts:

* **The caption Arabic is the committed Uthmani text**, sliced by word index
  from `assets/quran/uthmani.json`. The transcript's only job is timing —
  no model-typed Arabic ever reaches the screen.
* **`groups[].n_words` must sum exactly** to the verse range's word count. A
  mis-split fails loudly instead of silently shifting every later caption.
* **An unknown config key is an error** — a typo'd one would otherwise be
  silently ignored.
* **`signature` must be present** in every config: a string, or an explicit
  `null`.

## Environments

`./install.sh` sets everything up (and `--check` audits it); `INSTALL.md`
is the full guide. In short: `fetch.py`, `transcribe.py` and `quran.py` run
on any python3 with ffmpeg and yt-dlp on PATH; rendering needs Pillow
**with RAQM** (HarfBuzz Arabic shaping) plus PyYAML in `tools/render-venv`;
transcription needs a Whisper backend in `tools/asr-venv`. The two venvs
exist so whisper's dependency tree can never replace the RAQM Pillow.

Machine config lives in `.env` (gitignored; see `.env.example`):
`QC_ASR_BACKEND` / `QC_ASR_MODEL` / `QC_ASR_PYTHON`, `QC_FFMPEG` /
`QC_FFPROBE` / `QC_YT_DLP`, and the proxy pool `QC_PROXY_STATIC` /
`QC_PROXY_DATACENTER` used by `fetch.py --proxy` (static residential first,
then datacentre — a rotating exit cannot download at all, and the bot check
burns per exit IP). Switching ASR backends re-times everything cut from a
transcript; whisper.json records which backend wrote it.

## Styles

* **`default`** — the first-generation look (legacy
  `quran_reel_pipeline.py`): footage dimmed under centred Arabic + English,
  soft shadows, 0.4s fades, optional still-photo background
  (`background_image` + audio-only input), optional face-centred 9:16
  re-crop of a horizontal source. All text is Pillow-drawn PNG — the legacy
  libass path is gone (libass mis-rasterises Quranic fonts, and slim ffmpeg
  builds lack the filter entirely).
* **`bars`** — vertical 1080x1920: the footage graded down and letterboxed
  into a centred 16:9 band on pure black, white Thuluth on equal-height
  colour pills (auto-derived from the clip's own graded footage), a
  right-to-left wipe on the first card, strictly sequential crossfades
  after it, and the full band FX chain — glow, bar glow, text glow, scan,
  procedural snow, heat shimmer — confined to the band (pipeline/fx.py).
  Geometry and the emitted filtergraph are verified byte-identical to the
  legacy build's frozen golden fixture, so every measured behaviour carries
  over. Per-reel switches via `fx:` (e.g. `fx: {heat: false}` — heat is
  ~half the render time and the one to drop for a timing preview; never
  judge the LOOK without the full stack).

Subtitles are **centered by default** in both styles; `x_offset` /
`y_offset` (px) are the only placement knobs.
