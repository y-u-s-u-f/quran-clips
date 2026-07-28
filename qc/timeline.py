"""
qc.timeline -- clip time parsing and the caption transition schedule.

(The segment.cuts splice joins this module in the following extraction.)
"""


def hms(t):
    """'HH:MM:SS.mmm' -> seconds (float)."""
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


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
