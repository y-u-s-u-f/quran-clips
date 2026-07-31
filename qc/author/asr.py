"""
qc.author.asr -- the speech-to-text backend, chosen per machine.

`align` needs one thing from ASR: word-level timings over a 16 kHz mono WAV, as

    {"words":    [{"w": str, "t0": float, "t1": float, "p": float}, ...],
     "segments": [{"t0": float, "t1": float, "text": str}, ...]}

That contract is all the DP consumes, so the engine behind it is swappable.
Originally it was `mlx-whisper` inlined into `align`, which is Apple-silicon
only (MLX is a Metal framework), making the authoring half of the pipeline
unrunnable on Linux or Intel even though every other stage is portable.

Two backends ship, both producing the dict above:

  * `mlx`    -- mlx-whisper. Fastest on Apple silicon; the reference backend,
                and what the committed timings were produced with.
  * `faster` -- faster-whisper (CTranslate2). CPU or CUDA, runs anywhere.

Selection: `$QC_ASR_BACKEND`, else `[asr] backend` in `qc.toml`, else `mlx` on
Apple silicon and `faster` elsewhere. `qc doctor` prints which one would run.

Both are invoked as a SUBPROCESS against a separate interpreter, which is not
ceremony: whisper's dependency tree must never be installed into the render
interpreter, whose Pillow needs a RAQM build that a careless resolve replaces
(the incident is recorded in the repo's skill). The subprocess boundary is what
keeps those two dependency sets from ever meeting.

A backend change re-times a clip, so it is deliberately NOT silent: the chosen
backend is recorded in the ASR cache and `align` warns when a cache written by
a different backend is reused.
"""
import json
import os
import platform
import subprocess
import sys

from .. import config, env

# Per-backend default model. Both are large-v3-turbo, so the acoustic model is
# the same weights; only the runtime differs.
MODELS = {
    "mlx": "mlx-community/whisper-large-v3-turbo",
    "faster": "deepdml/faster-whisper-large-v3-turbo-ct2",
}

# Each snippet prints nothing and writes the contract dict to argv[2]. Keeping
# them as source strings (rather than importable modules) is what lets them run
# under an interpreter that cannot import `qc` at all.
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
    # faster-whisper returns a generator; it must be drained before `info` is
    # meaningful, and word timings only exist with word_timestamps=True.
    # beam_size 1 + temperature 0 keeps it as close to the mlx path's greedy
    # decode as the two runtimes allow.
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

_PIP = {"mlx": "mlx-whisper", "faster": "faster-whisper"}


def default_backend():
    """`mlx` on Apple silicon, `faster` anywhere else."""
    return "mlx" if (platform.system() == "Darwin"
                     and platform.machine() == "arm64") else "faster"


def backend():
    """The backend that will run: env var, then config, then the platform."""
    choice = (config.var("QC_ASR_BACKEND")
              or config.get("asr", "backend")
              or default_backend())
    choice = str(choice).lower()
    if choice not in _SNIPPETS:
        raise SystemExit(
            "unknown ASR backend %r (known: %s).\n"
            "  Set $QC_ASR_BACKEND or [asr] backend in qc.toml."
            % (choice, ", ".join(sorted(_SNIPPETS))))
    return choice


def model_for(name=None):
    """The model id (or local directory) for a backend.

    `QC_ASR_MODEL` / `[asr] model` may be a hub id or an absolute path to a
    downloaded model, which is what an offline or air-gapped machine needs --
    both backends accept either.
    """
    name = name or backend()
    override = config.var("QC_ASR_MODEL") or config.get("asr", "model")
    if override:
        return os.path.expanduser(str(override))
    return MODELS[name]


def interpreter():
    """The interpreter that runs the ASR snippet.

    `tools/asr-venv/bin/python` by default, overridable with `$QC_ASR_PYTHON`.
    A single-venv install can point it at `sys.executable`.
    """
    return env.interpreter("asr")


def available(name=None):
    """(ok, detail) -- can this backend actually run right now?"""
    name = name or backend()
    py = interpreter()
    if not os.path.exists(py):
        return False, "no interpreter at %s" % py
    mod = {"mlx": "mlx_whisper", "faster": "faster_whisper"}[name]
    p = subprocess.run([py, "-c", "import %s" % mod],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return False, "%s cannot import %s" % (py, mod)
    return True, "%s via %s" % (name, py)


def transcribe(wav, out, name=None, model=None):
    """Run the chosen backend over `wav`, writing the contract dict to `out`.

    Raises SystemExit naming the exact install command on a missing backend --
    the previous message hardcoded a Homebrew python path that does not exist
    off macOS.
    """
    name = name or backend()
    model = model or model_for(name)
    py = interpreter()
    if not os.path.exists(py):
        raise SystemExit(
            "no ASR interpreter at %s.\n"
            "  Create it:  python3 -m venv tools/asr-venv\n"
            "              tools/asr-venv/bin/pip install %s\n"
            "  or point $QC_ASR_PYTHON at an interpreter that has %s.\n"
            "  Never install whisper into the render interpreter: it can "
            "replace the RAQM Pillow build the renderer needs."
            % (py, _PIP[name], _PIP[name]))
    p = subprocess.run([py, "-c", _SNIPPETS[name], wav, out, model],
                       stdout=sys.stderr, stderr=sys.stderr)
    if p.returncode != 0:
        ok, detail = available(name)
        hint = "" if ok else "\n  %s -- pip install %s into it." % (detail, _PIP[name])
        raise SystemExit("ASR backend %r failed on %s%s" % (name, wav, hint))
    return json.load(open(out, encoding="utf-8"))
