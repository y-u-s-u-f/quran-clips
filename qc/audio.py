"""
qc.audio -- the audio chain both styles use: two-pass loudnorm + fades.

The chain is identical in the default and the bars style; only where the
numbers come from differs (build_render.py hard-codes them as module
constants, build_bars.py reads templates/bars.yaml `audio`). The filter
strings below reproduce the originals character for character, so the
floats must arrive as floats: -14.0 formats as "-14.0", -14 as "-14".
"""
import json

from qc.proc import FFMPEG, run

# default-style targets (style.yaml audio.*, mirrored as build_render constants)
LUFS = -14.0
TP = -1.0
LRA = 11.0


def measure_loudness(src, ss, dur, lufs=LUFS, tp=TP, lra=LRA):
    """Two-pass loudnorm: first pass returns measured stats (JSON)."""
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-ss", f"{ss:.3f}",
           "-t", f"{dur:.3f}", "-i", src,
           "-af", f"loudnorm=I={lufs}:TP={tp}:LRA={lra}:print_format=json",
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


def loudnorm_filter(st, lufs=LUFS, tp=TP, lra=LRA):
    """Pass-2 loudnorm filter string from the pass-1 stats dict."""
    return (f"loudnorm=I={lufs}:TP={tp}:LRA={lra}:"
            f"measured_I={st['input_i']}:measured_TP={st['input_tp']}:"
            f"measured_LRA={st['input_lra']}:measured_thresh={st['input_thresh']}:"
            f"offset={st['target_offset']}:linear=true:print_format=summary")


def afade_filter(dur, fade_in_s, fade_out_s):
    """Fade in from silence at t=0, fade out to land exactly on `dur`."""
    return (f"afade=t=in:st=0:d={fade_in_s},"
            f"afade=t=out:st={dur - fade_out_s:.3f}:d={fade_out_s}")
