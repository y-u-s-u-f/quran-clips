"""
qc.proc -- where the external binaries live, and the one echoing runner.

Every subprocess in the pipeline is an ffmpeg/ffprobe invocation; `run` echoes
the command line first so a failed render can be reproduced by hand from the
log.

`FFMPEG` / `FFPROBE` were `/opt/homebrew/...` literals, which pinned the repo to
Apple silicon + Homebrew: on any other host the first call died with
`FileNotFoundError: /opt/homebrew/bin/ffmpeg`, taking the render path,
`check --output` and the whole golden suite with it. They now resolve through
`qc.env` (env var -> `qc.toml` -> PATH -> platform hints) and remain
module-level names, so every existing `from qc.proc import FFMPEG` is unchanged.

Resolution happens at import, which is the right time: only the media modules
import this (`qc.check` and `qc.quran` deliberately do not), so importing it at
all means an ffmpeg call is coming. Failing here names the missing tool and the
knobs, instead of surfacing as a FileNotFoundError from inside a filtergraph.
"""
import shlex
import subprocess

from . import env

FFMPEG = env.require("ffmpeg", "render video and cut audio")
FFPROBE = env.require("ffprobe", "read stream metadata")


def run(cmd, **kw):
    print("+", " ".join(shlex.quote(str(c)) for c in cmd))
    return subprocess.run([str(c) for c in cmd], **kw)
