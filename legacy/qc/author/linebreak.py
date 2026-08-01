"""qc.author.linebreak -- ask a model where a caption card's two lines should
break, and never let it near the text.

The width heuristic in `emit.split_lines` minimises the wider line. A human
setting these by hand breaks on MEANING, and the two rules disagree more often
than you would hope: for

    عَزِيزٌ عَلَيْهِ مَا عَنِتُّمْ حَرِيصٌ عَلَيْكُم

the owner chose 4|2 (a syntactic seam, `...مَا عَنِتُّمْ` closes the clause) and
the script chose 3|3, splitting the phrase mid-clause. That judgement is what
this module buys.

    THE MODEL NEVER RETURNS ARABIC. It is handed the card's words as a
    numbered list and must answer with ONE INTEGER: how many words go on line
    one. This code assembles the lines from the mushaf words it already holds,
    so text corruption is structurally impossible -- the Uthmani codepoints are
    fragile (madda alif, the small high marks, the superscript alif) and the
    standing rule in this repo is that no model ever retypes them. A split
    index cannot be misspelled.

`judge()` below is the shared plumbing for that pattern -- one binary, one
model, one effort setting, one set of failure reasons -- and is reused by
`qc.author.translate`, which splits an ayah's ENGLISH across its cards the
same way: numbered list in, one integer out.

Everything here degrades to the width heuristic in silence: a missing CLI, a
timeout, unparseable output, an out-of-range index, or a proposal that blows
the template's own width cap. `qc author` must never fail because the
line-breaker was unavailable -- and the emitted clip.yaml records which path
was taken, because that comment is the durable record.
"""
import hashlib
import json
import os
from .. import env as _env
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `QC_CLAUDE_BIN` still wins; otherwise resolve on PATH rather than
# assuming a Homebrew prefix. None => the judge degrades to its
# documented fallback, which is already handled below.
CLAUDE = os.environ.get("QC_CLAUDE_BIN") or _env.find("claude") or "claude"
MODEL = "claude-opus-5"
EFFORT = "low"          # `claude --effort low|medium|high|xhigh|max`
TIMEOUT = 30.0

CACHE = os.path.join(ROOT, "sources", "_linebreak.json")

# Reasons recorded in the clip.yaml comment.
R_LLM = "LLM (syntactic seam)"
R_CACHE = "LLM (syntactic seam, cached)"
R_MISSING = "width heuristic (LLM unavailable)"
R_ERROR = "width heuristic (LLM call failed)"
R_PARSE = "width heuristic (LLM output did not parse as an index)"
R_RANGE = "width heuristic (LLM index out of range)"
R_WIDTH = "width heuristic (LLM split overflowed the width cap)"
R_WORDS = "width heuristic (LLM put more than %d words on a line)"
R_NOMEASURE = "width heuristic (renderer measurement unavailable)"


# ---------------------------------------------------------------------------
# measurement -- the renderer's own, not a second opinion
# ---------------------------------------------------------------------------

_MEASURE = None


def _measurer():
    """-> (fn(line) -> ink width px, max width px), or None.

    Borrows scripts/render_bars.py: the same font, the same RAQM shaping, the
    same baseline-anchored ink bbox, the same `text.max_line_width_frac` out of
    templates/bars.yaml. Measuring the caption a second way here would be a
    second renderer, and the two would drift.
    """
    global _MEASURE
    if _MEASURE is not None:
        return _MEASURE[0]
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        sys.path.insert(0, ROOT)
        import render_bars as RB          # noqa: E402  (imports qc.fonts -> RAQM gate)
        style = RB.load_yaml(os.path.join(ROOT, "templates", "bars.yaml"))
        tcfg = style["text"]
        font = RB.truetype(os.path.join(ROOT, tcfg["font"]), int(tcfg["nominal_pt"]))
        max_w = float(tcfg["max_line_width_frac"]) * int(style["canvas"]["width"])

        def width(line):
            b = RB.bbox_ls(RB.norm_ar(line), font)
            return b[2] - b[0]

        _MEASURE = [(width, max_w)]
    except BaseException:
        # SystemExit included: qc.fonts exits the process when Pillow has no
        # RAQM. Authoring must survive that; we just lose the width check.
        _MEASURE = [None]
    return _MEASURE[0]


