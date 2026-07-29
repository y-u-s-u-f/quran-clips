"""qc.author.propose -- "here's a URL": turn a long recitation into a few
candidate clips the owner can actually choose between.

`qc locate` answers *what is recited*; it says nothing about what is worth
posting. A 24-minute taraweeh recording yields ~100 located ayat, most of which
make a bad clip: a fragment that starts mid-sentence, a four-second ayah, a
stretch the captions half-guessed. This module ranks the windows.

Three ideas do the work:

    CONTIGUOUS SAME-SURAH RUNS, NOT SEGMENTS. Candidates are built only from
    stretches where the located ayat advance monotonically inside one surah.
    That immediately kills the failure mode `locate` warns about but cannot
    fix: an isolated cross-surah blip at high confidence sitting inside an
    otherwise continuous run (Yusuf 12:100 twice inside Al-Waqiah on
    vqxYwdR4RvQ, at conf 0.84). Shared Qur'anic formulas -- especially closing
    formulas, which is where the search index has least to go on -- misread as
    a famous ayah elsewhere in the mushaf. Confidence does not detect this;
    context does. A one-segment island of a foreign surah between two segments
    of the same surah is dropped, however sure the matcher is.

    SELF-CONTAINMENT IS MEASURED, NOT ASSUMED. `locate` snaps a run's edges
    onto the ayah's own first and last words, so we can ask how much of the
    first and last ayah the segment actually covers. A window whose opening
    ayah is only half covered starts mid-sentence, and no amount of confidence
    redeems it.

    STRUCTURE IS NOT MEANING. All of the above judges the container. A window
    can be whole ayat with clean edges and still open on a dangling referent --
    Al-Qamar 54:2, "And if they SEE A SIGN, they turn away", where the sign is
    the splitting of the moon in 54:1 that the window excludes. `coherence` asks
    a model that question in English, on numbers only, and either down-ranks the
    window or swaps in the extension that fixes it. It is advisory: if the judge
    is unavailable the ranking below is exactly what it always was.

    STYLE CHANGES THE ANSWER. For the bars style the passage must break into
    SHORT BALANCED lines -- never more than ~4 short words per line
    (SKILL.md:199-205) -- and one over-long line shrinks the type for the whole
    clip. That is a property of the WORDS, knowable before any audio is
    touched, so it is scored here, using the renderer's own measurement via
    `emit.split_lines` + `linebreak.fits`. A passage that is superb in the
    default style and has no interior seam ranks DOWN for bars.

Nothing here is precise to the frame and nothing here should be: `qc author`
re-aligns and energy-snaps the chosen window. Propose is allowed to be a second
or two out; it is not allowed to propose a window that is the wrong passage.
"""
import json
import os

from .. import quran as Q
from . import coherence as CO
from . import emit as EM
from . import fetch
from . import linebreak as LB
from . import locate as L

ROOT = fetch.ROOT
OUT_DIR = os.path.join(fetch.SOURCES, "_propose")

MIN_DUR = 20.0          # SKILL.md:85-86 -- target 20-45s
MAX_DUR = 45.0
BEST_LO, BEST_HI = 26.0, 38.0   # flat top of the duration score
MAX_AYAT = 5            # more than this is a lecture, not a clip
COVER_FULL = 0.75       # fraction of an ayah's words a segment must carry
                        # for that edge to count as self-contained
LOW_CONF = 0.5

FIT_LO, FIT_HI = 0.85, 1.30   # caption words per mushaf word, before penalty

WEIGHTS = {
    "conf": 0.22,
    "dur": 0.16,
    "contained": 0.18,
    "cover": 0.12,
    "clean": 0.12,
    "fit": 0.20,
}
BARS_WEIGHT = 0.55      # blended in as (1-w)*base + w*bars, for --style bars

JUDGE_N = 8             # how many candidates get the semantic-coherence call.
                        # One subprocess each, so this is the whole cost knob.
                        # 8 rather than 5 because a candidate that is judged
                        # incoherent and cannot be fixed drops out of the top 5,
                        # and something has to be ready to take its place --
                        # judging only the printed five would leave the list
                        # shorter than asked for. Below 8 that happened on
                        # vqxYwdR4RvQ; above 8 the extra calls never changed a
                        # printed row on either test video.


# ---------------------------------------------------------------------------
# the located timeline, quietly
# ---------------------------------------------------------------------------

