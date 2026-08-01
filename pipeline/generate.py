"""pipeline/generate.py -- reel config YAML -> rendered reel.

    tools/render-venv/bin/python pipeline/generate.py sources/<id>/<reel>.yaml
    tools/render-venv/bin/python pipeline/generate.py --print-schema

This file owns everything the two styles share: config validation, verse
resolution, aligning the Whisper word timings against the known-correct
mushaf text, caption grouping, silence handling, suppress/nudge corrections
and verse-number ornaments. Rendering and compositing are the style files'
job -- render_default.py (landscape/vertical, Arabic + English) and
render_bars.py (vertical letterbox band, Arabic-only pills), dispatched on
the config's `style:` key.

    TEXT INTEGRITY. Caption Arabic is built by slicing the word list of
    `quran.ayah()` -- the committed Uthmani text -- never from the Whisper
    transcript and never retyped. The transcript's only job is TIMING.

Config resolution: `input` defaults to the source.* next to the config,
`whisper` to the whisper.json next to it, `output` to reels/<config-name>.mp4.
So the normal layout needs none of the three spelled out:

    sources/<id>/source.mp4
    sources/<id>/whisper.json      (from transcribe.py)
    sources/<id>/my-reel.yaml      ->  reels/my-reel.mp4
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import quran  # noqa: E402

try:
    import yaml
except ImportError:
    sys.exit("PyYAML not available -- run with tools/render-venv/bin/python")

FFMPEG = os.environ.get("QC_FFMPEG") or "ffmpeg"
FFPROBE = os.environ.get("QC_FFPROBE") or "ffprobe"

# A pause longer than this (seconds) is a deliberate rest in the recitation,
# not a breath: drop the caption and leave the frame clean until the reciter
# resumes, rather than holding stale text on screen for the whole silence.
MAX_HOLD = 3.0

DEFAULTS = {
    "style": "default",          # default | bars
    "input": None,               # media path; default: source.* beside config
    "whisper": None,             # word timings; default: whisper.json beside it
    "output": None,              # default: reels/<config-name>.mp4

    "surah": None,               # verse span; omit all three to auto-detect
    "ayah_start": None,          # from the whisper transcript (reliable only
    "ayah_end": None,            # for short 1-4 ayah windows)

    "groups": None,              # caption splits -- see --print-schema

    # signature is REQUIRED (no default): a string to burn into the bottom of
    # every frame, or null for none. Every config must decide explicitly.

    "trim": None,                # [start, end] seconds cut from source first
    "suppress": [],              # [[a, b]] windows left uncaptioned
    "nudge": [],                 # [{"group": i, "start": dt, "end": dt}]
    "verse_numbers": None,       # ayah ornament; default: true for `default`
                                 # style, false for bars

    "x_offset": 0,               # px from dead center; +right / +down.
    "y_offset": 0,               # 0/0 = subtitles centered (the default)
    "signature_offset": 0,       # px, vertical only: + lower / - higher.
                                 # The signature is ALWAYS horizontally
                                 # centered; there is no x knob on purpose.

    # default style only ------------------------------------------------
    "orientation": "auto",       # auto (from source aspect) | vertical | horizontal
    "detect_subject": True,      # face detection drives the 9:16 re-crop
    "background_image": None,    # still photo background; input may be audio
    "frame_size": [1080, 1920],  # output frame when background_image is set
    "arabic_font": "uthmanic_hafs",   # uthmanic_hafs | thuluth
    "english_font": "albertus",
    "arabic_scale": 1.1375,      # x base size of height * 0.09
    "english_scale": 1.0,        # x the locked 0.72 English:Arabic ratio
    "line_gap": 0.0,             # Arabic->English gap, frac of height
    "dim": 0.35,                 # video brightness under captions (0-1)
    "english_max_chars": 60,     # English wrap cap

    # bars style only ----------------------------------------------------
    "bar_color": None,           # "#RRGGBB"; default: derived from the footage
    "crop": None,                # {x,y,w,h} source-px window -> the 16:9 band
    "fx": None,                  # per-stage switches, e.g. {heat: false};
                                 # stages: grade scrim glow barglow textglow
                                 # scan snow heat (all on by default)

    "tmp_dir": None,             # default /tmp/quran-pipeline/<reel-name>
}


def config_schema():
    return """Per-reel config YAML, kept in the source's own folder.
