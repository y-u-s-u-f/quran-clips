"""tests/wrap_parity.py -- render_text.wrap_english against its definition.

    tools/render-venv/bin/python tests/wrap_parity.py
    tools/render-venv/bin/python tests/wrap_parity.py --limit 4000

`wrap_english` solves the balanced split with two bottleneck passes. What it
has to return is what ENUMERATING every split returns -- fewest lines, then
the narrowest widest line, then the smallest spread, then the earliest break
-- and that enumeration is the definition, not merely an older way of getting
there. So it is kept here, and the solver is diffed against it over real
cards: every window of every Saheeh International translation, at the fonts,
sizes and width caps a render actually shapes.

`--limit` caps how many splits a case may cost the enumeration (it is
C(words-1, lines-1), which is why the solver exists). Cases past it are still
run through the solver and checked for the properties the enumeration cannot
be asked about: the same words back, in order, in the fewest lines.
"""
import argparse
import itertools
import os
import sys
from math import comb

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import quran  # noqa: E402
import render_text as RT  # noqa: E402

# One font/size/width/case pairing per ayah, cycled, so the corpus covers the
# authored range (both canvases' caps, both English fonts, `english_caps` off)
# without multiplying every case by all of them.
FONTS = [("albertus", 33), ("gentium", 33), ("albertus", 26), ("albertus", 41)]
WINDOWS = (4, 7, 9, 13, 21)


def enumerated(text, font, tracking_px, max_w, stroke_macron, caps):
    """The definition: first-fit for the line count, then every split at that
    count, keeping the first with the smallest (widest line, spread)."""
    if not text.strip():
        return []
    words = text.split(" ")

    def width_of(ws):
        return RT.en_width(RT.en_tokens(" ".join(ws), stroke_macron, caps),
                           font, tracking_px)

    greedy, cur = [], []
    for wd in words:
        if not cur or width_of(cur + [wd]) <= max_w:
            cur.append(wd)
        else:
            greedy.append(cur)
            cur = [wd]
    if cur:
        greedy.append(cur)
    n = len(greedy)
    if n <= 1:
        return [" ".join(ln) for ln in greedy]

    best, best_key = None, None
    for cuts in itertools.combinations(range(1, len(words)), n - 1):
        bounds = (0,) + cuts + (len(words),)
        cand = [words[bounds[i]:bounds[i + 1]] for i in range(n)]
        ws = [width_of(ln) for ln in cand]
        if max(ws) > max_w:
            continue
        key = (max(ws), max(ws) - min(ws))
        if best_key is None or key < best_key:
            best_key, best = key, cand
    return [" ".join(ln) for ln in (best or greedy)]


def cases(step):
    """(label, text, wrap args) over the mushaf's translations."""
    for i in range(0, quran.N_AYAT, step):
        s, a = quran.from_flat(i)
        en = quran.ayah(s, a)["en"]
        ws = en.split()
        fk, pt = FONTS[i % len(FONTS)]
        spec = RT.ENGLISH_FONTS[fk]
        args = (RT.truetype(spec["path"], pt),
                RT.ENGLISH["tracking_pct"] / 100.0 * pt,
                (0.55 * 1920) if i % 2 else (0.82 * 1080),
                spec["stroke_macron"], (i % 3) != 0)
        for size in WINDOWS:
            off = (i * size) % max(1, len(ws))
            chunk = " ".join(ws[off:off + size])
            if chunk.strip():
                yield "%d:%d/%d" % (s, a, size), chunk, args
        yield "%d:%d/full" % (s, a), en, args


def line_count(text, font, tracking_px, max_w, stroke_macron, caps):
    """First-fit's line count -- the fewest a wrap may use."""
    n, cur = 0, []
    for wd in text.split(" "):
        w = RT.en_width(RT.en_tokens(" ".join(cur + [wd]), stroke_macron,
                                     caps), font, tracking_px)
        if not cur or w <= max_w:
            cur.append(wd)
        else:
            n, cur = n + 1, [wd]
    return n + (1 if cur else 0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=400,
                    help="most splits a case may cost the enumeration")
    ap.add_argument("--step", type=int, default=3,
                    help="take every Nth ayah (1 = all 6236)")
    a = ap.parse_args(argv)

    checked = skipped = 0
    bad = []
    for label, text, args in cases(a.step):
        got = RT.wrap_english(text, *args)
        nl = line_count(text, *args)
        if " ".join(got) != " ".join(text.split(" ")):
            bad.append((label, "words changed", got))
        elif len(got) != nl:
            bad.append((label, "%d lines, first-fit needs %d" % (len(got), nl),
                        got))
        if comb(max(0, len(text.split(" ")) - 1), max(0, nl - 1)) > a.limit:
            skipped += 1
            continue
        checked += 1
        want = enumerated(text, *args)
        if got != want:
            bad.append((label, "differs from the enumeration", (want, got)))

    print("%d cards against the enumeration, %d solver-only (over --limit %d)"
          % (checked, skipped, a.limit))
    for label, why, detail in bad[:12]:
        print("  %s: %s\n    %r" % (label, why, detail))
    if bad:
        print("FAIL  %d card(s)" % len(bad), file=sys.stderr)
        return 1
    print("OK  wrap_english matches its definition")
    return 0


if __name__ == "__main__":
    sys.exit(main())
