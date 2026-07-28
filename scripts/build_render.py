#!/usr/bin/env python3
"""
build_render.py  (Agent C / renderer)

Reads clip.yaml + style.yaml, (re)builds the text/reciter/grade overlays via
render_text.py, then assembles and RUNS the full ffmpeg pipeline into
<clip>/output/final.mp4:

  * frame-accurate trim of the source segment (re-encode, crf 18)
  * colour treatment  : slight dim + soft vignette + subtle dark gradient
                        behind the text region (grade_overlay.png)
  * reciter side      : feathered, colour-matched portrait (reciter_overlay.png)
  * per-phrase text    : overlays with ~0.45 s opacity crossfades (the outgoing
                        fade-out overlaps the incoming fade-in -> dissolve)
  * audio             : NO added reverb; two-pass loudnorm to -14 LUFS
                        integrated (TP -1 dBTP), fade in 0.3 s / out 0.5 s
  * encode            : libx264 crf 18 preset slow yuv420p 30fps, aac 192k,
                        +faststart

Native landscape 1920x1080 output (per STYLE_SPEC authoritative fractions).
ffmpeg here is a SLIM build (no drawtext/subtitles/libass) -> all text is
pre-rendered PNG + overlay, which this pipeline relies on.

Run with tools/render-venv/bin/python.
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import render_text  # noqa: E402
# re-exported: build_bars.py and the golden harness reach for these through
# this module, which is the pipeline's entrypoint.
from qc.proc import FFMPEG, FFPROBE, run  # noqa: E402,F401
import qc.timeline  # noqa: E402
from qc.timeline import hms  # noqa: E402,F401
from qc.audio import (LUFS, TP, LRA, measure_loudness,  # noqa: E402,F401
                      loudnorm_filter, afade_filter)

CROSSFADE = 0.45   # style.yaml motion.crossfade_s
FADE_IN_A = 0.3    # style.yaml audio.fade_in_s
FADE_OUT_A = 0.5   # style.yaml audio.fade_out_s


DRY_ARGV_MARK = "=== DRY RUN ARGV ==="
DRY_FC_MARK = "=== DRY RUN FILTER_COMPLEX ==="
DRY_END_MARK = "=== END DRY RUN ==="


def emit_dry_run(cmd, fc):
    """Print the fully-derived ffmpeg argv (one token per line) + the complete
    filter_complex, for the golden regression tests. No encode is run."""
    print(DRY_ARGV_MARK)
    for tok in cmd:
        print(tok)
    print(DRY_FC_MARK)
    print(fc)
    print(DRY_END_MARK)


def build(clip_dir, dry_run=False):
    clip = render_text.load_yaml(os.path.join(clip_dir, "clip.yaml"))

    # ---- resolve the trim, splicing out any segment.cuts, BEFORE the style
    #      dispatch so that BOTH styles honour `cuts` (they used to be applied
    #      after it, and were therefore silently ignored on bars clips). This
    #      also remaps the in-memory phrase times onto the cut timeline.
    src, ss, dur = qc.timeline.segment(clip_dir, clip)

    if render_text.style_of(clip) != "default":
        import build_bars
        return build_bars.build(clip_dir, dry_run=dry_run,
                                ctx=(clip, src, ss, dur))
    style = render_text.load_yaml(render_text.template_path(clip))

    # Two video-track modes:
    #   LIVE  : the trimmed SOURCE footage IS the video track (audio + video
    #           from the same input). Optional video_bg.crop {x,y,w,h} (source
    #           px, chosen 16:9) reframes the reciter to one side; if absent the
    #           footage is cover-scaled to 1920x1080. No unsharp (real footage).
    #   STATIC: a scene_image still IS the video track (reciter baked in); the
    #           source is used for AUDIO ONLY. Optional scene_crop reframes the
    #           still; a deep upscale from a 720p still gets mild unsharp.
    vb = clip.get("video_bg") or {}
    live = str(vb.get("mode", "")).lower() == "live"
    if live:
        scene_png = None
        vc = vb.get("crop") or {}
        if vc:
            # crop the chosen 16:9 window, then scale to exact 1920x1080
            bg_src = (f"[0:v]crop={vc['w']}:{vc['h']}:{vc['x']}:{vc['y']},"
                      "scale=1920:1080:flags=lanczos,")
        else:
            # cover-scale (no distortion): fill 1920x1080, centre-crop overflow
            bg_src = ("[0:v]scale=1920:1080:flags=lanczos:"
                      "force_original_aspect_ratio=increase,crop=1920:1080,")
    else:
        scene_rel = clip.get("scene_image") or clip["reciter"]["photo"]
        scene_png = os.path.join(ROOT, scene_rel)
        assert os.path.exists(scene_png), f"scene image not found: {scene_png}"
        sc = clip.get("scene_crop") or {}
        if sc:
            crop_f = f"crop={sc['w']}:{sc['h']}:{sc['x']}:{sc['y']},"
            sharp_f = "unsharp=5:5:0.4,"
        else:
            crop_f, sharp_f = "", ""
        bg_src = f"[1:v]{crop_f}scale=1920:1080:flags=lanczos,{sharp_f}"

    phrases = clip["phrases"]
    assert len(phrases) >= 1, "clip.yaml has no phrases"

    # ---- (re)build overlays ----
    ov = render_text.build(clip_dir)
    grd_png = os.path.join(ov, "grade_overlay.png")
    phrase_pngs = [os.path.join(ov, f"phrase{i + 1}.png")
                   for i in range(len(phrases))]

    # ---- text fade schedule (dissolve = overlapping fade-out / fade-in) ----
    # The default style is the OVERLAPPING case of the shared scheduler: every
    # card fades with the same duration, later cards come up early enough to
    # straddle the outgoing card's fade-out -> fixed-position dissolve. (The
    # bars style drives the same function with sequential=True + wipes.)
    half = CROSSFADE
    sched, _ = qc.timeline.schedule(phrases, dur, crossfade_s=half)
    fades = [(t_in, t_out) for _, t_in, _, t_out, _ in sched]

    # ---- audio: two-pass loudnorm ----
    st = measure_loudness(src, ss, dur)
    ln = loudnorm_filter(st, LUFS, TP, LRA)
    afade = afade_filter(dur, FADE_IN_A, FADE_OUT_A)

    # ---- filtergraph ----
    # LIVE  : [0]=source (audio + video bg)  [1]=grade  [2..]=phrase overlays
    # STATIC: [0]=source (audio only) [1]=scene still [2]=grade [3..]=phrases
    grade_idx = 1 if live else 2      # ffmpeg input index of grade_overlay
    ph_base = 2 if live else 3        # input index of phrase1
    parts = [
        f"{bg_src}setsar=1,fps=30,format=rgba[bg];"
        f"[{grade_idx}:v]format=rgba[grd];[bg][grd]overlay=0:0:format=auto[b2]"
    ]
    base = "b2"
    for i, (f_in, f_out) in enumerate(fades):
        t_lbl = f"t{i + 1}"
        parts.append(
            f";[{ph_base + i}:v]format=rgba,"
            f"fade=t=in:st={f_in:.2f}:d={half}:alpha=1,"
            f"fade=t=out:st={f_out:.2f}:d={half}:alpha=1[{t_lbl}]"
        )
        if i < len(fades) - 1:
            nxt = f"b{3 + i}"
            parts.append(f";[{base}][{t_lbl}]overlay=0:0:format=auto[{nxt}]")
            base = nxt
        else:
            parts.append(
                f";[{base}][{t_lbl}]overlay=0:0:format=auto,"
                # whole-frame fade from/to black (matches refs; same as audio)
                f"fade=t=in:st=0:d={FADE_IN_A},"
                f"fade=t=out:st={dur - FADE_OUT_A:.3f}:d={FADE_OUT_A},"
                "format=yuv420p[vout]"
            )
    parts.append(f";[0:a]{ln},{afade}[aout]")
    fc = "".join(parts)

    out_dir = os.path.join(clip_dir, "output")
    if not dry_run:
        os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "final.mp4")

    cmd = [
        FFMPEG, "-y", "-hide_banner",
        "-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", src,
    ]
    if not live:   # STATIC mode: the scene still is a separate looped input
        cmd += ["-framerate", "30", "-loop", "1", "-i", scene_png]
    cmd += ["-framerate", "30", "-loop", "1", "-i", grd_png]
    for png in phrase_pngs:
        cmd += ["-framerate", "30", "-loop", "1", "-i", png]
    cmd += [
        "-filter_complex", fc,
        "-map", "[vout]", "-map", "[aout]",
        "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-crf", "18", "-preset", "slow",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        out_path,
    ]
    if dry_run:
        emit_dry_run(cmd, fc)
        return out_path
    p = run(cmd)
    if p.returncode != 0:
        sys.exit("ffmpeg failed")
    print("\nRENDER OK ->", out_path)
    print(f"  trim {ss:.3f}s +{dur:.3f}s | "
          f"video bg: {'LIVE footage' if live else 'STATIC still'}")
    print("  " + " | ".join(f"P{i + 1} in@{f_in:.2f} out@{f_out:.2f}"
                            for i, (f_in, f_out) in enumerate(fades)))
    return out_path


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv[1:]
    clip_dir = args[0] if args else os.path.join(ROOT, "clips", "at-tawbah-51-51")
    build(clip_dir, dry_run=dry)
