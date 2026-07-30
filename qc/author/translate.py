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
with anything but ascending in-range integers, the cut is made without it --
and the emitted clip.yaml names which of the two lower paths made it, because
they are not equally trustworthy:

  WORD-ANCHORED. `qc.quran.words()` carries one English gloss per display word,
  so the seam can be aimed at where the gloss of the card's boundary word falls
  in the translation, instead of at a share of the word count. It lands on that
  word when the gloss can be located and the cut is free to go there; when the
  nearest locatable gloss is a word or two late, or when keeping the cuts
  ascending and every card non-empty clamps the seam (`word_anchored` below
  does both), it lands NEAR it rather than on it. So this is better aimed than
  arithmetic, not a guarantee -- and whether the anchor word is also a clause
  boundary is luck regardless. Hence it still gets a comment.
  PROPORTIONAL. The last resort, when the glosses cannot line up with the ayah
  or the seam's own words go unlocated: each card gets the share of the English
  words that matches its share of the Arabic words. It is arithmetic over a
  translation whose word order is not the Arabic's, so it lands mid-clause
  often enough that a human has to look, and the comment says so.

The model's prompt still quotes the PROPORTIONAL position as its hint, not the
anchored one. It is calibrated against that neighbourhood ("a cut more than a
few words from its proportional position is almost certainly the wrong seam"),
and re-anchoring the hint would silently move the ruler the model is judged by.
"""
import hashlib
import os
import re

from .. import quran as Q
from . import align as A
from . import linebreak as LB

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(ROOT, "sources", "_entrans.json")

R_WHOLE = None                       # a whole ayah: nothing was decided
R_LLM = "LLM (clause seam)"
R_CACHE = "LLM (clause seam, cached)"
R_ANCHOR = ("WORD-ANCHORED word split -- cut at the gloss of this card's last "
            "Arabic word, or as near it as an ascending cut allows; not "
            "necessarily a clause seam (%s)")
R_PROP = "PROPORTIONAL word split -- CHECK BY HAND (%s)"
# Kept as one string because it is also the reason on the give-up path below,
# where every card carries the whole verse and nothing was split at all.
C_TOOSHORT = ("the translation has too few words to give every card its own "
              "fragment")
R_TOOSHORT = R_PROP % C_TOOSHORT

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
# the three ways to choose the cuts
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


# Function words say nothing about WHICH Arabic word an English word renders --
# "of" turns up in half the glosses of a long ayah and a dozen times in its
# translation -- so they must not be allowed to choose the alignment. They are
# not thrown away either: plenty of seams fall on a gloss that is nothing BUT
# function words (9:128 cuts between "Grievous" and "to him"), and a dropped
# glue token there costs the seam its anchor and slides the cut five words
# right. They stay in the sequence at a fraction of the weight, so content
# words choose the path and the glue follows it.
_GLUE = frozenset("""a an the of to in on at is are was were be been being am
 and or but so then that this those these his her hers its it their them they
 he she we you i me my your our us him not no with from for by as
 will would do does did has have had there which who whom what when o""".split())

_WORD = re.compile("[a-z]+")

# How far past the seam the first locatable gloss may sit. 0 means the seam's
# own word was found in the translation, which it is for ~81% of cuts, and ~96%
# are within 1 (measured over a random sample of the mushaf). Beyond 2 the
# words at the seam went unmatched entirely and the cut would be placed by
# whatever turned up next -- under 1% of cuts, and exactly the case where
# arithmetic is no worse than a guess dressed up as evidence.
_SLACK = 2


def _en_tokens(items):
    """A list of English strings -> [(token, index of the string it came from)].

    Letters only and lowercased, so "All-Hearing" meets "all hearing" and
    "permission?" meets "permission"; a trailing -s comes off long words
    because the gloss and the translation disagree about number often enough
    ("the believers" against "believers") to matter and never disagree about
    the stem.
    """
    out = []
    for i, s in enumerate(items):
        for t in _WORD.findall(s.lower()):
            out.append(((t[:-1] if len(t) > 4 and t.endswith("s") else t), i))
    return out


def _pair_words(glosses, en_words):
    """Align the glosses to the translation -> [(gloss index, english index)].

    A weighted longest common subsequence over the two token streams. Both
    sides say the same thing in the same ORDER; they differ in English word
    order locally, in the words the translation supplies and the gloss does
    not, and in wording ("the Most Gracious" against "the Entirely Merciful").
    LCS is the right shape precisely because it can only match FORWARD: an
    alignment that crossed itself would let one card's fragment begin before
    the previous card's fragment ends, which is not a fragment any more.
    """
    g, e = _en_tokens(glosses), _en_tokens(en_words)
    n, m = len(g), len(e)
    w = [1.0 if t not in _GLUE else 0.15 for t, _ in g]
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row, nxt = dp[i], dp[i + 1]
        for j in range(m - 1, -1, -1):
            row[j] = (nxt[j + 1] + w[i] if g[i][0] == e[j][0]
                      else max(nxt[j], row[j + 1]))
    out = []
    i = j = 0
    while i < n and j < m:
        if g[i][0] == e[j][0] and dp[i][j] >= dp[i + 1][j + 1] + w[i] - 1e-9:
            out.append((g[i][1], e[j][1]))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return out


def word_anchored(surah, ayah, cuts_ar, n_ar, en_words):
    """Arabic cut positions -> English cut positions, one word at a time.

    -> (cuts, None), or (None, why) when the glosses cannot carry this ayah.
    Same contract as proportional(): the English is cut where the ARABIC is,
    monotone and strictly increasing, every card left at least one word.

    The cut for Arabic position c is where the first locatable gloss of word c
    onwards lands in the translation, so the words the aligner could not place
    fall to the card BEFORE the seam. That is the conservative side: they are
    the supplied words of the clause that is already on screen ("[he is]",
    "[all]"), and holding them back would open the next card with them.
    """
    gl = Q.words(surah, ayah)
    if gl is None:
        return None, "no word-by-word glosses line up with this ayah"
    if len(gl) != n_ar:
        # Q.words() already refuses a table it cannot line up, and it counts
        # display words the same way ref_words does. If the two ever drift,
        # every gloss past the drift is someone else's word: bail, do not cut.
        return None, ("%d glosses for %d display words" % (len(gl), n_ar))
    m, n_en = len(cuts_ar), len(en_words)
    if n_en < m + 1:
        return None, C_TOOSHORT
    pairs = _pair_words(gl, en_words)
    out = []
    for i, c in enumerate(cuts_ar):
        anchor = next(((aj, ej) for aj, ej in pairs if aj >= c), None)
        if anchor is None or anchor[0] - c > _SLACK:
            return None, ("the English of Arabic word %d could not be located"
                          % (c + 1))
        k = max(anchor[1], (out[-1] + 1) if out else 1)
        k = min(k, n_en - (m - i))
        out.append(k)
    return out, None


def _key(surah, ayah, cuts_ar, n_ar):
    h = hashlib.sha1(("%d:%d|%s|%d" % (surah, ayah,
                                       ",".join(str(c) for c in cuts_ar), n_ar)
                      ).encode("utf-8"))
    return h.hexdigest()


def _ask(en_words, spans, n_ar, cuts_ar, prop):
    """-> (cuts, None) with cuts a list of English indices, or (None, cause).

    `spans` is [(w0, w1), ...], 0-based inclusive Arabic word indices per card.
    Only the numbers go to the model.

    The failure return is a bare CAUSE, not a finished reason string: what the
    caller falls back to is decided after this returns, and only the caller
    knows whether the cut ended up anchored or proportional.
    """
    n_cuts = len(cuts_ar)
    hi = len(en_words) - 1
    if hi < n_cuts:
        return None, C_TOOSHORT
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
        return None, ("LLM unavailable" if why == LB.R_MISSING
                      else "LLM call failed")
    # Strict, exactly as the line-breaker is strict: the whole answer must be
    # the integers we asked for. A chattier answer is a failed instruction
    # follow, and guessing which numbers it meant is how a card ends up with
    # someone else's clause under it.
    parts = [p.strip() for p in out.replace(" ", ",").split(",") if p.strip()]
    if len(parts) != n_cuts or not all(p.isdigit() for p in parts):
        return None, "LLM output did not parse as %d index(es)" % n_cuts
    cuts = [int(p) for p in parts]
    if any(not 1 <= k <= hi for k in cuts):
        return None, "LLM index out of range"
    if any(b <= a for a, b in zip(cuts, cuts[1:])):
        return None, "LLM indices were not ascending"
    return cuts, None


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def fragments(surah, ayah, spans, use_llm=True):
    """One ayah's cards -> [(english fragment, reason or None), ...].

    `spans` is [(w0, w1), ...] in card order: 0-based INCLUSIVE indices of the
    Arabic display words this card carries, within that ayah.

    A single card covering the whole ayah gets the whole translation and no
    comment -- that is the easy majority and nothing is decided. Otherwise the
    ayah's English is cut at the card seams: by the model, else anchored on the
    word-by-word glosses, else proportionally -- each with a comment naming
    which, so the clip.yaml keeps a durable record of how good the cut is.
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
    cuts, reason, cause = None, None, None
    if use_llm and prop is not None:
        key = _key(surah, ayah, cuts_ar, n_ar)
        cache = LB.cache_read(CACHE)
        hit = cache.get(key)
        if hit is not None and len(hit) == len(cuts_ar):
            cuts, reason = list(hit), R_CACHE
        else:
            cuts, cause = _ask(en_words, spans, n_ar, cuts_ar, prop)
            if cuts is not None:
                reason = R_LLM
                cache[key] = cuts
                LB.cache_write(cache, CACHE)
    elif prop is not None:
        cause = "LLM not requested"

    if cuts is None:
        # The model did not decide this one, so say who did. The glosses put
        # the seam on the right word when they can reach it; the arithmetic
        # only ever puts it in the right neighbourhood, and the comment it
        # carries has to keep saying so.
        anchored, why = word_anchored(surah, ayah, cuts_ar, n_ar, en_words)
        cause = cause or C_TOOSHORT   # only reachable when prop is None too
        if anchored is not None:
            cuts, reason = anchored, R_ANCHOR % cause
        else:
            cuts, reason = prop, R_PROP % ("%s; %s" % (cause, why))

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
