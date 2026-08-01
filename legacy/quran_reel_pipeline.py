#!/usr/bin/env python3
"""
Qur'an video -> subtitled reel pipeline. Config-driven: one JSON per video.

  python3 quran_reel_pipeline.py config.json

Stages:
  1. Extract audio from the source video.
  2. Resolve the verse span (from config, or ASR + corpus fuzzy-match when
     absent -- auto-detection is reliable only for ~1-4 ayah clips).
  3. Fetch Uthmani Arabic + English translation per ayah (quran.com API).
  4. Forced-align the known-correct Arabic against the audio for word-level
     timestamps (more reliable than any ASR model's own timestamps).
  5. Build captions from config `groups` (semantic, agent-authored) or, if
     absent, automatic pause grouping + proportional English split (draft).
  6. Render Arabic as single-line transparent PNGs (PIL; see ARABIC_FONTS
     pitfall note) at one shared font size; English as an ASS track.
  7. Composite: dim video -> timed Arabic overlays -> burn English ASS.

See config_schema() for every config key, or run with --print-schema.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).parent
FONT_DIR = TOOL_DIR / "fonts"
sys.path.insert(0, str(TOOL_DIR))

# apt ffmpeg has libass/libfreetype/libharfbuzz; Homebrew/minimal builds on
# some hosts don't (no `subtitles`/`drawtext` filter). Prefer the apt path.
FFMPEG = "/usr/bin/ffmpeg" if Path("/usr/bin/ffmpeg").exists() else "ffmpeg"
FFPROBE = "/usr/bin/ffprobe" if Path("/usr/bin/ffprobe").exists() else "ffprobe"

# PITFALL: libass (ffmpeg's `subtitles` filter) has a confirmed rasterization
# bug on this stack with these Quranic Arabic fonts -- HarfBuzz shapes them
# correctly (verified via `hb-shape`) but libass renders disconnected
# placeholder boxes, reproduced even with an isolated single-font fontsdir.
# PIL (FreeType+Raqm, a different code path) renders the same files correctly,
# so Arabic goes to PNG via PIL + ffmpeg `overlay`, bypassing libass. English
# stays on the libass ASS path (plain Latin OpenType renders fine there).
ARABIC_FONTS = {
    "scheherazade": FONT_DIR / "ScheherazadeNew-Medium.ttf",
    "uthmanic_hafs": FONT_DIR / "UthmanicHafs-v22.ttf",
}
# English renders through libass, which needs the font FAMILY name (resolved
# from FONT_DIR via the `fontsdir` option), not a file path.
ENGLISH_FONTS = {
    "gentium": "Gentium Plus",
    "albertus": "Albertus MT Lt",
}

# Tajweed ANNOTATION marks (reading aids, not letters or vowels) that some
# Quranic fonts cannot mark-attach, emitting a U+25CC dotted-circle placeholder
# cluster instead. Confirmed for UthmanicHafs-v22: U+06DF renders as a stray
# ring mid-word. Stripped for display only -- the aligner sees separate text,
# and all letters + pronunciation diacritics are preserved.
#   U+06DF SMALL HIGH ROUNDED ZERO -- silent-alif aid (أَسْرَفُوا۟)
#   U+06ED SMALL LOW MEEM          -- iqlab/ikhfa aid (قَوْلًۭا)
# If a new font shows stray rings, dump codepoints >= 0x06D0 for the phrase
# and add the offender here.
DISPLAY_STRIP_MARKS = {"\u06df", "\u06ed"}

# TATWEEL (U+0640, kashida) is a pure letter-stretching character carrying no
# phonetic value, but it BREAKS mark anchoring in PIL/Raqm when it sits next to
# U+0670 ARABIC LETTER SUPERSCRIPT ALEF (the dagger alif): the tiny alif loses
# its anchor to the base letter and gets dumped at the far end of the word,
# looking like a stray extra letter. Confirmed on UthmanicHafs-v22 with
# ٱلشَّيْطَـٰنُ (al-Shaytan), whose Uthmani spelling is tatweel + dagger alif.
# Diagnostic that proved it: render the same word four ways (as-is / no tatweel /
# no dagger alif / plain alif) and compare -- only the tatweel variants break,
# and no dotted-circle appears, so the glyph exists and this is purely a
# positioning failure. Stripped for DISPLAY ONLY; the aligner never sees it and
# no letter or vowel is lost.
DISPLAY_STRIP_CHARS = DISPLAY_STRIP_MARKS | {"\u0640"}

# Shadow/border styling is intentionally NOT config-exposed -- it is a fixed
# house style, tuned once and verified. Arabic gets a soft alpha-derived drop
# shadow (no hard outline ring); English gets a light ASS border+shadow.
AR_SHADOW = {"opacity": 0.78, "blur": 6.5, "offset": 2, "color": (0, 0, 0)}
EN_BORDER = {"outline": 0.8, "shadow": 0.5}

# English size as a fraction of the FITTED Arabic size. Locked house ratio,
# approved on luqman-31-21 (Arabic 89px / English 64px). Deriving English from
# the fitted Arabic size -- rather than from frame height independently -- keeps
# the pair's relative weight constant even when a wide phrase shrinks the
# Arabic to fit. `english_scale` multiplies this if a clip needs a deviation.
EN_AR_SIZE_RATIO = 0.72

# Caption fade in/out, seconds. House style, not config-exposed: a hard cut
# between recitation cards reads as a jolt against sustained melodic recitation.
# Alpha reaches 0 at both window edges, which also guarantees two consecutive
# Arabic overlays can never both be opaque on a shared boundary frame.
FADE = 0.4

# Channel signature burned into EVERY reel, horizontally centered inside the
# bottom 10% of the frame. Fixed house branding, deliberately NOT config-exposed
# (only the on/off switch is): position/size were matched against the user's
# reference frame, where the line sits clear of the caption block and just above
# the lower edge. Rendered through libass with the English font -- plain Latin
# text, so the Arabic PNG detour isn't needed.
SIGNATURE_TEXT = "TilawatQuraniyyah"
SIGNATURE_SIZE_FRAC = 0.026   # of frame height
SIGNATURE_Y_FRAC = 0.945      # center of the line -> inside the bottom 10%
SIGNATURE_ALPHA = "&H20FFFFFF"  # ~87% opaque white, matching the reference

# A pause longer than this (seconds) is a deliberate rest in the recitation,
# not a breath: drop the caption and leave the frame clean until the reciter
# resumes, rather than holding stale text on screen for the whole silence.
MAX_HOLD = 3.0

# YuNet face detector (ONNX, via cv2.FaceDetectorYN), used to center a vertical
# crop on the reciter and to place captions clear of his face.
# PITFALL: OpenCV 5.x REMOVED cv2.CascadeClassifier, and opencv-python-headless
# ships cv2.data EMPTY on this box -- the old Haar-cascade route is dead on both
# counts. YuNet is a small DNN detector, more accurate than Haar on tilted heads
# and stage lighting, and it needs no cascade XML.
MODEL_DIR = TOOL_DIR / "models"
FACE_MODEL = MODEL_DIR / "face_detection_yunet_2023mar.onnx"
FACE_MODEL_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
                  "face_detection_yunet/face_detection_yunet_2023mar.onnx")

QURAN_API = "https://api.quran.com/api/v4/verses/by_key/{}:{}?translations={}&fields=text_uthmani"
_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")

DEFAULTS = {
    "translation_id": 20,        # 20 = Saheeh International
    "arabic_font": "uthmanic_hafs",
    "english_font": "gentium",
    "arabic_scale": 1.1375,      # x base size of height * 0.09
    "english_scale": 1.0,        # x EN_AR_SIZE_RATIO of the fitted Arabic size
    # "auto" reads the source aspect; "vertical" (9:16) re-crops a horizontal
    # source onto the detected face; "horizontal" (16:9) puts captions in the
    # emptier side margin.
    "orientation": "auto",
    "detect_subject": True,      # YuNet face detection drives crop + caption placement
    # Replace the moving video with one still photo, scaled to COVER the frame and
    # center-cropped. Audio always comes from `input`, which may then be an
    # audio-only file (.m4a/.mp3).
    "background_image": None,
    "frame_size": [1080, 1920],  # output frame when background_image is set
    # Cut [start, end] (seconds) out of the source before anything else; end may
    # be omitted/null for "to the end". Timestamps downstream are relative to the
    # trimmed media, so alignment stays correct.
    "trim": None,
    "signature": True,           # burn the fixed channel signature at the bottom
    # Layout offsets from dead center (positive x = right, positive y = down).
    # Left unset, auto_layout derives them from the detected subject; setting
    # either here pins it and overrides automatic placement.
    "line_gap": 0.0,             # vertical space between Arabic and English, frac of height
    "verse_numbers": True,       # append the ayah number in Arabic-Indic ornament
    # Windows (seconds) where captions are suppressed entirely, e.g.
    # [[33, 58]] to leave 0:33-0:58 clean. Use for non-recitation stretches
    # (teaching asides, silence) the automatic pause logic can't know about.
    "suppress": [],
    # Per-caption timing corrections applied LAST, e.g.
    # [{"group": 3, "start": -1.8}] pulls caption 3 (0-indexed) 1.8s earlier.
    # For repeated phrases the aligner only timed once, and for lead/trail tweaks.
    "nudge": [],
    "dim": 0.35,                 # video brightness under captions (0-1)
    "english_max_chars": 60,     # wrap width cap (also bounded by frame space)
    "tmp_dir": "/tmp/quran_reel_pipeline",
}


def config_schema() -> str:
    return """Per-video config JSON. Only `input` and `output` are required.

