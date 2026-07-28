"""
qc.proc -- where the external binaries live, and the one echoing runner.

Every subprocess in the pipeline is an ffmpeg/ffprobe invocation; `run` echoes
the command line first so a failed render can be reproduced by hand from the
log.
"""
import shlex
import subprocess

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"


def run(cmd, **kw):
    print("+", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(cmd, **kw)
