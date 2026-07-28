---
name: make-post
description: >-
  Make a Quran recitation clip/post from a YouTube URL for the Quran clipping
  account. Trigger on requests like "make a post from <youtube url>", "make a
  clip from this recitation", "turn this recitation into a reel/post". Runs the
  proven ~/quran-clips pipeline: prep (identify surah/ayah, pick + time the
  segment, write clip.yaml) -> render (config-driven ffmpeg + Pillow overlays)
  -> QA vs the account's own IG refs -> user review loop -> export the approved
  reel to reels/ (permanent library; clip folder becomes disposable). TWO visual
  styles: the DEFAULT landscape style, and an opt-in vertical 9:16 "bars" style
  (letterboxed footage band + Arabic-only Thuluth captions on coloured pills) —
  triggered when the user says "bars style", "the bars one", "highlight style",
  "the new style", "the vertical one" or "the letterbox style".
---

# make-post — Quran clip pipeline

You are the ORCHESTRATOR. You do not render or edit files yourself; you spawn
subagents per stage, verify their outputs, and drive the user review loop.
Repo root: `~/quran-clips`. Read `style/STYLE_SPEC.md` and `templates/style.yaml`
before stage 1 if not already in context — they are the authoritative style.
If the run is **bars style**, read `templates/bars.yaml`, `style/refs2/STYLE2_SPEC.md`
and `style/refs2/FX_RECIPE.md` instead (plus `style/refs2/SNOW_SPEC.md` if snow is
questioned), and follow the `**bars**` branch in every stage below.

## Inputs (take the defaults; only ask when genuinely ambiguous)
- A YouTube recitation URL (required).
- Optional: preferred ayah / segment. **If the user didn't name one, that IS the
  answer — you pick it. Do not ask** (user feedback, al-infitar-6-9: "if i didnt
  specify then its clear").
- Optional: a reciter scene image. **No image supplied → LIVE footage mode. Do
  not ask** (same run: "i didnt give u a image and its kinda obvious"). Only the
  static-still path needs an image, and the user volunteers it when they want it.
- Target clip length: 20–45s.
- **Style. DEFAULT unless the user names the other one.** A bare "make a post
  from <url>" is always the original landscape style — never ask, never offer.
  - Canonical name: **`bars`** (it is literally the `style: bars` key in
    clip.yaml and `templates/bars.yaml`).
  - Switch the WHOLE pipeline to bars when the request contains any of (the user
    dictates these aloud, so match loosely, case-insensitively, and accept them
    anywhere in the sentence): **"bars"**, "bars style", "the bars one", "bar
    style", "the bar one", **"highlight"** / "highlighter" style, **"the new
    style"** / "the new one", "style 2" / "second style", **"vertical"** /
    "the vertical one" / "9:16", **"letterbox"** / "letterboxed" style,
    "thuluth style", "Arabic-only style". Nothing else switches it — if the
    wording is outside this list and you are unsure, use the DEFAULT and say so
    in one line; do not stall the run on a question.
  - Once chosen the style is fixed for the run and is recorded in exactly two
    places: `style: bars` in clip.yaml and a `style-bars` keyword in tags.yaml.
  - Mid-review the user may ask to "redo it in the bars style": that is a new
    clip dir (same `sources/` download reused), not an edit of the old one.

## Orchestration model
Spawn Opus subagents (background) for stages 1–2, a Fable subagent for stage 3.
Give each the clip dir path, **the chosen style (`default` or `bars`)**, and tell
it to read the same on-disk specs and to follow that style's branch of this file.
Verify every stage's output before advancing.

### Stage 1 — Prep agent (Opus)
Produce `clips/<name>/clip.yaml` and `clips/<name>/work/source.mp4`.
- `yt-dlp` for metadata + `--write-auto-subs --sub-lang ar-orig` (auto-ASR).
- Identify reciter + surah/ayah range: match the ASR against known Quran text;
  verse-end rhyme words are the strongest anchors.
- **STANDARD ASR (fixed 2026-07-21): if local transcription is needed** (no
  usable auto-subs, or verifying a passage), use the dedicated venv —
  `tools/asr-venv/bin/python` with `mlx_whisper.transcribe(...,
  path_or_hf_repo="mlx-community/whisper-large-v3-turbo", language="ar",
  word_timestamps=True)` (model already in the HF cache; Metal-accelerated).
  NEVER pip-install ANY whisper variant (openai-whisper, faster-whisper,
  whisperx, mlx-whisper) into `/opt/homebrew/bin/python3` or any other
  interpreter — past churn like that wiped the RAQM Pillow build (see raqm
  restore note below). If the venv is missing, recreate it:
  `/opt/homebrew/bin/python3 -m venv tools/asr-venv &&
  tools/asr-venv/bin/pip install mlx-whisper`.
- Download source ≤1080p into `sources/<youtube-video-id>.mp4` (flat; subs
  alongside as `sources/<id>.ar-orig.vtt`). If the default clients 403 or return
  DRM-only formats (learned al-infitar-6-9), add
  `--extractor-args "youtube:player_client=web_embedded"` — it was the only
  client exposing format 137 (1080p). REUSE if it already exists —
  `sources/` is the durable download cache, shared across clips. Then symlink:
  `ln -s ../../../sources/<id>.mp4 clips/<name>/work/source.mp4`.
  NEVER put a real source file inside a clip folder — clip folders are
  disposable after posting (see Stage 5).
- Pick the strongest SELF-CONTAINED segment ending on pause boundaries. The
  video's title-verse is usually the winner. Target 20–45s.
- Refine phrase timings with ffmpeg `silencedetect` (`noise=-35dB:d=0.35`);
  split phrases at Uthmani waqf marks.
- CAUTION (learned ash-shuara-61-62): under heavy hall reverb the noise floor
  can sit ~−22 dB and silencedetect@−35dB finds NOTHING — fall back to a
  10–20ms RMS envelope (+ pitch/ZCR if needed). Auto-ASR word timestamps drift
  up to ~1.7s during melismas: NEVER anchor segment/phrase boundaries on ASR
  alone; anchor on envelope waqf gaps (true silence cannot occur inside a held
  madd) and VERIFY the extracted final window (speech onset ~0.5s after t=0,
  dip at each card boundary, no next-verse speech in the tail).
- IG HOOK — tighten the head/dead air (learned al-ahzab-70-71): trim
  `segment.start` so recitation onset lands right as the 0.3s fade-from-black
  completes (~rel 0.1–0.3s), NOT ~0.5s+ later — a dead second at the head kills
  reach. Users may ask for it even tighter (onset ~rel 0.07). Also remove the
  breath BETWEEN ayat with `segment.cuts` (see Constraints) so the clip flows;
  cut only in verified true silence, leaving ~0.15s lead before the next phrase.
- LISTEN for ibtidāʾ: after a pause the reciter often RESTARTS from an earlier
  word before continuing. Captions must mirror what is actually RECITED, not just
  the verse text — if the reciter repeats words, those repeated words must appear
  at the START of the next phrase's caption too, in BOTH `ar` (reuse the exact
  Uthmani codepoints, incl. any waqf mark) and `en` (the matching translation
  fragment). The repeated text then correctly shows in both phrases.
