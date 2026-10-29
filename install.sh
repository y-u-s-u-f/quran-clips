#!/usr/bin/env bash
# install.sh -- set up everything the pipeline needs on this machine.
#
#     ./install.sh            full setup (idempotent; re-run any time)
#     ./install.sh --check    report only, install nothing
#
# What it does, in order:
#   1. checks the system tools (python3, ffmpeg, ffprobe, yt-dlp -- the last
#      three as QC_FFMPEG / QC_FFPROBE / QC_YT_DLP resolve, like every script)
#   2. builds tools/render-venv  -- PyYAML + a Pillow whose RAQM does Arabic
#      shaping (falls back to --system-site-packages when the pip wheel's
#      Pillow lacks RAQM but the system python's has it)
#   3. builds tools/asr-venv    -- mlx-whisper on Apple silicon,
#      faster-whisper everywhere else (QC_ASR_BACKEND overrides, read from
#      the shell or .env like every script reads it)
#   3b. builds tools/align-venv -- ctc-forced-aligner (torch) for align.py
#   3c. builds tools/render_gpu -- every style's Metal renderer, from
#       pipeline/gpu/render_gpu.swift with swiftc (macOS; rebuilt when older
#       than the source)
#   4. checks the vision API settings pipeline/crop.py uses (optional)
#   5. copies .env.example -> .env when absent
#   6. prints a doctor-style summary of what resolved and what is missing
#
# The extra venvs exist so whisper's and torch's dependency trees can NEVER
# be installed into the interpreter that renders Arabic: without RAQM, every
# caption silently renders unjoined.
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
ok=()    # summary lines
bad=()   # missing pieces + what they block

say()  { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --check)  CHECK_ONLY=1;;
        *) say "unknown flag: $arg (want --check)"; exit 2;;
    esac
done

# ---------------------------------------------------------------------------
# 1. system tools
# ---------------------------------------------------------------------------
say "== system tools"
PY="$(command -v python3 || true)"
if [ -n "$PY" ]; then
    ok+=("python3      $PY ($("$PY" -V 2>&1 | cut -d' ' -f2))")
else
    bad+=("python3: not found -- everything needs it. brew install python3 / apt install python3-venv")
fi
# QC_* from the shell, else .env -- through pipeline/generate.py's one reader.
envvar() { "$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import generate; print(generate.envvar(sys.argv[2]) or "")' "$ROOT/pipeline" "$1" 2>/dev/null; }

# The binaries the pipeline will run, so the check is of those and not PATH's.
FFMPEG=ffmpeg
for pair in ffmpeg:QC_FFMPEG ffprobe:QC_FFPROBE yt-dlp:QC_YT_DLP; do
    tool="${pair%%:*}"
    cmd="$([ -n "$PY" ] && envvar "${pair#*:}")"
    cmd="${cmd:-$tool}" miss="not found"
    [ "$cmd" != "$tool" ] && miss="$cmd not found"
    [ "$tool" = ffmpeg ] && FFMPEG="$cmd"
    if have "$cmd"; then
        ok+=("$(printf '%-12s %s' "$tool" "$(command -v "$cmd")")")
    else
        case "$tool" in
            ffmpeg|ffprobe) bad+=("$tool: $miss -- cutting, probing, audio and the mux need it. brew install ffmpeg / apt install ffmpeg");;
            yt-dlp)         bad+=("yt-dlp: $miss -- fetch.py needs it for YouTube sources (local files still work). brew install yt-dlp / apt install yt-dlp");;
        esac
    fi
done
# the heat effect needs ffmpeg's perlin source (ffmpeg >= 7.1)
if have "$FFMPEG" && ! "$FFMPEG" -hide_banner -filters 2>/dev/null | grep -q ' perlin '; then
    bad+=("ffmpeg has no 'perlin' filter -- the bars heat effect will fail; use fx: {heat: false} or upgrade ffmpeg")
fi

[ -z "$PY" ] && { say ""; printf '  MISSING: %s\n' "${bad[@]}"; exit 1; }

# ---------------------------------------------------------------------------
# 2. render venv (PyYAML + RAQM Pillow)
# ---------------------------------------------------------------------------
say "== render venv"
RVENV="$ROOT/tools/render-venv"
RPY="$RVENV/bin/python"

raqm_ok() { "$1" -c "from PIL import features; import sys; sys.exit(0 if features.check('raqm') else 1)" 2>/dev/null; }
yaml_ok() { "$1" -c "import yaml" 2>/dev/null; }