`signature` is the only required key; with the standard layout everything
else has a working default.

    style: bars                     # bars | default
    signature: "TilawatQuraniyyah"  # REQUIRED. Burned bottom-center; null = none

    surah: 33                       # verse span. Omit all three to auto-detect
    ayah_start: 56                  # from whisper.json (short clips only)
    ayah_end: 56

    trim: [927.3, 957.3]            # seconds of source this reel covers.
                                    # Omit to use the whole source.

    groups:                         # caption cards, in Arabic word order.
      - n_words: 6                  # n_words must sum EXACTLY over the verse
        english: "Indeed, Allah..." # range's word count -- a mis-split fails
      - n_words: 5                  # loudly instead of shifting every caption.
        english: "..."              # english is used by `default` style only.
        line_split: 3               # bars: words on line 1 (omit = auto)
                                    # Omit groups for draft auto-grouping.

    x_offset: 0                     # subtitle offset in px from centered
    y_offset: 0                     # (+x right, +y down). Default centered.
    signature_offset: 0             # px: + lower / - higher. The signature is
                                    # ALWAYS horizontally centered.

    verse_numbers: true             # ayah ornament (default: on for `default`,
                                    # off for bars)
    suppress: [[33, 58]]            # leave these second-windows uncaptioned
    nudge:                          # per-caption timing fix, applied LAST
      - {group: 3, start: -1.8}

    # default style: orientation (auto|vertical|horizontal), detect_subject,
    # background_image + frame_size, arabic_font, english_font, arabic_scale,
    # english_scale, line_gap, dim, english_max_chars
    # bars style: bar_color ("#RRGGBB" | omit for auto), crop {x,y,w,h},
    #   fx: {heat: false, ...} to switch stages off (grade scrim glow
    #   barglow textglow scan snow heat; heat is ~half the render time)

    input / whisper / output        # only to override the standard layout

Generation prints a verification block: every card's Arabic beside its
authored English, then each verse in BOTH reference editions (Saheeh
International and Mufti Taqi Usmani). Check every card against both before
accepting a render -- where the two editions agree on clause order, a card
whose English contradicts them is mis-split.

