"""
qc.env -- where the external tools live, resolved once.

The pipeline shells out to ffmpeg, ffprobe, yt-dlp, a `claude` CLI and two
sibling interpreters. Those used to be absolute `/opt/homebrew/...` literals
scattered across a dozen modules, which pinned the repo to one Homebrew layout
on Apple silicon: on any other machine the first ffmpeg call died with
`FileNotFoundError: /opt/homebrew/bin/ffmpeg`, and that took the render path,
`check --output` and the whole golden suite with it.

Resolution order for every tool, first hit wins:

  1. `$QC_<TOOL>` environment variable (e.g. `QC_FFMPEG`, `QC_YT_DLP`)
  2. `[tools]` in the config file (see `qc.config`)
  3. `shutil.which(<name>)` -- the normal PATH lookup
  4. a platform hint list (Homebrew on both arches, MacPorts, /usr/bin, ...)

`require()` raises with the resolution order spelled out, so a missing binary
says what to install and which knob to set rather than leaking an absolute path
nobody configured.

Nothing here imports a heavy dependency: `qc.check` and `qc ayah` must keep
working on a bare interpreter with no ffmpeg installed at all.
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Hints are only consulted when PATH has nothing. Keeping Homebrew's two prefixes
# first preserves the original behaviour on the author's machine bit for bit.
_HINTS = {
    "ffmpeg": ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
               "/opt/local/bin/ffmpeg", "/usr/bin/ffmpeg"),
    "ffprobe": ("/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe",
                "/opt/local/bin/ffprobe", "/usr/bin/ffprobe"),
    "yt-dlp": ("/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp",
               "/usr/bin/yt-dlp"),
    "claude": ("/opt/homebrew/bin/claude", "/usr/local/bin/claude",
               os.path.expanduser("~/.claude/local/claude")),
}

# `QC_YT_DLP` is friendlier than `QC_YT-DLP`, which no shell will export.
_ENV_ALIASES = {"yt-dlp": "QC_YT_DLP"}


def env_var(tool):
    """The environment variable that overrides `tool`."""
    return _ENV_ALIASES.get(tool, "QC_" + tool.upper().replace("-", "_"))


def find(tool):
    """Absolute path to `tool`, or None. Never raises -- see `require`."""
    from . import config
    override = config.var(env_var(tool))
    if override:
        # An explicit override is honoured verbatim: if the operator points at a
        # path that does not exist we must not silently fall through to a
        # different binary, or a pinned ffmpeg build turns into a wrong one and
        # the golden md5s drift for no visible reason.
        return override

    configured = config.get("tools", tool)
    if configured:
        return os.path.expanduser(str(configured))

    hit = shutil.which(tool)
    if hit:
        return hit

    for cand in _HINTS.get(tool, ()):
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def require(tool, why=""):
    """Absolute path to `tool` or exit with the resolution order."""
    hit = find(tool)
    if hit and (os.path.isabs(hit) and not os.path.exists(hit)):
        raise SystemExit(
            "%s is set to %r, which does not exist.\n"
            "Fix that path or unset %s to fall back to PATH."
            % (env_var(tool), hit, env_var(tool)))
    if not hit:
        raise SystemExit(
            "%s not found%s.\n"
            "  Looked at: $%s (environment or .env), the [tools] table in %s, "
            "PATH, then %s\n"
            "  Install it (macOS: brew install %s / Debian: apt install %s), "
            "or point $%s at it."
            % (tool, (" -- needed to %s" % why) if why else "",
               env_var(tool), config_path_for_message(), _HINTS.get(tool, ("(no hints)",))[0],
               tool, tool, env_var(tool)))
    return hit


def config_path_for_message():
    from . import config
    return os.path.relpath(config.path(), ROOT)


def interpreter(name, hint_cmd=None):
    """Resolve one of the sibling venv interpreters (`render`, `author`, `asr`).

    Layout stays `tools/<name>-venv/bin/python` by default so an existing
    checkout keeps working, but each is overridable (`QC_RENDER_PYTHON`,
    `QC_AUTHOR_PYTHON`, `QC_ASR_PYTHON`) for a system that puts its venvs
    elsewhere, or for a single-venv install where all three are the same
    interpreter.
    """
    from . import config
    override = config.var("QC_%s_PYTHON" % name.upper())
    if override:
        return os.path.expanduser(override)
    configured = config.get("interpreters", name)
    if configured:
        return os.path.expanduser(str(configured))
    default = os.path.join(ROOT, "tools", "%s-venv" % name, "bin", "python")
    if os.path.exists(default):
        return default
    # No sibling venv: fall back to the interpreter already running. A
    # single-venv install (one env with Pillow+PyYAML, the common case off
    # macOS) is then usable with no configuration, and callers that genuinely
    # need an isolated env still check importability and say so. Returning a
    # path that does not exist would instead surface as a bare
    # FileNotFoundError from subprocess, several frames from the cause.
    return sys.executable


def require_interpreter(name, packages="", extra=""):
    """Resolve a venv interpreter or exit with how to create it."""
    py = interpreter(name)
    if os.path.exists(py):
        return py
    # Falling back to the current interpreter is right for a single-venv
    # install: if the caller's own python already imports what is needed, the
    # separate venv is ceremony. The caller checks importability itself.
    raise SystemExit(
        "no %s interpreter at %s.\n"
        "  Create it:  python3 -m venv tools/%s-venv%s\n"
        "%s"
        "  or point $QC_%s_PYTHON at an interpreter that has %s."
        % (name, py, name,
           " --system-site-packages" if name == "render" else "",
           ("              tools/%s-venv/bin/pip install %s\n" % (name, packages))
           if packages else "",
           name.upper(), packages or "the dependencies"))


def describe():
    """Every resolved tool -> (path_or_None, source). Powers `qc doctor`."""
    from . import config
    rows = []
    for tool in ("ffmpeg", "ffprobe", "yt-dlp", "claude"):
        var = env_var(tool)
        origin = config.var_source(var)
        if origin:
            src = "$%s (%s)" % (var, origin)
        elif config.get("tools", tool):
            src = "config [tools]"
        elif shutil.which(tool):
            src = "PATH"
        elif find(tool):
            src = "platform hint"
        else:
            src = "not found"
        rows.append((tool, find(tool), src))
    for name in ("render", "author", "asr"):
        var = "QC_%s_PYTHON" % name.upper()
        origin = config.var_source(var)
        if origin:
            src = "$%s (%s)" % (var, origin)
        elif config.get("interpreters", name):
            src = "config [interpreters]"
        else:
            src = "default layout"
        py = interpreter(name)
        rows.append(("%s python" % name, py if os.path.exists(py) else None, src))
    return rows


def platform_note():
    """One line about the host, for `qc doctor` and error messages."""
    import platform
    return "%s %s (%s), python %s" % (
        platform.system(), platform.release(), platform.machine(),
        ".".join(str(v) for v in sys.version_info[:3]))
