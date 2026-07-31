"""qc.author.coherence -- does this candidate passage MEAN something on its own?

`propose` ranks windows on structure: match confidence, duration, whether the
window opens on an ayah's first word and closes on its last, coverage,
cleanliness, span-fit, bars splittability. Every one of those is a property of
the container. None of them reads the passage.

The failure that motivated this module: on vqxYwdR4RvQ the #1 bars candidate was
Al-Qamar 54:2-4, structurally perfect -- whole ayat, clean edges, all cards in
cap. But 54:2 opens "And if they see a sign, they turn away..." and *the sign*
is the splitting of the moon in 54:1, which the window excludes. The clip opens
on a dangling referent. 54:1-4 is both coherent and the more famous opening, and
nothing in the structural score can prefer it.

So a model is asked, per candidate: does this passage stand alone, or does it
open on a pronoun / conjunction / definite reference whose antecedent is in a
preceding ayah, or end mid-thought?

    THE MODEL NEVER SEES OR RETURNS ARABIC. It is handed the surah name, ayah
    NUMBERS, and the English (Sahih International) of the clip plus two ayat of
    context either side, and must answer with a verdict and at most a suggested
    pair of ayah NUMBERS. This code resolves the numbers back to mushaf text it
    already holds, so corruption of the Uthmani codepoints is structurally
    impossible -- the same standing rule as `linebreak`. A reply containing any
    Arabic codepoint is rejected outright rather than sanitised, because a model
    that ignored that instruction ignored the others too.

Everything degrades to the structural ranking in silence: a missing CLI, a
timeout, unparseable output, an out-of-range suggestion. `qc propose` must never
fail because the judge was unavailable, and the report says which path was used.
"""
import json
import os
from .. import env as _env
import re
import subprocess

from .. import quran as Q
from . import fetch

ROOT = fetch.ROOT

# `QC_CLAUDE_BIN` still wins; otherwise resolve on PATH rather than
# assuming a Homebrew prefix. None => the judge degrades to its
# documented fallback, which is already handled below.
CLAUDE = os.environ.get("QC_CLAUDE_BIN") or _env.find("claude") or "claude"
MODEL = "claude-opus-5"
EFFORT = "low"
TIMEOUT = 30.0

CACHE = os.path.join(ROOT, "sources", "_coherence.json")

CONTEXT_AYAT = 2        # how many ayat either side the model sees as context
MAX_EXTEND = 2          # a fix may move an edge by at most this many ayat
PENALTY = 0.80          # multiplier for a passage judged not to stand alone

VERDICTS = ("standalone", "needs_start_earlier", "needs_end_later", "both")

# Model output is English-only by construction; anything in these blocks means
# the instruction was not followed.
_ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")

R_OK = "llm"
R_CACHE = "llm (cached)"
R_MISSING = "structural only (claude CLI not found)"
R_ERROR = "structural only (claude CLI failed or timed out)"
R_PARSE = "structural only (judge output did not parse)"
R_ARABIC = "structural only (judge returned Arabic -- rejected)"
R_RANGE = "structural only (judge suggested an out-of-range fix)"
R_OFF = "structural only (judge disabled)"


# ---------------------------------------------------------------------------
# cache -- keyed by (surah, first, last); re-running a proposal is free
# ---------------------------------------------------------------------------

_VER = None


def _ver():
    """Short digest of the prompt + model + effort.

    Part of every cache key, because the verdicts are a function of the question
    as much as of the passage: editing PROMPT below changed 54:7-9 from
    standalone to needs_start_earlier, and a cache keyed on the ayat alone would
    have gone on serving the old answer forever.
    """
    global _VER
    if _VER is None:
        import hashlib
        h = hashlib.sha1(("%s|%s|%s" % (PROMPT, MODEL, EFFORT)).encode("utf-8"))
        _VER = h.hexdigest()[:8]
    return _VER


def _key(s, a, b):
    return "%s %d:%d-%d" % (_ver(), s, a, b)


def _cache_read():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _cache_write(d):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# the model call
# ---------------------------------------------------------------------------

