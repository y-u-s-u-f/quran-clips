"""pipeline/generate.py -- reel config YAML -> rendered reel.

    tools/render-venv/bin/python pipeline/generate.py sources/<id>/<reel>.yaml
    tools/render-venv/bin/python pipeline/generate.py --print-schema

This file owns everything the styles share: config validation, verse
resolution, aligning the Whisper word timings against the known-correct
mushaf text, caption grouping, silence handling, suppress/nudge corrections
and verse-number ornaments. Rendering and compositing are the style files'
job -- render_text.py (Arabic + English over graded footage, at either
1080x1920 `vertical` or 1920x1080 `horizontal`) and render_bars.py (1920x1080,
Arabic-only pills over the full picture), dispatched on the config's `style:`
key. `--vertical` then letterboxes the result onto 1080x1920 (letterbox.py).

    TEXT INTEGRITY. Caption Arabic is built by slicing the word list of
    `quran.ayah()` -- the committed Uthmani text -- never from the Whisper
    transcript and never retyped. The transcript's only job is TIMING.

Word timing comes from <reel>.align.json (pipeline/align.py's CTC forced
alignment) when it exists beside the config; otherwise the mushaf words are
matched onto whisper.json here. A config that names its verse span and has
been through align.py never reads a transcript at all.

Config resolution: `input` defaults to the source.* next to the config,
`whisper` to the whisper.json next to it, `output` to reels/<config-name>.mp4.
So the normal layout needs none of the three spelled out:

    sources/<id>/source.mp4
    sources/<id>/whisper.json      (from transcribe.py; only when no
                                    <reel>.align.json / no verse span)
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
    "style": "vertical",         # vertical | horizontal | bars
    "input": None,               # media path; default: source.* beside config
    "whisper": None,             # word timings; default: whisper.json beside it
    "output": None,              # default: reels/<config-name>.mp4

    "surah": None,               # verse span; omit all three to auto-detect
    "ayah_start": None,          # from the whisper transcript (reliable only
    "ayah_end": None,            # for short 1-4 ayah windows)

    "reciter": None,             # his name in Arabic, spelled as the post's
                                 # hashtag spells it. Never on screen: it is
                                 # written into the mp4's tags so
                                 # pipeline/publish.py can caption the reel
                                 # from the file alone.

    "groups": None,              # caption splits -- see --print-schema

    "signature": None,           # handle burned bottom-centre, or null/omitted
                                 # for none. `bars` never burns one whatever
                                 # this says -- see render_bars.py.

    "trim": None,                # [start, end] seconds cut from source first
    "suppress": [],              # [[a, b]] windows left uncaptioned
    "nudge": [],                 # [{"group": i, "start": dt, "end": dt}]
    "verse_numbers": None,       # ayah ornament; default: on for vertical and
                                 # horizontal, off for bars

    "x_offset": 0,               # px nudge from the style's own anchor, which
    "y_offset": 0,               # is already solved: +right / +down. 0/0 is
                                 # the anchor itself, not necessarily centre.
    "signature_offset": 0,       # px, vertical only: + lower / - higher.
                                 # The signature is ALWAYS horizontally
                                 # centered; there is no x knob on purpose.

    # every style ---------------------------------------------------------
    "crop": None,                # {x,y,w,h} source-px window -> the 16:9 band
                                 # (bars) or the whole canvas (vertical,
                                 # horizontal). pipeline/crop.py solves it.

    # vertical + horizontal -----------------------------------------------
    "arabic_font": "uthmanic_hafs",   # uthmanic_hafs | thuluth
    "english_font": "albertus",       # albertus | gentium
    "arabic_scale": 1.0,         # x the house nominal 72pt
    "english_scale": 1.0,        # x the house 33pt caps
    "text_width_frac": None,     # override the ink-width cap the fitted point
                                 # size is solved against, as a fraction of
                                 # canvas width. The house caps (0.51 Arabic /
                                 # 0.55 English on landscape) hold the type to
                                 # the reference reels' measure, and a *_scale
                                 # above them silently stops scaling instead of
                                 # erroring. Raise it only when the widest card
                                 # still leaves real margin at the size asked
                                 # for; splitting the widest groups is the
                                 # other way to buy the same size.
    "english_caps": True,        # ALL-CAPS English (the reference reels' own
                                 # setting); false sets it as authored
    "vignette": True,            # the grade plate's soft edge darkening
    "dim": 0.118,                # flat black over the footage, 0 (untouched)
                                 # to 1 (black). 0.118 = the measured 30/255.
                                 # The backing carries the type's contrast, so
                                 # this is the global mood knob: dial it down
                                 # for footage that should stay bright.
    "face_bottom": None,         # vertical only: fraction of the canvas
                                 # height at which the reciter's head box
                                 # ends, measured by crop.py. The caption
                                 # block hangs just below it; omit and the
                                 # block is centred on the frame instead.

    # bars style only ----------------------------------------------------
    "bar_color": None,           # "#RRGGBB"; default: derived from the footage
    "grade": None,               # eq= overrides for the picture grade, e.g.
                                 # {brightness: -0.12, gamma: 0.95}. The house
                                 # numbers target mean luma 0.15-0.32; a source
                                 # darker than the refs lands under it, and
                                 # only that source's config should move. Keys:
                                 # brightness contrast saturation gamma.
    "fx": None,                  # per-stage switches, e.g. {heat: false};
                                 # stages: grade scrim glow barglow textglow
                                 # scan snow heat (all on by default)

    "tmp_dir": None,             # default /tmp/quran-pipeline/<reel-name>
}


def config_schema():
    return """Per-reel config YAML, kept in the source's own folder.
