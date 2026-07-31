#!/usr/bin/env python3
"""Locate a surah boundary in a long recitation and print a trim point.

Combines the two measurements that must agree before you trust a trim:
  1. an RMS envelope at coarse-then-fine resolution, to find silence troughs
  2. the deepest trough's timing, so ASR can be aimed at the resumption

Usage:
    source /home/ubuntu/.hermes/hermes-agent/venv/bin/activate
    python3 find_trim_point.py recitation.m4a                 # scan first 120s
    python3 find_trim_point.py recitation.m4a --window 30 90  # narrow the scan

Then confirm the verse with ASR aimed just before the resumption:
    python3 -c "
    import sys; sys.path.insert(0,'/home/ubuntu/.hermes/tools/transcription')
    from transcribe import transcribe_quran
    t,m = transcribe_quran('probe.wav', window_ayahs=3)
    print(t); [print('%.3f'%x['score'], x['text'][:90]) for x in m[:3]]"

A score of 1.000 on the expected opening phrase is the confirmation. Run ASR in
terminal(background=True) -- model load plus generate exceeds 60s per segment.

After the reel is built, verify it with the sibling script, passing the trim so
the duration check compares against the post-trim span rather than the raw
source:
    python3 verify_reel.py OUT.mp4 --block-top 842 --block-height 235 \
        --subs <tmp_dir>/subs.ass --source SRC.m4a --trim-start 43.5
"""
import argparse
import subprocess
import sys
import tempfile
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qc import env as _env  # noqa: E402

FFMPEG = _env.require("ffmpeg", "cut an ASR probe at the trim point")


def to_wav(src, dst):
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-i", src, "-ac", "1", "-ar", "16000", dst],
        check=True,
    )


def envelope(wav, start, end, hop):
    import soundfile as sf
    import numpy as np

    a, sr = sf.read(wav)
    a = np.asarray(a, dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    n = int(hop * sr)
    out = []
    for i in range(int(start / hop), int(min(end, len(a) / sr) / hop)):
        seg = a[i * n : (i + 1) * n]
        if len(seg) == 0:
            break
        db = 20 * np.log10(max(1e-6, float((seg**2).mean()) ** 0.5))
        out.append((i * hop, db))
    return out, len(a) / sr


def troughs(env, floor_delta=12.0, min_len=0.6):
    """Runs sitting >=floor_delta dB below the median level.

    Never hardcode a dB threshold -- recitation clips are mastered at wildly
    different levels. Derive it from the clip's own median, same principle as
    the pipeline's measure_noise_floor().
    """
    import numpy as np

    med = float(np.median([d for _, d in env]))
    thr = med - floor_delta
    runs, cur = [], None
    for t, d in env:
        if d < thr:
            cur = cur or [t, t]
            cur[1] = t
        elif cur:
            runs.append(tuple(cur))
            cur = None
    if cur:
        runs.append(tuple(cur))
    hop = env[1][0] - env[0][0] if len(env) > 1 else 0.1
    return med, thr, [r for r in runs if (r[1] - r[0]) + hop >= min_len]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--window", nargs=2, type=float, default=[0.0, 120.0])
    ap.add_argument("--lead-in", type=float, default=0.5,
                    help="seconds of breath to keep before the resumption")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="trimscan_")
    wav = os.path.join(tmp, "full.wav")
    to_wav(args.audio, wav)

    lo, hi = args.window
    coarse, dur = envelope(wav, lo, hi, 1.0)
    med, thr, runs = troughs(coarse)
    print("source duration %.3fs" % dur)
    print("median level %.1f dB -> silence threshold %.1f dB" % (med, thr))
    print("\ncoarse (1s) candidate troughs in %.0f-%.0fs:" % (lo, hi))
    for a, b in runs:
        print("  %.1f - %.1f" % (a, b))

    if not runs:
        print("\nNo trough found. Widen --window, or the boundary may be marked")
        print("only by an unvoiced takbir/aameen at full level -- fall back to")
        print("ASR probes at 15s strides to find where the surah changes.")
        return 1

    # deepest trough wins: re-scan it at 20ms to get the real edges
    import numpy as np

    def depth(r):
        seg = [d for t, d in coarse if r[0] <= t <= r[1]]
        return float(np.mean(seg))

    best = min(runs, key=depth)
    fine, _ = envelope(wav, max(0, best[0] - 2), min(dur, best[1] + 3), 0.02)
    _, fthr, fruns = troughs(fine)
    if fruns:
        a, b = max(fruns, key=lambda r: r[1] - r[0])
        print("\nfine (20ms) edges of deepest trough: %.2f - %.2f" % (a, b))
        print("  speech resumes at %.2f" % (b + 0.02))
        trim = round(max(a, b + 0.02 - args.lead_in), 2)
        print("\nTRIM POINT: %.2f" % trim)
        print("  (resumption %.2f minus %.2fs lead-in, landing mid-silence)"
              % (b + 0.02, args.lead_in))
        print("  expected output duration: %.3f" % (dur - trim))
        print("\nNow confirm the verse with ASR on a segment starting ~%.1f:" % a)
        print("  %s -y -v error -ss %.1f -t 8 -i %s -ac 1 -ar 16000 probe.wav"
              % (FFMPEG, a, args.audio))
    return 0


if __name__ == "__main__":
    sys.exit(main())
