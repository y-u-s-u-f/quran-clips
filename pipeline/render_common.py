"""pipeline/render_common.py -- what both styles DELIVER with.

The look lives in the renderer that owns it (invariant 4): fonts, colours,
pill geometry and every measured constant behind them stay in render_text.py
and render_bars.py. This is the other half -- the FILE every style hands to
publish.py (1080p30 H.264 from VideoToolbox, AAC 192k, two-pass loudnorm to
-14 LUFS, one pair of head and tail fades) and the plumbing both of them
build with: the source reframe, the ink helpers.

Imported by render_text.py, render_bars.py and fx.py; not a standalone CLI.
"""
import json
import os
import subprocess

from PIL import Image, ImageDraw

from generate import FFMPEG

FPS = 30
# One pair for picture and sound: the frame fades from/to black exactly as
# the audio fades in and out (all three reference reels).
FADE_IN_S, FADE_OUT_S = 0.3, 0.5
AUDIO = {"lufs": -14.0, "tp": -1.0, "lra": 11.0, "bitrate": "192k"}

# Every style's graph is the input spec of tools/render_gpu
# (pipeline/gpu/render_gpu.swift), which renders it on Metal;
# tests/gpu_parity.py pins its frames.
_HERE = os.path.dirname(os.path.abspath(__file__))
GPU_SRC = os.path.join(_HERE, "gpu", "render_gpu.swift")
GPU_BIN = os.path.join(os.path.dirname(_HERE), "tools", "render_gpu")
# VideoToolbox H.264 quality for the picture, per style: the lowest whose
# PSNR matches x264 crf 18 / faster on the same frames, the delivery
# quality this pipeline is held to. Measured against each reel's lossless
# frames (PSNR-Y dB / MB):
#   bars --vertical  hujurat-4-5: x264 48.87 / 8.8; VT 0.65 47.83 / 7.5,
#                    0.67 48.59 / 9.0, 0.68 48.98 / 9.8, 0.70 49.58 / 11.1
#   horizontal       hujurat-4-5: x264 48.47 / 9.7; VT 0.70 47.15 / 8.8,
#                    0.72 47.76 / 10.3, 0.74 48.30 / 11.5 (0.75 is the same
#                    stream), 0.76 48.65 / 12.4; ahzab-56-56: x264 48.28 /
#                    10.1, VT 0.74 48.06 / 12.4, 0.76 48.41 / 13.3
#   ... --vertical   hujurat-4-5: x264 51.35 / 3.6; VT 0.74 51.59 / 5.7, 0.76
#                    52.02 / 6.1; ahzab-56-56: x264 51.47 / 3.6, VT 0.74 51.42
#                    / 5.8, 0.76 51.87 / 6.3
#   vertical         hujurat-4-5: x264 45.06 / 16.6; VT 0.72 44.81 / 23.5,
#                    0.74 45.45 / 26.8, 0.76 45.92 / 29.0; ahzab-56-56 on a
#                    hand-set 9:16 crop: x264 48.09 / 11.2, VT 0.74 47.99 /
#                    13.9, 0.76 48.38 / 15.0
# The text styles are mostly static, and VideoToolbox spends its bits on them
# less well than x264: matching x264 there costs 25-75% more file.
VT_QUALITY = {"bars": 0.68, "vertical": 0.76, "horizontal": 0.76}

# `--vertical` delivery: a landscape picture PORTRAIT_W wide on a 1080x1920
# canvas, black above and below. PORTRAIT_PAD is appended inside the style's
# own graph before format=yuv420p, so the portrait file is the SAME single
# encode -- letterboxing the finished mp4 afterwards (letterbox.py) costs a
# whole second lossy generation. It only pads: both landscape styles compose
# a portrait render at PORTRAIT_W already (render_bars.geometry,
# render_text.geometry).
# letterbox.py keeps its own copy of the scale+pad: it stays standalone and
# must not import Pillow through this module.
PORTRAIT_W, PORTRAIT_H = 1080, 1920
PORTRAIT_PAD = "pad=%d:%d:0:(oh-ih)/2:black" % (PORTRAIT_W, PORTRAIT_H)

# ---------------------------------------------------------------------------
# type + ink
# ---------------------------------------------------------------------------
PROBE = ImageDraw.Draw(Image.new("L", (1, 1)))


def norm_ar(s, strip):
    """Caption text with the renderer's own display-strip set removed. Each
    style passes its own: the marks a face cannot draw differ per face."""
    return "".join(c for c in s if c not in strip)


