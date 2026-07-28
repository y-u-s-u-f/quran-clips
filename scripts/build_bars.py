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
    instantly reads as wrong): Glow -> Glow Scan -> Snow, per FX_RECIPE.md.
    Heat Wave is deliberately absent (invisible, +66% bitrate, tears the pill).
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
from qc.timeline import hms  # noqa: E402

FFMPEG = build_render.FFMPEG


def build(clip_dir, dry_run=False):
    clip = render_text.load_yaml(os.path.join(clip_dir, "clip.yaml"))
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

    seg = clip["segment"]
    ss = hms(seg["start"])
    dur = round(hms(seg["end"]) - ss, 3)
    src = os.path.join(clip_dir, "work", "source.mp4")

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
    n_ph = len(phrases)

    def snap(t):
        """On the frame grid, so a fade-out's last frame and the next fade-in's
        first frame cannot straddle the same frame through rounding."""
        return round(t * fps) / float(fps)

    sched = []
    for i, ph in enumerate(phrases):
        d_in = float(tr["wipe_in_s"]) if kinds[i] == "wipe" else xf
        d_out = float(tr["wipe_out_s"]) if kinds[i] == "wipe" else xf
        if seq:
            t_in = max(0.0, ph["t0"] - 0.05) if i == 0 else ph["t0"] - d_in
            t_out = (min(dur - d_out, ph["t1"] - 0.15)
                     if i == n_ph - 1 else ph["t1"])
        else:
            t_in = max(0.0, ph["t0"] - 0.05) if i == 0 else ph["t0"] - 0.24
            t_out = (min(dur - d_out, ph["t1"] - 0.15)
                     if i == n_ph - 1 else ph["t1"] + 0.02)
        sched.append([kinds[i], t_in, d_in, t_out, d_out])

    cuts = []
    if seq:
        guard = 0.5 / fps          # outgoing is gone half a frame early, so no
        for i in range(n_ph - 1):  # single frame can carry both captions
            t0n, t1c = float(phrases[i + 1]["t0"]), float(phrases[i]["t1"])
            gap = t0n - t1c
            d_out, d_in = sched[i][4], sched[i + 1][2]
            nom_out = float(tr["wipe_out_s"]) if kinds[i] == "wipe" else xf
            # `wipe_out_anchor: end` -- a wipe-out is EXEMPT from the shrink so
            # that it mirrors its wipe-in exactly (same duration, and since both
            # sweeps cover the same `trav`, the same front speed). It is placed
            # by subtracting its full duration from `end`, i.e. it starts BEFORE
            # t1 and eats the outgoing caption's tail, rather than starting at
            # t1 and being clipped. The incoming fade-in is still fitted to the
            # gap below exactly as before, so caption N+1 does not move.
            anch = kinds[i] == "wipe" and \
                str(tr.get("wipe_out_anchor", "start")).lower() == "end"
            if d_out + d_in + guard > gap:            # shrink both, in ratio
                k = max(0.0, gap - guard) / (d_out + d_in)
                d_out, d_in = max(minf, d_out * k), max(minf, d_in * k)
            if anch:                                  # ...but not the wipe-out
                d_out = nom_out
            t_in = snap(t0n - d_in)                   # up on the word onset
            end = t_in - guard                        # outgoing gone by here
            slack = end - d_out - snap(t1c)
            if slack > 0 and not anch:                # spare gap: fade slower
                d_out = min(nom_out, d_out + slack)
            t_out = end - d_out
            floor = sched[i][1] + sched[i][2]         # fully up only by here
            if t_out < floor:                         # -> cannot fade at all
                d_out = max(1.0 / fps, end - floor)
                t_out = end - d_out
                cuts.append(i + 1)
            sched[i + 1][2], sched[i + 1][1] = d_in, t_in
            sched[i][4], sched[i][3] = d_out, t_out
    sched = [tuple(x) for x in sched]

    # ---- audio ---------------------------------------------------------------
    lufs, tp, lra = float(aud["lufs"]), float(aud["true_peak_dbtp"]), float(aud["lra"])
    st = qc.audio.measure_loudness(src, ss, dur, lufs, tp, lra)
    ln = qc.audio.loudnorm_filter(st, lufs, tp, lra)
    afi, afo = float(aud["fade_in_s"]), float(aud["fade_out_s"])
    afade = qc.audio.afade_filter(dur, afi, afo)

    # ---- FX constants (FX_RECIPE parameter mapping, scaled to this band) -----
    g, sc, sn = style["fx"]["glow"], style["fx"]["scan"], style["fx"]["snow"]
    sig = round(float(g["scatter"]) * 2.86 * BH / 405.0, 2)
    ssig = round(float(sc["scatter"]) * 1.20 * float(sc["radius"]) * 2 * BH / 405.0, 2)
    lo, hi = round(float(g["lo"]) * 255), round(float(g["hi"]) * 255)
    knee = round(255.0 / (hi - lo), 6)
    sth = round(float(sc["threshold"]) * 255)
    sknee = round(255.0 / (255 - sth), 6)
    tint = [c / 255.0 for c in render_text.hexrgb(rep["bar_hex"], (201, 162, 39))]
    gr, gg, gb = [round(c * float(g["gain"]), 4) for c in tint]
    sr, sg, sb = [round(c * float(sc["gain"]), 4) for c in tint]
    # The pill's own glow rides the BAR layer alone: keyed off the composite it
    # is at the mercy of scene brightness (measured: 3.4x the gain moved the
    # excess at d=5 by 1 DN). Two gaussians, near + far, because the refs' tail
    # is exponential rather than gaussian (STYLE2_SPEC B.6).
    bg_ = style["fx"]["barglow"]
    bgsn = round(float(bg_["sigma_near_px"]), 2)
    bgsf = round(float(bg_["sigma_far_px"]), 2)
    bgn = [round(c * float(bg_["gain"]) * float(bg_["weight_near"]), 4) for c in tint]
    bgf = [round(c * float(bg_["gain"]) * float(bg_["weight_far"]), 4) for c in tint]
    # the tight halo rides the TEXT layer alone and carries its own near-white
    # tint -- the refs' halo is warm neutral, notably NOT the bar hue.
    tg = style["fx"]["textglow"]
    tgsig = round(float(tg["sigma_px"]), 2)
    tgr, tgg, tgb = [round(c / 255.0 * float(tg["gain"]), 4)
                     for c in render_text.hexrgb(tg["tint"], (255, 244, 224))]
    LUMA = ("colorchannelmixer=rr=0.299:rg=0.587:rb=0.114:"
            "gr=0.299:gg=0.587:gb=0.114:br=0.299:bg=0.587:bb=0.114")
    KNEE = (f"lutrgb=r='clip((val-{lo})*{knee},0,255)':"
            f"g='clip((val-{lo})*{knee},0,255)':b='clip((val-{lo})*{knee},0,255)'")
    SKNEE = (f"lutrgb=r='clip((val-{sth})*{sknee},0,255)':"
             f"g='clip((val-{sth})*{sknee},0,255)':b='clip((val-{sth})*{sknee},0,255)'")

    # ---- filtergraph ---------------------------------------------------------
    # inputs: [0]=source  [1..2n]=bar,text per phrase  [2n+1]=snow  [2n+2]=scrim
    ph_base = 1
    snow_idx = ph_base + 2 * len(phrases)
    scrim_idx = snow_idx + 1
    wtgt = str(tr["wipe_target"]).lower()
    parts = [
        f"[0:v]{render_bars.band_source_chain(clip, style)},setsar=1,fps={fps},"
        f"format=gbrp[bnd]"
        f";[{scrim_idx}:v]format=gbrp[scr]"
        f";[bnd][scr]blend=all_mode=multiply:shortest=1,format=rgba,"
        f"pad={W}:{H}:0:{BY}:color=black[bg]"
        # the text layers are accumulated a second time onto a black plate to
        # key the tight halo off; `d=` keeps this branch finite (every other
        # input on it is an infinite still).
        f";color=c=black:s={W}x{H}:r={fps}:d={dur:.3f}[tg0]"
        # ...and the bar layers likewise, as a WHITE silhouette so the glow's
        # amplitude does not follow the (clip-derived) pill colour.
        f";color=c=black:s={W}x{H}:r={fps}:d={dur:.3f}[bq0]"
    ]
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
            parts.append(
                f";color=c=black:s={W}x{H}:r={fps}[kb{i + 1}]"
                f";color=c=white:s={W}x{H}:r={fps}[kw{i + 1}]"
                f";[kb{i + 1}][kw{i + 1}]overlay=x='{xe}':y=0:shortest=1,"
                f"gblur=sigma={feather / 4.0:.2f}:steps=2,format=gray[m{i + 1}]"
            )
            if wtgt == "all":
                parts.append(f";[m{i + 1}]split=2[mbar{i + 1}][mtext{i + 1}]")
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
                parts.append(
                    f";[{sidx}:v]format=rgba,split=2[p{lbl}][pa{lbl}]"
                    f";[pa{lbl}]alphaextract[a{lbl}]"
                    f";[a{lbl}][{masks[name]}]blend=all_mode=multiply:shortest=1[am{lbl}]"
                    f";[p{lbl}][am{lbl}]alphamerge[x{lbl}]"
                )
            else:
                parts.append(
                    f";[{sidx}:v]format=rgba,"
                    f"fade=t=in:st={t_in:.3f}:d={d_in:.3f}:alpha=1,"
                    f"fade=t=out:st={t_out:.3f}:d={d_out:.3f}:alpha=1[x{lbl}]"
                )
            if name == "bar":
                parts.append(
                    f";[x{lbl}]split=2[x{lbl}c][x{lbl}g]"
                    f";[x{lbl}g]alphaextract[q{i + 1}]"
                    f";color=c=white:s={W}x{H}:r={fps}:d={dur:.3f}[qw{i + 1}]"
                    f";[qw{i + 1}][q{i + 1}]alphamerge[qm{i + 1}]"
                    f";[bq{i}][qm{i + 1}]overlay=0:0:format=auto:shortest=1[bq{i + 1}]")
                lbl = f"{lbl}c"
            if name == "text":
                parts.append(f";[x{lbl}]split=2[x{lbl}c][x{lbl}g]"
                             f";[tg{i}][x{lbl}g]overlay=0:0:format=auto:"
                             f"shortest=1[tg{i + 1}]")
                lbl = f"{lbl}c"
            nxt = f"c{i + 1}{name[0]}"
            parts.append(
                f";[{base}][x{lbl}]overlay=0:0:format=auto:shortest=1[{nxt}]")
            base = nxt

    # ---- HEAT WAVE (4th Node effect; LAST, over the composited band) ---------
    # Applied after glow/scan/snow so the footage, the pills and the glyphs are
    # displaced by one shared field -- Node runs over a flattened export, so the
    # warp inherently hits the text. Measured in both refs at 0.96 px RMS @720
    # against a 0.10 px noise floor. See FX_RECIPE.md "Heat Wave - motion re-test".
    ht = style["fx"].get("heat", {})
    if int(ht.get("enable", 0)):
        S = int(ht.get("supersample", 3))
        rms = float(ht["rms_frac_w"]) * W            # RMS displacement in px
        ctr = float(ht.get("perlin_centre", 130.26))
        sdp = float(ht.get("perlin_sd", 18.4))
        k = round(rms * S / sdp, 5)                  # px -> 8-bit map slope
        SW, SH = BW * S, BH * S
        psrc = (f"perlin=size={BW}x{BH}:rate={fps}:octaves={int(ht.get('octaves', 6))}"
                f":persistence={ht.get('persistence', 0.6)}"
                f":xscale={ht['xscale']}:yscale={ht['xscale']}"
                f":tscale={ht['tscale']}:random_mode=seed")
        pmap = (f"scroll=vertical={ht['scroll_v']},"
                f"scale={SW}:{SH}:flags=bicubic,format=gbrp,"
                f"lutrgb=r='128+(val-{ctr})*{k}':g='128+(val-{ctr})*{k}':"
                f"b='128+(val-{ctr})*{k}'")
        # neighbour up / area down: the upscale must not soften, the downscale
        # must average -- that is what turns integer `displace` into 1/S px.
        heat_in = "bandfx0"
        heatg = (f";{psrc}:seed={int(ht.get('seed_x', 11))},{pmap}[xm]"
                 f";{psrc}:seed={int(ht.get('seed_y', 77))},{pmap}[ym]"
                 f";[bandfx0]scale={SW}:{SH}:flags=neighbor[bigb]"
                 f";[bigb][xm][ym]displace=edge=smear,"
                 f"scale={BW}:{BH}:flags=area[bandfx]")
    else:
        heat_in, heatg = "bandfx", ""

    # FX, applied to the captioned flat but cropped to the band and pasted back
    # so nothing can bleed into the letterbox.
    parts.append(
        f";[{base}]split=2[full][pre]"
        f";[pre]format=gbrp,crop={BW}:{BH}:0:{BY}[band]"
        ";[band]split=3[fxbase][g1][sc0]"
        f";[g1]{LUMA},{KNEE},gblur=sigma={sig}:steps=3,"
        f"colorchannelmixer=rr={gr}:gg={gg}:bb={gb}[glow]"
        ";[fxbase][glow]blend=all_mode=screen:shortest=1[s1]"
        f";[bq{len(phrases)}]format=gbrp,split=2[bqn][bqf]"
        f";[bqn]gblur=sigma={bgsn}:steps=3,"
        f"colorchannelmixer=rr={bgn[0]}:gg={bgn[1]}:bb={bgn[2]}[bgn]"
        f";[bqf]gblur=sigma={bgsf}:steps=3,"
        f"colorchannelmixer=rr={bgf[0]}:gg={bgf[1]}:bb={bgf[2]}[bgf]"
        f";[bgn][bgf]blend=all_mode=addition:shortest=1,"
        f"crop={BW}:{BH}:0:{BY}[barglow]"
        ";[s1][barglow]blend=all_mode=screen:shortest=1[s1a]"
        f";[tg{len(phrases)}]format=gbrp,crop={BW}:{BH}:0:{BY},{LUMA},"
        f"gblur=sigma={tgsig}:steps=3,"
        f"colorchannelmixer=rr={tgr}:gg={tgg}:bb={tgb}[tglow]"
        ";[s1a][tglow]blend=all_mode=screen:shortest=1[s1b]"
        f";[sc0]{LUMA},{SKNEE},gblur=sigma={ssig}:steps=3,"
        "lutrgb=r='clip(val*3,0,255)':g='clip(val*3,0,255)':b='clip(val*3,0,255)',"
        f"colorchannelmixer=rr={sr}:gg={sg}:bb={sb}[scan]"
        ";[s1b][scan]blend=all_mode=screen:shortest=1[s2]"
        f";[{snow_idx}:v]format=gbrp,scale={BW}:{BH}:flags=neighbor,setsar=1[pt]"
        f";[s2][pt]blend=all_mode=screen:shortest=1[{heat_in}]"
        f"{heatg}"
        f";[full][bandfx]overlay=0:{BY}:shortest=1,"
        f"fade=t=in:st=0:d={mot['video_fade_in_s']},"
        f"fade=t=out:st={dur - float(mot['video_fade_out_s']):.3f}:"
        f"d={mot['video_fade_out_s']},format=yuv420p[vout]"
    )
    parts.append(f";[0:a]{ln},{afade}[aout]")
    fc = "".join(parts)

    out_dir = os.path.join(clip_dir, "output")
    if not dry_run:
        os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "final.mp4")

    cmd = [FFMPEG, "-y", "-hide_banner",
           "-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", src]
    for png in phrase_pngs:
        cmd += ["-framerate", str(fps), "-loop", "1", "-i", png]
    cmd += ["-stream_loop", "-1", "-i", snow]
    cmd += ["-framerate", str(fps), "-loop", "1", "-i", rep["scrim"]]
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
