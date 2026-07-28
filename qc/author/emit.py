"""qc.author.emit -- turn an aligned, energy-snapped window into clip.yaml.

The schema is NOT redesigned here: this writes exactly the file the current
renderer already reads (`segment.start`/`end` absolute, `phrases[].t0/t1`
relative to the segment, `ar` or `ar1`/`ar2` for the bars style). Anything the
pipeline cannot know -- the crop, the reciter's name, the topical tags -- is
either carried in from a reference clip or left with a loud TODO.

Two things here matter as much as the numbers:

    THE COMMENTS ARE THE DELIVERABLE. Every shipped clip.yaml in this repo
    explains itself: why the segment starts where it starts, how deep the gap
    at each caption swap is, which pause is a restart. That prose is the only
    durable record of the reasoning -- the audio analysis is thrown away, the
    clip folder is deleted after posting, and the yaml is what survives. So
    the boundary rationale is written out in full, in the same voice, with the
    real measured numbers rather than round ones.

    THE ARABIC COMES FROM THE MUSHAF, NFC-NORMALISED. `qc.quran.ayah()` hands
    back alquran.cloud's bytes, which spell the madda alif DECOMPOSED
    (U+0627 U+0653) where every existing clip.yaml has it PRECOMPOSED
    (U+0622). Both render identically; only one matches the repo. Everything
    written here goes through `qc.quran.nfc()`.
"""
import os

from .. import quran as Q
from . import align as A
from . import energy as E
from . import linebreak as LB

# Card splitting. A true waqf is always a caption swap. Beyond that, a card is
# split only because it is too long to read or too wide to set: the bars
# reference art never carries more than ~4 short words per line, and a
# two-line card of 9 words forces the whole clip's font size down (that is the
# documented reason at-tawbah-128-128 is 4 cards, not 3).
MAX_WORDS = 8
MAX_DUR = 9.5
MAX_LINE_WORDS = 4
MIN_CARD_WORDS = 3
MIN_CARD_DUR = 1.8
HEAD_TARGET = 0.12   # onset lands here after segment.start (SKILL.md:96-99)