- Ibtidāʾ check is MANDATORY per pause (learned al-anam-76-77 + al-anam-122-122:
  prep shipped two missed restarts the user caught by ear): for EVERY internal
  pause ≥~0.5s, run word-timestamp ASR on the window around it and COMPARE the
  words after the pause against the words before — a restart shows as repeated
  text. Never classify a pause as "just breath" without this check. Also expect
  ABORTED starts: the reciter may begin a word/phrase, break off, then restart
  from earlier — the aborted fragment gets its own short caption card (e.g.
  «لَأَكُونَنَّ» / "I will surely be—"), then the restart card repeats the
  earlier words.
- WHOLE-AYAH repeats are different from ibtidāʾ (learned al-infitar-6-9): a
  reciter may recite an entire ayah twice before moving on. Do NOT caption it as
  a repeated card — start `segment.start` on the SECOND, complete pass, which
  also buys a tighter hook. mlx-whisper DEDUPES such repeats into one segment, so
  ASR alone will hide them: an unexplained envelope dip plus a longer-than-
  expected span before your intended onset is the tell — window-scope the ASR
  there to confirm which pass is which.
- Fetch exact text (keyless, verified):
  `https://api.alquran.cloud/v1/ayah/S:A/editions/quran-uthmani,en.sahih`
  Fetch with `curl` and parse the raw JSON `text` fields (learned al-anam batch):
  WebFetch's summarizer CORRUPTS Uthmani codepoints (drops/normalizes diacritics,
  e.g. إِنِّى → إِنَّ) — never let a model transcribe the Arabic; copy the JSON
  string verbatim.
- Write `clip.yaml` in the schema of `clips/at-tawbah-51-51/clip.yaml`
  (source_url, reciter, scene_image, scene_crop, text.center_x_frac/center_y_frac,
  segment.start/end as absolute HH:MM:SS.mmm, surah, ayah_range, phrases[] with
  t0/t1 RELATIVE to clip start + ar + en + ayah).
- Scene image: if the user gave one, place it under `assets/reciters/` and set
  `scene_image`; add a `scene_crop` (16:9 window in still coords) if reframing.
- `scene_crop` framing (user-approved on ash-shuara-61-62): place the reciter's
  HEAD CENTER at ≈ x0.72–0.76, y0.45–0.50 of the frame with ~0.25–0.30 headroom
  above the hood/hair — NOT hugging the top edge. Prefer a mild zoom (~1.1–1.15x,
  crop narrower than the still) over a full-width crop, so horizontal position
  stays adjustable during review; a full-width crop pins x and only allows
  vertical shifts. VIEW the still and measure the head coords before choosing.
- LIVE-footage crop framing (learned al-ahzab-70-71): if the reciter is CENTERED
  in-source but the user wants text on one side, crop a 16:9 window that moves
  him to the opposite side AND excludes any burned-in channel overlays (handle,
  logo). VIEW keyframes; grab an overlay-free frame with a `drawgrid` (yellow
  every 0.10W, red every 0.05W) to MEASURE the reciter silhouette's left/right
  edges in output fractions, and get exact caption extents from the phrase PNG
  alpha (`Image.open(p).split()[3].getbbox()`).
- Caption side = the side the reciter FACES (user rule, al-anam batch): put the
  text in the empty space in front of his gaze, not blindly opposite his body.
  (The IG refs satisfy both — reciter on one side, facing the text side.) In
  multi-angle sources the facing can differ per camera angle, so decide per
  clip. If the facing side is where the source's burned-in logos live, the user
  accepts fully visible logos over a framing-wrecking zoom (al-anam-102-103) —
  keep natural framing, keep logos clear of the caption block, and flag it.
- Camera stability is a PER-SEGMENT check, not per-video (learned
  al-anam-102-103: a video that looked single-static-camera in spread samples
  intercut angles + a wide congregation shot inside one chosen window): frame-
  sweep EVERY candidate segment's own window (~every 2s) before writing its
  crop; framing measured on one angle can be wrong for another.