def located(vid):
    """-> (segments, meta). `locate.run` prints a report; this does not."""
    meta = fetch.read_meta(vid)
    vtt = os.path.join(fetch.SOURCES, "%s.ar-orig.vtt" % vid)
    if not os.path.exists(vtt):
        raise SystemExit("no %s. Run `qc source add <url>` first."
                         % os.path.relpath(vtt, ROOT))
    hint = Q.surah_from_title(meta.get("title") or "")
    boost = {s: L.TITLE_BOOST for s in hint} or None
    toks = L.tokenize(L.parse_vtt(vtt))
    tl, _ = L.timeline(toks, boost)
    return tl, meta


# ---------------------------------------------------------------------------
# phantoms
# ---------------------------------------------------------------------------

def drop_phantoms(tl, span=2):
    """Remove isolated cross-surah blips sitting inside a same-surah run.

    A segment is a phantom when every located segment within `span` positions
    on BOTH sides belongs to one other surah. That is the exact shape of the
    Yusuf 12:100 misreads on vqxYwdR4RvQ, and it is a shape a genuine surah
    change cannot have: a real change of surah is followed by more of the new
    surah, not by a snap back.

    At the very start or end of a video only one side exists; there a
    unanimous run of at least `span` segments on that side is enough, since a
    surah that appears once at the tail of a recording and never again is the
    same misread with less evidence around it (the second Yusuf 12:100 on
    vqxYwdR4RvQ is the last segment in the file).

    -> (kept, phantoms)
    """
    keep, bad = [], []
    for i, r in enumerate(tl):
        left = [x["surah"] for x in tl[max(0, i - span):i]]
        right = [x["surah"] for x in tl[i + 1:i + 1 + span]]
        sides = [s for s in (left, right) if s]
        ok = bool(sides) and all(len(set(s)) == 1 and r["surah"] not in s
                                 for s in sides)
        if ok and len(sides) == 2:
            ok = set(left) == set(right)
        elif ok:
            ok = len(sides[0]) >= span
        (bad if ok else keep).append(r)
    return keep, bad


# ---------------------------------------------------------------------------
# repeats
# ---------------------------------------------------------------------------

def mark_repeats(tl):
    """Flag every pass of an ayah that is recited more than once.

    The reciter genuinely repeats ayat, and `locate` also genuinely
    misidentifies some. Either way a window containing a repeat is unsafe
    unless we can say which pass is the good one -- so we name a preferred
    pass (most evidence: confidence x words) and mark only the others. That
    keeps the strong pass usable instead of throwing the ayah away.

    -> {id(seg): 'preferred' | 'other'} for repeated ayat only.
    """
    by = {}
    for r in tl:
        by.setdefault((r["surah"], r["ayah"]), []).append(r)
    flags = {}
    for key, rs in by.items():
        if len(rs) < 2:
            continue
        best = max(rs, key=lambda r: r["conf"] * r["words"])
        for r in rs:
            flags[id(r)] = "preferred" if r is best else "other"
    return flags


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

def runs(tl, max_gap=6.0, max_skip=4):
    """Split the timeline into contiguous, forward-moving, same-surah runs.

    Broken by: a change of surah, a backward jump in ayah number (a repeat or
    a misread -- either way not a clip boundary we can trust), a jump forward
    of more than `max_skip` ayat (the reciter skipped, or locate lost the
    thread), or more than `max_gap` seconds of unlocated audio.
    """
    out, cur = [], []
    for r in tl:
        if cur:
            p = cur[-1]
            broke = (r["surah"] != p["surah"]
                     or r["ayah"] < p["ayah"]
                     or r["ayah"] - p["ayah"] > max_skip
                     or r["start"] - p["end"] > max_gap)
            if broke:
                out.append(cur)
                cur = []
        cur.append(r)
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# scoring pieces
# ---------------------------------------------------------------------------

def _ayah_words(s, a):
    try:
        return Q.tokens(Q.ayah(s, a)["ar"])
    except Exception:
        return []


def coverage(seg):
    """How much of its own ayah does this segment carry? -> 0..1 (clamped)."""
    n = len(_ayah_words(seg["surah"], seg["ayah"]))
    if not n:
        return 0.0
    return min(1.0, seg["words"] / float(n))


