"""pipeline/crop.py -- solve a reel's 16:9 framing ONCE, at authoring time.

    tools/render-venv/bin/python pipeline/crop.py sources/<id>/<reel>.yaml
        [--frames 4] [--annotate out.png] [--write] [--force]
        [--dry-run] [--measurements FILE]

Samples frames out of the reel's own window, asks a vision model where the
reciter is, computes the crop window with plain arithmetic, prints the solve,
and with --write edits `crop:` and `x_offset:` into the config.

`bars` and `hz` only. `style: default` re-crops at RENDER time from YuNet
(render_default.py `detect_subject`/`vertical_crop_filter`) and is left alone.

    WHY THIS IS AUTHORING AND NEVER RENDERING. CLAUDE.md invariant 4: nothing
    outside committed files may affect a rendered pixel. A model call inside a
    renderer would make two renders of one config differ, and the difference
    would be invisible until someone diffed the frames. So the model is asked
    once, here, the answer becomes four integers in a tracked YAML with a
    provenance comment naming the model and the date, and generate.py and the
    renderers stay pure and offline. The shape -- shell out to a model, force a
    strictly-parsed numeric answer, cache to disk, degrade to something
    deterministic, record which path was taken -- is
    legacy/qc/author/linebreak.py's, and is followed here for the same reason:
    the model's judgement is worth having and its output is never trusted.

The model NEVER sees or returns a crop. It answers where the head, the crown
and the shoulders are, as fractions of the frame, and the window is arithmetic
from there. So a bad answer is a visibly wrong box on --annotate, not a
plausible-looking crop nobody can check.

--- the horizontal rule: equal gaps -------------------------------------------

Ported from legacy/qc/author/crop.py:352 `targets()`. The frame width is
divided between the caption column and the reciter's block, and the three gaps
-- outside the caption, between the two, outside the reciter -- are equal. Two
gaps when the reciter runs off his own outer edge, which a wide shot always
makes him.

It reproduces the shipped reels only under one reading of its two terms, so the
reading is written down here:

  "caption block" is the FIXED-WIDTH caption column, not the ink of the current
  phrase. Ink edges swing +/-0.09 W between phrases; the column centre does not
  -- measured 0.302 +/- 0.006 across every frame of four reels.

  "reciter block" is his HEAD BOUNDING BOX INCLUDING HEADWEAR, edge to edge --
  not his head centre (with the centre the three gaps come out ~3x apart), and
  NOT his body, which is what legacy's max(body, head) used.

  The head reading is what the rule's own two published numbers pin. Solving
  the three-gap branch for the block that lands the caption at `cap` gives
  block = 1 - 3*cap + cap_w/2, and the hz reference reel's pair -- head centre
  0.780 of the window, caption column at 0.302 -- is the SAME equation twice:
  both reduce to gap + block/2 = 0.2225, i.e. block = 0.346. The two-gap branch
  cannot produce that pair at all (it forces 2*gap + block = 1 - cap_w). Badr
  al-Turki's head spans 0.336 of that window and his robe 0.461, so the pair
  names the head.

  Measured over the five known-good windows, caption anchor error against the
  shipped value, |predicted - shipped|:

                   head block   max(body, head)
      YkXjYyKwHJ4     0.0021        0.0359
      vqxYwdR4RvQ     0.0537        0.0093
      9Yci0oWB2fE     0.0139        0.0417
      1CQYaDe-nLE     0.0037        0.1759
      -GZR1C9Acd4     0.0163        0.0606

  The body block's 1CQYaDe-nLE row is the point: a 0.124 W anchor puts the
  pills hard against the left edge of frame, which is the -0.105 failure of
  sources/9Yci0oWB2fE/meta.yaml one step short of going negative. The one row
  it wins, vqxYwdR4RvQ, is a reciter in full profile, where the head is only
  0.116 W and the rule honestly asks for a caption further out than the 0.300
  that source's cached solve was hand-pinned to.

  Checked the other way round -- each shipped reel's head width measured in its
  FINAL frame, run through the rule, against the anchor that reel actually
  ships:

      reel                        head w   rule says   shipped   delta
      BADR-AL-TURKI (hz)           0.341     0.304      0.310    0.006
      ABDULLAH-AL-JUHANY (bars)    0.335     0.297      0.301    0.004
      AHMAD-AL-HUDHAIFI (bars)     0.314     0.304      0.299    0.005
      AHMED-BIN-TALIB (bars)       0.148     0.359      0.302    0.057

  Three of four inside 0.006. AHMED-BIN-TALIB is vqxYwdR4RvQ, and its 0.057 is
  a KNOWN HAND-PIN THE SOLVER DISAGREES WITH ON PURPOSE, not a bug to tune out:
  it is a wide profile shot with a 0.148 W head, the rule honestly asks for a
  caption further out than the 0.300 that clip was pinned to, and moving the
  rule to reproduce 0.300 would move the three reels that currently land on it.
  vqxYwdR4RvQ is a regression target for its WINDOW and not for its anchor.

--- the body: a containment constraint, never gap arithmetic ------------------

The body is measured for ONE job: to veto a window that cuts his silhouette.

This is the bug that produced this paragraph. A live solve of vqxYwdR4RvQ on
2026-08-01 (head cx 0.545, w 0.120 W; body 0.460..0.675 W; 4/4 frames, model
confidence 0.98 -- the perception was right) chose {x:180, y:106, w:666, h:374}
and REPORTED `outer 0.106`, a healthy margin. Its right edge was at x=845 and
his robe reaches x=864: it measured the margin from his head (0.231 of the
window) while his body spans 0.413 of it, and cut through his back while
printing a number that said it had not. Gaps from the head is right; taking
"there is air beyond his head" to mean "there is air beyond HIM" is not.

So the two roles are separated. The head sets the three gaps. The body sets a
band of legal window lefts, and inside that band the gap rule still picks. The
band is:

  CONTAIN     the window must hold body_left..body_right.
  CAPTION     ... unless holding him would push the caption column onto his
              head, in which case containment yields and he bleeds off his
              outer edge, which is what the two-gap branch of the rule already
              says happens on a wide shot. This is not hypothetical: the
              shipped ABDULLAH-AL-JUHANY reel (1CQYaDe-nLE, window 666 px wide
              because a full-width burned-in name plate forces the zoom) runs
              his gold bisht 72 px off the right edge, and the window that
              would contain it sits 72 px further right, where the pills land
              on his ghutrah. Frame-checked: his head clears the caption column
              by 0.057 of the window as shipped and by -0.052 contained.

  The order matters and is measurable. Containment first would move
  1CQYaDe-nLE's window 72 px (5.6% of frame) off its shipped value and put text
  on his face; caption-clearance first leaves all four shipped windows where
  they are and still moves vqxYwdR4RvQ off his back.

  The band is per zoom level and does NOT choose between zoom levels -- see
  solve() for the two versions that did and what each of them broke. Bleeding
  off the outer edge is a shipped look: BADR-AL-TURKI and ABDULLAH-AL-JUHANY
  both do it. What was never shipped is doing it while printing an `outer` gap
  that says there is air there, so an overhang is now always printed.

--- clamping the body reading ------------------------------------------------

The body is not trusted raw. BODY_FROM_FACE and HEAD_FROM_FACE are legacy/qc/
author/crop.py:89-90, where they clamp a motion-blob silhouette for exactly
this reason, and they are carried over with their arithmetic: a face is
head_w / HEAD_FROM_FACE wide, and the body may reach BODY_FROM_FACE face widths
out from the CENTRE OF HIS HEAD on either side -- 1.3 head widths a side.

Half-extents actually measured, in head widths, over the five known sources
(the wide ones re-checked against the pixels, not just against the model):

      YkXjYyKwHJ4  0.593 / 0.779      1CQYaDe-nLE  0.847 / 1.163
      vqxYwdR4RvQ  0.708 / 1.083      -GZR1C9Acd4  0.689 / 0.821
      9Yci0oWB2fE  0.637 / 0.637

None is clamped; 1.163 is 1CQYaDe-nLE's gold shoulder, verified at x~890 of
1280 by eye. What IS clamped is the failure class: 9Yci0oWB2fE's swaying
congregation once measured `reciter_w_frac` 1.201 against a 0.198 W head -- 3.0
head widths a side -- solved the caption anchor to -0.105, off the left edge of
frame, and REPORTED SUCCESS (sources/9Yci0oWB2fE/meta.yaml carries the
write-up). A body wider than 2.6 face widths from the centre of his head is a
detection artefact, not a reciter.

Legacy clamps at 2.4 face widths a side, not 2.6. The looser number is kept
because legacy measures from the FACE centre and this file measures from the
HEAD centre, which on a profile shot is not the same point -- vqxYwdR4RvQ's
ghutrah puts the two ~0.15 head widths apart.

--- the vertical rule: face at 0.275 of the window ---------------------------

FACE_Y_FRAC and FACE_Y_BAND are the numbers legacy/qc/author/crop.py carries,
re-measured from pixels rather than inherited: all 16 frames of four shipped
reels put the face centre at 0.275 of the footage height, spanning 0.247-0.33.
Headroom above the crown over the same frames is 0.00-0.08, mean 0.035.

So the face target is primary and the crown is the guard: put the face at
0.275, then refuse to clip the crown and refuse more air above it than any
shipped reel has. Ordering it the other way -- crown plus a headroom budget,
then clamp the face -- costs precision where it is checkable: with headroom
capped at the 0.05 budget, vqxYwdR4RvQ's window lands 18 px (4% of its height)
below the shipped one; face-first lands within 4 px, and 9Yci0oWB2fE within 5.

    KNOWN LIMIT, inherited whole, and it cost a day. What the eye reads is
    HEADROOM and the face is a proxy for it. The proxy holds for an upright
    reciter and breaks for a bowed one: Salih al-Ansari (-GZR1C9Acd4) recites
    deeply bowed, so his face sits low inside a tall head and pinning it at
    0.275 pushes his crown off the top of the band. Centring the face at 0.50
    was tried on 2026-07-30 and reverted the next day -- it rescued him by
    accident and looked wrong on every upright reciter in reels/. Here the two
    constraints are simply allowed to be inconsistent: when no y satisfies both
    the band and the crown, the CROWN WINS -- y falls back to whatever the
    crown/headroom constraint alone allows, and FACE_Y_BAND is the one that
    gives way.

    UPDATE 2026-08-01: that fallback used to also refuse to write. It no
    longer does -- FACE_Y_BAND is the shipped reels' own scatter, not a
    tolerance, and report() prints the miss rather than raising it as an
    error; see FACE_Y_BAND's own comment for the measurement that this
    dropped a hand-solve for a difference smaller than the model's own
    run-to-run noise. The crown/headroom constraint itself is untouched by
    this -- it was never the thing doing the refusing, only the thing the
    band's fallback falls back to.

This is also why the model is asked for `face_cy` separately from the head box.
A head box including a ghutrah does not centre on the face: on 9Yci0oWB2fE the
old detector's head+ghutrah box centred 57 px below the face on a 390 px head
(see the hand-solve note in sources/9Yci0oWB2fE/meta.yaml), which is 0.09 of
that window's height -- enough on its own to walk the 0.24-0.34 band.

--- and why crown_y and face_cy are then RECONCILED against the head box ------

The two of them come back as free-floating scalars and the arithmetic above
reads the DIFFERENCE between them. Nothing makes the model keep that difference
consistent with the head it just boxed, and at 32b/fp8 it does not.

Three --force solves of vqxYwdR4RvQ on 2026-08-01, each already pooling 3
passes over the same four frames (PASSES was 3 at the time), landed the face
at 0.265, 0.275 and 0.231 of the window: the SAME source wrote twice and
refused once, back when landing outside FACE_Y_BAND still refused. All three had
headroom pinned at ~0.08, so it was MAX_HEADROOM binding every time -- `want`
falls below `crown - MAX_HEADROOM*h` exactly when face_cy - crown_y < 0.195*h,
and the model put that distance at 0.375 head heights (0.090 H against a
0.240 H head) where every hand-measured upright source in the fixtures shows
0.500-0.560.

Widening MAX_HEADROOM is not the fix: 0.08 is the most air any shipped reel has
above the crown, and loosening it walks the crown toward the top edge. The
INPUT is what is wrong, so it is repaired before vertical() sees it, in
_reconcile(): crown becomes the head box's own top, and face_cy is clamped to
FACE_IN_HEAD head heights below that.

Not because the box holds still -- it does not. Over 15 passes of vqxYwdR4RvQ
head_h came back anywhere from 0.160 to 0.280 H and head_cy from 0.280 to
0.350. It is because tying the two scalars TO the box makes the crown
constraint self-satisfying, and then the face fraction stops depending on the
box at all: with face = crown + r*head_h and r at the FACE_IN_HEAD floor,
`want` lands inside [lo, hi] and y = face - FACE_Y_FRAC*h, so fy comes out at
0.275 whatever the box does. The window's y still follows the box, which is
right -- it is where he is. What no longer follows it is the write/refuse
decision.

The separately-reported crown_y is dropped for the box top because it is the
reading that disagrees with itself: over those same passes it sat 0.000-0.030 H
BELOW its own box top on vqxYwdR4RvQ and 0.000-0.040 H ABOVE it on
YkXjYyKwHJ4, and on YkXjYyKwHJ4 that one 0.040 alone swings the crown-to-face
distance from 0.400 to 0.750 head heights -- i.e. it reads an upright reciter
as more bowed than the hand-measured bowed one.

face_cy stays a separate reported field, and inside the band the model's own
answer is used untouched -- which is exactly what the ghutrah case above needs.

--- the guards ---------------------------------------------------------------

Each one closes a failure that has already happened here:

  body reach clamped to BODY_FROM_FACE face widths either side of the head
  centre, and to the frame. See the section above for the 1.201 incident it
  closes.

  caption centre must land in (0, 1), or nothing is written.

  the window may not cut his silhouette while a containing window exists that
  still clears the caption. When none does, the overhang is PRINTED as a
  fraction of the window rather than swallowed -- see the vqxYwdR4RvQ solve
  that reported `outer 0.106` over a cut back.

  obstructions are avoided, not fatal. If no window clears them the solver says
  which box blocked and falls back to the best obstruction-ignoring window with
  a warning, because returning nothing is worse: the same file records two of
  six "burned-in" boxes on that source being a microphone cluster and a pillar
  edge, and the solver refusing every window because of them. The prompt tells
  the model at length that furniture is not a graphic, and a box seen in fewer
  than half the frames is dropped as a hallucination.

  --annotate draws the head box, the clamped body extent, the window, the
  caption column and the three gaps, labelled, over a real frame. A bad solve
  is caught by eye in one look; the numbers alone never catch it -- the cut
  back above passed every printed number it had.
"""
import argparse
import datetime
import json
import os
import re
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import generate  # noqa: E402  (config/path helpers only -- no Pillow)


