"""pipeline/render_common.py -- what both styles DELIVER with.

The look lives in the renderer that owns it (invariant 4): fonts, colours,
pill geometry and every measured constant behind them stay in render_text.py
and render_bars.py. This is the other half -- the FILE every style hands to
publish.py (1080p30 h264 crf 18, AAC 192k, two-pass loudnorm to -14 LUFS, one
pair of head and tail fades) and the ink helpers both of them measure with.

Imported by render_text.py and render_bars.py; not a standalone CLI.
"""
import json
import os
import subprocess

from PIL import Image, ImageDraw

FFMPEG = os.environ.get("QC_FFMPEG") or "ffmpeg"

FPS = 30
VIDEO_FADE_IN_S, VIDEO_FADE_OUT_S = 0.3, 0.5
AUDIO = {"lufs": -14.0, "tp": -1.0, "lra": 11.0,
         "fade_in_s": 0.3, "fade_out_s": 0.5}
ENCODE = {"crf": 18, "preset": "slow", "audio_bitrate": "192k"}

# ---------------------------------------------------------------------------
# type + ink
# ---------------------------------------------------------------------------
PROBE = ImageDraw.Draw(Image.new("L", (1, 1)))


def fit_pt(nominal_pt, min_pt, max_width, widest):
    """One shared point size for every phrase (a caption swap never changes
    the type size), shrunk only far enough that the WIDEST line fits. Never
    scaled UP, never below min_pt."""
    return max(int(min_pt), int(nominal_pt * min(1.0, max_width / widest)))


def trim_to_ink(card):
    """A full-canvas card cropped to its non-transparent pixels -> (img, x, y).

    The compositor pays for the whole overlay every frame a card is up, and
    what is cropped away is (0,0,0,0), which `overlay` composites to nothing.
    The box is snapped OUTWARD to even coordinates because `overlay` blends in
    yuv420 by default: on an odd edge the overlay's 2x2 chroma/alpha blocks
    would straddle a different grid than the full-canvas card's and the result
    would shift.

    Worth 33.8s -> 10.5s CPU and 1618 -> 633 MB peak RSS on the caption stage
    of a 6-card 1080p `vertical` reel at 11%, measured against full-canvas
    overlays with the same gates build_graph adds. `bars` pays for a caption
    card THREE times -- the composite, the barglow accumulator and the
    textglow accumulator -- and its ink is ~1210x480 against a 1920x1080
    canvas: 7.7s CPU and ~280 MB RSS per card, linear in card count, measured
    by differencing a 1-card against a 4-card render."""
    box = card.getbbox()
    if box is None:                       # an empty card composites to nothing
        return card, 0, 0
    x0, y0 = box[0] - box[0] % 2, box[1] - box[1] % 2
    x1 = min(card.width, box[2] + box[2] % 2)
    y1 = min(card.height, box[3] + box[3] % 2)
    return card.crop((x0, y0, x1, y1)), x0, y0


# ---------------------------------------------------------------------------
# audio (two-pass loudnorm + fades)
# ---------------------------------------------------------------------------

def measure_loudness(src, dur):
    # -vn: without it the null muxer still pulls the video stream, so pass 1
    # decodes the whole clip's picture to measure its audio (measured 2.07s ->
    # 0.60s CPU on a 23s 1080p source; the JSON is byte-identical).
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-t", "%.3f" % dur, "-i", src,
           "-vn", "-af", "loudnorm=I=%s:TP=%s:LRA=%s:print_format=json"
           % (AUDIO["lufs"], AUDIO["tp"], AUDIO["lra"]),
           "-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = p.stderr
    start, end = out.rfind("{"), out.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError("loudnorm pass-1 produced no JSON:\n" + out[-2000:])
    return json.loads(out[start:end + 1])


def loudnorm_filter(st):
    return ("loudnorm=I=%s:TP=%s:LRA=%s:measured_I=%s:measured_TP=%s:"
            "measured_LRA=%s:measured_thresh=%s:offset=%s:linear=true:"
            "print_format=summary"
            % (AUDIO["lufs"], AUDIO["tp"], AUDIO["lra"], st["input_i"],
               st["input_tp"], st["input_lra"], st["input_thresh"],
               st["target_offset"]))


def audio_fades(dur):
    return ("afade=t=in:st=0:d=%s,afade=t=out:st=%.3f:d=%s"
            % (AUDIO["fade_in_s"], dur - AUDIO["fade_out_s"],
               AUDIO["fade_out_s"]))


# ---------------------------------------------------------------------------
# the delivery encode
# ---------------------------------------------------------------------------

def encode(in_argv, filter_complex, dur, out):
    """Run the style's finished graph out to `out`. `in_argv` is the input
    list its build_graph returned, and the graph must end on [vout] + [aout]."""
    cmd = [FFMPEG, "-y", "-hide_banner"] + in_argv
    cmd += ["-filter_complex", filter_complex, "-map", "[vout]", "-map",
            "[aout]", "-t", "%.3f" % dur,
            "-c:v", "libx264", "-crf", str(ENCODE["crf"]),
            "-preset", ENCODE["preset"], "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-c:a", "aac", "-b:a", ENCODE["audio_bitrate"],
            "-movflags", "+faststart", out]
    if subprocess.run(cmd).returncode != 0:
        raise SystemExit("ffmpeg render failed")
