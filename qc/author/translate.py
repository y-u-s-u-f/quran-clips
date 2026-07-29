"""qc.author.translate -- the default style's `en:`, split to match the cards.

The landscape style draws a Sahih International fragment under every caption.
The translation itself is not a judgement call: `assets/quran/en.sahih.json` is
alquran.cloud's `en.sahih` edition, fetched once and committed beside the
Uthmani text exactly as the Arabic is (see qc.quran's module docstring), so
`qc.quran.ayah()['en']` already hands back the verified string offline. What IS
a judgement call is that A CARD IS USUALLY A FRAGMENT OF AN AYAH, and English
word order is not Arabic word order, so an ayah's translation has to be CUT to
match its cards. That cut is what this module decides.

    THE MODEL NEVER SEES ARABIC AND NEVER RETURNS TEXT. It is handed the
    ENGLISH words as a numbered list plus, for each card, HOW MANY Arabic
    words it carries and where they fall in the ayah -- counts and indices,
    no Arabic characters in either direction. It answers with cut indices
    into the numbered English, and this code assembles the fragments from the
    stored translation. So the same guarantee the line-breaker gives for the
    mushaf holds here: text corruption is structurally impossible, because no
    text is ever round-tripped. It reuses `linebreak.judge` -- same binary,
    same model, same effort -- rather than opening a second channel.

Failure is never silent. If the model is unavailable, times out, or answers
with anything but ascending in-range integers, the ayah is cut PROPORTIONALLY
(each card gets the share of the English words that matches its share of the
Arabic words) and the emitted clip.yaml says so in a comment, because a
proportional cut lands mid-clause often enough that a human has to look.
"""
import hashlib
import os

from .. import quran as Q
from . import align as A
from . import linebreak as LB

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(ROOT, "sources", "_entrans.json")

R_WHOLE = None                       # a whole ayah: nothing was decided
R_LLM = "LLM (clause seam)"
R_CACHE = "LLM (clause seam, cached)"
R_PROP = "PROPORTIONAL word split -- CHECK BY HAND (%s)"
R_TOOSHORT = ("PROPORTIONAL word split -- CHECK BY HAND (the translation has "
              "too few words to give every card its own fragment)")

PROMPT = """\
A verse of the Qur'an has been broken into %(ncards)d caption cards for a
video. Each card shows some of the verse's Arabic words, and under them the
matching part of the English translation (Saheeh International). Your job is
to say WHERE THE ENGLISH IS CUT. You are not translating and not rewriting
anything.

The English translation of the whole verse, numbered word by word:

%(list)s

The Arabic verse has %(nar)d words. The cards, in order, carry:

%(cards)s

Answer with %(ncuts)d cut point%(s)s: ascending integers, comma separated.
A cut at k means English word k ends one card's fragment and word k+1 begins
the next. Every cut must be between 1 and %(hi)d, and no two may be equal.

Splitting the English purely by proportion would cut at %(prop)s. That is the
right NEIGHBOURHOOD but rarely the right word: your job is to move each cut to
the nearest real clause boundary -- the end of a clause, before a conjunction
that opens the next one, before a quoted speech. A cut more than a few words
from its proportional position is almost certainly the wrong seam, because
the card it belongs to is only on screen while those Arabic words are recited.

Do not cut inside a noun phrase, between a preposition and its object, between
a verb and its auxiliary, or inside a quotation.

Output the integers and nothing else. No words, no explanation, no quotes.
"""


# ---------------------------------------------------------------------------
# the two ways to choose the cuts
# ---------------------------------------------------------------------------

def proportional(cuts_ar, n_ar, n_en):
    """Arabic cut positions -> English cut positions, by share of the verse.

    Monotone and strictly increasing: each cut is placed at its proportional
    spot, then pushed right far enough that every later cut still has a word
    left to take. Returns None when there simply are not enough English words.
    """
    m = len(cuts_ar)
    if n_en < m + 1:
        return None
    out = []
    for i, c in enumerate(cuts_ar):
        k = int(round(c * n_en / float(n_ar)))
        k = max(k, (out[-1] + 1) if out else 1)
        k = min(k, n_en - (m - i))
        out.append(k)
    return out


def _key(surah, ayah, cuts_ar, n_ar):
    h = hashlib.sha1(("%d:%d|%s|%d" % (surah, ayah,
                                       ",".join(str(c) for c in cuts_ar), n_ar)
                      ).encode("utf-8"))
    return h.hexdigest()