def fits(lines):
    """Do all of `lines` fit the template's per-line width cap at nominal_pt?

    -> True / False / None (measurement unavailable).

    Note the cap is not a hard clip in the renderer: `fit_pt` shrinks the whole
    clip's point size until the widest line fits. That is exactly why it is
    enforced here -- one greedy line drags every caption in the clip down.
    """
    m = _measurer()
    if m is None:
        return None
    width, max_w = m
    return all(width(ln) <= max_w for ln in lines)


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def _key(words, max_line_words):
    h = hashlib.sha1((" ".join(words) + "|%d" % max_line_words).encode("utf-8"))
    return h.hexdigest()


def cache_read(path=None):
    try:
        with open(path or CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def cache_write(d, path=None):
    path = path or CACHE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    except Exception:
        pass


_cache_read = cache_read
_cache_write = cache_write


# ---------------------------------------------------------------------------
# the model call
# ---------------------------------------------------------------------------

PROMPT = """\
You are breaking one line of Qur'anic Arabic (Uthmani mushaf text) into TWO
caption lines for a video overlay. The words, in order:

%(list)s

Answer with a SINGLE INTEGER: the number of words that go on line 1. The rest
go on line 2. It must be between 1 and %(hi)d, and neither line may exceed
%(maxw)d words. Both lines also have to fit a width cap, so avoid piling the
long words onto one line.

Break at a syntactic or semantic seam: between clauses, before a new predicate,
at a natural place to draw breath. Do NOT split a genitive construction
(idafa), do NOT separate a preposition from its object, a negative particle
from its verb, or a conjunction from what it introduces.

Output the integer and nothing else. No Arabic, no explanation, no punctuation.
"""


def judge(prompt):
    """Put one question to the model. -> (answer text, None) | (None, reason).

    THE one place in the authoring pipeline that shells out to a model, so
    that everything asking for a judgement uses the same binary, the same
    model, the same effort and the same failure semantics. Callers get a bare
    string back and must parse it strictly themselves -- see the note at the
    top of this module: what comes back is only ever an INDEX, never text we
    are going to display.
    """
    if not (os.path.isfile(CLAUDE) and os.access(CLAUDE, os.X_OK)):
        return None, R_MISSING
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
    return out, None


def _ask(words, max_line_words):
    """-> (k, reason) with k an int, or (None, reason)."""
    lo, hi = 1, len(words) - 1
    prompt = PROMPT % {
        "list": "\n".join("%d. %s" % (i + 1, w) for i, w in enumerate(words)),
        "hi": hi,
        "maxw": max_line_words,
    }
    out, reason = judge(prompt)
    if out is None:
        return None, reason
    # Strict: the whole answer must be one integer. Anything chattier is a
    # failed instruction-follow, and guessing at which number it meant is how
    # a caption ends up split in the wrong place.
    if not out.isdigit():
        return None, R_PARSE
    k = int(out)
    if not (lo <= k <= hi):
        return None, R_RANGE
    return k, R_LLM


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def split(words, heuristic, max_line_words, use_llm=True):
    """One card's display words -> ([line, ...], reason).

    `heuristic` is emit.split_lines -- called first, both to answer the
    single-line case and to have a validated fallback ready before the model is
    ever consulted. The model's index is accepted only if it is in range, both
    lines are non-empty, and both fit the renderer's width cap.
    """
    base = heuristic(words)
    if len(base) < 2 or len(words) < 3:
        return base, None            # single line, or nothing to decide
    if not use_llm:
        return base, R_MISSING

    base_k = len(base[0].split())
    key = _key(words, max_line_words)
    cache = _cache_read()
    hit = cache.get(key)
    if hit is not None:
        k, reason = hit, R_CACHE
    else:
        k, reason = _ask(words, max_line_words)

    if k is None:
        return base, reason

    # The per-line word cap is a style fact (the bars reference art never
    # carries more than ~4 short words per line), so it is enforced -- but only
    # when it is satisfiable at all, i.e. when the heuristic itself managed it.
    n = len(words)
    base_ok = max(base_k, n - base_k) <= max_line_words
    if base_ok and max(k, n - k) > max_line_words:
        return base, R_WORDS % max_line_words

    lines = [" ".join(words[:k]), " ".join(words[k:])]
    ok = fits(lines)
    if ok is None:
        return base, R_NOMEASURE
    if not ok:
        return base, R_WIDTH

    if hit is None:
        cache[key] = k
        _cache_write(cache)
    if k == base_k:
        # Same answer either way -- worth recording, since the two rules agree
        # less often than you would hope.
        return base, reason + " -- the width heuristic agreed"
    return lines, reason
