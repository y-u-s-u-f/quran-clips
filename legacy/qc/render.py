"""
qc.render -- the three ways to look at a bars clip.

    full     `qc render <clip>`              output/final.mp4, ~384 s for 24 s
    preview  `qc render <clip> --preview`    output/preview.mp4, target <=30 s
    stills   `qc frames <clip> --at 0,6,12`  output/frames/*.png, full fidelity

The preview is a DIFFERENT PICTURE, deliberately: half canvas, and every
expensive glow/particle/warp stage switched off. It answers "is the timing
right, does the caption swap read, does the text sit where I meant" in half a
minute. It cannot answer anything about glow, grain or shimmer, and it must
never be mistaken for the finished thing -- hence output/preview.mp4, a
`comment=qc-preview` tag in the container, and an export that refuses it.

The stills are the opposite trade: the FULL graph at full size, but only the
frames asked for. Because the renderer's window support moves the whole
schedule rather than seeking into it, one still costs one frame of work, and
that is what makes crop / colour / caption-position questions answerable in
seconds instead of minutes. What a still cannot show is the phase of the two
time-varying effects (snow's scroll and heat's perlin field both restart with
the window), so it is a picture of the composition, not of the grain.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "scripts"), ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from qc import scale as S           # noqa: E402


def _load(clip_dir):
    import build_render             # noqa: F401  (its import wires the paths)
    import render_text
    import qc.timeline
    clip = render_text.load_yaml(os.path.join(clip_dir, "clip.yaml"))
    if render_text.style_of(clip) == "default":
        sys.exit("preview / stills are implemented for the `bars` style only; "
                 "%s is the default landscape style"
                 % os.path.basename(clip_dir.rstrip("/")))
    # MUTATES the phrase times onto the cut timeline -- exactly once per load,
    # which is why the stills loop resolves the segment here and not per frame.
    src, ss, dur = qc.timeline.segment(clip_dir, clip)
    return clip, src, ss, dur


def preview(clip_dir, scale=0.5):
    import build_render
    t0 = time.time()
    out = build_render.build(clip_dir, opts=S.preview_opts(scale))
    el = time.time() - t0
    print("\nPREVIEW %s in %.1fs  (NOT a final render: half canvas, no %s)"
          % (os.path.relpath(out, ROOT), el, "/".join(S.PREVIEW_OFF)))
    return out, el


def frames(clip_dir, times, out_dir=None):
    import build_bars
    import render_bars
    import render_text
    t0 = time.time()
    clip, src, ss, dur = _load(clip_dir)
    style = render_text.load_yaml(render_text.template_path(clip))
    fps = int(style["meta"]["fps"])
    # build_bars always writes under <clip>/output/, so the stills land in
    # <clip>/output/frames/ whatever -o says. Make that directory unconditionally
    # -- ffmpeg's image2 muxer does not create it and fails with a bare
    # "No such file or directory" -- and treat -o as a copy-out destination.
    built_dir = os.path.join(clip_dir, "output", "frames")
    os.makedirs(built_dir, exist_ok=True)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    rep = render_bars.build(clip_dir, clip=clip, style=style)
    made = []
    for t in times:
        t = max(0.0, min(float(t), max(0.0, dur - 1.0 / fps)))
        name = os.path.join("frames", "t%07.3f.png" % t)
        opts = S.Opts(out_name=name, window=(t, 1.0 / fps), still=True,
                      reuse=rep)
        made.append(build_bars.build(clip_dir, ctx=(clip, src, ss, dur),
                                     opts=opts))
    if out_dir and os.path.abspath(out_dir) != os.path.abspath(built_dir):
        import shutil
        made = [shutil.copy2(p, os.path.join(out_dir, os.path.basename(p)))
                for p in made]
    el = time.time() - t0
    print("\nFRAMES %d still(s) in %.1fs (full fidelity):" % (len(made), el))
    for p in made:
        print("  " + os.path.relpath(p, ROOT))
    return made, el
