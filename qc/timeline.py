"""
qc.timeline -- clip time parsing.

(The transition scheduler and the segment.cuts splice join this module in the
following extractions.)
"""


def hms(t):
    """'HH:MM:SS.mmm' -> seconds (float)."""
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)
