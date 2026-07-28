"""qc.author.energy -- RMS envelope, dip finding, waqf classification.

Alignment gives word ONSETS. It does not, and cannot, tell you whether the
moment between two words is a real stop. That is an energy question, and it is
the question the whole clip hangs on: a caption swap placed inside continuous
recitation reads as a mistake, and a segment that ends before the waqf lets the
next ayah leak into the tail.

    Why the envelope, not the ASR. Whisper emits CONTIGUOUS word spans -- the
    end of one word is the start of the next, always -- so there is no such
    thing as an ASR-visible pause. Every gap in this pipeline is found here.

    Why not `silencedetect`. It is a fixed absolute threshold, and under the
    Haram's reverb the noise floor sits around -22 dB, so `noise=-35dB` finds
    nothing at all (learned on ash-shuara-61-62, SKILL.md:100-103). Everything
    here is measured RELATIVE to the clip's own speech level instead, so a
    reverberant hall and a dry studio are judged on the same scale.

    What "waqf" means numerically. A true stop drops the envelope to the room
    floor: 30-45 dB below speech in a dry room, 15-25 dB under heavy reverb. A
    breath inside one continuous breath-group only dips 10-15 dB, because the
    reverb tail never decays. Anything shallower than WAQF_DB below speech is
    reported as a NON-WAQF boundary and said so in the emitted comments -- the
    owner needs to know when a caption swap sits inside continuous recitation,
    because that is a layout judgement, not a fact about the audio (see
    clips/at-tawbah-128-128, whose P1->P2 boundary troughs to only -24 dB
    against -10 dB speech).

The 50 ms analysis window is deliberate: it is what the hand-built envelopes in
SKILL.md used, so the dB numbers written into clip.yaml comments stay
comparable with the ones already recorded in the shipped clips.
"""
import array
import math
import wave

HOP = 0.010          # 10 ms envelope
WIN = 0.050          # 50 ms analysis window -- matches the hand-built envelopes
WAQF_DB = 15.0       # depth below speech under which a gap is NOT a true waqf
WAQF_SUSTAIN = 0.20  # ...and for how long it has to stay down there
DIP_PROM = 5.0       # topographic prominence to count as a dip at all
FLOOR_DB = -90.0

# DEPTH ALONE IS NOT ENOUGH, and this is the one measurement that took a
# rewrite to get right. On at-tawbah-128-128 six separate moments trough 16 dB
# below speech, and only two of them are stops: the rest are the closure of a
# geminated consonant (the doubled ta of عَنِتُّمْ momentarily hits -24.7 dB) or
# the gap between a word's final sukun and the next attack. What separates
# them is DURATION, and it separates them completely -- the two true waqfs
# stay below speech-15 dB for 0.65 s and 0.37 s, while every impostor is back
# above it within 0.06 s. Cutting on depth alone put an ASR chunk boundary in
# the middle of أَنفُسِكُمْ, which Whisper then heard as أخسكم.


def envelope(wav_path, hop=HOP, win=WIN):
    """16 kHz mono WAV -> (times, dB). Stdlib only; the render venv has no numpy."""
    with wave.open(wav_path, "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError("expected 16-bit mono WAV, got %d ch / %d bytes"
                             % (w.getnchannels(), w.getsampwidth()))
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    s = array.array("h")
    s.frombytes(raw)
    n = len(s)
    h = max(1, int(round(hop * sr)))
    k = max(h, int(round(win * sr)))
    # Prefix sums of squares: O(n) rather than O(n*win), which matters only
    # because this is pure Python.
    ps = [0.0] * (n + 1)
    acc = 0.0
    for i, v in enumerate(s):
        acc += float(v) * float(v)
        ps[i + 1] = acc
    times, db = [], []
    i = 0
    half = k // 2
    while i < n:
        lo, hi = max(0, i - half), min(n, i + half)
        m = (ps[hi] - ps[lo]) / float(max(1, hi - lo))
        r = math.sqrt(m) / 32768.0
        times.append(i / float(sr))
        db.append(20.0 * math.log10(r) if r > 1e-9 else FLOOR_DB)
        i += h
    return times, db


def speech_level(db, pct=0.90):
    """The clip's own speech level: the `pct` quantile of the envelope.

    Not the max -- one plosive would set the scale. Not the mean either, since
    a clip that is a third silence would drag it down.
    """
    xs = sorted(db)
    return xs[min(len(xs) - 1, int(pct * len(xs)))] if xs else 0.0


def floor_level(db, pct=0.05):
    xs = sorted(db)
    return xs[max(0, int(pct * len(xs)))] if xs else FLOOR_DB


def _idx(times, t, hop=HOP):
    return max(0, min(len(times) - 1, int(round(t / hop))))