Every key has a working default with the standard layout; an unknown key is
an error rather than a silent no-op.

    style: vertical                 # vertical | horizontal | bars
    signature: "TilawatQuraniyyah"  # handle, burned bottom-centre. Omit or
                                    # null for none. `bars` NEVER burns one,
                                    # whatever this says.

    surah: 33                       # verse span. Omit all three to auto-detect
    ayah_start: 56                  # from whisper.json (short clips only)
    ayah_end: 56
    reciter: "..."                  # his name in ARABIC, spelled the way the
                                    # post's hashtag spells it. Copied from
                                    # the source's own title or an earlier
                                    # post, never transliterated. Goes into
                                    # the mp4's tags, never on screen;
                                    # pipeline/publish.py captions from them.

    trim: [927.3, 957.3]            # seconds of source this reel covers.
                                    # Omit and pipeline/align.py measures it
                                    # off the recitation and writes it here.

    groups:                         # caption cards, in Arabic word order.
      - n_words: 6                  # n_words must sum EXACTLY over the verse
        english: "Indeed, Allah..." # range's word count -- a mis-split fails
      - n_words: 5                  # loudly instead of shifting every caption.
        english: "..."              # english: vertical + horizontal only;
        line_split: 3               # bars is Arabic-only. bars: words on
                                    # line 1 (omit = auto). Omit groups
                                    # entirely for draft auto-grouping.

    crop: {x: 78, y: 30, w: 1280, h: 720}   # the reframing window, in SOURCE
    x_offset: -365                  # pixels, and the caption anchor. BOTH are
    face_bottom: 0.38               # solved by `pipeline/crop.py --write`;
                                    # see Framing below for which style uses
                                    # which key.
    y_offset: 0                     # px nudges on the solved anchor, both
                                    # styles (+x right, +y down)
    signature_offset: 0             # px: + lower / - higher. The signature is
                                    # ALWAYS horizontally centered.

    verse_numbers: true             # ayah ornament (default: on for vertical
                                    # and horizontal, off for bars)
    suppress: [[33, 58]]            # leave these second-windows uncaptioned
    nudge:                          # per-caption timing fix, applied LAST
      - {group: 3, start: -1.8}

    # vertical + horizontal: arabic_font (uthmanic_hafs | thuluth),
    #   english_font (albertus | gentium), arabic_scale, english_scale
    #   (multipliers on the house 72pt Arabic / 33pt English caps),
    #   text_width_frac (raises the ink-width cap the fitted size is solved
    #   against, so a *_scale is not silently clipped by it),
    #   english_caps (true = ALL-CAPS, the reference reels' setting),
    #   vignette (true | false), dim (0 untouched .. 1 black; 0.118 house)
    # bars style: bar_color ("#RRGGBB" | omit for auto),
    #   grade: {brightness: -0.12, gamma: 0.95, ...} to move the picture
    #   grade for THIS source only (keys: brightness contrast saturation
    #   gamma; the house numbers target mean luma 0.15-0.32),
    #   fx: {heat: false, ...} to switch stages off (grade scrim glow
    #   barglow textglow scan snow heat; heat is ~half the render time)

    input / whisper / output        # only to override the standard layout

