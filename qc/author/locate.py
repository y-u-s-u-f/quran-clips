"""qc.author.locate -- "what does this video actually recite?"

Input is YouTube's Arabic auto-caption VTT, which is far better than it looks:
`ar-orig` carries a timestamp per WORD (`<00:11:18.520><c> رسول</c>`), so we
get an approximate onset for every word in the video for free, without running
any ASR. It is also lossy in every way that matters -- no diacritics, invented
hamza placement, words dropped or hallucinated during long melismas, and
timestamps that drift by a second or more when a syllable is held. So this
module is built to be right about WHICH ayah and roughly right about WHEN, and
makes no claim to be right to the frame.

    Accuracy contract: locate to within a couple of seconds. A later phase
    refines the chosen window with Whisper + an energy envelope, which is the
    only thing that can find a true waqf gap (SKILL.md:94-108).

Method, in order:
  1. Parse the VTT into a flat, de-duplicated (time, word) stream. YouTube's
     roll-up captions repeat every line two or three times; the ~10 ms "cue"
     between real cues is the tell.
  2. Slide a window over the word stream and `qc.quran.search` each window.
  3. Walk the windows left to right with a CONTINUITY boost: a candidate that
     continues the ayah we were just in, or starts the next one, is favoured.
     Recitation is sequential, and that prior is what disambiguates the short
     formulaic ayat that match a dozen places in the mushaf.
  4. Vote per word POSITIONALLY -- each candidate votes only on the window
     tokens it actually accounts for -- then take the argmax ayah per word and
     group into runs.
  5. Snap each run's edges onto the words that match the ayah's own first and
     last tokens -- the window vote is only accurate to +/- half a window, and
     verse-end rhyme words are the strongest anchors in the whole problem
     (SKILL.md:64-65).

The video title is a HINT (`qc.quran.surah_from_title`) fed in as a scoring
boost, never as an answer. Titles lie: they name one famous ayah in a 17-minute
taraweeh recording, or say nothing at all.
"""
import os
import re
import sys

from .. import quran as Q
from . import fetch

ROOT = fetch.ROOT

WINDOW = 12          # words per matching window
STEP = 3             # window hop
MIN_RUN = 3          # words; shorter runs are noise, not an ayah
CONTINUITY = 1.30    # boost for staying in / advancing to the next ayah
TITLE_BOOST = 1.12   # deliberately small -- a hint, not a verdict

# The window winner's BLANKET vote: the weight it puts on every token in its
# window, including the ones it cannot account for. It used to be 1.0, i.e. the
# winner took the whole window, which is exactly how short ayat died (see
# match()). It is kept well below 1.0 so that a token positionally claimed by a
# rival outvotes the blanket, but well above 0 so that caption noise -- words
# the mushaf has no match for at all -- still lands on the dominant ayah
# instead of falling out of the timeline.
SMOOTH = 0.50
MIN_PHRASE = 2       # positional votes need >=2 ADJACENT matched query tokens
REL = 0.6            # a candidate must score >= REL x the winner to vote at all
LOCAL = 1.30         # positional vote boost for an ayah beside the window winner
LOCAL_SPAN = 4       # ...within this many ayat, either side


# --- VTT -------------------------------------------------------------------

_TS = re.compile(r"(\d+):(\d\d):(\d\d)\.(\d+)")
_CUE = re.compile(r"^(\d+:\d\d:\d\d\.\d+)\s+-->\s+(\d+:\d\d:\d\d\.\d+)")
_INLINE = re.compile(r"<(\d+:\d\d:\d\d\.\d+)>(?:<c>)?([^<]*)")
_TAGS = re.compile(r"<[^>]*>")


def _secs(ts):
    h, m, s, f = _TS.match(ts).groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + float("0." + f)


def parse_vtt(path):
    """VTT -> [(t_seconds, word), ...] in time order, de-duplicated.

    YouTube's roll-up format emits each line up to three times: once as the
    "new" bottom line with inline word timings, then again as static context in
    the following cues. Every real cue is preceded by a ~10 ms spacer cue that
    only repeats the previous text. We therefore take, from each cue, only the
    LAST text line, only when the cue is longer than 50 ms, and then drop any
    (time, word) pair we have already emitted.
    """
    text = open(path, encoding="utf-8", errors="replace").read()
    words = []
    seen = set()
    cue_t0 = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = _CUE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        t0, t1 = _secs(m.group(1)), _secs(m.group(2))
        body = []
        i += 1
        # Block terminator is a TRULY empty line. YouTube writes the unused
        # top caption line as a single SPACE, so testing `.strip()` here silently
        # threw away every cue that had one -- which is most of the cues that
        # begin a new line of recitation.
        while i < len(lines) and lines[i].rstrip("\r") != "" \
                and not _CUE.match(lines[i].strip()):
            body.append(lines[i])
            i += 1
        if t1 - t0 < 0.05:          # roll-up spacer: pure repetition
            continue
        payload = ""
        for ln in body:
            if ln.strip():
                payload = ln
        if not payload.strip():
            continue
        cue_t0 = t0
        # leading run before the first inline timestamp belongs to the cue start
        head = _TAGS.sub("", payload.split("<", 1)[0])
        for w in head.split():
            key = (round(cue_t0, 2), w)
            if key not in seen:
                seen.add(key)
                words.append((cue_t0, w))
        for ts, chunk in _INLINE.findall(payload):
            t = _secs(ts)
            for w in chunk.split():
                key = (round(t, 2), w)
                if key not in seen:
                    seen.add(key)
                    words.append((t, w))
    words.sort(key=lambda x: x[0])
    return words


