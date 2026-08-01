"""pipeline/transcribe.py -- word-level Whisper over a source folder.

    python3 pipeline/transcribe.py sources/<id>      (or just the bare <id>)
    python3 pipeline/transcribe.py sources/<id> --force

Reads sources/<id>/source.mp4 and writes, next to it:

    whisper.json    {"words": [{"w","t0","t1","p"}...], "segments": [...],
                     "backend": ..., "model": ...}
    whisper.srt     the segments as a human-readable SRT

Skips work when whisper.json already exists (--force re-runs). The backend is
chosen per machine, from .env:

    QC_ASR_BACKEND   mlx (Apple silicon default) | faster (everywhere else)
    QC_ASR_MODEL     hub id or an absolute path to downloaded weights
    QC_ASR_PYTHON    interpreter that can import the backend
                     (default: tools/asr-venv/bin/python, else this python)

Both backends load the same large-v3-turbo weights; only the runtime differs.
They do NOT emit identical word boundaries, so switching backends re-times
everything cut from this transcript -- whisper.json records which backend
wrote it for exactly that reason.

The backend runs as a SUBPROCESS against its own interpreter: whisper's
dependency tree must never land in the interpreter whose Pillow carries the
RAQM build that shapes Arabic (generate.py's), and the process boundary is
what keeps those dependency sets apart.
"""
import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = os.path.join(ROOT, "sources")

# Per-backend default model -- the same acoustic weights, two runtimes.
MODELS = {
    "mlx": "mlx-community/whisper-large-v3-turbo",
    "faster": "deepdml/faster-whisper-large-v3-turbo-ct2",
}
MODULES = {"mlx": "mlx_whisper", "faster": "faster_whisper"}
_PIP = {"mlx": "mlx-whisper", "faster": "faster-whisper"}

# Each snippet prints nothing and writes the contract dict to argv[2]. Kept as
# source strings (not importable modules) so they run under an interpreter
# that cannot import this package at all.
_SNIPPETS = {
    "mlx": r"""
import json, sys
import mlx_whisper
wav, out, model = sys.argv[1], sys.argv[2], sys.argv[3]
r = mlx_whisper.transcribe(
    wav, path_or_hf_repo=model, language="ar", word_timestamps=True,
    condition_on_previous_text=False, temperature=0.0, verbose=None)
words = []
for seg in r.get("segments", []):
    for w in seg.get("words", []):
        words.append({"w": w["word"].strip(),
                      "t0": float(w["start"]), "t1": float(w["end"]),
                      "p": float(w.get("probability", 0.0))})
json.dump({"words": words, "backend": "mlx", "model": model,
           "segments": [{"t0": s["start"], "t1": s["end"], "text": s["text"]}
                        for s in r.get("segments", [])]},
          open(out, "w"), ensure_ascii=False, indent=1)
""",
    # faster-whisper returns a generator; word timings only exist with
    # word_timestamps=True. beam_size 1 + temperature 0 keeps it as close to
    # the mlx path's greedy decode as the two runtimes allow.
    "faster": r"""
import json, sys
from faster_whisper import WhisperModel
wav, out, model = sys.argv[1], sys.argv[2], sys.argv[3]
m = WhisperModel(model, device="auto", compute_type="default")
segs, _ = m.transcribe(
    wav, language="ar", word_timestamps=True, beam_size=1,
    condition_on_previous_text=False, temperature=0.0)
words, segments = [], []
for s in segs:
    segments.append({"t0": float(s.start), "t1": float(s.end), "text": s.text})
    for w in (s.words or []):
        words.append({"w": w.word.strip(), "t0": float(w.start),
                      "t1": float(w.end), "p": float(getattr(w, "probability", 0.0))})
json.dump({"words": words, "backend": "faster", "model": model,
           "segments": segments}, open(out, "w"), ensure_ascii=False, indent=1)
""",
}


# --- machine config (.env) -------------------------------------------------

def _dotenv():
    path = os.path.join(ROOT, ".env")
    out = {}
    if os.path.exists(path):
        for raw in open(path, encoding="utf-8"):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            k, sep, v = line.partition("=")
            if sep:
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                elif " #" in v:
                    v = v.split(" #", 1)[0].rstrip()
                out[k.strip()] = v
    return out


_ENV = None


