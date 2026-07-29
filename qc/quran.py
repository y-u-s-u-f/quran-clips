"""qc.quran -- offline mushaf: exact text lookup + fuzzy Arabic search.

The two JSON files under `assets/quran/` are committed copies of
alquran.cloud's `quran-uthmani` and `en.sahih` editions, fetched once and
stored verbatim. They exist so that no stage of the pipeline ever has to hit
the network to learn what an ayah says, and so that anything we put on screen
can be checked against them character for character.

    TEXT INTEGRITY. The Uthmani strings are load-bearing bytes, not prose.
    They carry codepoints (superscript alif U+0670, small high rounded zero
    U+06DF, small low meem U+06ED, the wasla alif U+0671, ...) that a model
    "helpfully" normalising the text will silently destroy -- SKILL.md:124-129
    records a run where WebFetch's summariser turned إِنِّى into إِنَّ. Nothing
    in this module ever rewrites the mushaf: `ayah()` hands back the stored
    string unmodified, and everything else (normalisation, search) works on
    throwaway copies.

Two things WERE changed when the API response was written to disk, both
recorded here because they are the only edits the data has ever had:

  * The API prefixes the BASMALAH to ayah 1 of every surah except 1 and 9,
    glued into the same string. Left in, `qc ayah 112:1` would put
    "بِسْمِ ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ قُلْ هُوَ ٱللَّهُ أَحَدٌ" on screen. It is
    stripped from those 112 ayat and kept once, verbatim, as `basmalah()`.
    (Surahs 95 and 97 carry it with a stray shadda on the ba -- بِّسْمِ -- which
    is an artifact of that edition, handled explicitly.)
  * A UTF-8 BOM the API emits on 1:1 only.

The search side is deliberately blunt. Its only consumer is caption/ASR
matching, where the input is already lossy -- YouTube's Arabic auto-captions
have no diacritics at all, guess hamza placement, and drop or invent whole
words during melismas. So we reduce both sides to a bare letter skeleton and
score on shared rare words plus word-order contiguity. That is enough to say
"this is 9:128" and nowhere near enough to say "this is the exact wording";
the exact wording always comes from `ayah()`.
"""
import json
import math
import unicodedata
import os
import re
import sys

_pyrange = range   # `range` is shadowed below by the public ayah-range helper

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QURAN_DIR = os.path.join(ROOT, "assets", "quran")

N_AYAT = 6236

# ---------------------------------------------------------------------------
# Surah names. scripts/status.py already owns the canonical 114-entry
# transliteration table (it is what reel filenames are built from, so the two
# must never drift). Import it rather than keeping a second copy; fall back to
# the editions' own englishName if that import is unavailable for any reason.
# ---------------------------------------------------------------------------


def _load_surah_names():
    try:
        if os.path.join(ROOT, "scripts") not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import status  # noqa: F401
        return dict(status.SURAH)
    except Exception:
        return None


_SURAH_NAMES = None
_DATA = {}


def _load(edition):
    if edition not in _DATA:
        path = os.path.join(QURAN_DIR, "%s.json" % edition)
        with open(path, encoding="utf-8") as f:
            _DATA[edition] = json.load(f)
    return _DATA[edition]


def _offsets():
    """Cumulative flat index of the first ayah of each surah (1-based surah)."""
    d = _load("uthmani")
    if "_offsets" not in d:
        offs = {}
        n = 0
        for s in d["surahs"]:
            offs[s["number"]] = n
            n += s["ayahs"]
        d["_offsets"] = offs
        d["_counts"] = {s["number"]: s["ayahs"] for s in d["surahs"]}
    return d["_offsets"]


def ayah_count(surah):
    _offsets()
    return _load("uthmani")["_counts"][int(surah)]


def flat_index(surah, num):
    """(surah, ayah) -> 0-based index into the flat 6236-ayah arrays."""
    surah, num = int(surah), int(num)
    offs = _offsets()
    if surah not in offs:
        raise KeyError("no surah %r" % surah)
    n = ayah_count(surah)
    if not 1 <= num <= n:
        raise KeyError("surah %d has %d ayat, not %d" % (surah, n, num))
    return offs[surah] + num - 1


