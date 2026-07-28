"""qc.author.align -- forced alignment of a KNOWN mushaf text onto audio.

`qc locate` says which ayat a video recites and roughly when. This module takes
that answer, plus the window the author actually wants, and produces a time for
every WORD -- the step SKILL.md:87-123 used to do by hand with per-pause ASR
re-runs and manual restart hunting.

Three commitments, in order of importance:

    1. THE TEXT IS NEVER INFERRED. The Arabic comes from `qc.quran` and only
       from there. ASR is a clock, not a transcriber: it tells us WHEN a word
       was said, and its own spelling is thrown away. Whisper on Quranic
       Arabic drops words, invents hamza seats and merges repeats; none of
       that can reach the screen if the text side is fixed up front.

    2. WHISPER ONLY EVER SEES THE WINDOW. We cut `window +/- PAD` seconds to a
       16 kHz mono WAV and transcribe that. A 17-minute taraweeh recording
       costs minutes and drifts; 25 seconds costs a few seconds and does not.

    3. THE ALIGNMENT PERMITS REPEATS. A plain Needleman-Wunsch assumes both
       sequences advance monotonically, which is exactly what recitation does
       NOT do: after a pause the reciter restarts from an earlier word
       (ibtida'), aborts a word and begins again, or recites a whole ayah
       twice. The DP below therefore has a JUMP edge that lets the reference
       pointer move backwards (or skip ahead) at a fixed cost. Restarts then
       FALL OUT of the alignment as backward jumps and are reported, instead
       of being hunted pause by pause by ear.

The output is an ordered list of EVENTS -- (reference word, asr word, t0, t1)
in the order they were actually recited. A word repeated three times appears
three times. Everything downstream (energy snapping, card splitting, caption
text) reads that list, so repeats are captioned as recited by construction.
"""
import difflib
import json
import os
import shlex
import subprocess
import sys

from .. import quran as Q
from ..proc import FFMPEG

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ASR_PY = os.path.join(ROOT, "tools", "asr-venv", "bin", "python")
ASR_MODEL = "mlx-community/whisper-large-v3-turbo"

PAD = 2.0            # seconds of context either side of the requested window


def run(cmd):
    """qc.proc.run, but echoing to STDERR.

    `qc author -n` writes the clip.yaml to stdout, and the repo's convention of
    echoing every ffmpeg command line would otherwise paste itself into the
    top of the file when it is piped somewhere.
    """
    print("+ " + " ".join(shlex.quote(c) for c in cmd), file=sys.stderr)
    return subprocess.run(cmd)


# --- DP costs. Deliberately few, and all in the same "one substitution" unit.
COST_GAP_REF = 0.60  # a mushaf word Whisper never emitted (melisma, dropped)
COST_GAP_ASR = 0.60  # a Whisper word with no mushaf counterpart (hallucination)
COST_JUMP = 1.80     # move the reference pointer off-sequence: a restart
JUMP_FREE = 1        # advancing by exactly one word is the normal edge, free


# ---------------------------------------------------------------------------
# audio + ASR
# ---------------------------------------------------------------------------

def extract_wav(src, start, end, out, pad=PAD, rate=16000):
    """Cut [start-pad, end+pad] to 16 kHz mono WAV. -> (path, wav_t0_abs).

    The returned offset is what converts a time in the WAV back to a time in
    the source; everything in this module works in WAV-relative seconds and
    only `emit` converts back.
    """
    t0 = max(0.0, float(start) - pad)
    t1 = float(end) + pad
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-loglevel", "error", "-y",
           "-ss", "%.3f" % t0, "-t", "%.3f" % (t1 - t0), "-i", src,
           "-vn", "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le", out]
    p = run(cmd)
    if p.returncode != 0 or not os.path.exists(out):
        raise SystemExit("ffmpeg failed to extract %s" % out)
    return out, t0


