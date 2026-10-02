# Installing quran-clips

macOS or Linux. One script does the whole setup and is safe to re-run:

```sh
./install.sh            # install everything
./install.sh --check    # report what resolved, change nothing
```

It checks the system tools, builds the three Python environments, checks the
vision API settings, and creates `.env` from the template. A green summary means
every pipeline stage can run; anything listed under `MISSING:` names exactly
which stage it blocks and the command that fixes it.

## 1. System tools

Installed with your package manager, not by the script:

```sh
# macOS
brew install ffmpeg yt-dlp python3
# Debian/Ubuntu
sudo apt install ffmpeg yt-dlp python3-venv
```

| tool | needed by | notes |
|---|---|---|
| `python3` | everything | 3.10+ |
| `ffmpeg` / `ffprobe` | rendering, probing, audio | the bars `heat` effect needs ffmpeg's `perlin` source (ffmpeg ≥ 7.1); older builds work with `fx: {heat: false}` in the reel config |
| `yt-dlp` | `fetch.py` for YouTube sources | local files work without it |

## 2. The three Python environments

`install.sh` builds all three. They are deliberately separate — whisper's and
torch's dependency trees must never be installed into the interpreter that
renders Arabic (see "Arabic shaping" below):

* **`tools/render-venv`** — PyYAML + Pillow with RAQM. Runs `generate.py`,
  `crop.py` and the two style renderers.
* **`tools/asr-venv`** — the Whisper backend: `mlx-whisper` on Apple
  silicon, `faster-whisper` (CPU/CUDA) everywhere else, or whichever
  `QC_ASR_BACKEND` in `.env` names. Runs the transcription subprocess only.
* **`tools/align-venv`** — `ctc-forced-aligner` (torch, Meta's MMS model,
  ~1.2 GB on first use). Runs `align.py`. Installed from git: the PyPI
  package of that name is an unrelated project.

`fetch.py`, `transcribe.py`, `quran.py`, `publish.py` and `letterbox.py` run
on any plain `python3`.

### Arabic shaping (RAQM) — the one thing worth understanding

Pillow must be built against RAQM (HarfBuzz + FriBiDi). Without it, Arabic
does not fail — it silently renders unjoined, left-to-right, and every
caption is wrong. The renderers hard-exit at import when RAQM is missing
rather than produce that.

Recent pip wheels of Pillow bundle RAQM on macOS and manylinux, which is
what `install.sh` tries first. When the wheel lacks it but your system
python's Pillow has it, the script rebuilds the venv with
`--system-site-packages` to inherit that Pillow instead. If both routes
fail, install a RAQM-enabled Pillow system-wide (e.g. Homebrew python +
`libraqm`) and re-run `./install.sh`.

Never `pip install` whisper, torch, opencv, or anything heavy into the render
interpreter: a careless dependency resolve replacing its Pillow is exactly
the failure the split exists to prevent.

## 3. Machine configuration — `.env`

`install.sh` creates `.env` from `.env.example` when it is absent. Open it
and fill in what applies to this machine: on a laptop with ffmpeg on PATH
that is usually nothing; on a cloud host it is at least the proxy pool; to
publish, the Meta credentials. `.env.example` documents every key, the
precedence (shell > `.env` > default) and the proxy escalation order.

## 4. Optional: a vision API for framing

`pipeline/crop.py` solves each reel's crop and caption anchor by asking a
vision model where the reciter is. It calls any OpenAI-compatible API set by
`QC_VISION_BASE_URL`, `QC_VISION_API_KEY` and `QC_VISION_MODEL` in `.env`,
and caches every answer in the source's `crop.json`. Without it you
hand-write `crop:` and `x_offset:`/`face_bottom:`; nothing else needs it, and
rendering never calls it, so a reel re-renders identically on a machine with
no API key at all.

## 5. Verify

```sh
./install.sh --check                 # every line green, .env present
tools/render-venv/bin/python pipeline/generate.py --print-schema
python3 pipeline/quran.py 1:1        # prints the ayah + BOTH translations
```

Then run the flow in `README.md` (fetch, transcribe, align, crop, generate);
`.agents/skills/make-post/SKILL.md` is the step-by-step version.
