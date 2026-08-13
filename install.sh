#!/usr/bin/env bash
# install.sh -- set up everything the pipeline needs on this machine.
#
#     ./install.sh            full setup (idempotent; re-run any time)
#     ./install.sh --check    report only, install nothing
#     ./install.sh --hermes   also register skills/ with the Hermes harness
#
# What it does, in order:
#   1. checks the system tools (python3, ffmpeg, ffprobe, yt-dlp, curl)
#   2. builds tools/render-venv  -- PyYAML + a Pillow whose RAQM does Arabic
#      shaping (falls back to --system-site-packages when the pip wheel's
#      Pillow lacks RAQM but the system python's has it)
#   3. builds tools/asr-venv    -- mlx-whisper on Apple silicon,
#      faster-whisper everywhere else (override with QC_ASR_BACKEND in .env)
#   3b. builds tools/align-venv -- ctc-forced-aligner (torch) for align.py
#   4. resolves the framing model for pipeline/crop.py (optional)
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
REGISTER_HERMES=0
for arg in "$@"; do
    case "$arg" in
        --check)  CHECK_ONLY=1;;
        --hermes) REGISTER_HERMES=1;;
        *) say "unknown flag: $arg (want --check or --hermes)"; exit 2;;
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
for tool in ffmpeg ffprobe yt-dlp curl; do
    if have "$tool"; then
        ok+=("$(printf '%-12s %s' "$tool" "$(command -v "$tool")")")
    else
        case "$tool" in
            ffmpeg|ffprobe) bad+=("$tool: not found -- rendering and probing need it. brew install ffmpeg / apt install ffmpeg");;
            yt-dlp)         bad+=("yt-dlp: not found -- fetch.py needs it for YouTube sources (local files still work). brew install yt-dlp / apt install yt-dlp");;
            curl)           bad+=("curl: not found -- the face-model download needs it");;
        esac
    fi
done
# the heat effect needs ffmpeg's perlin source (ffmpeg >= 7.1)
if have ffmpeg && ! ffmpeg -hide_banner -filters 2>/dev/null | grep -q ' perlin '; then
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
if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
    ASR_PKG="mlx-whisper";    ASR_MOD="mlx_whisper"
else
    ASR_PKG="faster-whisper"; ASR_MOD="faster_whisper"
fi
if [ "$CHECK_ONLY" = 0 ]; then
    if [ ! -x "$APY" ]; then
        say "  creating $AVENV"
        "$PY" -m venv "$AVENV" || exit 1
        "$APY" -m pip -q install --upgrade pip
    fi
    "$APY" -c "import $ASR_MOD" 2>/dev/null || {
        say "  installing $ASR_PKG (first transcription will also download the model)"
        "$APY" -m pip -q install "$ASR_PKG"
    }
fi
if [ -x "$APY" ] && "$APY" -c "import $ASR_MOD" 2>/dev/null; then
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
if [ "$CHECK_ONLY" = 0 ]; then
    if [ ! -x "$FPY" ]; then
        say "  creating $FVENV"
        "$PY" -m venv "$FVENV" || exit 1
        "$FPY" -m pip -q install --upgrade pip
    fi
    "$FPY" -c "import ctc_forced_aligner, yaml" 2>/dev/null || {
        say "  installing ctc-forced-aligner + torch (first align will also download the ~1.2GB MMS model)"
        "$FPY" -m pip -q install "$FPKG" pyyaml
    }
fi
if [ -x "$FPY" ] && "$FPY" -c "import ctc_forced_aligner, yaml" 2>/dev/null; then
    ok+=("align venv   $FPY (ctc-forced-aligner)")
else
    bad+=("align venv: cannot import ctc_forced_aligner -- align.py cannot run (generate.py still works off whisper.json). $FPY -m pip install $FPKG pyyaml")
fi

# ---------------------------------------------------------------------------
# 4. framing model (pipeline/crop.py; optional, authoring only)
# ---------------------------------------------------------------------------
# No download: crop.py rides the user's own `claude` CLI auth. Nothing at
# RENDER time consults a model, so this being absent cannot change a pixel.
if have "${QC_CLAUDE:-claude}"; then
    ok+=("framing      $(command -v "${QC_CLAUDE:-claude}") (crop.py)")
else
    ok+=("framing      claude CLI absent (optional; hand-write crop:/x_offset:)")
fi

# ---------------------------------------------------------------------------
# 4b. Hermes harness (optional). Claude Code finds the skill by itself through
# .claude/skills/make-post; Hermes needs the repo's skills/ dir named in its
# config. Same SKILL.md either way -- the symlink and this entry point at one
# file, which is why the two harnesses cannot drift.
# ---------------------------------------------------------------------------
HCFG="${HERMES_HOME:-$HOME/.hermes}/config.yaml"
if have hermes; then
    if [ -f "$HCFG" ] && grep -qF "$ROOT/skills" "$HCFG"; then
        ok+=("hermes       skills/ registered in $HCFG")
    elif [ "$REGISTER_HERMES" = 1 ] && [ "$CHECK_ONLY" = 0 ]; then
        # Textual edit, not a YAML round-trip: config.yaml is the user's own
        # commented file and safe_dump would strip every comment in it.
        "$PY" - "$HCFG" "$ROOT/skills" <<'PYEOF'
import pathlib, sys
cfg, entry = pathlib.Path(sys.argv[1]), sys.argv[2]
cfg.parent.mkdir(parents=True, exist_ok=True)
text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
item = "    - %s\n" % entry
if entry in text:
    pass
elif "  external_dirs: []" in text:
    text = text.replace("  external_dirs: []\n", "  external_dirs:\n" + item, 1)
elif "  external_dirs:\n" in text:
    text = text.replace("  external_dirs:\n", "  external_dirs:\n" + item, 1)
elif "\nskills:\n" in text:
    text = text.replace("\nskills:\n", "\nskills:\n  external_dirs:\n" + item, 1)
else:
    if text and not text.endswith("\n"):
        text += "\n"
    text += "skills:\n  external_dirs:\n" + item
cfg.write_text(text, encoding="utf-8")
PYEOF
        ok+=("hermes       skills/ registered in $HCFG (./install.sh --hermes)")
    else
        ok+=("hermes       present; run ./install.sh --hermes to enable /make-post")
    fi
else
    ok+=("hermes       not installed (optional; Claude Code drives the pipeline alone)")
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
say "  python3 pipeline/transcribe.py sources/<id>        # only to find the span"
say "  tools/align-venv/bin/python pipeline/align.py sources/<id>/<reel>.yaml"
say "  tools/render-venv/bin/python pipeline/generate.py sources/<id>/<reel>.yaml"
