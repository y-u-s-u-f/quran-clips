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
import os, sys, json, subprocess, shlex

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import render_text  # noqa: E402

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"

CROSSFADE = 0.45   # style.yaml motion.crossfade_s
FADE_IN_A = 0.3    # style.yaml audio.fade_in_s
FADE_OUT_A = 0.5   # style.yaml audio.fade_out_s
LUFS = -14.0
TP = -1.0
LRA = 11.0


def hms(t):
    """'HH:MM:SS.mmm' -> seconds (float)."""
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def run(cmd, **kw):
    print("+", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(cmd, **kw)


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


def measure_loudness(src, ss, dur, lufs=LUFS, tp=TP, lra=LRA):
    """Two-pass loudnorm: first pass returns measured stats (JSON)."""
    LUFS, TP, LRA = lufs, tp, lra
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-ss", f"{ss:.3f}",
           "-t", f"{dur:.3f}", "-i", src,
           "-af", f"loudnorm=I={LUFS}:TP={TP}:LRA={LRA}:print_format=json",
           "-f", "null", "-"]
    p = run(cmd, capture_output=True, text=True)
    out = p.stderr
    start = out.rfind("{")
    end = out.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError("loudnorm pass-1 produced no JSON:\n" + out[-2000:])
    stats = json.loads(out[start:end + 1])
    print("  loudnorm pass-1:", {k: stats[k] for k in
          ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")})
    return stats


def build(clip_dir, dry_run=False):
    clip = render_text.load_yaml(os.path.join(clip_dir, "clip.yaml"))
    if render_text.style_of(clip) != "default":
        import build_bars
        return build_bars.build(clip_dir, dry_run=dry_run)
    style = render_text.load_yaml(render_text.template_path(clip))

    seg = clip["segment"]
    ss = hms(seg["start"])
    dur = round(hms(seg["end"]) - ss, 3)
    src = os.path.join(clip_dir, "work", "source.mp4")

    # ---- optional mid-segment cuts (remove dead air, e.g. the breath between
    #      ayat) ----
    # segment.cuts: list of {start,end} in CLIP-RELATIVE seconds (relative to
    # segment.start, in the UNCUT timeline; cuts must fall in true silence
    # between phrases). We splice the kept sub-intervals into
    # work/source_seg.mp4 (re-encoded, audio+video) and then run the normal
    # single-trim pipeline over it (ss=0). Phrase t0/t1 are shifted earlier by
    # the length of every cut that precedes them. clip.yaml keeps the UNCUT
    # phrase times + the cuts list; this remap is applied only here at render.
    cuts = sorted((float(c["start"]), float(c["end"]))
                  for c in (seg.get("cuts") or []))
    if cuts:
        kept, prev = [], 0.0
        for c0, c1 in cuts:
            if c0 > prev:
                kept.append((prev, c0))
            prev = c1
        if prev < dur - 1e-3:
            kept.append((prev, dur))
        vlab, alab, parts = [], [], []
        for i, (k0, k1) in enumerate(kept):
            a0, a1 = ss + k0, ss + k1
            parts.append(f"[0:v]trim=start={a0:.3f}:end={a1:.3f},"
                         f"setpts=PTS-STARTPTS[v{i}];")
            parts.append(f"[0:a]atrim=start={a0:.3f}:end={a1:.3f},"
                         f"asetpts=PTS-STARTPTS[a{i}];")
            vlab.append(f"[v{i}]")
            alab.append(f"[a{i}]")
        n = len(kept)
        parts.append("".join(vlab) + f"concat=n={n}:v=1:a=0[vseg];")
        parts.append("".join(alab) + f"concat=n={n}:v=0:a=1[aseg]")
        seg_path = os.path.join(clip_dir, "work", "source_seg.mp4")
        scmd = [FFMPEG, "-y", "-hide_banner", "-i", src,
                "-filter_complex", "".join(parts),
                "-map", "[vseg]", "-map", "[aseg]",
                "-c:v", "libx264", "-crf", "16", "-preset", "medium",
                "-pix_fmt", "yuv420p", "-r", "30",
                "-c:a", "aac", "-b:a", "256k", seg_path]
        if run(scmd).returncode != 0:
            sys.exit("ffmpeg splice (segment.cuts) failed")
        cut_total = sum(c1 - c0 for c0, c1 in cuts)

        def _remap(t):
            return round(t - sum(c1 - c0 for c0, c1 in cuts
                                 if c1 <= t + 1e-6), 3)
        for ph in clip["phrases"]:
            ph["t0"], ph["t1"] = _remap(ph["t0"]), _remap(ph["t1"])
        src, ss, dur = seg_path, 0.0, round(dur - cut_total, 3)
        print(f"  spliced {n} kept interval(s) -> source_seg.mp4 | removed "
              f"{cut_total:.2f}s | new dur {dur:.3f}s")

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
    # Same schedule as the original fixed 2-card template, applied per card:
    #   first card fades in just before its words (t0-0.05, clamped to 0);
    #   later cards fade in EARLY (t0-0.24) so the incoming fade overlaps the
    #   outgoing card's fade-out (t1+0.02) -> fixed-position dissolve;
    #   last card fades out early (t1-0.15) and never past dur-CROSSFADE.
    half = CROSSFADE
    fades = []                      # (in_st, out_st) per phrase
    for i, ph in enumerate(phrases):
        f_in = max(0.0, ph["t0"] - 0.05) if i == 0 else ph["t0"] - 0.24
        f_out = (min(dur - half, ph["t1"] - 0.15)
                 if i == len(phrases) - 1 else ph["t1"] + 0.02)
        fades.append((f_in, f_out))

    # ---- audio: two-pass loudnorm ----
    st = measure_loudness(src, ss, dur)
    ln = (f"loudnorm=I={LUFS}:TP={TP}:LRA={LRA}:"
          f"measured_I={st['input_i']}:measured_TP={st['input_tp']}:"
          f"measured_LRA={st['input_lra']}:measured_thresh={st['input_thresh']}:"
          f"offset={st['target_offset']}:linear=true:print_format=summary")
    afade = (f"afade=t=in:st=0:d={FADE_IN_A},"
             f"afade=t=out:st={dur - FADE_OUT_A:.3f}:d={FADE_OUT_A}")

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
