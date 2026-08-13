"""pipeline/quran.py -- offline mushaf: verse fetch, Arabic search, translation.

The JSON files under `assets/quran/` are committed copies of alquran.cloud's
`quran-uthmani`, `en.sahih` and `ar.muyassar` editions -- the last being the
King Fahd Complex tafsir, read only by `tafsir()`, for post descriptions and
never for anything on screen -- plus a word-by-word English gloss table. They
exist so no stage of the pipeline ever hits the network to learn what an ayah
says, and so anything put on screen can be checked against them character for
character.

    TEXT INTEGRITY. The Uthmani strings are load-bearing bytes, not prose.
    They carry codepoints (superscript alif U+0670, small high rounded zero
    U+06DF, small low meem U+06ED, the wasla alif U+0671, ...) that any
    "helpful" normalisation silently destroys. Nothing in this module ever
    rewrites the mushaf: `ayah()` hands back the stored string unmodified, and
    everything else (normalisation, search) works on throwaway copies.

The search side is deliberately blunt. Its consumers feed it lossy input --
YouTube auto-captions and Whisper transcripts have unreliable diacritics,
invented hamza placement, and words dropped or hallucinated during melismas.
Both sides are reduced to a bare letter skeleton and scored on shared rare
words plus word-order contiguity: enough to say "this is 9:128", never enough
to say "this is the exact wording". The exact wording always comes from
`ayah()`.

CLI:
    python3 pipeline/quran.py 33:56            one ayah (Arabic + translation)
    python3 pipeline/quran.py 55:1-13          an inclusive range
    python3 pipeline/quran.py --search "نص"    locate Arabic text in the mushaf
    python3 pipeline/quran.py --words 2:255    word-by-word English glosses
"""
import bisect
import json
import math
import os
import re
import sys
import unicodedata

_pyrange = range   # `range` is shadowed below by the public ayah-range helper

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QURAN_DIR = os.path.join(ROOT, "assets", "quran")

N_AYAT = 6236

_DATA = {}
_SURAH_NAMES = None


def _load(edition):
    if edition not in _DATA:
        path = os.path.join(QURAN_DIR, "%s.json" % edition)
        with open(path, encoding="utf-8") as f:
            _DATA[edition] = json.load(f)
    return _DATA[edition]


_OFFSETS = None    # surah -> flat index of its first ayah
_COUNTS = None     # surah -> ayah count
_STARTS = None     # the offsets in surah order, for from_flat's bisect


def _offsets():
    """Cumulative flat index of the first ayah of each surah (1-based surah).

    Cached in this module, never back into the loaded edition: the JSON dicts
    _load hands out are the mushaf, and a derived table does not belong among
    its keys."""
    global _OFFSETS, _COUNTS, _STARTS
    if _OFFSETS is None:
        offs, counts, n = {}, {}, 0
        for s in _load("uthmani")["surahs"]:
            offs[s["number"]] = n
            counts[s["number"]] = s["ayahs"]
            n += s["ayahs"]
        _OFFSETS, _COUNTS = offs, counts
        _STARTS = [offs[s] for s in sorted(offs)]
    return _OFFSETS


def ayah_count(surah):
    _offsets()
    return _COUNTS[int(surah)]


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
    """Inverse of flat_index: 0-based flat index -> (surah, ayah).

    Hot: search() calls it once per candidate ayah whenever a surah boost is
    in play, which is every call that a video title fed a hint to."""
    _offsets()
    s = bisect.bisect_right(_STARTS, idx)      # surahs starting at or before
    if not s:
        raise IndexError(idx)
    return s, idx - _STARTS[s - 1] + 1


def surah_name(n):
    global _SURAH_NAMES
    if _SURAH_NAMES is None:
        _SURAH_NAMES = {s["number"]: s["englishName"]
                        for s in _load("uthmani")["surahs"]}
    return _SURAH_NAMES[int(n)]


def surah_name_ar(n, plain=False):
    """The surah's Arabic name. `plain` drops the vocalisation, for text that
    is TYPED rather than recited -- an Instagram hashtag cannot carry harakat.

    NFC first: alquran.cloud stores surah 3's madda alif DECOMPOSED (U+0627
    U+0653), and stripping marks off that spelling turns Aal-\u02bfImran's
    name into two words spelled with a bare alif. Precomposed U+0622 is a
    letter, so it survives the strip.
    """
    name = _load("uthmani")["surahs"][int(n) - 1]["name"]
    if not plain:
        return name
    # U+0671 ALEF WASLA -> U+0627 ALEF: no keyboard types the wasla, so a
    # hashtag written with one matches nothing.
    return " ".join(_MARKS.sub("", nfc(name))
                    .replace("\u0671", "\u0627").split())