# Run inside tools/asr-venv, which is a SEPARATE interpreter from the render
# venv (mlx-whisper must never be installed into the render/system python --
# see the RAQM incident in SKILL.md). So: shell out, don't import.
_ASR_SNIPPET = r"""
import json, sys
import mlx_whisper
wav, out, model = sys.argv[1], sys.argv[2], sys.argv[3]
r = mlx_whisper.transcribe(
    wav, path_or_hf_repo=model, language="ar", word_timestamps=True,
    condition_on_previous_text=False, temperature=0.0, verbose=None)
words = []
for seg in r.get("segments", []):
    for w in seg.get("words", []):
        words.append({"w": w["word"].strip(),
                      "t0": float(w["start"]), "t1": float(w["end"]),
                      "p": float(w.get("probability", 0.0))})
json.dump({"words": words,
           "segments": [{"t0": s["start"], "t1": s["end"], "text": s["text"]}
                        for s in r.get("segments", [])]},
          open(out, "w"), ensure_ascii=False, indent=1)
"""


def asr(wav, cache=None, model=ASR_MODEL):
    """mlx-whisper with word timestamps over the window WAV. -> [word dicts].

    Cached to `cache` (json), because the DP and the energy pass get re-run
    far more often than the audio changes.
    """
    if cache and os.path.exists(cache):
        return json.load(open(cache, encoding="utf-8"))
    if not os.path.exists(ASR_PY):
        raise SystemExit(
            "%s missing. Recreate it:\n"
            "  /opt/homebrew/bin/python3 -m venv tools/asr-venv && "
            "tools/asr-venv/bin/pip install mlx-whisper\n"
            "NEVER pip-install whisper into /opt/homebrew/bin/python3."
            % os.path.relpath(ASR_PY, ROOT))
    out = cache or (os.path.splitext(wav)[0] + ".asr.json")
    p = subprocess.run([ASR_PY, "-c", _ASR_SNIPPET, wav, out, model],
                       stdout=sys.stderr, stderr=sys.stderr)
    if p.returncode != 0:
        raise SystemExit("mlx-whisper failed on %s" % wav)
    return json.load(open(out, encoding="utf-8"))


def asr_chunked(wav, points, workdir, model=ASR_MODEL):
    """Transcribe the WAV as several independent pieces cut at true silences.

    This exists because of a single, decisive failure: Whisper DEDUPES a
    repeated phrase inside one decode window. Run over the whole 28 s of
    at-tawbah-128-128 it returns the ayah exactly once -- the entire ibtida'
    pass is missing, and its 4.4 s are silently absorbed into the duration of
    the word before it. No alignment can recover a repeat the transcript does
    not contain.

    Cutting at the deepest point of a gap and decoding each piece separately
    fixes it: the second pass is in a different decode window, so it is
    transcribed again, and the repeat reappears as a backward jump in the DP.
    The cut cannot truncate a word, because it is placed in silence.

    -> [word dicts] with times already mapped back to the full WAV.
    """
    words = []
    for n, (t0, t1) in enumerate(zip(points, points[1:])):
        if t1 - t0 < 0.4:
            continue
        piece = os.path.join(workdir, "chunk%02d.wav" % n)
        cache = os.path.join(workdir, "chunk%02d.asr.json" % n)
        if not os.path.exists(piece):
            run([FFMPEG, "-hide_banner", "-nostats", "-loglevel", "error", "-y",
                 "-ss", "%.3f" % t0, "-t", "%.3f" % (t1 - t0), "-i", wav,
                 "-c", "copy", piece])
        d = asr(piece, cache=cache, model=model)
        for w in d["words"]:
            words.append({"w": w["w"], "t0": w["t0"] + t0, "t1": w["t1"] + t0,
                          "p": w["p"], "chunk": n})
    return {"words": words}


# ---------------------------------------------------------------------------
# reference text
# ---------------------------------------------------------------------------

