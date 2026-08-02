"""pipeline/align.py -- forced word timing for a reel config.

    tools/align-venv/bin/python pipeline/align.py sources/<id>/<reel>.yaml

Reads the reel config's verse span and gets the KNOWN mushaf text for it
from quran.py -- never the whisper transcript's own words. A CTC forced
aligner (Meta's MMS, via the ctc-forced-aligner package) then places each
already-known word on the audio directly: it cannot hallucinate text,
because it never chooses WHAT was said, only WHEN each word was spoken.
That is why it times more accurately than Whisper's word boundaries,
which fall out of a free decode -- measured against a Whisper transcript
of the same clip, its word onsets sit on the real attack instead of
inheriting the previous word's end.

Writes, next to the config:

    <reel>.align.json   {"words": [{"w","t0","t1","score"}...],
                         "backend": "ctc-forced-aligner", "model": ...}

generate.py prefers this file over whisper.json + Needleman-Wunsch
alignment when it exists beside the config.

A config with NO `trim:` is aligned against the whole source and the trim
is derived from where the first and last mushaf word actually land, then
written back into the config. The <star> token between words is what
makes this safe: the CTC path can absorb any amount of non-recitation
audio, so nothing has to be guessed off an envelope by ear.

The HEAD of that window is then measured rather than padded (head_cut):
a reel has to open ON the recitation, and a flat pad produced reels that
started with 0.38s of silence, which is where viewers scroll. The cut
lands 0.12s before the first word's onset, clamped so it never reaches
back past the start of the waqf that onset comes out of. A hand-set
`trim:` is never moved -- it is the author's -- but its head gap is
reported every run, and a loose one is called out with the value it
should have had. The tail is deliberately still padded: a held final word
wants its decay.

whisper.json is needed only to DISCOVER the verse span. Name the span in
the config and this script -- and generate.py after it -- never read a
transcript, so transcribe.py can be skipped entirely.

Runs under tools/align-venv, never tools/render-venv: torch and
transformers must never land in the interpreter whose Pillow carries the
RAQM build that shapes Arabic (CLAUDE.md invariant #3).

Forced alignment assumes each reference word is spoken once, so a
reciter's ibtida'-restart (breaking off and repeating a phrase) leaves
the first utterance with no reference word to claim it -- the alignment
times only the second, seconds late. That case is corrected here from
whisper.json, which DOES transcribe both utterances: two consecutive
segments carrying the same words are the restart, and the phrase's
opening word is pulled back onto the first of them. It cannot be found
in the confidence scores instead -- a long held madd scores just as low.

The correction needs whisper.json, so it only runs when transcribe.py
has been used. Name the span and skip the transcript and a restart goes
uncorrected; `nudge` is still the override either way, applied last.
"""
import argparse
import array
import json
import math
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import generate  # noqa: E402
import quran  # noqa: E402

MODEL = "MahmoudAshraf/mms-300m-1130-forced-aligner"
LANGUAGE = "ara"           # ISO 639-3; the mushaf is always Arabic

# A reel opens ON the recitation. Dead air at the head is where viewers
# scroll. The head is MEASURED (head_cut); a flat pad on both sides
# measurably produced reels starting with 0.38s of silence. The tail keeps
# the pad: a held final word wants its decay.
HEAD_LEAD = 0.12           # air kept before the first word, when available
HEAD_LOOKBACK = 2.0        # how far back to look for the waqf it comes out of
HEAD_MAX = 0.25            # a hand-set trim looser than this gets called out
TAIL_PAD = 0.30
ENV_STEP = 0.05            # RMS bin, seconds


