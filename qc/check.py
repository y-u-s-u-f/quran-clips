"""
qc.check -- the committed assertions that replace the QA agent.

Everything here is CHEAP and CONFIG-TIME: it loads clip.yaml and the style
template, measures text with Pillow, and runs the real scheduler. It never
looks at a rendered frame, and the only ffmpeg it runs is one audio extract
for the `head` check (skipped when the source is not in sources/), so the
whole pass is a second or two per clip and can be run before a 6-minute render
rather than after it.

The ten checks exist because each one is a failure this pipeline has actually
shipped, and every one of them is silent at render time:

  schema      a typo'd key (`ar_2:` for `ar2:`) is not an error to yaml.
              clip.yaml then renders a one-line card and nothing says why.
  phrases     t0/t1 out of order, overlapping, or past the segment end put a
              caption on screen over the wrong words, or off the end.
  cuts        a segment.cut inside a phrase silently eats recited words.
  font        AM_Thulth renders a codepoint it has no glyph for as ZERO WIDTH
              NOTHING -- not as tofu. A caption that is missing a letter looks
              like a caption that is slightly narrower.
  verse       the mushaf is load-bearing bytes. A plain alif where the Uthmani
              text has a wasla or a madda is invisible on screen at 96pt and
              wrong in the thing this account exists to publish.
  geometry    the bars look is a fixed type size on a fixed anchor; a caption
              that is too wide silently drags the WHOLE clip's point size down
              (shrink-to-fit is per clip, not per line).
  transitions the scheduler already detects a changeover with no room to fade
              -- but it printed that AFTER the encode, at the end of a six
              minute render, in the middle of a wall of timing output.
  english     `en:` is required by the default style and must not exist on a
              bars clip (the bars references carry no translation).
  fx          an unknown name in a clip's `fx:` map.
  head        the clip opens on the first word of the ayah it claims -- in the
              text AND in the audio. al-qamar-7-9 started 2 s early, inside
              the last word of the PREVIOUS ayah, and nothing else here could
              see it.

`qc check --output` is the other half: the handful of facts about a finished
file that are worth asserting mechanically (container geometry, letterbox
purity, loudness). It needs ffprobe/ffmpeg and takes a few seconds.

What is deliberately NOT here: anything that re-measures a constant the
renderer was given. Glow falloff, snow blob counts and heat RMS are INPUTS in
templates/bars.yaml; re-deriving them from the output frame and comparing them
to themselves is a tautology that costs minutes. The scalars derived from
those inputs are pinned by tests/test_fx_scalars.py instead, which is
milliseconds.
"""
import difflib
import os
import re
import sys
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# clip.yaml schema
#
# A map of key -> what may be under it. `None` means "anything, we do not
# police the contents"; a dict recurses; a tuple is (dict-spec, "list") for a
# list of maps. Everything not listed is REJECTED -- that is the whole point of
# the check, so adding a key to clip.yaml means adding it here too.
# ---------------------------------------------------------------------------
CROP = {"x": int, "y": int, "w": int, "h": int}

CLIP_SCHEMA = {
    "source_url": str,
    "style": str,
    "reciter": {"name_en": str, "name_ar": str, "photo": str},
    "video_bg": {"mode": str, "crop": CROP},
    "scene_image": str,
    "scene_crop": CROP,
    "text": {"center_x_frac": float, "center_y_frac": float},
    "bar_color": str,
    "fx": None,                      # validated against the fx registry
    "segment": {"start": str, "end": str,
                "cuts": [{"start": float, "end": float}]},
    "surah": int,
    "ayah_range": list,
    "phrases": [{"t0": float, "t1": float, "ayah": int,
                 "ar": str, "ar1": str, "ar2": str, "en": str}],
}

REQUIRED = ("segment", "surah", "ayah_range", "phrases")
PHRASE_REQUIRED = ("t0", "t1", "ayah")


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
class Result(object):
    """One clip's findings. `fail(check, msg)` is the only way to fail: every
    message must name the offending value and say what to do about it."""

    def __init__(self, name):
        self.name = name
        self.ran = []
        self.problems = []          # (check, message)
        self.warnings = []          # (check, message) -- advisory, still PASS

    def start(self, check):
        self.ran.append(check)

    def fail(self, check, msg):
        self.problems.append((check, msg))

    def warn(self, check, msg):
        """Advisory: worth a human glance, but not a defect. Does not fail."""
        self.warnings.append((check, msg))

    @property
    def ok(self):
        return not self.problems

    def text(self):
        out = ["%s  %s" % ("PASS" if self.ok else "FAIL", self.name)]
        by = {}
        for c, m in self.problems:
            by.setdefault(c, []).append(m)
        for c in self.ran:
            if c in by:
                for m in by[c]:
                    out.append("  FAIL %-11s %s" % (c, m))
        bw = {}
        for c, m in self.warnings:
            bw.setdefault(c, []).append(m)
        for c in self.ran:
            if c in bw:
                for m in bw[c]:
                    out.append("  warn %-11s %s" % (c, m))
        return "\n".join(out)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _cp(s):
    """A run of characters as hex codepoints, for a message that has to be
    actionable about text that looks identical on screen."""
    return " ".join("U+%04X" % ord(c) for c in s)


def _near(key, options):
    m = difflib.get_close_matches(str(key), [str(o) for o in options], 1, 0.6)
    return " -- did you mean `%s`?" % m[0] if m else ""


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _hms(t):
    return sum(float(p) * 60 ** i
               for i, p in enumerate(reversed(str(t).split(":"))))