Output resolution (`default` style): the caption canvas is never smaller
than 720 on its short side -- a low-quality source is upscaled to 720p
minimum -- and never larger than 1080 (a high-quality source caps at
1080p). `bars` output is always 1080x1920.
"""


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


# ---------- media ----------------------------------------------------------

def get_video_info(path):
    """Frame size + duration. An AUDIO-ONLY input (a recitation .m4a paired
    with a background_image) has no video stream, so width/height come back 0
    and the caller supplies the target frame size."""
    out = run([FFPROBE, "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height",
               "-show_entries", "format=duration", "-of", "json", path]).stdout
    data = json.loads(out)
    streams = data.get("streams") or []
    return {"width": int(streams[0]["width"]) if streams else 0,
            "height": int(streams[0]["height"]) if streams else 0,
            "duration": float(data.get("format", {}).get("duration", 0))}


def trim_media(path, out_path, start, end):
    """Cut [start, end) out of the source and re-encode. Re-encoding rather
    than stream-copying is deliberate: a copy cuts only at the nearest
    keyframe, which shifts the real start by up to a GOP and would offset
    every aligned word against the audio the viewer hears."""
    cmd = [FFMPEG, "-y", "-v", "error", "-ss", "%.3f" % start]
    if end is not None:
        cmd += ["-to", "%.3f" % end]
    cmd += ["-i", path]
    if get_video_info(path)["width"]:
        cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "16",
                "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-vn"]
    run(cmd + ["-c:a", "aac", "-b:a", "192k", out_path])
    return out_path


def extract_audio(src, out_wav):
    run([FFMPEG, "-y", "-i", src, "-ar", "16000", "-ac", "1", "-vn", out_wav])


# ---------- whisper words -> mushaf word timings ----------------------------

def load_whisper_window(whisper_path, t0, t1):
    """The transcript's words inside [t0, t1), times shifted to window-relative.

    whisper.json covers the WHOLE source; a reel covers its `trim` window.
    Words straddling an edge are kept and clamped -- dropping them would
    orphan the first or last caption word."""
    data = json.load(open(whisper_path, encoding="utf-8"))
    out = []
    for w in data["words"]:
        if w["t1"] <= t0 or (t1 is not None and w["t0"] >= t1):
            continue
        out.append({"w": w["w"],
                    "t0": max(0.0, w["t0"] - t0),
                    "t1": max(0.0, w["t1"] - t0)})
    return out, data.get("backend")


def align_words(ref_words, asr_words):
    """Time each mushaf display word off the Whisper words. -> [{"start","end"}]

    Needleman-Wunsch over the NORMALISED word sequences: the transcript is
    lossy (dropped/invented words, creative hamza spelling), so the mushaf
    text is matched against it rather than trusted from it. Exact normalised
    match scores highest, consonant-skeleton match catches the common ASR
    misspellings, and gaps absorb what the model dropped or hallucinated.
    Unmatched mushaf words get times interpolated between their matched
    neighbours -- every word must carry a time, because groups slice by index.
    """
    rn = [quran.normalize(w) or quran.normalize(w, keep_super_alif=True)
          for w in ref_words]
    an = [quran.normalize(w["w"]) for w in asr_words]
    n, m = len(rn), len(an)
    if m == 0:
        raise ValueError("no whisper words in the reel's window -- wrong trim, "
                         "or transcribe.py has not run")

    GAP = -0.6

    def sim(a, b):
        if not a or not b:
            return -0.4
        if a == b:
            return 2.0
        if quran.skeleton(a) == quran.skeleton(b):
            return 1.0
        return -0.5

    # score + backtrack ('d' diag, 'u' up = skip ref, 'l' left = skip asr)
    prev = [j * GAP for j in range(m + 1)]
    back = []
    for i in range(1, n + 1):
        cur = [i * GAP] + [0.0] * m
        row = [""] * (m + 1)
        for j in range(1, m + 1):
            d = prev[j - 1] + sim(rn[i - 1], an[j - 1])
            u = prev[j] + GAP
            l_ = cur[j - 1] + GAP
            best = max(d, u, l_)
            cur[j] = best
            row[j] = "d" if best == d else ("u" if best == u else "l")
        back.append(row)
        prev = cur

    matches = {}                       # ref index -> asr index
    i, j = n, m
    while i > 0 and j > 0:
        move = back[i - 1][j]
        if move == "d":
            if sim(rn[i - 1], an[j - 1]) > 0:
                matches[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif move == "u":
            i -= 1
        else:
            j -= 1

    stamps = [None] * n
    for ri, ai in matches.items():
        stamps[ri] = {"start": asr_words[ai]["t0"], "end": asr_words[ai]["t1"]}

    # Interpolate the unmatched: spread each hole linearly between the
    # bracketing matched times (window edges at the ends).
    if not matches:
        raise ValueError("alignment matched no words at all -- is the verse "
                         "span right for this window?")
    idx = 0
    while idx < n:
        if stamps[idx] is not None:
            idx += 1
            continue
        lo = idx - 1
        hi = idx
        while hi < n and stamps[hi] is None:
            hi += 1
        t_lo = stamps[lo]["end"] if lo >= 0 else max(
            0.0, stamps[hi]["start"] - 0.5 * (hi - idx + 1)) if hi < n else 0.0
        t_hi = stamps[hi]["start"] if hi < n else (
            stamps[lo]["end"] + 0.5 * (n - idx)) if lo >= 0 else 0.0
        span = max(0.0, t_hi - t_lo)
        k = hi - idx
        for step in range(k):
            a = t_lo + span * step / k
            b = t_lo + span * (step + 1) / k
            stamps[idx + step] = {"start": a, "end": b}
        idx = hi

    # Monotonic guarantee: a skeleton mismatch can land a match slightly out
    # of order; downstream grouping assumes non-decreasing time.
    t = 0.0
    for st in stamps:
        st["start"] = max(st["start"], t)
        st["end"] = max(st["end"], st["start"])
        t = st["end"]
    n_matched = len(matches)
    print("      aligned %d/%d mushaf words to the transcript (%d interpolated)"
          % (n_matched, n, n - n_matched))
    return stamps


def identify_verse_span(asr_words):
    """Fuzzy-match transcript thirds against the mushaf; return the min/max
    ayah matched. Reliable for short windows (1-4 ayahs) only."""
    text_tokens = []
    for w in asr_words:
        text_tokens.extend(quran.tokens(w["w"]))
    if not text_tokens:
        return None
    n_parts = max(1, min(4, len(text_tokens) // 8))
    size = len(text_tokens) // n_parts
    surahs, ayahs = {}, []
    for k in range(n_parts):
        part = text_tokens[k * size:(k + 1) * size if k < n_parts - 1 else None]
        hits = quran.search(part, top=1)
        if hits:
            s, a, score, _ = hits[0]
            if score < 0.25:
                continue
            surahs[s] = surahs.get(s, 0) + 1
            ayahs.append((s, a))
    if not ayahs:
        return None
    surah = max(surahs, key=surahs.get)
    nums = [a for s, a in ayahs if s == surah]
    return {"surah": surah, "ayah_start": min(nums), "ayah_end": max(nums)}


# ---------- reference text --------------------------------------------------

def fetch_verses(surah, a0, a1):
    """Uthmani Arabic + BOTH English editions per ayah (Saheeh International
    and Mufti Taqi Usmani), from the committed offline files -- never the
    network. Two translations so a card's English can be verified against
    both: one rendering can paraphrase in a way that hides a mis-split."""
    return [{"surah": v["surah"], "ayah": v["ayah"],
             "text_uthmani": quran.nfc(v["ar"]),
             "translation": v["en"], "translation2": v["en2"]}
            for v in quran.range(surah, a0, a1)]


def spoken_words(verses):
    """Flatten to the display-word list, dropping pure annotation tokens (the
    rub-el-hizb etc.) that no one speaks -- keeping them would offset display
    text against the timestamps."""
    return [w for v in verses for w in quran.display_words(v["text_uthmani"])]


# ---------- caption building ------------------------------------------------

def apply_groups(word_timestamps, words, groups, final_end):
    """Time config-supplied semantic groups into index-aligned
    (arabic, english) caption lists. n_words must sum to the word count
    exactly, or ValueError -- catches a bad grouping immediately instead of
    silently misaligning every later caption."""
    total = sum(g["n_words"] for g in groups)
    if total != len(word_timestamps):
        raise ValueError(
            "config groups cover %d words but the verse range has %d -- "
            "groups must partition every word exactly once"
            % (total, len(word_timestamps)))

    arabic, english, i = [], [], 0
    for g in groups:
        ts = word_timestamps[i:i + g["n_words"]]
        span = {"start": ts[0]["start"], "end": ts[-1]["end"]}
        arabic.append({**span, "text": " ".join(words[i:i + g["n_words"]]),
                       "line_split": g.get("line_split")})
        english.append({**span, "text": g.get("english", "")})
        i += g["n_words"]
    return _fill_gaps(arabic, english, final_end)


def auto_group(word_timestamps, words, verses, final_end,
               min_words=3, max_words=7, pause_gap=0.35):
    """Draft fallback: break at pauses within [min_words, max_words], then
    split each verse's translation proportionally. Arabic (VSO) and English
    (SVO) word order don't align 1:1, so the English can drift out of step
    with what's on screen -- use config `groups` for anything final."""
    arabic, current = [], []
    for i, (w, wt) in enumerate(zip(words, word_timestamps)):
        current.append((w, wt))
        last = i == len(word_timestamps) - 1
        pause = (not last
                 and word_timestamps[i + 1]["start"] - wt["end"] > pause_gap)
        if len(current) >= max_words or last or (len(current) >= min_words and pause):
            arabic.append({"start": current[0][1]["start"],
                           "end": current[-1][1]["end"],
                           "text": " ".join(w for w, _ in current),
                           "line_split": None})
            current = []

    word_verse = [v for v in verses
                  for _ in quran.display_words(v["text_uthmani"])]
    by_verse, i = {}, 0
    for ph in arabic:
        v = word_verse[i] if i < len(word_verse) else verses[-1]
        by_verse.setdefault((v["surah"], v["ayah"]), []).append(ph)
        i += len(ph["text"].split())

    english = [None] * len(arabic)
    for v in verses:
        group = by_verse.get((v["surah"], v["ayah"]), [])
        for ph, chunk in zip(group, _split_ratio(v["translation"], len(group))):
            english[arabic.index(ph)] = {**ph, "text": chunk}
    return _fill_gaps(arabic, english, final_end)