def dips(times, db, speech, min_prom=DIP_PROM, reach=2.0, hop=HOP):
    """Every local minimum of the envelope worth considering as a boundary.

    Each dip carries two different depths, and they answer different questions:

      `prom`    topographic prominence -- how much the envelope has to climb
                to escape this minimum. This is what separates a real
                inter-word gap from the micro-minimum between two syllables.
      `depth`   speech level minus the trough. The number quoted in clip.yaml
                comments, and what the shipped clips' comments already quote.
      `sustain` how long the envelope stays below speech - WAQF_DB around the
                trough. See the note above: this, not depth, is what actually
                distinguishes a stop from a consonant closure.

    `t1`/`t0` are where the envelope falls through, and climbs back above, the
    half-way level between speech and the trough -- i.e. where the previous
    phrase has died away and the next one comes up.
    """
    n = len(db)
    span = int(reach / hop)
    out = []
    i = 1
    while i < n - 1:
        if not (db[i] <= db[i - 1] and db[i] <= db[i + 1]):
            i += 1
            continue
        # flat minima: take the middle
        j = i
        while j + 1 < n and db[j + 1] == db[i]:
            j += 1
        c = (i + j) // 2
        v = db[c]
        lmax = v
        k = c
        while k > 0 and c - k < span:
            k -= 1
            if db[k] < v:
                break
            lmax = max(lmax, db[k])
        rmax = v
        k = c
        while k < n - 1 and k - c < span:
            k += 1
            if db[k] < v:
                break
            rmax = max(rmax, db[k])
        prom = min(lmax, rmax) - v
        if prom >= min_prom:
            depth = speech - v
            half = v + 0.5 * max(depth, prom)
            t1 = _cross_back(times, db, c, half)
            t0 = _cross_fwd(times, db, c, half)
            lvl = speech - WAQF_DB
            sustain = (_cross_fwd(times, db, c, lvl)
                       - _cross_back(times, db, c, lvl))
            out.append({"t": times[c], "db": v, "prom": prom, "depth": depth,
                        "sustain": sustain, "t1": t1, "t0": t0,
                        "waqf": depth >= WAQF_DB and sustain >= WAQF_SUSTAIN})
        i = j + 1
    # keep only the deepest dip of any cluster within 150 ms
    out.sort(key=lambda d: d["t"])
    merged = []
    for d in out:
        if merged and d["t"] - merged[-1]["t"] < 0.15:
            if d["depth"] > merged[-1]["depth"]:
                merged[-1] = d
        else:
            merged.append(d)
    return merged


def _cross_back(times, db, i, level):
    k = i
    while k > 0 and db[k] < level:
        k -= 1
    return times[k]


def _cross_fwd(times, db, i, level):
    k = i
    while k < len(db) - 1 and db[k] < level:
        k += 1
    return times[k]


def nearest_dip(ds, t, radius=0.6):
    cand = [d for d in ds if abs(d["t"] - t) <= radius]
    return min(cand, key=lambda d: (-d["depth"], abs(d["t"] - t))) if cand else None


def chunk_points(ds, t_lo, t_hi, min_chunk=3.0):
    """Times at which it is safe to cut the audio into separate ASR passes.

    Whisper DEDUPES a phrase repeated inside one decode window: on
    at-tawbah-128-128 the whole ibtida' pass simply vanished from the
    transcript, and its 4.4 s were absorbed into the duration of the word
    before it. Cutting the audio at true silences and transcribing each piece
    independently is what makes the repeat visible again -- and it cannot
    truncate a word, because the cut is at the deepest point of a gap.
    """
    pts = [t_lo]
    for d in sorted(ds, key=lambda d: d["t"]):
        if not d["waqf"]:
            continue
        if d["t"] - pts[-1] < min_chunk or t_hi - d["t"] < min_chunk:
            continue
        pts.append(d["t"])
    pts.append(t_hi)
    return pts


def onset(ds, times, db, t_hint, speech, back=2.0, rise_db=18.0, hop=HOP):
    """When recitation actually starts, for the IG hook.

    The head has to begin ~0.1-0.3 s before the first sound (SKILL.md:96-99),
    and Whisper's own first-word start is routinely 100-300 ms late because it
    anchors on the vowel rather than the attack. So the onset is taken from
    the envelope: the moment the silence BEFORE the passage climbs back
    through the half-way level. If there is no such silence in the window
    (the pad landed inside the previous ayah) fall back to a threshold walk.
    """
    # `+ 0.5`: Whisper's first word in a decode chunk routinely starts BEFORE
    # the audio does -- it anchors on the chunk, not the attack -- so the
    # silence we are looking for can sit slightly after the hint.
    cand = [d for d in ds if d["waqf"] and t_hint - back <= d["t"] <= t_hint + 0.5]
    if cand:
        return max(cand, key=lambda d: d["t"])["t0"]
    level = speech - rise_db
    i = _idx(times, t_hint, hop)
    j = i
    while j > 0 and db[j] >= level:
        j -= 1
    if j < i:
        return times[j + 1]
    for k in range(_idx(times, max(0.0, t_hint - back), hop), len(db)):
        if db[k] >= level:
            return times[k]
    return t_hint


def tail(ds, t_last, limit=2.5, lead=0.4):
    """The waqf gap that closes the passage. -> dip dict, or None.

    `t_last` is the ASR end of the final word, which under a long madd is
    routinely a second early -- Whisper stops emitting once the word is
    identified. So the search starts a little before it and reaches well past.
    """
    cand = [d for d in ds if d["waqf"] and d["t"] >= t_last - lead
            and d["t"] <= t_last + limit]
    return min(cand, key=lambda d: d["t"]) if cand else None


def gap_mid(d, cap=0.75):
    """A time INSIDE a waqf gap: halfway across it, capped.

    Ending a segment here rather than at the trough gives the last madd its
    full decay without reaching the next ayah's attack -- "ends mid
    true-silence gap", as clips/al-anam-122-122 puts it.
    """
    return min(0.5 * (d["t1"] + d["t0"]), d["t1"] + cap)