# ---------------------------------------------------------------------------
# 1. schema
# ---------------------------------------------------------------------------
def check_schema(res, clip):
    res.start("schema")
    if not isinstance(clip, dict):
        res.fail("schema", "clip.yaml did not parse to a map")
        return

    def walk(node, spec, path):
        for k, v in node.items():
            if k not in spec:
                res.fail("schema",
                         "unknown key `%s`%s -- remove it or add it to "
                         "qc/check.py CLIP_SCHEMA. Unknown keys are IGNORED by "
                         "the renderer, so a typo here silently changes the "
                         "output" % (path + str(k), _near(k, spec)))
                continue
            want = spec[k]
            if want is None or v is None:
                continue
            if isinstance(want, dict):
                if not isinstance(v, dict):
                    res.fail("schema", "`%s` must be a map, got %s"
                             % (path + str(k), type(v).__name__))
                else:
                    walk(v, want, path + str(k) + ".")
            elif isinstance(want, list):
                if not isinstance(v, list):
                    res.fail("schema", "`%s` must be a list, got %s"
                             % (path + str(k), type(v).__name__))
                else:
                    for i, item in enumerate(v):
                        if not isinstance(item, dict):
                            res.fail("schema", "`%s[%d]` must be a map, got %s"
                                     % (path + str(k), i, type(item).__name__))
                        else:
                            walk(item, want[0], "%s%s[%d]." % (path, k, i))
            elif want is float:
                if not _num(v):
                    res.fail("schema", "`%s` must be a number, got %r"
                             % (path + str(k), v))
            elif want is int:
                if not _num(v) or int(v) != v:
                    res.fail("schema", "`%s` must be a whole number, got %r"
                             % (path + str(k), v))
            elif want is str and not isinstance(v, str):
                res.fail("schema", "`%s` must be a string, got %r"
                         % (path + str(k), v))

    walk(clip, CLIP_SCHEMA, "")

    for k in REQUIRED:
        if clip.get(k) is None:
            res.fail("schema", "missing required key `%s`" % k)
    for i, ph in enumerate(clip.get("phrases") or [], 1):
        if not isinstance(ph, dict):
            continue
        for k in PHRASE_REQUIRED:
            if ph.get(k) is None:
                res.fail("schema", "phrase %d is missing `%s`" % (i, k))
        if not (ph.get("ar") or ph.get("ar1")):
            res.fail("schema",
                     "phrase %d has no Arabic: it needs `ar:` (one line) or "
                     "`ar1:`/`ar2:` (two lines)" % i)
        if ph.get("ar") and ph.get("ar1"):
            res.fail("schema",
                     "phrase %d has BOTH `ar:` and `ar1:` -- the renderer uses "
                     "`ar1`/`ar2` and silently drops `ar`" % i)
        if ph.get("ar2") and not ph.get("ar1"):
            res.fail("schema",
                     "phrase %d has `ar2:` without `ar1:` -- `ar2` is only read "
                     "when `ar1` is present, so this line never renders" % i)


# ---------------------------------------------------------------------------
# 2. phrases
# ---------------------------------------------------------------------------
def check_phrases(res, clip, dur):
    res.start("phrases")
    phrases = clip.get("phrases") or []
    if not phrases:
        res.fail("phrases", "clip.yaml has no phrases")
        return
    prev_t1 = None
    for i, ph in enumerate(phrases, 1):
        t0, t1 = ph.get("t0"), ph.get("t1")
        if not (_num(t0) and _num(t1)):
            return                                  # schema already said so
        if t0 >= t1:
            res.fail("phrases", "P%d has t0 %.2f >= t1 %.2f -- a phrase must "
                                "have positive length" % (i, t0, t1))
        if i == 1 and t0 < 0:
            res.fail("phrases", "P1 t0 is %.2f: phrase times are relative to "
                                "segment.start and cannot be negative -- move "
                                "segment.start earlier instead" % t0)
        if prev_t1 is not None and t0 < prev_t1:
            res.fail("phrases",
                     "P%d starts at %.2f, before P%d ends at %.2f -- captions "
                     "are strictly sequential; lower P%d t1 or raise P%d t0"
                     % (i, t0, i - 1, prev_t1, i - 1, i))
        if t1 > dur + 1e-6:
            res.fail("phrases",
                     "P%d ends at %.2f, past the segment's %.2fs -- extend "
                     "segment.end or shorten the phrase" % (i, t1, dur))
        prev_t1 = t1


# ---------------------------------------------------------------------------
# 3. cuts
# ---------------------------------------------------------------------------
def check_cuts(res, clip, dur):
    res.start("cuts")
    cuts = ((clip.get("segment") or {}).get("cuts")) or []
    if not cuts:
        return
    spans = []
    for i, c in enumerate(cuts, 1):
        if not isinstance(c, dict) or not (_num(c.get("start"))
                                           and _num(c.get("end"))):
            return
        c0, c1 = float(c["start"]), float(c["end"])
        if c0 >= c1:
            res.fail("cuts", "cut %d has start %.2f >= end %.2f" % (i, c0, c1))
        if c0 < 0 or c1 > dur + 1e-6:
            res.fail("cuts", "cut %d (%.2f-%.2f) falls outside the segment "
                             "[0, %.2f]" % (i, c0, c1, dur))
        spans.append((c0, c1, i))
    spans.sort()
    for (a0, a1, ai), (b0, b1, bi) in zip(spans, spans[1:]):
        if b0 < a1:
            res.fail("cuts", "cuts %d (%.2f-%.2f) and %d (%.2f-%.2f) overlap"
                     % (ai, a0, a1, bi, b0, b1))
    for c0, c1, ci in spans:
        for j, ph in enumerate(clip.get("phrases") or [], 1):
            if not (_num(ph.get("t0")) and _num(ph.get("t1"))):
                continue
            if c0 < float(ph["t1"]) and float(ph["t0"]) < c1:
                res.fail("cuts",
                         "cut %d (%.2f-%.2f) overlaps P%d (%.2f-%.2f) -- cuts "
                         "must fall in the silence BETWEEN phrases; this one "
                         "deletes recited audio"
                         % (ci, c0, c1, j, ph["t0"], ph["t1"]))


def remap(clip, dur):
    """Phrase times + duration on the post-`segment.cuts` timeline, in pure
    python. qc.timeline.apply_cuts does this too, but only as a side effect of
    running an ffmpeg splice."""
    cuts = sorted((float(c["start"]), float(c["end"]))
                  for c in (((clip.get("segment") or {}).get("cuts")) or [])
                  if _num(c.get("start")) and _num(c.get("end")))
    if not cuts:
        return list(clip.get("phrases") or []), dur
    total = sum(c1 - c0 for c0, c1 in cuts)

    def r(t):
        return round(t - sum(c1 - c0 for c0, c1 in cuts if c1 <= t + 1e-6), 3)
    out = [dict(ph, t0=r(float(ph["t0"])), t1=r(float(ph["t1"])))
           for ph in clip.get("phrases") or []]
    return out, round(dur - total, 3)