def envvar(name, default=None):
    global _ENV
    if os.environ.get(name):
        return os.environ[name]
    if _ENV is None:
        _ENV = _dotenv()
    return _ENV.get(name) or default


# --- backend selection -----------------------------------------------------

def backend():
    choice = (envvar("QC_ASR_BACKEND")
              or ("mlx" if (platform.system() == "Darwin"
                            and platform.machine() == "arm64") else "faster"))
    choice = choice.lower()
    if choice not in _SNIPPETS:
        raise SystemExit("unknown ASR backend %r (known: %s)"
                         % (choice, ", ".join(sorted(_SNIPPETS))))
    return choice


def model_for(name):
    override = envvar("QC_ASR_MODEL")
    return os.path.expanduser(override) if override else MODELS[name]


def _can_import(py, module):
    return subprocess.run([py, "-c", "import " + module],
                          capture_output=True).returncode == 0


def interpreter(name):
    """The interpreter that will run the ASR snippet: QC_ASR_PYTHON, else
    tools/asr-venv, else this python -- whichever can import the backend.
    An environment that already works is never rebuilt."""
    module = MODULES[name]
    candidates = [envvar("QC_ASR_PYTHON"),
                  os.path.join(ROOT, "tools", "asr-venv", "bin", "python"),
                  sys.executable]
    for py in candidates:
        if py and os.path.exists(py) and _can_import(py, module):
            return py
    raise SystemExit(
        "no interpreter can import %s.\n"
        "  python3 -m venv tools/asr-venv && "
        "tools/asr-venv/bin/pip install %s" % (module, _PIP[name]))


# --- transcription ---------------------------------------------------------

def extract_wav(src, wav):
    ffmpeg = envvar("QC_FFMPEG", "ffmpeg")
    rc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                         "-i", src, "-ar", "16000", "-ac", "1", "-vn",
                         wav]).returncode
    if rc != 0:
        raise SystemExit("could not extract 16k mono audio from %s" % src)


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def write_srt(data, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(data.get("segments", []), start=1):
            f.write("%d\n%s --> %s\n%s\n\n"
                    % (i, srt_time(seg["t0"]), srt_time(seg["t1"]),
                       seg["text"].strip()))


def find_source(folder):
    for name in ("source.mp4", "source.mkv", "source.webm", "source.mov",
                 "source.m4a", "source.mp3", "source.wav"):
        p = os.path.join(folder, name)
        if os.path.exists(p):
            return p
    raise SystemExit("no source.* media file in %s -- run fetch.py first, or "
                     "place one there" % folder)


def transcribe(folder, force=False):
    folder = folder if os.path.isdir(folder) else os.path.join(SOURCES, folder)
    if not os.path.isdir(folder):
        raise SystemExit("no such source folder: %s" % folder)
    out_json = os.path.join(folder, "whisper.json")
    out_srt = os.path.join(folder, "whisper.srt")
    if os.path.exists(out_json) and not force:
        data = json.load(open(out_json, encoding="utf-8"))
        print("whisper.json exists (%d words, backend %s) -- use --force to "
              "re-run" % (len(data.get("words", [])), data.get("backend")))
        return out_json

    src = find_source(folder)
    name = backend()
    model = model_for(name)
    py = interpreter(name)
    print("transcribing %s" % os.path.relpath(src, ROOT))
    print("  backend : %s (%s)" % (name, py))
    print("  model   : %s" % model)

    with tempfile.TemporaryDirectory(prefix="quran-transcribe-") as tmp:
        wav = os.path.join(tmp, "audio.wav")
        extract_wav(src, wav)
        p = subprocess.run([py, "-c", _SNIPPETS[name], wav, out_json, model],
                           stdout=sys.stderr, stderr=sys.stderr)
        if p.returncode != 0:
            raise SystemExit("ASR backend %r failed on %s" % (name, src))

    data = json.load(open(out_json, encoding="utf-8"))
    write_srt(data, out_srt)
    print("  %d words, %d segments -> %s + %s"
          % (len(data["words"]), len(data["segments"]),
             os.path.relpath(out_json, ROOT), os.path.relpath(out_srt, ROOT)))
    return out_json


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="word-level Whisper transcript for a source folder")
    ap.add_argument("folder", help="sources/<id> (or the bare <id>)")
    ap.add_argument("--force", action="store_true",
                    help="re-transcribe even if whisper.json exists")
    a = ap.parse_args(argv)
    transcribe(a.folder, force=a.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