if [ "$CHECK_ONLY" = 0 ]; then
    if [ ! -x "$RPY" ]; then
        say "  creating $RVENV"
        "$PY" -m venv "$RVENV" || exit 1
        "$RPY" -m pip -q install --upgrade pip
    fi
    yaml_ok "$RPY" || "$RPY" -m pip -q install pyyaml
    raqm_ok "$RPY" || "$RPY" -m pip -q install --upgrade pillow
    if ! raqm_ok "$RPY" && raqm_ok "$PY"; then
        # the pip wheel's Pillow lacks RAQM but the system python's has it:
        # rebuild the venv over the system site-packages and keep that Pillow.
        say "  pip Pillow lacks RAQM; rebuilding venv with --system-site-packages"
        rm -rf "$RVENV"
        "$PY" -m venv --system-site-packages "$RVENV" || exit 1
        "$RPY" -m pip -q install pyyaml
    fi
fi
if [ -x "$RPY" ] && raqm_ok "$RPY" && yaml_ok "$RPY"; then
    ok+=("render venv  $RPY (Pillow+RAQM, PyYAML)")
else
    bad+=("render venv: $RPY missing RAQM Pillow or PyYAML -- generate.py cannot run. See INSTALL.md 'Arabic shaping'")
fi

# ---------------------------------------------------------------------------
# 3. ASR venv
# ---------------------------------------------------------------------------
say "== asr venv"
AVENV="$ROOT/tools/asr-venv"
APY="$AVENV/bin/python"
ASR_BACKEND="$(envvar QC_ASR_BACKEND)"
if [ -z "$ASR_BACKEND" ]; then
    if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
        ASR_BACKEND=mlx
    else
        ASR_BACKEND=faster
    fi
fi
case "$ASR_BACKEND" in
    mlx)    ASR_PKG="mlx-whisper";    ASR_MOD="mlx_whisper";;
    faster) ASR_PKG="faster-whisper"; ASR_MOD="faster_whisper";;
    *) say "QC_ASR_BACKEND=$ASR_BACKEND: want mlx or faster"; exit 2;;
esac
# Each heavy import costs seconds, so a working venv is imported once and the
# answer kept; only an install attempt earns a second import.
ASR_OK=0
[ -x "$APY" ] && "$APY" -c "import $ASR_MOD" 2>/dev/null && ASR_OK=1
if [ "$CHECK_ONLY" = 0 ] && [ "$ASR_OK" = 0 ]; then
    if [ ! -x "$APY" ]; then
        say "  creating $AVENV"
        "$PY" -m venv "$AVENV" || exit 1
        "$APY" -m pip -q install --upgrade pip
    fi
    say "  installing $ASR_PKG (first transcription will also download the model)"
    case "$ASR_BACKEND" in
        # mlx-whisper lists torch, but only its checkpoint converter imports
        # it, and torch has no Python 3.15 wheel on PyPI (2026-10).
        mlx) "$APY" -m pip -q install --no-deps mlx-whisper &&
             "$APY" -m pip -q install mlx numba numpy tqdm more-itertools \
                 tiktoken huggingface_hub scipy;;
        *)   "$APY" -m pip -q install "$ASR_PKG";;
    esac
    "$APY" -c "import $ASR_MOD" 2>/dev/null && ASR_OK=1
fi
if [ "$ASR_OK" = 1 ]; then
    ok+=("asr venv     $APY ($ASR_PKG)")
else
    bad+=("asr venv: cannot import $ASR_MOD -- transcribe.py cannot run. $APY -m pip install $ASR_PKG")
fi

