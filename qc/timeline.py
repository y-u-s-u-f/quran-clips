"""
qc.timeline -- clip time parsing, the segment.cuts splice, and the caption
transition schedule.

`segment()` is the one entry point a style driver needs: it resolves a clip to
the (src, ss, dur) its ffmpeg pipeline should trim, having first spliced out any
segment.cuts and remapped the phrase times to the cut timeline.
"""
import os
import sys

from qc.proc import FFMPEG, run


def hms(t):
    """'HH:MM:SS.mmm' -> seconds (float)."""
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def source_link(clip_dir, clip):
    """Return the clip's `work/source.mp4`, creating it if it does not exist.

    Every shipped clip carries this as a RELATIVE symlink into the shared
    `sources/` cache -- the video is never copied into a clip folder. Nothing
    used to create it, so `qc render` died instantly on every freshly authored
    clip ("Error opening input .../work/source.mp4", or for bars the less
    obvious "ffmpeg bar-colour sampling failed", since bar-colour sampling is
    the first thing to open it). Four clips in a row hit that and hand-made the
    link, so it is created here instead, at the one place both style drivers
    resolve the source through.

    The target is derived from clip.yaml's `source_url`, which is where the
    video id lives; a clip whose source is not in the cache is a `qc source
    add` away, and we say so rather than failing inside ffmpeg.
    """
    link = os.path.join(clip_dir, "work", "source.mp4")
    if os.path.exists(link):
        return link
    url = str(clip.get("source_url") or "")
    vid = url.rsplit("v=", 1)[-1].split("&")[0].strip() if "v=" in url else ""
    if not vid:
        sys.exit("no work/source.mp4 and clip.yaml has no usable source_url "
                 "(%r) to derive it from" % url)
    target = os.path.join("..", "..", "..", "sources", "%s.mp4" % vid)
    cached = os.path.join(os.path.dirname(os.path.abspath(clip_dir)),
                          "..", "sources", "%s.mp4" % vid)
    if not os.path.exists(os.path.normpath(cached)):
        sys.exit("source %s.mp4 is not in sources/ -- run `qc source add %s`"
                 % (vid, url))
    os.makedirs(os.path.join(clip_dir, "work"), exist_ok=True)
    if os.path.islink(link):
        os.unlink(link)                      # dangling link: repoint it
    os.symlink(target, link)
    return link


# ---------------------------------------------------------------------------
# segment.cuts
# ---------------------------------------------------------------------------
def segment(clip_dir, clip):
    """Resolve a loaded clip to the (src, ss, dur) to trim, applying any
    segment.cuts. MUTATES clip["phrases"] t0/t1 onto the cut timeline, so call
    it exactly once per loaded clip dict."""
    seg = clip["segment"]
    ss = hms(seg["start"])
    dur = round(hms(seg["end"]) - ss, 3)
    src = source_link(clip_dir, clip)
    return apply_cuts(clip_dir, clip, src, ss, dur)


def apply_cuts(clip_dir, clip, src, ss, dur):
    """Optional mid-segment cuts (remove dead air, e.g. the breath between
    ayat).

    segment.cuts: list of {start,end} in CLIP-RELATIVE seconds (relative to
    segment.start, in the UNCUT timeline; cuts must fall in true silence
    between phrases). We splice the kept sub-intervals into
    work/source_seg.mp4 (re-encoded, audio+video) and then run the normal
    single-trim pipeline over it (ss=0). Phrase t0/t1 are shifted earlier by
    the length of every cut that precedes them. clip.yaml keeps the UNCUT
    phrase times + the cuts list; this remap is applied only here at render.

    Returns the (possibly rewritten) (src, ss, dur).
    """
    cuts = sorted((float(c["start"]), float(c["end"]))
                  for c in (clip["segment"].get("cuts") or []))
    if not cuts:
        return src, ss, dur

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
    return src, ss, dur


