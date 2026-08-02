# pipeline/ — Qur'an reel pipeline

Turn a recitation video (YouTube or local) into a subtitled reel. Six
workflow scripts plus three style renderers; each step reads and writes plain
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

Whisper (transcribe.py, word-level)   -- ONLY to discover the verse span
    └── whisper.json + whisper.srt

Identify verses + timestamps
    └── quran.py search over the transcript (or captions), or the span
        stated in the reel config

Write the reel config YAML (by hand / by the agent)
    ├── verse groups       n_words splits + English per card
    ├── trim window        or omit it -- but only when the source is
    │                      roughly the reel; see `trim:` below
    └── config             style, signature, offsets, fonts, ...

align.py (CTC forced alignment of the KNOWN mushaf text)
    ├── <reel>.align.json  per-word timings
    ├── writes `trim:` back into the config when it was omitted
    └── corrects ibtida' restarts off whisper.json, when it exists --
        only when Whisper split the two utterances into separate
        segments; a restart collapsed into one long word is missed

crop.py (bars + hz only -- `default` frames itself at render time)
    ├── samples frames, asks a model (the local `claude` CLI) for the
    │   reciter's head box, silhouette, posture and any burned-in graphics
    ├── computes the 16:9 window: three EQUAL gaps across the frame --
    │   edge | caption column | his head | edge
    └── writes `crop:` and `x_offset:` back into the config, so the render
        stays offline and reproducible. Estimate, not an exact optimum: the
        reference reels it was calibrated against were never consistent with
        each other, so "fits the dimensions and looks good" is the bar.
        Refuses only when there is no face at all (draped, hooded, turned
        away -- a hand solve) or the caption anchor falls off the frame; a
        face outside the shipped reels' own 0.24-0.34 spread is now just a
        printed note, not a refusal. `--annotate` draws the solve over a
        frame and is NOT optional: the guards only check the model's numbers
        against each other, so a shot with no visible face returns a
        confident self-consistent solve boxed on something that is not his
        head, and only looking at the frame catches that.