def _split_ratio(text, n):
    """Split into n roughly-equal word chunks, preferring a nearby
    comma/semicolon over the exact proportional boundary."""
    words = text.split()
    if n <= 1:
        return [text]
    if len(words) <= n:
        return words + [""] * (n - len(words))
    per, chunks, start = len(words) / n, [], 0
    for k in range(1, n):
        target = round(per * k)
        best = next((c for off in range(4) for c in (target + off, target - off)
                     if 0 < c < len(words) and words[c - 1].endswith((",", ";"))),
                    target)
        best = max(start + 1, min(best, len(words) - (n - len(chunks) - 1)))
        chunks.append(" ".join(words[start:best]))
        start = best
    chunks.append(" ".join(words[start:]))
    return chunks


def _fill_gaps(arabic, english, final_end):
    """Extend each caption's end toward the next one's start so text stays on
    screen through breath pauses -- but cap the hold at MAX_HOLD seconds past
    the caption's own spoken end. A pause longer than that is a deliberate
    rest, so the caption drops and the frame goes clean until the reciter
    resumes. Display timing only; the audio is never touched."""
    for i in range(len(arabic)):
        nxt = arabic[i + 1]["start"] if i + 1 < len(arabic) else final_end
        end = min(max(nxt, arabic[i]["end"]), arabic[i]["end"] + MAX_HOLD)
        arabic[i]["end"] = english[i]["end"] = end
    return arabic, english