def from_flat(idx):
    """Inverse of flat_index: 0-based flat index -> (surah, ayah)."""
    offs = _offsets()
    for s in _pyrange(114, 0, -1):
        if idx >= offs[s]:
            return s, idx - offs[s] + 1
    raise IndexError(idx)


def surah_name(n):
    global _SURAH_NAMES
    if _SURAH_NAMES is None:
        _SURAH_NAMES = _load_surah_names() or {
            s["number"]: s["englishName"] for s in _load("uthmani")["surahs"]
        }
    return _SURAH_NAMES[int(n)]


def surah_name_ar(n):
    return _load("uthmani")["surahs"][int(n) - 1]["name"]


def basmalah():
    """The basmalah, verbatim -- surah 1 ayah 1, which is where it comes from.

    Every surah but 1 and 9 opens with it, but it is NOT part of their ayah 1
    and is not stored there; ask for it explicitly when you need it.
    """
    return _load("uthmani")["basmalah"]


def ayah(surah, num):
    """-> {'surah','ayah','ar','en'}. `ar` is the stored Uthmani text, verbatim."""
    i = flat_index(surah, num)
    return {
        "surah": int(surah),
        "ayah": int(num),
        "ar": _load("uthmani")["ayahs"][i],
        "en": _load("en.sahih")["ayahs"][i],
    }


def nfc(text):
    """Canonical (NFC) form. See same_text()."""
    return unicodedata.normalize("NFC", text or "")


def same_text(a, b):
    """Are these the same Uthmani string, ignoring Unicode composition only?

    alquran.cloud writes the madda alif DECOMPOSED (U+0627 U+0653) while every
    clip.yaml in this repo carries it PRECOMPOSED (U+0622) -- the two render
    identically through HarfBuzz but are different byte strings. That is the
    ONLY licensed difference: this compares under NFC and nothing else, so a
    dropped shadda or a normalised hamza still fails, which is the point.
    Anything emitting clip.yaml text should pass ayah()['ar'] through nfc()
    first, to stay consistent with the existing clips.
    """
    return nfc(a) == nfc(b)


def range(surah, a, b):  # noqa: A001 -- deliberate: qc.quran.range(9, 128, 129)
    """Inclusive ayah range as a list of ayah() dicts."""
    a, b = int(a), int(b)
    if b < a:
        a, b = b, a
    return [ayah(surah, n) for n in _pyrange(a, b + 1)]


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

# Everything that is a mark rather than a letter: harakat/tanwin/shadda/sukun
# (U+064B-U+0652), the Quranic annotation block (U+0653-U+065F, U+0670,
# U+06D6-U+06ED, which is where superscript alif, the waqf signs, the small
# high rounded zero and the small low meem live), tatweel, and the Arabic-
# script combining ranges added for Quranic orthography.
_MARKS = re.compile(
    "[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED"
    "\u0640\u08D3-\u08FF\uFE70-\uFE7F]"
)
# Anything that is not an Arabic letter, after marks are gone.
_NON_LETTER = re.compile("[^ء-ؿف-يٱ-ە]")

_LETTER_MAP = {
    # every alif shape -> bare alif (ASR never gets hamza seats right)
    "آ": "ا", "أ": "ا", "إ": "ا",
    "ٱ": "ا", "ٲ": "ا", "ٳ": "ا",
    "ٵ": "ا", "ا": "ا",
    # alif maqsura -> ya
    "ى": "ي", "ئ": "ي",
    # ta marbuta -> ha
    "ة": "ه",
    # waw with hamza -> waw
    "ؤ": "و", "ۆ": "و", "ۇ": "و",
    # bare hamza carries no information once seats are flattened
    "ء": "",
    # Farsi/Urdu shapes that occasionally leak out of ASR
    "ک": "ك", "ی": "ي", "ھ": "ه",
}