{
  "input":  "/path/to/source.mp4",      REQUIRED
  "output": "/path/to/result.mp4",      REQUIRED

  "surah": 39,                          verse span; omit all three to
  "ayah_start": 53,                     auto-detect via ASR (only reliable
  "ayah_end": 55,                       for ~1-4 ayah clips)

  "groups": [                           caption splits, in Arabic word order.
    {"n_words": 6, "english": "..."},   n_words must sum EXACTLY to the ayah
    {"n_words": 5, "english": "..."}    range's word count. Omit for draft
  ],                                    auto-grouping (lower quality).

  "translation_id": 20,                 quran.com translation (20 = Saheeh Intl)
  "arabic_font": "uthmanic_hafs",       "uthmanic_hafs" (default) | "scheherazade"
  "english_font": "gentium",            "gentium" | "albertus"
  "arabic_scale": 1.1375,               font size multiplier (base height*0.09)
  "english_scale": 1.0,                 multiplier on the LOCKED English:Arabic
                                        size ratio (0.72 of the fitted Arabic
                                        size). Leave at 1.0 unless a clip needs
                                        to deviate from house style.

  "orientation": "auto",                "auto" (from source aspect) | "vertical" | "horizontal"
  "detect_subject": true,               YuNet face detection -> crop + caption placement
  "background_image": null,             path to a photo used as a STATIC background for
                                        the whole reel (scaled to cover + center-cropped).
                                        Audio comes from `input`, which may be audio-only.
  "frame_size": [1080, 1920],           output frame when background_image is set
  "trim": [63.5, null],                 cut [start, end] seconds from the source first;
                                        null/omitted end = to the end of the media
  "signature": true,                    burn the fixed channel signature (bottom 10%)
  "verse_numbers": true,                append the ayah-number ornament per verse
  "suppress": [[33, 58]],               windows (s) to leave completely uncaptioned
  "nudge": [{"group": 3, "start": -1.8}],  shift one caption's start/end by a signed
                                        delta in seconds (0-indexed group). Applied
                                        last, so pause/silence logic can't undo it.
                                        Use for a phrase the reciter REPEATED (the
                                        aligner timed only one utterance) or a
                                        caption that should lead/trail slightly.

  Placement is AUTOMATIC from the detected subject:
    vertical   -> horizontally centered, block below the face
    horizontal -> emptier side margin (default left), vertically centered
  Set either offset to pin it manually and override auto layout:
  "x_offset": 0,                        px; negative = left, positive = right
  "y_offset": 0,                        px; negative = up, positive = down
  "line_gap": 0.0,                      space between the two lines, frac of height

  "dim": 0.35,                           video brightness 0-1, lower = dimmer
  "english_max_chars": 60,               English wrap width cap
  "tmp_dir": "/tmp/quran_reel_pipeline"  intermediate files
}

