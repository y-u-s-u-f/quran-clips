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
       twice. The DP below therefore has a WRAPAROUND edge that lets the
       reference pointer move backwards, priced by how far back it reaches.
       Restarts then FALL OUT of the alignment as backward wraps and are
       reported, instead of being hunted pause by pause by ear.

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
from . import asr as _asr

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Kept as names because callers and tests referenced them; both now derive from
# the resolved backend instead of hardcoding an mlx model and a venv path.
ASR_PY = _asr.interpreter()
ASR_MODEL = None                 # None => qc.author.asr picks per backend

PAD = 2.0            # seconds of context either side of the requested window


def run(cmd):
    """qc.proc.run, but echoing to STDERR.

    `qc author -n` writes the clip.yaml to stdout, and the repo's convention of
    echoing every ffmpeg command line would otherwise paste itself into the
    top of the file when it is piped somewhere.
    """
    print("+ " + " ".join(shlex.quote(c) for c in cmd), file=sys.stderr)
    return subprocess.run(cmd)


# --- DP costs. Deliberately few, and all in the same "one substitution" unit:
# the match edge scores `1 - 2*sim`, so a perfect match is worth REWARD = 1.00
# and a total mismatch costs 1.00. Every number below is a multiple of that.
COST_GAP_REF = 0.60   # a mushaf word Whisper never emitted (melisma, dropped)
COST_GAP_ASR = 0.60   # a Whisper word with no mushaf counterpart (hallucination)

# The CAP on a forward run of COST_GAP_REF. A decoder that loses a whole ayah
# body loses it in ONE failure, not once per word, so the linear charge
# over-prices exactly the case that occurs; past three words it stops growing.
# 1.80 = 3 x COST_GAP_REF, and is exactly the flat price the previous
# formulation charged for the forward half of its any-to-any jump; it is left
# alone so that forward motion -- the only off-sequence edge a clip without a
# repeat can use -- is priced as it was before this change.
# This cap is also the STRUCTURAL constraint that makes the wrap gate feasible
# at all; see the safety argument in align().
COST_SKIP = 1.80

# The backward wrap: an ibtida'. Priced flat + per reference word reached back
# over, W(d) = COST_JUMP + COST_JUMP_SPAN * d. The span term is not decoration.
# On a contiguous rewind the pointer sits exactly L past the landing word, so
# d == L, where L is the number of words about to be re-recited; and re-matching
# those L words is worth (REWARD + COST_GAP_ASR) * L = 1.60 * L, because each
# one both earns a match and saves the DP from absorbing it as a stray ASR word.
# A FLAT price cannot gate a reward that grows linearly in L -- that is how a
# refrain wrap gets bought. W(d) tracks it because d == L.
#   DETECTION  W(L) < 1.60 * L, so a real restart is worth buying.
#   SAFETY     W(d) > COST_SKIP for every d >= 1. COST_SKIP caps everything a
#              detour into an identical earlier copy could ever save, so once
#              the wrap costs more than the cap, no detour can pay for itself
#              at ANY distance.
#
# SAFETY IS QUANTIFIED OVER EVERY d, NOT OVER "REFRAIN-LENGTH" d. An earlier
# draft of this block asserted it only for d >= 3, reasoning that the shortest
# Quranic refrain is three display words. That reasoning does not survive: the
# DP does not wrap by refrain, it wraps by whatever distance is cheapest, and a
# one- or two-word rewind onto a single repeated word is both legal and common
# (Quranic Arabic repeats single words constantly). Priced at 0.80 + 0.50 the
# gate had a hole at exactly those distances -- W(1) = 1.30 < 1.80 and
# W(2) = 1.80, a tie -- and a straight-through recitation with a three-word
# dropout was captioned as an ibtida'.
#
# 1.60 + 0.40 closes it: W(1) = 2.00 clears COST_SKIP by 0.20, and detection
# still holds from L = 2 (W(2) = 2.40 against 3.20, 0.80 of margin).
#
# The cost of that is a DELIBERATE, DOCUMENTED MISS: a one-word restart
# (W(1) = 2.00 against a 1.60 reward) is not detected. That is the right side
# to miss on -- a single repeated word is far more often an accidental overlap
# than an ibtida', and the failure modes are not symmetric. A missed restart
# costs one clip a hand-fix; a false restart duplicates Qur'anic words on
# screen and drops real ayat from the timeline.
COST_JUMP = 1.60
COST_JUMP_SPAN = 0.40


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