def _ask(en_words, spans, n_ar, cuts_ar, prop):
    """-> (cuts, reason) with cuts a list of English indices, or (None, reason).

    `spans` is [(w0, w1), ...], 0-based inclusive Arabic word indices per card.
    Only the numbers go to the model.
    """
    n_cuts = len(cuts_ar)
    hi = len(en_words) - 1
    if hi < n_cuts:
        return None, R_TOOSHORT
    cards = "\n".join(
        "  card %d: Arabic words %d-%d of %d (%.0f%% of the verse)"
        % (i + 1, w0 + 1, w1 + 1, n_ar, 100.0 * (w1 - w0 + 1) / n_ar)
        for i, (w0, w1) in enumerate(spans))
    prompt = PROMPT % {
        "ncards": len(spans),
        "list": "\n".join("%d. %s" % (i + 1, w) for i, w in enumerate(en_words)),
        "nar": n_ar,
        "cards": cards,
        "ncuts": n_cuts,
        "s": "" if n_cuts == 1 else "s",
        "hi": hi,
        "prop": ", ".join("word %d" % k for k in prop),
    }
    out, why = LB.judge(prompt)
    if out is None:
        # linebreak's own reason strings name the width heuristic it falls back
        # to, which is not what happens here; keep only the cause.
        return None, R_PROP % ("LLM unavailable" if why == LB.R_MISSING
                               else "LLM call failed")
    # Strict, exactly as the line-breaker is strict: the whole answer must be
    # the integers we asked for. A chattier answer is a failed instruction
    # follow, and guessing which numbers it meant is how a card ends up with
    # someone else's clause under it.
    parts = [p.strip() for p in out.replace(" ", ",").split(",") if p.strip()]
    if len(parts) != n_cuts or not all(p.isdigit() for p in parts):
        return None, R_PROP % ("LLM output did not parse as %d index(es)"
                               % n_cuts)
    cuts = [int(p) for p in parts]
    if any(not 1 <= k <= hi for k in cuts):
        return None, R_PROP % "LLM index out of range"
    if any(b <= a for a, b in zip(cuts, cuts[1:])):
        return None, R_PROP % "LLM indices were not ascending"
    return cuts, R_LLM


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def fragments(surah, ayah, spans, use_llm=True):
    """One ayah's cards -> [(english fragment, reason or None), ...].

    `spans` is [(w0, w1), ...] in card order: 0-based INCLUSIVE indices of the
    Arabic display words this card carries, within that ayah.

    A single card covering the whole ayah gets the whole translation and no
    comment -- that is the easy majority and nothing is decided. Otherwise the
    ayah's English is cut at the card seams; the cuts come from the model, or
    proportionally with a comment saying to check them.
    """
    v = Q.ayah(surah, ayah)
    en_words = v["en"].split()
    # DISPLAY words, counted exactly as the aligner counts them -- the mushaf
    # writes the waqf signs (ۖ ۗ ۚ ...) as separate whitespace-delimited
    # tokens, so `v['ar'].split()` returns 9 for 54:8 where the caption (and
    # the reference the spans index into) has 8. Counting them here shifted
    # every proportion in the ayah and dropped the last card's clause.
    n_ar = len(A.ref_words(surah, ayah, ayah))

    spans = [(max(0, min(w0, n_ar - 1)), max(0, min(w1, n_ar - 1)))
             for w0, w1 in spans]

    if len(spans) == 1 and spans[0][0] <= 0 and spans[0][1] >= n_ar - 1:
        return [(v["en"], R_WHOLE)]

    # Arabic cut positions: "this many words of the ayah are behind us". A
    # span that does not start at 0 or end at the last word contributes a cut
    # too -- the clip may open or close mid-ayah, and the English in front of
    # or behind the recited part then belongs to no card and is dropped.
    cuts_ar = sorted({w0 for w0, _ in spans if w0 > 0} |
                     {w1 + 1 for _, w1 in spans if w1 + 1 < n_ar})

    prop = proportional(cuts_ar, n_ar, len(en_words))
    cuts, reason = None, None
    if use_llm and prop is not None:
        key = _key(surah, ayah, cuts_ar, n_ar)
        cache = LB.cache_read(CACHE)
        hit = cache.get(key)
        if hit is not None and len(hit) == len(cuts_ar):
            cuts, reason = list(hit), R_CACHE
        else:
            cuts, reason = _ask(en_words, spans, n_ar, cuts_ar, prop)
            if cuts is not None:
                cache[key] = cuts
                LB.cache_write(cache, CACHE)
    elif prop is not None:
        reason = R_PROP % "LLM not requested"

    if cuts is None:
        cuts = prop
        if reason is None:
            reason = R_TOOSHORT

    if cuts is None:
        # Fewer English words than cards. Nothing can be cut without emitting
        # an empty `en:`, which the renderer would draw as a gap and `qc check`
        # rejects outright, so every card carries the whole verse and says so.
        return [(v["en"], R_TOOSHORT) for _ in spans]

    edge = {0: 0, n_ar: len(en_words)}
    for c, k in zip(cuts_ar, cuts):
        edge[c] = k

    out = []
    for w0, w1 in spans:
        a, b = edge[w0], edge[w1 + 1]
        out.append((" ".join(en_words[a:b]), reason))
    return out
