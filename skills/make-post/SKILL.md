---
name: make-post
description: >-
  Make a Quran recitation clip/reel from a YouTube URL or local video.
  Trigger on "make a post from <url>", "make a clip from this recitation",
  "turn this into a reel". Drives pipeline/: fetch -> (transcribe) -> config
  -> align -> crop -> generate. Styles: vertical | horizontal | bars.
---

# make-post — Quran reel pipeline

Run the scripts; author the YAML; make the judgement calls below. Do not
re-derive what a script already does. From repo root. On an unfamiliar
machine: `./install.sh --check` first. Reference: `pipeline/README.md`.

## Sequence

```
python3 pipeline/fetch.py <url-or-path> [--name SLUG] [--proxy] [--timestamps A-B]
# captions.srt first if present (cheapest span finder) — then, only if needed:
python3 pipeline/transcribe.py sources/<id>
python3 pipeline/quran.py --search "<arabic>" ; python3 pipeline/quran.py s:a-b
# write sources/<id>/<reel>.yaml
tools/align-venv/bin/python  pipeline/align.py    sources/<id>/<reel>.yaml
tools/render-venv/bin/python pipeline/crop.py     sources/<id>/<reel>.yaml --write --annotate /tmp/c.png
tools/render-venv/bin/python pipeline/generate.py sources/<id>/<reel>.yaml [--vertical]
ffmpeg -v error -i reels/<reel>.mp4 -f null -     # decode gate
python3 pipeline/publish.py reels/<reel>.mp4      # ONLY if asked
```

Know the verses → skip transcribe. Name `surah`/`ayah_start`/`ayah_end`.