# ---------------------------------------------------------------------------
# 4. font ink
#
# The failure mode this exists for: AM_Thulth has no .notdef box. HarfBuzz maps
# an unsupported codepoint to glyph 0 and glyph 0 in this face is EMPTY, so a
# missing letter costs the line a few pixels of width and nothing else. There
# is no tofu, no warning and no visual cue -- the caption is just quietly
# wrong. So every codepoint is rendered and asked whether it made ink.
# ---------------------------------------------------------------------------
BASE = "ب"        # a plain beh, to hang a combining mark on


def _ink_bbox(font, s, rtl=True):
    from PIL import Image, ImageDraw
    im = Image.new("L", (900, 500), 0)
    d = ImageDraw.Draw(im)
    if rtl:
        d.text((450, 350), s, font=font, fill=255, anchor="ls",
               direction="rtl", language="ar")
    else:
        d.text((450, 350), s, font=font, fill=255, anchor="ls")
    return im.getbbox()


def _renders_ink(font, ch, rtl=True):
    """Does this ONE codepoint contribute ink in this face? A combining mark is
    judged against a bare base letter: same picture => the mark drew nothing."""
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me"):
        if _ink_bbox(font, BASE, rtl) != _ink_bbox(font, BASE + ch, rtl):
            return True
        # an unchanged bbox is not proof (a mark can sit inside the base's
        # box), so fall back to comparing the two renderings pixel for pixel
        return _pixels(font, BASE, rtl) != _pixels(font, BASE + ch, rtl)
    return _ink_bbox(font, ch, rtl) is not None


def _pixels(font, s, rtl):
    from PIL import Image, ImageDraw
    im = Image.new("L", (900, 500), 0)
    d = ImageDraw.Draw(im)
    if rtl:
        d.text((450, 350), s, font=font, fill=255, anchor="ls",
               direction="rtl", language="ar")
    else:
        d.text((450, 350), s, font=font, fill=255, anchor="ls")
    return im.tobytes()


def check_font(res, clip, style_name, style, captions):
    res.start("font")
    import render_text
    from qc.fonts import truetype
    if style_name == "bars":
        fonts = [(os.path.join(ROOT, style["text"]["font"]),
                  int(style["text"]["nominal_pt"]), True,
                  [t for t, _en in captions])]
    else:
        fonts = [(render_text.AR_FONT_PATH, 72, True,
                  [t for t, _en in captions]),
                 (render_text.EN_FONT_PATH, 29, False,
                  [e for _t, e in captions if e])]
    for path, pt, rtl, texts in fonts:
        if not os.path.exists(path):
            res.fail("font", "font not found: %s" % path)
            continue
        font = truetype(path, pt)
        seen = set()
        for t in texts:
            for ch in t:
                if ch in seen or ch.isspace():
                    continue
                seen.add(ch)
                if not _renders_ink(font, ch, rtl):
                    res.fail("font",
                             "%s renders U+%04X (%s) as NOTHING -- this face "
                             "has no glyph for it and draws zero width rather "
                             "than tofu, so the caption silently loses a "
                             "letter. Remove it from the caption or add it to "
                             "qc.arabic.STRIP_MARKS."
                             % (os.path.basename(path), ord(ch),
                                unicodedata.name(ch, "unnamed")))


# ---------------------------------------------------------------------------
# 5. verse text integrity
#
# The comparison is deliberately STRICT: NFC, whitespace collapsed, and only
# the WAQF / annotation signs dropped (they are pause marks printed in the
# mushaf that no caption in this repo carries). Every letter and every
# pronunciation diacritic must match the Uthmani text exactly -- the alif
# family is NOT folded, which is the entire point: a plain alif where the
# mushaf has a wasla (U+0671) or a madda (U+0622) is exactly the error this
# check exists to catch, and qc.quran.normalize() would hide it.
#
# Repeats and ibtida' need no special case: each phrase is matched on its own
# against the ayah it declares, so a card that repeats an earlier fragment is
# still a contiguous substring of that ayah.
# ---------------------------------------------------------------------------
# U+06D6..U+06DC waqf signs, U+06DD end of ayah, U+06DE rub el hizb,
# U+06DF small high rounded zero, U+06E9 place of sajdah, U+06ED small low meem.
WAQF = re.compile("[ۖ-۟۩ۭ]")
# U+0640 TATWEEL is not a letter and carries no pronunciation: it only stretches
# the join between two letters so a caption line can be widened to match its
# neighbour's pill. It is layout, not text, so it is dropped before comparing --
# the same thing qc.quran.normalize() already does for the alignment side.
KASHIDA = re.compile("ـ+")


def strict_ar(s):
    from qc import quran as Q
    return " ".join(KASHIDA.sub("", WAQF.sub("", Q.nfc(s or ""))).split())


def check_verse(res, clip, phrase_texts):
    res.start("verse")
    from qc import quran as Q
    surah = clip.get("surah")
    if not isinstance(surah, int):
        return
    for i, (ph, text) in enumerate(zip(clip.get("phrases") or [],
                                       phrase_texts), 1):
        a = ph.get("ayah")
        if not isinstance(a, int):
            continue
        try:
            ref = strict_ar(Q.ayah(surah, a)["ar"])
        except KeyError as e:
            res.fail("verse", "P%d claims ayah %s:%s -- %s" % (i, surah, a, e))
            continue
        got = strict_ar(text)
        if got in ref:
            continue
        # Point at the first divergence: find the best-matching start in the
        # ayah and report the codepoints on both sides of it.
        best, at = 0, 0
        for st in range(len(ref)):
            n = 0
            while st + n < len(ref) and n < len(got) and ref[st + n] == got[n]:
                n += 1
            if n > best:
                best, at = n, st
        res.fail("verse",
                 "P%d is not the mushaf text of %d:%d. It matches for %d "
                 "characters, then has %r [%s] where the Uthmani text has %r "
                 "[%s]. Copy the text from `qc ayah %d:%d` -- do not retype it."
                 % (i, surah, a, best, got[best:best + 5],
                    _cp(got[best:best + 5]), ref[at + best:at + best + 5],
                    _cp(ref[at + best:at + best + 5]), surah, a))