def tokenize(words):
    """[(t, raw_word)] -> [(t, normalised_token)], dropping empties."""
    out = []
    for t, w in words:
        for tok in Q.tokens(w):
            out.append((t, tok))
    return out


# --- matching --------------------------------------------------------------

def _windows(toks):
    n = len(toks)
    i = 0
    while i < n:
        j = min(n, i + WINDOW)
        yield i, j, [t for _, t in toks[i:j]]
        if j >= n:
            break
        i += STEP


def _phrases(qis, minlen=MIN_PHRASE):
    """Query indices an ayah matched -> the ones inside a run of >= minlen.

    An ayah that shares one scattered word with the window (الله, من, الذين)
    has told us nothing. Two or more ADJACENT query tokens is a phrase, and a
    phrase is evidence about those particular tokens.
    """
    if minlen <= 1:
        return set(qis)
    out = set()
    for q in sorted(qis):
        if q - 1 in qis:
            continue
        run = [q]
        while run[-1] + 1 in qis:
            run.append(run[-1] + 1)
        if len(run) >= minlen:
            out.update(run)
    return out


def match(toks, title_boost=None):
    """Vote an ayah onto every token. -> list parallel to toks of (flat_idx, score).

    Each window elects a winner, as before, and the winner still smooths itself
    across the whole window -- but only at SMOOTH weight. On top of that, EVERY
    plausible candidate in the window votes at full weight on precisely the
    tokens it can account for.

    That second half is the whole point. Under pure winner-take-all a window is
    a single label, so an ayah shorter than the window can only be found when
    it happens to dominate one; ٱقْتَرَبَتِ ٱلسَّاعَةُ وَٱنشَقَّ ٱلْقَمَرُ (54:1, four
    words) sat in window 0 alongside eight words of 54:2, scored 0.42 against
    0.64, and vanished -- taking the whole 54:1-4 clip with it. Positionally it
    was never in doubt: it owned tokens 0-3 outright and 54:2 owned 4-11.
    """
    n = len(toks)
    votes = [dict() for _ in range(n)]
    prev = None
    for i, j, win in _windows(toks):
        cands = Q.search(win, top=6, boost=title_boost, detail=True)
        if not cands:
            prev = None
            continue
        scored = []
        for s, a, sc, hits, qis in cands:
            idx = Q.flat_index(s, a)
            if prev is not None and prev <= idx <= prev + 2:
                sc *= CONTINUITY
            scored.append((idx, sc, hits, qis))
        scored.sort(key=lambda r: -r[1])
        best_idx, best_sc, best_hits, _ = scored[0]
        # A window that matched almost nothing should not vote at all.
        if best_hits < 3 or best_sc < 0.25:
            continue
        prev = best_idx
        for k in range(i, j):
            votes[k][best_idx] = votes[k].get(best_idx, 0.0) + best_sc * SMOOTH
        # Nearest-first, so that a dead heat between two ayat carrying the SAME
        # formula (ٱلْحَمْدُ لِلَّهِ رَبِّ ٱلْعَٰلَمِينَ is 1:2 and 39:75 and 40:65 and
        # 6:45 ...) is inserted -- and so broken by the argmax below -- in
        # favour of the one nearest what is actually being recited.
        rest = sorted(scored[1:], key=lambda r: abs(r[0] - best_idx))
        own = set(scored[0][3])
        for idx, sc, hits, qis in [scored[0]] + rest:
            if sc < max(0.20, REL * best_sc):
                continue
            # The winner explains what it can; a rival is only heard about the
            # tokens the winner CANNOT explain. That is the whole gap we are
            # closing -- 54:1 owns four words 54:2 has no account of -- and it
            # keeps the rest of the window quiet: 11:88 and 12:67 also contain
            # عَلَيْهِ تَوَكَّلْتُ, but 9:129 has already accounted for those two
            # words, so they are not evidence against it.
            near = _phrases(qis) if idx == best_idx else _phrases(qis) - own
            # Same contiguity prior as CONTINUITY, applied INSIDE the window:
            # the neighbour of the ayah that won this window is a far likelier
            # explanation of its leftover tokens than an identical formula from
            # a different juz'. ٱلْحَمْدُ لِلَّهِ رَبِّ ٱلْعَٰلَمِينَ is 1:2 when 1:5
            # won the window; it is 39:75 or 40:65 nowhere near as often.
            w = sc * (LOCAL if abs(idx - best_idx) <= LOCAL_SPAN else 1.0)
            for q in near:
                k = i + q
                if i <= k < j:
                    votes[k][idx] = votes[k].get(idx, 0.0) + w
    out = []
    for v in votes:
        if not v:
            out.append((None, 0.0))
        else:
            idx = max(v, key=v.get)
            out.append((idx, v[idx] / sum(v.values())))
    return out