Generation prints a verification block: every card's Arabic beside its
authored English, then each verse in BOTH reference editions (Saheeh
International and Mufti Taqi Usmani). Check every card against both before
accepting a render -- where the two editions agree on clause order, a card
whose English contradicts them is mis-split.

FRAMING. `crop` is the window of SOURCE pixels that becomes the canvas, so a
render never consults a detector and reproduces anywhere. It is REQUIRED
whenever the source is on the wrong side of square for the style (a landscape
source for a `vertical` reel, or the reverse); a source already the right way
round is cover-scaled and needs none. The caption anchor is solved with it and
differs per style:

    horizontal   block centred vertically, its column OPPOSITE the reciter,
                 via `x_offset` (crop.py's equal-gap solve)
    vertical     block centred horizontally, its top just BELOW his chin,
                 via `face_bottom`
    bars         the pill column, via `x_offset`

With neither key the block is centred on both axes -- which is also the right
answer for a shot with no reciter in it.

Output is 1080x1920 for `vertical`, 1920x1080 for `horizontal` and `bars`,
30fps, always: a low-quality source is upscaled rather than delivered small.
"""


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


# ---------- media ----------------------------------------------------------

def get_video_info(path):
    """Frame size + duration. An audio-only input has no video stream, so
    width/height come back 0 and run_config refuses it: every style renders
    over footage."""
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
    every aligned word against the audio the viewer hears.

    `veryfast`/crf 10 rather than `slow`/crf 16: this file is a THROWAWAY the
    renderer re-encodes at crf 18 minutes later, so nothing about it matters
    except that it not lose what the final encode would have kept -- and a
    faster preset buys that back with bits, which cost disk here and nothing
    else. Measured against a LOSSLESS cut of the same 27.5s 1080p window:

        slow     crf 16    71.0s CPU    6.4 MB    50.14 dB
        veryfast crf 14    20.5s CPU    7.4 MB    49.46 dB
        veryfast crf 10    25.1s CPU   14.8 MB    50.39 dB

    so crf 10 is -65% CPU and BETTER than what it replaces. Do not read file
    size as fidelity across presets: crf 14 is the larger file of the first
    two and the worse picture."""
    cmd = [FFMPEG, "-y", "-v", "error", "-ss", "%.3f" % start]
    if end is not None:
        cmd += ["-to", "%.3f" % end]
    cmd += ["-i", path, "-c:v", "libx264", "-preset", "veryfast", "-crf", "10",
            "-pix_fmt", "yuv420p"]
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
    _enforce_monotonic(stamps)
    n_matched = len(matches)
    print("      aligned %d/%d mushaf words to the transcript (%d interpolated)"
          % (n_matched, n, n - n_matched))
    return stamps


def _enforce_monotonic(stamps):
    t = 0.0
    for st in stamps:
        st["start"] = max(st["start"], t)
        st["end"] = max(st["end"], st["start"])
        t = st["end"]


def tag_output(path, surah, a0, a1, reciter):
    """Write the reel's identity into the mp4's own tags.

    pipeline/publish.py builds a post's caption from these and nothing else,
    so a reel stays publishable with no config beside it, no filename
    convention to honour and no memory of which source it came from.

    A stream-copy remux: ffmpeg cannot rewrite metadata in place, and the
    alternative -- passing -metadata through three different renderers'
    output calls -- would put the same four lines in three files. Costs about
    a second on a 6 MB reel and touches no pixel.
    """
    tmp = path + ".tags.mp4"
    cmd = [FFMPEG, "-y", "-v", "error", "-i", path, "-map", "0", "-c", "copy",
           "-movflags", "+faststart+use_metadata_tags",
           "-metadata", "title=%s %d:%d-%d" % (quran.surah_name(surah),
                                               surah, a0, a1),
           "-metadata", "comment=Quran %d:%d-%d" % (surah, a0, a1)]
    if reciter:
        cmd += ["-metadata", "artist=%s" % reciter]
    run(cmd + [tmp])
    os.replace(tmp, path)
    if not reciter:
        print("      no `reciter:` in the config -- publish.py will refuse "
              "this reel until one is set (or passed with --reciter)")


def align_path_for(config_path):
    """Where pipeline/align.py writes this reel's forced word timings."""
    return os.path.splitext(os.path.abspath(config_path))[0] + ".align.json"


def require_whisper(cfg):
    """whisper.json is needed only to DISCOVER things -- the verse span, and
    the timings when no forced alignment exists. A config that names its span
    and has been through align.py never reads it."""
    if not os.path.exists(cfg["whisper"]):
        raise SystemExit(
            "%s not found -- run pipeline/transcribe.py on this source first, "
            "or name the span (surah/ayah_start/ayah_end) in the config and "
            "run pipeline/align.py, which needs no transcript"
            % cfg["whisper"])
    return cfg["whisper"]


def format_window(w):
    return "[%.2f, %s]" % (w[0], "end" if w[1] is None else "%.2f" % w[1])


def check_align_window(path, data, window):
    """Refuse an align file measured against a different `trim:`.

    Its word times are RELATIVE to the window align.py cut, so editing `trim:`
    afterwards drifts every caption by the difference with nothing on screen
    to say so. Tolerance is 10ms -- under a frame at 30fps, and both sides are
    written to 2dp.

    A file with no `trim` key predates the stamp: warn and render. Erroring
    would brick every already-aligned config in sources/ over a window that
    was never recorded and so cannot be shown to be wrong, and align.py
    re-aligns such a file anyway, so the gap closes on its own."""
    stored = data.get("trim")
    if stored is None:
        print("      ! %s has no `trim` stamp, so nothing can confirm its word "
              "times were measured against %s. Re-run pipeline/align.py if the "
              "captions drift." % (os.path.basename(path),
                                   format_window(window)))
        return
    try:
        got = [None if v is None else float(v) for v in stored]
        assert len(got) == 2 and got[0] is not None
    except (TypeError, ValueError, AssertionError):
        raise SystemExit("%s: `trim` must be [start, end] seconds (end may be "
                         "null), not %r -- re-run pipeline/align.py"
                         % (path, stored))
    if any((a is None) != (b is None) or
           (a is not None and abs(a - b) > 0.01) for a, b in zip(got, window)):
        drift = window[0] - got[0]
        raise SystemExit(
            "%s was aligned against trim %s but the config now says %s -- its "
            "word times are relative to that window.%s Re-run "
            "pipeline/align.py on this config."
            % (path, format_window(got), format_window(window),
               "" if abs(drift) <= 0.01
               else " Every caption would land %+.2fs off." % drift))


def load_forced_alignment(path, ref_words, window):
    """Per-word timestamps from pipeline/align.py's CTC forced alignment
    (Meta's MMS model, run against the KNOWN mushaf text -- see align.py's
    docstring). Already 1:1 with ref_words, since the aligner places
    exactly one span per known word: no Needleman-Wunsch matching or
    interpolation needed. Falls back to align_words() + whisper.json when
    this file doesn't exist."""
    data = json.load(open(path, encoding="utf-8"))
    check_align_window(path, data, window)
    words = data["words"]
    if len(words) != len(ref_words):
        raise ValueError(
            "%s has %d words but the verse range has %d -- the config's "
            "span changed since align.py ran; re-run pipeline/align.py"
            % (path, len(words), len(ref_words)))
    stamps = [{"start": w["t0"], "end": w["t1"]} for w in words]
    _enforce_monotonic(stamps)
    print("      %d/%d mushaf words timed by %s"
          % (len(stamps), len(ref_words), data.get("backend", "?")))
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
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise SystemExit("unknown config key(s) in %s: %s -- a typo'd key "
                         "would be silently ignored otherwise"
                         % (path, ", ".join(unknown)))
    cfg = {**DEFAULTS, **raw}
    if cfg["style"] not in ("vertical", "horizontal", "bars"):
        raise SystemExit(
            "style must be `vertical` (1080x1920), `horizontal` (1920x1080) or "
            "`bars` (1920x1080), not %r." % cfg["style"])
    if cfg["face_bottom"] is not None and cfg["style"] != "vertical":
        raise SystemExit("`face_bottom` places the block below the reciter's "
                         "chin and is a `vertical` key; this config is `%s`, "
                         "where the column is placed with `x_offset`."
                         % cfg["style"])
    if cfg["verse_numbers"] is None:
        cfg["verse_numbers"] = cfg["style"] != "bars"
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

    cfg["output"] = os.path.abspath(
        output_override or cfg["output"]
        or os.path.join(ROOT, "reels", stem + ".mp4"))
    cfg["tmp_dir"] = cfg["tmp_dir"] or os.path.join(
        "/tmp", "quran-pipeline", stem)
    return cfg


# Wall seconds of bake per second of heat map, measured at 1920x1080/30
# (46.5s for a 5s map). `perlin` is single-threaded, and a reel that spills
# into a fresh 30s bucket pays this once per axis.
HEAT_BAKE_RATE = 9.3


def warn_heat_bake(cfg, dur, tmp):
    """Announce a pending heat bake instead of going silent for many minutes.

    Only a reel longer than every cached map pays this, and paying it once
    buys every shorter reel after it -- so the note is a heads-up, never a
    reason to re-cut the span."""
    import render_bars    # Pillow at module level, and pipeline/align.py
                          # imports this file under an interpreter without it:
                          # every render_bars import stays inside a function.
    if not render_bars.switches(cfg.get("fx"))["heat"]:
        return
    span, paths = render_bars.heat_map_paths(dur)
    missing = [p for p in paths if not os.path.exists(p)]
    if not missing:
        return
    hint = " Every later reel up to %ds then reuses it." % span
    print("      heat: %.1fs reel, no cached map covers it -- baking %ds, "
          "%d of 2 maps missing. Expect ~%dmin of perlin first.%s Set "
          "fx: {heat: false} to skip it."
          % (dur, span, len(missing),
             round(HEAT_BAKE_RATE * span * len(missing) / 60.0), hint))


def run_config(config_path, output_override=None, vertical=False):
    cfg = load_config(config_path)
    cfg = resolve_paths(cfg, config_path, output_override)
    tmp = cfg["tmp_dir"]
    os.makedirs(tmp, exist_ok=True)
    os.makedirs(os.path.dirname(cfg["output"]), exist_ok=True)

    print("[1/6] Reading source...")
    source = cfg["input"]
    info = get_video_info(source)
    if not info["width"]:
        raise SystemExit("%s has no video stream -- every style renders over "
                         "footage" % os.path.basename(cfg["input"]))
    t0w = 0.0
    window = (0.0, None)
    if cfg.get("trim"):
        t0, t1 = (list(cfg["trim"]) + [None])[:2]
        t0w = float(t0)
        window = (t0w, None if t1 is None else float(t1))
        trimmed = os.path.join(tmp, "trimmed.mp4")
        print("      trimming to %s-%ss" % (t0, t1 if t1 is not None else "end"))
        source = trim_media(source, trimmed, float(t0),
                            None if t1 is None else float(t1))
        info = get_video_info(source)
    print("      %sx%s, %.1fs" % (info["width"], info["height"],
                                  info["duration"]))

    align_path = align_path_for(config_path)
    forced = os.path.exists(align_path)
    asr_words = []
    if forced and cfg["surah"] is not None:
        print("[2/6] Word timings from %s" % os.path.basename(align_path))
    else:
        print("[2/6] Loading whisper words...")
        asr_words, asr_backend = load_whisper_window(
            require_whisper(cfg), t0w,
            t0w + info["duration"] if cfg.get("trim") else None)
        print("      %d words in window (backend %s)"
              % (len(asr_words), asr_backend))

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

    if forced:
        print("[4/6] Using forced alignment (pipeline/align.py)...")
        stamps = load_forced_alignment(align_path, words, window)
    else:
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

    plan = {"cfg": cfg, "src": source, "info": info,
            "arabic": arabic, "english": english, "verses": verses,
            "tmp": tmp, "out": cfg["output"]}

    if cfg["style"] == "bars":
        warn_heat_bake(cfg, info["duration"], tmp)

    print("[6/6] Rendering (%s style)..." % cfg["style"])
    if cfg["style"] == "bars":
        import render_bars
        render_bars.render(plan)
    else:
        import render_text
        render_text.render(plan)
    if vertical:
        import letterbox
        print("      letterboxing to %dx%d" % (letterbox.W, letterbox.H))
        letterbox.letterbox(cfg["output"])
    tag_output(cfg["output"], int(surah), a0, a1, cfg["reciter"])
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
    ap.add_argument("--vertical", action="store_true",
                    help="letterbox the finished reel to 1080x1920")
    # These runs are long and normally watched: line-buffer so progress reaches
    # a pipe or a log as it happens, not all at once at exit.
    sys.stdout.reconfigure(line_buffering=True)
    a = ap.parse_args(argv)
    if a.print_schema:
        print(config_schema())
        return 0
    if not a.config:
        ap.error("config path required (or --print-schema)")
    result = run_config(a.config, a.output, a.vertical)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