# ---------- silences --------------------------------------------------------

def measure_noise_floor(wav_path, margin_db=12.0):
    """Silence threshold in dBFS, derived from the clip's own mean volume.

    A FIXED threshold cannot work here: recitation clips are mastered at
    wildly different levels, and a hardcoded -32 dB once classified nearly an
    entire recitation as "silence". Sitting the threshold `margin_db` BELOW
    the clip's own mean separates true rests from quiet passages regardless
    of mastering level."""
    proc = subprocess.run([FFMPEG, "-v", "info", "-i", wav_path, "-af",
                           "volumedetect", "-f", "null", "-"],
                          capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
    return (float(m.group(1)) - margin_db) if m else -45.0


def detect_silences(wav_path, min_len=MAX_HOLD, noise_db=None):
    """Silence windows as [(start, end), ...] via ffmpeg silencedetect; only
    runs of at least `min_len` are returned.

    Needed because alignment can stretch a word's end across a long rest --
    the aligner has no notion of "the reciter stopped here". Measured silence
    is the ground truth for where recitation actually stops."""
    if noise_db is None:
        noise_db = measure_noise_floor(wav_path)
    proc = subprocess.run([FFMPEG, "-v", "info", "-i", wav_path, "-af",
                           "silencedetect=noise=%.1fdB:d=%s" % (noise_db, min_len),
                           "-f", "null", "-"],
                          capture_output=True, text=True)
    spans, start = [], None
    for m in re.finditer(r"silence_(start|end): (-?[\d.]+)", proc.stderr):
        kind, val = m.group(1), float(m.group(2))
        if kind == "start":
            start = val
        elif start is not None:
            spans.append((start, val))
            start = None
    if start is not None:      # silence running to EOF emits no silence_end
        spans.append((start, None))
    return spans


def clip_to_speech(arabic, english, silences):
    """Pull any caption end back to the start of a long silence it runs into.
    TRAILING silence (end None, running to EOF) is skipped: the last caption
    stays up through the natural tail rather than vanishing the instant the
    final word decays."""
    for cap_a, cap_e in zip(arabic, english):
        for s_start, s_end in silences:
            if s_end is None:
                continue
            if cap_a["start"] < s_start < cap_a["end"]:
                cap_a["end"] = cap_e["end"] = max(cap_a["start"], s_start)
    return arabic, english


# ---------- corrections -----------------------------------------------------

def suppress_windows(arabic, english, windows):
    """Drop captions overlapping any [start, end] window, trimming partial
    overlaps. For stretches the automatic pause logic cannot detect -- a
    teaching aside, an interruption, audio that shouldn't be captioned."""
    if not windows:
        return arabic, english
    keep_a, keep_e = [], []
    for cap_a, cap_e in zip(arabic, english):
        start, end = cap_a["start"], cap_a["end"]
        for w_start, w_end in windows:
            if start >= w_start and end <= w_end:
                start = end = None
                break
            if w_start <= start < w_end:      # overlaps the window's head
                start = w_end
            elif w_start < end <= w_end:      # overlaps its tail
                end = w_start
            elif start < w_start and end > w_end:
                end = w_start                 # spans it: keep the leading part
        if start is None or end is None or end - start < 0.2:
            continue
        cap_a["start"], cap_a["end"] = start, end
        cap_e["start"], cap_e["end"] = start, end
        keep_a.append(cap_a)
        keep_e.append(cap_e)
    return keep_a, keep_e


def apply_nudges(arabic, english, nudges):
    """Shift individual captions' start/end by a signed delta, in seconds.

    Applied LAST, after gap filling, silence clipping and suppression, so
    nothing downstream can undo the hand correction. The canonical case:
    the reciter REPEATS a phrase, the aligner timed only one utterance, and
    the caption's start is pulled back to cover both. An overlap created by a
    nudge is resolved by moving the neighbour's edge, never by letting two
    cards share frames."""
    for nd in nudges:
        i = nd["group"]
        if not 0 <= i < len(arabic):
            raise ValueError("nudge group index %d out of range (0-%d)"
                             % (i, len(arabic) - 1))
        for key in ("start", "end"):
            if key not in nd:
                continue
            for track in (arabic, english):
                track[i][key] = max(0.0, track[i][key] + float(nd[key]))
        if arabic[i]["start"] >= arabic[i]["end"]:
            raise ValueError("nudge on group %d inverted its window "
                             "(%.2f >= %.2f)"
                             % (i, arabic[i]["start"], arabic[i]["end"]))
        if i and arabic[i - 1]["end"] > arabic[i]["start"]:
            for track in (arabic, english):
                track[i - 1]["end"] = arabic[i]["start"]
        if i + 1 < len(arabic) and arabic[i]["end"] > arabic[i + 1]["start"]:
            for track in (arabic, english):
                track[i + 1]["start"] = arabic[i]["end"]
    return arabic, english


# ---------- verse numbers ---------------------------------------------------

# Whether a font's shaping rules already draw the ayah ornament around bare
# Arabic-Indic digits, or need an explicit U+06DD ARABIC END OF AYAH prefix.
# These behave OPPOSITELY per font and getting it wrong renders two
# concentric rings: UthmanicHafs-v22 auto-encloses bare digits.
FONT_NEEDS_AYAH_MARK = {"uthmanic_hafs": False, "thuluth": False}

_ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤"
                                             "٥٦٧٨٩")