- BALANCED composition (user-approved al-ahzab-70-71): outer margins EQUAL —
  (video-edge → reciter-outer-edge) == (caption-outer-edge → video-edge) — and
  the caption block CENTRED between the reciter's inner edge and the screen edge.
  Solve (translate-only, widest caption half-width `hw`): with reciter output
  width `w_r`, left margin `A = (0.49 − w_r)/3` (using hw≈0.252/0.51-wide caps),
  `caption_center = (A + w_r + 1)/2`. If moving the reciter far enough hits a
  channel logo, ZOOM IN (smaller crop_w) so the crop's right edge clears the logo
  while still placing the reciter left — a wider crop can't do both. Then re-VIEW
  a gridded render and nudge.

#### Stage 1 — **bars** branch (everything above still applies except as stated)
Identical source download, surah/ayah identification, ASR rules, ibtidāʾ checks,
whole-ayah-repeat rule, alquran.cloud fetch and IG-hook head-tighten. What changes:
- **NO English.** `arabic_only: true` in bars.yaml — the renderer never draws a
  translation. Do NOT write `en:` into a bars clip.yaml (it is ignored, and it
  invites someone to "fix" a missing translation that is deliberate).
- **NO ayah medallion**, ever, including on the ayah-completing card. AM_Thulth
  cannot typeset one and NEITHER reference reel shows one. This is a style
  decision, not a bug — do not report it as one and do not add a medallion glyph.
- **Segment selection**: prefer a passage that breaks into SHORT BALANCED
  COUPLETS (~3–4 short words per line, two lines per card). A long unbroken
  clause with no interior syntactic seam is a bad bars segment even if it is a
  great default-style segment — pick a different window rather than fight it.
  Ref card durations are 2.7–8.6s, median 4.7s (STYLE2_SPEC row 33); aim there.
- **Framing**: same LIVE-crop rules, but the crop is scaled into the 1080x608
  band, so it is still a 16:9 window — the framing math is unchanged. Put the
  reciter on ONE side with the OPPOSITE side clear: the caption block is
  CENTRE-anchored at `text.center_x_frac: 0.30` (refs measure 0.275 W rose /
  0.317 W gold), so with the widest allowed bar (0.50 W) the caption spans
  ~0.05–0.55 W. Frame the reciter clear of ~0.60 W to the right. The default
  style's equal-outer-margin solve does NOT apply — the bars anchor is fixed.
- **clip.yaml keys** (schema: `clips/at-tawbah-128-128/clip.yaml`): add
  `style: bars`; phrases carry `ar1` + optional `ar2` (two lines) or `ar` (one
  line) — never `ar` together with `ar1`; keep `ayah:` per phrase; `bar_color:`
  is an OPTIONAL per-clip hex override. **Never hand-tune `bar.auto.*` in
  bars.yaml** — that rule was validated against both references; if the colour
  is wrong for one clip, set `bar_color:` on that clip only.

##### Splitting an ayah into two-line cards (do this deterministically)
1. Draft the split on MEANING first: break at natural syntactic / rhythmic
   boundaries (after a completed clause, before a new predicate, at the seam the
   reciter himself breathes at). Balance the two lines — a 4-word line over a
   1-word line reads wrong; both refs never exceed ~4 short words per line.
2. MEASURE before you commit. Line length drives the ONE shared point size for
   the whole clip: `render_bars` shrinks every card until the widest line fits
   `max_line_width_frac: 0.45` (486px at 1080). One over-long line therefore
   shrinks the ENTIRE clip's type and pushes the bar past the references'
   0.503 W ceiling. Run this against the draft clip.yaml (heredoc, from repo
   root; it is the same measurement the renderer does):
   ```
   /opt/homebrew/bin/python3 - clips/<name> <<'EOF'
   import sys, os
   sys.path.insert(0, "scripts"); import render_bars as rb
   from render_text import load_yaml, truetype, ROOT
   st = load_yaml("templates/bars.yaml"); t = st["text"]
   f96 = truetype(os.path.join(ROOT, t["font"]), int(t["nominal_pt"]))
   clip = load_yaml(os.path.join(sys.argv[1], "clip.yaml"))
   lines = [l for ph in clip["phrases"] for l in rb.phrase_lines(ph)]
   W = int(st["canvas"]["width"]); cap = float(t["max_line_width_frac"]) * W
   raw = {l: rb.bbox_ls(l, f96)[2] - rb.bbox_ls(l, f96)[0] for l in lines}
   pt = max(int(t["min_pt"]), int(int(t["nominal_pt"]) * min(1.0, cap / max(raw.values()))))
   fp = truetype(os.path.join(ROOT, t["font"]), pt)
   base = rb.bbox_ls("ــ", fp)
   print(f"pt={pt}  cap={cap:.0f}px  widest raw@96={max(raw.values()):.0f}px")
   for l in lines:
       b = rb.bbox_ls(l, fp); s = rb.bbox_ls(rb.strip_tashkeel(l), fp)
       noink = [hex(ord(c)) for c in set(l) if not c.isspace()
                and rb.bbox_ls("ـ"+c+"ـ", fp)[1::2] == base[1::2]]
       print(f"  barW={(b[2]-b[0]+2*float(st['bar']['pad_x_px']))/W:.3f}W "
             f"ink/bar={(s[3]-s[1])/float(st['bar']['height_px']):.2f} "
             f"{'MISSING '+','.join(noink) if noink else ''} {l}")
   EOF
   ```
   ACCEPT the split only when: `pt` == 96 (the nominal — anything lower means a
   line is too long, go re-split it), every `barW` ≤ 0.50 W, and no line prints
   `MISSING` (see the zero-width font trap in Constraints). For reference, the
   shipped `at-tawbah-128-128` measures pt=96, barW 0.266–0.498 W,
   ink/bar 2.43–2.80.