# U+0670 ARABIC LETTER SUPERSCRIPT ALEF is the one mark that is really a
# LETTER: the Uthmani script writes the long /aa/ of ٱلرَّحْمَٰنِ, ذَٰلِكَ,
# ٱلسَّمَٰوَٰتِ and يَٰٓأَيُّهَا as a small alif above the line. Modern spelling
# resolves it inconsistently -- الرحمن and ذلك drop it, السماوات and يا ايها
# write it out -- so there is no single right answer. We therefore generate
# BOTH readings and index both (see _index), which costs a few thousand extra
# keys and makes either convention match.
_SUPER_ALIF = "\u0670"


def normalize(text, keep_super_alif=False):
    """Arabic text -> bare letter skeleton, for matching only. Never for display.

    Drops every diacritic and annotation mark, then folds the alif family,
    alif maqsura, ta marbuta and the hamza seats onto one representative
    letter each. The result is what YouTube's undiacriticised Arabic
    auto-captions would plausibly spell, so the mushaf and the captions meet
    in the same alphabet.
    """
    s = text or ""
    if keep_super_alif:
        s = s.replace(_SUPER_ALIF, "\u0627")
    s = _MARKS.sub("", s)
    s = "".join(_LETTER_MAP.get(c, c) for c in s)
    s = _NON_LETTER.sub(" ", s)
    return " ".join(s.split())


def tokens(text, keep_super_alif=False):
    """normalize() and split into word tokens.

    One orthographic split beyond whitespace: the Uthmani script writes the
    vocative particle JOINED to its noun (يَٰٓأَيُّهَا, يَٰقَوْمِ, يَٰبَنِىٓ) while
    every modern transcription -- including YouTube's captions -- writes it
    separately (يا ايها). Left alone this loses the whole opening of the ~150
    ayat that begin يَٰٓأَيُّهَا, which are exactly the ayat this account clips
    most. Splitting it on BOTH sides keeps mushaf and captions in the same
    tokenisation.
    """
    n = normalize(text, keep_super_alif=keep_super_alif)
    if not n:
        return []
    out = []
    for w in n.split():
        if w.startswith("\u064a\u0627") and len(w) >= 5:
            out.append("\u064a\u0627")
            out.append(w[2:])
        else:
            out.append(w)
    return out


# Weak/long letters. ASR spelling of a word differs from the mushaf's mostly in
# these (mater lectionis written or not: داوود/داود, هذا/هاذا, الصلوة/الصلاة).
# Removing them gives a coarse consonant skeleton used as a fuzzy fallback key.
_WEAK = str.maketrans("", "", "اويه")


def skeleton(word):
    s = word.translate(_WEAK)
    return s if len(s) >= 2 else word


# ---------------------------------------------------------------------------
# Index + search
# ---------------------------------------------------------------------------

_INDEX = None