# --- machine config (.env) -------------------------------------------------
# Copied, not imported. Every pipeline script carries its own reader so it
# stays runnable on its own; see CLAUDE.md's codebase map.

def _dotenv():
    """KEY=value pairs from ROOT/.env; process environment wins on conflict."""
    path = os.path.join(ROOT, ".env")
    out = {}
    if os.path.exists(path):
        for raw in open(path, encoding="utf-8"):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            k, sep, v = line.partition("=")
            if sep:
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                elif " #" in v:
                    v = v.split(" #", 1)[0].rstrip()
                out[k.strip()] = v
    return out


_ENV = None


def envvar(name, default=None):
    global _ENV
    if os.environ.get(name):
        return os.environ[name]
    if _ENV is None:
        _ENV = _dotenv()
    return _ENV.get(name) or default


# --- the owner's numbers ---------------------------------------------------

CAPTION_W = {"bars": 0.45,     # = render_bars.TEXT["max_line_width_frac"]
             "hz": 0.504}      # = 2 x the old CAPTION_HALF_W 0.252
CANVAS_W = {"bars": 1080,      # render_bars.BAND_W; x_offset is px on this
            "hz": 1920}
MIN_GAP = 0.02                 # a gap thinner than this is not a gap
FACE_Y_FRAC = 0.275            # face centre in the window; measured, see above
FACE_Y_BAND = (0.24, 0.34)     # the shipped reels' own observed spread -- a
# 0.10-wide range, not a correctness bound. It used to gate a hard refusal;
# demoted 2026-08-01 because the reference reels it is measured from were
# never consistent with each other to begin with, and pooling PASSES queries
# to stabilise a threshold drawn through the middle of their own scatter was
# chasing noise smaller than the scatter: three --force solves of
# vqxYwdR4RvQ on the SAME four frames landed the face at 0.243 (inside), 0.275
# (inside) and 0.231 (outside) of the window -- a 0.012 run-to-run wobble,
# 12% of the band's own width, flipping a write into a refusal for the same
# source. report() still prints when a solve falls outside the band -- it is
# worth a look -- but the config is written either way; --annotate is the
# check now, not the number.
MAX_HEADROOM = 0.08            # most air above the crown any shipped reel has
FACE_IN_HEAD = (0.50, 0.85)    # how far the face centre sits below the top of
# the head box, in head heights -- the band _reconcile() clamps face_cy into.
# Hand-measured on the five fixture sources: 0.500 (9Yci0oWB2fE), 0.528
# (1CQYaDe-nLE), 0.529 (YkXjYyKwHJ4), 0.560 (vqxYwdR4RvQ) upright, median
# 0.528; 0.759 on the bowed al-Ansari (-GZR1C9Acd4). Live, over 15 passes of
# vqxYwdR4RvQ, the model answers 0.350-0.687 per pass and 0.35-0.46 after the
# median -- it puts the face most of a head too high, and that is the whole
# instability; see "reconciled against the head box" above.
# 0.50 is the LOW edge because it is the tightest reading the pixels actually
# show, so it is the smallest clamp that keeps `want` off the MAX_HEADROOM
# ceiling: at 0.50 the crown-to-face distance is 0.50 * head_h, and vqxYwdR4RvQ
# needs 0.195 * h against a head that is 0.37-0.47 of the window tall.
# 0.85 is a below-the-chin guard and nothing more. It has to stay CLEAR of
# 0.759, because that reading is precisely what makes -GZR1C9Acd4 fall outside
# FACE_Y_BAND; a high edge tight enough to look symmetric (0.56, say) would
# clamp the bowed reciter back into the band and hide his broken framing
# instead of printing it.
MIN_CONF = 0.4                 # a frame the model is unsure of is not a vote
BODY_FROM_FACE = 2.6           # legacy/qc/author/crop.py:89 -- how far his
HEAD_FROM_FACE = 2.0           # legacy/qc/author/crop.py:90 -- silhouette may
# reach from the centre of his head, in FACE widths, and the head-width-to-
# face-width converter that makes the first number usable here (this file has
# no face box, only a head box). See "clamping the body reading" above: the
# five known sources measure 0.593-1.163 head widths a side against this
# 1.3-head-width bar, and 9Yci0oWB2fE's congregation measured 3.0.

# --- the model -------------------------------------------------------------
#
# Shelled out to the Claude Code CLI (`claude -p`), not an HTTP API: the
# question reaches the model over the user's own local `claude` auth, so this
# script needs no API key of its own (contrast the OpenRouter/Qwen transport
# it replaces, which needed OPENROUTER_API_KEY). Only the transport changed --
# the prompt, the schema and every guard below are untouched.

MODEL = "sonnet"                       # the alias passed to `claude --model`