def tafsir(surah, num):
    """al-Tafsir al-Muyassar for one ayah, verbatim (King Fahd Complex).

    Never goes on screen: this is the ayah EXPLAINED in modern Arabic, for
    a post's description, where a caption carries the recited text itself.
    """
    return _load("ar.muyassar")["ayahs"][flat_index(surah, num)]


def basmalah():
    """The basmalah, verbatim -- surah 1 ayah 1, which is where it comes from.

    Every surah but 1 and 9 opens with it, but it is NOT part of their ayah 1
    and is not stored there; ask for it explicitly when you need it.
    """
    return _load("uthmani")["basmalah"]


def ayah(surah, num):
    """-> {'surah','ayah','ar','en','en2'}. `ar` is the stored Uthmani text,
    verbatim. `en` is Saheeh International, `en2` Mufti Taqi Usmani (None on
    a checkout that predates the second edition).

    TWO translations on purpose: a caption's English is verified against
    both, because one rendering can paraphrase in a way that hides a
    mis-split -- where the two agree on clause order, a card boundary that
    contradicts them is wrong."""
    i = flat_index(surah, num)
    try:
        en2 = _load("en.taqi")["ayahs"][i]
    except FileNotFoundError:
        en2 = None
    return {
        "surah": int(surah),
        "ayah": int(num),
        "ar": _load("uthmani")["ayahs"][i],
        "en": _load("en.sahih")["ayahs"][i],
        "en2": en2,
    }


def range(surah, a, b):  # noqa: A001 -- deliberate: quran.range(9, 128, 129)
    """Inclusive ayah range as a list of ayah() dicts."""
    a, b = int(a), int(b)
    if b < a:
        a, b = b, a
    return [ayah(surah, n) for n in _pyrange(a, b + 1)]


def display_words(text):
    """The whitespace tokens of an ayah that are actually WORDS.

    The mushaf writes the waqf and sajda signs (ۖ ۗ ۚ ۞ ...) as their own
    space-delimited tokens, so `ar.split()` over-counts. A token is a word only
    when normalising it leaves a letter behind.
    """
    out = []
    for w in nfc(text).split():
        if (normalize(w).replace(" ", "")
                or normalize(w, keep_super_alif=True).replace(" ", "")):
            out.append(w)
    return out


def words(surah, num):
    """-> the ayah's English word-by-word glosses, one per DISPLAY word, or None.

    None means the gloss table and the mushaf DISAGREE about how many words the
    ayah has (true for exactly one ayah, 37:130), or the optional en.wbw.json
    is simply not on disk. There is no partially usable answer in the mismatch
    case -- an offset table cuts every later gloss one word out -- so callers
    must fall back to something that does not need per-word English.
    """
    i = flat_index(surah, num)
    try:
        gl = _load("en.wbw")["words"][i]
    except FileNotFoundError:
        return None
    if len(gl) != len(display_words(_load("uthmani")["ayahs"][i])):
        return None
    return list(gl)


def nfc(text):
    """Canonical (NFC) form. See same_text()."""
    return unicodedata.normalize("NFC", text or "")


def same_text(a, b):
    """Are these the same Uthmani string, ignoring Unicode composition only?

    alquran.cloud writes the madda alif DECOMPOSED (U+0627 U+0653) while
    precomposed U+0622 renders identically through HarfBuzz -- different byte
    strings, same text. That is the ONLY licensed difference: compare under NFC
    and nothing else, so a dropped shadda or a normalised hamza still fails.
    """
    return nfc(a) == nfc(b)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