def rms_envelope(src, t0, t1, step=ENV_STEP):
    """RMS in dBFS per `step` seconds over [t0, t1) -> [(t, db), ...].

    `silencedetect` is unusable on these sources. A Haram recording's hall
    reverb never falls below about -35 dB and the ambience floor sits near
    -25, so no absolute threshold separates a waqf from speech: measured,
    both `-32dB:d=0.35` and `-40dB:d=0.2` returned ZERO hits across a window
    that plainly contains waqf pauses. The same audio's RELATIVE minima are
    unmistakable -- a waqf reads as a 0.4-0.9s dip to -27..-34 dB against a
    -11 dB speech level -- so the envelope is dumped and the dip found by
    comparison against this window's own range, never against a constant.

    stdlib only (`array`, `math`): the windows are ~2s of 16 kHz mono, and
    align.py already pulls in torch -- it does not need to grow another
    numerical dependency to sum 32000 squares."""
    sr = 16000
    n = max(1, int(sr * step))
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-ss", "%.3f" % t0, "-to", "%.3f" % t1, "-i", src,
         "-ar", str(sr), "-ac", "1", "-vn", "-f", "s16le", "-"],
        check=True, capture_output=True).stdout
    pcm = array.array("h")
    pcm.frombytes(raw[:len(raw) - len(raw) % pcm.itemsize])
    out = []
    for i in range(0, len(pcm) - n + 1, n):
        acc = sum(s * s for s in pcm[i:i + n])
        # Floor the ratio at one LSB: digital silence would be -inf dB and
        # a single -inf drags the window's range to meaninglessness.
        out.append((t0 + i / float(sr),
                    20.0 * math.log10(max(math.sqrt(acc / n), 1.0) / 32768.0)))
    return out


def head_cut(src, onset, lead=HEAD_LEAD, lookback=HEAD_LOOKBACK):
    """Where to cut so the reel opens ON the recitation -> (t, gap_seconds).

    `lead` before the first word's onset, but never back past the start of
    the quiet run that onset comes out of: on a short breath there may be
    less than `lead` of air to take, and opening on the tail of the previous
    word is worse than opening a frame late.

    "Quiet" is the bottom third of THIS window's own dynamic range, which is
    what makes it work where a threshold does not (see rms_envelope). The
    returned gap is the measured length of that run, 0.0 when none was found
    -- continuous recitation with no waqf before the first word, where the
    lead is simply taken and the caller is told nothing was measured."""
    lo = max(0.0, onset - lookback)
    if onset - lo < 4 * ENV_STEP:
        return max(0.0, onset - lead), 0.0
    env = rms_envelope(src, lo, onset)
    if len(env) < 4:
        return max(0.0, onset - lead), 0.0

    floor = min(db for _t, db in env)
    peak = max(db for _t, db in env)
    if peak - floor < 6.0:            # nothing resembling speech-vs-gap here
        return max(0.0, onset - lead), 0.0
    quiet = [db < floor + 0.35 * (peak - floor) for _t, db in env]

    # A CTC onset can sit a bin or two either side of the real attack, so the
    # run is allowed to end slightly before the window does.
    i = len(quiet) - 1
    while i >= 0 and not quiet[i] and env[-1][0] - env[i][0] < 3 * ENV_STEP:
        i -= 1
    if i < 0 or not quiet[i]:
        return max(0.0, onset - lead), 0.0
    j = i
    while j >= 0 and quiet[j]:
        j -= 1
    gap_start = env[j + 1][0]
    return max(gap_start, onset - lead), env[i][0] + ENV_STEP - gap_start


def extract_window(src, out_wav, t0, t1):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", "%.3f" % t0]
    if t1 is not None:
        cmd += ["-to", "%.3f" % t1]
    cmd += ["-i", src, "-ar", "16000", "-ac", "1", "-vn", out_wav]
    subprocess.run(cmd, check=True)