# ---------------------------------------------------------------------------
# caption transition schedule
# ---------------------------------------------------------------------------
# Both styles place their caption fades from the same three magic offsets,
# which were tuned once on the reference reels and must not drift:
#
#   0.05  the FIRST card comes up a hair before its own first word (clamped
#         to 0), so the frame is never empty on the word onset;
#   0.24  a LATER card in overlapping mode comes up early enough that its
#         fade-in straddles the outgoing card's fade-out -> a fixed-position
#         dissolve rather than a blink;
#   0.15  the LAST card starts leaving slightly before its last word ends, so
#         it is gone into the tail fade rather than cut off by it;
#   0.02  a non-last card in overlapping mode holds a beat past its last word.
#
# The default style only ever wanted the overlapping variant with one uniform
# crossfade duration; the bars style added `sequential` (never two captions on
# screen at once), the gap fitting that serialises a changeover into the pause
# the recitation leaves, `wipe_out_anchor`, and hard-cut detection. This is the
# superset -- the default style's schedule is `sequential=False` with
# kinds=["crossfade"] * n.
def schedule(phrases, dur, fps=30, kinds=None, crossfade_s=0.45,
             wipe_in_s=None, wipe_out_s=None, sequential=False,
             min_fade_s=0.0, wipe_out_anchor="start"):
    """Return (sched, cuts).

    sched: one (kind, t_in, d_in, t_out, d_out) per phrase, in seconds.
    cuts : indices i (1-based, into the phrase list) whose changeover had no
           room left to fade at all and is therefore a hard cut.
    """
    n_ph = len(phrases)
    if kinds is None:
        kinds = ["crossfade"] * n_ph
    xf = float(crossfade_s)
    wipe_in_s = xf if wipe_in_s is None else float(wipe_in_s)
    wipe_out_s = xf if wipe_out_s is None else float(wipe_out_s)
    minf = float(min_fade_s)

    def snap(t):
        """On the frame grid, so a fade-out's last frame and the next fade-in's
        first frame cannot straddle the same frame through rounding."""
        return round(t * fps) / float(fps)

    sched = []
    for i, ph in enumerate(phrases):
        d_in = wipe_in_s if kinds[i] == "wipe" else xf
        d_out = wipe_out_s if kinds[i] == "wipe" else xf
        if sequential:
            t_in = max(0.0, ph["t0"] - 0.05) if i == 0 else ph["t0"] - d_in
            t_out = (min(dur - d_out, ph["t1"] - 0.15)
                     if i == n_ph - 1 else ph["t1"])
        else:
            t_in = max(0.0, ph["t0"] - 0.05) if i == 0 else ph["t0"] - 0.24
            t_out = (min(dur - d_out, ph["t1"] - 0.15)
                     if i == n_ph - 1 else ph["t1"] + 0.02)
        sched.append([kinds[i], t_in, d_in, t_out, d_out])

    cuts = []
    if sequential:
        guard = 0.5 / fps          # outgoing is gone half a frame early, so no
        for i in range(n_ph - 1):  # single frame can carry both captions
            t0n, t1c = float(phrases[i + 1]["t0"]), float(phrases[i]["t1"])
            gap = t0n - t1c
            d_out, d_in = sched[i][4], sched[i + 1][2]
            nom_out = wipe_out_s if kinds[i] == "wipe" else xf
            # `wipe_out_anchor: end` -- a wipe-out is EXEMPT from the shrink so
            # that it mirrors its wipe-in exactly (same duration, and since both
            # sweeps cover the same `trav`, the same front speed). It is placed
            # by subtracting its full duration from `end`, i.e. it starts BEFORE
            # t1 and eats the outgoing caption's tail, rather than starting at
            # t1 and being clipped. The incoming fade-in is still fitted to the
            # gap below exactly as before, so caption N+1 does not move.
            anch = kinds[i] == "wipe" and \
                str(wipe_out_anchor).lower() == "end"
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
    return [tuple(x) for x in sched], cuts
