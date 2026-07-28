"""`qc` -- command line front end. Run via ./bin/qc (which picks the venv).

    qc source add <url> [--no-video]   download + metadata + auto-captions
    qc locate <video_id> [-v]          which surah/ayat does this video recite?
    qc ayah <surah>:<ayah>[-<ayah>]    print the mushaf text (Uthmani + Sahih)
    qc author <video_id> <surah>:<a>[-<b>] <start> <end> [options]
                                       align + time a window into clip.yaml

`author` options:
    -o DIR        where to write clip.yaml + tags.yaml (default: a temp dir)
    --like NAME   copy reciter / video_bg / source_url from clips/NAME
    --style S     bars | default   (default: bars)
    -n            print the yaml, do not write anything
"""
import sys

USAGE = __doc__


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE.strip())
        return 0
    cmd = argv.pop(0)

    if cmd == "source":
        from .author import fetch
        if not argv or argv[0] != "add":
            print("usage: qc source add <url> [--no-video]", file=sys.stderr)
            return 2
        argv.pop(0)
        no_video = "--no-video" in argv
        rest = [a for a in argv if not a.startswith("-")]
        if not rest:
            print("usage: qc source add <url> [--no-video]", file=sys.stderr)
            return 2
        fetch.add(rest[0], skip_video=no_video)
        return 0

    if cmd == "locate":
        from .author import locate
        verbose = "-v" in argv or "--verbose" in argv
        rest = [a for a in argv if not a.startswith("-")]
        if not rest:
            print("usage: qc locate <video_id> [-v]", file=sys.stderr)
            return 2
        from .author import fetch
        locate.run(fetch.video_id(rest[0]), verbose=verbose)
        return 0

    if cmd == "ayah":
        from . import quran
        if not argv:
            print("usage: qc ayah <surah>:<ayah>[-<ayah>]", file=sys.stderr)
            return 2
        spec = argv[0]
        s, _, rng = spec.partition(":")
        a, _, b = rng.partition("-")
        b = b or a
        for v in quran.range(int(s), int(a), int(b)):
            print("%s %d:%d" % (quran.surah_name(v["surah"]), v["surah"], v["ayah"]))
            print(v["ar"])
            print(v["en"])
            print()
        return 0

    if cmd == "author":
        return _author(argv)

    print("unknown command %r\n\n%s" % (cmd, USAGE.strip()), file=sys.stderr)
    return 2


def _opt(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        v = argv[i + 1]
        del argv[i:i + 2]
        return v
    return default


def _timecode(s):
    """'676.33' or '00:11:16.33' -> seconds."""
    parts = str(s).split(":")
    t = 0.0
    for p in parts:
        t = t * 60 + float(p)
    return t


def _author(argv):
    import os
    from .author import emit, fetch

    out = _opt(argv, "-o")
    like = _opt(argv, "--like")
    style = _opt(argv, "--style", "bars")
    dry = "-n" in argv
    rest = [a for a in argv if not a.startswith("-")]
    if len(rest) < 4:
        print("usage: qc author <video_id> <surah>:<a>[-<b>] <start> <end> "
              "[-o DIR] [--like CLIP] [--style bars|default] [-n]",
              file=sys.stderr)
        return 2
    vid = fetch.video_id(rest[0])
    s, _, rng = rest[1].partition(":")
    a, _, b = rng.partition("-")
    b = b or a
    start, end = _timecode(rest[2]), _timecode(rest[3])

    src = os.path.join(fetch.SOURCES, "%s.mp4" % vid)
    if not os.path.exists(src):
        print("no %s -- run `qc source add` first" % src, file=sys.stderr)
        return 2

    meta = {"source_url": "https://www.youtube.com/watch?v=%s" % vid}
    if like:
        meta.update(_carry_over(like))
    # Scratch audio + cached ASR live under sources/, which is gitignored
    # and is already the durable cache for everything derived from a video.
    work = os.path.join(fetch.SOURCES, "_align", vid)
    p = emit.plan(src, int(s), int(a), int(b), start, end, work)

    for j in p["jumps"]:
        print("! restart detected at rel %.2f (reference pointer jumps back)"
              % (j["t"] - p["seg_start"]), file=sys.stderr)
    for n, c in enumerate(p["cards"][1:]):
        if c["in"] and not c["in"]["waqf"]:
            print("! P%d->P%d is NOT a true waqf: trough only %.0f dB below "
                  "speech, %.2fs of silence" % (n + 1, n + 2, c["in"]["depth"],
                                                c["in"]["sustain"]),
                  file=sys.stderr)

    text = emit.clip_yaml(p, meta, style=style)
    if dry or not out:
        print(text)
        return 0
    cp, tp = emit.write(out, p, meta, style=style)
    print("wrote %s\n       %s" % (cp, tp))
    return 0


def _carry_over(name):
    """Lift the non-derivable fields off a shipped clip: reciter, crop, text."""
    import os
    from .author import fetch
    path = name if name.endswith(".yaml") else \
        os.path.join(fetch.ROOT, "clips", name, "clip.yaml")
    try:
        import yaml
        d = yaml.safe_load(open(path, encoding="utf-8"))
    except Exception as e:
        print("could not read %s (%s)" % (path, e), file=sys.stderr)
        return {}
    out = {}
    for k in ("reciter", "video_bg", "text", "source_url"):
        if d.get(k):
            out[k] = d[k]
    return out


if __name__ == "__main__":
    sys.exit(main())