# Frame width sent to the model. Anthropic tokenises an image at roughly
# (w_px * h_px) / 750 tokens, so a 1280x720 frame costs ~1.2k tokens and a 4K
# frame would cost ~10x that for no more precision than the source's own
# detail already gives up at this size.
FRAME_W = 1280
EST_TOKENS_PER_FRAME = 1200
CTX = 1000000                          # tokens, Claude Sonnet's context window
GRID = 1000.0                          # the model's own coordinate space --
# unchanged from the Qwen-VL transport this replaces because the PROMPT text
# asking for it is unchanged (see to_fractions() for why that prompt asks for
# a 0-1000 grid rather than fractions or pixels).

TIMEOUT = 180.0                        # a `claude -p` call spends a couple of
# turns reading N images off disk before it answers -- measured 6-8s for one
# frame and ~7.5s for four on this machine -- so 180s leaves headroom for a
# slower box without hanging forever on a wedged CLI.
RETRIES = 3
# Bounded retries for a transient CLI hiccup (non-zero exit, a timeout) or a
# reply that fails the defensive parse in parse() -- never a silent default.
# See ask().

PASSES = 1
# Was 3 -- independent `claude -p` calls over the SAME frames pooled into one
# median, carried from the OpenRouter/Qwen transport this replaces. The whole
# reason to pool was to stabilise the FACE_Y_BAND refusal against run-to-run
# noise: the same four vqxYwdR4RvQ frames landed the face at 0.243, writable,
# on one run and 0.231, refused, on the next. That was a 0.012 wobble
# flipping a decision inside a 0.10-wide band that was only ever the shipped
# reels' own scatter, not a tolerance -- see FACE_Y_BAND's comment. Now that
# the band no longer refuses, there is nothing left for pooling to buy: one
# pass costs one subprocess call instead of three, ~8s instead of ~25s for a
# 4-frame solve. median()/_spread() already handle a single pass correctly
# (a one-element spread is 0.0 by construction, not a division), so nothing
# downstream needed to change to drop this.

PROMPT = """You are framing a Qur'an recitation video for a vertical social \
media reel. I am showing you %d frames sampled from one continuous shot of the \
same recitation, in time order. Report, PER FRAME and in that same order, where \
the reciter is.

Each frame is %d by %d pixels, but give every coordinate NORMALISED TO A \
0-1000 GRID over the frame, as a whole number: x is 0 at the left edge and \
1000 at the right edge, y is 0 at the top edge and 1000 at the bottom edge, so \
a width of 1000 is the whole frame width and a height of 500 is half the frame \
height.

The reciter is the man leading the recitation: nearest the camera, largest in \
frame, usually the only one speaking. Worshippers praying or seated behind him \
are NOT the reciter and must never widen any of his boxes.

For each frame report:

  reciter_present  false if he is out of frame, cut away from, or completely \
occluded. The other fields are then ignored, so any value will do.
  face_visible     true ONLY if you can actually SEE facial features in this \
frame -- an eye, the brow, the nose, the mouth, the line of the cheek or jaw. \
It is false when his head is turned away from the camera, when it is bowed far \
enough that the cloth hides everything, when a ghutrah or shemagh is drawn \
across his face, when a mic or its windscreen covers it, and whenever the shot \
is the back or the top of his head. Answer for THIS frame and answer honestly: \
"false" costs nothing here, it only routes the shot to a human, whereas a \
"true" you cannot support makes every other number below a guess about a face \
that is not there. If you are unsure, answer false. Still fill in head, \
crown_y and the rest from the head's outline as best you can -- they are used \
to show a person the shot, not to crop it.
  head             his head's bounding box INCLUDING all headwear -- keffiyeh, \
ghutrah, shemagh, turban, imamah, taqiyah, hair. The box a hat-wearing head \
fills: from the top of the cloth to the bottom of the chin or beard, and only \
as wide as the head and its cloth. It must NOT reach down to his shoulders or \
out over his robe, and it must NOT include a microphone, a windscreen or a \
boom arm, even when one stands directly in front of his face. The head ends \
where the cloth ends. cx and cy are its CENTRE.
  crown_y          the y of the very topmost pixel of that head, cloth \
included -- the same value as head cy minus half of head h.
  face_cy          the vertical centre of the FACE itself, halfway between the \
eyebrows and the chin. On a reciter who bows his head this sits well below the \
centre of the head box. Report both honestly; the difference matters.
  body_left        the leftmost x of HIS shoulders and torso, robe included.
  body_right       the rightmost x of the same. Not his outstretched hands, not \
a person beside him, not a lectern or a rail.
  facing           WHICH WAY HIS NOSE POINTS ON THE SCREEN, not his own left \
and right: "left" if his face points toward the left edge of the picture \
(toward x=0), "right" if it points toward the right edge (toward x=1), \
"frontal" if he is square to the camera and you can see both of his cheeks. A \
man photographed from his own right side is facing "left" here.
  posture          "upright", "bowed" (rukuu', or reciting with the head \
lowered over a mushaf), or "prostrate" (sujuud).
  headroom_frac    how much empty space should sit above his crown in the final \
crop -- the ONLY fraction here, given as a fraction of the crop's height. \
Reference reels run 0.00 to 0.08.
  obstructions     GRAPHICS BURNED INTO THE VIDEO, and nothing else: channel \
logos, station watermarks, social-media handles, lower-thirds and name plates, \
hard subtitles, tickers, timestamps, URLs. Things that are part of the picture \
file rather than part of the room. They are usually crisp, flat, brightly \
outlined, and identical in every frame. Check all four corners and the whole \
bottom strip before answering: a broadcast of a mosque normally carries two to \
four of them, a logo in one top corner and a name plate or a row of social \
handles along the bottom. Box each one.
                   A microphone, a mic boom or stand, a pillar, a lamp, a \
chandelier, a mushaf stand, a curtain, a rail, a doorway, a dark window, a \
piece of carved wall or a person is NOT an obstruction, however much of the \
frame it crosses, and however rectangular it looks. If it is part of the room \
it is not a graphic. Report an empty list when there are no graphics. An \
earlier version of this tool read the furniture as graphics and could then find \
no legal crop at all, so an invented box is far worse than a missed one.
  confidence       0 to 1, how sure you are of THIS frame's boxes.

Answer with the JSON object the schema describes, and nothing else."""

_BOX = {"type": "object", "additionalProperties": False,
        "properties": {k: {"type": "number"} for k in ("x", "y", "w", "h")},
        "required": ["x", "y", "w", "h"]}

# strict: true demands additionalProperties false and every property listed in
# required, at every level -- an optional key is a schema error, not an
# omission.
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"frames": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "reciter_present": {"type": "boolean"},
            "face_visible": {"type": "boolean"},
            "head": {"type": "object", "additionalProperties": False,
                     "properties": {k: {"type": "number"}
                                    for k in ("cx", "cy", "w", "h")},
                     "required": ["cx", "cy", "w", "h"]},
            "crown_y": {"type": "number"},
            "face_cy": {"type": "number"},
            "body_left": {"type": "number"},
            "body_right": {"type": "number"},
            "facing": {"type": "string", "enum": ["left", "right", "frontal"]},
            "posture": {"type": "string",
                        "enum": ["upright", "bowed", "prostrate"]},
            "headroom_frac": {"type": "number"},
            "obstructions": {"type": "array", "items": _BOX},
            "confidence": {"type": "number"},
        },
        "required": ["reciter_present", "face_visible", "head", "crown_y",
                     "face_cy", "body_left", "body_right", "facing", "posture",
                     "headroom_frac", "obstructions", "confidence"],
    }}},
    "required": ["frames"],
}


# --- sampling --------------------------------------------------------------

def sample_times(t0, t1, n):
    """n timestamps spread across [t0, t1], inset from both ends.

    Inset because the first and last seconds of a reel's window are where a
    fade, a bow or a camera change lives, and a frame taken there answers about
    a moment the reel barely shows."""
    lo, hi = t0 + 0.06 * (t1 - t0), t0 + 0.94 * (t1 - t0)
    if n == 1:
        return [(lo + hi) / 2.0]
    return [lo + (hi - lo) * i / (n - 1.0) for i in range(n)]


def extract(src, times, tmp, W, H):
    """One JPEG per timestamp, scaled to FRAME_W wide. -> ([paths], (w, h))"""
    ff = envvar("QC_FFMPEG", "ffmpeg")
    os.makedirs(tmp, exist_ok=True)
    fw = min(FRAME_W, W)
    fh = int(round(H * fw / float(W) / 2.0)) * 2
    out = []
    for i, t in enumerate(times):
        p = os.path.join(tmp, "crop-%02d.jpg" % i)
        subprocess.run(
            [ff, "-y", "-v", "error", "-ss", "%.3f" % t, "-i", src,
             "-frames:v", "1", "-vf",
             "scale=%d:%d:flags=lanczos" % (fw, fh), "-q:v", "3", p],
            check=True)
        out.append(p)
    return out, (fw, fh)


def to_fractions(parsed):
    """The model's 0-1000 grid -> fractions of the frame, in place.

    The grid is not decoration, it is the model's own coordinate space: the
    Qwen-VL series is trained on grounding normalised to 0-1000, and it answers
    in that space whatever units the prompt asks for. Measured on
    vqxYwdR4RvQ frame 0 against a hand-read head at cx 0.567 W, crown 0.139 H,
    body 0.465..0.684 W:

        asked for 0..1 fractions   head cx 0.610   body 0.510..0.730
        asked for frame pixels     head cx 0.469   body 0.391..0.547
        asked for the 0-1000 grid  head cx 0.545   body 0.460..0.675

    The pixel form is the worst of the three because the model normalises to
    1000 anyway and the divisor is then wrong by 1280/1000. Asking in the
    grid's own units and dividing here is the only form that is not a guess
    about what the model meant."""
    for f in parsed.get("frames") or []:
        if not isinstance(f, dict):
            continue
        for k in ("body_left", "body_right", "crown_y", "face_cy"):
            if isinstance(f.get(k), (int, float)):
                f[k] = f[k] / GRID
        h = f.get("head")
        if isinstance(h, dict):
            for k in ("cx", "cy", "w", "h"):
                if isinstance(h.get(k), (int, float)):
                    h[k] = h[k] / GRID
        for b in f.get("obstructions") or []:
            if isinstance(b, dict):
                for k in ("x", "y", "w", "h"):
                    if isinstance(b.get(k), (int, float)):
                        b[k] = b[k] / GRID
    return parsed