PROMPT = """\
You are judging whether a short passage of the Qur'an works as a STANDALONE
video clip. You see only the English translation (Sahih International); you must
never write Arabic.

Surah %(name)s (%(surah)d). The clip is ayat %(a)d-%(b)d.

BEFORE the clip (context only -- NOT part of the clip):
%(before)s

THE CLIP (ayat %(a)d-%(b)d):
%(body)s

AFTER the clip (context only -- NOT part of the clip):
%(after)s

Judge two things.

1. OPENING. Read the clip's FIRST ayah next to the ayah immediately before it.
   Does that preceding ayah supply something the opening depends on -- a pronoun
   or "they" / "it" / "that Day", a definite reference ("the sign", "the
   punishment"), or an event that the opening is a REACTION or CONSEQUENCE to?
   Ask literally: after hearing only the clip, could a viewer say what is being
   referred to? If the answer is in the ayah right before, the clip does not
   stand alone. Grammar alone does not settle it: a noun that reads as
   indefinite in English can still be pointing at whatever the previous ayah
   just described. Ask what the passage is ABOUT, not how it is worded.

   One rule is absolute and the guard rails below do NOT override it: if the
   clip's opening sentence contains a bare pronoun -- "it", "he", "they",
   "them", "this" -- naming something that is introduced only before the clip,
   the clip does not stand alone.

2. CLOSING. Does the clip end mid-thought -- cutting off the apodosis of a
   conditional ("if X..." with the "then Y" in the next ayah), a list, an answer
   to a question it just posed, or a consequence that lands after it?

Guard rails, so you do not "fix" passages that are already fine:

  - A clip of a SINGLE complete ayah stands alone unless its own sentence is
    unfinished. Never extend a single ayah merely because the next one continues
    the theme or the story; a whole ayah chosen on its own is a deliberate
    choice, not an accident.
  - An opening that addresses the listener or makes a fresh statement -- "O you
    who have believed", "O mankind", "Indeed, those who...", a question, a named
    subject -- stands alone even when it begins with "And".
  - Adding context that would merely enrich the passage is NOT a reason. Only a
    viewer left asking "who/what is this about?" or left hanging mid-sentence is.

If it does NOT stand alone, propose the smallest fix: move the start earlier
and/or the end later by AT MOST %(ext)d ayat, staying inside surah %(surah)d.
If no fix that small works, answer standalone.

Reply with ONE line of JSON and nothing else -- no prose, no code fence:

{"verdict": "standalone|needs_start_earlier|needs_end_later|both",
 "first": <first ayah number of the range you recommend>,
 "last": <last ayah number of the range you recommend>,
 "reason": "<one short English sentence, under 140 characters>"}

For "standalone", first and last must be %(a)d and %(b)d. The reason must be
plain English -- no Arabic script, no transliteration of the verse.
"""


def _en(s, n):
    try:
        return Q.ayah(s, n)["en"]
    except Exception:
        return None


def _lines(s, lo, hi):
    out = []
    for n in _clamp_range(s, lo, hi):
        en = _en(s, n)
        if en:
            out.append("  %d:%d  %s" % (s, n, en))
    return "\n".join(out) or "  (none -- this is the edge of the surah)"


def _clamp_range(s, lo, hi):
    try:
        cnt = Q.ayah_count(s)
    except Exception:
        return []
    return [n for n in range(max(1, lo), min(cnt, hi) + 1)]


def _ask(s, a, b):
    """-> (dict verdict, reason) or (None, reason)."""
    if not (os.path.isfile(CLAUDE) and os.access(CLAUDE, os.X_OK)):
        return None, R_MISSING
    body = _lines(s, a, b)
    if body.startswith("  (none"):
        return None, R_PARSE
    prompt = PROMPT % {
        "name": Q.surah_name(s), "surah": s, "a": a, "b": b,
        "before": _lines(s, a - CONTEXT_AYAT, a - 1),
        "body": body,
        "after": _lines(s, b + 1, b + CONTEXT_AYAT),
        "ext": MAX_EXTEND,
    }
    cmd = [CLAUDE, "-p", "--model", MODEL, "--effort", EFFORT,
           "--output-format", "json"]
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=TIMEOUT, cwd=ROOT)
    except (OSError, subprocess.SubprocessError):
        return None, R_ERROR
    if p.returncode != 0:
        return None, R_ERROR
    out = (p.stdout or "").strip()
    try:
        d = json.loads(out)
        out = str(d.get("result", "")).strip()
    except Exception:
        pass                      # `--output-format json` unavailable: use raw
    return _parse(out, s, a, b)


def _parse(out, s, a, b):
    """Strict: anything we cannot read exactly is a fallback, not a guess."""
    if _ARABIC.search(out):
        return None, R_ARABIC
    # Tolerate a code fence but nothing looser -- the payload itself must be one
    # JSON object with the four keys we asked for.
    out = out.strip()
    if out.startswith("```"):
        out = out.strip("`")
        out = out.split("\n", 1)[-1] if out[:4].lower().startswith("json") else out
    i, j = out.find("{"), out.rfind("}")
    if i < 0 or j < i:
        return None, R_PARSE
    try:
        d = json.loads(out[i:j + 1])
    except Exception:
        return None, R_PARSE
    if not isinstance(d, dict):
        return None, R_PARSE
    verdict = d.get("verdict")
    reason = d.get("reason")
    if verdict not in VERDICTS or not isinstance(reason, str) or not reason:
        return None, R_PARSE
    try:
        first, last = int(d["first"]), int(d["last"])
    except Exception:
        return None, R_PARSE
    reason = " ".join(reason.split())[:180]
    if _ARABIC.search(reason):
        return None, R_ARABIC

    if verdict == "standalone":
        if (first, last) != (a, b):
            return None, R_RANGE
    else:
        # A fix must be a small nudge outward, never a different passage.
        if not (a - MAX_EXTEND <= first <= a and b <= last <= b + MAX_EXTEND):
            return None, R_RANGE
        if (first, last) == (a, b):
            return None, R_RANGE
        try:
            if first < 1 or last > Q.ayah_count(s):
                return None, R_RANGE
        except Exception:
            return None, R_RANGE
    return {"verdict": verdict, "first": first, "last": last,
            "reason": reason}, R_OK