def verse_end_marker(ayah_num, arabic_font):
    digits = str(ayah_num).translate(_ARABIC_DIGITS)
    return ("۝" + digits) if FONT_NEEDS_AYAH_MARK.get(arabic_font, True) \
        else digits


def append_verse_numbers(arabic, verses, arabic_font):
    """Append each ayah's verse-number ornament to the caption that ends that
    ayah, matching printed Qur'an convention. Display text only, after
    alignment -- the ornament is never spoken."""
    ends, seen = {}, 0
    for v in verses:
        seen += len(quran.display_words(v["text_uthmani"]))
        ends[seen] = v["ayah"]
    consumed = 0
    for cap in arabic:
        consumed += len(cap["text"].split())
        if consumed in ends:
            cap["text"] = "%s %s" % (cap["text"],
                                     verse_end_marker(ends[consumed], arabic_font))
    return arabic


# ---------- verification report ---------------------------------------------

def print_verification(arabic, english, verses):
    """The card-by-card cross-check block: every card's Arabic beside its
    authored English, then each verse's TWO reference translations (Saheeh
    International and Mufti Taqi Usmani).

    This is what the operator (or the agent driving the pipeline) reads to
    verify that every frame's English matches its Arabic: a card's English
    must cover the same clause as its Arabic words, judged against BOTH
    editions -- where the two agree on clause order, a card whose English
    contradicts them is mis-split. The script can slice and time; only a
    reader can judge meaning, so the material is put in front of one."""
    print("      --- verify each card's English against its Arabic ---")
    for i, (a, e) in enumerate(zip(arabic, english)):
        print("      card %d [%6.2f-%6.2f]  %s" % (i + 1, a["start"], a["end"],
                                                   a["text"]))
        if e["text"]:
            print("             en: %s" % e["text"])
    print("      --- reference translations, per verse ---")
    for v in verses:
        print("      %d:%d [sahih] %s" % (v["surah"], v["ayah"],
                                          v["translation"]))
        if v.get("translation2"):
            print("      %d:%d [taqi ] %s" % (v["surah"], v["ayah"],
                                              v["translation2"]))