Fixed house style in the script, NOT config-exposed: shadow/outline, the
English:Arabic size ratio (EN_AR_SIZE_RATIO), caption fade in/out (FADE), and
the long-pause drop threshold (MAX_HOLD -- a pause longer than this clears the
caption instead of holding it).
Relative styling feedback ("more left", "5% dimmer") = adjust the value in
this config from its CURRENT setting."""


def strip_diacritics(text: str) -> str:
    return _DIACRITICS.sub("", text)


def strip_display_marks(text: str) -> str:
    return "".join(c for c in text if c not in DISPLAY_STRIP_CHARS)


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


# ---------- audio/video ----------

def extract_audio(video_path: str, out_wav: str):
    run([FFMPEG, "-y", "-i", video_path, "-ar", "16000", "-ac", "1", "-vn", out_wav])


def get_video_info(video_path: str) -> dict:
    """Frame size + duration. An AUDIO-ONLY input (a recitation .m4a paired with
    a `background_image`) has no video stream, so width/height come back 0 and
    the caller supplies the target frame size."""
    out = run([FFPROBE, "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height", "-show_entries", "format=duration",
               "-of", "json", video_path]).stdout
    data = json.loads(out)
    streams = data.get("streams") or []
    return {"width": int(streams[0]["width"]) if streams else 0,
            "height": int(streams[0]["height"]) if streams else 0,
            "duration": float(data.get("format", {}).get("duration", 0))}


def trim_media(path: str, out_path: str, start: float, end: float | None) -> str:
    """Cut [start, end) out of the source and re-encode. Re-encoding rather than
    stream-copying is deliberate: a copy cuts only at the nearest keyframe, which
    shifts the real start by up to a GOP and would offset every forced-alignment
    timestamp against the audio the viewer hears."""
    cmd = [FFMPEG, "-y", "-v", "error", "-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-to", f"{end:.3f}"]
    cmd += ["-i", path]
    if get_video_info(path)["width"]:
        cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-vn"]
    run(cmd + ["-c:a", "aac", "-b:a", "192k", out_path])
    return out_path


# ---------- verse identification (only when span absent from config) ----------

def transcribe_chunked(wav_path: str, chunk_s: float = 25.0, overlap_s: float = 3.0) -> str:
    """Transcribe in overlapping chunks with the Quran-specialized Tarteel
    Whisper model, for verse *identification* only (word timing comes from
    forced_align -- this model's own generation_config has no timestamp
    support and rejects language/task kwargs on modern transformers)."""
    from transformers import WhisperProcessor, WhisperForConditionalGeneration
    import librosa

    model_id = "tarteel-ai/whisper-base-ar-quran"
    processor = WhisperProcessor.from_pretrained(model_id)
    model = WhisperForConditionalGeneration.from_pretrained(model_id)

    audio, sr = librosa.load(wav_path, sr=16000)
    chunk_len, overlap = int(chunk_s * sr), int(overlap_s * sr)

    texts, pos = [], 0
    while pos < len(audio):
        end = min(pos + chunk_len, len(audio))
        inputs = processor(audio[pos:end], sampling_rate=16000, return_tensors="pt")
        raw = processor.batch_decode(model.generate(inputs.input_features, max_new_tokens=256),
                                     skip_special_tokens=True)[0]
        texts.append(raw.split("|>")[-1].strip() if "|>" in raw else raw.strip())
        if end == len(audio):
            break
        pos += chunk_len - overlap
    return " ".join(texts)


def identify_verse_span(transcript: str):
    """Fuzzy-match transcript thirds against the corpus; return the min/max
    ayah matched. Reliable for short clips (1-4 ayahs) only."""
    from verse_lookup import match_verse

    words = transcript.split()
    n_parts = max(1, min(4, len(words) // 8))
    parts = [" ".join(words[i * len(words) // n_parts:(i + 1) * len(words) // n_parts])
             for i in range(n_parts)]

    surah, ayahs = None, []
    for part in parts:
        if part.strip() and (m := match_verse(part, top_k=1, window_ayahs=1)):
            surah = surah or m[0]["surah_number"]
            ayahs.append(m[0]["ayah_number"])
    if surah is None or not ayahs:
        return None
    return {"surah": surah, "ayah_start": min(ayahs), "ayah_end": max(ayahs)}


def suppress_windows(arabic: list, english: list, windows: list) -> tuple:
    """Drop captions overlapping any [start, end] window (seconds), and trim
    those that only partly overlap. Used for stretches the automatic pause logic
    cannot detect -- a teaching aside, an interruption, audio the user simply
    doesn't want captioned. Captions fully inside a window are removed."""
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


# ---------- reference text ----------

def fetch_verse_range(surah: int, ayah_start: int, ayah_end: int, translation_id: int) -> list:
    """Uthmani Arabic + English translation per ayah from quran.com.
    Requires a User-Agent header or the API 403s. Strips footnote markup
    (`<sup foot_note=...>N</sup>`) INCLUDING its inner digit -- stripping
    tags alone leaves stray footnote numbers as visible caption text."""
    import urllib.request

    verses = []
    for ayah in range(ayah_start, ayah_end + 1):
        req = urllib.request.Request(QURAN_API.format(surah, ayah, translation_id),
                                      headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            verse = json.loads(resp.read().decode())["verse"]
        t = re.sub(r"<sup[^>]*>.*?</sup>", "", verse["translations"][0]["text"])
        t = re.sub(r"<[^>]+>", "", t).strip()
        verses.append({"surah": surah, "ayah": ayah, "text_uthmani": verse["text_uthmani"],
                        "translation": re.sub(r"\s+([.,;:])", r"\1", t)})
    return verses


def spoken_words(verses: list) -> list:
    """Flatten Uthmani text to a word list, dropping pure Quranic annotation
    (e.g. the rub-el-hizb '۞') -- those reduce to empty under diacritic
    stripping and the aligner never emits a timestamp for them, so keeping
    them would offset display text against the timestamps."""
    return [w for v in verses for w in v["text_uthmani"].split() if strip_diacritics(w).strip()]


# ---------- forced alignment ----------

def forced_align(wav_path: str, verses: list) -> list:
    """ctc-forced-aligner (MMS) word-level timestamps against the plain
    undiacritized Arabic. Returns [{start, end, text}, ...] in word order."""
    import ctc_forced_aligner as cfa
    import torch

    model, tokenizer = cfa.load_alignment_model("cpu", dtype=torch.float32)
    audio = cfa.load_audio(wav_path, model.dtype, model.device)
    plain = "\n".join(strip_diacritics(v["text_uthmani"]) for v in verses)

    emissions, stride = cfa.generate_emissions(model, audio, batch_size=8)
    tokens_starred, text_starred = cfa.preprocess_text(plain, romanize=True, language="ara")
    segments, scores, blank_id = cfa.get_alignments(emissions, tokens_starred, tokenizer)
    spans = cfa.get_spans(tokens_starred, segments, blank_id)
    return cfa.postprocess_results(text_starred, spans, stride, scores)


# ---------- caption building ----------

def apply_groups(word_timestamps: list, words: list, groups: list, final_end: float) -> tuple:
    """Time config-supplied semantic groups into index-aligned
    (arabic, english) caption lists. n_words must sum to the aligned word
    count exactly, or ValueError -- catches a bad grouping immediately
    instead of silently misaligning every later caption."""
    total = sum(g["n_words"] for g in groups)
    if total != len(word_timestamps):
        raise ValueError(f"config groups cover {total} words but forced alignment produced "
                          f"{len(word_timestamps)} -- groups must partition every word exactly once")

    arabic, english, i = [], [], 0
    for g in groups:
        ts = word_timestamps[i:i + g["n_words"]]
        span = {"start": ts[0]["start"], "end": ts[-1]["end"]}
        arabic.append({**span, "text": " ".join(words[i:i + g["n_words"]])})
        english.append({**span, "text": g["english"]})
        i += g["n_words"]
    return _fill_gaps(arabic, english, final_end)


def auto_group(word_timestamps: list, words: list, verses: list, final_end: float,
                min_words: int = 3, max_words: int = 7, pause_gap: float = 0.35) -> tuple:
    """Draft fallback: break at pauses within [min_words, max_words], then
    split each verse's translation proportionally. Arabic (VSO) and English
    (SVO) word order don't align 1:1, so the English can drift out of step
    with what's on screen -- use config `groups` for anything final."""
    arabic, current = [], []
    for i, (w, wt) in enumerate(zip(words, word_timestamps)):
        current.append((w, wt))
        last = i == len(word_timestamps) - 1
        pause = not last and word_timestamps[i + 1]["start"] - wt["end"] > pause_gap
        if len(current) >= max_words or last or (len(current) >= min_words and pause):
            arabic.append({"start": current[0][1]["start"], "end": current[-1][1]["end"],
                            "text": " ".join(w for w, _ in current)})
            current = []

    word_verse = [v for v in verses
                  for _ in range(sum(1 for w in v["text_uthmani"].split() if strip_diacritics(w).strip()))]
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


def _split_ratio(text: str, n: int) -> list:
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
                     if 0 < c < len(words) and words[c - 1].endswith((",", ";"))), target)
        best = max(start + 1, min(best, len(words) - (n - len(chunks) - 1)))
        chunks.append(" ".join(words[start:best]))
        start = best
    chunks.append(" ".join(words[start:]))
    return chunks