def write_trim(config_path, t0, t1):
    """Put `trim: [t0, t1]` into the config, in place.

    Line-level edit rather than a YAML round-trip: safe_dump would strip the
    hand-written comments that carry every reel's reasoning."""
    text = open(config_path, encoding="utf-8").read()
    if not text.endswith("\n"):
        text += "\n"
    lines = text.splitlines(keepends=True)
    new = "trim: [%.2f, %.2f]\n" % (t0, t1)
    for i, ln in enumerate(lines):
        if re.match(r"^trim\s*:", ln):
            lines[i] = new
            break
    else:
        # where a hand-written trim goes: just under the verse span
        after = [i for i, ln in enumerate(lines)
                 if re.match(r"^(surah|ayah_start|ayah_end)\s*:", ln)]
        lines.insert(max(after) + 1 if after else len(lines), "\n" + new)
    open(config_path, "w", encoding="utf-8").write("".join(lines))


def find_repeats(whisper_path, t0):
    """Ibtida' restarts, as (first_utterance_start, second_utterance_start).

    Two consecutive Whisper segments carrying the SAME words is the reciter
    breaking off and starting the phrase again. Whisper transcribes both
    utterances; the mushaf reference has the phrase once, so forced alignment
    can only ever claim one of them -- this is where the other one is."""
    segments = json.load(open(whisper_path, encoding="utf-8")).get("segments", [])
    out = []
    for a, b in zip(segments, segments[1:]):
        toks = tuple(quran.tokens(a["text"]))
        if toks and toks == tuple(quran.tokens(b["text"])):
            out.append((a["t0"] - t0, b["t0"] - t0))
    return out


def apply_repeats(results, repeats, min_fix=0.5):
    """Pull a restarted phrase's opening word back onto its FIRST utterance.

    The aligner times the second utterance (nothing is left to claim the
    first), so the caption would come up seconds late and the first attempt
    would sit uncaptioned. The word to move is the first one the aligner
    placed at/after the second utterance -- no text matching needed. Its
    predecessor's end is clamped so the two cannot overlap. Corrections
    smaller than min_fix are noise, not a missed utterance."""
    fixed = []
    for first_start, second_start in repeats:
        k = next((i for i, r in enumerate(results)
                  if r["start"] >= second_start), None)
        if k is None or results[k]["start"] - first_start < min_fix:
            continue
        fixed.append((k, results[k]["start"], first_start))
        results[k]["start"] = first_start
        if k:
            results[k - 1]["end"] = min(results[k - 1]["end"], first_start)
    return fixed


