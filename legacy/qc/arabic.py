"""
qc.arabic -- house-style normalisation of the Quranic text.

Both styles drop the same two small tajweed ANNOTATION marks before laying
anything out. They are reading aids, not letters and not harakat, and each one
misbehaves in a different way:

  U+06DF SMALL HIGH ROUNDED ZERO -- the silent-letter aid over the alif of
      واو الجماعة (ءَامَنُوا۟). KFGQPC Uthmanic Hafs v22 cannot mark-attach it
      through RAQM and falls back to an ugly U+25CC dotted-circle cluster;
      AM_Thulth has no such codepoint at all and renders it ZERO WIDTH (not as
      tofu), which would silently corrupt every width measurement.
  U+06ED SMALL LOW MEEM -- the iqlab/ikhfa aid (قَوْلًۭا سَدِيدًۭا), i.e. the
      tiny floating "meem" that reads as a random symbol over the account's
      clean style.

The consonantal text and every pronunciation diacritic (harakat, tanwin,
shadda, madda, superscript alif, small waw/yeh silah) are untouched.
"""

STRIP_MARKS = {"۟", "ۭ"}

# Combining marks, for the tashkeel-stripped "letter body" measurement the bars
# style needs (AM_Thulth's marks overshoot the em box by ~0.23em, so a pill
# sized off the full ink bbox would sit wrong).
TASHKEEL = set(
    "ًٌٍَُِّْٕٓٔ"
    "ٰۖۗۘۙۚۛۜ۟۠ۡ"
    "ۣ۪ۭۢۤۧۨ۫۬"
)


def norm_ar(s):
    """Drop the annotation marks. Applied to every `ar` / `ar1` / `ar2`."""
    return "".join(c for c in s if c not in STRIP_MARKS)


def strip_tashkeel(s):
    """Letter bodies only -- no combining marks at all."""
    return "".join(c for c in s if c not in TASHKEEL)