# ---------------------------------------------------------------------------
# 3b. align venv (CTC forced alignment -- torch/transformers, kept out of the
#     render venv for the same reason as whisper's tree)
# ---------------------------------------------------------------------------
say "== align venv"
FVENV="$ROOT/tools/align-venv"
FPY="$FVENV/bin/python"
# PyPI's "ctc-forced-aligner" is an UNRELATED project (an English-only onnx
# aligner); Meta's MMS one only ships from git.
FPKG="git+https://github.com/MahmoudAshraf97/ctc-forced-aligner.git"
FOK=0
[ -x "$FPY" ] && "$FPY" -c "import ctc_forced_aligner, yaml" 2>/dev/null && FOK=1
if [ "$CHECK_ONLY" = 0 ] && [ "$FOK" = 0 ]; then
    if [ ! -x "$FPY" ]; then
        say "  creating $FVENV"
        "$PY" -m venv "$FVENV" || exit 1
        "$FPY" -m pip -q install --upgrade pip
    fi
    say "  installing ctc-forced-aligner + torch (first align will also download the ~1.2GB MMS model)"
    # Python 3.15 wheels are only on the nightly index (2026-10).
    "$FPY" -m pip -q install torch ||
        "$FPY" -m pip -q install --pre torch --index-url https://download.pytorch.org/whl/nightly/cpu
    "$FPY" -m pip -q install "$FPKG" pyyaml
    "$FPY" -c "import ctc_forced_aligner, yaml" 2>/dev/null && FOK=1
fi
if [ "$FOK" = 1 ]; then
    ok+=("align venv   $FPY (ctc-forced-aligner)")
else
    bad+=("align venv: cannot import ctc_forced_aligner -- align.py cannot run (generate.py still works off whisper.json). $FPY -m pip install $FPKG pyyaml")
fi

# ---------------------------------------------------------------------------
# 3c. GPU renderer (Metal + VideoToolbox; the only renderer, every style)
# ---------------------------------------------------------------------------
say "== gpu renderer"
GSRC="$ROOT/pipeline/gpu/render_gpu.swift"
GBIN="$ROOT/tools/render_gpu"
if [ "$CHECK_ONLY" = 0 ] && have swiftc && { [ ! -x "$GBIN" ] || [ "$GSRC" -nt "$GBIN" ]; }; then
    say "  swiftc -O $GSRC"
    # the log holds deprecation warnings (AVFoundation calls deprecated on
    # macOS 27, not removed); only an error stops the build
    mkdir -p "$ROOT/tools"
    swiftc -O "$GSRC" -o "$GBIN" 2>"$ROOT/tools/render_gpu.log" ||
        { grep -A3 "error:" "$ROOT/tools/render_gpu.log"; rm -f "$GBIN"; }
fi
if [ -x "$GBIN" ] && [ ! "$GSRC" -nt "$GBIN" ]; then
    ok+=("gpu render   $GBIN")
elif have swiftc; then
    bad+=("gpu render: $GBIN missing or older than $GSRC -- every render needs it. Re-run ./install.sh")
else
    bad+=("gpu render: no swiftc (Xcode command line tools: xcode-select --install) -- every render needs tools/render_gpu (macOS + Metal only)")
fi

# ---------------------------------------------------------------------------
# 4. the vision API (pipeline/crop.py; optional, authoring only)
# ---------------------------------------------------------------------------
# Nothing at RENDER time consults a model, so this being absent cannot change
# a pixel.
if [ -n "$(envvar QC_VISION_API_KEY)" ] && [ -n "$(envvar QC_VISION_MODEL)" ]; then
    ok+=("framing      $(envvar QC_VISION_MODEL) (crop.py)")
else
    ok+=("framing      QC_VISION_API_KEY/QC_VISION_MODEL unset (optional; hand-write crop:/x_offset:)")
fi

# ---------------------------------------------------------------------------
# 5. .env
# ---------------------------------------------------------------------------
if [ ! -f "$ROOT/.env" ] && [ -f "$ROOT/.env.example" ] && [ "$CHECK_ONLY" = 0 ]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    ok+=(".env         created from .env.example (all keys optional)")
elif [ -f "$ROOT/.env" ]; then
    ok+=(".env         present")
fi

# ---------------------------------------------------------------------------
# 6. summary
# ---------------------------------------------------------------------------
say ""
say "== summary"
printf '  %s\n' "${ok[@]}"
if [ "${#bad[@]}" -gt 0 ]; then
    say ""
    say "  MISSING:"
    printf '  ! %s\n' "${bad[@]}"
    exit 1
fi
say ""
say "All set. Next:"
say "  python3 pipeline/fetch.py <youtube-url-or-file>"
say "  python3 pipeline/transcribe.py sources/<id>        # span finder + ibtida' repair"
say "  tools/align-venv/bin/python  pipeline/align.py    sources/<id>/<reel>.yaml"
say "  tools/render-venv/bin/python pipeline/crop.py     sources/<id>/<reel>.yaml --write --annotate /tmp/<reel>-crop.png"
say "  tools/render-venv/bin/python pipeline/generate.py sources/<id>/<reel>.yaml"