# ---------- orchestration ---------------------------------------------------

def load_config(path):
    raw = yaml.safe_load(open(path, encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("config %s is not a YAML mapping" % path)
    if "signature" not in raw:
        raise SystemExit(
            "config %s has no `signature:` key. It is required on every reel: "
            "a string to burn into the frame, or null for none." % path)
    unknown = sorted(set(raw) - set(DEFAULTS) - {"signature"})
    if unknown:
        raise SystemExit("unknown config key(s) in %s: %s -- a typo'd key "
                         "would be silently ignored otherwise"
                         % (path, ", ".join(unknown)))
    cfg = {**DEFAULTS, **raw}
    if cfg["style"] not in ("default", "bars"):
        raise SystemExit("style must be `default` or `bars`, not %r"
                         % cfg["style"])
    if cfg["verse_numbers"] is None:
        cfg["verse_numbers"] = cfg["style"] == "default"
    return cfg


def resolve_paths(cfg, config_path, output_override=None):
    src_dir = os.path.dirname(os.path.abspath(config_path))
    stem = os.path.splitext(os.path.basename(config_path))[0]

    if not cfg["input"]:
        for name in ("source.mp4", "source.mkv", "source.webm", "source.mov",
                     "source.m4a", "source.mp3", "source.wav"):
            p = os.path.join(src_dir, name)
            if os.path.exists(p):
                cfg["input"] = p
                break
        if not cfg["input"]:
            raise SystemExit("no `input` in config and no source.* in %s"
                             % src_dir)
    elif not os.path.isabs(cfg["input"]):
        cfg["input"] = os.path.join(src_dir, cfg["input"])

    if not cfg["whisper"]:
        cfg["whisper"] = os.path.join(src_dir, "whisper.json")
    elif not os.path.isabs(cfg["whisper"]):
        cfg["whisper"] = os.path.join(src_dir, cfg["whisper"])
    if not os.path.exists(cfg["whisper"]):
        raise SystemExit("%s not found -- run pipeline/transcribe.py %s first"
                         % (cfg["whisper"], src_dir))

    cfg["output"] = os.path.abspath(
        output_override or cfg["output"]
        or os.path.join(ROOT, "reels", stem + ".mp4"))
    cfg["tmp_dir"] = cfg["tmp_dir"] or os.path.join(
        "/tmp", "quran-pipeline", stem)
    if cfg["background_image"] and not os.path.isabs(cfg["background_image"]):
        cfg["background_image"] = os.path.join(src_dir, cfg["background_image"])
    return cfg


def run_config(config_path, output_override=None):
    cfg = load_config(config_path)
    cfg = resolve_paths(cfg, config_path, output_override)
    tmp = cfg["tmp_dir"]
    os.makedirs(tmp, exist_ok=True)
    os.makedirs(os.path.dirname(cfg["output"]), exist_ok=True)

    print("[1/6] Reading source...")
    source = cfg["input"]
    info = get_video_info(source)
    t0w = 0.0
    if cfg.get("trim"):
        t0, t1 = (list(cfg["trim"]) + [None])[:2]
        t0w = float(t0)
        trimmed = os.path.join(tmp, "trimmed.m4a" if not info["width"]
                               else "trimmed.mp4")
        print("      trimming to %s-%ss" % (t0, t1 if t1 is not None else "end"))
        source = trim_media(source, trimmed, float(t0),
                            None if t1 is None else float(t1))
        info = get_video_info(source)
    still = cfg.get("background_image")
    if still and not os.path.exists(still):
        raise SystemExit("background_image not found: %s" % still)
    if not info["width"] and not still:
        raise SystemExit("input has no video stream; set background_image to "
                         "render over a still photo")
    print("      %sx%s, %.1fs" % (info["width"] or "-", info["height"] or "-",
                                  info["duration"]))

    print("[2/6] Loading whisper words...")
    asr_words, asr_backend = load_whisper_window(
        cfg["whisper"], t0w, t0w + info["duration"] if cfg.get("trim") else None)
    print("      %d words in window (backend %s)" % (len(asr_words), asr_backend))

    surah, a0, a1 = cfg["surah"], cfg["ayah_start"], cfg["ayah_end"]
    if surah is None:
        print("[3/6] Identifying verse span from the transcript...")
        span = identify_verse_span(asr_words)
        if span is None:
            raise SystemExit("could not auto-identify the verse span; set "
                             "surah/ayah_start/ayah_end in the config")
        surah, a0, a1 = span["surah"], span["ayah_start"], span["ayah_end"]
        print("      detected %s %d:%d-%d"
              % (quran.surah_name(surah), surah, a0, a1))
    else:
        a0 = int(a0 if a0 is not None else 1)
        a1 = int(a1 if a1 is not None else a0)
        print("[3/6] Verse span from config: %s %d:%d-%d"
              % (quran.surah_name(surah), surah, a0, a1))

    verses = fetch_verses(int(surah), a0, a1)
    words = spoken_words(verses)

    print("[4/6] Aligning %d mushaf words..." % len(words))
    stamps = align_words(words, asr_words)

    print("[5/6] Building captions...")
    if cfg.get("groups"):
        print("      %d config groups" % len(cfg["groups"]))
        arabic, english = apply_groups(stamps, words, cfg["groups"],
                                       info["duration"])
    else:
        print("      no groups in config -- automatic grouping (draft quality)")
        arabic, english = auto_group(stamps, words, verses, info["duration"])

    wav = os.path.join(tmp, "audio.wav")
    extract_audio(source, wav)
    silences = detect_silences(wav)
    if silences:
        print("      %d long pause(s) >= %ss: %s"
              % (len(silences), MAX_HOLD,
                 ", ".join("%.1f-%.1fs" % (a, b) if b else "%.1fs-end" % a
                           for a, b in silences)))
        arabic, english = _fill_gaps(
            *clip_to_speech(arabic, english, silences), info["duration"])
    if cfg["suppress"]:
        before = len(arabic)
        arabic, english = suppress_windows(arabic, english, cfg["suppress"])
        print("      suppressed %s -> %d caption(s) dropped"
              % (cfg["suppress"], before - len(arabic)))
    if cfg["nudge"]:
        arabic, english = apply_nudges(arabic, english, cfg["nudge"])
        print("      %d caption nudge(s) applied" % len(cfg["nudge"]))
    if cfg["verse_numbers"]:
        arabic = append_verse_numbers(arabic, verses, cfg["arabic_font"])
    gaps = sum(1 for i in range(len(arabic) - 1)
               if arabic[i + 1]["start"] - arabic[i]["end"] > 0.05)
    print("      %d captions, %d pause gap(s) left clean" % (len(arabic), gaps))
    print_verification(arabic, english, verses)

    plan = {"cfg": cfg, "src": source, "info": info, "wav": wav,
            "arabic": arabic, "english": english, "verses": verses,
            "tmp": tmp, "out": cfg["output"]}

    print("[6/6] Rendering (%s style)..." % cfg["style"])
    if cfg["style"] == "bars":
        import render_bars
        render_bars.render(plan)
    else:
        import render_default
        render_default.render(plan)
    print("Done: %s" % cfg["output"])
    return {"surah": int(surah), "ayah_start": a0, "ayah_end": a1,
            "captions": len(arabic), "output": cfg["output"],
            "style": cfg["style"],
            "signature": cfg["signature"]}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="render a reel from a per-reel YAML config")
    ap.add_argument("config", nargs="?", help="sources/<id>/<reel>.yaml")
    ap.add_argument("-o", "--output", help="override the output path")
    ap.add_argument("--print-schema", action="store_true")
    a = ap.parse_args(argv)
    if a.print_schema:
        print(config_schema())
        return 0
    if not a.config:
        ap.error("config path required (or --print-schema)")
    result = run_config(a.config, a.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