def _index():
    """Build the inverted index over the normalised mushaf. ~0.4s, cached."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    ayahs = _load("uthmani")["ayahs"]
    norm = []
    exact = {}     # normalised word -> [(flat_idx, position), ...]
    skel = {}      # consonant skeleton -> [(flat_idx, position), ...]
    for i, a in enumerate(ayahs):
        ws = tokens(a)
        alt = tokens(a, keep_super_alif=True)
        norm.append(ws)
        seen = set()
        for p, w in enumerate(ws):
            forms = {w}
            if p < len(alt):
                forms.add(alt[p])
            for form in forms:
                if (form, p) in seen:
                    continue
                seen.add((form, p))
                exact.setdefault(form, []).append((i, p))
            skel.setdefault(skeleton(w), []).append((i, p))
    _INDEX = {"norm": norm, "exact": exact, "skel": skel}
    return _INDEX


def _idf(postings, n_docs=N_AYAT):
    # Postings are (ayah, position) pairs, so count distinct ayat.
    df = len({p[0] for p in postings}) or 1
    return math.log(1.0 + n_docs / float(df))


def search(toks, top=8, boost=None, span=None, detail=False):
    """Locate a sequence of normalised Arabic word tokens in the mushaf.

    `toks`   list of tokens (already normalize()d, or raw Arabic -- raw text is
             normalised for you if you pass a string).
    `boost`  optional {surah: multiplier} -- how a video title's surah name is
             fed in. It nudges ranking; it never decides it on its own.
    `span`   optional (a, b): only consider these flat ayah indices.

    Returns [(surah, ayah, score, hits)] sorted by score, best first, where
    `score` is roughly the fraction of the query's information content found
    in that ayah (0..~1.3 -- contiguity can push it over 1) and `hits` is how
    many query tokens matched.

    `detail` appends a fifth element: the frozenset of QUERY token indices that
    matched that ayah. A caller sliding a window over a transcript needs this
    to know WHICH of its tokens an ayah accounts for -- a 4-word ayah sharing a
    12-word window with its 8-word neighbour loses on score every time, and
    without the positions there is no way to see that it still owns the first
    four words (see qc.author.locate.match).

    Scoring: IDF-weighted token overlap, plus a bonus whenever query token i
    and i+1 land on consecutive positions of the same ayah. Word ORDER is what
    separates a real hit from an ayah that merely shares common words like
    الله / الذين / من, and it survives ASR noise better than exact wording.
    """
    if isinstance(toks, str):
        toks = tokens(toks)
    toks = [t for t in toks if t]
    if not toks:
        return []
    ix = _index()
    lo, hi = (span if span else (0, N_AYAT))

    votes = {}      # flat idx -> weight
    pairs = {}      # flat idx -> set of (query_i, ayah_pos)
    total_w = 0.0
    for qi, t in enumerate(toks):
        posts = ix["exact"].get(t)
        fuzzy = 0.6
        if not posts:
            posts = ix["skel"].get(skeleton(t))
            if not posts:
                total_w += 1.0     # an unmatchable token still costs us
                continue
        else:
            fuzzy = 1.0
        w = _idf(posts)
        total_w += w
        if len(posts) > 4000:      # a stopword-ish token: no discriminative value
            continue
        for idx, pos in posts:
            if not lo <= idx < hi:
                continue
            votes[idx] = votes.get(idx, 0.0) + w * fuzzy
            pairs.setdefault(idx, set()).add((qi, pos))

    if not votes:
        return []

    out = []
    for idx, v in votes.items():
        pr = pairs[idx]
        # contiguity: consecutive query tokens on consecutive ayah positions
        run = sum(1 for (qi, p) in pr if (qi + 1, p + 1) in pr)
        score = (v + run * 1.2) / max(total_w, 1e-6)
        if boost:
            s, _ = from_flat(idx)
            score *= boost.get(s, 1.0)
        qis = frozenset(qi for qi, _ in pr)
        out.append((idx, score, len(qis), qis))

    out.sort(key=lambda r: -r[1])
    res = []
    for idx, score, hits, qis in out[:top]:
        s, a = from_flat(idx)
        res.append((s, a, score, hits, qis) if detail else (s, a, score, hits))
    return res


def surah_from_title(title):
    """Surah numbers plausibly named in a video title -> {surah: 1.0}.

    Matches the transliterated English name with the definite article and
    punctuation made optional (\"Surah At-Tawbah\", \"surat al tawba\",
    \"Attawbah\") and the Arabic name as written in the mushaf. A HINT only:
    callers pass the result to search(boost=...), which lets caption evidence
    override it.
    """
    if not title:
        return {}
    t = re.sub(r"[^a-zء-ۿ ]+", " ", title.lower())
    t_sq = re.sub(r"[^a-zء-ۿ]+", "", title.lower())
    hits = {}
    for n in _pyrange(1, 115):
        en = surah_name(n).lower()
        forms = {en, re.sub(r"^(adh|ash|al|an|ar|as|at|az|ad)-", "", en),
                 en.replace("-", " "), en.replace("-", "")}
        for f in forms:
            f = f.strip()
            if len(f) < 3:
                continue
            if (" %s " % f) in (" %s " % t) or f.replace(" ", "") in t_sq:
                hits[n] = 1.0
                break
        ar = normalize(surah_name_ar(n))
        # mushaf names are "سُورَةُ ٱلْفَاتِحَةِ"-style; keep the last word
        ar_last = ar.split()[-1] if ar else ""
        if len(ar_last) >= 4 and ar_last in normalize(title):
            hits[n] = 1.0
    return hits
