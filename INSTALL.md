# Installing quran-clips

macOS or Linux. One script does the whole setup and is safe to re-run:

```sh
./install.sh            # install everything
./install.sh --check    # report what resolved, change nothing
```

It checks the system tools, builds the two Python environments, fetches the
face-detection model, and creates `.env` from the template. A green summary
means every pipeline stage can run; anything listed under `MISSING:` names
exactly which stage it blocks and the command that fixes it.

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
| `curl` | one-time face-model download | optional |

## 2. The two Python environments

`install.sh` builds both. They are deliberately separate — whisper's
dependency tree must never be installed into the interpreter that renders
Arabic (see "Arabic shaping" below):

* **`tools/render-venv`** — PyYAML + Pillow with RAQM. Runs `generate.py`
  and the two style renderers.
* **`tools/asr-venv`** — the Whisper backend: `mlx-whisper` on Apple
  silicon, `faster-whisper` (CPU/CUDA) everywhere else. Runs the
  transcription subprocess only.

`fetch.py`, `transcribe.py` and `quran.py` themselves run on any plain
`python3`.

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

Never `pip install` whisper, opencv, or anything heavy into the render
interpreter: a careless dependency resolve replacing its Pillow is exactly
the failure the two-venv split exists to prevent.

## 3. Machine configuration — `.env`

`install.sh` creates `.env` from `.env.example` when it is absent — this IS
part of the setup, not an optional extra. After the script runs, open
`.env` and fill in what applies to this machine (on a laptop with ffmpeg on
PATH that is usually nothing; on a cloud host it is at least the proxy
pool). Every key is optional and nothing in it affects a rendered pixel.
The ones that matter:

```sh
QC_ASR_BACKEND=mlx|faster      # default: mlx on Apple silicon, else faster
QC_ASR_MODEL=...               # hub id or a local path (offline machines)
QC_ASR_PYTHON=...              # override the asr interpreter
QC_FFMPEG= QC_FFPROBE= QC_YT_DLP=   # pin exact binaries
QC_PROXY_STATIC=user:pass@host:port,...      # fetch.py --proxy, tier 1
QC_PROXY_DATACENTER=user:pass@host:port,...  # fallback tier
```

Switching ASR backends re-times everything cut from a transcript (the two
runtimes do not emit identical word boundaries); `whisper.json` records
which backend wrote it.

The proxy pool is for cloud hosts, where YouTube bot-checks datacentre IPs.
Escalation is fixed — static residential, then datacentre, then fail — and
one exit is used per fetch, because a signed googlevideo URL embeds the exit
IP that resolved it and a rotating proxy cannot download at all.

## 4. Optional: the Claude Code CLI for framing

`pipeline/crop.py` solves each reel's crop and caption anchor by asking a
vision model where the reciter is. It shells out to the local `claude` binary
over your own auth -- no API key -- and caches every answer in the source's
`crop.json`. Without it you hand-write `crop:` and `x_offset:`/`face_bottom:`;
nothing else needs it, and rendering never calls it (invariant 4), so a reel
re-renders identically on a machine that has no `claude` at all.

## 5. Verify

```sh
./install.sh --check                 # every line green, .env present
tools/render-venv/bin/python pipeline/generate.py --print-schema
python3 pipeline/quran.py 1:1        # prints the ayah + BOTH translations
```

Then the full flow, end to end:

```sh
python3 pipeline/fetch.py "https://www.youtube.com/watch?v=..."
python3 pipeline/transcribe.py sources/<id>
# write sources/<id>/<reel>.yaml   (schema: generate.py --print-schema)
tools/render-venv/bin/python pipeline/generate.py sources/<id>/<reel>.yaml
```

`pipeline/README.md` documents the pipeline itself and the reel config
schema.
