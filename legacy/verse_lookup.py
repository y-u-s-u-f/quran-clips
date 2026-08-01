#!/usr/bin/env python3
"""
Quran verse lookup: fuzzy-match a raw Arabic ASR transcript against the
full Qur'an corpus (quran-simple-clean, no diacritics) to find the best
matching surah:ayah.

Usage (library):
    from verse_lookup import match_verse
    match_verse("الم ذلك الكتاب لا ريب فيه هدى للمتقين")

Usage (CLI):
    python3 verse_lookup.py "الم ذلك الكتاب لا ريب فيه هدى للمتقين"
"""
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

QURAN_PATH = Path(__file__).parent / "quran-simple-clean.json"

_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_TATWEEL = re.compile(r"\u0640")
_NON_ARABIC = re.compile(r"[^\u0621-\u064A\s]")


def normalize(text: str) -> str:
    """Strip diacritics, tatweel, punctuation, normalize alef/hamza variants,
    and collapse whitespace so ASR output and reference text compare fairly."""
    text = _ARABIC_DIACRITICS.sub("", text)
    text = _TATWEEL.sub("", text)
    text = _NON_ARABIC.sub(" ", text)
    # normalize common letter variants
    text = text.replace("\u0622", "\u0627")  # آ -> ا
    text = text.replace("\u0623", "\u0627")  # أ -> ا
    text = text.replace("\u0625", "\u0627")  # إ -> ا
    text = text.replace("\u0629", "\u0647")  # ة -> ه
    text = text.replace("\u0649", "\u064A")  # ى -> ي
    text = re.sub(r"\s+", " ", text).strip()
    return text


_INDEX = None  # lazily built: list of (surah_num, surah_name, ayah_num, norm_text, raw_text)


def _build_index():
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    data = json.loads(QURAN_PATH.read_text(encoding="utf-8"))
    idx = []
    for surah in data["data"]["surahs"]:
        for ayah in surah["ayahs"]:
            raw = ayah["text"]
            idx.append((
                surah["number"],
                surah.get("englishName", surah.get("name", "")),
                surah.get("name", ""),
                ayah["numberInSurah"],
                normalize(raw),
                raw,
            ))
    _INDEX = idx
    return idx


def match_verse(transcript: str, top_k: int = 3, window_ayahs: int = 1):
    """
    Fuzzy-match a transcript against the Qur'an corpus.

    Returns a list of up to top_k matches, each:
        {
          "surah_number": int, "surah_name_ar": str, "surah_name_en": str,
          "ayah_number": int, "score": float (0-1), "text": str,
          "span": "N:M" or "N:M-N:K" if window_ayahs>1
        }
    sorted best-first. score is a similarity ratio (SequenceMatcher), not a
    calibrated probability -- treat >0.6 as a confident single-ayah match,
    and note transcripts spanning multiple ayat will score lower per-ayah
    (increase window_ayahs to test multi-ayah spans).
    """
    idx = _build_index()
    query = normalize(transcript)
    if not query:
        return []

    results = []
    n = len(idx)
    for i in range(n):
        surah_num, en_name, ar_name, ayah_num, norm_text, raw_text = idx[i]
        # also try concatenating with following ayahs for multi-ayah recitations
        texts = [norm_text]
        raws = [raw_text]
        spans = [f"{surah_num}:{ayah_num}"]
        if window_ayahs > 1:
            j = i
            combined_norm = norm_text
            combined_raw = raw_text
            for _ in range(window_ayahs - 1):
                j += 1
                if j >= n or idx[j][0] != surah_num:
                    break
                combined_norm += " " + idx[j][4]
                combined_raw += " " + idx[j][5]
                texts.append(combined_norm)
                raws.append(combined_raw)
                spans.append(f"{surah_num}:{ayah_num}-{surah_num}:{idx[j][3]}")

        for text, raw, span in zip(texts, raws, spans):
            score = SequenceMatcher(None, query, text).ratio()
            results.append({
                "surah_number": surah_num,
                "surah_name_en": en_name,
                "surah_name_ar": ar_name,
                "ayah_number": ayah_num,
                "score": round(score, 4),
                "text": raw,
                "span": span,
            })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_k]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 verse_lookup.py '<arabic transcript>' [window_ayahs]", file=sys.stderr)
        sys.exit(1)
    transcript = sys.argv[1]
    window = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    matches = match_verse(transcript, top_k=5, window_ayahs=window)
    for m in matches:
        print(f"[{m['score']:.3f}] {m['span']} ({m['surah_name_en']}): {m['text']}")