3. If a line will not fit, SPLIT THE CARD IN TWO rather than shrinking the type
   — that is exactly what at-tawbah-128-128 did (its original card 1 carried a
   5-word first line at 567px raw, which dragged the whole clip to 85pt and blew
   the bar to 0.545 W, wider than any bar in either reference).
4. A single-line card is legitimate and both refs use them (rose 3 of 9, gold 1
   of 3) — use one for a short ayah-completing tail rather than padding to two.

##### Timing the card swaps
- Card boundaries must land in REAL WAQF GAPS. `silencedetect` at −35 dB is
  useless on this material (see the reverb CAUTION above): build a 10–20ms RMS
  envelope, find the troughs, and CROSS-CHECK with mlx-whisper word timestamps
  from `tools/asr-venv` (word onsets bracket the gap; never anchor on ASR alone
  — it drifts up to ~1.7s in melismas).
- **Known tension — read before you split.** Bars captions are STRICTLY
  SEQUENTIAL (`transitions.sequential: true`): caption N must reach alpha 0
  before caption N+1 leaves 0, so a changeover needs the FULL fade-out plus the
  full fade-in inside the recited gap — ~1.9s at nominal (crossfade_s 0.95 x2;
  wipe_in_s/wipe_out_s 1.02 — card 1 enters on a wipe, the rest cross-fade).
  Real recitation rarely leaves that. `build_bars` then shrinks both fades
  proportionally down to `transitions.min_fade_s` (0.20) — except a wipe-out
  under `wipe_out_anchor: end`, which keeps its full length and instead eats the
  outgoing caption's tail — and if even that will not fit it prints
  `HARD CUT at changeover(s): Pn->Pn+1`.
  So: the more cards you cut, the snappier every swap becomes. Prefer FEWER,
  LONGER cards placed on real gaps over many cards on marginal ones.
- Splitting at a point that is NOT a true waqf is ALLOWED but degrades the
  result. at-tawbah-128-128 has one: the P1→P2 boundary sits in continuous
  recitation (the 50ms RMS envelope only troughs to −24 dB at 6.40/6.90 against
  −10 dB speech), so the swap was placed in the last dip before the word onset
  at 7.11. HOW TO NOTICE: the envelope minimum at your boundary is within ~15 dB
  of the speech level, and/or the mlx-whisper gap between the bracketing words
  is under ~0.35s. WHAT IT COSTS: fades get fitted down toward min_fade_s and
  the swap reads as a snap over held sound. Take it only when the alternative is
  an over-long line; note it in a clip.yaml comment as that clip did, and flag
  it to the user in the stage-4 handoff.
- `segment.cuts` is NOT supported on bars (build_render delegates to build_bars
  before the splice step). Do not write `cuts:` into a bars clip.yaml — it is
  silently ignored. Choose a segment that does not need dead air removed, or
  tighten `segment.start`/`end` instead.

### Stage 2 — Render agent (Opus)
- Run the EXISTING scripts with Homebrew python:
  `/opt/homebrew/bin/python3 scripts/build_render.py clips/<name>`
  (it calls `render_text.py` internally). Output: `clips/<name>/output/final.mp4`.
  **This entrypoint is the same for BOTH styles** — `build_render.build()` reads
  `style:` from clip.yaml and delegates to `build_bars.build()` (which calls
  `render_bars.build()`) when it is `bars`. Do not invoke `build_bars.py` /
  `render_bars.py` directly; the delegation is the supported path.
- Adapt CONFIGS (clip.yaml / style.yaml / bars.yaml), not the code — code changes
  only for genuinely new needs (N-phrase support, per-phrase medallions, and
  balanced English wrap are already in as of 2026-07-20 — see Constraints).
- Verify: `ffprobe` the output, VIEW extracted frames (Read the .jpg/.png), and
  read the loudnorm pass-2 summary. Confirm 1920x1080, ~30fps, correct duration.
- **bars**: confirm **1080x1920**, 30/1 fps, duration == segment length. Then
  read the stdout the scripts already print, which is the cheapest QA there is:
  - `render_bars.py OK` — `bar <target>/<drawn>` colour, `Arabic pt=` (must be
    96; a lower value means a line is too long, go back to stage 1 step 2), and
    one `barN.png+textN.png: n line(s) bars x x0..x1 (w …)` line per phrase —
    every `w` must be ≤ 540px (0.50 W) and the x-span must be centred on 324.
  - `RENDER OK ->` — geometry `1080x1920 band 1080x608@y656`, `wipe_target=bar`,
    `sequential, min fade 0.2s`, the per-phrase fade schedule, and any
    `HARD CUT at changeover(s)` line. A hard cut is a stage-1 timing problem;
    report it up rather than papering over it in bars.yaml.
  - Cached artefacts in `work/` (`snow_*.mp4`, `scrim_*.png`,
    `bar_color_sample.png`) are keyed by their parameters and are reused — deleting
    them only costs time, never correctness.

### Stage 3 — QA agent (model: fable)
- Adversarial, frame-by-frame compare against the account's own refs
  `style/refs/ig_*.mp4` (and `style/refs/frames/`).
- On any conflict, THE REELS BEAT THE WRITTEN SPEC.
- Verdict: SHIP / FIX_AND_RERENDER / ASK_USER, with parameter-level diffs
  (which style.yaml / clip.yaml key to change and to what).

