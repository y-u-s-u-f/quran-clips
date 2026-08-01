"""
qc.config -- machine-level settings: `.env` and the optional `qc.toml`.

Three tiers exist and all are kept distinct:

  * `templates/*.yaml`  -- how a style LOOKS. Per-style, committed, reviewed.
  * `clip.yaml`         -- what one clip SAYS. Per-clip, authored by `qc author`.
  * `.env` / `qc.toml`  -- where THIS machine keeps its tools, models and
                           proxies. Per-checkout, gitignored, optional.

Nothing that affects a rendered pixel belongs in the third tier; that is what
`templates/` is for, and a look-affecting number in an untracked file would make
a render impossible to reproduce from the repo alone.

`.env` is the primary surface because it is what an operator already has for
credentials, and proxy passwords must never land in a committed file. `qc.toml`
remains for anyone who prefers structured config; `.env` wins on a conflict
since it is the more specific, per-shell thing.

Key mapping between the two, so one concept never has two spellings:

    QC_FFMPEG=/usr/bin/ffmpeg        <->  [tools]        ffmpeg
    QC_ASR_MODEL=/models/whisper     <->  [asr]          model
    QC_ASR_BACKEND=faster            <->  [asr]          backend
    QC_RENDER_PYTHON=/v/bin/python   <->  [interpreters] render
    QC_PROXY_STATIC=user:pw@h:p,...  <->  [proxy]        static

Reading `.env` here (rather than requiring python-dotenv) keeps the dependency
list unchanged: the parser below handles the whole format the file actually uses
-- comments, blank lines, `export ` prefixes, and quoted values.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_cache = None
_env_cache = None
_warned = False


def env_path():
    """Where `.env` is read from."""
    return os.environ.get("QC_ENV_FILE") or os.path.join(ROOT, ".env")


def path():
    """Where `qc.toml` would be read from, whether or not it exists."""
    return os.environ.get("QC_CONFIG") or os.path.join(ROOT, "qc.toml")


def _parse_env(text):
    """`KEY=value` lines -> dict. Tolerates `export`, comments and quotes."""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        # Strip one matching quote pair; a bare `#` after an unquoted value is a
        # trailing comment, which is common in hand-edited env files.
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        if key:
            out[key] = val
    return out


def dotenv():
    """Parsed `.env` as a dict. Cached; `{}` when absent.

    Values are NOT pushed into `os.environ`: the process environment stays
    authoritative, so a variable exported in the shell keeps beating the file,
    and `qc doctor` can honestly report which of the two a value came from.
    """
    global _env_cache
    if _env_cache is not None:
        return _env_cache
    p = env_path()
    if not os.path.exists(p):
        _env_cache = {}
        return _env_cache
    try:
        with open(p, encoding="utf-8") as fh:
            _env_cache = _parse_env(fh.read())
    except OSError as e:
        raise SystemExit("could not read %s: %s" % (p, e))
    return _env_cache


def var(name, default=None):
    """One setting, process environment first, then `.env`."""
    if name in os.environ and os.environ[name] != "":
        return os.environ[name]
    val = dotenv().get(name)
    return default if val in (None, "") else val


def var_source(name):
    """Where `var(name)` came from: 'env', '.env', or None. For `qc doctor`."""
    if os.environ.get(name):
        return "env"
    if dotenv().get(name):
        return ".env"
    return None


def load():
    """Parsed `qc.toml` as a nested dict. Cached; `{}` when absent."""
    global _cache, _warned
    if _cache is not None:
        return _cache
    p = path()
    if not os.path.exists(p):
        _cache = {}
        return _cache
    try:
        import tomllib
    except ImportError:                                    # python < 3.11
        if not _warned:
            import sys
            print("note: %s ignored (needs python 3.11+ for tomllib)" % p,
                  file=sys.stderr)
            _warned = True
        _cache = {}
        return _cache
    try:
        with open(p, "rb") as fh:
            _cache = tomllib.load(fh)
    except Exception as e:                                 # malformed toml
        raise SystemExit("could not parse %s: %s" % (p, e))
    return _cache


def get(table, key, default=None):
    """One value out of `[table]` in `qc.toml`, or `default`."""
    return load().get(table, {}).get(key, default)


def reset():
    """Drop the caches. For tests that write a config and re-read it."""
    global _cache, _env_cache, _warned
    _cache = None
    _env_cache = None
    _warned = False
