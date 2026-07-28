#!/usr/bin/env python3
"""
golden.py -- regression safety net for the render pipeline.

The renderers build their ffmpeg command line and filtergraph by string
concatenation. This freezes, per golden clip, the exact argv + filter_complex
that the current code produces, plus the md5 of every generated intermediate
(overlay PNGs, and for the bars style the scrim PNG + the snow mp4) and of the
final encode. Any refactor that is meant to be behaviour-preserving must leave
all of these byte-identical.

  render-venv/bin/python scripts/golden.py check <clip>           tier 1: env + argv + graph
  render-venv/bin/python scripts/golden.py check --layers <clip>  tier 2: + regenerate layers
  render-venv/bin/python scripts/golden.py check --full <clip>    tier 3: + full render md5
  render-venv/bin/python scripts/golden.py check --all            tier 1 over every golden clip
  render-venv/bin/python scripts/golden.py bless <clip>           re-record after an INTENDED
                                                   change (add --full to also
                                                   re-record output.md5)

Tiers 2 and 3 NEVER write into a clip's output/ directory -- the encode is
redirected to a scratch path.

Run with tools/render-venv/bin/python.
"""
import os
import sys
import glob
import shutil
import difflib
import hashlib
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
GOLD = os.path.join(ROOT, "tests", "golden")
PYTHON = os.path.join(ROOT, "tools", "render-venv", "bin", "python")
FFMPEG = "/opt/homebrew/bin/ffmpeg"

ARGV_MARK = "=== DRY RUN ARGV ==="
FC_MARK = "=== DRY RUN FILTER_COMPLEX ==="
END_MARK = "=== END DRY RUN ==="


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def clip_dir(name):
    return os.path.join(ROOT, "clips", name)


def golden_dir(name):
    return os.path.join(GOLD, name)


def golden_clips():
    if not os.path.isdir(GOLD):
        return []
    return sorted(d for d in os.listdir(GOLD)
                  if os.path.isdir(os.path.join(GOLD, d)))


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read(path):
    with open(path) as f:
        return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def is_bars(name):
    sys.path.insert(0, HERE)
    import render_text
    clip = render_text.load_yaml(os.path.join(clip_dir(name), "clip.yaml"))
    return render_text.style_of(clip) != "default"


# --------------------------------------------------------------------------
# environment fingerprint
# --------------------------------------------------------------------------
def ffmpeg_version():
    out = subprocess.run([FFMPEG, "-version"], capture_output=True,
                         text=True).stdout
    return out.splitlines()[0].strip() if out else "<ffmpeg not found>"


def env_report():
    lines = [ffmpeg_version()]
    out = subprocess.run([FFMPEG, "-version"], capture_output=True,
                         text=True).stdout
    for ln in out.splitlines():
        s = ln.strip()
        if s.startswith(("libavformat", "libavcodec", "libavfilter",
                         "libavutil")):
            lines.append(s)
    lines.append("python3 " + sys.version.split()[0])
    try:
        import PIL
        from PIL import features
        lines.append("Pillow %s  raqm=%s" % (PIL.__version__,
                                             features.check("raqm")))
    except Exception as e:                                # pragma: no cover
        lines.append("Pillow <unavailable: %s>" % e)
    return lines


ENV_PATH = os.path.join(GOLD, "ENV.txt")
ENV_HEADER = (
    "Environment the golden checksums in this tree were recorded against.\n"
    "The md5s below tests/golden/ are valid ONLY for this exact ffmpeg build:\n"
    "a different ffmpeg (or libx264) will produce a different final.mp4 and a\n"
    "different snow layer even with a byte-identical filtergraph. If ffmpeg is\n"
    "upgraded, the argv/filtergraph goldens stay meaningful but the media md5s\n"
    "must be re-blessed against a known-good visual review.\n"
    "\n")


def write_env():
    write(ENV_PATH, ENV_HEADER + "\n".join(env_report()) + "\n")
    return ENV_PATH