# --- the model call --------------------------------------------------------

def ask(paths, dims, cwd):
    """One `claude -p` call, N frames. -> (parsed dict, envelope dict).

    Frames are referenced by absolute path in the prompt text rather than
    base64-encoded inline: `claude -p` reads an image file off disk when the
    prompt names its path (verified live against this repo's own `claude`
    install -- there is no image_url payload the way OpenRouter's API took
    one), so extract()'s files are named here instead of read and encoded."""
    listing = "\n".join("  frame %d: %s" % (i, p) for i, p in enumerate(paths))
    prompt = (PROMPT % (len(paths), dims[0], dims[1])
              + "\n\nThe frames, in the same time order, are these image "
                "files on disk -- read each one before answering:\n" + listing)
    cli = envvar("QC_CLAUDE", "claude")
    cmd = [cli, "-p", prompt, "--model", MODEL, "--output-format", "json",
           "--allowedTools", "Read", "--strict-mcp-config",
           "--json-schema", json.dumps(SCHEMA)]
    # --allowedTools Read: this call only ever needs to look at the sampled
    # frames, never to edit a file or run a command.
    # --strict-mcp-config with no --mcp-config: zero MCP servers, so whatever
    # MCP setup the user's own `claude` has cannot make this call slower or
    # less deterministic -- crop.py's answer must depend only on the frames.
    # --json-schema SCHEMA: `claude -p` DOES have a structured-output flag
    # (verified against `claude --help`, not assumed) and passing it cut a
    # live gt9y-QGgMsA solve's malformed-reply rate to zero -- without it the
    # model fenced its JSON in ```json, tacked a trailing sentence on after
    # the closing fence, or drew an obstruction box as x0/y0/x1/y1 with a
    # "label" key, none of which OpenRouter's `strict: true` ever let through.
    # It is a reliability improvement, not a replacement for parse()'s
    # defensive parsing below -- nothing guarantees a future CLI version (or
    # a differently-behaving model) keeps honouring it as strictly.
    last_err = None
    for attempt in range(RETRIES):
        if attempt:
            time.sleep(2.0)
        try:
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True,
                                   text=True, timeout=TIMEOUT)
        except FileNotFoundError:
            # Not a transient failure -- retrying won't install the CLI.
            raise SystemExit(
                "%r is not on PATH. Install the Claude Code CLI and sign in "
                "(`claude auth login`), or point QC_CLAUDE at its binary in "
                "%s." % (cli, os.path.join(ROOT, ".env")))
        except subprocess.TimeoutExpired:
            last_err = "claude -p timed out after %.0fs" % TIMEOUT
        else:
            if proc.returncode != 0:
                last_err = ("claude -p exited %d: %s" % (
                    proc.returncode, (proc.stderr or proc.stdout)[:400]))
            else:
                try:
                    envelope = json.loads(proc.stdout)
                except ValueError as exc:
                    last_err = ("claude -p did not print its JSON envelope "
                                "(%s): %s" % (exc, proc.stdout[:400]))
                else:
                    if envelope.get("is_error"):
                        last_err = ("claude -p reported an error: %s"
                                    % json.dumps(envelope)[:400])
                    else:
                        try:
                            parsed = parse(envelope.get("result"))
                        except ValueError as exc:
                            last_err = str(exc)
                        else:
                            return parsed, envelope
        print("      %s (attempt %d/%d)%s"
              % (last_err, attempt + 1, RETRIES,
                 "" if attempt == RETRIES - 1 else ", retrying"),
              file=sys.stderr)
    raise SystemExit("claude -p failed after %d attempts: %s"
                     % (RETRIES, last_err))