- **fetch** — `sources/<id>/`, ≤30fps at intake, AAC guaranteed, stub gate.
  Keep existing `source.mp4`. If `captions.srt` exists, dedupe rolling
  windows (keep each block's last line) before transcribing — timings are
  loose (±1–2s), good enough to choose a span, never to `trim:`.
- **transcribe** — `whisper.json` + `.srt`. Trust mushaf for WHAT; ASR for
  WHERE.
- **quran.py** — only Arabic/translations. `--search` / `s:a-b` / `--words`.
- **align** — CTC of known mushaf → `<reel>.align.json`. Omit `trim:` only
  when source ≈ reel (long sources misfire quietly — see Trim).
- **crop** (every style) — → `crop:` plus `x_offset:` (bars, horizontal) or
  `face_bottom:` (vertical). Column styles use the equal-gap rule; vertical
  centres him and reports where his head box ENDS. Estimate; refuses on no
  face / off-frame caption / no room under his chin, and treats an empty shot
  as a centred window with no anchor key. **Always `--annotate` and look**
  (see below). Local `claude -p`; cached in `crop.json`.
- **generate** — validates, renders. Read timing line, verification block,
  bars bar-widths. Never pipe it through `tail`: output is withheld until the
  process exits, so a healthy 15-minute render reads as hung. `grep
  --line-buffered`, or redirect to a file and read that.

## Verify English against BOTH translations

`generate.py` prints each card's Arabic + English, then Saheeh + Taqi for
every verse. **Check every card against both.** Card English must cover the
same clause as its Arabic; where the editions agree on order, contradiction
means a mis-split — fix `groups`/`english:`, re-run. Do not accept unverified.

## Config you author

`generate.py --print-schema` is authoritative.

```yaml
style: vertical                   # or horizontal | bars
signature: null                   # omit/null = none; bars NEVER burns one
surah: 78
ayah_start: 31
ayah_end: 34
reciter: "..."                    # Arabic, COPIED from title/earlier hashtag
trim: [16.4, 48.0]                # see Trim
groups:
  - n_words: 3                    # MUST sum to span word count
    english: "..."                # vertical + horizontal
    line_split: 2                 # bars; omit = auto
```

**No Arabic in configs** — `n_words` slices mushaf. Count with:

```
tools/render-venv/bin/python -c "import sys; sys.path.insert(0,'pipeline'); import generate; \
  w=generate.spoken_words(generate.fetch_verses(57,16,16)); \
  print(len(w)); [print(i,x) for i,x in enumerate(w)]"
```

Not `ayah()['ar'].split()` (counts unspoken marks; 57:16 is 28, not 30).

Judgement: split at waqf; never span two ayat; avoid opening on a connective;
`suppress` non-recitation.

## Trim

Open ON the first word. `align.py` auto-trim (no `trim:`): 0.12s before
onset, clamped to the waqf; prints `head: …`. Hand-set `trim:` is never
moved; >0.25s head gap is called out. Tail keeps 0.30s.

**Auto-trim only when source ≈ reel.** On a long multi-surah recording it
returns valid JSON while a trailing word is captured downstream — only tell
is a collapsed score. Then: pick rough span from captions/whisper → hand
`trim:` → read scores (low mid-phrase madd is normal) → re-bound if needed.
Also hand-set for deliberate excerpts / second takes.

Haram audio has no true silence (~−35dB floor). Use RMS envelope dips
(0.4–0.9s to −27…−34dB vs speech ~−11dB), not `silencedetect`.

## Ibtidāʾ restarts

Restarted phrase → **its own card** (same words, separate group). `align.py`
corrects onset from consecutive identical Whisper segments and prints it.
Whisper may collapse into one long word — scan align output for duration
outliers (e.g. 3.1s vs 0.7–1.2s neighbours) before assuming no restart.
Check card start vs envelope before `nudge:` (0-based indices).

## When crop.py cannot see a face

Guards check the model's numbers against each other — not whether a face
exists. A draped/hooded/turned head can box a shoulder and look clean
(high confidence, fy=0.275). **`--annotate` and look.** Bowed posture is a
different failure. Hand-solve: measure crown/body/congregation/graphics over
several frames; head centre at **0.771 of the window**; keep crown in frame.
`x_offset`: match outer margins, or centre in free space if he runs off the
edge — `(f-0.5)*1920` for bars and horizontal. For vertical, hand-set
`face_bottom` to where his head box ends as a fraction of the canvas height.

## Caption line balance (bars)

Budget: 213pt, 864px ink cap → **2–3 words/line**. If reported pt < nominal,
one line is dragging the reel down.

1. Lines within ~30px width; >60px visible. Auto-balance first.
2. Fix card boundary (`n_words`) before `line_split`. No kashida.
3. Single-line only at waqf/ayah end; at 120pt three words rarely fit.
4. `line_split` past the cap re-wraps silently.
5. One over-cap line shrinks the whole reel via `fit_pt` — re-cut first;
   accept ~3pt loss over a broken clause.
6. Some cards won't balance — pick the closer break, note why, stop.

## Styles

All 30fps, fixed size; a weak source is upscaled, never delivered small.

- **vertical** — 1080×1920; block centred horizontally, hung under his chin
  (`face_bottom`). A landscape source NEEDS a `crop:` — generate refuses
  without one rather than centre-cropping him blind.
- **horizontal** — 1920×1080; block centred vertically, column opposite him
  (`x_offset`). Same renderer and look as vertical.
- **bars** — 1920×1080 pills + FX, no letterbox, no signature ever.
  `fx: {heat: false}` for timing (~27% cheaper); never judge LOOK without the
  full stack. Optional `bar_color:`. A source lit darker than the reference
  reels lands under the grade's own 0.15-0.32 mean-luma target (measured: a
  Dubai taraweeh sat at 0.05) — lift THAT config with
  `grade: {brightness: -0.12, gamma: 0.95}` rather than the constant in
  `render_bars.py`, which would drag every in-range reel out the bright side.
  The grade also feeds the pill-colour derivation, so expect the bar hue to
  move with it. Heat maps live in `tools/cache/heat/`
  and any map at least as long as the reel is used as-is, so span length
  costs nothing until a reel outruns every cached map — 60s covers what we
  cut. Only then does perlin run (~9.3s of wall per second of map, per axis),
  and it announces itself before the render.
- **`--vertical`** (any style) letterboxes the finished 1920×1080 onto
  1080×1920: 1080×608 of picture, about a third of the frame, so bars' 213pt
  Arabic arrives on the phone nearer 120pt-equivalent. The native `vertical`
  style is what avoids that trade. Safe to re-run through generate, which
  renders 1920×1080 fresh each time; `pipeline/letterbox.py` on its own
  applied twice shrinks the picture to a postage stamp.
- `english:` from Saheeh, cut at clauses, verify vs both editions.
- Look knobs (vertical + horizontal only): `arabic_font`, `english_font`,
  `arabic_scale`, `english_scale`, `english_caps`, `vignette`, `dim`.
- `x_offset`/`y_offset` nudge the solved anchor. Signature always centred
  horizontally (`signature_offset` is vertical only).

## Egress (cloud)

Only `fetch.py` leaves the machine. `.env` pool + `--proxy`: static
residential → datacentre → fail. Rotating exits cannot download. Prefer
full download + `trim` over `--timestamps` through an authenticated proxy.

## Hard rules

- No model-typed Arabic — from `quran.py` only.
- `signature` optional (omit = none); bars ignores it.
- Never pip-install into the render venv — `./install.sh` only.
- Trust fetch's stub gate; don't bypass.
- Decode-check before calling a render done.
- Do not `open` artefacts; report the path.
- Never publish unless asked (`--caption-only` is safe for review). A reel
  that posted is tagged green in Finder; `--draft` and `--caption-only` are not.
- Don't delete/overwrite a different reel; filename is identity.
- `legacy/` is archived — never run it here.