def recorded_ffmpeg_version():
    if not os.path.exists(ENV_PATH):
        return None
    for ln in read(ENV_PATH).splitlines():
        if ln.startswith("ffmpeg version"):
            return ln.strip()
    return None


# --------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------
def dry_run(name):
    """Run the real builder in --dry-run mode; return (argv list, filtergraph).

    This performs every derivation step (Pillow overlays, bar-colour sampling,
    snow/scrim generation, the loudnorm measurement pass, transition
    scheduling) -- only the final encode is skipped.
    """
    cmd = [PYTHON, os.path.join(HERE, "build_render.py"),
           clip_dir(name), "--dry-run"]
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if p.returncode != 0:
        sys.exit("dry-run failed for %s:\n%s" % (name, p.stdout[-3000:]
                                                 + p.stderr[-3000:]))
    lines = p.stdout.splitlines()
    try:
        i = lines.index(ARGV_MARK)
        j = lines.index(FC_MARK)
        k = lines.index(END_MARK)
    except ValueError:
        sys.exit("dry-run produced no markers for %s (is --dry-run wired up?)"
                 % name)
    argv = lines[i + 1:j]
    fc = "\n".join(lines[j + 1:k])
    return argv, fc


# --------------------------------------------------------------------------
# layers
# --------------------------------------------------------------------------
def layer_paths(name, argv):
    """Generated intermediates whose bytes the render depends on: every overlay
    PNG, plus (bars) the scrim PNG and the snow mp4 actually referenced by the
    argv (work/ also accumulates stale layers from earlier parameter sets)."""
    cd = clip_dir(name)
    paths = sorted(glob.glob(os.path.join(cd, "work", "overlays", "*.png")))
    work = os.path.join(cd, "work") + os.sep
    for tok in argv:
        ap = os.path.abspath(os.path.join(ROOT, tok))
        if ap.startswith(work) and os.path.isfile(ap) and ap not in paths:
            base = os.path.basename(ap)
            if base.startswith(("scrim_", "snow_")):
                paths.append(ap)
    return sorted(set(paths))


def layers_text(name, argv):
    cd = clip_dir(name)
    rows = sorted((os.path.relpath(p, cd), md5_file(p))
                  for p in layer_paths(name, argv))
    return "".join("%s  %s\n" % (m, r) for r, m in rows)


def purge_layers(name, argv):
    """Delete the regenerable intermediates so the next dry-run rebuilds them
    from scratch (scrim/snow are content-addressed caches and would otherwise
    be reused)."""
    for p in layer_paths(name, argv):
        os.remove(p)
    ovl = os.path.join(clip_dir(name), "work", "overlays")
    if os.path.isdir(ovl) and not os.listdir(ovl):
        os.rmdir(ovl)


# --------------------------------------------------------------------------
# full render (always to scratch)
# --------------------------------------------------------------------------
def render_to_scratch(name, argv, dest=None):
    """Run the frozen argv with the output path swapped for a scratch file.
    The clip's own output/final.mp4 is never touched."""
    tmpdir = tempfile.mkdtemp(prefix="golden-%s-" % name)
    out = dest or os.path.join(tmpdir, "final.mp4")
    cmd = list(argv[:-1]) + [out]
    print("  rendering to scratch: %s" % out)
    p = subprocess.run(cmd, cwd=ROOT)
    if p.returncode != 0:
        sys.exit("scratch render failed for %s" % name)
    return out, tmpdir


# --------------------------------------------------------------------------
# check / bless
# --------------------------------------------------------------------------
def diff(a, b, label):
    return "\n".join(difflib.unified_diff(
        a.splitlines(), b.splitlines(),
        fromfile="golden/" + label, tofile="current/" + label, lineterm=""))


def wrap(s, width=110):
    """The filtergraph is one enormous line; chop it at filter boundaries so a
    unified diff points at the chain that changed instead of the whole graph."""
    out = []
    for chain in s.split(";"):
        while len(chain) > width:
            cut = chain.rfind(",", 0, width)
            if cut <= 0:
                cut = width
            out.append(chain[:cut + 1])
            chain = chain[cut + 1:]
        out.append(chain)
    return "\n".join(out)