def hhmmss(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - 3600 * h - 60 * m
    return "%02d:%02d:%06.3f" % (h, m, s)


# ---------------------------------------------------------------------------
# card splitting
# ---------------------------------------------------------------------------

def word_dips(events, ds, radius=0.6):
    """Attach each envelope dip to the inter-word boundary it belongs to.

    -> {k: dip}, where k means "between events k and k+1".

    Matched against the INTERVAL [events[k].t1, events[k+1].t0], not against a
    single boundary instant. Inside one ASR chunk Whisper's word spans are
    contiguous and that interval is a point, but across a real pause it is the
    whole gap -- and the trough of a waqf sits at its far end, right under the
    next word's onset. Measuring to the nearest boundary INSTANT therefore
    scored the true waqf as 0.8 s away and handed the caption swap to a
    consonant closure 0.9 s earlier instead.

    Where several dips land on the same boundary, the deepest wins.
    """
    out = {}
    if len(events) < 2:
        return out
    lo = [events[k]["t1"] for k in range(len(events) - 1)]
    hi = [events[k + 1]["t0"] for k in range(len(events) - 1)]

    def dist(k, t):
        return max(0.0, lo[k] - t, t - hi[k])

    for d in ds:
        k = min(range(len(events) - 1), key=lambda k: dist(k, d["t"]))
        if dist(k, d["t"]) > radius:
            continue
        if k not in out or d["depth"] > out[k]["depth"]:
            out[k] = d
    return out


def split_cards(events, wd, max_words=MAX_WORDS, max_dur=MAX_DUR):
    """-> ([(lo, hi), ...] index pairs into `events`, {cut_index: reason}).

    Pass 1 cuts at every TRUE waqf -- a real stop is always a caption swap.
    Pass 2 keeps subdividing any card that is still too long to set, each time
    at the deepest dip it still contains. That second pass is what puts the
    at-tawbah card-1 swap in the shallow dip before عَزِيزٌ: the card is 9
    words and 11 s, no true waqf exists inside it, so the swap goes in the
    quietest moment available and is flagged as non-waqf.
    """
    cuts = {k for k, d in wd.items() if d["waqf"]}
    reason = {k: "true waqf, %.2fs of silence" % d["sustain"]
              for k, d in wd.items() if d["waqf"]}

    def bounds():
        out, lo = [], 0
        for k in sorted(cuts):
            out.append((lo, k))
            lo = k + 1
        out.append((lo, len(events) - 1))
        return out

    changed = True
    while changed:
        changed = False
        for lo, hi in bounds():
            n = hi - lo + 1
            dur = events[hi]["t1"] - events[lo]["t0"]
            if n <= max_words and dur <= max_dur:
                continue
            # Never carve off a one-word or sub-second card. Without this the
            # over-length pass cascades on a passage that has no true waqf at
            # all: it splits, both halves are still over length, it splits
            # those, and al-anam-122-122 comes out with a card reading «فِى».
            inner = [k for k in range(lo, hi)
                     if k not in cuts and k in wd
                     and k - lo + 1 >= MIN_CARD_WORDS
                     and hi - k >= MIN_CARD_WORDS
                     and wd[k]["t"] - events[lo]["t0"] >= MIN_CARD_DUR
                     and events[hi]["t1"] - wd[k]["t"] >= MIN_CARD_DUR]
            if not inner:
                continue
            k = max(inner, key=lambda k: wd[k]["depth"])
            cuts.add(k)
            reason[k] = ("no true waqf here, but the card ran to %d words / "
                         "%.1fs -- split at the deepest dip it contains"
                         % (n, dur))
            changed = True
            break
    return bounds(), reason


def split_lines(words):
    """One card's display words -> one or two caption lines.

    Width proxy is the bare letter count (diacritics add no advance width to
    speak of), and the objective is to minimise the WIDER line: that is what
    keeps the pill from blowing out and the shrink-to-fit from dragging the
    whole clip's point size down. Where two splits tie, the one that fits
    within MAX_LINE_WORDS per line wins.

    This is a typographic rule, not a linguistic one. A human setting these by
    hand breaks on sense ("...a Messenger" / "from among yourselves"), and the
    two rules agree less often than you would hope -- see the report.
    """
    n = len(words)
    lens = [len(Q.normalize(w).replace(" ", "")) for w in words]
    if n <= MAX_LINE_WORDS and sum(lens) + n - 1 <= 20:
        return [" ".join(words)]
    best = None
    for k in range(1, n):
        a, b = lens[:k], lens[k:]
        w1 = sum(a) + len(a) - 1
        w2 = sum(b) + len(b) - 1
        ok = (k <= MAX_LINE_WORDS and n - k <= MAX_LINE_WORDS)
        score = (0 if ok else 1, max(w1, w2), -k)
        if best is None or score < best[0]:
            best = (score, k)
    k = best[1]
    return [" ".join(words[:k]), " ".join(words[k:])]


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------

def plan(src, surah, a, b, start, end, workdir, pad=A.PAD,
         head_target=HEAD_TARGET):
    """Align + snap the window. -> a dict describing the finished clip.

    All times inside are WAV-relative until the very end, where `abs_*` and
    the phrase times (relative to segment.start) are computed.
    """
    al = A.run_window(src, start, end, surah, a, b, workdir, pad=pad)
    ev = al["events"]
    if not ev:
        raise SystemExit("alignment produced no events -- is the window right?")
    times, db, speech = al["times"], al["db"], al["speech"]
    floor = E.floor_level(db)

    wd = word_dips(ev, al["dips"])
    bounds, reason = split_cards(ev, wd)

    # --- head: pull segment.start back to just before the first attack
    on = E.onset(al["dips"], times, db, ev[0]["t0"], speech)
    seg_start = on - head_target

    # --- boundaries between cards
    edges = []
    for (lo1, hi1), (lo2, _) in zip(bounds, bounds[1:]):
        bd = dict(wd[hi1])
        bd["reason"] = reason.get(hi1, "")
        # The swap-in time is where the envelope comes back up -- except at a
        # non-waqf boundary, where there is no "back up" to speak of and the
        # dip is only 50 ms wide. There, hold the caption until just before
        # the next word's attack, which is the latest defensible moment.
        bd["t0"] = max(bd["t0"], ev[lo2]["t0"] - 0.05)
        edges.append(bd)

    # --- tail: end inside the gap that follows the last word
    tl = E.tail(al["dips"], ev[-1]["t1"])
    if tl is None:
        tl = {"t": ev[-1]["t1"], "t1": ev[-1]["t1"], "t0": ev[-1]["t1"],
              "depth": 0.0, "sustain": 0.0, "waqf": False}
    seg_end = E.gap_mid(tl)

    cards = []
    for n, (lo, hi) in enumerate(bounds):
        words = [al["ref"][e["ref"]]["ar"] for e in ev[lo:hi + 1]]
        t0 = edges[n - 1]["t0"] if n else on
        t1 = edges[n]["t1"] if n < len(edges) else tl["t1"]
        cards.append({
            "lo": lo, "hi": hi, "words": words,
            "ayah": al["ref"][ev[lo]["ref"]]["ayah"],
            "t0": t0, "t1": t1,
            "in": edges[n - 1] if n else None,
            "out": edges[n] if n < len(edges) else None,
            "guessed": [k for k, e in enumerate(ev[lo:hi + 1]) if e["sim"] is None],
        })

    return {
        "align": al, "events": ev, "cards": cards, "edges": edges,
        "speech_db": speech, "floor_db": floor, "dips": al["dips"],
        "wav_t0": al["wav_t0"],
        "onset": on, "seg_start": seg_start, "seg_end": seg_end,
        "tail_depth": tl["depth"], "tail": tl,
        "abs_start": al["wav_t0"] + seg_start,
        "abs_end": al["wav_t0"] + seg_end,
        "abs_onset": al["wav_t0"] + on,
        "surah": surah, "ayah_range": (a, b),
        "jumps": al["jumps"],
    }


# ---------------------------------------------------------------------------
# yaml
# ---------------------------------------------------------------------------

def _wrap(text, width=74, prefix="# "):
    out, line = [], []
    n = 0
    for w in text.split():
        if line and n + 1 + len(w) > width:
            out.append(prefix + " ".join(line))
            line, n = [w], len(w)
        else:
            n += (1 if line else 0) + len(w)
            line.append(w)
    if line:
        out.append(prefix + " ".join(line))
    return out


def clip_yaml(p, meta=None, style="bars"):
    """Render the plan as clip.yaml text, comments and all."""
    meta = dict(meta or {})
    L = []
    s, a, b = p["surah"], p["ayah_range"][0], p["ayah_range"][1]
    ay = "%d" % a if a == b else "%d-%d" % (a, b)
    src_url = meta.get("source_url")
    L.append('source_url: "%s"' % src_url if src_url
             else "# TODO source_url: the clip renders without it, the tracker does not\n"
                  'source_url: ""')
    dur = p["abs_end"] - p["abs_start"]
    L += _wrap("THIS clip = Surah %s ayah %s, at abs %s-%s in the source. "
               "Timings below were produced by `qc author`: mlx-whisper "
               "(large-v3-turbo) word timestamps over this window only, "
               "force-aligned onto the mushaf text with a repeat-permitting "
               "DP, then every boundary snapped to the deepest trough of a "
               "10 ms RMS envelope. Speech level in this window measures "
               "%.0f dB, room floor %.0f dB."
               % (Q.surah_name(s), ay, hhmmss(p["abs_start"]),
                  hhmmss(p["abs_end"]), p["speech_db"], p["floor_db"]))
    if style:
        L.append("style: %s" % style)
    rec = meta.get("reciter")
    if rec:
        L.append("reciter:")
        L.append('  name_en: "%s"' % rec.get("name_en", ""))
        if rec.get("name_ar"):
            L.append('  name_ar: "%s"' % rec["name_ar"])
    else:
        L.append("# TODO reciter: name_en / name_ar")
    bg = meta.get("video_bg")
    if bg:
        L.append("# Framing carried over unchanged from a shipped clip on this")
        L.append("# source -- crop solving is not this stage's job.")
        L.append("video_bg:")
        L.append("  mode: %s" % bg.get("mode", "live"))
        if bg.get("crop"):
            L.append("  crop:")
            for k in ("x", "y", "w", "h"):
                L.append("    %s: %s" % (k, bg["crop"][k]))
    else:
        L.append("# TODO video_bg: crop not solved by this stage")
    if meta.get("text"):
        L.append("text:")
        for k, v in meta["text"].items():
            L.append("  %s: %s" % (k, v))

    L.append("segment:")
    L += ["  " + x for x in _wrap(
        "onset abs %.2f; start %.2f -> onset rel %.2f (IG hook). End %.2f sits "
        "%.2f s after the last word, at the deepest point of the following "
        "gap (%.0f dB below speech)%s."
        % (p["abs_onset"], p["abs_start"], p["onset"] - p["seg_start"],
           p["abs_end"], p["seg_end"] - p["events"][-1]["t1"], p["tail_depth"],
           "" if p["tail_depth"] >= E.WAQF_DB else
           " -- SHALLOW: check by ear that nothing leaks into the tail"))]
    L.append('  start: "%s"   # absolute in source' % hhmmss(p["abs_start"]))
    L.append('  end: "%s"     # absolute in source; duration %.2fs'
             % (hhmmss(p["abs_end"]), dur))
    L.append("surah: %d" % s)
    L.append("ayah_range: [%d, %d]" % (a, b))

    for j in p["jumps"]:
        ref = p["align"]["ref"]
        frm = ref[min(j["from_ref"], len(ref) - 1)]["ar"]
        to = ref[min(j["to_ref"], len(ref) - 1)]["ar"]
        L += _wrap("RESTART detected by the alignment at rel %.2f: the "
                   "reference pointer jumps back from %s to %s -- ibtida'. "
                   "The repeated words are captioned as recited, not as "
                   "written."
                   % (j["t"] - p["seg_start"], frm, to))

    L.append("phrases:   # times RELATIVE to clip start")
    for n, c in enumerate(p["cards"]):
        bi = c["in"]
        if bi:
            flag = "" if bi["waqf"] else "  <-- NON-WAQF"
            L += ["  " + x for x in _wrap(
                "P%d->P%d%s: %s. The 50 ms RMS envelope troughs to %.0f dB at "
                "rel %.2f, %.0f dB below the %.0f dB speech level.%s"
                % (n, n + 1, flag, bi["reason"], bi["db"],
                   bi["t"] - p["seg_start"], bi["depth"], p["speech_db"],
                   "" if bi["waqf"] else
                   " That is continuous recitation, not a true waqf -- the swap "
                   "sits in the last dip before the next word, and a human "
                   "should confirm it reads cleanly."))]
        if c["guessed"]:
            L += ["  " + x for x in _wrap(
                "%d word(s) on this card were not emitted by the ASR "
                "(melisma); their times are interpolated." % len(c["guessed"]))]
        L.append("  - t0: %.2f" % (c["t0"] - p["seg_start"]))
        L.append("    t1: %.2f" % (c["t1"] - p["seg_start"]))
        L.append("    ayah: %d" % c["ayah"])
        if style == "bars":
            # WHERE the break falls is a judgement call, so it is made by a
            # model that only ever returns an INDEX -- see qc/author/linebreak.
            lines, why = LB.split(c["words"], split_lines, MAX_LINE_WORDS,
                                  use_llm=meta.get("llm_linebreak", True))
            if why:
                L.append("    # line break: %s" % why)
        else:
            lines, why = [" ".join(c["words"])], None
        if len(lines) == 1:
            L.append('    ar: "%s"' % Q.nfc(lines[0]))
        else:
            L.append('    ar1: "%s"' % Q.nfc(lines[0]))
            L.append('    ar2: "%s"' % Q.nfc(lines[1]))
        if style != "bars":
            L.append('    en: ""   # TODO: translation fragment for this card '
                     '-- splitting en.sahih to match an arbitrary phrase is a '
                     'human job, not an alignment one')
    return "\n".join(L) + "\n"


def tags_yaml(p, meta=None):
    meta = dict(meta or {})
    s, a, b = p["surah"], p["ayah_range"][0], p["ayah_range"][1]
    name = Q.surah_name(s).lower()   # folder/tag slug convention: at-tawbah
    en = Q.ayah(s, a)["en"]
    title = en.split(".")[0].strip()
    if len(title) > 95:
        title = title.split(",")[0].strip()
    rec = (meta.get("reciter") or {}).get("name_en", "")
    slug = rec.lower().replace(" ", "-") if rec else ""
    L = ["# Per-clip tags + post status. Edit `posted:` by hand (false/true) OR use",
         "#   /opt/homebrew/bin/python3 scripts/status.py post|unpost %s"
         % meta.get("folder", "<clip>"),
         "# The same script mirrors `tags` + a green Posted / red \"Not Posted\" marker onto",
         "# the folder's macOS Finder tags (visible/filterable in Finder).",
         "surah: %s" % name,
         "surah_number: %d" % s,
         "ayah_range: [%d, %d]" % (a, b),
         "reciter: %s" % (rec or "TODO"),
         'title: "%s"' % title.replace('"', "'"),
         'source_url: "%s"' % (meta.get("source_url") or ""),
         "tags:",
         "  - surah-%s" % name]
    if slug:
        L.append("  - %s" % slug)
    for t in meta.get("extra_tags", []):
        L.append("  - %s" % t)
    if meta.get("style"):
        L.append("  - style-%s" % meta["style"])
    L.append("# TODO topical tags (theme of the passage, venue) -- not derivable")
    L.append("posted: false")
    L.append('posted_at: ""')
    return "\n".join(L) + "\n"


def write(outdir, p, meta=None, style="bars"):
    os.makedirs(outdir, exist_ok=True)
    m = dict(meta or {})
    m.setdefault("style", style)
    m.setdefault("folder", os.path.basename(outdir.rstrip("/")))
    cp = os.path.join(outdir, "clip.yaml")
    tp = os.path.join(outdir, "tags.yaml")
    open(cp, "w", encoding="utf-8").write(clip_yaml(p, m, style=style))
    open(tp, "w", encoding="utf-8").write(tags_yaml(p, m))
    return cp, tp
