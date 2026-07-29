"""`qc` -- command line front end. Run via ./bin/qc (which picks the venv).

    qc source add <url> [--no-video]   download + metadata + auto-captions
    qc locate <video_id> [-v]          which surah/ayat does this video recite?
    qc ayah <surah>:<ayah>[-<ayah>]    print the mushaf text (Uthmani + Sahih)
    qc crop <video_id> [options]       solve the 16:9 framing once per source
    qc propose <video_id> [options]    ranked clip-worthy candidate windows
    qc author <video_id> <surah>:<a>[-<b>] <start> <end> [options]
                                       align + time a window into clip.yaml
    qc check <clip> [<clip>...] | all  validate clip.yaml before rendering
    qc check --output <clip>...        validate a rendered output/final.mp4
    qc render <clip> [--preview]       render (preview = half size, <=30s)
    qc frames <clip> --at 0,6,12,18    full-fidelity stills at these seconds
    qc export <clip> [<clip>...] | all export a finished clip to reels/

`check` runs no ffmpeg and takes under a second: clip.yaml schema (an unknown
key is an ERROR -- a typo'd one is silently ignored by the renderer), phrase
and cut ordering, that every codepoint has a glyph, that the Arabic is the
mushaf text, the bars caption geometry, and that no caption changeover is a
hard cut. `--output` needs a rendered file and takes a few seconds.

`render --preview` writes output/preview.mp4 and never final.mp4: half canvas,
no heat/snow/barglow/textglow/scan. Use it for timing and layout, never for
the look. `qc export` refuses a preview.

`propose` options:
    --style S     bars | default   (default: bars) -- changes the ranking
    -n N          how many candidates to print (default: 5)
    --range MM:SS-MM:SS   skip proposal, score this window instead
    --verses S:A[-B]      skip proposal, score these ayat instead
    --json        machine-readable only, to stdout

`crop` options (needs tools/author-venv -- see requirements/author.txt):
    --style S     bars | default   (default: bars)
    --frames N    how many frames to sample (default: 40)
    --side L      force the caption side: left | right
    --face X,Y[,W] hand-entered face centre (+ optional width) in source px
    --exclude X,Y,W,H   an extra no-go box (repeatable) -- for an INTERMITTENT
                  banner, which a temporal-variance test cannot see
    --annotate P  write an annotated frame to P
    --sheet P     write a contact sheet to P
    --write       cache the result in sources/meta/<id>.yaml

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

    if cmd == "propose":
        return _propose(argv)

    if cmd == "crop":
        from .author import crop, fetch
        style = _opt(argv, "--style", "bars")
        frames = int(_opt(argv, "--frames", "40"))
        side = _opt(argv, "--side")
        face = _opt(argv, "--face")
        ann = _opt(argv, "--annotate")
        sheet = _opt(argv, "--sheet")
        write = "--write" in argv
        ex = []
        while "--exclude" in argv:
            ex.append([int(v) for v in _opt(argv, "--exclude").split(",")])
        rest = [a for a in argv if not a.startswith("-")]
        if not rest:
            print("usage: qc crop <video_id> [--style bars|default] [--frames N] "
                  "[--side left|right] [--face X,Y[,W]] [--exclude X,Y,W,H] "
                  "[--annotate P] [--sheet P] [--write]", file=sys.stderr)
            return 2
        if style not in ("bars", "default"):
            print("unknown style %r (the framing cache is keyed by style: "
                  "bars | default)" % style, file=sys.stderr)
            return 2
        xy = tuple(float(v) for v in face.split(",")) if face else None
        crop.run(fetch.video_id(rest[0]), style=style, frames=frames, side=side,
                 face_xy=xy, extra_boxes=ex, annotate_path=ann, sheet_path=sheet,
                 write=write)
        return 0

    if cmd == "author":
        return _author(argv)

    if cmd == "check":
        from . import check
        out = "--output" in argv
        dirs = _clip_dirs([a for a in argv if not a.startswith("-")])
        if not dirs:
            print("usage: qc check [--output] <clip> [<clip>...] | all",
                  file=sys.stderr)
            return 2
        return check.run_output(dirs) if out else check.run(dirs)

    if cmd == "render":
        from . import render
        prev = "--preview" in argv
        rest = _clip_dirs([a for a in argv if not a.startswith("-")])
        if len(rest) != 1:
            print("usage: qc render <clip> [--preview]", file=sys.stderr)
            return 2
        if prev:
            render.preview(rest[0])
        else:
            import os
            sys.path.insert(0, os.path.join(_root(), "scripts"))
            import build_render
            build_render.build(rest[0])
        return 0

    if cmd == "frames":
        from . import render
        at = _opt(argv, "--at", "0")
        out = _opt(argv, "-o")
        rest = _clip_dirs([a for a in argv if not a.startswith("-")])
        if len(rest) != 1:
            print("usage: qc frames <clip> --at 0,6,12,18 [-o DIR]",
                  file=sys.stderr)
            return 2
        render.frames(rest[0], [float(x) for x in at.split(",") if x != ""],
                      out_dir=out)
        return 0

    if cmd == "export":
        import os
        sys.path.insert(0, os.path.join(_root(), "scripts"))
        import export_reel
        names = [os.path.basename(d.rstrip("/"))
                 for d in _clip_dirs([a for a in argv
                                      if not a.startswith("-")])]
        if not names:
            print("usage: qc export <clip> [<clip>...] | all", file=sys.stderr)
            return 2
        # export_reel refuses a preview render; see its is_preview()
        return 0 if all([export_reel.export(n) for n in names]) else 1

    print("unknown command %r\n\n%s" % (cmd, USAGE.strip()), file=sys.stderr)
    return 2


def _root():
    import os
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _clip_dirs(names):
    """Clip arguments -> directories. A bare name means clips/<name>; `all`
    means every clip folder that is not scratch (leading underscore)."""
    import os
    clips = os.path.join(_root(), "clips")
    out = []
    for n in names:
        if n == "all":
            out += [os.path.join(clips, d) for d in sorted(os.listdir(clips))
                    if not d.startswith((".", "_"))
                    and os.path.isfile(os.path.join(clips, d, "clip.yaml"))]
        elif os.path.isdir(n):
            out.append(os.path.abspath(n))
        else:
            out.append(os.path.join(clips, n.rstrip("/").split("/")[-1]))
    return out


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


def _propose(argv):
    from .author import fetch, propose

    style = _opt(argv, "--style", "bars")
    n = int(_opt(argv, "-n", "5"))
    rng = _opt(argv, "--range")
    verses = _opt(argv, "--verses")
    as_json = "--json" in argv
    rest = [a for a in argv if not a.startswith("-")]
    if not rest:
        print("usage: qc propose <video_id> [--style bars|default] [-n 5] "
              "[--range MM:SS-MM:SS] [--verses S:A-B] [--json]",
              file=sys.stderr)
        return 2
    if style not in ("bars", "default"):
        print("unknown style %r" % style, file=sys.stderr)
        return 2
    propose.run(fetch.video_id(rest[0]), style=style, n=n,
                rng=rng, verses=verses, as_json=as_json)
    return 0


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
    if style not in ("bars", "default"):
        print("unknown style %r (known: bars, default)" % style, file=sys.stderr)
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
    # The framing is a property of the SOURCE *and of the style* -- bars and
    # default place the reciter and the caption by different rules -- so only
    # the block solved for THIS style may be inherited. If there is none, say
    # so; silently using the other one is what mis-framed al-qamar-7-9.
    from .author import crop as _crop
    fr = _crop.read_framing(vid, style)
    if fr.get("crop"):
        meta["video_bg"] = {"mode": "live", "crop": fr["crop"]}
        meta["text"] = {"center_x_frac": fr.get("text_center_x_frac", 0.30)}
        meta["framing_from"] = "solved for the %s style by `qc crop` and " \
                               "cached in sources/meta/%s.yaml" % (style, vid)
    elif not like:
        have = _crop.solved_styles(vid)
        print("! no %s-style framing cached for %s%s.\n"
              "  Solve it first:  qc crop %s --style %s --write\n"
              "  (a crop solved for another style frames the reciter for that "
              "style's caption anchor, so it is NOT reused here.)\n"
              "  Continuing without video_bg -- the clip.yaml will carry a "
              "TODO."
              % (style, vid,
                 " (this source has: %s)" % ", ".join(have) if have else
                 " (this source has no framing at all)", vid, style),
              file=sys.stderr)
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