def _runs(assign, toks):
    """Group the per-token assignment into contiguous same-ayah runs."""
    runs = []
    cur = None
    for k, (idx, conf) in enumerate(assign):
        if idx is None:
            continue
        if cur and cur["idx"] == idx and k - cur["j"] <= 3:
            cur["j"] = k
            cur["conf"].append(conf)
        else:
            if cur and cur["j"] - cur["i"] + 1 >= MIN_RUN:
                runs.append(cur)
            cur = {"idx": idx, "i": k, "j": k, "conf": [conf]}
    if cur and cur["j"] - cur["i"] + 1 >= MIN_RUN:
        runs.append(cur)
    # merge runs of the same ayah separated only by a dropped stretch
    merged = []
    for r in runs:
        if merged and merged[-1]["idx"] == r["idx"] and r["i"] - merged[-1]["j"] <= WINDOW:
            merged[-1]["j"] = r["j"]
            merged[-1]["conf"] += r["conf"]
        else:
            merged.append(r)
    return merged


def _eq(toks, k, words):
    """Does the caption token at k equal `word`, allowing a 1-token split?

    `words` is the set of accepted spellings for that mushaf word. Captions
    also sometimes break a word in two across a held syllable (المومنين ->
    ال مومنين), so a two-token concatenation counts too.
    """
    if toks[k][1] in words:
        return True
    if k + 1 < len(toks) and toks[k][1] + toks[k + 1][1] in words:
        return True
    return False


def _snap(run, toks):
    """Pull a run's edges onto the ayah's own first/last words.

    The window vote smears a boundary by up to WINDOW/2 tokens in each
    direction. The ayah's first token and (especially) its rhyming last token
    are distinctive, so search a small neighbourhood of each edge for them.
    """
    s, a = Q.from_flat(run["idx"])
    raw = Q.ayah(s, a)["ar"]
    ay = Q.tokens(raw)
    alt = Q.tokens(raw, keep_super_alif=True)
    if not ay:
        return run["i"], run["j"]
    first = {ay[0]} | ({alt[0]} if len(alt) == len(ay) else set())
    last = {ay[-1]} | ({alt[-1]} if len(alt) == len(ay) else set())
    lo, hi = run["i"], run["j"]
    span = max(4, WINDOW)
    cand = [k for k in range(max(0, lo - span), min(len(toks), lo + span))
            if _eq(toks, k, first)]
    if cand:
        lo = min(cand, key=lambda k: abs(k - run["i"]))
    cand = [k for k in range(max(0, hi - span), min(len(toks), hi + span + 1))
            if _eq(toks, k, last)]
    if cand:
        hi = min(cand, key=lambda k: abs(k - run["j"]))
    return (lo, hi) if lo <= hi else (run["i"], run["j"])


def timeline(toks, title_boost=None):
    """-> [{'surah','ayah','start','end','conf','words'}], in time order."""
    assign = match(toks, title_boost)
    out = []
    for run in _runs(assign, toks):
        lo, hi = _snap(run, toks)
        s, a = Q.from_flat(run["idx"])
        start = toks[lo][0]
        # a word's onset is all we have; its end is the next word's onset
        end = toks[hi + 1][0] if hi + 1 < len(toks) else toks[hi][0] + 1.5
        out.append({
            "surah": s, "ayah": a, "start": start, "end": end,
            "conf": sum(run["conf"]) / len(run["conf"]),
            "words": hi - lo + 1,
            "covered": (run["i"], run["j"]),
        })
    # Suppress segments buried inside a stronger one. A long ayah's window
    # votes occasionally leave a short pocket of tokens voting for some other
    # ayah that shares a formula ("...الله بكل شيء عليم"), producing a 6-word
    # segment sitting inside a 30-word one. Keep strongest-first by evidence
    # (confidence x words) and drop anything that then overlaps a kept segment
    # over more than half its own duration.
    out.sort(key=lambda r: -(r["conf"] * r["words"]))
    kept = []
    for r in out:
        dur = max(r["end"] - r["start"], 1e-6)
        ov = sum(max(0.0, min(r["end"], k["end"]) - max(r["start"], k["start"]))
                 for k in kept)
        if ov / dur < 0.5:
            kept.append(r)
    kept.sort(key=lambda r: r["start"])
    return kept, assign