def parse(text):
    """Strict: the answer is JSON matching SCHEMA, or it is nothing.

    There is no `response_format: json_schema, strict: true` enforcement over
    a CLI call the way OpenRouter's API gave the old transport -- `claude -p
    --output-format json` guarantees only the ENVELOPE is JSON, not whatever
    the model puts in its `result` string -- so the reply is parsed
    defensively by hand: strip a markdown fence (a model told to answer with
    "the JSON object and nothing else" still wraps it in ```json and/or
    tacks on a stray closing sentence often enough that failing on either
    would be failing on a formality), locate the JSON object by its own
    braces rather than trusting the fence to be clean, then walk SCHEMA over
    the parsed object with _validate() -- the same schema OpenRouter used to
    hand the provider for server-side enforcement. Raises ValueError, not
    SystemExit, so ask() can retry a malformed reply a bounded number of
    times before giving up loudly -- never substituting a default for a
    field that failed to come back."""
    s = (text or "").strip()
    fence = re.search(r"```(?:[a-zA-Z]*)\s*\n(.*?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    start = s.find("{")
    if start == -1:
        raise ValueError("model output has no JSON object at all: %s"
                         % s[:400])
    s = _first_json_object(s[start:])
    try:
        out = json.loads(s)
    except ValueError as exc:
        raise ValueError("model output did not parse as JSON (%s): %s"
                         % (exc, s[:400]))
    problems = _validate(SCHEMA, out, "response")
    if problems:
        raise ValueError("model output failed schema validation: %s (%s)"
                         % ("; ".join(problems[:6]), s[:200]))
    return out


def _first_json_object(s):
    """The first balanced {...} in s. -> the substring.

    Cuts off anything the model appended after the object -- the trailing
    "All four frames show the same static shot..." sentence this exists for
    was observed live on gt9y-QGgMsA, past the closing ``` of an otherwise
    well-formed reply."""
    depth = 0
    for i, ch in enumerate(s):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[:i + 1]
    raise ValueError("no balanced JSON object found: %s" % s[:400])


def _validate(schema, value, path):
    """SCHEMA, walked by hand over a parsed reply. -> [problem, ...].

    Every field SCHEMA lists as required must be present, and every value
    must be of the type (or enum) SCHEMA declares, at every level -- this is
    the "required field is present and numeric" half of what `strict: true`
    checked server-side over the OpenRouter transport. NOT the other half: an
    extra property (Claude volunteers a "frame" index or an obstruction
    "label" the OpenRouter transport's strict schema would have refused to
    emit at all) is left alone rather than failed, because every consumer
    downstream (usable(), median(), _boxes()) already reads fields by name
    and ignores anything else -- rejecting a harmless addition would fail a
    reply that answered the actual question correctly."""
    t = schema.get("type")
    problems = []
    if t == "object":
        if not isinstance(value, dict):
            return ["%s: expected an object, got %r" % (path, value)]
        for req in schema.get("required", []):
            if req not in value:
                problems.append("%s.%s: missing" % (path, req))
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                problems += _validate(props[k], v, "%s.%s" % (path, k))
    elif t == "array":
        if not isinstance(value, list):
            return ["%s: expected an array, got %r" % (path, value)]
        items = schema.get("items")
        if items:
            for i, item in enumerate(value):
                problems += _validate(items, item, "%s[%d]" % (path, i))
    elif t == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            problems.append("%s: expected a number, got %r" % (path, value))
    elif t == "boolean":
        if not isinstance(value, bool):
            problems.append("%s: expected a boolean, got %r" % (path, value))
    elif t == "string":
        if not isinstance(value, str):
            problems.append("%s: expected a string, got %r" % (path, value))
        elif "enum" in schema and value not in schema["enum"]:
            problems.append("%s: %r not one of %s"
                            % (path, value, schema["enum"]))
    return problems


# --- medianing -------------------------------------------------------------

_NUM = ("crown_y", "face_cy", "body_left", "body_right", "headroom_frac")


def usable(f):
    """Is this frame a vote? A dropped frame is louder than a wrong one."""
    if not isinstance(f, dict) or not f.get("reciter_present"):
        return False
    if float(f.get("confidence") or 0.0) < MIN_CONF:
        return False
    h = f.get("head") or {}
    if not all(isinstance(h.get(k), (int, float))
               for k in ("cx", "cy", "w", "h")):
        return False
    return all(isinstance(f.get(k), (int, float)) for k in _NUM)


def median(frames):
    """Median every number across the usable frames. -> measurements dict.

    Median-of-N is the whole defence against an imprecise box: a VLM's head cx
    is good to a few percent and 3% of frame width is visible, but the errors
    are not correlated between frames the way a systematic bias would be."""
    keep = [f for f in frames if usable(f)]
    if not keep:
        raise SystemExit(
            "no usable frame: the model saw no reciter (or was under %.2f "
            "confident) in all %d. Check the sampled frames, or the trim window."
            % (MIN_CONF, len(frames)))
    med = {k: statistics.median([float(f[k]) for f in keep]) for k in _NUM}
    for k in ("cx", "cy", "w", "h"):
        med["head_" + k] = statistics.median([float(f["head"][k]) for f in keep])
    med["facing"] = _vote([f.get("facing") for f in keep], "frontal")
    med["posture"] = _vote([f.get("posture") for f in keep], "upright")
    # Majority, not any(): one pass claiming a face in a shot that has none is
    # exactly the hallucination this field exists to outvote. A frame that
    # omitted the key counts as "no face" -- absent evidence is not evidence.
    med["face_seen"] = sum(1 for f in keep if f.get("face_visible") is True)
    med["face_visible"] = med["face_seen"] * 2 > len(keep)
    med["confidence"] = statistics.median([float(f["confidence"]) for f in keep])
    med["obstructions"] = _boxes([f.get("obstructions") or [] for f in keep])
    med["n"], med["n_frames"] = len(keep), len(frames)
    # The spread is the fixed-camera test: this whole file assumes one window
    # frames the whole reel, which is false if the shot cuts or the camera
    # moves. Reported, never silently averaged away.
    med["spread_cx"] = _spread([float(f["head"]["cx"]) for f in keep])
    med["spread_cy"] = _spread([float(f["head"]["cy"]) for f in keep])
    return _reconcile(med)


def _reconcile(m):
    """Make crown_y and face_cy agree with the head box before the geometry.

    Medianed per key, the three readings can come from three different passes,
    and vertical() then subtracts two of them from each other. The box is the
    one of the three that holds still, so it is the one that arbitrates: crown
    is its top, and face_cy is clamped to FACE_IN_HEAD head heights below that.
    Both raw readings are kept for report() to print -- when the clamp fires it
    is the model that was wrong, and that is worth seeing rather than hiding."""
    top = m["head_cy"] - m["head_h"] / 2.0
    lo = top + FACE_IN_HEAD[0] * m["head_h"]
    hi = top + FACE_IN_HEAD[1] * m["head_h"]
    m["crown_reported"], m["crown_y"] = m["crown_y"], top
    m["face_reported"] = m["face_cy"]
    m["face_cy"] = min(max(m["face_cy"], lo), hi)
    return m


def _vote(values, fallback):
    values = [v for v in values if v]
    if not values:
        return fallback
    return max(set(values), key=values.count)


def _spread(v):
    return (max(v) - min(v)) if len(v) > 1 else 0.0


def _boxes(per_frame, min_share=0.5):
    """Obstruction boxes seen in at least half the frames, unioned.

    A box in one frame of four is either a hallucination or an INTERMITTENT
    lower-third; neither may narrow the window. A real burned-in graphic is in
    every frame."""
    clusters = []
    for boxes in per_frame:
        for b in boxes:
            if not all(isinstance(b.get(k), (int, float))
                       for k in ("x", "y", "w", "h")):
                continue
            for c in clusters:
                if _overlaps(c["box"], b):
                    c["box"] = _union(c["box"], b)
                    c["n"] += 1
                    break
            else:
                clusters.append({"box": dict(b), "n": 1})
    need = max(1, int(round(min_share * len(per_frame))))
    return [c["box"] for c in clusters if c["n"] >= need]


def _overlaps(a, b):
    return (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and
            a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])


def _union(a, b):
    x0, y0 = min(a["x"], b["x"]), min(a["y"], b["y"])
    x1 = max(a["x"] + a["w"], b["x"] + b["w"])
    y1 = max(a["y"] + a["h"], b["y"] + b["h"])
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


# --- the geometry (no model involved) --------------------------------------

def targets(style, side, block):
    """Where the reciter's block centre and the caption centre must land.

    Both are fractions of the OUTPUT window; `side` is the caption's side.
    `block` is the head bounding box width as a fraction of the same window --
    see the module docstring for why it is the head and not the body.

    ONE rule for both styles, because the two per-style rules this replaces
    were the same rule written twice: default's `A = (0.49 - w_r)/3` has 0.49 =
    1 - 0.504, the caption column's width, so A is "split what is left over into
    three equal gaps"; bars' "anchor 0.30 W, reciter clears 0.60 W" is
    (0.60 - 0.45)/2 + 0.45/2 = 0.30 exactly, equal gaps either side of a 0.45
    column with the reciter running off his outer edge."""
    cap_w = CAPTION_W[style]
    gaps = 1.0 - block - cap_w
    outer = gaps / 3.0
    if outer < MIN_GAP:
        # No room for air on his outer side: let him run off that edge and
        # split what is left into the two gaps that bracket the caption.
        outer, inner = 0.0, max(gaps / 2.0, MIN_GAP)
    else:
        inner = outer
    fx = outer + block / 2.0
    cap = outer + block + inner + cap_w / 2.0
    if side == "left":                    # caption LEFT -> reciter RIGHT
        return 1.0 - fx, 1.0 - cap, outer, inner
    return fx, cap, outer, inner


def caption_side(m):
    """Which side the caption column goes on.

    The composition rule is "the side he faces" -- a reciter turned to screen
    left has the room in front of his face on that side, and text behind a head
    reads as text stuck to it. Square to camera says nothing, so fall back to
    the side he is NOT on."""
    if m["facing"] in ("left", "right"):
        return m["facing"]
    return "left" if m["head_cx"] > 0.5 else "right"


def vertical(H, h, m):
    """-> (y, lo, hi, ok). The legal band of window tops, and the pick in it.

    lo/hi are returned rather than just y because the obstruction search moves
    the window afterwards, and it may only move it INSIDE this band: an earlier
    version let the search slide y toward 0.275 for a cheaper cost, which on
    -GZR1C9Acd4 walked the window down 28 px past his crown and then reported
    the face at a legal 0.328 -- the exact bowed-reciter failure this rule
    exists to refuse."""
    crown = m["crown_y"] * H
    face = m["face_cy"] * H
    lo = max(face - FACE_Y_BAND[1] * h, crown - MAX_HEADROOM * h, 0.0)
    hi = min(face - FACE_Y_BAND[0] * h, crown, float(H - h))
    want = face - FACE_Y_FRAC * h
    ok = lo <= hi
    if not ok:
        # The band and the crown disagree -- a bowed reciter. Keep the crown in
        # frame and let the face land where it lands; the caller refuses to
        # write and asks for a hand solve.
        lo = max(crown - MAX_HEADROOM * h, 0.0)
        hi = max(min(crown, float(H - h)), lo)
    lo, hi = max(0.0, lo), min(float(H - h), hi)
    hi = max(hi, lo)
    y = int(round(min(max(want, lo), hi) / 2.0)) * 2
    return max(0, min(y, (H - h) // 2 * 2)), lo, hi, ok


def body_edges(W, m):
    """His silhouette in source px, clamped. -> (left, right, clamped).

    NEVER used by targets(): the body's only job is containment. The clamp is
    legacy/qc/author/crop.py:89-90 -- a face is head_w / HEAD_FROM_FACE wide,
    and he may reach BODY_FROM_FACE face widths out from the centre of his head
    on either side. See "clamping the body reading" in the module docstring for
    the five sources' measured reach and for the 1.201-W congregation this
    exists to cut back down to a reciter."""
    head_cx = m["head_cx"] * W
    reach = (BODY_FROM_FACE / HEAD_FROM_FACE) * m["head_w"] * W
    raw_l, raw_r = m["body_left"] * W, m["body_right"] * W
    left = max(raw_l, head_cx - reach, 0.0)
    right = min(raw_r, head_cx + reach, float(W))
    return left, right, (left > raw_l + 0.5 or right < raw_r - 0.5)


def horizontal(W, w, m, side, cap_cx, cap_w):
    """-> (lo, hi, c_lo, c_hi, contained). The legal band of window lefts.

    Two constraints, and their ORDER is the whole fix (see the module
    docstring's body section):

      CAPTION  the caption column may not cross his head box. Hard: pills on a
               ghutrah is the one thing no shipped reel does.
      CONTAIN  inside what the caption leaves, the window must hold his whole
               silhouette. Yields when it cannot, because a wide shot running
               him off his outer edge is the two-gap branch of the rule working
               as designed -- the shipped ABDULLAH-AL-JUHANY reel does exactly
               that, 72 px of bisht past the right edge, and the containing
               window for it puts text on his face.

    Returned as a band rather than an x because the obstruction search moves
    the window afterwards and may only move it inside here -- same reason
    vertical() returns one."""
    bl, br, _ = body_edges(W, m)
    lo, hi = 0.0, float(W - w)
    head_l = (m["head_cx"] - m["head_w"] / 2.0) * W
    head_r = (m["head_cx"] + m["head_w"] / 2.0) * W
    if side == "left":                # caption LEFT: his head must stay right
        hi = min(hi, head_l - (cap_cx + cap_w / 2.0) * w)
    else:                             # caption RIGHT: his head must stay left
        lo = max(lo, head_r - (cap_cx - cap_w / 2.0) * w)
    lo = max(0.0, min(lo, float(W - w)))
    hi = max(lo, min(hi, float(W - w)))
    c_lo, c_hi = br - w, bl           # contain right edge / contain left edge
    if max(lo, c_lo) <= min(hi, c_hi):
        return max(lo, c_lo), min(hi, c_hi), c_lo, c_hi, True
    return lo, hi, c_lo, c_hi, False


def place(W, H, m, style, side, scale):
    """The window at one zoom level, before obstruction avoidance."""
    base_w = min(float(W), H * 16.0 / 9.0)
    w = int(round(base_w * scale / 2.0)) * 2
    h = int(round(w * 9.0 / 16.0 / 2.0)) * 2
    if w > W or h > H or w < 160:
        return None
    head_w = m["head_w"] * W
    body_l, body_r, clamped = body_edges(W, m)
    block = min(head_w / float(w), 1.0)
    fx_t, cap_cx, outer, inner = targets(style, side, block)
    x_lo, x_hi, c_lo, c_hi, contained = horizontal(W, w, m, side, cap_cx,
                                                   CAPTION_W[style])
    # Even both ends before clamping, so rounding x to an even px inside the
    # band cannot land 1 px back outside it and re-open the cut. The band is
    # then the ONLY thing bounding x, so it carries the frame limit too.
    lim = float((W - w) // 2 * 2)
    x_lo = min(max(0.0, 2.0 * int(-(-x_lo // 2.0))), lim)
    x_hi = min(max(x_lo, 2.0 * int(x_hi // 2.0)), lim)
    gap_x = (m["head_cx"] * W - fx_t * w)      # what the gap rule alone wants
    x = int(round(min(max(gap_x, x_lo), x_hi) / 2.0)) * 2
    y, y_lo, y_hi, y_ok = vertical(H, h, m)
    s = {"x": x, "y": y, "w": w, "h": h, "scale": scale, "y_lo": y_lo,
         "y_hi": y_hi, "block": block,
         "body_frac": max(body_r - body_l, 0.0) / float(w),
         "body_l": body_l, "body_r": body_r, "body_clamped": clamped,
         "x_lo": x_lo, "x_hi": x_hi, "c_lo": c_lo, "c_hi": c_hi,
         "contained": contained, "gap_x": gap_x,
         "fx_target": fx_t, "caption_cx": cap_cx, "outer": outer,
         "inner": inner, "y_ok": y_ok}
    return shift(s, W, H, m, 0, 0)


def shift(sol, W, H, m, dx, dy):
    """The same window moved, with its errors recomputed.

    x is confined to the band horizontal() returned and y to vertical()'s --
    see there for why."""
    s = dict(sol)
    x = min(max(sol["x"] + dx, sol["x_lo"]), sol["x_hi"])
    s["x"] = int(min(max(round(x / 2.0) * 2, sol["x_lo"]), sol["x_hi"]))
    y = min(max(sol["y"] + dy, sol["y_lo"]), sol["y_hi"])
    s["y"] = max(0, min(int(round(y / 2.0)) * 2, (H - sol["h"]) // 2 * 2))
    s["fx"] = (m["head_cx"] * W - s["x"]) / float(s["w"])
    s["fy"] = (m["face_cy"] * H - s["y"]) / float(s["h"])
    s["headroom"] = (m["crown_y"] * H - s["y"]) / float(s["h"])
    # Signed, in window widths: negative = this edge cuts through him.
    s["margin_l"] = (sol["body_l"] - s["x"]) / float(s["w"])
    s["margin_r"] = (s["x"] + s["w"] - sol["body_r"]) / float(s["w"])
    return s


def cost(s):
    return (3.0 * abs(s["fx"] - s["fx_target"])
            + 2.0 * abs(s["fy"] - FACE_Y_FRAC) + 0.5 * (1.0 - s["scale"]))


def hits(s, boxes, W, H, pad=6):
    """The first obstruction box the window overlaps, or None."""
    for b in boxes:
        bx, by = b["x"] * W, b["y"] * H
        bw, bh = b["w"] * W, b["h"] * H
        if (s["x"] < bx + bw + pad and bx - pad < s["x"] + s["w"] and
                s["y"] < by + bh + pad and by - pad < s["y"] + s["h"]):
            return b
    return None


def solve(W, H, m, style, side):
    """Largest 16:9 window that frames him by the rules and misses the graphics.

    Zoom is a last resort, not a knob: start at the biggest window the source
    allows and shrink only because a bigger one cannot both place him AND clear
    a burned-in logo. That is the reasoning written by hand into
    clips/al-ahzab-70-71's config, made mechanical.

    CONTAINMENT LIVES IN THE BAND, NOT HERE, and that is deliberate. It is
    applied by horizontal() inside each zoom level, so within a window size the
    solve shifts minimally to hold him; it is NOT a term or a tier in the
    choice BETWEEN zoom levels. Both alternatives were built and measured on
    the four shipped reels:

      as a tier (contains him beats cost), the live YkXjYyKwHJ4 solve jumps to
      a 1536 px window to swallow a body reading that over-runs his real robe
      by 85 px, and lands the face at 0.230 of it -- outside the band, so the
      whole solve then refuses to write a source that has a good window.
      as a cost penalty, no weight separates the two cases: the one that leaves
      1CQYaDe-nLE's shipped window alone also lets a window that cuts 60 px off
      YkXjYyKwHJ4's robe win, and the one that stops that drags 1CQYaDe-nLE
      37 px right, onto the edge of his head.

    Both are the same mistake -- treating "contains him" as more important than
    the vertical rule and the caption clearance, when the shipped reels say it
    is less. BADR-AL-TURKI and ABDULLAH-AL-JUHANY both run the reciter's robe
    off the right edge of frame. Cutting his outer edge is a look this account
    ships; cutting it while REPORTING clean air beside him was the bug."""
    boxes = m["obstructions"]
    best, best_forced = None, None
    for i in range(26):
        s = place(W, H, m, style, side, 1.0 - 0.02 * i)
        if s is None:
            continue
        step = max(8, s["w"] // 100)
        for dx in _offsets(int(0.16 * s["w"]), step):
            for dy in _offsets(int(0.05 * s["h"]), step):
                c = shift(s, W, H, m, dx, dy)
                c["cost"] = cost(c)
                c["blocked_by"] = hits(c, boxes, W, H)
                c["rank"] = (0 if c["blocked_by"] is None else 1, c["cost"])
                # The 0.16 gate is a sanity bound on the gap rule, and the
                # containment band can pin x past it on a source that cannot
                # pan (it had nothing to reject before the band existed). Keep
                # the gate, keep a way out.
                if best_forced is None or c["rank"] < best_forced["rank"]:
                    best_forced = c
                if abs(c["fx"] - c["fx_target"]) > 0.16:
                    continue
                if best is None or c["rank"] < best["rank"]:
                    best = c
        if best is not None and best["rank"][0] == 0 and \
                s["scale"] > 0.92 and \
                abs(best["fx"] - best["fx_target"]) < 0.02:
            break        # a near-full-size window already frames him; stop
            # before trading resolution for a placement it already has.
    if best is None:
        best = best_forced
    if best is None:
        raise SystemExit("no 16:9 window fits inside %dx%d" % (W, H))
    return best


def _offsets(limit, step):
    """0, +step, -step, +2*step ... -- nearest-first so ties keep the ideal."""
    out = [0]
    k = step
    while k <= limit:
        out += [k, -k]
        k += step
    return out


def x_offset_px(sol, style):
    """The caption anchor as generate.py's generic `x_offset`, in canvas px.

    Not a new config key: bars already draws its pills at BAND_W/2 + x_offset,
    so the anchor IS this number. The bars golden uses -216 on a 1080 canvas,
    which is 0.30 W."""
    return int(round((sol["caption_cx"] - 0.5) * CANVAS_W[style]))


# --- reporting -------------------------------------------------------------

def report(m, sol, style, W, H):
    """Print the solve, and every reason it might be wrong. -> [problems]"""
    bad = []
    print("head    : cx %.3f cy %.3f  %.3f x %.3f W/H  (median of %d/%d frames, "
          "confidence %.2f)" % (m["head_cx"], m["head_cy"], m["head_w"],
                                m["head_h"], m["n"], m["n_frames"],
                                m["confidence"]))
    print("          face visible in %d/%d frames%s"
          % (m.get("face_seen", 0), m["n"],
             "" if m.get("face_visible", True) else "  <- NO FACE, see below"))
    print("          facing %s, posture %s; body %.3f..%.3f W; crown %.3f H"
          % (m["facing"], m["posture"], m["body_left"], m["body_right"],
             m["crown_y"]))
    print("          head spread across frames: cx %.3f W, cy %.3f H"
          % (m["spread_cx"], m["spread_cy"]))
    was = (m["face_reported"] - m["crown_reported"]) / m["head_h"]
    now = (m["face_cy"] - m["crown_y"]) / m["head_h"]
    if abs(was - now) > 0.005:
        print("          reconciled to the head box: crown %.3f -> %.3f H, "
              "face %.3f -> %.3f H; the face was reported %.2f head heights "
              "below the crown and the pixels say %.2f-%.2f, so %.2f is used."
              % (m["crown_reported"], m["crown_y"], m["face_reported"],
                 m["face_cy"], was, FACE_IN_HEAD[0], FACE_IN_HEAD[1], now))
    if max(m["spread_cx"], m["spread_cy"]) > 0.06:
        print("! the head moves more than 6%% of frame between samples -- this "
              "shot cuts or the camera pans, and ONE crop is wrong for it. "
              "Check the sampled frames before writing.", file=sys.stderr)
    if m["posture"] != "upright":
        print("! posture is %r. The 0.275 face rule is a PROXY for headroom and "
              "it fails on a reciter who bows -- check the top of his head is "
              "in frame on --annotate, not just the number below."
              % m["posture"], file=sys.stderr)
    if sol["body_clamped"]:
        print("! body came back reaching further than %.1f face widths from "
              "the centre of his head (%.3f..%.3f W measured, %.3f..%.3f W "
              "used). That is the 9Yci0oWB2fE failure: the model is measuring "
              "the congregation, not the reciter -- check --annotate."
              % (BODY_FROM_FACE, m["body_left"], m["body_right"],
                 sol["body_l"] / float(W), sol["body_r"] / float(W)),
              file=sys.stderr)

    print("crop    : {x: %d, y: %d, w: %d, h: %d}   (%.2fx zoom of %dx%d)"
          % (sol["x"], sol["y"], sol["w"], sol["h"], W / float(sol["w"]), W, H))
    print("head    : x %.3f of window (target %.3f), y %.3f  [block %.3f W, "
          "body %.3f W]" % (sol["fx"], sol["fx_target"], sol["fy"],
                            sol["block"], sol["body_frac"]))
    print("gaps    : outer %.3f | caption %.3f | inner %.3f | block %.3f | "
          "outer %.3f" % (sol["outer"], CAPTION_W[style], sol["inner"],
                          sol["block"], sol["outer"]))
    print("face    : %.3f of window height (rule %.3f, band %.2f-%.2f); "
          "headroom %.3f (model asked %.3f)"
          % (sol["fy"], FACE_Y_FRAC, FACE_Y_BAND[0], FACE_Y_BAND[1],
             sol["headroom"], m["headroom_frac"]))
    # Before the band check, because the band is a statement ABOUT a face and
    # is meaningless when there is not one. Every other guard here compares the
    # model's numbers with each other, so all of them pass on a shot where it
    # boxed something that is not his head: on gt9y-QGgMsA (head fully draped,
    # turned away) it boxed his shoulder and returned confidence 0.98, zero
    # spread across 12 frames and a face at exactly 0.275 -- a window whose top
    # edge sat 130px BELOW his real crown. Nothing downstream could have caught
    # that; only "is there a face at all" can.
    if not m.get("face_visible", True):
        bad.append("no face is visible in %d of %d frames"
                   % (m["n"] - m.get("face_seen", 0), m["n"]))
        print("! NO FACE IS VISIBLE in this shot (%d of %d frames). Every "
              "number above is then a box drawn round something that is not "
              "his face, and it will still look self-consistent -- confident, "
              "steady between frames, landing inside the band. Do not trust "
              "it and do not re-run for a better answer. HAND-SOLVE the crop: "
              "measure his crown, his body edges, where the congregation and "
              "any burned-in graphics start over several frames, put his head "
              "centre at %.3f of the window, and write crop:/x_offset: "
              "yourself with the reasoning in a comment."
              % (m["n"] - m.get("face_seen", 0), m["n"], sol["fx_target"]),
              file=sys.stderr)
    if not sol["y_ok"]:
        # An aesthetic note, not a refusal -- see FACE_Y_BAND's comment.
        # 0.24-0.34 is the shipped reels' own scatter, not a tolerance, and the
        # model's run-to-run noise on this number is bigger than the band is
        # wide, so gating a write on it was a coin flip wearing a threshold's
        # clothes. The crown was already kept in frame by vertical()'s own
        # fallback (that part never refused); this print is only "the face
        # landed somewhere the references didn't," worth a look on --annotate,
        # never worth a SystemExit.
        print("! face lands at %.3f of the window, outside the %.2f-%.2f the "
              "shipped reels span. Not fatal -- check --annotate before "
              "trusting it, especially on a bowed reciter (see the al-Ansari "
              "clips), but the config is written regardless."
              % (sol["fy"], FACE_Y_BAND[0], FACE_Y_BAND[1]), file=sys.stderr)
    # Containment. Printed after the gaps because it is the thing the gaps
    # cannot see: `outer` is the air beyond his HEAD, and a window can have
    # 0.106 of it while cutting his back (the vqxYwdR4RvQ solve of 2026-08-01).
    print("body    : %.3f..%.3f of window; margin left %+.3f, right %+.3f "
          "(%.3f W of frame, clamped to %.1f face widths of head centre)"
          % ((sol["body_l"] - sol["x"]) / float(sol["w"]),
             (sol["body_r"] - sol["x"]) / float(sol["w"]),
             sol["margin_l"], sol["margin_r"], sol["body_frac"] * sol["w"] / W,
             BODY_FROM_FACE))
    edge, want = None, None
    # The bound also has to be INSIDE the frame, or the frame edge is what
    # actually moved the window and containment would be taking the credit.
    if sol["contained"]:
        if sol["gap_x"] < sol["c_lo"] - 1.0 and sol["c_lo"] > 0.0:
            edge, want = "right", sol["c_lo"]
        elif sol["gap_x"] > sol["c_hi"] + 1.0 and sol["c_hi"] < W - sol["w"]:
            edge, want = "left", sol["c_hi"]
    if edge:
        print("          the %s edge is set by CONTAINMENT, not the gap rule: "
              "equal gaps wanted x=%d, his silhouette needs x=%d, so the "
              "window moved %d px."
              % (edge, int(round(sol["gap_x"])), int(round(want)),
                 abs(int(round(want - sol["gap_x"])))))
    if min(sol["margin_l"], sol["margin_r"]) < 0.0:
        print("! this window CUTS HIM: %s edge, %d px of silhouette outside "
              "the crop. At this zoom no window holds him that also keeps the "
              "caption column off his head, so he bleeds off that edge -- the "
              "two-gap branch of the rule, which the shipped BADR-AL-TURKI and "
              "ABDULLAH-AL-JUHANY reels both do. Confirm on --annotate that it "
              "reads as framing and not as an accident."
              % ("left" if sol["margin_l"] < sol["margin_r"] else "right",
                 int(round(abs(min(sol["margin_l"], sol["margin_r"]))
                           * sol["w"]))), file=sys.stderr)
    if abs(sol["fx"] - sol["fx_target"]) > 0.02 and not edge:
        print("! the source cannot pan far enough: he wants to be at %.3f of "
              "the window and can only reach %.3f. A tighter window would get "
              "there at the cost of resolution."
              % (sol["fx_target"], sol["fx"]), file=sys.stderr)

    cap = sol["caption_cx"]
    print("caption : centre %.3f of window -> x_offset %d px on the %d-wide %s "
          "canvas" % (cap, x_offset_px(sol, style), CANVAS_W[style], style))
    if not 0.0 < cap < 1.0:
        bad.append("caption centre %.3f is off the frame" % cap)
        print("! caption centre %.3f is OFF THE FRAME. This is the -0.105 "
              "failure in sources/9Yci0oWB2fE/meta.yaml -- the block width is "
              "wrong, not the formula." % cap, file=sys.stderr)
    if m["obstructions"]:
        print("graphics: %d burned-in box(es) seen in most frames:"
              % len(m["obstructions"]))
        for b in m["obstructions"]:
            print("          x %.3f..%.3f  y %.3f..%.3f"
                  % (b["x"], b["x"] + b["w"], b["y"], b["y"] + b["h"]))
    if sol.get("blocked_by"):
        b = sol["blocked_by"]
        print("! no window clears the graphic at x %.3f..%.3f y %.3f..%.3f; "
              "this is the best window IGNORING it. Either it is furniture the "
              "model mislabelled (check --annotate), or accept the logo, or "
              "hand-solve." % (b["x"], b["x"] + b["w"], b["y"], b["y"] + b["h"]),
              file=sys.stderr)
    return bad


def annotate(frame_path, sol, m, style, out_path):
    """Draw the solve over a real frame. Pillow, because the pipeline has it."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(frame_path).convert("RGB")
    # The frame was scaled for the model; the solve is in source px.
    W, H = im.size
    sx, sy = W / float(m["_W"]), H / float(m["_H"])
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.load_default(size=max(11, W // 80))
    except TypeError:                     # Pillow < 10.1: fixed-size bitmap
        font = ImageFont.load_default()

    def box(x0, y0, x1, y1, colour, width=2):
        d.rectangle([x0 * sx, y0 * sy, x1 * sx, y1 * sy], outline=colour,
                    width=width)

    for b in m["obstructions"]:           # red: burned-in graphics
        box(b["x"] * m["_W"], b["y"] * m["_H"], (b["x"] + b["w"]) * m["_W"],
            (b["y"] + b["h"]) * m["_H"], (255, 40, 40))
    # cyan: the body extent the solver CONTAINED (clamped), dim cyan: what the
    # model actually said. Two lines because when they differ the clamp fired,
    # and that is the congregation case you want to see rather than read.
    for f in (m["body_left"], m["body_right"]):
        d.line([f * W, 0, f * W, H], fill=(0, 110, 110), width=1)
    for px in (sol["body_l"], sol["body_r"]):
        d.line([px * sx, 0, px * sx, H], fill=(0, 230, 230), width=2)
    hx0 = (m["head_cx"] - m["head_w"] / 2.0) * W    # yellow: head box
    hx1 = (m["head_cx"] + m["head_w"] / 2.0) * W
    d.rectangle([hx0, (m["head_cy"] - m["head_h"] / 2.0) * H, hx1,
                 (m["head_cy"] + m["head_h"] / 2.0) * H],
                outline=(255, 220, 0), width=2)
    cy = m["crown_y"] * H
    d.line([0, cy, W, cy], fill=(255, 220, 0), width=1)

    x, y = sol["x"] * sx, sol["y"] * sy               # green: the window
    w, h = sol["w"] * sx, sol["h"] * sy
    d.rectangle([x, y, x + w, y + h], outline=(0, 255, 0), width=3)
    d.line([x, y + FACE_Y_FRAC * h, x + w, y + FACE_Y_FRAC * h],
           fill=(0, 255, 0), width=1)
    cw = CAPTION_W[style]                             # blue: caption column
    c0 = x + (sol["caption_cx"] - cw / 2.0) * w
    d.rectangle([c0, y + 0.30 * h, c0 + cw * w, y + 0.70 * h],
                outline=(80, 160, 255), width=2)

    # The three gaps, labelled, along the top of the window -- the whole point
    # of the rule is that they are equal, and that is checkable by eye only if
    # they are drawn.
    if sol["caption_cx"] < 0.5:
        edges = [(0.0, sol["outer"], "outer %.3f" % sol["outer"]),
                 (sol["outer"] + cw, sol["outer"] + cw + sol["inner"],
                  "inner %.3f" % sol["inner"]),
                 (1.0 - sol["outer"], 1.0, "outer %.3f" % sol["outer"])]
    else:
        edges = [(0.0, sol["outer"], "outer %.3f" % sol["outer"]),
                 (sol["outer"] + sol["block"],
                  sol["outer"] + sol["block"] + sol["inner"],
                  "inner %.3f" % sol["inner"]),
                 (1.0 - sol["outer"], 1.0, "outer %.3f" % sol["outer"])]
    for a, b, label in edges:
        d.line([x + a * w, y + 0.08 * h, x + b * w, y + 0.08 * h],
               fill=(255, 0, 255), width=3)
        d.text((x + a * w + 3, y + 0.09 * h), label, fill=(255, 0, 255),
               font=font)
    d.text((x + 6, y + 6), "crop %d,%d %dx%d   face %.3f H   caption %.3f W"
           % (sol["x"], sol["y"], sol["w"], sol["h"], sol["fy"],
              sol["caption_cx"]), fill=(0, 255, 0), font=font)
    im.save(out_path)
    return out_path


# --- the cache -------------------------------------------------------------

def cache_path(config_path):
    return os.path.join(os.path.dirname(os.path.abspath(config_path)),
                        "crop.json")


def cache_key(times):
    """Keyed by the frame timestamps, so re-running to tune the GEOMETRY costs
    nothing and only a change of frames pays again."""
    return "%s|%s" % (MODEL, ",".join("%.2f" % t for t in times))


def cache_read(path, key):
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path, encoding="utf-8")).get(key)
    except ValueError:
        return None


def cache_write(path, key, entry):
    data = {}
    if os.path.exists(path):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except ValueError:
            data = {}
    data[key] = entry
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write("\n")


# --- writing the config ----------------------------------------------------

MARK = "# framing solved by pipeline/crop.py"


def block_lines(sol, style, source, when, m):
    """The provenance comment plus the two keys, as YAML lines.

    The comment names the model and the date because that is the only record
    of WHERE these numbers came from once they are four integers in a config
    -- and because a re-solve six months from now will be a different model."""
    return [
        "%s on %s,\n" % (MARK, when),
        "# %s, median of %d/%d sampled frames.\n"
        % (source, m["n"], m["n_frames"]),
        "# Head box %.3f of the window, %s caption column %.3f -> equal %.3f\n"
        % (sol["block"], style, CAPTION_W[style], sol["outer"]),
        "# gaps; face centre at %.3f of the window height. Re-solve with\n"
        % sol["fy"],
        "# `pipeline/crop.py <this-file> --force`; edit these two by hand and\n",
        "# they stay edited (crop.py then refuses without --force).\n",
        "crop: {x: %d, y: %d, w: %d, h: %d}\n"
        % (sol["x"], sol["y"], sol["w"], sol["h"]),
        "x_offset: %d\n" % x_offset_px(sol, style),
    ]


def write_config(path, lines_to_write, force):
    """Put `crop:` and `x_offset:` into the config as a LINE EDIT.

    yaml.safe_dump would round-trip the file and eat every hand-written comment
    in it, and those comments are where each reel's reasoning lives -- the same
    reason align.py's write_trim edits lines."""
    text = open(path, encoding="utf-8").read()
    if not text.endswith("\n"):
        text += "\n"
    lines = text.splitlines(keepends=True)

    # Our own previous block: the marker, its comment lines, and the keys.
    at = next((i for i, l in enumerate(lines) if l.startswith(MARK)), None)
    if at is not None:
        end = at
        while end < len(lines) and (
                lines[end].lstrip().startswith("#")
                or re.match(r"^(crop|x_offset)\s*:", lines[end])):
            end += 1
        del lines[at:end]

    stray = [i for i, l in enumerate(lines)
             if re.match(r"^(crop|x_offset)\s*:", l)]
    if stray and not force:
        raise SystemExit(
            "%s already has a hand-written %s (line %d). It was put there by a "
            "person and this solve would silently replace it -- pass --force if "
            "that is what you want."
            % (path, lines[stray[0]].split(":")[0], stray[0] + 1))
    for i in reversed(stray):
        del lines[i]

    # Under the verse span / trim, where a reader looks for the source facts.
    keys = r"^(style|signature|surah|ayah_start|ayah_end|trim)\s*:"
    after = [i for i, l in enumerate(lines) if re.match(keys, l)]
    at = max(after) + 1 if after else len(lines)
    while at < len(lines) and not lines[at].strip():
        at += 1
    lines[at:at] = ["\n"] + lines_to_write
    open(path, "w", encoding="utf-8").write("".join(lines))


# --- entry point -----------------------------------------------------------

def run(config_path, frames=4, annotate_path=None, write=False, force=False,
        dry_run=False, measurements=None):
    cfg = generate.load_config(config_path)
    style = cfg["style"]
    if style not in CAPTION_W:
        raise SystemExit(
            "crop.py solves `bars` and `hz` only; this config is `%s`.\n"
            "The default style re-crops at RENDER time from its own YuNet pass "
            "(render_default.py detect_subject/vertical_crop_filter) and must "
            "not be given a crop here." % style)
    cfg = generate.resolve_paths(cfg, config_path)
    src = cfg["input"]
    info = generate.get_video_info(src)
    W, H = info["width"], info["height"]
    if not W:
        raise SystemExit("%s has no video stream" % src)

    # Frames come from the reel's OWN window: how the reciter sits during these
    # 30 seconds is the question, not how he sits across a 20-minute upload.
    t0, t1 = 0.0, info["duration"]
    if cfg.get("trim"):
        t0 = float(cfg["trim"][0])
        t1 = float(cfg["trim"][1]) if len(cfg["trim"]) > 1 and \
            cfg["trim"][1] is not None else info["duration"]
    cap = max(1, (CTX - 8192) // EST_TOKENS_PER_FRAME)
    if frames > cap:
        print("      --frames %d would not fit %s's %d-token context at ~%d "
              "tokens a frame; using %d." % (frames, MODEL, CTX,
                                             EST_TOKENS_PER_FRAME, cap))
        frames = cap
    times = sample_times(t0, t1, frames)
    print("%s  %dx%d  %s, %d frames over %.1f-%.1fs"
          % (os.path.basename(config_path), W, H, style, len(times), t0, t1))

    tmp_dir = os.path.join(cfg["tmp_dir"], "crop")
    paths, dims = extract(src, times, tmp_dir, W, H)
    key = cache_key(times)
    entry = None
    if measurements:
        # Already in fractions: a hand-written regression fixture is written in
        # the geometry's own units, not in some frame's pixels.
        entry = {"parsed": json.load(open(measurements, encoding="utf-8")),
                 "usage": {}, "source": measurements, "grid": False}
    elif not force:
        entry = cache_read(cache_path(config_path), key)
        if entry:
            print("      cached solve (%s) -- --force re-queries"
                  % entry.get("when", "?"))
    if entry is None:
        if dry_run:
            raise SystemExit(
                "--dry-run and nothing cached for these frames. Pass "
                "--measurements FILE with a {\"frames\": [...]} object, or drop "
                "--dry-run to ask the model.")
        pooled, usage, cost_usd = [], {}, 0.0
        for p in range(PASSES):
            parsed, envelope = ask(paths, dims, tmp_dir)
            pooled.extend(parsed["frames"])
            cost_usd += float(envelope.get("total_cost_usd") or 0.0)
            for k, v in (envelope.get("usage") or {}).items():
                if isinstance(v, (int, float)):
                    usage[k] = usage.get(k, 0) + v
            print("      pass %d/%d: %d frame reading(s)"
                  % (p + 1, PASSES, len(parsed["frames"])))
        entry = {"parsed": {"frames": pooled}, "usage": usage,
                 "cost_usd": cost_usd, "model": MODEL,
                 "when": datetime.date.today().isoformat(), "times": times,
                 "frame_size": list(dims), "grid": True, "passes": PASSES}
        cache_write(cache_path(config_path), key, entry)
    if entry.get("grid"):
        to_fractions(entry["parsed"])
    usage = entry.get("usage") or {}
    if usage:
        # Printed so the cost of a solve is checked against a bill rather than
        # trusted: one 4-frame pass measured $0.09-0.13 total_cost_usd (the
        # CLI's own figure, not a hand computation the way the OpenRouter
        # transport needed PRICE_IN/PRICE_OUT for -- Claude Code reports it
        # directly in the --output-format json envelope).
        print("usage   : %s  ->  $%.5f"
              % (json.dumps({k: v for k, v in usage.items()
                             if isinstance(v, (int, float))}),
                 entry.get("cost_usd") or 0.0))

    m = median(entry["parsed"]["frames"])
    m["_W"], m["_H"] = W, H
    side = caption_side(m)
    sol = solve(W, H, m, style, side)
    print("caption : side %s (he faces %s)" % (side, m["facing"]))
    bad = report(m, sol, style, W, H)

    if annotate_path:
        print("annotate: %s" % annotate(paths[len(paths) // 2], sol, m, style,
                                        annotate_path))
    if not write:
        print("(dry run -- pass --write to put crop: and x_offset: in %s)"
              % os.path.basename(config_path))
        return sol
    if bad:
        raise SystemExit("NOT written: %s. Fix it or hand-solve; a wrong crop "
                         "in a config outlives the session that made it."
                         % "; ".join(bad))
    source = ("measurements from %s" % os.path.basename(measurements)
              if measurements
              else "%s via claude code CLI" % entry.get("model", MODEL))
    when = entry.get("when") or datetime.date.today().isoformat()
    write_config(config_path, block_lines(sol, style, source, when, m), force)
    print("wrote   : %s" % os.path.relpath(config_path, ROOT))
    return sol


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Solve a bars/hz reel's 16:9 crop with a vision model.")
    p.add_argument("config", help="sources/<id>/<reel>.yaml")
    p.add_argument("--frames", type=int, default=4,
                   help="frames to sample and show the model (default 4)")
    p.add_argument("--annotate", metavar="PNG",
                   help="write a labelled debug frame")
    p.add_argument("--write", action="store_true",
                   help="edit crop: and x_offset: into the config")
    p.add_argument("--force", action="store_true",
                   help="re-query the model, and overwrite a hand-written crop")
    p.add_argument("--dry-run", action="store_true",
                   help="never call the model: use --measurements or the cache")
    p.add_argument("--measurements", metavar="JSON",
                   help='offline input: {"frames": [...]}, in FRACTIONS of'
                        ' the frame (not the model\'s 0-1000 grid)')
    a = p.parse_args(argv)
    run(a.config, frames=a.frames, annotate_path=a.annotate, write=a.write,
        force=a.force, dry_run=a.dry_run, measurements=a.measurements)


if __name__ == "__main__":
    main()