#### Stage 3 — **bars** branch
Compare against **`style/refs2/`** — `ig_DNon3t8J9je.mp4` ("gold", 720x1280,
18.87s, 3 phrases) and `ig_DNrnY9b2EHe.mp4` ("rose", 55.03s, 9 phrases), plus
`style/refs2/frames/`. NOT `style/refs/` — that is the landscape style and every
comparison against it will be wrong. The refs are 720x1280, so scale our 1080
measurements by /1.5 (or theirs by x1.5) before comparing. The measured spec is
`style/refs2/STYLE2_SPEC.md`; on any conflict THE REELS BEAT THE WRITTEN SPEC.
Run these checks and report each PASS/FAIL with the number you measured:
1. **Bar-height ÷ glyph-ink ratio — the single number that most determines
   whether the style reads right.** The pill is a highlight band struck THROUGH
   the letter bodies, never a box enclosing them. Refs: bar h 29.0px (σ 1.0,
   row 6) against glyph ink 77–90px (row 27) => ink/bar **2.66–3.10** including
   marks. Measure OURS tashkeel-stripped (the honest comparison — see the
   overshoot trap in Constraints) with the stage-1 snippet: per line **2.4–2.8**,
   clip mean **2.55–2.70**. The ratio rises with point size, so BELOW ~2.4 means
   the type has been auto-shrunk (check `pt` — it must be 96; the fix is a
   shorter line in stage 1, never a bars.yaml edit); above ~2.9 the bar is
   swamped and reads as an underline rather than a highlight.
2. **Bar hugs the text width.** bar w == ink w + 54px (2 x pad_x 27); refs
   0.29–0.50 W, widest 362px@720 = **0.503 W**. FAIL any bar > 0.50 W. Neither
   edge is fixed — only the centre.
3. **Centre-anchor alignment.** Every bar centre on x = 324px (0.30 W); refs
   0.275 W rose / 0.317 W gold, ±8px jitter. Line 2 must NOT be indented
   (refs: 0 ±5px) and line 1 / line 2 must appear and leave SIMULTANEOUSLY
   (zero stagger, row 40).
4. **Glow falloff.** Excess outside the pill must decay as **exp(−d/28px@720)**
   — target ~51/36/25/13/8/5 DN at d = 5/10/20/40/60/80 px@720-equivalent (gold
   R, spec row 24), i.e. d40/d5 ≈ 0.27 and effectively zero past 80px (0.11 W).
   Glow hue == bar hue. Text halo is a SEPARATE tighter pass: σ 6–8px@720,
   ~40 DN at the glyph edge, warm near-white — NOT the bar hue.
5. **Letterbox purity — zero glow and zero particle spill.** The two black bands
   (0.3414 H each) must be bit-exact `#000000` apart from ±2 DN codec ringing,
   with high-frequency σ = 0.00 (refs measure exactly that; the picture interior
   is 1.1–1.6). Sample rows well inside both bands AND the two rows either side
   of the band edge. FX_RECIPE's own verdict: bleeding glow or specks into the
   bars is "the one thing that would instantly read as 'not the same style'".
6. **Caption no-overlap invariant.** Threshold near-white low-chroma pixel count
   per frame; no frame may carry two captions. Refs show a full clear of 17–22
   frames (0.57–0.73s) between phrases. Also check no bar pulse during a phrase
   (refs 0.97–1.01, < ±2%) and phrase durations in 2.7–8.6s.
7. **Band grade.** Band mean luma **0.15–0.32** (refs 0.319 gold / 0.149–0.173
   rose), fraction above 0.40 luma 0.12–0.27. Brighter than that and the glow
   washes the frame yellow.
8. **Bar colour** derived, not invented: H within ~5° of the clip's dominant
   hue, L = clamp(1.78 x mean L, 0.34, 0.48), S = clamp(2.2 x mean S, 0.22,
   0.60). If it reads wrong, the fix is `bar_color:` on THIS clip — never
   `bar.auto.*` in bars.yaml.
9. **Snow / heat sanity** (only if they look off): ~24 blobs/band above 40 DN
   and ~12 above 60 DN, neutral white (R=G=B within 3%), rising ~2.9–3.3
   px/frame on a 405-tall band (SNOW_SPEC §C — FX_RECIPE §D's particle figures
   are SUPERSEDED, do not use them). Heat: ~0.96px RMS bar-edge wander @720.
10. **Absences that are correct**: no English, no ayah medallion, no Ken-Burns
    zoom (< 1% scale drift), no film grain added. Do not raise these as defects.
- Verdict: SHIP / FIX_AND_RERENDER / ASK_USER, with parameter-level diffs
  (which bars.yaml / clip.yaml key to change and to what).

### Stage 4 — User review loop
- Show the user the clip / frames. They annotate screenshots with red markup.
- Translate each annotation into a concrete config change, re-render (stage 2),
  re-QA if warranted. Repeat until they approve.
- **bars**: the per-clip levers are `video_bg.crop` (framing), `bar_color`, the
  phrase split (`ar1`/`ar2` vs `ar`, and how many cards) and `segment.start/end`.
  A caption-position or type-size complaint is NOT a per-clip fix — the anchor
  and point size are style-wide; re-split the lines instead. Hand up any hard cut
  or non-waqf boundary you inherited from stage 1 so the user can judge it.

### Stage 5 — Export (after user approval only)
- Run `/opt/homebrew/bin/python3 scripts/export_reel.py <name>`. It:
  - stream-copy remuxes `output/final.mp4` → `reels/RECITER-SURAH-a0-a1.mp4`
    (e.g. `BADR-AL-TURKI-AHZAB-70-71.mp4`; reciter part from tags.yaml
    `reciter_short` if set, else `reciter`; surah name article-stripped,
    e.g. al-ahzab → AHZAB);
  - embeds MP4 metadata (title, artist=reciter, date, comment with verse ref +
    source YouTube URL + source segment timestamps) so the reel is
    self-describing forever;
  - applies the tags.yaml keyword tags + a red "Not Posted" (or green "Posted")
    Finder marker onto the reel FILE;
  - drops a Finder alias to the reel inside the clip folder.