def source_chain(crop, W, H):
    """[0:v] -> exactly WxH of footage. `crop` is the authored window in SOURCE
    pixels (pipeline/crop.py solves it); without one the footage is
    cover-scaled, never distorted."""
    if crop:
        return ("crop=%d:%d:%d:%d,scale=%d:%d:flags=lanczos"
                % (crop["w"], crop["h"], crop["x"], crop["y"], W, H))
    return ("scale=%d:%d:flags=lanczos:force_original_aspect_ratio=increase,"
            "crop=%d:%d" % (W, H, W, H))


def fit_pt(nominal_pt, min_pt, max_width, widest):
    """One shared point size for every phrase (a caption swap never changes
    the type size), shrunk only far enough that the WIDEST line fits. Never
    scaled UP, never below min_pt."""
    return max(int(min_pt), int(nominal_pt * min(1.0, max_width / widest)))


def trim_to_ink(card):
    """A full-canvas card cropped to its non-transparent pixels -> (img, x, y).

    The compositor pays for the whole overlay every frame a card is up, and
    what is cropped away is (0,0,0,0), which `overlay` composites to nothing.
    The box is snapped OUTWARD to even coordinates so a card stays on the 2x2
    chroma grid if it is ever blended in yuv420. Both renderers composite onto
    rgba today, where `format=auto` blends in RGB and the snap changes no
    pixel; the boxes are pinned by the bars golden, so it stays.

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
            % (FADE_IN_S, dur - FADE_OUT_S, FADE_OUT_S))


# ---------------------------------------------------------------------------
# the delivery encode
# ---------------------------------------------------------------------------

def encode(in_argv, filter_complex, dur, out, meta, tmp, style):
    """The style's finished graph out to `out`: `in_argv` is the input list
    its build_graph returned, and the graph ends on [vout] + [aout].
    tools/render_gpu renders [vout] to a picture-only VideoToolbox H.264,
    then `mux` adds the sound, the colour tags and the mp4 tags. A missing
    or stale binary is an install problem, never a fallback."""
    if not os.access(GPU_BIN, os.X_OK):
        raise SystemExit("tools/render_gpu missing: run ./install.sh")
    if os.path.getmtime(GPU_SRC) > os.path.getmtime(GPU_BIN):
        raise SystemExit("tools/render_gpu is older than %s: run ./install.sh"
                         % os.path.relpath(GPU_SRC))
    plan_path = os.path.join(tmp, "gpu-plan.json")
    video = os.path.join(tmp, "gpu-video.mov")
    with open(plan_path, "w") as f:
        json.dump({"in_argv": in_argv, "fc": filter_complex, "dur": dur,
                   "vt_quality": VT_QUALITY[style]}, f)
    p = subprocess.run([GPU_BIN, plan_path, video], stdout=subprocess.PIPE,
                       text=True)
    if p.returncode != 0:
        raise SystemExit("render_gpu failed")
    mux(video, in_argv, filter_complex, dur, out, meta, json.loads(p.stdout))


def mux(video, in_argv, filter_complex, dur, out, meta, tags):
    """The delivered file: `video` stream-copied beside the graph's
    [0:a] -> [aout] chain, run on its first input. `meta` ({title, comment,
    artist}) is written here: publish.py captions a reel from these tags
    alone. VideoToolbox writes no VUI, so `tags` (render_gpu's report of the
    last frame) go into the SPS the way an ffmpeg encode tags the same frame:
    video_format 5, the range flag, the colour description, the sample
    aspect ratio."""
    first = in_argv[:in_argv.index("-i") + 2]
    audio = next(c for c in filter_complex.split(";") if c.startswith("[0:a]"))
    vui = ("h264_metadata=video_format=5:video_full_range_flag=%d:"
           "colour_primaries=%d:transfer_characteristics=%d:"
           "matrix_coefficients=%d:sample_aspect_ratio=%d/%d"
           % ((tags["color_range"] == 1, tags["color_primaries"],
               tags["color_trc"], tags["colorspace"]) + tuple(tags["sar"])))
    cmd = [FFMPEG, "-y", "-hide_banner"] + first
    cmd += ["-i", video, "-filter_complex", audio, "-map", "1:v", "-map",
            "[aout]", "-t", "%.3f" % dur, "-c:v", "copy", "-bsf:v", vui,
            "-c:a", "aac", "-b:a", AUDIO["bitrate"]]
    for k, v in (meta or {}).items():
        cmd += ["-metadata", "%s=%s" % (k, v)]
    if subprocess.run(cmd + ["-movflags", "+faststart", out]).returncode:
        raise SystemExit("ffmpeg mux failed")