generate.py
    ├── validates the config (unknown key = error; groups must cover the
    │   verse range's word count exactly)
    ├── word timing from <reel>.align.json when it exists, else aligns the
    │   mushaf words against whisper.json
    ├── builds/corrects captions (silences, suppress, nudge, verse numbers)
    └── dispatches on style:
        ├── render_default.py   landscape/vertical, Arabic + English
        ├── render_bars.py      1080x1920 letterbox band, Arabic-only pills
        └── render_hz.py        1920x1080 native landscape, Arabic + English
                                in an off-centre column over graded footage
```

## Directory layout

```
sources/<id>/                one folder per source video
    source.mp4               downloaded (fetch.py; <=30fps preferred),
                             symlinked local file (re-encoded to 30fps if
                             the original is faster), or
                             hand-placed
    captions.srt             YouTube Arabic auto-captions, when they exist
    whisper.json             word-level timings   (transcribe.py)
    whisper.srt              readable transcript  (transcribe.py)
    <reel-name>.yaml         one config per reel cut from this source
    <reel-name>.align.json   that reel's forced word timings (align.py)
    meta.yaml                fetched video metadata (legacy sources carry it)

reels/<reel-name>.mp4        generated output only, flat, no subfolders

pipeline/                    this package
    fetch.py  transcribe.py  quran.py  crop.py  generate.py
    render_default.py  render_bars.py  render_hz.py

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

# 2. transcribe it -- ONLY if you don't know which verses these are
python3 pipeline/transcribe.py sources/<id>

# 3. find the verses (if you don't already know them)
python3 pipeline/quran.py --search "نص من التلاوة"
python3 pipeline/quran.py 78:31-40          # read the span + translation

# 4. write sources/<id>/<reel-name>.yaml    (schema below)

# 5. time it                            -> <reel-name>.align.json (+ trim:)
tools/align-venv/bin/python pipeline/align.py sources/<id>/<reel-name>.yaml

# 6. render                             -> reels/<reel-name>.mp4
tools/render-venv/bin/python pipeline/generate.py sources/<id>/<reel-name>.yaml
```

Steps 2-3 exist only to answer "which verses is this?". If you already
know, name the span in the config and neither `transcribe.py` nor
`whisper.json` is needed at all: `align.py` times the reel from the mushaf
text directly.

## The reel config

`tools/render-venv/bin/python pipeline/generate.py --print-schema` prints the
authoritative schema (generated from the code, cannot drift). The shape:

```yaml
style: bars                       # bars | default | hz
signature: null                   # REQUIRED on every reel; null = burn
                                  # nothing, and null is the default

surah: 78                         # omit all three to auto-detect from the
ayah_start: 31                    # transcript (reliable for short spans only)
ayah_end: 40

trim: [15.0, 55.0]                # seconds of the source this reel covers.
                                  # Omit it and align.py measures it off the
                                  # recitation and writes it back here -- but
                                  # only sound when the source is roughly the
                                  # reel; it aligns the WHOLE file, so on a
                                  # multi-surah recording set it by hand.
                                  # Head: align.py measures it -- auto-trim
                                  # cuts 0.12s before the first word (never
                                  # past the waqf it sits in), and a hand-set
                                  # trim gets its head gap reported, with a
                                  # warning over 0.25s. Tail keeps 0.30s.

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
verse_numbers: true               # ayah ornament (default: on for `default`
                                  # and `hz`, off for bars)

# default style: orientation, detect_subject, background_image + frame_size,
#   arabic_font (uthmanic_hafs|thuluth), english_font, arabic_scale,
#   english_scale, line_gap, dim, text_width, english_max_chars
# bars style:    bar_color ("#RRGGBB", omit for auto-derivation from the
#   graded footage)
# bars + hz:     crop {x,y,w,h}, the reframing window in SOURCE pixels
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
1080p. `bars` is always 1080x1920, `hz` always 1920x1080.

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
transcription needs a Whisper backend in `tools/asr-venv`; forced alignment
needs `ctc-forced-aligner` (torch) in `tools/align-venv`. The extra venvs
exist so neither whisper's nor torch's dependency tree can ever replace the
RAQM Pillow.

`align.py` installs from git, not PyPI: the PyPI name `ctc-forced-aligner`
belongs to an unrelated English-only project. Its MMS checkpoint (~1.2 GB)
downloads to the HuggingFace cache on first run.

`docs/asr-and-alignment.md` records which ASR models were measured for this
pipeline and why the smaller Quran-specific ones were rejected — read it
before proposing a model swap.

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
  The type is 120pt nominal on a 54px pill (both raised 1.25× on
  2026-08-01 to match the account's newer reels). The 486px per-line ink
  cap did NOT rise with them, so cards run 2–3 words to a line and
  `fit_pt` quietly shrinks the whole reel when one line still overflows —
  see the caption-balance rules in the make-post skill.
  The geometry is the legacy build's; the emitted filtergraph is NO LONGER
  byte-identical to its frozen golden fixture (the 2026-08-01 speed work
  changed it deliberately — see `OPTIMIZATIONS.md` and CLAUDE.md invariant
  2), so a render is now held to PSNR against the previous render instead.
  Per-reel switches via `fx:` (e.g. `fx: {heat: false}` — heat is ~27% of
  the render, 118s → 87s on a 27.5s 8-card reel, and the one to drop for a
  timing preview; never judge the LOOK without the full stack).
* **`hz`** — native landscape 1920x1080, the account's original look
  (legacy `scripts/render_text.py` + `build_render.py`): the footage
  reframed by `crop` and pushed down under one black plate (flat dim + soft
  vignette + a backing gradient under the type), warm-white Uthmanic Hafs
  on a single line with the ayah medallion inline, tracked ALL-CAPS
  Albertus balanced onto 1–2 lines beneath it, one shared soft drop shadow,
  0.45s dissolves. The type sits in a COLUMN opposite the reciter — set it
  with `x_offset`; the block is vertically auto-centred and `y_offset` is
  the per-clip nudge. No face detection: the crop is authored, so a render
  is reproducible. Verified against `reels/BADR-AL-TURKI-AHZAB-56-56.mp4`
  (its config is `sources/YkXjYyKwHJ4/ahzab-56-56.yaml`).

Subtitles are **centered by default** in every style; `x_offset` /
`y_offset` (px) are the only placement knobs.