def ref_words(surah, a, b):
    """The mushaf words of an ayah range, one entry per DISPLAY word.

    `qc.quran.tokens` splits the joined vocative (يَٰٓأَيُّهَا -> يا ايها), which is
    right for search and wrong here: a caption line is built from display
    words, so the skeleton has to stay 1:1 with them. We therefore normalise
    per whitespace-separated word and keep both super-alif readings as
    alternative spellings for matching.
    """
    out = []
    for v in Q.range(surah, a, b):
        for w in Q.nfc(v["ar"]).split():
            forms = {Q.normalize(w).replace(" ", ""),
                     Q.normalize(w, keep_super_alif=True).replace(" ", "")}
            forms.discard("")
            if not forms:
                continue
            out.append({"ar": w, "surah": v["surah"], "ayah": v["ayah"],
                        "forms": sorted(forms)})
    return out


def _sim(asr_form, forms):
    """0..1 similarity of one ASR word against a mushaf word's spellings."""
    best = 0.0
    for f in forms:
        if asr_form == f:
            return 1.0
        r = difflib.SequenceMatcher(None, asr_form, f).ratio()
        if r > best:
            best = r
    return best


# ---------------------------------------------------------------------------
# the repeat-permitting DP
# ---------------------------------------------------------------------------

def align(asr_words, ref, cost_jump=COST_JUMP):
    """Align ASR words onto reference words, allowing the ref pointer to jump.

    States are (i, j): i ASR words consumed, reference pointer at j. Edges:

        (i,j) -> (i+1, j+1)   match/substitute        1 - 2*sim
        (i,j) -> (i,   j+1)   ref word not spoken     COST_GAP_REF
        (i,j) -> (i+1, j)     ASR word not in text    COST_GAP_ASR
        (i,j) -> (i,   k)     JUMP, any k != j+1      cost_jump

    The jump edge is the whole point. With it, an ibtida' restart is simply
    the cheapest path: pay one jump, then match the repeated words for free,
    instead of paying a substitution for every repeated word (which is what a
    monotone NW is forced to do, and why it smears restarts into noise).

    -> (events, jumps). `events` is [{ref, i, t0, t1, sim}] in recitation
    order; `jumps` is [{at_time, from_ref, to_ref, kind}].
    """
    n, m = len(asr_words), len(ref)
    if not n or not m:
        return [], []
    forms = [r["forms"] for r in ref]
    aw = [w["form"] for w in asr_words]

    INF = float("inf")
    # D[i][j], and back[i][j] = (pi, pj, op)
    D = [[INF] * (m + 1) for _ in range(n + 1)]
    B = [[None] * (m + 1) for _ in range(n + 1)]
    # Recitation may begin at any reference word (the window need not start on
    # an ayah boundary), so every start state is free.
    for j in range(m + 1):
        D[0][j] = 0.0

    def relax_row(i):
        # within-row edges: ref gaps (j -> j+1) and jumps (j -> any k)
        for _ in range(2):
            for j in range(m):
                if D[i][j] + COST_GAP_REF < D[i][j + 1]:
                    D[i][j + 1] = D[i][j] + COST_GAP_REF
                    B[i][j + 1] = (i, j, "gap_ref")
            best_j = min(range(m + 1), key=lambda j: D[i][j])
            base = D[i][best_j] + cost_jump
            for k in range(m + 1):
                if k != best_j + JUMP_FREE and base < D[i][k]:
                    D[i][k] = base
                    B[i][k] = (i, best_j, "jump")

    relax_row(0)
    for i in range(n):
        for j in range(m + 1):
            if D[i][j] == INF:
                continue
            # ASR word consumed with no reference word
            if D[i][j] + COST_GAP_ASR < D[i + 1][j]:
                D[i + 1][j] = D[i][j] + COST_GAP_ASR
                B[i + 1][j] = (i, j, "gap_asr")
            if j < m:
                c = 1.0 - 2.0 * _sim(aw[i], forms[j])
                if D[i][j] + c < D[i + 1][j + 1]:
                    D[i + 1][j + 1] = D[i][j] + c
                    B[i + 1][j + 1] = (i, j, "match")
        relax_row(i + 1)

    # End anywhere: recitation may stop mid-ayah.
    j = min(range(m + 1), key=lambda k: D[n][k])
    i = n
    path = []
    while B[i][j] is not None:
        pi, pj, op = B[i][j]
        path.append((pi, pj, op, i, j))
        i, j = pi, pj
    path.reverse()

    events, jumps = [], []
    prev_ref = None
    for pi, pj, op, ci, cj in path:
        if op == "match":
            w = asr_words[pi]
            events.append({"ref": pj, "i": pi, "t0": w["t0"], "t1": w["t1"],
                           "sim": _sim(aw[pi], forms[pj]), "asr": w["w"]})
            prev_ref = pj
        elif op == "jump":
            t = asr_words[pi]["t0"] if pi < n else asr_words[-1]["t1"]
            frm = prev_ref if prev_ref is not None else pj
            kind = "restart" if cj <= frm else "skip"
            jumps.append({"t": t, "from_ref": frm, "to_ref": cj, "kind": kind})

    # A jump that lands where the path was already heading is bookkeeping, not
    # a restart; and a jump before any word was matched is just the entry
    # point. Keep only real backward moves.
    jumps = [j_ for j_ in jumps
             if j_["kind"] == "restart" and j_["to_ref"] < j_["from_ref"]]

    # Fill in reference words the ASR never emitted, so a caption card that
    # contains one still has usable bounds: interpolate between neighbours.
    events = _fill_gaps(events, ref)
    return events, jumps