def judge(s, a, b, use_llm=True, cache=None):
    """-> (verdict dict or None, reason string). Cached by (surah, first, last)."""
    if not use_llm:
        return None, R_OFF
    own = cache is None
    cache = _cache_read() if own else cache
    key = _key(s, a, b)
    hit = cache.get(key)
    if isinstance(hit, dict) and hit.get("verdict") in VERDICTS:
        return hit, R_CACHE
    v, reason = _ask(s, a, b)
    if v is not None:
        cache[key] = v
        _cache_write(cache)
    return v, reason


# ---------------------------------------------------------------------------
# applying it to a ranked candidate list
# ---------------------------------------------------------------------------

def _overlap(x, y):
    return max(0.0, min(x["end"], y["end"]) - max(x["start"], y["start"]))


def _alt(all_cands, c, first, last):
    """The already-scored candidate for the suggested range, or None.

    Looking the fix up in the full candidate list rather than building it is
    what makes the fix FEASIBLE by construction: everything in that list came
    out of one contiguous same-surah run, sits inside MIN_DUR..MAX_DUR and
    MAX_AYAT, and (for bars) already carries its measured width score. If the
    extension is not in there, it is not a window we could have proposed anyway.
    """
    hits = [x for x in all_cands
            if x["surah"] == c["surah"] and x["ayah_from"] == first
            and x["ayah_to"] == last and _overlap(x, c) > 0]
    if not hits:
        return None
    return max(hits, key=lambda x: _overlap(x, c))


def apply(pool, all_cands, use_llm=True):
    """Judge `pool` in place-ish and return (new_pool, status).

    A candidate judged not to stand alone is down-ranked by PENALTY. Where the
    suggested extension exists as a scored candidate AND is itself judged
    standalone, it REPLACES the original, marked as adjusted -- the second call
    matters: without it the judge could push a good window into a worse one and
    nothing would ever check.
    """
    cache = _cache_read()
    reasons, out = [], []
    for c in pool:
        c = dict(c)
        v, reason = judge(c["surah"], c["ayah_from"], c["ayah_to"],
                          use_llm=use_llm, cache=cache)
        reasons.append(reason)
        if v is None:
            c["coherence"] = None
            out.append(c)
            continue
        if v["verdict"] == "standalone":
            c["coherence"] = {"verdict": "standalone", "reason": v["reason"]}
            out.append(c)
            continue

        alt = _alt(all_cands, c, v["first"], v["last"])
        av = None
        if alt is not None:
            av, ar = judge(c["surah"], v["first"], v["last"],
                           use_llm=use_llm, cache=cache)
            reasons.append(ar)
        if alt is not None and av is not None and av["verdict"] == "standalone":
            a2 = dict(alt)
            a2["coherence"] = {
                "verdict": "adjusted",
                "adjusted_from": "%d:%d-%d" % (c["surah"], c["ayah_from"],
                                               c["ayah_to"]),
                "reason": v["reason"],
                "confirm": av["reason"],
            }
            out.append(a2)
        else:
            c["coherence"] = {
                "verdict": v["verdict"],
                "reason": v["reason"],
                "wanted": "%d:%d-%d" % (c["surah"], v["first"], v["last"]),
                "fix_note": ("not a proposable window in this video"
                             if alt is None else
                             "that window exists, but the judge would not call "
                             "it standalone either"),
            }
            c["score"] = round(c["score"] * PENALTY, 4)
            out.append(c)

    # A replacement can collide with something already in the pool.
    seen, uniq = set(), []
    for c in out:
        k = (c["surah"], c["ayah_from"], c["ayah_to"], c["start"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    uniq.sort(key=lambda c: -c["score"])

    good = [r for r in reasons if r in (R_OK, R_CACHE)]
    if not reasons:
        status = R_OFF
    elif not good:
        status = reasons[0]
    elif len(good) < len(reasons):
        bad = [r for r in reasons if r not in (R_OK, R_CACHE)][0]
        status = "llm (%d/%d judged; rest fell back: %s)" % (
            len(good), len(reasons), bad)
    else:
        status = R_CACHE if all(r == R_CACHE for r in reasons) else R_OK
    return uniq, status