def measure_noise_floor(wav_path: str, margin_db: float = 12.0) -> float:
    """Silence threshold in dBFS, derived from the clip's own mean volume.

    PITFALL: a FIXED threshold cannot work here. Recitation clips are mastered
    at wildly different levels -- this stack's reference clip has a mean volume
    of -33.4 dBFS, so a hardcoded -32 dB threshold classified nearly the entire
    recitation as "silence" and would have blanked captions over live audio.
    Sitting the threshold `margin_db` BELOW the clip's own mean separates true
    rests from quiet passages regardless of mastering level."""
    proc = subprocess.run([FFMPEG, "-v", "info", "-i", wav_path, "-af", "volumedetect",
                           "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
    return (float(m.group(1)) - margin_db) if m else -45.0


def detect_silences(wav_path: str, min_len: float = MAX_HOLD,
                     noise_db: float | None = None) -> list:
    """Silence windows in the audio as [(start, end), ...], via ffmpeg
    silencedetect. Only runs of at least `min_len` are returned. The threshold
    defaults to the clip's measured noise floor (see measure_noise_floor).

    Needed because forced alignment can stretch a word's end across a long
    rest -- the aligner has no notion of "the reciter stopped here", so it
    happily extends the last word of a phrase to the resumption. Trusting the
    alignment alone left a caption on screen through a 23-second silence.
    Measured silence is the ground truth for where recitation actually stops."""
    if noise_db is None:
        noise_db = measure_noise_floor(wav_path)
    proc = subprocess.run([FFMPEG, "-v", "info", "-i", wav_path, "-af",
                           f"silencedetect=noise={noise_db:.1f}dB:d={min_len}", "-f", "null", "-"],
                          capture_output=True, text=True)
    spans, start = [], None
    for m in re.finditer(r"silence_(start|end): (-?[\d.]+)", proc.stderr):
        kind, val = m.group(1), float(m.group(2))
        if kind == "start":
            start = val
        elif start is not None:
            spans.append((start, val))
            start = None
    if start is not None:  # silence running to EOF emits no silence_end
        spans.append((start, None))
    return spans


def clip_to_speech(arabic: list, english: list, silences: list) -> tuple:
    """Pull any caption end back to the start of a long silence it runs into, so
    the caption drops when the reciter rests instead of hanging over dead air.

    TRAILING silence (end is None, i.e. running to EOF) is skipped: the last
    caption should stay up through the clip's natural tail rather than vanishing
    the instant the final word decays. A caption is never shortened below its
    own start."""
    for cap_a, cap_e in zip(arabic, english):
        for s_start, s_end in silences:
            if s_end is None:
                continue
            if cap_a["start"] < s_start < cap_a["end"]:
                cap_a["end"] = cap_e["end"] = max(cap_a["start"], s_start)
    return arabic, english


def _fill_gaps(arabic: list, english: list, final_end: float) -> tuple:
    """Extend each caption's end toward the next one's start so text stays on
    screen through breath pauses -- but cap the hold at MAX_HOLD seconds past
    the caption's own spoken end. A pause longer than that is a deliberate rest
    in the recitation, so the caption drops and the frame goes clean until the
    reciter resumes, instead of stale text sitting through a long silence.
    Display timing only; the video/audio is never touched."""
    for i in range(len(arabic)):
        nxt = arabic[i + 1]["start"] if i + 1 < len(arabic) else final_end
        end = min(max(nxt, arabic[i]["end"]), arabic[i]["end"] + MAX_HOLD)
        arabic[i]["end"] = english[i]["end"] = end
    return arabic, english


# Whether a font's shaping rules already draw the ayah ornament around bare
# Arabic-Indic digits, or need an explicit U+06DD ARABIC END OF AYAH prefix.
# PITFALL: these two behave OPPOSITELY, and getting it wrong is visible.
# UthmanicHafs-v22 auto-encloses bare digits, so adding U+06DD yields TWO rings
# (one empty ornament plus the numbered one). Scheherazade New does the reverse:
# bare digits render as plain unenclosed numerals and U+06DD is required.
# Verified by rendering all four combinations per font and inspecting.
FONT_NEEDS_AYAH_MARK = {"uthmanic_hafs": False, "scheherazade": True}


def apply_nudges(arabic: list, english: list, nudges: list) -> tuple:
    """Shift individual captions' start/end by a signed delta, in seconds.

    Applied LAST, after gap filling, silence clipping and suppression, so nothing
    downstream can undo the hand correction. Two cases this exists for, both of
    which forced alignment cannot infer:

    - The reciter REPEATS a phrase. The aligner sees the phrase once in the known
      Arabic and lands its timestamps on one of the two utterances, leaving the
      other uncaptioned. Pulling that caption's start back over the first
      utterance covers both.
    - A caption should simply lead or trail the spoken word slightly.

    A start pulled earlier into the previous caption is resolved by pulling the
    PREVIOUS caption's end back to the new start, never by letting two cards
    overlap: `composite` would render both on the shared frames.
    """
    for n in nudges:
        i = n["group"]
        if not 0 <= i < len(arabic):
            raise ValueError(f"nudge group index {i} out of range (0-{len(arabic) - 1})")
        for key in ("start", "end"):
            if key not in n:
                continue
            for track in (arabic, english):
                track[i][key] = max(0.0, track[i][key] + float(n[key]))
        if arabic[i]["start"] >= arabic[i]["end"]:
            raise ValueError(f"nudge on group {i} inverted its window "
                             f"({arabic[i]['start']:.2f} >= {arabic[i]['end']:.2f})")
        if i and arabic[i - 1]["end"] > arabic[i]["start"]:
            for track in (arabic, english):
                track[i - 1]["end"] = arabic[i]["start"]
        if i + 1 < len(arabic) and arabic[i]["end"] > arabic[i + 1]["start"]:
            for track in (arabic, english):
                track[i + 1]["start"] = arabic[i]["end"]
    return arabic, english


def verse_end_marker(ayah: int, arabic_font: str) -> str:
    """The ayah-number ornament for `ayah`, encoded the way `arabic_font` wants
    (see FONT_NEEDS_AYAH_MARK)."""
    digits = str(ayah).translate(str.maketrans("0123456789", "\u0660\u0661\u0662"
                                                "\u0663\u0664\u0665\u0666\u0667\u0668\u0669"))
    return ("\u06dd" + digits) if FONT_NEEDS_AYAH_MARK.get(arabic_font, True) else digits


def append_verse_numbers(arabic: list, verses: list, arabic_font: str) -> list:
    """Append each ayah's verse-number ornament to the caption that ends that
    ayah, matching printed Qur'an convention.

    Appended to DISPLAY text only, after alignment: the ornament is never spoken
    and adding it before `spoken_words()`/forced alignment would offset every
    word timestamp in the clip. Word counts are walked to find which caption
    holds each ayah's final word, so a caption spanning an ayah boundary gets the
    number in the middle where it belongs."""
    ends, seen = {}, 0
    for v in verses:
        seen += len([w for w in v["text_uthmani"].split() if strip_diacritics(w).strip()])
        ends[seen] = v["ayah"]

    consumed = 0
    for cap in arabic:
        consumed += len(cap["text"].split())
        if consumed in ends:
            cap["text"] = f"{cap['text']} {verse_end_marker(ends[consumed], arabic_font)}"
    return arabic


# ---------- rendering ----------

def shared_font_size(texts: list, font_path: str, base: int, max_width: int, floor: int = 20) -> int:
    """One size fitting every caption on a single line. Per-caption
    auto-shrink would make the Arabic visibly change size between captions,
    so the widest phrase sets the size for the whole clip."""
    from PIL import Image, ImageDraw, ImageFont

    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    size = base
    while size > floor:
        font = ImageFont.truetype(font_path, size)
        if max(probe.textbbox((0, 0), t, font=font)[2] for t in texts) <= max_width:
            return size
        size = int(size * 0.94)
    return size


def arabic_png_pad() -> int:
    """Transparent margin `render_arabic_png` adds around the glyphs so the
    drop shadow has room to blur into. It is part of every PNG's reported
    height, so layout must subtract it to position anything flush against the
    Arabic's real ink — otherwise `line_gap: 0` still leaves a visible slack
    band (measured at 31px before this was accounted for)."""
    return int(AR_SHADOW["blur"]) + AR_SHADOW["offset"] + 6


def render_arabic_png(text: str, font_path: str, size: int, out_path: str) -> tuple:
    """One Arabic line to a transparent PNG (PIL/FreeType+Raqm), with a soft
    alpha-derived drop shadow: paint a solid layer through the glyph alpha,
    offset + blur it, composite the ink over that. Reads as a subtle lift off
    the footage rather than an outline ring. Returns (width, height)."""
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    font = ImageFont.truetype(font_path, size)
    bbox = ImageDraw.Draw(Image.new("RGBA", (10, 10))).textbbox((0, 0), text, font=font)
    pad = arabic_png_pad()
    w, h = (bbox[2] - bbox[0]) + pad * 2, (bbox[3] - bbox[1]) + pad * 2

    ink = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(ink).text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=(255, 255, 255, 255))

    solid = Image.new("RGBA", (w, h), AR_SHADOW["color"] + (int(AR_SHADOW["opacity"] * 255),))
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow.paste(solid, (0, 0), ink.split()[3])
    off = -AR_SHADOW["offset"]
    shadow = shadow.transform((w, h), Image.AFFINE, (1, 0, off, 0, 1, off))
    shadow = shadow.filter(ImageFilter.GaussianBlur(AR_SHADOW["blur"] * 0.6))

    Image.alpha_composite(shadow, ink).save(out_path)
    return w, h


def wrap_by_chars(text: str, max_chars: int) -> list:
    """Greedy wrap driven by character count, breaking only between words."""
    lines, cur, n = [], [], 0
    for w in text.split():
        extra = len(w) + (1 if cur else 0)
        if cur and n + extra > max_chars:
            lines.append(" ".join(cur))
            cur, n = [w], len(w)
        else:
            cur.append(w)
            n += extra
    if cur:
        lines.append(" ".join(cur))
    return lines


def safe_char_count(center_x: int, width: int, margin: int, size: int, ratio: float = 0.32) -> int:
    """Max chars/line before clipping: a \\pos-centered block shifted off
    true center can only extend as far as the CLOSER screen edge on both
    sides. `ratio` (avg glyph width / point size) is calibrated from real
    rendered pixel width -- 0.32 for Gentium Plus."""
    usable = 2 * max(1, min(center_x - margin, (width - margin) - center_x))
    return max(10, int(usable / (size * ratio)))


ASS_TEMPLATE = """[Script Info]
ScriptType: v4.00+
WrapStyle: 0
PlayResX: {width}
PlayResY: {height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: English,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H30000000,0,0,0,0,100,100,0,0,1,{outline},{shadow},8,{margin},{margin},0,1
Style: Signature,{font},{sig_size},{sig_alpha},&H000000FF,&H00000000,&H30000000,0,0,0,0,100,100,{sig_spacing},0,1,0.6,0.4,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_time(seconds: float) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def build_english_ass(phrases: list, width: int, height: int, out_path: str,
                       font_name: str, size: int, center_x: int, top_y: int, max_chars: int,
                       duration: float = 0.0, signature: bool = True):
    """English ASS track, top-anchored (\\an8) at (center_x, top_y) so
    multi-line text grows downward only and can never collide with the Arabic
    sitting above it. Each event carries \\fad matching the Arabic overlay's
    fade so both lines of the pair appear and leave together.

    The channel signature rides in the same ASS file as one full-duration
    middle-anchored (\\an5) event in the bottom 10% of the frame -- a second
    subtitles filter pass would cost another full decode/encode of every frame
    for one static line."""
    margin = int(width * 0.06)
    chars = min(max_chars, safe_char_count(center_x, width, margin, size))
    fade_ms = int(FADE * 1000)
    sig_size = max(12, int(height * SIGNATURE_SIZE_FRAC))

    lines = [ASS_TEMPLATE.format(width=width, height=height, font=font_name, size=size,
                                  margin=margin, sig_size=sig_size,
                                  sig_alpha=SIGNATURE_ALPHA,
                                  sig_spacing=round(sig_size * 0.06, 1), **EN_BORDER)]
    for ph in phrases:
        text = "\\N".join(wrap_by_chars(ph["text"], chars))
        # Clamp so in+out can't exceed the event and the text always peaks
        # at full opacity, mirroring the Arabic fade clamp in composite().
        fade = min(fade_ms, max(0, int((ph["end"] - ph["start"]) * 500)))
        lines.append(f"Dialogue: 0,{ass_time(ph['start'])},{ass_time(ph['end'])},"
                      f"English,,0,0,0,,{{\\an8\\pos({center_x},{top_y})"
                      f"\\fad({fade},{fade})}}{text}\n")
    if signature and duration > 0:
        lines.append(f"Dialogue: 0,{ass_time(0)},{ass_time(duration)},"
                      f"Signature,,0,0,0,,{{\\an5\\pos({width // 2},"
                      f"{int(height * SIGNATURE_Y_FRAC)})}}{SIGNATURE_TEXT}\n")
    Path(out_path).write_text("".join(lines), encoding="utf-8")


def english_block_height(phrases: list, width: int, size: int, max_chars: int, center_x: int) -> int:
    """Tallest rendered English block in lines * line height, needed to center
    the Arabic+English pair vertically as one unit."""
    margin = int(width * 0.06)
    chars = min(max_chars, safe_char_count(center_x, width, margin, size))
    max_lines = max(len(wrap_by_chars(ph["text"], chars)) for ph in phrases)
    return int(max_lines * size * 1.25)


# ---------- subject detection (vision) ----------

def detect_subject(video_path: str, info: dict, samples: int = 5) -> dict | None:
    """Locate the speaker's face with the YuNet DNN detector over frames sampled
    across the clip, returning the MEDIAN box in pixels:
    {"cx", "cy", "w", "h", "bottom", "hits"}.

    Sampling several frames and taking the median rejects per-frame jitter and
    the occasional false positive on patterned fabric, which a one-frame probe
    would silently accept. Returns None when fewer than half the samples produced
    a detection -- callers must fall back to geometric centering rather than
    trusting a weak signal."""
    try:
        import cv2
    except ImportError:
        print("      opencv not installed -- skipping subject detection")
        return None

    if not FACE_MODEL.exists():
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        print(f"      fetching face model -> {FACE_MODEL.name}")
        try:
            run(["curl", "-sL", "-o", str(FACE_MODEL), FACE_MODEL_URL])
        except subprocess.CalledProcessError:
            print("      face model download failed -- skipping subject detection")
            return None

    # Detect on the full frame (no downscale): input size is set per frame so
    # returned coordinates are already in source pixels, no rescaling needed.
    detector = cv2.FaceDetectorYN.create(str(FACE_MODEL), "",
                                         (info["width"], info["height"]),
                                         score_threshold=0.6)
    tmp_png = str(Path(info.get("tmp_dir", "/tmp")) / "_probe.png")
    boxes = []
    for k in range(samples):
        t = info["duration"] * (k + 1) / (samples + 1)
        run([FFMPEG, "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", video_path,
             "-frames:v", "1", "-update", "1", tmp_png])
        frame = cv2.imread(tmp_png)
        if frame is None:
            continue
        _, faces = detector.detect(frame)
        if faces is not None and len(faces):
            # Largest detection per frame = the foreground speaker, not an
            # audience member's head at the frame edge.
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])[:4]
            boxes.append((int(x), int(y), int(w), int(h)))

    if len(boxes) * 2 < samples:
        print(f"      subject detection weak ({len(boxes)}/{samples} frames) -- ignoring")
        return None

    med = lambda vals: sorted(vals)[len(vals) // 2]
    x, y = med([b[0] for b in boxes]), med([b[1] for b in boxes])
    w, h = med([b[2] for b in boxes]), med([b[3] for b in boxes])
    return {"cx": x + w // 2, "cy": y + h // 2, "w": w, "h": h,
            "bottom": y + h, "hits": len(boxes)}


def vertical_crop_filter(info: dict, subject: dict | None, target_w: int = 1080,
                          target_h: int = 1920) -> tuple:
    """ffmpeg filter string cropping a horizontal source to 9:16 centered on the
    subject's face (geometric center when detection failed), plus the resulting
    crop window x. Cropping on the face rather than the frame center matters
    because a stage/interview shot rarely has the speaker dead center -- a
    naive center crop leaves him off to one side of the reel."""
    crop_w = int(info["height"] * target_w / target_h) // 2 * 2
    center = subject["cx"] if subject else info["width"] // 2
    x = max(0, min(info["width"] - crop_w, center - crop_w // 2))
    return (f"crop={crop_w}:{info['height']}:{x}:0,"
            f"scale={target_w}:{target_h}:flags=lanczos"), x


def auto_layout(info: dict, subject: dict | None, orientation: str) -> dict:
    """Caption placement derived from where the subject actually is.

    Vertical (9:16): horizontally centered, block top pushed BELOW the face so
    text never covers it. Horizontal (16:9): block goes in whichever side margin
    has more empty space beside the subject (tie or no detection -> left, the
    house default) and is centered vertically.

    Returns {"x_offset", "top_target"}. `x_offset` is in run_config's convention
    (px from dead center; negative = left). `top_target` is an ABSOLUTE desired y
    for the block's top, or None to keep it vertically centered -- it cannot be
    expressed as a y_offset here because run_config centers the block first, and
    the block's height is only known after the Arabic PNGs are rendered."""
    w, h = info["width"], info["height"]
    if orientation == "vertical":
        if not subject:
            return {"x_offset": 0, "top_target": None}
        # Sit the block's top a little under the chin: clears the face plus any
        # mic/boom hardware that typically sits just below it.
        return {"x_offset": 0,
                "top_target": min(int(h * 0.78), subject["bottom"] + int(subject["h"] * 0.45))}
    left_space = subject["cx"] - subject["w"] // 2 if subject else w // 2
    right_space = w - (subject["cx"] + subject["w"] // 2) if subject else w // 2
    side = "right" if right_space > left_space else "left"
    center = (w - right_space // 2) if side == "right" else left_space // 2
    return {"x_offset": center - w // 2, "top_target": None}


# ---------- compositing ----------

def _escape(path: str) -> str:
    return path.replace("\\", "\\\\").replace(":", "\\:")


def composite(video: str, ass_path: str, overlays: list, out_path: str, dim: float,
               duration: float, still_image: str | None = None,
               size: tuple | None = None):
    """Dim the source, fade each Arabic PNG in/out over its own window, overlay
    it, then burn the English ASS (faded via libass \\fad) on top.

    Each Arabic overlay gets `fade=in`+`fade=out:alpha=1` on its own PNG input
    rather than a shared binary `enable` gate, because `enable` cannot ramp
    alpha. The overlay is still bounded to its window so an invisible input
    never costs compositing time outside it.

    `still_image` swaps the moving background for one photo, scaled to COVER the
    target frame and center-cropped (never letterboxed), with audio taken from
    `video` -- which may then be an audio-only file. Done here rather than by
    pre-rendering a static background video because that intermediate would cost
    a second full-length encode of an entirely static image.

    PITFALL (kept for the record): ffmpeg's between() is INCLUSIVE on both ends
    while caption ends can equal the next caption's start, so a binary gate made
    consecutive Arabic overlays BOTH render on the shared boundary frame and
    visibly stack. The eps pullback below still enforces that, and the fades now
    make it doubly safe -- alpha reaches 0 at each window's edges, so even an
    exact boundary collision would composite nothing.
    """
    eps = 0.01
    if still_image:
        w, h = size or (1080, 1920)
        # -loop 1 on the photo + explicit -t (below) is what terminates the
        # graph; see the looped-input pitfall on the overlay inputs.
        cmd = [FFMPEG, "-y", "-loop", "1", "-framerate", "25", "-i", still_image,
               "-i", video]
        audio_map, base = "1:a", 2
        bg = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
              f"crop={w}:{h},setsar=1,format=rgba,"
              f"colorchannelmixer=rr={dim}:gg={dim}:bb={dim}[bg]")
    else:
        cmd = [FFMPEG, "-y", "-i", video]
        audio_map, base = "0:a", 1
        bg = f"[0:v]colorchannelmixer=rr={dim}:gg={dim}:bb={dim}[bg]"

    for ov in overlays:
        # -loop 1 turns each still PNG into a continuous stream so the fade
        # filter has a timeline to ramp alpha along; a single-frame input has no
        # duration for fade to act over.
        # PITFALL: a looped image input NEVER ends, and overlay's default
        # eof_action=repeat means the filtergraph has no EOF either -- the encode
        # runs forever and writes an ever-growing file with no moov atom. `-t`
        # bounds the output to the source duration, which is the fix; ffmpeg's
        # `-shortest` alone does NOT help here because no input is ever short.
        cmd += ["-loop", "1", "-i", ov["path"]]

    parts = [bg]
    label = "bg"
    for i, ov in enumerate(overlays):
        end = max(ov["start"], ov["end"] - eps)
        # Fade can't exceed half the window or in/out would overlap and the
        # card would never reach full opacity.
        fade = min(FADE, max(0.0, (end - ov["start"]) / 2))
        parts.append(f"[{i + base}:v]format=rgba,"
                      f"fade=t=in:st={ov['start']:.3f}:d={fade:.3f}:alpha=1,"
                      f"fade=t=out:st={max(0.0, end - fade):.3f}:d={fade:.3f}:alpha=1[fx{i}]")
        parts.append(f"[{label}][fx{i}]overlay=x={ov['x']}:y={ov['y']}:"
                      f"enable='between(t,{ov['start']:.3f},{end:.3f})'[ov{i}]")
        label = f"ov{i}"
    parts.append(f"[{label}]format=yuv420p,subtitles=filename={_escape(ass_path)}:"
                  f"fontsdir={_escape(str(FONT_DIR))}[vout]")

    run(cmd + ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", audio_map,
               "-t", f"{duration:.3f}",
               "-c:v", "libx264", "-crf", "18", "-preset", "medium",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", out_path])


# ---------- orchestration ----------

def run_config(config: dict) -> dict:
    cfg = {**DEFAULTS, **config}
    for key in ("input", "output"):
        if not cfg.get(key):
            raise ValueError(f"config missing required key: {key}")
    if cfg["arabic_font"] not in ARABIC_FONTS:
        raise ValueError(f"arabic_font must be one of {sorted(ARABIC_FONTS)}")
    if cfg["english_font"] not in ENGLISH_FONTS:
        raise ValueError(f"english_font must be one of {sorted(ENGLISH_FONTS)}")
    font_path = str(ARABIC_FONTS[cfg["arabic_font"]])

    tmp = Path(cfg["tmp_dir"])
    tmp.mkdir(parents=True, exist_ok=True)
    wav = str(tmp / "audio.wav")

    print("[1/7] Reading source...")
    source = cfg["input"]
    info = get_video_info(source)
    if cfg.get("trim"):
        t0, t1 = (list(cfg["trim"]) + [None])[:2]
        trimmed = str(tmp / ("trimmed.m4a" if not info["width"] else "trimmed.mp4"))
        print(f"      trimming to {t0}-{t1 if t1 is not None else 'end'}s")
        source = trim_media(source, trimmed, float(t0), None if t1 is None else float(t1))
        info = get_video_info(source)
    info["tmp_dir"] = str(tmp)

    # A background_image replaces the moving picture entirely, so the frame size
    # comes from the config rather than the source, and the source may legitimately
    # be audio-only (width/height 0).
    still = cfg.get("background_image")
    if still and not Path(still).exists():
        raise ValueError(f"background_image not found: {still}")
    if info["width"]:
        print(f"      {info['width']}x{info['height']}, {info['duration']:.1f}s")
    elif not still:
        raise ValueError("input has no video stream; set background_image to "
                         "render over a still photo")
    else:
        print(f"      audio-only input, {info['duration']:.1f}s")
    if still:
        info["width"], info["height"] = cfg["frame_size"]
        print(f"      still background -> {info['width']}x{info['height']}: "
              f"{Path(still).name}")

    orientation = cfg["orientation"]
    if orientation == "auto":
        orientation = "vertical" if info["height"] >= info["width"] else "horizontal"
        print(f"      orientation: {orientation} (from source aspect)")

    # With a still background there is no subject to track: the frame is already
    # the configured size and captions center on it.
    if still:
        print("[2/7] Still background -- skipping subject detection.")
        subject = None
    else:
        print("[2/7] Locating subject...")
        subject = detect_subject(source, info) if cfg["detect_subject"] else None
        if subject:
            print(f"      face at x={subject['cx']} y={subject['cy']} "
                  f"({subject['w']}x{subject['h']}px, {subject['hits']} frames)")

    # A vertical reel from a HORIZONTAL source must be re-cropped from the
    # original, not letterboxed or cropped a second time downstream: crop first,
    # then re-detect, since every pixel coordinate the layout depends on shifts.
    if not still and orientation == "vertical" and info["width"] > info["height"]:
        print("      cropping horizontal source to 9:16 on the subject...")
        vf, crop_x = vertical_crop_filter(info, subject)
        cropped = str(tmp / "cropped.mp4")
        run([FFMPEG, "-y", "-v", "error", "-i", source, "-vf", vf,
             "-c:v", "libx264", "-preset", "slow", "-crf", "16",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", cropped])
        source = cropped
        info = {**get_video_info(source), "tmp_dir": str(tmp)}
        print(f"      cropped at x={crop_x} -> {info['width']}x{info['height']}")
        subject = detect_subject(source, info) if cfg["detect_subject"] else None

    # Automatic placement supplies only what the config didn't pin: an explicit
    # x_offset/y_offset in the config always wins.
    layout = auto_layout(info, subject, orientation)
    top_target = layout["top_target"] if "y_offset" not in config else None
    cfg["x_offset"] = config.get("x_offset", layout["x_offset"])
    cfg["y_offset"] = config.get("y_offset", 0)
    placement = cfg["y_offset"] if top_target is None else f"auto (block top -> {top_target})"
    print(f"      layout: x_offset={cfg['x_offset']} y_offset={placement}")

    print("[3/7] Extracting audio...")
    extract_audio(source, wav)

    surah, a0, a1 = cfg.get("surah"), cfg.get("ayah_start"), cfg.get("ayah_end")
    if surah is None:
        print("[4/7] Identifying verse span (ASR)...")
        span = identify_verse_span(transcribe_chunked(wav))
        if span is None:
            raise RuntimeError("could not auto-identify verse span; set surah/ayah_start/ayah_end in config")
        surah, a0, a1 = span["surah"], span["ayah_start"], span["ayah_end"]
        print(f"      detected surah {surah}, ayahs {a0}-{a1}")
    else:
        print("[4/7] Verse span from config.")

    print(f"[5/7] Fetching {surah}:{a0}-{a1}...")
    verses = fetch_verse_range(surah, a0, a1, cfg["translation_id"])

    print("[6/7] Forced-aligning audio...")
    words = spoken_words(verses)
    stamps = forced_align(wav, verses)
    if len(words) != len(stamps):
        n = min(len(words), len(stamps))
        print(f"      WARNING: word count mismatch ({len(words)} vs {len(stamps)}), trimming to {n}")
        words, stamps = words[:n], stamps[:n]

    print("      Building captions...")
    if cfg.get("groups"):
        print(f"      {len(cfg['groups'])} config groups")
        arabic, english = apply_groups(stamps, words, cfg["groups"], info["duration"])
    else:
        print("      no groups in config -- automatic grouping (draft quality)")
        arabic, english = auto_group(stamps, words, verses, info["duration"])

    # Cut captions back at measured silences, THEN re-close the short gaps:
    # clipping alone would leave sub-MAX_HOLD holes where a caption was trimmed
    # to a silence that turned out shorter than the next caption's lead-in.
    silences = detect_silences(wav)
    if silences:
        print(f"      {len(silences)} long pause(s) >= {MAX_HOLD}s: "
              + ", ".join(f"{a:.1f}-{b:.1f}s" if b else f"{a:.1f}s-end" for a, b in silences))
        arabic, english = _fill_gaps(*clip_to_speech(arabic, english, silences),
                                     info["duration"])
    if cfg["suppress"]:
        before = len(arabic)
        arabic, english = suppress_windows(arabic, english, cfg["suppress"])
        print(f"      suppressed {cfg['suppress']} -> {before - len(arabic)} caption(s) dropped")
    if cfg["nudge"]:
        arabic, english = apply_nudges(arabic, english, cfg["nudge"])
        print(f"      {len(cfg['nudge'])} caption nudge(s) applied")
    if cfg["verse_numbers"]:
        arabic = append_verse_numbers(arabic, verses, cfg["arabic_font"])
    dropped = sum(1 for i in range(len(arabic) - 1)
                  if arabic[i + 1]["start"] - arabic[i]["end"] > 0.05)
    print(f"      {len(arabic)} captions, {dropped} pause gap(s) left clean")

    print("[7/7] Rendering + compositing...")
    width, height = info["width"], info["height"]
    center_x = width // 2 + cfg["x_offset"]
    margin = int(width * 0.06)
    max_ar_width = 2 * min(center_x - margin, (width - margin) - center_x)

    texts = [strip_display_marks(p["text"]) for p in arabic]
    ar_size = shared_font_size(texts, font_path, int(height * 0.09 * cfg["arabic_scale"]), max_ar_width)
    # English is derived from the FITTED Arabic size via the locked house ratio,
    # so the pair keeps its approved relative weight even when a wide phrase
    # forces the Arabic to shrink.
    en_size = max(12, int(ar_size * EN_AR_SIZE_RATIO * cfg["english_scale"]))
    print(f"      arabic: {cfg['arabic_font']} @ {ar_size}px | "
          f"english: {cfg['english_font']} @ {en_size}px "
          f"(ratio {en_size / ar_size:.2f})")

    # Render Arabic first so the real PNG heights are known, then center the
    # Arabic + gap + English stack as ONE block on the frame's vertical center
    # (shifted by y_offset). Using the tallest Arabic PNG keeps every caption's
    # baseline fixed, so text never jumps vertically between captions.
    png_dir = tmp / "arabic"
    png_dir.mkdir(exist_ok=True)
    rendered = []
    for i, text in enumerate(texts):
        path = str(png_dir / f"{i:03d}.png")
        rendered.append((path,) + render_arabic_png(text, font_path, ar_size, path))

    ar_h = max(h for _, _, h in rendered)
    # The Arabic PNGs carry transparent shadow padding on every edge, so the
    # English must be pulled up by one pad to sit flush at line_gap 0.
    gap = int(height * cfg["line_gap"]) - arabic_png_pad()
    en_h = english_block_height(english, width, en_size, cfg["english_max_chars"], center_x)
    block_h = ar_h + gap + en_h
    if top_target is None:
        block_top = (height - block_h) // 2 + cfg["y_offset"]
    else:
        # Absolute top from auto_layout (below the face). Clamp so the block
        # can't run off the bottom of a frame it doesn't fit under.
        block_top = min(top_target, height - block_h - int(height * 0.04))
    print(f"      block: top={block_top} height={block_h}")

    overlays = [{"path": path, "start": ph["start"], "end": ph["end"],
                 "x": center_x - w // 2, "y": block_top + (ar_h - h) // 2}
                for (path, w, h), ph in zip(rendered, arabic)]

    ass_path = str(tmp / "subs.ass")
    build_english_ass(english, width, height, ass_path, ENGLISH_FONTS[cfg["english_font"]],
                       en_size, center_x, block_top + ar_h + gap, cfg["english_max_chars"],
                       duration=info["duration"], signature=cfg["signature"])

    composite(source, ass_path, overlays, cfg["output"], cfg["dim"], info["duration"],
              still_image=still, size=(width, height))
    print(f"Done: {cfg['output']}")

    return {"surah": surah, "ayah_start": a0, "ayah_end": a1, "output": cfg["output"],
            "orientation": orientation, "subject": subject,
            "background_image": still, "frame": f"{width}x{height}",
            "signature": SIGNATURE_TEXT if cfg["signature"] else None,
            "captions": len(arabic), "pause_gaps": dropped,
            "x_offset": cfg["x_offset"], "y_offset": cfg["y_offset"],
            "arabic_font": cfg["arabic_font"], "arabic_size": ar_size,
            "english_font": cfg["english_font"], "english_size": en_size,
            "block_top": block_top, "block_height": block_h}


def main():
    p = argparse.ArgumentParser(description="Qur'an video subtitle pipeline (config-driven)")
    p.add_argument("config", nargs="?", help="path to the per-video config JSON")
    p.add_argument("--print-schema", action="store_true", help="print the config schema and exit")
    args = p.parse_args()

    if args.print_schema:
        print(config_schema())
        return
    if not args.config:
        p.error("config path required (or use --print-schema)")

    result = run_config(json.loads(Path(args.config).read_text(encoding="utf-8")))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