def check_ayah_span(res, clip, phrase_texts):
    """A card may carry PART of one ayah, or a whole ayah -- never two.

    Multiple cards per ayah is normal and often necessary; one card drawn from
    two ayat is not, because a caption is read as a single verse by anyone who
    cannot see the numbering, and the card's own `ayah:` field can then only be
    half true. `qc author` produced exactly that on al-qamar-1-5 -- 54:1 split
    across two cards, its last word riding on the same card as the opening of
    54:2 -- and it had to be regrouped by hand.

    Ayah membership is not recorded anywhere but the per-phrase `ayah:` field,
    so it is established the same way check_verse establishes wording: against
    the mushaf. A straddle is unmistakable in that text -- the head of the card
    is the TAIL of one ayah and the rest is the HEAD of the next -- so that is
    what is tested for, exactly, on the same strict_ar comparison check_verse
    uses. Anything else that fails to sit inside its declared ayah is a wording
    problem and is check_verse's to report, not this one's.
    """
    res.start("ayah-span")
    from qc import quran as Q
    surah = clip.get("surah")
    if not isinstance(surah, int):
        return

    def words(n):
        try:
            return strict_ar(Q.ayah(surah, n)["ar"]).split()
        except KeyError:
            return None

    for i, (ph, text) in enumerate(zip(clip.get("phrases") or [],
                                       phrase_texts), 1):
        a = ph.get("ayah")
        if not isinstance(a, int):
            continue
        got = strict_ar(text).split()
        own = words(a)
        if not got or own is None:
            continue
        if _sublist(own, got):
            continue                    # wholly inside its declared ayah: fine
        split = None
        for x in (a - 1, a):
            first, second = words(x), words(x + 1)
            if first is None or second is None:
                continue
            for k in range(1, len(got)):
                if (k <= len(first) and len(got) - k <= len(second)
                        and got[:k] == first[len(first) - k:]
                        and got[k:] == second[:len(got) - k]):
                    split = (x, k)
                    break
            if split:
                break
        if split:
            x, k = split
            res.fail("ayah-span",
                     "P%d spans TWO ayat: its first %d word(s) are the end of "
                     "%d:%d and its last %d are the start of %d:%d (it "
                     "declares `ayah: %d`). A card may show part of one ayah "
                     "or all of it, never text from two -- split it into two "
                     "phrases at the boundary and give each its own `ayah:`."
                     % (i, k, surah, x, len(got) - k, surah, x + 1, a))


def _sublist(hay, needle):
    """Does `needle` occur as a contiguous run of whole words in `hay`?"""
    n = len(needle)
    return any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


# ---------------------------------------------------------------------------
# 6. bars geometry (pure Pillow -- no video, no ffmpeg)
# ---------------------------------------------------------------------------
# Letter-body height over pill height. The pill is a FIXED 44px, so this ratio
# moves with whichever glyphs the line happens to contain -- a line without deep
# descenders measures shorter than one with them, at identical, correct
# composition. The 2.4-2.8 band originally used here was simply the observed
# range of the only bars clip that existed (at-tawbah-128-128, 2.43-2.80); a
# second clip (al-qamar-2-4) measured 2.30-2.82 and was confirmed correct by
# eye, which showed the band was fitted to n=1 rather than to a defect.
#
# What the check is actually for: the pill must read as a highlight struck
# THROUGH the letters, not as a box behind them. That fails when the ratio gets
# SMALL. Below ~1.8 the pill approaches the letter height and reads as a box;
# an unusually large ratio is not a defect at all, just a thin strike.
INK_BAR_FAIL_MIN = 1.8            # below this the pill reads as a box
INK_BAR_WARN_MIN, INK_BAR_WARN_MAX = 2.25, 2.95    # observed across real clips
BAR_MAX_FRAC = 0.50


def check_geometry(res, clip, style):
    res.start("geometry")
    import render_bars
    lay = render_bars.layout(clip, style)
    W = int(style["canvas"]["width"])
    nominal = int(style["text"]["nominal_pt"])
    if lay["pt"] != nominal:
        res.fail("geometry",
                 "shrink-to-fit dropped the type to %dpt (nominal %dpt) "
                 "because the widest line measures %.0fpx against the %.0fpx "
                 "cap: %r. The size is shared by the WHOLE clip, so this one "
                 "line shrinks every caption -- split it instead."
                 % (lay["pt"], nominal, lay["widest"], lay["max_w"],
                    lay["widest_line"]))
    for p in lay["phrases"]:
        for ln in p["lines"]:
            w = ln["x1"] - ln["x0"]
            if w > BAR_MAX_FRAC * W + 1e-6:
                res.fail("geometry",
                         "P%d line %d: pill is %.0fpx = %.3f W, over the %.2f W "
                         "ceiling (the widest bar in either reference reel is "
                         "0.503 W). Shorten the line: %r"
                         % (p["i"], ln["n"], w, w / W, BAR_MAX_FRAC, ln["text"]))
            c = (ln["x0"] + ln["x1"]) / 2.0
            if abs(c - lay["cx"]) > 1.0:
                res.fail("geometry",
                         "P%d line %d: pill centre is x=%.1f, %.1fpx off the "
                         "configured anchor text.center_x_frac=%s (x=%d)"
                         % (p["i"], ln["n"], c, c - lay["cx"],
                            style["text"]["center_x_frac"], lay["cx"]))
            r = ln["ink_body_h"] / float(lay["bar_h"])
            if r < INK_BAR_FAIL_MIN:
                res.fail("geometry",
                         "P%d line %d: letter-body height %.0fpx over a %dpx "
                         "pill is a ratio of %.2f, under %.1f -- the pill is "
                         "reading as a box BEHIND the letters instead of a "
                         "highlight struck through them: %r"
                         % (p["i"], ln["n"], ln["ink_body_h"], lay["bar_h"], r,
                            INK_BAR_FAIL_MIN, ln["text"]))
            elif not INK_BAR_WARN_MIN <= r <= INK_BAR_WARN_MAX:
                res.warn("geometry",
                         "P%d line %d: letter-body/pill ratio %.2f is outside "
                         "the %.2f-%.2f seen in shipped clips -- probably just "
                         "this line's glyphs, worth a glance: %r"
                         % (p["i"], ln["n"], r, INK_BAR_WARN_MIN,
                            INK_BAR_WARN_MAX, ln["text"]))


