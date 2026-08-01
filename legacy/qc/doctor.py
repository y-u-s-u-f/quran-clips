"""
qc.doctor -- what this machine resolves to, and what is missing.

Every stage of the pipeline shells out to something, and before `qc doctor` a
misconfigured host announced itself as a traceback from inside a filtergraph
several minutes into a render. This prints the whole resolution table up front,
says which command each missing piece blocks, and exits non-zero when a stage is
unrunnable, so it works as a setup check and in CI.

It deliberately runs no ffmpeg and downloads nothing: it must stay usable on a
machine where the thing being diagnosed is broken.
"""
import os
import sys

from . import config, env

# Which tool gates which command, so a missing binary reports consequences
# rather than just absence.
GATES = {
    "ffmpeg": "render, frames, export, check --output",
    "ffprobe": "check --output, export, status",
    "yt-dlp": "source add",
    "claude": "propose --judge, author line-breaking (both degrade to a fallback)",
}
OPTIONAL = ("claude",)


def _mark(ok):
    return "ok  " if ok else "MISS"


def run(argv=None):
    argv = argv or []
    verbose = "-v" in argv or "--verbose" in argv

    print("host      %s" % env.platform_note())
    dot = config.env_path()
    print("env       %s%s" % (os.path.relpath(dot, env.ROOT),
                              "" if os.path.exists(dot) else "  (absent)"))
    cfg = config.path()
    print("config    %s%s" % (os.path.relpath(cfg, env.ROOT),
                              "" if os.path.exists(cfg) else "  (absent; all defaults)"))
    print()

    rows = env.describe()
    missing_required, missing_optional = [], []
    width = max(len(r[0]) for r in rows)
    for name, path, source in rows:
        ok = bool(path) and os.path.exists(path)
        # An interpreter row legitimately resolves to the running python when no
        # sibling venv exists; that is a working single-venv install, not a fault.
        print("%s %-*s %s" % (_mark(ok), width, name,
                              (path or "not found") + ("" if not verbose else "   [%s]" % source)))
        if not ok:
            base = name.split()[0]
            (missing_optional if base in OPTIONAL else missing_required).append(base)

    print()
    from .author import asr
    backend = asr.backend()
    ok, detail = asr.available(backend)
    print("%s asr backend %r (%s)" % (_mark(ok), backend, detail))
    if not ok:
        print("     default for this host is %r; override with $QC_ASR_BACKEND "
              "or [asr] backend" % asr.default_backend())

    # Egress. Only `source add` needs it, so a missing pool is a note unless the
    # operator pinned QC_PROXY_ENABLED=always.
    from . import proxy
    print()
    if proxy.enabled():
        rows = proxy.describe()
        total = sum(n for _, n, _ in rows)
        print("%s egress via proxy pool (%d endpoint(s), tried static -> "
              "datacenter -> fail)" % (_mark(total > 0), total))
        for tier, n, src in rows:
            print("     %-11s %d%s" % (tier, n, "  [%s]" % src if src else ""))
        if verbose:
            for label, _u in proxy.attempts():
                print("       %s" % label)
    elif proxy.configured():
        print("ok   egress direct (pool configured but QC_PROXY_ENABLED=never)")
    else:
        print("ok   egress direct (no proxy pool configured)")
        print("     On a cloud host YouTube bot-checks the datacentre IP; set "
              "QC_PROXY_STATIC in .env")

    blocked = []
    for tool in missing_required:
        if tool in GATES:
            blocked.append("%s: blocks %s" % (tool, GATES[tool]))
    if not ok:
        blocked.append("asr: blocks author (word timings)")

    if missing_optional:
        print()
        for tool in missing_optional:
            print("note  %s absent -- %s" % (tool, GATES.get(tool, "optional")))

    if blocked:
        print()
        print("BLOCKED")
        for b in blocked:
            print("  - " + b)
        print()
        print("Text-only commands (ayah, check) work regardless.")
        return 1

    print()
    print("all stages runnable")
    return 0