def asr(wav, cache=None, model=None):
    """Word timestamps over the window WAV. -> the `qc.author.asr` contract.

    Cached to `cache` (json), because the DP and the energy pass get re-run
    far more often than the audio changes.

    The engine is chosen by `qc.author.asr` (mlx-whisper on Apple silicon,
    faster-whisper elsewhere) rather than hardcoded here, so the authoring half
    of the pipeline runs off macOS. A cache written by a different backend is
    still honoured -- re-transcribing would be worse -- but it is reported,
    because the two runtimes do not emit identical word boundaries and a silent
    swap would move cut points under a clip that was already reviewed.
    """
    if cache and os.path.exists(cache):
        d = json.load(open(cache, encoding="utf-8"))
        was = d.get("backend")
        if was and was != _asr.backend():
            print("  note: %s was transcribed by the %r backend, now running "
                  "%r -- reusing the cache (delete it to re-transcribe)"
                  % (os.path.relpath(cache), was, _asr.backend()),
                  file=sys.stderr)
        return d
    out = cache or (os.path.splitext(wav)[0] + ".asr.json")
    return _asr.transcribe(wav, out, model=model)


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

def align(asr_words, ref, cost_jump=COST_JUMP, cost_span=COST_JUMP_SPAN,
          cost_skip=COST_SKIP):
    """Align ASR words onto reference words, with bounded backward wraparound.

    States are (i, j): i ASR words consumed, reference pointer at j. Edges:

        (i,j) -> (i+1, j+1)   match / substitute   1 - 2*sim
        (i,j) -> (i+1, j)     ASR word not in text COST_GAP_ASR
        (i,j) -> (i,   j+1)   ref word not spoken  COST_GAP_REF
        (i,j) -> (i,   k>j+1) SKIP forward         min(COST_GAP_REF*(k-j),
                                                       COST_SKIP)
        (i,j) -> (i,   k<j)   WRAP backward        COST_JUMP
                                                   + COST_JUMP_SPAN*(j-k)

    The two off-sequence edges used to be one free-for-all "jump to any k at a
    flat price", and they are split because forward and backward mean opposite
    things. Forward is the DECODER's failure -- a run of mushaf words Whisper
    never emitted -- and one melisma swallowing six words is one failure, so
    the linear charge is capped at COST_SKIP. Backward is the RECITER's choice
    -- an ibtida' -- and it is the thing we are trying to detect.

    WHY THE WRAP IS PRICED PER WORD, which is the important comment in this
    file. Both a real restart and a spurious rewind into an identical earlier
    refrain ("fa-bi-ayyi ala'i rabbikuma tukadhdhiban", 31 times in ar-Rahman)
    are backward moves of indistinguishable distance -- the genuine ibtida' in
    at-tawbah 9:128 reaches back 4 reference words, and so does a rewind from
    one ar-Rahman refrain into the previous one, that refrain being 4 words
    long -- so no hard bound on the distance separates them. What separates
    them is what the wrap BUYS:

      * A real restart re-matches L words the reciter genuinely said twice.
        On a contiguous rewind the pointer sits exactly L past the landing
        word, so d == L, and the wrap is worth (1 + COST_GAP_ASR) * L: each
        re-matched word earns its match AND saves the DP from writing the
        second utterance off as a stray ASR word. That reward is LINEAR in L,
        which is why a flat price cannot gate it and COST_JUMP_SPAN must.
      * A spurious rewind into an identical earlier copy of a refrain
        re-matches the same L words either way -- against copy #2 instead of
        copy #1 -- so the match reward cancels exactly and it buys only the
        reference words the forward route would otherwise have had to cross.
        COST_SKIP caps that at 1.80 however many words the decoder lost, and
        W(d) > COST_SKIP for every d >= 1 -- every distance, not merely the
        ones a refrain is long enough to present. The DP wraps by whatever
        distance is cheapest, not by refrain, so the guarantee has to hold at
        d = 1 or it does not hold at all.

    Without the cap the detour payoff is COST_GAP_REF * (words lost), which
    grows without limit, and then NO pair of wrap constants is both safe on
    ar-Rahman and sensitive on 9:128 -- the feasible band is empty. The cap is
    what makes one exist; it is a structural constraint, not a tuning knob.

    Within a row the edges form cycles (forward then back), so the row is
    relaxed forward, backward, forward. That is exact rather than iterated to a
    fixed point because every longer combination is dominated: going forward t
    words and then wrapping to k costs strictly more than wrapping straight to
    k, and two wraps in a row cost two COST_JUMPs to reach a point one reaches
    for one.

    O(n*m) states with O(1) amortised work each -- the wrap relaxation is a
    suffix minimum, not a scan over targets, so it does not cost O(m^2) -- plus
    the n*m `_sim` calls, which dominate. A window is ~25 s: a few hundred ASR
    words against a few dozen reference words. Measured at 369 ASR words
    against 123 reference words, an order of magnitude past anything `qc
    author` asks for, the whole DP is 0.33 s.

    -> (events, jumps). `events` is [{ref, i, t0, t1, sim, asr}] in recitation
    order; `jumps` is [{t, from_ref, to_ref, kind, span, cost, i}], where
    `span` is the distance the DP priced and `cost` is what it paid -- the two
    things the flat formulation could not state. The pointer sits one past
    `from_ref` when it wraps, so span == from_ref - to_ref + 1.
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

    def forward(i):
        """Ref gaps j -> j+1, then the flat cap on a run of them."""
        d, b = D[i], B[i]
        for j in range(m):
            c = d[j] + COST_GAP_REF
            if c < d[j + 1]:
                d[j + 1] = c
                b[j + 1] = (i, j, "gap_ref")
        # `best` trails two behind k, so it is the cheapest state from which a
        # SKIP (a move of two or more) can start.
        best, best_j = INF, None
        for k in range(m + 1):
            if best + cost_skip < d[k]:
                d[k] = best + cost_skip
                b[k] = (i, best_j, "skip")
            if k and d[k - 1] < best:
                best, best_j = d[k - 1], k - 1

    def backward(i):
        """Wraps k < j, priced cost_jump + cost_span * (j - k).

        The cheapest source for a landing at k is the j > k minimising
        d[j] + cost_span*j, so one right-to-left suffix minimum prices every
        landing at once -- O(m), not O(m^2).
        """
        d, b = D[i], B[i]
        best, best_j = INF, None
        landing = [None] * (m + 1)
        for k in range(m, -1, -1):
            if best < INF:
                landing[k] = (best + cost_jump - cost_span * k, best_j)
            v = d[k] + cost_span * k
            if v < best:
                best, best_j = v, k
        for k in range(m + 1):
            if landing[k] and landing[k][0] < d[k]:
                d[k] = landing[k][0]
                b[k] = (i, landing[k][1], "wrap")

    def relax_row(i):
        forward(i)
        backward(i)
        forward(i)

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
        elif op == "wrap":
            t = asr_words[pi]["t0"] if pi < n else asr_words[-1]["t1"]
            frm = prev_ref if prev_ref is not None else pj
            jumps.append({"t": t, "from_ref": frm, "to_ref": cj,
                          "kind": "restart", "span": pj - cj,
                          "cost": cost_jump + cost_span * (pj - cj), "i": pi})

    # A wrap that lands on the word just matched is one aborted word, not an
    # ibtida'; it belongs in `events` (he said it twice) but reporting it as a
    # restart would put a caption split inside a single word. And a wrap that
    # does not end up behind the last matched word is bookkeeping. Keep only
    # real backward moves. `kind` stays on every survivor for the callers that
    # read it; forward motion can no longer reach this list at all.
    jumps = [j_ for j_ in jumps if j_["to_ref"] < j_["from_ref"]]

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