def align(config_path, force=False):
    cfg = generate.load_config(config_path)
    cfg = generate.resolve_paths(cfg, config_path)
    out_path = generate.align_path_for(config_path)
    if os.path.exists(out_path) and not force:
        print("%s exists -- use --force to re-run"
              % os.path.relpath(out_path, generate.ROOT))
        return out_path

    # No trim in the config: align the WHOLE source and read the window back
    # off the alignment. The <star> token between words lets the CTC path
    # absorb whatever is not recitation, so the first and last mushaf word
    # land on their real onsets instead of being guessed off an envelope.
    t0, t1 = 0.0, None
    auto_trim = not cfg.get("trim")
    if not auto_trim:
        t0, t1 = (list(cfg["trim"]) + [None])[:2]
        t0 = float(t0)
        t1 = float(t1) if t1 is not None else None

    surah, a0, a1 = cfg["surah"], cfg["ayah_start"], cfg["ayah_end"]
    if surah is None:
        asr_words, _ = generate.load_whisper_window(
            generate.require_whisper(cfg), t0, t1)
        span = generate.identify_verse_span(asr_words)
        if span is None:
            raise SystemExit("could not auto-identify the verse span; set "
                             "surah/ayah_start/ayah_end in the config")
        surah, a0, a1 = span["surah"], span["ayah_start"], span["ayah_end"]
    else:
        a0 = int(a0 if a0 is not None else 1)
        a1 = int(a1 if a1 is not None else a0)

    verses = generate.fetch_verses(int(surah), a0, a1)
    words = generate.spoken_words(verses)
    ref_text = " ".join(words)
    print("aligning %d mushaf words (%s %d:%d-%d) against %s"
          % (len(words), quran.surah_name(surah), surah, a0, a1,
             os.path.relpath(cfg["input"], generate.ROOT)))

    from ctc_forced_aligner import (
        generate_emissions, get_alignments, get_spans, load_alignment_model,
        load_audio, postprocess_results, preprocess_text,
    )
    import torch

    with tempfile.TemporaryDirectory(prefix="quran-align-") as tmp:
        wav = os.path.join(tmp, "audio.wav")
        extract_window(cfg["input"], wav, t0, t1)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model, tokenizer = load_alignment_model(device, MODEL, dtype=torch.float32)
        audio_waveform = load_audio(wav, model.dtype, model.device)
        emissions, stride = generate_emissions(model, audio_waveform)

    tokens_starred, text_starred = preprocess_text(
        ref_text, romanize=True, language=LANGUAGE,
        split_size="word", star_frequency="segment")
    segments, scores, blank = get_alignments(emissions, tokens_starred, tokenizer)
    spans = get_spans(tokens_starred, segments, blank)
    results = postprocess_results(text_starred, spans, stride, scores)

    if len(results) != len(words):
        raise SystemExit("aligner returned %d spans for %d mushaf words -- "
                         "cannot map 1:1" % (len(results), len(words)))

    # Before the auto-trim shift below, while `results` and the transcript
    # still share the alignment window's clock.
    if os.path.exists(cfg["whisper"]):
        for k, was, now in apply_repeats(results,
                                         find_repeats(cfg["whisper"], t0)):
            print("  repeated phrase: %s starts at %.2fs, not %.2fs "
                  "(reciter restarted; caption now covers both)"
                  % (words[k], now, was))

    if auto_trim:
        duration = generate.get_video_info(cfg["input"])["duration"]
        onset = results[0]["start"]
        cut, gap = head_cut(cfg["input"], onset)
        # Round FIRST, then shift by the same value that lands in the config:
        # the times below are relative to the trim generate.py will re-cut.
        t0 = round(max(0.0, cut), 2)
        t1 = round(min(duration, results[-1]["end"] + TAIL_PAD), 2)
        for r in results:
            r["start"] -= t0
            r["end"] -= t0
        write_trim(config_path, t0, t1)
        print("  derived trim: [%.2f, %.2f] -> written into %s"
              % (t0, t1, os.path.basename(config_path)))
        print("  head: %.0fms before the first word (%s)"
              % ((onset - t0) * 1000,
                 "waqf gap %.2fs" % gap if gap else
                 "no gap measured -- lead taken as-is"))
    else:
        # A hand-set trim is the author's, so it is reported and never moved.
        # This is the one number that decides whether the reel opens ON the
        # recitation, and it is free here: results[0] is already relative to
        # the window the config asked for.
        head = results[0]["start"]
        if head > HEAD_MAX:
            print("  ! %.2fs of dead air before the first word. A reel should "
                  "open on the recitation -- even a second of nothing and "
                  "people scroll. Set trim start to %.2f (%.0fms lead)."
                  % (head, t0 + head - HEAD_LEAD, HEAD_LEAD * 1000))
        else:
            print("  head: %.0fms before the first word" % (head * 1000))

    window = (t1 - t0) if t1 is not None else None
    out_words = [{"w": w,
                  "t0": round(max(0.0, r["start"]), 3),
                  "t1": round(r["end"] if window is None
                              else min(r["end"], window), 3),
                  "score": round(r["score"], 3)}
                 for w, r in zip(words, results)]
    json.dump({"words": out_words, "backend": "ctc-forced-aligner", "model": MODEL},
              open(out_path, "w"), ensure_ascii=False, indent=1)
    print("  %d words -> %s" % (len(out_words), os.path.relpath(out_path, generate.ROOT)))
    return out_path


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="forced word timing for a reel config, via Meta's MMS CTC model")
    ap.add_argument("config", help="sources/<id>/<reel>.yaml")
    ap.add_argument("--force", action="store_true",
                    help="re-align even if <reel>.align.json exists")
    a = ap.parse_args(argv)
    align(a.config, force=a.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