- `reels/` is the permanent library. Once posted (`status.py post <name>` —
  flips tags.yaml + folder tag + reel marker green), the user may DELETE the
  clip folder by hand; the reel's own Finder marker then becomes the posted
  source of truth (`status.py post/unpost <REEL-NAME>` still works; `sync`
  never touches ownerless reels). Re-running export_reel.py is idempotent.
- **bars needs NOTHING different here** (verified against the script): the ffmpeg
  call is `-map_metadata -1 -c copy -movflags +faststart` — a pure stream-copy
  remux with no scaling, no filter and no resolution assumption anywhere in
  `export_reel.py` or `status.py`. A 1080x1920 final.mp4 exports byte-identical
  video to the reel. Same filename scheme, same metadata, same Finder markers.

## Constraints (hard technical facts — do not violate)
- **ffmpeg is a SLIM build**: NO drawtext / subtitles / libass. ALL text is
  transparent PNGs from Pillow, composited with
  `overlay enable='between(t,a,b)'` + `fade ...:alpha=1`. The scripts already do
  this; don't reach for drawtext.
- **Text rendering MUST use `/opt/homebrew/bin/python3`** — its Pillow needs
  RAQM (HarfBuzz+FriBiDi) for Arabic shaping. `render_text.py` hard-fails without
  raqm. NEVER pip-install Pillow into a venv for this.
  - **If raqm breaks** (`features.check('raqm')` is False, or PIL imports as an
    empty namespace / `cannot import name 'Image'`): a stock PyPI Pillow wheel
    for this Python (currently cp314) does NOT bundle raqm, and pip churn from a
    subagent (e.g. installing faster-whisper for ASR) can wipe the working
    install (learned al-ahzab-70-71). RESTORE with a source build linked to
    Homebrew libraqm — do NOT just `pip install Pillow`:
    ```
    brew install libraqm                       # pulls harfbuzz + fribidi
    rm -rf /opt/homebrew/lib/python3.*/site-packages/PIL   # clear stale namespace
    PKG_CONFIG_PATH=/opt/homebrew/lib/pkgconfig CPATH=/opt/homebrew/include:/opt/homebrew/include/freetype2 LIBRARY_PATH=/opt/homebrew/lib \
      /opt/homebrew/bin/python3 -m pip install --force-reinstall --no-cache-dir \
      --break-system-packages --no-binary :all: Pillow
    /opt/homebrew/bin/python3 -c "from PIL import features; print(features.check('raqm'))"  # must be True
    ```
- **Arabic mark stripping** (`_STRIP_MARKS` in `render_text.py`): KFGQPC Uthmanic
  Hafs v22 cannot RAQM-attach some small tajweed ANNOTATION marks, which then
  emit an ugly U+25CC dotted-circle fallback cluster; others just read as
  "random floating symbols" over the clean style. Stripped globally (letters +
  all pronunciation diacritics kept): **U+06DF** SMALL HIGH ROUNDED ZERO
  (silent-alif aid, e.g. ءَامَنُوا۟) and **U+06ED** SMALL LOW MEEM (iqlab aid,
  e.g. قَوْلًۭا). If a new clip shows stray circles/marks in the render, dump the
  phrase codepoints (`ord(c)` for c ≥ 0x06D0) and add the offender to the set.
- Binaries: `/opt/homebrew/bin/ffmpeg`, `/opt/homebrew/bin/ffprobe`.
- **clip.yaml strings: NO backslash escapes** (learned al-anam-76-77): the
  repo's minimal YAML loader (`render_text._coerce`) only strips outer quotes —
  `\"` renders as a literal backslash+quote in the video. Use curly quotes
  “ ” for quoted speech in `en` (matches ash-shuara-61-62 precedent).