def check(name, layers=False, full=False):
    gd = golden_dir(name)
    if not os.path.isdir(gd):
        print("FAIL %s: no golden recorded (tests/golden/%s missing)"
              % (name, name))
        return False

    # --- env gate FIRST: a toolchain bump must not look like a regression ---
    was = recorded_ffmpeg_version()
    now = ffmpeg_version()
    if was and was != now:
        print("GOLDEN INVALID: ffmpeg changed (was %s, now %s)" % (was, now))
        print("  argv/filtergraph goldens may still hold, but every media md5"
              " must be re-blessed after a visual review.")
        return False

    argv, fc = dry_run(name)
    ok = True

    g_argv = read(os.path.join(gd, "argv.txt"))
    c_argv = "".join(t + "\n" for t in argv)
    if g_argv != c_argv:
        ok = False
        print("FAIL %s: ffmpeg argv differs" % name)
        print(diff(g_argv, c_argv, "argv.txt"))

    g_fc = read(os.path.join(gd, "filtergraph.txt"))
    c_fc = fc + "\n"
    if g_fc != c_fc:
        ok = False
        print("FAIL %s: filter_complex differs" % name)
        print(diff(wrap(g_fc.strip()), wrap(c_fc.strip()), "filtergraph.txt"))

    if layers:
        purge_layers(name, argv)
        argv, _ = dry_run(name)                 # regenerates every layer
        g_lay = read(os.path.join(gd, "layers.md5"))
        c_lay = layers_text(name, argv)
        if g_lay != c_lay:
            ok = False
            print("FAIL %s: generated layers differ" % name)
            print(diff(g_lay, c_lay, "layers.md5"))
        else:
            print("  layers ok (%d files)" % len(c_lay.splitlines()))

    if full:
        out, tmp = render_to_scratch(name, argv)
        got = md5_file(out)
        want = read(os.path.join(gd, "output.md5")).split()[0]
        shutil.rmtree(tmp, ignore_errors=True)
        if got != want:
            ok = False
            print("FAIL %s: output md5 differs (golden %s, got %s)"
                  % (name, want, got))
        else:
            print("  output md5 ok (%s)" % got)

    if ok:
        print("PASS %s%s" % (name, " (argv + filtergraph)"
                             if not (layers or full) else ""))
    return ok


def bless(name, full=False):
    gd = golden_dir(name)
    argv, fc = dry_run(name)
    write(os.path.join(gd, "argv.txt"), "".join(t + "\n" for t in argv))
    write(os.path.join(gd, "filtergraph.txt"), fc + "\n")
    write(os.path.join(gd, "layers.md5"), layers_text(name, argv))
    if full:
        out, tmp = render_to_scratch(name, argv)
        write(os.path.join(gd, "output.md5"),
              "%s  output/final.mp4\n" % md5_file(out))
        shutil.rmtree(tmp, ignore_errors=True)
    write_env()
    print("blessed %s -> tests/golden/%s%s"
          % (name, name, " (incl. output.md5)" if full else ""))


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    mode, args = args[0], args[1:]
    layers = "--layers" in args
    full = "--full" in args
    allc = "--all" in args
    names = [a for a in args if not a.startswith("--")]
    names = [n.rstrip("/").split("/")[-1] for n in names]

    if mode == "check":
        if allc:
            names = golden_clips()
        if not names:
            sys.exit("usage: golden.py check [--layers|--full] <clip> | --all")
        ok = all([check(n, layers=layers, full=full) for n in names])
        sys.exit(0 if ok else 1)
    elif mode == "bless":
        if allc:
            names = golden_clips()
        if not names:
            sys.exit("usage: golden.py bless [--full] <clip>")
        for n in names:
            bless(n, full=full)
    elif mode == "env":
        print(write_env())
        print("\n".join(env_report()))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