# ---------------------------------------------------------------------------
# 7. transition feasibility
# ---------------------------------------------------------------------------
def check_transitions(res, clip, style_name, style, phrases, dur):
    res.start("transitions")
    from qc import timeline
    if style_name != "bars":
        return
    tr = style["transitions"]
    kinds = [str(tr["first"] if i == 0 else tr["rest"]).lower()
             for i in range(len(phrases))]
    seq = str(tr["sequential"]).lower() in ("true", "yes", "1")
    sched, cuts = timeline.schedule(
        phrases, dur, fps=int(style["meta"]["fps"]), kinds=kinds,
        crossfade_s=float(tr["crossfade_s"]),
        wipe_in_s=float(tr["wipe_in_s"]), wipe_out_s=float(tr["wipe_out_s"]),
        sequential=seq, min_fade_s=float(tr["min_fade_s"]),
        wipe_out_anchor=tr.get("wipe_out_anchor", "start"))
    minf = float(tr["min_fade_s"])
    for c in cuts:
        gap = float(phrases[c]["t0"]) - float(phrases[c - 1]["t1"])
        res.fail("transitions",
                 "P%d->P%d is a HARD CUT: the recitation leaves %.2fs between "
                 "them, and even at the transitions.min_fade_s floor the pair "
                 "of fades needs %.2fs -- P%d is still arriving when it "
                 "already has to leave, so the caption blinks. Move the swap "
                 "into a real pause (a true waqf), merge the two cards, or "
                 "lower min_fade_s (now %.2fs)."
                 % (c, c + 1, gap, 2 * minf + 1.0 / int(style["meta"]["fps"]),
                    c, minf))


# ---------------------------------------------------------------------------
# 8. english
# ---------------------------------------------------------------------------
def check_english(res, clip, style_name):
    res.start("english")
    for i, ph in enumerate(clip.get("phrases") or [], 1):
        if not isinstance(ph, dict):
            continue
        en = ph.get("en")
        if style_name == "bars":
            if en:
                res.fail("english",
                         "P%d carries `en:` but this is a bars clip -- the "
                         "reference reels are Arabic only and render_bars.py "
                         "never draws it. Delete the line." % i)
        elif not (en or "").strip():
            res.fail("english",
                     "P%d has no `en:` -- the default style draws the Sahih "
                     "International fragment under the Arabic, and an empty "
                     "one leaves a gap under the caption." % i)


# ---------------------------------------------------------------------------
# 10. the head
#
# The clip must OPEN ON THE FIRST WORD OF THE AYAH IT CLAIMS -- and it is the
# only check here that listens to the audio, because that is the only place
# the failure is visible. al-qamar-7-9 shipped a `segment.start` of 43.920,
# which sits inside نُّكُرٍ, the last word of 54:6: the caption said 54:7 and
# the first thing you heard was the end of the previous ayah. Every text-side
# check passed, because the TEXT was right; only the recording was wrong. The
# owner heard it in one play, after a six-minute render.
#
# Two assertions, cheapest first:
#
#   text   phrase 1 must begin with the first word of ayah_range[0]. Free, and
#          it catches a clip that claims an ayah it does not open with.
#   audio  the first real attack after segment.start must land on P1's t0.
#          One ffmpeg extract of the segment (plus a second of pre-roll so the
#          pause in front of the first word is visible) and the same envelope
#          the author used -- about a second per clip.
#
# The audio half is SKIPPED, not failed, when the source is not in sources/
# (which is gitignored, so a fresh checkout has none) or when the head sits in
# continuous recitation with no pause to measure against -- there, "did the
# start land in the right place" is not a question the envelope can answer.
# ---------------------------------------------------------------------------
HEAD_PRE = 1.0       # seconds of pre-roll: the pause before the first word
HEAD_LOOK = 3.0      # how far into the clip to look for the first attack
HEAD_TOL = 0.35      # how far the attack may be from P1's t0
HEAD_TIMEOUT = 90    # an Opus-in-MP4 source can wedge ffmpeg outright
# The attack test above compares the author's answer against `E.attacks` -- the
# SAME function the author used to pick `segment.start`. So when the author
# latches onto a spurious rise, this check finds the identical rise in the
# identical place and agrees with it. Both al-hijr clips that shipped broken
# passed the attack test that way:
#   al-hijr-96-99 started 1.67 s EARLY, mid-word in the previous ayah -- the
#     "pause" it keyed on was a 0.21 s hamza glottal stop INSIDE ٱلْمُسْتَهْزِءِينَ,
#     which reads as a dip (-34.6 dB) but is not a waqf.
#   al-hijr-27-28 started 1.73 s LATE, inside the long madd of وَٱلْجَآنَّ, at
#     -20.9 dB = full speech level. The owner heard "...aana khalaqnahu".
# What neither had, and what a correct start always has, is SUSTAINED QUIET in
# front of it: a real waqf. So measure that directly and independently of
# attacks. A 0.21 s glottal stop fails it; so does starting mid-vowel.
# It WARNS rather than fails: a reciter who runs straight from one ayah into the
# next leaves no pause to find, and such a clip is legitimate (if abrupt).
HEAD_QUIET_DB = 12   # "quiet" = this far below the window's speech level
HEAD_QUIET_S = 0.30  # ...sustained for this long immediately before the start


def _source_path(clip):
    m = re.search(r"(?:v=|youtu\.be/|/live/)([A-Za-z0-9_-]{11})",
                  str(clip.get("source_url") or ""))
    if not m:
        return None
    p = os.path.join(ROOT, "sources", "%s.mp4" % m.group(1))
    return p if os.path.exists(p) else None


