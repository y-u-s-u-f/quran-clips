#!/usr/bin/env python3
"""
build_bars.py  (Agent C / renderer -- "bars" style ffmpeg driver)

Sibling of build_render.py for the vertical 1080x1920 "bars" style
(templates/bars.yaml). Reached from build_render.build() when clip.yaml
carries `style: bars`; the original landscape path is untouched.

Assembles and RUNS into <clip>/output/final.mp4:

  * frame-accurate trim of the source segment
  * the 16:9 footage band: crop -> 1080x608 -> GRADE (eq/colorbalance/vignette;
    step 0 of FX_RECIPE and the biggest single contributor to the look) ->
    padded into 1080x1920 on pure black
  * per-phrase caption overlays from render_bars.py, TWO layers per phrase
    (barN = the pill, textN = white Thuluth + its hard drop shadow) so the two
    can be animated independently, transitioned per templates/bars.yaml
    `transitions`:
      - wipe      : directional RTL sweep. Entry pins the right edge and walks
                    the left edge out to full width; exit pins the left edge
                    and sweeps the right edge back. Implemented as a moving,
                    feathered mask multiplied into the layer's alpha. Which
                    layers it drives is `wipe_target` -- `bar` (default) sweeps
                    the pill only and cross-fades the text over the same
                    window, `all` sweeps both.
      - crossfade : uniform alpha dissolve (the pipeline's 0.45 s feel)
  * FX confined STRICTLY to the footage band (any spill into the letterbox
    instantly reads as wrong), per FX_RECIPE.md. WHICH effects run, and in what
    order, is data: templates/bars.yaml `fx.order` lists the band chain and
    every effect carries its own `enable`, overridable per clip with
    `fx: {scan: false}` in clip.yaml. The effects themselves live in qc/fx/ --
    each owns its filter strings, its derived scalars and all of its hooks, so
    switching one off removes its accumulator plate and its per-phrase
    branches too and ffgraph's tap() recomputes the band `split=N` by itself.
    The `grade` and `scrim` stages are switchable the same way but are wired
    ahead of the composite, at their own sites.
    Heat Wave is ON and runs LAST, over the composited band, so the footage,
    the pills and the glyphs are displaced by one shared perlin field. It is
    the most expensive stage in the graph (~49% of render time); set
    `fx: {heat: false}` for a fast preview.
  * audio: the same two-pass loudnorm + fades as build_render.py
  * encode: libx264 yuv420p 30fps, aac, +faststart

Run with tools/render-venv/bin/python.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import render_text          # noqa: E402
import render_bars          # noqa: E402
import build_render         # noqa: E402

sys.path.insert(0, ROOT)
import qc.audio             # noqa: E402
import qc.ffgraph           # noqa: E402
import qc.fx                # noqa: E402
import qc.timeline          # noqa: E402

FFMPEG = build_render.FFMPEG


def build(clip_dir, dry_run=False, ctx=None):
    """`ctx` is the (clip, src, ss, dur) build_render.build() already resolved
    -- it owns loading clip.yaml and applying segment.cuts, and its phrase times
    are on the cut timeline. Absent (i.e. this module run directly), do it
    here; the two paths must stay equivalent."""
    if ctx is None:
        clip = render_text.load_yaml(os.path.join(clip_dir, "clip.yaml"))
        src, ss, dur = qc.timeline.segment(clip_dir, clip)
    else:
        clip, src, ss, dur = ctx
    style = render_text.load_yaml(render_text.template_path(clip))

    W = int(style["canvas"]["width"])
    H = int(style["canvas"]["height"])
    band = style["band"]
    BW, BH, BY = int(band["width"]), int(band["height"]), int(band["y"])
    fps = int(style["meta"]["fps"])
    tr = style["transitions"]
    aud = style["audio"]
    mot = style["motion"]
    enc = style["encode"]

    # ---- (re)build overlays + the particle layer; also derives the bar colour
    rep = render_bars.build(clip_dir)
    # inputs are interleaved bar,text per phrase (see the -i loop below)
    phrase_pngs = [p[k] for p in rep["phrases"] for k in ("bar", "text")]
    snow = rep["snow"]
    phrases = clip["phrases"]
    assert len(phrases) == len(rep["phrases"]), "phrase/overlay count mismatch"

    # ---- transition schedule -------------------------------------------------
    # `sequential` (bars default): NO two captions are ever on screen together.
    # Each card is fully up on its own word onset, and every changeover is
    # serialised into the gap the recitation leaves -- fade-out to alpha 0
    # first, incoming fade-in only after. `sequential: false` restores
    # build_render's overlapping dissolve (incoming starts 0.24 s early).
    xf = float(tr["crossfade_s"])
    seq = str(tr["sequential"]).lower() in ("true", "yes", "1")
    minf = float(tr["min_fade_s"])
    kinds = [str(tr["first"] if i == 0 else tr["rest"]).lower()
             for i in range(len(phrases))]
    sched, cuts = qc.timeline.schedule(
        phrases, dur, fps=fps, kinds=kinds, crossfade_s=xf,
        wipe_in_s=float(tr["wipe_in_s"]), wipe_out_s=float(tr["wipe_out_s"]),
        sequential=seq, min_fade_s=minf,
        wipe_out_anchor=tr.get("wipe_out_anchor", "start"))

    # ---- audio ---------------------------------------------------------------
    lufs, tp, lra = float(aud["lufs"]), float(aud["true_peak_dbtp"]), float(aud["lra"])
    st = qc.audio.measure_loudness(src, ss, dur, lufs, tp, lra)
    ln = qc.audio.loudnorm_filter(st, lufs, tp, lra)
    afi, afo = float(aud["fade_in_s"]), float(aud["fade_out_s"])
    afade = qc.audio.afade_filter(dur, afi, afo)

    # ---- the effect chain ----------------------------------------------------
    # `fx.order` in the template is the chain order; every effect resolves its
    # own enable flag, which a clip overrides with `fx: {name: false}`. An
    # unknown name in either place raises here rather than silently doing
    # nothing. Everything about an effect -- its filter strings, its derived
    # scalars, its plate and its per-phrase branch -- lives in qc/fx/.
    fx_chain = qc.fx.fx_chain(style, clip)
    fx_layer = qc.fx.by_layer(fx_chain)
    scrim_on = qc.fx.enabled(style, clip, "scrim")
    tint = [c / 255.0 for c in render_text.hexrgb(rep["bar_hex"], (201, 162, 39))]

    # ---- filtergraph ---------------------------------------------------------
    # inputs: [0]=source  [1..2n]=bar,text per phrase  [2n+1]=snow  [2n+2]=scrim
    ph_base = 1
    g_ = qc.ffgraph.Graph()
    vin = g_.input(src, ss=f"{ss:.3f}", t=f"{dur:.3f}")
    ph_in = [g_.input(png, framerate=fps, loop=1) for png in phrase_pngs]
    snow_in = g_.input(snow, stream_loop=-1)
    # the scrim is the LAST input, so dropping it cannot shift any other index
    scrim_in = g_.input(rep["scrim"], framerate=fps, loop=1) if scrim_on else None

    # what the effects need to know about this canvas, this band and this clip
    fxctx = qc.fx.Ctx(W=W, H=H, BW=BW, BH=BH, BY=BY, fps=fps, dur=dur,
                      tint=tint, nphrases=len(phrases), band_src="band",
                      snow_in=snow_in, hexrgb=render_text.hexrgb)

    wtgt = str(tr["wipe_target"]).lower()
    g_.chain(vin, [render_bars.band_source_chain(clip, style), "setsar=1",
                   f"fps={fps}", "format=gbrp"], "bnd")
    if scrim_on:
        g_.chain(scrim_in, "format=gbrp", "scr")
        g_.chain(["bnd", "scr"],
                 ["blend=all_mode=multiply:shortest=1", "format=rgba",
                  f"pad={W}:{H}:0:{BY}:color=black"], "bg")
    else:
        g_.chain("bnd", ["format=rgba", f"pad={W}:{H}:0:{BY}:color=black"],
                 "bg")
    # The caption layers are accumulated a second time onto black plates, to key
    # the barglow / textglow passes off; `d=` keeps those branches finite (every
    # other input on them is an infinite still). Each plate belongs to its
    # effect and disappears with it.
    for eff in qc.fx.plates(fx_chain):
        eff.plate(g_, fxctx)
    base = "bg"
    for i, (kind, t_in, d_in, t_out, d_out) in enumerate(sched):
        masks = {}                                # layer name -> mask label
        if kind == "wipe":
            x0, x1 = rep["phrases"][i]["x0"], rep["phrases"][i]["x1"]
            feather = float(tr["wipe_feather_px"])
            # front travel: the full bar span plus enough margin for the
            # feathered front to clear both ends completely.
            trav = (x1 - x0) + 2 * feather + 40
            a = t_in + d_in                       # entry complete
            # A full-frame white plate slid over black: while it enters, its
            # LEFT edge is the front and everything right of it is already lit
            # (right edge pinned); on the way out the plate's RIGHT edge is the
            # front and it is dragged off to the left (left edge pinned).
            # `min(0, ...)` keeps the plate flush during the steady phase.
            # (drawbox is not usable here -- its `t` is the box thickness, not
            # the timestamp, so a time-varying box silently draws nothing.)
            fi = f"{x1:.1f}-{trav:.1f}*(t-{t_in:.3f})/{d_in:.3f}"
            fo = f"{x1:.1f}-{trav:.1f}*(t-{t_out:.3f})/{d_out:.3f}"
            xe = f"if(lt(t,{a:.3f}),max(0,{fi}),min(0,{fo}-{W}))"
            g_.chain(None, f"color=c=black:s={W}x{H}:r={fps}", f"kb{i + 1}")
            g_.chain(None, f"color=c=white:s={W}x{H}:r={fps}", f"kw{i + 1}")
            g_.chain([f"kb{i + 1}", f"kw{i + 1}"],
                     [f"overlay=x='{xe}':y=0:shortest=1",
                      f"gblur=sigma={feather / 4.0:.2f}:steps=2", "format=gray"],
                     f"m{i + 1}")
            if wtgt == "all":
                g_.chain(f"m{i + 1}", "split=2",
                         [f"mbar{i + 1}", f"mtext{i + 1}"])
                masks = {"bar": f"mbar{i + 1}", "text": f"mtext{i + 1}"}
            else:
                # `bar`: the pill alone rides the sweep; the text dissolves.
                masks = {"bar": f"m{i + 1}"}
        # bar first, text over it -- and so the shadow (which lives in the text
        # layer) lands ON the pill, as measured in the references.
        for j, name in enumerate(("bar", "text")):
            sidx = ph_base + 2 * i + j
            lbl = f"{name}{i + 1}"
            if name in masks:
                g_.chain(ph_in[sidx - ph_base], ["format=rgba", "split=2"],
                         [f"p{lbl}", f"pa{lbl}"])
                g_.chain(f"pa{lbl}", "alphaextract", f"a{lbl}")
                g_.chain([f"a{lbl}", masks[name]],
                         "blend=all_mode=multiply:shortest=1", f"am{lbl}")
                g_.chain([f"p{lbl}", f"am{lbl}"], "alphamerge", f"x{lbl}")
            else:
                g_.chain(ph_in[sidx - ph_base],
                         ["format=rgba",
                          f"fade=t=in:st={t_in:.3f}:d={d_in:.3f}:alpha=1",
                          f"fade=t=out:st={t_out:.3f}:d={d_out:.3f}:alpha=1"],
                         f"x{lbl}")
            # the layer's glow effect, if enabled, takes its own branch off
            # this layer here and hands back the label to composite instead
            eff = fx_layer.get(name)
            if eff is not None:
                lbl = eff.per_phrase(g_, fxctx, i, lbl)
            nxt = f"c{i + 1}{name[0]}"
            g_.chain([base, f"x{lbl}"], "overlay=0:0:format=auto:shortest=1",
                     nxt)
            base = nxt

    # ---- FX ------------------------------------------------------------------
    # Applied to the captioned flat but cropped to the band and pasted back, so
    # nothing can bleed into the letterbox. WHICH effects run and in what order
    # is `fx.order` (see qc/fx/); this loop just threads the band label through
    # them. Every branch off the band is a tap, so the split's degree is
    # whatever the enabled consumers add up to: disabling `scan` takes it from
    # 3 to 2 with no edit here at all.
    full, pre = g_.chain(base, "split=2", ["full", "pre"])
    g_.chain(pre, ["format=gbrp", f"crop={BW}:{BH}:0:{BY}"], "band")
    band_lbl = g_.tap("band", "fxbase")
    for eff in fx_chain:
        band_lbl = eff.apply(g_, band_lbl, fxctx)
    g_.chain([full, band_lbl],
             [f"overlay=0:{BY}:shortest=1",
              f"fade=t=in:st=0:d={mot['video_fade_in_s']}",
              f"fade=t=out:st={dur - float(mot['video_fade_out_s']):.3f}:"
              f"d={mot['video_fade_out_s']}", "format=yuv420p"], "vout")
    g_.chain(vin.a, [ln, afade], "aout")
    fc, in_argv = g_.render()

    out_dir = os.path.join(clip_dir, "output")
    if not dry_run:
        os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "final.mp4")

    cmd = [FFMPEG, "-y", "-hide_banner"] + in_argv
    cmd += ["-filter_complex", fc,
            "-map", "[vout]", "-map", "[aout]",
            "-t", f"{dur:.3f}",
            "-c:v", "libx264", "-crf", str(enc["crf"]),
            "-preset", str(enc["preset"]),
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", str(enc["audio_bitrate"]),
            "-movflags", "+faststart", out_path]
    if dry_run:
        build_render.emit_dry_run(cmd, fc)
        return out_path
    if build_render.run(cmd).returncode != 0:
        sys.exit("ffmpeg failed")

    print("\nRENDER OK ->", out_path)
    print(f"  trim {ss:.3f}s +{dur:.3f}s | {W}x{H} band {BW}x{BH}@y{BY} | "
          f"bar {rep['bar_hex']}")
    sw = qc.fx.switches(style, clip)
    print("  fx: " + " ".join(("+" if sw[n] else "-") + n
                              for n in ("grade", "scrim") + qc.fx.BAND_FX))
    print(f"  wipe_target={wtgt} (text always cross-fades unless 'all')"
          + (f" | sequential, min fade {minf}s" if seq else " | OVERLAPPING"))
    if cuts:
        print("  HARD CUT at changeover(s): " + ", ".join(f"P{c}->P{c + 1}"
                                                          for c in cuts))
    print("  " + " | ".join(
        f"P{i + 1} {k} in@{ti:.2f}+{di:.2f} out@{to:.2f}+{do:.2f}"
        for i, (k, ti, di, to, do) in enumerate(sched)))
    return out_path


if __name__ == "__main__":
    _args = [a for a in sys.argv[1:] if a != "--dry-run"]
    build(_args[0] if _args else os.path.join(ROOT, "clips", "at-tawbah-128-128"),
          dry_run="--dry-run" in sys.argv[1:])