def fit_score(segs, a, b):
    """Does the window hold as many words as the ayat it claims? -> (0..1, ratio)

    This is the check that caught the worst proposal of the first pass. On
    vqxYwdR4RvQ `locate` reports "Al-Qamar 54:37, 23 words" over 4:34-5:00 --
    but 54:37 is nine words, and the captions in that window are 54:36, 37, 38
    and the head of 39. The run's tail failed to snap (54:36 and 54:38 both end
    on the same rhyme, `فَذُوقُوا۟ عَذَابِى وَنُذُرِ`, so the last-word anchor is
    ambiguous exactly there). Nothing about that segment's confidence, its
    duration or its self-containment gives it away; only counting does. A ratio
    far from 1 means the claimed ayah range and the audio in the window are not
    the same thing, and handing it to `qc author` would caption the wrong ayat.
    """
    have = sum(r["words"] for r in segs)
    want = sum(len(_ayah_words(segs[0]["surah"], n)) for n in range(a, b + 1))
    if not want:
        return 0.0, 0.0
    ratio = have / float(want)
    if FIT_LO <= ratio <= FIT_HI:
        return 1.0, ratio
    if ratio < FIT_LO:
        return max(0.0, 1.0 - (FIT_LO - ratio) / 0.55), ratio
    return max(0.0, 1.0 - (ratio - FIT_HI) / 0.55), ratio


def dur_score(d):
    if d < MIN_DUR or d > MAX_DUR:
        return 0.0
    if BEST_LO <= d <= BEST_HI:
        return 1.0
    if d < BEST_LO:
        return 0.4 + 0.6 * (d - MIN_DUR) / (BEST_LO - MIN_DUR)
    return 0.4 + 0.6 * (MAX_DUR - d) / (MAX_DUR - BEST_HI)


_BARS_CACHE = {}