def _fill_gaps(events, ref):
    """Insert synthetic events for ref words skipped between two matches.

    A word Whisper dropped inside a melisma still has to be captioned. Its
    time is interpolated across the hole and marked `sim: None`, so callers
    can tell a measured onset from a guessed one.
    """
    out = []
    for k, e in enumerate(events):
        if out:
            prev = out[-1]
            hole = e["ref"] - prev["ref"]
            if 1 < hole <= 4 and e["t0"] > prev["t1"]:
                span = (e["t0"] - prev["t1"]) / float(hole)
                for h in range(1, hole):
                    out.append({"ref": prev["ref"] + h, "i": None,
                                "t0": prev["t1"] + span * (h - 1),
                                "t1": prev["t1"] + span * h,
                                "sim": None, "asr": ""})
        out.append(e)
    return out


def asr_forms(data):
    """ASR json -> [{'w','form','t0','t1'}], dropping non-Arabic tokens."""
    out = []
    for w in data["words"]:
        f = Q.normalize(w["w"]).replace(" ", "")
        if not f:
            continue
        out.append({"w": w["w"], "form": f, "t0": w["t0"], "t1": w["t1"]})
    return out


def run_window(src, start, end, surah, a, b, workdir, pad=PAD):
    """The whole alignment step. -> dict with wav_t0, events, jumps, ref, asr.

    Times in the result are WAV-relative (add `wav_t0` for absolute source
    time). `start`/`end` are absolute source seconds.
    """
    from . import energy as E
    tag = "%.2f-%.2f" % (start, end)
    workdir = os.path.join(workdir, tag)
    os.makedirs(workdir, exist_ok=True)
    wav = os.path.join(workdir, "win.wav")
    if not os.path.exists(wav):
        extract_wav(src, start, end, wav, pad=pad)
    wav_t0 = max(0.0, float(start) - pad)

    times, db = E.envelope(wav)
    speech = E.speech_level(db)
    ds = E.dips(times, db, speech)
    pts = E.chunk_points(ds, 0.0, times[-1])
    data = asr_chunked(wav, pts, workdir) if len(pts) > 2 \
        else asr(wav, cache=os.path.join(workdir, "win.asr.json"))

    words = asr_forms(data)
    ref = ref_words(surah, a, b)
    events, jumps = align(words, ref)
    return {"wav": wav, "wav_t0": wav_t0, "events": events, "jumps": jumps,
            "ref": ref, "asr": words, "window": (float(start), float(end)),
            "times": times, "db": db, "speech": speech, "dips": ds,
            "chunks": pts}
