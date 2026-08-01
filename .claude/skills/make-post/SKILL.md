---
name: make-post
description: >-
  Make a Quran recitation clip/reel from a YouTube URL for the Quran clipping
  account. Trigger on "make a post from <youtube url>", "make a clip from this
  recitation", "turn this into a reel". Drives the scripted ~/quran-clips
  pipeline: `qc source add` -> `qc propose` -> `qc author` -> `qc check` ->
  `qc render` -> `qc export`. TWO styles: `bars` (vertical 9:16, Arabic-only
  Thuluth captions on coloured pills — the default of the CLI) and `default`
  (landscape 1920x1080 with English), selected with `--style`.
---

# make-post — Quran clip pipeline

The pipeline is SCRIPTED. Your job is to run the commands, read what they print,
and make the few judgement calls the tools hand up. Do not re-derive by hand
anything a command already does.

Repo root `~/quran-clips`. All commands below are run from there.
`./bin/qc` and `./bin/quran-clips` are the same CLI under two names; scripts under
`scripts/` must be invoked with the render interpreter explicitly.
Either name with no arguments prints the authoritative usage — read it if a flag
here looks stale.

**Run `./bin/qc doctor` FIRST on an unfamiliar machine, and whenever anything
shells out unexpectedly.** It prints where ffmpeg/ffprobe/yt-dlp/claude and the
three interpreters resolved from, which ASR backend this host selects, the egress
plan, and which commands anything missing would block. That is one second and it
replaces guessing; before it existed a misconfigured host announced itself as a
traceback several minutes into a render.

Machine-level settings live in `.env` (see `.env.example`): tool paths, the ASR
model, and the proxy pool. Nothing there affects a rendered pixel — style
geometry stays in the committed `templates/*.yaml`.

## Styles
- `bars` — 1080x1920, letterboxed footage band, Arabic-only captions on pills.
  Params `templates/bars.yaml`. This is the CLI default for `--style`.
- `default` — 1920x1080 landscape, Arabic + English. Params `templates/style.yaml`.
- The style is fixed per clip by the `style:` key in clip.yaml, and it also keys
  the cached crop, so pass the same `--style` to every command in a run.
- A bare "make a post from <url>" means: pick the style the user last used /
  asked for; if nothing indicates otherwise, `bars`. Do not stall on a question.

## End-to-end sequence

```
./bin/qc source add <youtube-url>            # yt-dlp -> sources/<id>.mp4 + subs + sources/meta/<id>.yaml
./bin/qc locate <video_id>                   # which surah/ayat is recited (uses the auto-captions)
./bin/qc crop <video_id> --style bars --write # solve the 16:9 framing ONCE per source, cache it
./bin/qc propose <video_id> --style bars      # ranked candidate windows; show these to the user
./bin/qc author <video_id> 9:128 <start> <end> --style bars -o clips/<name>
./bin/qc check clips/<name>                   # config-time assertions, ~1s. MUST pass before rendering
./bin/qc render clips/<name>                  # -> clips/<name>/output/final.mp4
./bin/qc check --output clips/<name>          # geometry / letterbox purity / loudness of the finished file
./bin/qc export clips/<name>                  # -> reels/RECITER-SURAH-a-b.mp4 + Finder tags
tools/render-venv/bin/python scripts/status.py post <clip-or-reel-name>   # after it is actually posted
```

What each one does:
- **`source add`** downloads ≤1080p video + Arabic auto-captions into `sources/`
  (the durable cache, shared across clips). `--no-video` for audio/meta only.
  Re-running is cheap; never copy a source into a clip folder.
- **`locate`** matches the auto-captions against the mushaf and prints the ayah
  range. `-v` for detail. `./bin/qc ayah 9:128-129` prints the mushaf text.
