"""`qc` -- command line front end. Run via ./bin/qc (which picks the venv).

    qc source add <url> [--no-video]   download + metadata + auto-captions
    qc locate <video_id> [-v]          which surah/ayat does this video recite?
    qc ayah <surah>:<ayah>[-<ayah>]    print the mushaf text (Uthmani + Sahih)
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

    print("unknown command %r\n\n%s" % (cmd, USAGE.strip()), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