def bars_score(surah, a, b):
    """Does this passage break into short, balanced, in-cap caption lines?

    Cards are chunked the way `emit` chunks them when the audio has no opinion
    (<= MAX_WORDS per card, balanced), then each card is split by the same
    `emit.split_lines` the authoring path uses and measured with
    `linebreak.fits`, which borrows the renderer's font, shaping and
    `text.max_line_width_frac`. There is deliberately no second width model
    here: a passage is bars-friendly iff the real renderer says its lines fit
    at nominal point size.

    -> (score 0..1, note)
    """
    key = (surah, a, b)
    if key in _BARS_CACHE:
        return _BARS_CACHE[key]
    cards = []
    for n in range(a, b + 1):
        try:
            words = Q.nfc(Q.ayah(surah, n)["ar"]).split()
        except Exception:
            continue
        if not words:
            continue
        k = max(1, (len(words) + EM.MAX_WORDS - 1) // EM.MAX_WORDS)
        size = (len(words) + k - 1) // k
        for i in range(0, len(words), size):
            cards.append(words[i:i + size])
    if not cards:
        res = (0.0, "no text")
        _BARS_CACHE[key] = res
        return res

    good = wide = fat = 0
    for c in cards:
        lines = EM.split_lines(c)
        over = max(len(ln.split()) for ln in lines) > EM.MAX_LINE_WORDS
        ok = LB.fits(lines)
        if over:
            fat += 1
        elif ok is False:
            wide += 1
        else:
            good += 1
    n = float(len(cards))
    score = (good + 0.45 * wide) / n
    notes = []
    if fat:
        notes.append("%d/%d card(s) cannot get under %d words a line"
                     % (fat, len(cards), EM.MAX_LINE_WORDS))
    if wide:
        notes.append("%d/%d card(s) overflow the width cap (shrinks the whole "
                     "clip's type)" % (wide, len(cards)))
    if not notes:
        notes.append("all %d card(s) split into short, in-cap lines" % len(cards))
    res = (score, "; ".join(notes))
    _BARS_CACHE[key] = res
    return res


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------

def candidates(tl, style="bars"):
    tl, phantoms = drop_phantoms(tl)
    flags = mark_repeats(tl)
    out = []
    for run in runs(tl):
        for i in range(len(run)):
            for j in range(i, len(run)):
                segs = run[i:j + 1]
                start, end = segs[0]["start"], segs[-1]["end"]
                dur = end - start
                if dur > MAX_DUR:
                    break
                if dur < MIN_DUR:
                    continue
                if segs[-1]["ayah"] - segs[0]["ayah"] + 1 > MAX_AYAT:
                    break
                c = _score(segs, flags, style)
                if c:
                    out.append(c)
    out.sort(key=lambda c: -c["score"])
    return out, phantoms


def _score(segs, flags, style):
    a, b = segs[0]["ayah"], segs[-1]["ayah"]
    s = segs[0]["surah"]
    start, end = segs[0]["start"], segs[-1]["end"]
    dur = end - start

    # An 'other' pass of a repeated ayah is not the one to clip.
    if any(flags.get(id(r)) == "other" for r in segs):
        return None

    w = sum(r["words"] for r in segs) or 1
    conf = sum(r["conf"] * r["words"] for r in segs) / w

    head, tail = coverage(segs[0]), coverage(segs[-1])
    contained = 0.5 * min(1.0, head / COVER_FULL) + 0.5 * min(1.0, tail / COVER_FULL)

    located_ayat = {r["ayah"] for r in segs}
    n_ayat = b - a + 1
    cover = len(located_ayat) / float(n_ayat)

    low = [r for r in segs if r["conf"] < LOW_CONF]
    clean = 1.0 - min(1.0, len(low) / float(len(segs)))
    if any(flags.get(id(r)) == "preferred" for r in segs):
        clean *= 0.85          # a repeat is nearby; usable, but not first choice

    fit, ratio = fit_score(segs, a, b)
    parts = {
        "conf": conf,
        "dur": dur_score(dur),
        "contained": contained,
        "cover": cover,
        "clean": clean,
        "fit": fit,
    }
    base = sum(WEIGHTS[k] * v for k, v in parts.items())

    bars, bars_note = (bars_score(s, a, b) if style == "bars" else (None, None))
    score = base if bars is None else (1 - BARS_WEIGHT) * base + BARS_WEIGHT * bars

    return {
        "surah": s, "surah_name": Q.surah_name(s), "ayah_from": a, "ayah_to": b,
        "start": round(start, 2), "end": round(end, 2), "duration": round(dur, 2),
        "conf": round(conf, 3), "score": round(score, 4),
        "base_score": round(base, 4),
        "bars_score": None if bars is None else round(bars, 3),
        "bars_note": bars_note,
        "parts": {k: round(v, 3) for k, v in parts.items()},
        "word_ratio": round(ratio, 2),
        "head_cover": round(head, 2), "tail_cover": round(tail, 2),
        "low_conf_ayat": ["%d:%d" % (r["surah"], r["ayah"]) for r in low],
        "missing_ayat": [n for n in range(a, b + 1) if n not in located_ayat],
        "repeat_nearby": any(flags.get(id(r)) == "preferred" for r in segs),
        "opening": _opening(s, a),
        "author_cmd": "qc author %%s %d:%d-%d %.2f %.2f --style %%s"
                      % (s, a, b, start, end),
    }


def _opening(s, a, n=5):
    try:
        return " ".join(Q.nfc(Q.ayah(s, a)["ar"]).split()[:n])
    except Exception:
        return ""


def dedupe(cands, keep):
    """Rank greedily, refusing a candidate that mostly repeats a better one."""
    out = []
    for c in cands:
        d = max(c["duration"], 1e-6)
        clash = False
        for k in out:
            ov = min(c["end"], k["end"]) - max(c["start"], k["start"])
            if ov / d > 0.5 or ov / max(k["duration"], 1e-6) > 0.5:
                clash = True
                break
        if not clash:
            out.append(c)
        if len(out) >= keep:
            break
    return out


# ---------------------------------------------------------------------------
# explicit windows (decision 11: the owner often already knows)
# ---------------------------------------------------------------------------

def _tc(s):
    t = 0.0
    for p in str(s).split(":"):
        t = t * 60 + float(p)
    return t


def from_range(tl, spec, style):
    lo, hi = [_tc(x) for x in spec.split("-", 1)]
    inside = [r for r in tl if r["end"] > lo and r["start"] < hi]
    if not inside:
        raise SystemExit("no located ayat inside %s -- run `qc locate` and "
                         "check the times" % spec)
    surah = max({r["surah"] for r in inside},
                key=lambda s: sum(r["words"] for r in inside if r["surah"] == s))
    inside = [r for r in inside if r["surah"] == surah]
    c = _score(inside, {}, style) or {}
    c.update({"start": round(lo, 2), "end": round(hi, 2),
              "duration": round(hi - lo, 2),
              "surah": surah, "surah_name": Q.surah_name(surah),
              "ayah_from": inside[0]["ayah"], "ayah_to": inside[-1]["ayah"],
              "explicit": "--range %s" % spec})
    c["author_cmd"] = "qc author %%s %d:%d-%d %.2f %.2f --style %%s" % (
        surah, inside[0]["ayah"], inside[-1]["ayah"], lo, hi)
    return [c]


def from_verses(tl, spec, style):
    s, _, rng = spec.partition(":")
    a, _, b = rng.partition("-")
    s, a, b = int(s), int(a), int(b or a)
    inside = [r for r in tl if r["surah"] == s and a <= r["ayah"] <= b]
    if not inside:
        raise SystemExit("%d:%d-%d is not in this video's located timeline"
                         % (s, a, b))
    c = _score(inside, {}, style)
    if c is None:
        c = {"surah": s, "surah_name": Q.surah_name(s),
             "ayah_from": a, "ayah_to": b, "conf": 0.0, "score": 0.0,
             "opening": _opening(s, a), "parts": {}}
    c.update({"start": round(inside[0]["start"], 2),
              "end": round(inside[-1]["end"], 2),
              "duration": round(inside[-1]["end"] - inside[0]["start"], 2),
              "ayah_from": a, "ayah_to": b,
              "explicit": "--verses %s" % spec})
    c["author_cmd"] = "qc author %%s %d:%d-%d %.2f %.2f --style %%s" % (
        s, a, b, inside[0]["start"], inside[-1]["end"])
    return [c]


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _why(c):
    p = c.get("parts") or {}
    bits = []
    if p.get("contained", 0) >= 0.95:
        bits.append("self-contained (opens on the ayah's first word, closes on "
                    "its last)")
    elif c.get("head_cover", 1) < COVER_FULL:
        bits.append("starts PART-WAY into %d:%d (only %.0f%% of it located)"
                    % (c["surah"], c["ayah_from"], 100 * c.get("head_cover", 0)))
    if c.get("conf", 0) >= 0.75:
        bits.append("high caption confidence %.2f" % c["conf"])
    elif c.get("conf", 0) < 0.6:
        bits.append("LOW confidence %.2f" % c["conf"])
    if p.get("dur", 0) >= 1.0:
        bits.append("%.0fs sits in the sweet spot" % c["duration"])
    if p.get("fit", 1) < 0.9:
        r = c.get("word_ratio", 1.0)
        bits.append("SPAN MISMATCH: the window holds %.1fx the words of %d:%d%s "
                    "-- it %s than the ayat it claims" % (
                        r, c["surah"], c["ayah_from"],
                        "" if c["ayah_to"] == c["ayah_from"]
                        else "-%d" % c["ayah_to"],
                        "covers more (spilled ayat, or the reciter restarts "
                        "mid-ayah)" if r > 1 else "covers less"))
    if c.get("missing_ayat"):
        bits.append("ayah(s) %s not located inside the window"
                    % ", ".join(str(x) for x in c["missing_ayat"]))
    if c.get("low_conf_ayat"):
        bits.append("weak: %s" % ", ".join(c["low_conf_ayat"]))
    if c.get("repeat_nearby"):
        bits.append("ayah is recited more than once; this is the stronger pass")
    if c.get("bars_note"):
        bits.append("bars: %s" % c["bars_note"])
    co = c.get("coherence")
    if co:
        if co["verdict"] == "standalone":
            bits.append("meaning: stands alone -- %s" % co["reason"])
        elif co["verdict"] == "adjusted":
            bits.append("meaning: ADJUSTED from %s -- %s"
                        % (co["adjusted_from"], co["reason"]))
        else:
            bits.append("meaning: DOES NOT STAND ALONE (%s) -- %s%s"
                        % (co["verdict"], co["reason"],
                           "" if co.get("fix_available") else
                           "; %s is not a proposable window here" % co["wanted"]))
    return bits


def demoted(pool, shown):
    """Judged-incoherent candidates that the judgement pushed off the list.

    Without this they vanish silently, which is the one thing the judge must not
    do: on vqxYwdR4RvQ the structural #1 (Al-Qamar 54:2-4) is exactly such a
    case, and "your best-looking window opens on a referent it cuts" is the most
    useful line in the whole report.
    """
    keys = {(c["surah"], c["ayah_from"], c["ayah_to"], c["start"]) for c in shown}
    out = []
    for c in pool:
        co = c.get("coherence")
        if not co or co["verdict"] in ("standalone", "adjusted"):
            continue
        if (c["surah"], c["ayah_from"], c["ayah_to"], c["start"]) in keys:
            continue
        out.append(c)
    return out


def report(vid, cands, phantoms, style, meta, n, coh=None, dropped=()):
    print("video : %s" % vid)
    if meta.get("title"):
        print("title : %s" % meta["title"])
    print("style : %s" % style)
    if coh:
        print("meaning: %s" % coh)
    if phantoms:
        print("dropped %d phantom segment(s) (isolated cross-surah blip inside "
              "a run): %s" % (len(phantoms), ", ".join(
                  "%s %d:%d @%s conf %.2f" % (Q.surah_name(r["surah"]),
                                              r["surah"], r["ayah"],
                                              L.hhmmss(r["start"]), r["conf"])
                  for r in phantoms)))
    print()
    if not cands:
        print("no candidate window scored -- nothing in this video is a "
              "contiguous %g-%gs same-surah passage." % (MIN_DUR, MAX_DUR))
        return
    for i, c in enumerate(cands[:n], 1):
        print("%d. %s - %s  (%.1fs)   %s %d:%d%s   conf %.2f   score %.3f%s" % (
            i, L.hhmmss(c["start"]), L.hhmmss(c["end"]), c["duration"],
            c["surah_name"], c["surah"], c["ayah_from"],
            "" if c["ayah_to"] == c["ayah_from"] else "-%d" % c["ayah_to"],
            c["conf"], c["score"],
            "" if c.get("bars_score") is None else
            "  (bars %.2f)" % c["bars_score"]))
        print("   %s" % c.get("opening", ""))
        for b in _why(c):
            print("   - %s" % b)
        print("   %s" % (c["author_cmd"] % (vid, style)))
        print()
    if dropped:
        print("down-ranked on meaning (structurally strong, but the passage "
              "does not stand alone):")
        for c in dropped:
            co = c["coherence"]
            print("   %s - %s   %s %d:%d%s   structural %.3f -> %.3f"
                  % (L.hhmmss(c["start"]), L.hhmmss(c["end"]), c["surah_name"],
                     c["surah"], c["ayah_from"],
                     "" if c["ayah_to"] == c["ayah_from"]
                     else "-%d" % c["ayah_to"],
                     c["score"] / CO.PENALTY, c["score"]))
            print("      %s" % co["reason"])
            print("      would need %s -- %s" % (
                co["wanted"], "offered above" if co.get("fix_available")
                else "not a proposable window in this video"))
        print()


def run(vid, style="bars", n=5, rng=None, verses=None, as_json=False,
        judge=True):
    tl, meta = located(vid)
    coh, pool = None, []
    if rng:
        cands, phantoms = from_range(tl, rng, style), []
        cands, coh = CO.apply(cands, cands, use_llm=judge)
        pool = cands
    elif verses:
        cands, phantoms = from_verses(tl, verses, style), []
        cands, coh = CO.apply(cands, cands, use_llm=judge)
        pool = cands
    else:
        cands, phantoms = candidates(tl, style)
        # Judge a slightly deeper pool than we print, then re-rank and re-dedupe:
        # a down-ranked window has to be able to fall out of the top n, and a
        # swapped-in extension can now overlap something below it.
        pool = dedupe(cands, max(n, JUDGE_N))
        pool, coh = CO.apply(pool, cands, use_llm=judge)
        cands = dedupe(pool, n)
    drop = demoted(pool, cands[:n])
    payload = {"video_id": vid, "style": style, "coherence": coh,
               "down_ranked": drop,
               "title": meta.get("title"), "duration": meta.get("duration"),
               "phantoms": [{"surah": r["surah"], "ayah": r["ayah"],
                             "start": round(r["start"], 2),
                             "conf": round(r["conf"], 2)} for r in phantoms],
               "candidates": cands[:n]}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        report(vid, cands, phantoms, style, meta, n, coh, drop)
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, "%s.%s.json" % (vid, style))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print("json  : %s" % os.path.relpath(path, ROOT))
    return payload