- **`crop`** solves the reciter/caption framing from sampled frames and caches it
  in `sources/meta/<id>.yaml`, keyed by style. `--side left|right`,
  `--face X,Y[,W]`, `--exclude X,Y,W,H` (for an intermittent banner),
  `--annotate P` / `--sheet P` to inspect, `--write` to cache. `qc author`
  refuses to reuse a crop solved for the OTHER style — solve it per style.
  Needs `tools/author-venv`.
  **Placement is ONE rule for both styles** (`qc.author.crop.targets`, rewritten
  2026-07-30 — the two old per-style rules were the same rule twice):
  - *Vertical:* **face centre at 0.275 H.** Soft cost, 0.24–0.34 warning band.
    Centring at 0.50 H was tried on 2026-07-30 and reverted on 2026-07-31 —
    it broke every upright reciter in `reels/`.
    **The face is a PROXY for headroom, and it fails on a reciter who bows.**
    A bowed reciter's face sits low inside a tall head, so pinning the face
    crops his crown off the top of the band. Always check the top of his head
    is in frame; do not trust the number alone. There is no automatic fix —
    anchoring on the crown was tried and abandoned (the motion blob's top edge
    lands *above* the shipped window on all three cached sources, and YuNet's
    face height varies 140–465 px across comparable shots, so no
    `crown = face_top − k × face_h` estimate holds). **A bowed reciter is a
    hand-solve** in that clip's own `video_bg.crop`, with the reasoning in a
    comment — see the four Salih al-Ansari clips (`-GZR1C9Acd4`).
  - *Horizontal:* **equal gaps.** The frame holds two blocks, the reciter and
    the caption column; every gap between them and the frame edges is the same
    width — three gaps when he fits with air on his outer side, two when a wide
    shot makes him bleed off it. The caption lands centred in the gap on its own
    side. The caption column's width is the style's own: 0.45 W for bars
    (`text.max_line_width_frac`), 0.504 W for default. Caption goes on the side
    he FACES. `bars`'s old fixed "anchor 0.30 / clear 0.60" is this rule's
    answer for one particular reciter width — it now adapts instead.
  **`! camera is NOT fixed` is not a warning to wave through.** It means the
  source cuts between angles, and ONE cached crop cannot be right for all of
  them: the solve lands on the dominant angle, so a clip whose window sits on a
  minority angle silently violates the placement rule above. `crop` samples the
  whole source and has no `--range`, so verify the framing on frames from the
  CLIP'S OWN window and, when it differs, override `video_bg.crop` per clip in
  clip.yaml — never re-cache the source, which is shared with every other clip
  cut from it.
- **`propose`** ranks clip-worthy windows and asks a model (in English, on ayah
  NUMBERS only) whether each stands alone. `-n N`, `--range MM:SS-MM:SS` or
  `--verses S:A-B` to skip proposing and score a chosen window, `--no-judge`,
  `--json`. Give the user the top candidates and let them pick.