def check_head(res, clip):
    res.start("head")
    from qc import quran as Q
    phrases = clip.get("phrases") or []
    surah, ayat = clip.get("surah"), clip.get("ayah_range")
    if not phrases or not isinstance(surah, int) or not isinstance(ayat, list):
        return
    a = ayat[0]
    try:
        want = strict_ar(Q.ayah(surah, a)["ar"]).split()[0]
    except Exception:
        return
    got = strict_ar(phrase_texts(clip)[0]).split()
    if got and got[0] != want:
        res.fail("head",
                 "P1 opens with %r [%s] but ayah_range starts at %d:%d, whose "
                 "first word is %r [%s]. Either the clip starts mid-ayah (say "
                 "so in ayah_range) or segment.start is in the wrong verse."
                 % (got[0], _cp(got[0]), surah, a, want, _cp(want)))

    src = _source_path(clip)
    t0 = phrases[0].get("t0")
    if not src or not _num(t0):
        return
    import subprocess
    import tempfile
    from qc.author import energy as E
    from qc.proc import FFMPEG
    seg = clip["segment"]
    s0, s1 = _hms(seg["start"]), _hms(seg["end"])
    pre = min(HEAD_PRE, s0)
    wav = tempfile.mktemp(prefix="qc-head-", suffix=".wav")
    try:
        p = subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-ss", "%.3f" % (s0 - pre),
             "-t", "%.3f" % (s1 - s0 + pre), "-i", src, "-vn", "-ac", "1",
             "-ar", "16000", "-c:a", "pcm_s16le", wav], timeout=HEAD_TIMEOUT)
    except subprocess.TimeoutExpired:
        res.fail("head",
                 "ffmpeg wedged reading %s (no output in %ds). That is the "
                 "Opus-in-MP4 deadlock: re-mux the source with "
                 "`-c:v copy -c:a aac -b:a 192k` (qc source add now does this "
                 "on download)." % (os.path.basename(src), HEAD_TIMEOUT))
        return
    if p.returncode != 0 or not os.path.exists(wav):
        return
    try:
        times, db = E.envelope(wav)
        speech = E.speech_level(db)
        if pre >= HEAD_QUIET_S:
            # Walk back from segment.start (offset `pre`) while the envelope
            # stays quiet; how far we get is the pause the clip opens after.
            quiet, i = 0.0, None
            for k in range(len(times) - 1, -1, -1):
                if times[k] <= pre:
                    i = k
                    break
            lvl = db[i] if i is not None else None
            while i is not None and i > 0 and db[i] < speech - HEAD_QUIET_DB:
                i -= 1
                quiet = pre - times[i]
                if quiet >= HEAD_QUIET_S:
                    break
            if quiet < HEAD_QUIET_S:
                res.warn("head",
                         "segment.start has only %.2fs of quiet in front of it "
                         "(want %.2fs at %d dB below the %.1f dB speech level; "
                         "the envelope reads %.1f dB right at the start). A "
                         "correct start sits after a real waqf. This is what a "
                         "start buried mid-word looks like, and also what a "
                         "glottal stop mistaken for a pause looks like -- "
                         "measure the gap before the first word by hand before "
                         "trusting it. Legitimate if the reciter ran straight "
                         "into this ayah with no pause."
                         % (quiet, HEAD_QUIET_S, HEAD_QUIET_DB, speech,
                            lvl if lvl is not None else float("nan")))
        at = E.attacks(times, db, speech, lo=pre - 0.6, hi=pre + HEAD_LOOK)
        if at and abs(at[0] - (pre + float(t0))) > HEAD_TOL:
            res.fail("head",
                     "the first word of this clip attacks %.2fs after "
                     "segment.start, but P1 is captioned from %.2fs. The head "
                     "is %.2fs of something else -- the previous ayah's last "
                     "word or its reverb tail. Move segment.start to %s (the "
                     "attack, less the %.2fs hook)."
                     % (at[0] - pre, float(t0), at[0] - pre - float(t0),
                        _hms_str(s0 + at[0] - pre - 0.12), 0.12))
    finally:
        if os.path.exists(wav):
            os.remove(wav)