- **N-phrase support** (generalized 2026-07-20, ash-shuara-61-62 run):
  `build_render.py` now loops the fade schedule / overlay chain over any
  number of phrases (assert is `>= 1`). Medallions are per-phrase via
  `med_suffix(i)` in `render_text.py`: a phrase gets its ayah medallion iff it
  COMPLETES its ayah (next phrase has a different ayah, or it's the last).
  English wrap is balanced min-raggedness (no orphan last words); Arabic
  auto-size measures every phrase INCLUDING medallions. Output is native
  landscape **1920x1080**, not the 1080x1920 canvas listed in style.yaml.
- **Fonts** (in `assets/fonts/`): `uthmanic_hafs_v22.ttf` (Arabic);
  `Albertus MT Lt Regular.ttf` (English — rendered ALL-CAPS at ONE uniform size,
  `smallcaps: false`). The ALLĀH macron is hand-drawn (U+0100 is tofu in this
  font). The ayah medallion is digits only — Uthmanic Hafs auto-encloses them in
  the rosette; a leading U+06DD adds a spurious second rosette.
- **Video-track modes** (`build_render.py`). TWO paths, chosen per clip:
  - **LIVE footage** (added al-ahzab-70-71): set `video_bg.mode: live` — the
    trimmed SOURCE footage IS the video track (audio + video from one input, no
    `scene_image`). This is actually CLOSER to true account style (the IG refs
    are live single-camera footage, not stills). Optional `video_bg.crop
    {x,y,w,h}` (source px, a 16:9 window) reframes the reciter; if the reciter
    is centered in-source, crop to move him to one side and to drop burned-in
    channel overlays (handle/logo). No unsharp (real footage). If no crop, the
    footage is cover-scaled to 1920x1080.
  - **STATIC still**: the older path — a `scene_image` still (reciter baked in)
    IS the video, source is AUDIO ONLY, optional `scene_crop` + mild unsharp.
- **Non-30fps sources are fine** (learned al-infitar-6-9): a 25fps source needs
  no special handling — the splice step writes `work/source_seg.mp4` with
  `-r 30`, so the cut/phrase remap math already happens on the 30fps timeline.
  Just ffprobe the output to confirm 30/1.
- **Mid-segment cuts** (`segment.cuts`, added al-ahzab-70-71): remove dead air
  (e.g. the breath between ayat) without a separate editor. List of
  `{start,end}` in CLIP-RELATIVE seconds on the UNCUT timeline; cuts MUST fall
  in verified true silence between phrases. build_render.py splices the kept
  sub-intervals into `work/source_seg.mp4` and shifts later phrase t0/t1 earlier
  by each preceding cut's length. Keep clip.yaml phrase times in the UNCUT
  timeline; the remap happens only at render. Hard cut (fine for a near-static
  shot in a pause); add an xfade only if a jump is visible.
- **Style anchors** (full detail in STYLE_SPEC.md / style.yaml): static
  background; text block on the side OPPOSITE the reciter, vertically ~centered;
  ~0.45s dissolves between phrase cards timed to recited pauses; ayah-end
  medallion ONLY when an ayah completes; whole-frame black fade 0.3s in / 0.5s
  out; audio NO added reverb, two-pass loudnorm to −14 LUFS (TP −1 dBTP), audio
  fades 0.3 / 0.5s.
- **BARS STYLE — hard facts** (added 2026-07-28, at-tawbah-128-128 is the one
  worked example; params in `templates/bars.yaml`, forensics in
  `style/refs2/STYLE2_SPEC.md`, effects in `style/refs2/FX_RECIPE.md`):
  - Selected ONLY by `style: bars` in clip.yaml. **Absent => default style**, so
    every pre-existing clip is untouched. `render_text.STYLE_TEMPLATES` maps
    `default -> templates/style.yaml`, `bars -> templates/bars.yaml`; an unknown
    value hard-exits. `render_text.build()` and `build_render.build()` both
    delegate to `render_bars` / `build_bars` when the style is not default.
  - Output **1080x1920**, with the footage as a 16:9 **1080x608 band at y=656**
    on pure black. 30fps. Two centre-anchored caption lines (or one), each on a
    hard-edged OPAQUE rounded pill (44px tall, radius = h/2, 2–3px AA, never
    blurred), Arabic only.
  - **Arabic only — no English translation.** `meta.arabic_only: true`.
  - **No ayah medallion in this style, by design.** AM_Thulth cannot typeset one
    and neither reference reel shows one. The default style always ends on a
    medallion, so expect someone to report its absence as a bug — it is not.
  - **Font `assets/fonts/AM_Thulth_Regular_0.1.ttf` renders codepoints missing
    from its cmap as ZERO-WIDTH NOTHING, not as tofu** — a dropped mark is
    invisible and silently changes the measured width. U+06DF is one such
    (already in `render_bars._STRIP_MARKS` with U+06ED). VALIDATE every new
    clip's text against the cmap before trusting a measurement: the stage-1
    snippet flags any codepoint that adds no ink between two tatweels. This face
    has no U+0100 macron and no medallion rosette — do not reach for either.
  - **Bar geometry is measured on TASHKEEL-STRIPPED text.** AM_Thulth's Thuluth
    diacritics overshoot the ascender by ~0.23 em (yMax 2409 vs hhea ascent
    1946), so a full-ink bbox or font metrics would place the pill far too high.
    `render_bars` anchors the bar to the baseline (`bar.baseline_below_center_px:
    12`) and clamps it into the tashkeel-stripped letter-body band. Any
    hand-measurement you do must strip marks the same way
    (`render_bars.strip_tashkeel`) or the numbers will not match.
  - **The pill is painted PRE-DARKENED.** `bar.color` is the colour it must READ
    AS in the finished frame; the glow/scan passes screen OVER it, so
    `render_bars.predraw_color` backs it off by `fx_screen_lift: 0.179` /
    `fx_sat_retain: 0.88` before drawing. Judge the colour in the final frame,
    never in `work/overlays/barN.png`.
  - **Bar colour is AUTO-DERIVED from the footage** (`bar.auto`, from the clip's
    own graded band). Per-clip override is `bar_color: "#RRGGBB"` in clip.yaml.
    **Never hand-tune `bar.auto.*`** — those coefficients (L x1.78 clamp
    0.34–0.48, S x2.2 clamp 0.22–0.60) were validated against BOTH references.
  - **One shared point size for the whole clip**: nominal 96pt, shrunk (down to
    min 60) only until the widest line fits `max_line_width_frac: 0.45`
    (486px). One long line therefore shrinks every card — split the card instead.
  - **Captions are strictly sequential** (`transitions.sequential: true`): never
    two on screen at once. Both fades of a changeover are fitted into the real
    recited gap, shrinking proportionally but never below `min_fade_s: 0.20`;
    when even that will not fit, `build_bars` prints `HARD CUT at changeover(s)`.
  - **`segment.cuts` does NOT work on bars** — `build_render.build()` delegates
    to `build_bars` BEFORE the splice step, so a `cuts:` list is silently
    ignored. Landscape clips are unaffected.
  - FX are confined strictly to the band and applied before the pad, so the
    letterbox stays bit-exact black. Heat wave is applied LAST over the composited
    band (footage + pills + glyphs shimmer together) — this is correct, not a bug.
  - The `work/` caches (`snow_*.mp4`, `scrim_*.png`, `bar_color_sample.png`) are
    keyed by their parameters; stale ones are harmless.
- **Style references are ONLY the account's own IG posts in `style/refs/`**
  (yt-dlp downloads them anonymously). NEVER use
  `~/Library/Mobile Documents/com~apple~CloudDocs/Clips/` as a style reference —
  different account, different style (its fonts are fine only when the user
  explicitly points to them). For the bars style the equivalent set is
  `style/refs2/` — never mix the two ref folders across styles.

## Adding a new clip — checklist
- Dir name: `clips/<surah-slug>-<firstAyah>-<lastAyah>/`
  (e.g. `at-tawbah-51-51`). Subdirs: `work/` (source.mp4, overlays/) and
  `output/` (final.mp4) are created by the scripts.
- Goes in **clip.yaml** (per-clip): source_url, reciter, scene_image +
  scene_crop, text.center_x/y_frac (text side opposite reciter), segment
  start/end, surah, ayah_range, phrases[] (timings + ar + en + ayah).
- Goes in **templates/style.yaml** (shared, fixed): fonts, colors, sizes,
  crossfade, shadow, grade/vignette, audio targets, overlays=none. Change here
  only for account-wide style shifts, not one clip.
- **Also write `clips/<name>/tags.yaml`** (added 2026-07-20): `surah`,
  `surah_number`, `ayah_range`, `reciter`, optional `reciter_short` (ONLY used
  for the reel filename, e.g. `Ibrahim bin Mudhish` → `IBRAHIM-BIN-MUDHISH-…`),
  `title`, `source_url`, a `tags` list (surah slug + reciter + place + theme
  keywords), and `posted: false` / `posted_at: ""`. While the clip folder
  exists this is the source of truth for tags + post status; after export +
  deletion the reel's own Finder marker takes over (see Stage 5). Then run
  `/opt/homebrew/bin/python3 scripts/status.py sync` to mirror the tags + a
  green "Posted" / red "Not Posted" marker onto the clip folder AND its
  exported reel. Mark posted with `scripts/status.py post <clip-or-reel-name>`
  (unpost/list/sync also). tags.yaml is read via render_text.load_yaml; Finder
  tags are written via the `xattr` CLI (macOS Python has no os.setxattr);
  status.py is also the shared library (SURAH names, reel_name/parse_reel,
  xattr helpers) imported by export_reel.py.

### …in the **bars** style — the diffs only
Everything above holds (same dir naming, same `work/`+`output/`, same tags.yaml,
same status.py/export_reel.py). Model on `clips/at-tawbah-128-128/`. Changes:
- **clip.yaml**: add `style: bars`. Keep source_url, reciter, video_bg (mode +
  crop), segment start/end, surah, ayah_range. Phrases carry `t0`/`t1`/`ayah`
  plus `ar1` (+ optional `ar2`) for a two-line card or `ar` for a single-line
  card. **No `en:`. No medallion. No `cuts:`.** Optional `bar_color: "#RRGGBB"`
  to override the auto-derived pill colour for this clip only.
- **Shared params live in `templates/bars.yaml`**, not `templates/style.yaml`.
  Change bars.yaml only for account-wide shifts of the bars style, never to fix
  one clip — and never `bar.auto.*` at all.
- **tags.yaml**: identical schema, plus a **`style-bars`** keyword in `tags` so
  the library is filterable by style after the clip folder is deleted.
- Before rendering, run the stage-1 line-measurement snippet and confirm pt=96,
  every barW ≤ 0.50 W, no MISSING codepoints.
- QA against `style/refs2/`, not `style/refs/`.

## Convergence (self-improvement — do this at the end of every run)

This skill already learns: the inline `(learned <clip>)` tags and the technical
facts throughout exist because past clips paid for them once. Keep that going — at
the end of every run, fold any NEW, durable learning back in before ending the turn.
Skip one-off per-clip content and anything already captured here.

Pick the destination by scope:

1. **A reusable pipeline/style/render lesson** (a timing or waqf-detection failure
   mode and its fix; a crop/composition rule for a new source type — live footage,
   burned-in overlays, heavy reverb; an ASR/silencedetect gotcha; a font/raqm/mark
   issue and its stray-codepoint fix): add it to the relevant place in THIS file —
   as a `(learned <clip-slug>)` note on the exact step it corrects, or a new bullet
   under **Constraints**. Use `Edit`. Terse, imperative, self-contained, and tag it
   with the clip that taught it (matches the existing style).

2. **A shared, account-wide style/param shift** (a value the account now consistently
   wants — margins, sizes, fade lengths, loudnorm target, medallion rule): change
   `templates/style.yaml`, NOT one clip.yaml — that's the shared source of truth for
   every future clip. **In a bars run the equivalent file is `templates/bars.yaml`**
   — and even then never `bar.auto.*` (reference-validated; per-clip colour goes in
   `bar_color:`). A lesson learned on one style does NOT transfer to the other:
   tag it with the style as well as the clip, and if it is bars-specific put it in
   the bars branch of the matching stage, not in the shared prose.

3. **A recurring user preference or correction** (they always want the head-tighten at
   onset ~0.07, a reciter-name spelling, a filename convention, a framing they keep
   re-asking for): if it's a workflow default, encode it here in the matching step;
   if it's account style, put it in `style.yaml`.

4. **A forced code change** (a genuinely new capability the scripts had to gain): note
   it under **Constraints** with the date + clip so the next run expects it — exactly
   as the existing `(added 2026-07-20)` / `(generalized …)` notes do.

Rules: one lesson per real insight, phrased so a future run applies it without the
original context. If nothing new was learned, write nothing. Grow the notes; never
delete an existing `(learned …)` tag or Constraint to make room.