# Everything that is a mark rather than a letter: harakat/tanwin/shadda/sukun,
# the Quranic annotation block (superscript alif, waqf signs, small high
# rounded zero, small low meem), tatweel, and the combining ranges added for
# Quranic orthography.
_MARKS = re.compile(
    "[ؐ-ًؚ-ٰٟۖ-ۭ"
    "ـ࣓-ࣿﹰ-ﹿ]"
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
# LETTER: the Uthmani script writes the long /aa/ of ٱلرَّحْمَٰنِ and ذَٰلِكَ as a
# small alif above the line, and modern spelling resolves it inconsistently.
# Both readings are generated and indexed (see _index), so either convention
# matches.
_SUPER_ALIF = "ٰ"


def normalize(text, keep_super_alif=False):
    """Arabic text -> bare letter skeleton, for matching only. Never for display.

    Drops every diacritic and annotation mark, then folds the alif family,
    alif maqsura, ta marbuta and the hamza seats onto one representative
    letter each. The result is what an undiacriticised transcript would
    plausibly spell, so the mushaf and the transcript meet in one alphabet.
    """
    s = text or ""
    if keep_super_alif:
        s = s.replace(_SUPER_ALIF, "ا")
    s = _MARKS.sub("", s)
    s = "".join(_LETTER_MAP.get(c, c) for c in s)
    s = _NON_LETTER.sub(" ", s)
    return " ".join(s.split())


def tokens(text, keep_super_alif=False):
    """normalize() and split into word tokens.

    One orthographic split beyond whitespace: the Uthmani script writes the
    vocative particle JOINED to its noun (يَٰٓأَيُّهَا) while every modern
    transcription writes it separately (يا ايها). Splitting it on BOTH sides
    keeps mushaf and transcript in the same tokenisation -- without this the
    ~150 ayat that open يَٰٓأَيُّهَا lose their first word.
    """
    n = normalize(text, keep_super_alif=keep_super_alif)
    if not n:
        return []
    out = []
    for w in n.split():
        if w.startswith("يا") and len(w) >= 5:
            out.append("يا")
            out.append(w[2:])
        else:
            out.append(w)
    return out


# Weak/long letters. ASR spelling differs from the mushaf's mostly in these
# (mater lectionis written or not: داوود/داود, الصلوة/الصلاة). Removing them
# gives a coarse consonant skeleton used as a fuzzy fallback key.
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
    """Locate a sequence of Arabic word tokens in the mushaf.

    `toks`   a raw Arabic string (normalised for you) or a token list.
    `boost`  optional {surah: multiplier} -- how a video title's surah name is
             fed in. It nudges ranking; it never decides it on its own.
    `span`   optional (a, b): only consider these flat ayah indices.

    Returns [(surah, ayah, score, hits)] sorted by score, best first, where
    `score` is roughly the fraction of the query's information content found
    in that ayah and `hits` is how many query tokens matched. `detail` appends
    a fifth element: the frozenset of QUERY token indices that matched.

    Scoring: IDF-weighted token overlap, plus a bonus whenever query token i
    and i+1 land on consecutive positions of the same ayah. Word ORDER is what
    separates a real hit from an ayah that merely shares common words, and it
    survives ASR noise better than exact wording.
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
    punctuation made optional ("Surah At-Tawbah", "surat al tawba",
    "Attawbah") and the Arabic name as written in the mushaf. A HINT only:
    callers pass the result to search(boost=...), which lets transcript
    evidence override it.
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_ref(spec):
    s, _, rng = spec.partition(":")
    a, _, b = rng.partition("-")
    return int(s), int(a), int(b or a)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip().split("CLI:")[-1].strip())
        return 0
    if argv[0] == "--search":
        if len(argv) < 2:
            print("usage: quran.py --search <arabic text>", file=sys.stderr)
            return 2
        for s, a, score, hits in search(" ".join(argv[1:])):
            print("%-14s %d:%-4d score %.3f  (%d tokens matched)"
                  % (surah_name(s), s, a, score, hits))
        return 0
    if argv[0] == "--words":
        s, a, _ = _parse_ref(argv[1])
        gl = words(s, a)
        if gl is None:
            print("no word-by-word glosses for %d:%d" % (s, a), file=sys.stderr)
            return 1
        for w, g in zip(display_words(ayah(s, a)["ar"]), gl):
            print("%s\t%s" % (w, g))
        return 0
    s, a, b = _parse_ref(argv[0])
    for v in range(s, a, b):
        print("%s %d:%d" % (surah_name(v["surah"]), v["surah"], v["ayah"]))
        print(v["ar"])
        print("[sahih] %s" % v["en"])
        if v["en2"]:
            print("[taqi ] %s" % v["en2"])
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