def _hms_str(t):
    return "%02d:%02d:%06.3f" % (int(t // 3600), int((t % 3600) // 60),
                                 t - 3600 * int(t // 3600) - 60 * int((t % 3600) // 60))


# ---------------------------------------------------------------------------
# 9. fx names
# ---------------------------------------------------------------------------
def check_fx(res, clip, style_name, style):
    res.start("fx")
    if style_name != "bars":
        if clip.get("fx"):
            res.fail("fx", "`fx:` only applies to the bars style; this clip is "
                           "`%s` and the map is ignored" % style_name)
        return
    from qc import fx
    try:
        fx.switches(style, clip)
    except SystemExit as e:
        res.fail("fx", str(e))


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
def phrase_texts(clip):
    """One caption's Arabic per phrase, as the renderer will lay it out."""
    from qc.arabic import norm_ar
    out = []
    for ph in clip.get("phrases") or []:
        if not isinstance(ph, dict):
            out.append("")
        elif ph.get("ar1"):
            out.append(norm_ar(" ".join(
                [ph["ar1"]] + ([ph["ar2"]] if ph.get("ar2") else []))))
        else:
            out.append(norm_ar(ph.get("ar") or ""))
    return out


def check_clip(clip_dir):
    """Every config-time check for one clip. -> Result."""
    import render_text
    name = os.path.basename(os.path.abspath(clip_dir).rstrip("/"))
    res = Result(name)
    path = os.path.join(clip_dir, "clip.yaml")
    if not os.path.exists(path):
        res.start("schema")
        res.fail("schema", "no clip.yaml in %s" % clip_dir)
        return res
    try:
        clip = render_text.load_yaml(path)
    except Exception as e:
        res.start("schema")
        res.fail("schema", "clip.yaml does not parse: %s" % e)
        return res

    check_schema(res, clip)
    if not isinstance(clip, dict):
        return res

    style_name = render_text.style_of(clip)
    if style_name not in render_text.STYLE_TEMPLATES:
        res.fail("schema", "unknown style %r (known: %s)"
                 % (style_name, ", ".join(sorted(render_text.STYLE_TEMPLATES))))
        return res
    style = render_text.load_yaml(render_text.template_path(clip))

    seg = clip.get("segment") or {}
    try:
        dur = round(_hms(seg["end"]) - _hms(seg["start"]), 3)
    except Exception:
        res.fail("schema", "segment.start/end must be \"HH:MM:SS.mmm\" strings "
                           "(quoted -- YAML reads 12:30 as a number)")
        return res
    if dur <= 0:
        res.fail("schema", "segment.end %s is not after segment.start %s"
                 % (seg.get("end"), seg.get("start")))
        return res

    check_phrases(res, clip, dur)
    check_cuts(res, clip, dur)
    check_english(res, clip, style_name)
    check_fx(res, clip, style_name, style)
    check_head(res, clip)

    texts = phrase_texts(clip)
    ens = [(ph.get("en") or "") if isinstance(ph, dict) else ""
           for ph in clip.get("phrases") or []]
    check_font(res, clip, style_name, style, list(zip(texts, ens)))
    check_verse(res, clip, texts)
    check_ayah_span(res, clip, texts)

    phrases, cut_dur = remap(clip, dur)
    if all(_num(p.get("t0")) and _num(p.get("t1")) for p in phrases):
        check_transitions(res, clip, style_name, style, phrases, cut_dur)
    if style_name == "bars":
        check_geometry(res, clip, style)
    return res


def run(clip_dirs):
    ok = True
    for cd in clip_dirs:
        res = check_clip(cd)
        print(res.text())
        ok = ok and res.ok
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# --output: the handful of facts about a FINISHED file worth asserting
#
# Not a look review -- the owner does that by eye in a minute, and it is the
# thing an automated pass was worst at. These are the mechanical properties
# that are invisible on a phone screen and fatal on Instagram: wrong frame
# size, wrong frame rate, a truncated or over-long file, a moov atom at the
# end (no faststart => the reel stalls before it plays), grey instead of black
# in the letterbox (an FX pass leaking outside the band), and loudness off
# target.
# ---------------------------------------------------------------------------
LOUDNESS_TOL = 0.5          # LU
LETTERBOX_MAX_LUMA = 2      # 8-bit; pure black after yuv420p round-tripping
LETTERBOX_SAMPLES = 8
# The row immediately touching the band is NOT measurable as letterbox. y=656
# and y=1264 are 16-px macroblock boundaries, and x264's in-loop deblocking
# filter smooths across block edges by design, so it pulls a little of the
# band's own edge luma into the first row outside it (plus DCT ringing). It is
# an artefact of the ENCODE, not of the filtergraph, and every bars clip has it:
# measured on row 1264, the golden at-tawbah-128-128 and the shipped
# al-qamar-1-5 both read exactly 2 -- i.e. flush with LETTERBOX_MAX_LUMA, no
# headroom at all -- while al-hijr-22-23 reads 3 on 2 pixels because its band
# bottom is slightly brighter. Row 1265 and beyond is pure 0 in every clip.
# So skip one row on each side. This costs nothing in detection power: an FX
# pass that is not cropped to the band hazes HUNDREDS of rows and shows up
# far outside the guard, which is why the ceiling below stays tight at 2.
BAND_EDGE_GUARD = 1         # rows adjacent to the band, excluded from the scan


def _probe(path):
    import json
    from qc.proc import FFPROBE
    import subprocess
    p = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", path],
                       capture_output=True, text=True)
    return json.loads(p.stdout or "{}")


def _faststart(path):
    """Is `moov` before `mdat`? Read the top-level box table; ~2 reads."""
    with open(path, "rb") as f:
        while True:
            head = f.read(8)
            if len(head) < 8:
                return False
            size = int.from_bytes(head[:4], "big")
            kind = head[4:8].decode("latin-1")
            if kind == "moov":
                return True
            if kind == "mdat":
                return False
            if size == 1:
                size = int.from_bytes(f.read(8), "big") - 8
                f.seek(size - 8, 1)
            elif size == 0:
                return False
            else:
                f.seek(size - 8, 1)


def _letterbox_luma(path, w, h, by, bh, n=LETTERBOX_SAMPLES):
    """Max luma found in the black bands above and below the footage, over `n`
    frames spread across the file. -> (max_top, max_bottom)."""
    import subprocess
    import tempfile
    from PIL import Image
    from qc.proc import FFMPEG
    tmp = tempfile.mkdtemp(prefix="qc-letterbox-")
    sel = "+".join("eq(n\\,%d)" % f for f in _sample_frames(path, n))
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", path,
                    "-vf", "select='%s'" % sel, "-vsync", "0",
                    os.path.join(tmp, "%02d.png")], check=True)
    top = bot = 0
    g = BAND_EDGE_GUARD
    for f in sorted(os.listdir(tmp)):
        im = Image.open(os.path.join(tmp, f)).convert("L")
        if by - g > 0:
            top = max(top, im.crop((0, 0, w, by - g)).getextrema()[1])
        if by + bh + g < h:
            bot = max(bot, im.crop((0, by + bh + g, w, h)).getextrema()[1])
        im.close()
    for f in os.listdir(tmp):
        os.remove(os.path.join(tmp, f))
    os.rmdir(tmp)
    return top, bot


def _sample_frames(path, n):
    d = _probe(path)
    nb = 0
    for s in d.get("streams", []):
        if s.get("codec_type") == "video":
            nb = int(s.get("nb_frames") or 0)
    if nb <= 0:
        nb = 300
    return [int(round(i * (nb - 1) / float(max(1, n - 1)))) for i in range(n)]


def _decode_errors(path):
    """Decode every packet and return the ffmpeg error lines. [] means clean.

    Existence checks and ffprobe metadata are NOT integrity checks: the moov
    atom, dimensions, frame rate and duration all survive a corrupted bitstream,
    so a file whose H.264 payload was interleaved by a second writer still
    reports 1920x1080 at the right duration and passes every other assertion
    here while being unplayable. Only a full decode pass sees it.

    Cheap enough to be unconditional: no filters, no encode, discard the output.
    """
    import subprocess
    from qc.proc import FFMPEG
    p = subprocess.run([FFMPEG, "-v", "error", "-xerror", "-i", path,
                        "-f", "null", "-"],
                       capture_output=True, text=True)
    return [ln for ln in (p.stderr or "").splitlines() if ln.strip()]