- **`author`** aligns the window against the mushaf, measures the envelope, sets
  the head, splits the cards at real waqf gaps, breaks the lines, fills the
  default style's `en:` from the committed Sahih International edition (cut to
  match the cards by the same indices-only judge), and writes `clip.yaml` +
  `tags.yaml`. When the judge is unavailable the `en:` carries a comment naming
  what cut it instead: `WORD-ANCHORED word split` used the committed word-by-word
  glosses, so the seam is aimed at the card's own last word and wants only a
  glance; `PROPORTIONAL word split -- CHECK BY HAND` is arithmetic and needs an
  eye.
  `-o DIR`, `--like CLIP` (carry reciter/crop/url off
  a shipped clip), `-n` to print without writing. It prints warnings on stderr —
  `! restart detected` (ibtidāʾ) and `! Pn->Pn+1 is NOT a true waqf` — pass those
  up to the user; they are judgement calls, not bugs.
  **Two things `author` gets wrong often enough to check every time:**
  *Held words — at EVERY boundary, not just the clip's end.* An ASR word edge
  lands where the word is *identified*, not where the reciter stops: a held
  final madd bleeds into the NEXT word's ASR span, so the raw edge sits
  seconds before the real seam. The tell: a short word carrying an implausibly
  long span right after a madd-final word (on as-sajdah-10-11, قُلْ "spanned"
  1.76s because كافرون was held 1.55s into it — the swap fired mid-hold and
  the owner heard it instantly). At the clip's end this cuts the clip
  mid-hold: read the envelope past the nominal end and move `segment.end`
  into the real trough. At interior ayah boundaries the emitter now rescues
  this itself (`emit._bleed_dip`, 2026-07-31: a deep dip inside the incoming
  word's span beats the word edge) — but a LEGATO junction, where the reciter
  runs one word into the next with no envelope drop at all, has no measurable
  seam; author's number there is an estimate and the emitted comment says so.
  Those are the boundaries to confirm by ear before export.
  *Line breaks.* The breaker optimises for the width cap, not for balance
  between the two lines of a card — see "Caption line balance" below.
- **`check --output` now DECODES the file, not just its header.** Geometry, fps,
  duration and faststart all read from container metadata, which survives a
  corrupted bitstream intact: a file whose payload was interleaved by a second
  writer reports perfect metadata and is unplayable. The decode pass plus a
  decoded-frame count is what catches it. If it fires, the usual cause is two
  writers on one output path — re-render to a path nothing else owns rather than
  re-running the same command.
- **`check`** is the QA stage. Eleven assertions: schema (unknown key = error),
  phrase/cut ordering, glyph coverage, Arabic == mushaf text (waqf signs and
  U+0640 tatweel stripped first — everything else must match exactly), no card spanning
  two ayat, bars caption geometry, no hard-cut changeover, English present/absent
  per style, fx names, and that the clip opens on the first word of the ayah it
  claims. Never render over a failing check.
- **`render`** writes `output/final.mp4` (~6 min for 24s of bars). `--preview`
  writes `output/preview.mp4` instead: half canvas, no heat/snow/glow/scan,
  ≤30s — use it for TIMING and LAYOUT only, never to judge the look.
  `./bin/qc frames <clip> --at 0,6,12,18 [-o DIR]` writes full-fidelity stills.
  (preview + frames are implemented for `bars` only.)
- **`export`** stream-copy remuxes to `reels/RECITER-SURAH-a0-a1.mp4`, embeds
  title/artist/date/source metadata, applies the tags.yaml keywords and a red
  "Not Posted" Finder marker, and drops an alias in the clip folder. Idempotent;
  refuses a preview render. `reels/` is the permanent library — once posted, the
  clip folder is disposable.
- **`status.py`** `list` / `post <name>` / `unpost <name>` flip tags.yaml + the
  Finder markers on both the clip folder and the reel. `sync` pulls the other
  way — the green/red Finder marker is truth, tags.yaml `posted:` is updated to
  match; it never demotes (reports conflicts instead). `sync --to-finder` is the
  old tags.yaml -> Finder direction, only for a fresh checkout with no xattrs.

Fixing a clip: edit `clips/<name>/clip.yaml` (segment start/end, `segment.cuts`,
the card split, `bar_color`), re-run `qc check`, re-render. Per-clip problems get
per-clip fixes; only an account-wide shift touches `templates/*.yaml`, and
`bar.auto.*` is reference-validated — never hand-tune it.

**Moving a card boundary by hand: the new time must be MEASURED, never
copied.** Raw Whisper word edges are not boundaries (held finals bleed into
the next word's span — see above), and eyeballed times drift. The recipe:
re-run `qc author <id> <s>:<a>-<b> <start> <end> -n` on the clip's own window
(the ASR is cached under `sources/_align/`, so this is fast) and read the
boundary times and per-edge comments it emits — they are envelope-snapped.
If your grouping differs from author's, the emitted numbers still give you
every dip; put the swap at the dip's swap-in (`t0`), which is just before the
next word's attack. A boundary you cannot back with a dip is an ear call —
say so in the yaml comment instead of writing a confident number.

## Caption line balance (bars)

`qc check` only asserts the width cap and the pill ratio. It will happily pass a
card whose lines are 169px and 411px, and that reads as broken. The owner's
standard, in priority order:

1. **The two lines of a card should be close in width.** A shipped example the
   owner called good: `قُلْ يَتَوَفَّىٰكُم مَّلَكُ` / `ٱلْمَوْتِ ٱلَّذِى وُكِّلَ` at 370 / 380px.
   Aim inside ~30px; over ~60px is visible.
2. **Fix it by moving the CARD boundary first, the line break second, kashida
   last.** Searching the whole ayah's card partitions usually finds a layout that
   is already balanced and needs almost no stretching — much better than
   stretching a bad split into shape.
3. **Kashida (U+0640 `ـ`) is approved as a touch-up**, with hard limits learned
   the painful way:
   - **max ~6 per line.** More and it stops reading as calligraphic elongation.
   - **never between `ل` and an alif form** (`ا أ إ آ ٱ`) — lam-alef is a required
     ligature and a kashida splits it into two glyphs. This is the failure the
     owner spotted first.
   - **never after a letter that does not join leftward** (`ا أ إ آ د ذ ر ز و ؤ ء
     ة ى ٱ`) — the tatweel is left dangling as a stray dash.
   - `qc.check.strict_ar` and `qc.quran.normalize()` both strip U+0640 before
     comparing, so a widened line still matches the mushaf. It is layout, not
     text — and it is the ONLY thing a model may add to Arabic.
4. **Single-line cards: avoid, except where the card closes on a waqf or the end
   of the ayah** — there they are encouraged, and a long held final word is
   exactly the case for one wide pill. Allowed elsewhere for meaning, just not
   preferred.
5. **Consecutive cards should read as connected meaning** — no awkward starts.
   `بِكُمْ` opening a card is the kind of seam to avoid. Least important of the five.

Measure with the same machinery `render_bars.layout()` uses — `bbox_ls(line,
truetype(text.font, text.nominal_pt))`, cap `text.max_line_width_frac * 1080`
= 486px — not by eye and not by counting letters.

A card can only be a single line if its ink is tall enough for the pill: the
pill-ratio assertion fails a line with no ascender (`تُرْجَعُونَ` alone measures
1.55 against a 1.8 floor). Group it with a word carrying a `ك`/`ل`/`ا`.

## Egress: downloads go through a proxy pool on a cloud host

`qc source add` is the ONLY command that leaves the machine. On a cloud host
YouTube bot-checks the datacentre IP outright ("Sign in to confirm you're not a
bot"), so configure the pool in `.env` and let the CLI escalate:

    QC_PROXY_STATIC=user:pass@host:port,...      # five static residential exits
    QC_PROXY_DATACENTER=user:pass@host:port,...  # fallback tier
    QC_PROXY_ENABLED=auto                        # auto | always | never

Order is fixed: **static residential -> datacentre -> fail.** No direct attempt is
made once a pool is configured. `qc doctor` prints the plan; passwords are
redacted in every log line and error.

Three things worth knowing before debugging a failed download, all measured:

- **A rotating proxy cannot download.** yt-dlp resolves a signed googlevideo URL
  that embeds the resolving exit IP, so a rotating exit 403s the media fetch. The
  pool is sticky per video id for exactly this reason. Rotating exits are fine for
  small metadata calls and nothing else.
- **The bot check fires at the player-API stage, before any bytes move, and burns
  per exit IP.** Retrying one endpoint harder makes it worse; moving to the next
  endpoint is what recovers. Sweeping
  `--extractor-args youtube:player_client=<tv|web_safari|ios|mweb|android_vr>`
  helps; repeated attempts on one exit do not. Keep a couple of non-US exits in
  reserve — they often still resolve when the primary ones are burned.
- **`--download-sections` needs the relay.** It always routes the range fetch
  through a child ffmpeg, and ffmpeg cannot CONNECT-tunnel https through an
  authenticated proxy: it reports `402 Payment Required`, which looks like a
  billing failure and is not (curl on the same endpoint and URL returns 206).
  `qc.relay` serves an unauthenticated local port that does the upstream CONNECT,
  which is what keeps range downloads working. Do not "fix" a stalled range fetch
  by exporting `http_proxy` — ffmpeg ignores it for https — and do not add
  `--force-keyframes-at-cuts`, which produced a 262-byte stub over a proxy.

**Never accept a download because the file exists.** A stalled or stub fetch
leaves a file that is present and worthless, so gate on real size AND a probeable
duration before treating a source as usable.

## Regression harness
`scripts/golden.py` freezes, for four golden clips (bars, default, 1-cut and
2-cut splice), the exact ffmpeg argv + filtergraph, the md5 of every intermediate
layer, and the md5 of the final encode. Any change to render code must leave them
byte-identical.

```
tools/render-venv/bin/python scripts/golden.py check --all        # tier 1: env + argv + filtergraph
tools/render-venv/bin/python scripts/golden.py check --layers <clip>  # tier 2: + regenerate layers
tools/render-venv/bin/python scripts/golden.py check --full <clip>    # tier 3: + full render md5
tools/render-venv/bin/python scripts/golden.py bless <clip> [--full]  # re-record an INTENDED change
tools/render-venv/bin/python scripts/golden.py env                    # ffmpeg/python build report
tools/render-venv/bin/python -m unittest discover -s tests            # fx scalar pins, milliseconds
```
Tiers 2 and 3 never write into a clip's `output/`. Byte-identity is coupled to
the ffmpeg build; the checker gates on `ffmpeg -version` first, so an upgrade
reports "golden invalid" rather than a fake regression.

## Hard rules
- **NEVER pip-install into `/opt/homebrew/bin/python3`.** A past install there
  destroyed the RAQM-enabled Pillow that does Arabic shaping, and it is not
  reproducible from PyPI. Use the venvs: `tools/render-venv` (rendering + all
  `scripts/`; `--system-site-packages`, PyYAML only), `tools/asr-venv`
  (whisper), `tools/author-venv` (opencv/numpy for `qc crop`). `tools/` is
  gitignored, so a fresh clone recreates them from `requirements/`.
  The interpreter is no longer hardcoded — it resolves per machine and each venv
  is overridable (`QC_RENDER_PYTHON`, `QC_AUTHOR_PYTHON`, `QC_ASR_PYTHON`), and
  falls back to the running python when `tools/<n>-venv` is absent. The RULE is
  unchanged and is about dependency isolation, not about one absolute path:
  whisper and opencv must never resolve into the render interpreter.
- **The ASR backend is per machine.** `mlx` (Apple silicon, the reference backend
  the committed timings came from) or `faster` (faster-whisper, everywhere else),
  auto-selected, overridable with `QC_ASR_BACKEND`. **Switching backends re-times
  a clip** — the two runtimes do not emit identical word boundaries, so cut points
  move. Caches record which backend wrote them and warn rather than silently
  re-cutting a reviewed clip. If you see that warning on a clip you are revising,
  delete the cache and re-derive, or keep the original backend.
- **NEVER `golden.py bless` to silence a failing golden.** A failure means
  something broke. Diagnose it, or stop and report. Bless only re-records a
  change you deliberately made and understand.
- **No model ever retypes Arabic.** Uthmani codepoints (madda alif, small high
  marks, superscript alif) corrupt on retyping, and so does any summarising
  fetch. Arabic comes from `qc.quran` / `qc ayah`, copied byte-for-byte. LLM
  judges return indices and numbers only — the line-breaker answers with a
  single integer, the coherence judge with ayah numbers. Build caption lines by
  SLICING the word list from `qc.quran` by index, never by typing the words out.
  The one and only character a model may insert is U+0640 tatweel, under the
  rules in "Caption line balance" — it is stripped before every comparison, so
  it changes layout and nothing else.
- **A text card must never span two ayat.** `qc author` cuts at every ayah
  boundary unconditionally, before the energy envelope gets a say, and
  `qc check`'s `ayah-span` assertion fails any clip that straddles one. A card
  may show part of an ayah or a whole ayah — never words from two.
- **Do not `open` produced artefacts.** Build the file, report the path.
- **Do not touch `reels/` or `clips/at-tawbah-128-128/output/final.mp4`** without
  explicit intent — the first is the permanent library, the second is a golden.
- ffmpeg here is a SLIM build: no drawtext/subtitles/libass. All text is Pillow
  PNGs composited with `overlay` + `fade:alpha=1`. Don't reach for drawtext.

## Reference material (read only when a question actually needs it)
`style/STYLE_SPEC.md` + `templates/style.yaml` (default style);
`style/refs2/STYLE2_SPEC.md`, `FX_RECIPE.md`, `SNOW_SPEC.md` + `templates/bars.yaml`
(bars). Style references are ONLY the account's own IG posts: `style/refs/` for
default, `style/refs2/` for bars — never mix them, and never use the iCloud
`Clips/` folder. On any conflict, the reels beat the written spec.