# --- reporting -------------------------------------------------------------

def hhmmss(t):
    return "%d:%05.2f" % (int(t // 60), t % 60)


def run(vid, verbose=False):
    meta = fetch.read_meta(vid)
    vtt = os.path.join(fetch.SOURCES, "%s.ar-orig.vtt" % vid)
    if not os.path.exists(vtt):
        raise SystemExit(
            "no %s. Run `qc source add <url>` first. If the video simply has no\n"
            "Arabic auto-captions, locate cannot run yet -- Whisper fallback is a\n"
            "later phase." % os.path.relpath(vtt, ROOT))

    title = meta.get("title") or ""
    hint = Q.surah_from_title(title)
    boost = {s: TITLE_BOOST for s in hint} or None

    words = parse_vtt(vtt)
    toks = tokenize(words)
    tl, assign = timeline(toks, boost)

    print("video   : %s" % vid)
    if title:
        print("title   : %s" % title)
    if hint:
        print("title hint: %s (boost x%.2f -- hint only)"
              % (", ".join(Q.surah_name(s) for s in sorted(hint)), TITLE_BOOST))
    print("captions: %d words, %.1f s .. %.1f s"
          % (len(toks), toks[0][0] if toks else 0, toks[-1][0] if toks else 0))
    print()

    for r in tl:
        print("  %-8s - %-8s  %-14s %3d:%-3d  conf %.2f  (%d words)" % (
            hhmmss(r["start"]), hhmmss(r["end"]), Q.surah_name(r["surah"]),
            r["surah"], r["ayah"], r["conf"], r["words"]))

    assigned = sum(1 for a, _ in assign if a is not None)
    cov = assigned / float(len(assign) or 1)
    surahs = sorted({r["surah"] for r in tl})
    confs = [r["conf"] for r in tl] or [0.0]
    dur = meta.get("duration")
    span = (tl[-1]["end"] - tl[0]["start"]) if tl else 0.0

    print()
    print("coverage : %.0f%% of caption words assigned to an ayah" % (100 * cov))
    if dur:
        print("           %.0f%% of the %ss video spanned by located ayat"
              % (100 * span / float(dur), dur))
    print("segments : %d, mean confidence %.2f" % (len(tl), sum(confs) / len(confs)))

    # Honesty pass: say when the answer is not a single clean passage.
    notes = []
    if len(surahs) > 1:
        notes.append("MULTI-SURAH: %s. This is a compilation or a taraweeh "
                     "recording, not one passage -- do not treat the title's "
                     "surah as the answer." %
                     ", ".join("%s (%d)" % (Q.surah_name(s), s) for s in surahs))
    nonmono = sum(1 for x, y in zip(tl, tl[1:])
                  if Q.flat_index(y["surah"], y["ayah"])
                  < Q.flat_index(x["surah"], x["ayah"]))
    if nonmono:
        notes.append("%d backward jump(s) in ayah order -- either the reciter "
                     "repeats, or those segments are misidentified." % nonmono)
    if cov < 0.6:
        notes.append("LOW COVERAGE (%.0f%%): most caption words could not be "
                     "placed. Treat this result as unreliable." % (100 * cov))
    low = [r for r in tl if r["conf"] < 0.5]
    if low:
        notes.append("%d segment(s) below conf 0.5: %s" % (
            len(low), ", ".join("%d:%d" % (r["surah"], r["ayah"]) for r in low)))
    if hint and tl and not (set(surahs) & set(hint)):
        notes.append("The title names %s but the captions match %s. Trusting "
                     "the captions." % (
                         ", ".join(Q.surah_name(s) for s in hint),
                         ", ".join(Q.surah_name(s) for s in surahs)))
    if notes:
        print()
        for n in notes:
            print("! " + n)
    else:
        print("no anomalies flagged")

    if verbose:
        print("\n--- unassigned stretches ---")
        k = 0
        while k < len(assign):
            if assign[k][0] is None:
                j = k
                while j < len(assign) and assign[j][0] is None:
                    j += 1
                if j - k >= 4:
                    print("  %s..%s  %s" % (
                        hhmmss(toks[k][0]), hhmmss(toks[j - 1][0]),
                        " ".join(t for _, t in toks[k:j])[:90]))
                k = j
            else:
                k += 1
    return tl


def at(vid, t):
    """Which ayah is being recited at absolute second `t`? Debug helper."""
    tl = run(vid)
    for r in tl:
        if r["start"] - 1.0 <= t <= r["end"] + 1.0:
            return r
    return None
