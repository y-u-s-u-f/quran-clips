---
name: make-post
description: >-
  Make a Quran recitation clip/reel from a YouTube URL or local video.
  Trigger on "make a post from <url>", "make a clip from this recitation",
  "turn this into a reel". Runs fetch -> (transcribe) -> config -> align ->
  crop -> generate. Styles: vertical | horizontal | bars.
---

# make-post — make a reel

Run the steps, author the YAML, fix what the output tells you. From repo root.
Unfamiliar machine: `./install.sh --check` first.

## Steps

```
python3 pipeline/fetch.py <url-or-path> [--name SLUG] [--proxy] [--timestamps A-B]
# find the span: captions.srt if fetch got one, else
python3 pipeline/transcribe.py sources/<id>
python3 pipeline/quran.py --search "<arabic>"     # -> surah:ayah
# write sources/<id>/<reel>.yaml   (see Config)
tools/align-venv/bin/python  pipeline/align.py    sources/<id>/<reel>.yaml
tools/render-venv/bin/python pipeline/crop.py     sources/<id>/<reel>.yaml --write --annotate /tmp/c.png
tools/render-venv/bin/python pipeline/generate.py sources/<id>/<reel>.yaml [--vertical]
ffmpeg -v error -i reels/<reel>.mp4 -f null -     # decode gate, always
python3 pipeline/publish.py reels/<reel>.mp4      # ONLY if asked
```

- Know the verses already → skip transcribe, name them in the config.
- `captions.srt` is the cheapest span finder: dedupe rolling windows (keep each
  block's last line). Timings are ±1–2s — enough to pick a span, never to `trim:`.
- Open `/tmp/c.png` and look, every time. crop.py's guards check its own numbers
  against each other, not whether it found a real face.
- Never pipe `generate.py` through `tail`: output is withheld until the process
  exits, so a healthy 15-minute render reads as hung. `grep --line-buffered`, or
  redirect to a file and read that.

## Config

`generate.py --print-schema` is authoritative. Unknown key = error.

```yaml
style: vertical                   # vertical | horizontal | bars
signature: null                   # omit/null = none; bars never burns one
surah: 78
ayah_start: 31
ayah_end: 34
reciter: "..."                    # Arabic, COPIED from the title or an earlier config
trim: [16.4, 48.0]                # see Fixing → trim
groups:
  - n_words: 3                    # MUST sum to the span's word count
    english: "..."                # vertical + horizontal; bars is Arabic-only
    line_split: 2                 # bars only; omit = auto
# crop:, x_offset:, face_bottom: are written by crop.py --write
# optional: suppress, nudge, verse_numbers, y_offset, arabic_font, english_font,
#   arabic_scale, english_scale, english_caps, vignette, dim, bar_color, fx
```

Requirements:

- **No Arabic typed by you, anywhere.** `n_words` slices the mushaf. Count with:

  ```
  tools/render-venv/bin/python -c "import sys; sys.path.insert(0,'pipeline'); import generate; \
    w=generate.spoken_words(generate.fetch_verses(57,16,16)); \
    print(len(w)); [print(i,x) for i,x in enumerate(w)]"
  ```

  Not `ayah()['ar'].split()` — it counts unspoken marks (57:16 is 28, not 30).
- Split groups at waqf; never span two ayat; don't open a card on a connective.
- `english:` from Saheeh, cut at clause boundaries.
- `suppress:` anything that is not recitation.
- Hand-write `crop:`/`x_offset:`/`face_bottom:` only if crop.py refused.

Per style: `vertical` (1080×1920) needs `crop:` + `face_bottom:` — generate
refuses a landscape source without a crop rather than centre-crop him blind.
`horizontal` and `bars` (1920×1080) need `x_offset:`. `--vertical` letterboxes a
1920×1080 render onto 1080×1920, leaving the picture about a third of the frame,
so bars' Arabic arrives nearer 120pt-equivalent; native `vertical` avoids that
trade. Safe to re-run through generate.

## Verify English against BOTH translations

`generate.py` prints each card's Arabic + English, then Saheeh + Taqi for every
verse. **Check every card against both.** Card English must cover the same clause
as its Arabic; where the editions agree on order, a contradiction means a
mis-split — fix `groups`/`english:` and re-run. Do not accept unverified.

## Fixing

**Trim / the opening word.** Open ON the first word. Auto-trim (omit `trim:`) is
only safe when the source is roughly the reel; on a long recording it returns
valid JSON while a trailing word is captured, and the only tell is a collapsed
score. So: rough span from captions/whisper → hand `trim:` → read the scores (low
mid-phrase madd is normal) → re-bound. Hand-set trim is never moved; align prints
the head gap and calls out >0.25s. Also hand-set for deliberate excerpts and
second takes. Haram audio has no true silence (~−35dB floor): find boundaries by
RMS envelope dips (0.4–0.9s at −27…−34dB against speech ~−11dB), not
`silencedetect`.

**Restarted phrase (ibtidāʾ).** It gets its own card: same words, separate group.
align.py corrects the onset from consecutive identical Whisper segments and prints
it. Whisper sometimes collapses a restart into one long word — scan align output
for duration outliers (3.1s beside 0.7–1.2s neighbours) before concluding there
was none. Check the card start against the envelope before reaching for `nudge:`
(0-based indices).

**crop.py boxed a shoulder.** A draped, hooded or turned head can box clean and
confident (fy=0.275) and still be wrong; bowed posture fails differently. Look at
the annotation. Hand-solve by measuring crown / body / congregation / graphics over
several frames: head centre at **0.771 of the window**, crown kept in frame. For
`x_offset`, match the outer margins, or centre him in the free space if he runs off
the edge — `(f-0.5)*1920`. For `face_bottom`, where his head box ends as a fraction
of canvas height.

**Bars caption lines.** Budget is 213pt with an 864px ink cap → 2–3 words a line.
If the reported pt is under nominal, one long line is dragging the whole reel down.
Lines should land within ~30px of each other; >60px is visible. Let auto-balance
try first, then fix the card boundary (`n_words`) before `line_split` — a
`line_split` past the cap re-wraps silently, and there is no kashida. Single line
only at a waqf or ayah end. Take ~3pt of loss over a broken clause; some cards
won't balance, so pick the closer break and stop.

**Bars timing previews.** `fx: {heat: false}` renders ~27% cheaper, but never
judge the LOOK without the full stack.

**fetch: "Sign in to confirm you're not a bot".** It is per exit IP and per player
client, and fetch walks the clients itself. Read the printed `client` and `WxH`
before blaming the proxy pool: a client without a GVS PO token exits 0 at 640x360,
a silent quality failure rather than an error. Pool + `--proxy` goes static
residential → datacentre → fail; rotating exits cannot download. Prefer a full
download plus `trim:` over `--timestamps` through an authenticated proxy.

## Rules

- No model-typed Arabic — it comes from `quran.py` only.
- Never pip-install into a venv; `./install.sh` only.
- Trust fetch's stub gate; don't bypass it.
- Decode-check before calling a render done.
- Do not `open` artefacts; report the path.
- Never publish unless asked (`--caption-only` is safe for review).
- Don't delete or overwrite a different reel; the filename is its identity.