def _frame_count(path):
    """Frames actually decodable, which is not what the container claims."""
    import subprocess
    from qc.proc import FFPROBE
    p = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                        "-count_frames", "-show_entries",
                        "stream=nb_read_frames", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    try:
        return int((p.stdout or "").strip().split(",")[0])
    except (ValueError, IndexError):
        return None


def _loudness(path):
    """Integrated loudness of the finished file, per EBU R128."""
    import re as _re
    import subprocess
    from qc.proc import FFMPEG
    p = subprocess.run([FFMPEG, "-v", "info", "-nostats", "-i", path,
                        "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = _re.findall(r"I:\s*(-?\d+\.\d+)\s*LUFS", p.stderr)
    return float(m[-1]) if m else None


def check_output(clip_dir):
    import render_text
    name = os.path.basename(os.path.abspath(clip_dir).rstrip("/"))
    res = Result(name + " output/final.mp4")
    res.start("output")
    path = os.path.join(clip_dir, "output", "final.mp4")
    if not os.path.exists(path):
        res.fail("output", "no output/final.mp4 -- render it first")
        return res
    clip = render_text.load_yaml(os.path.join(clip_dir, "clip.yaml"))
    style = render_text.load_yaml(render_text.template_path(clip))
    bars = render_text.style_of(clip) == "bars"
    # Canvas comes from the template for BOTH styles. It used to be read from the
    # template for bars and hardcoded 1920x1080 for default, so the two disagreed
    # with the renderer whenever a template was edited.
    W, H = render_text.canvas_of(style)
    if bars:
        fps = int(style["meta"]["fps"])
        lufs = float(style["audio"]["lufs"])
    else:
        from qc.audio import LUFS
        fps, lufs = int((style.get("meta") or {}).get("fps") or 30), LUFS

    seg = clip["segment"]
    dur = round(_hms(seg["end"]) - _hms(seg["start"]), 3)
    _, dur = remap(clip, dur)

    d = _probe(path)
    v = next((s for s in d.get("streams", [])
              if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams", [])
              if s.get("codec_type") == "audio"), None)
    if v is None:
        res.fail("output", "no video stream")
        return res
    if (int(v["width"]), int(v["height"])) != (W, H):
        res.fail("output", "frame is %dx%d, the %s style is %dx%d -- this is a "
                           "preview or a stale render"
                 % (int(v["width"]), int(v["height"]),
                    render_text.style_of(clip), W, H))
    num, den = (v.get("r_frame_rate") or "0/1").split("/")
    got_fps = float(num) / float(den or 1)
    if abs(got_fps - fps) > 0.01:
        res.fail("output", "frame rate is %.3f, the style is %d" % (got_fps, fps))
    got_dur = float(d.get("format", {}).get("duration") or 0)
    if abs(got_dur - dur) > 0.10:
        res.fail("output", "duration is %.3fs, the segment (after cuts) is "
                           "%.3fs -- the encode was truncated or the source is "
                           "short" % (got_dur, dur))
    if not _faststart(path):
        res.fail("output", "the moov atom is AFTER the media data: re-encode "
                           "with -movflags +faststart or the reel stalls "
                           "before it starts playing")

    # INTEGRITY, before trusting anything measured above. Every assertion so far
    # reads container metadata, which survives a corrupted bitstream intact: a
    # file whose H.264 payload was interleaved by a second writer still reports
    # the right dimensions, frame rate and duration. Decoding is the only way to
    # see it, and it is the difference between shipping a reel and shipping an
    # unplayable one.
    errs = _decode_errors(path)
    if errs:
        res.fail("output",
                 "the bitstream does not decode cleanly -- %d ffmpeg error(s), "
                 "first: %s\n    Metadata (size, fps, duration) can look "
                 "perfect on a corrupted file, so this is the check that "
                 "catches it. Usual cause is two writers on one output path: "
                 "re-render to a path nothing else owns."
                 % (len(errs), errs[0][:160]))
    else:
        got_frames = _frame_count(path)
        want = int(round(dur * fps))
        # One frame of slack: the segment boundary rarely lands exactly on a
        # frame and the encoder rounds.
        if got_frames is not None and abs(got_frames - want) > 1:
            res.fail("output",
                     "decoded %d frames, expected about %d (%.3fs x %d fps) -- "
                     "the encode is truncated even though the container "
                     "duration reads %.3fs"
                     % (got_frames, want, dur, fps, got_dur))
    if (d.get("format", {}).get("tags") or {}).get("comment", "") \
            .startswith("qc-preview"):
        res.fail("output", "this file is tagged qc-preview -- it is not a "
                           "final render")

    if bars:
        band = style["band"]
        top, bot = _letterbox_luma(path, W, H, int(band["y"]),
                                   int(band["height"]))
        if max(top, bot) > LETTERBOX_MAX_LUMA:
            res.fail("output",
                     "the letterbox is not black: max luma %d above / %d below "
                     "the %dx%d band at y=%d (ceiling %d, measured excluding "
                     "the %d row(s) touching the band). Most likely an FX pass "
                     "is not cropped to the band -- that hazes hundreds of "
                     "rows, so dump a row histogram of the letterbox to see "
                     "how far it reaches. If instead it is a pixel or two on "
                     "one row only, that is x264 deblocking at a macroblock "
                     "edge, not a filtergraph bug: widen BAND_EDGE_GUARD "
                     "rather than raising the ceiling."
                     % (top, bot, int(band["width"]), int(band["height"]),
                        int(band["y"]), LETTERBOX_MAX_LUMA, BAND_EDGE_GUARD))
    if a is None:
        res.fail("output", "no audio stream")
    else:
        got = _loudness(path)
        if got is None:
            res.fail("output", "could not measure loudness (ebur128 produced "
                               "no summary)")
        elif abs(got - lufs) > LOUDNESS_TOL:
            res.fail("output",
                     "integrated loudness is %.1f LUFS, target %.1f "
                     "(+-%.1f LU) -- the loudnorm pass did not take"
                     % (got, lufs, LOUDNESS_TOL))
    return res


def run_output(clip_dirs):
    ok = True
    for cd in clip_dirs:
        res = check_output(cd)
        print(res.text())
        ok = ok and res.ok
    return 0 if ok else 1
