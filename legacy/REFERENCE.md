# Original reel pipeline: configs and measured reference values

Archive of the **first-generation** Qur'an reel implementation: the standalone
`quran_reel_pipeline.py` script and its per-clip JSON configs, as used to produce
the eight reels in the parent Drive folder.

Superseded by the `quran-clips` package (`y-u-s-u-f/quran-clips`), which solves the
same problems with a different and better-verified schema. Kept because the
measured values below were expensive to derive and are not reproducible from the
new package's configs.

Uploaded 2026-07-31.

## What is here

- `configs/*.json` — 14 per-clip configs, one per reel plus iteration variants.
- `SKILL.md` — the operating procedure, as it stands after consolidation.

## The eight delivered reels

| config | span | orientation | background | length |
|---|---|---|---|---|
| `al-mulk-67-19-21.json` | Al-Mulk 67:19-21 | vertical | bg4-nabawi | 64.6s |
| `baqarah-2-187.json` | Al-Baqarah 2:187 | vertical | bg8-dubai-sunset | 101.5s |
| `baqarah-2-198-199.json` | Al-Baqarah 2:198-199 | vertical | bg1-ghana-corridor | 72.9s |
| `baqarah-2-208-209.json` | Al-Baqarah 2:208-209 | vertical | bg7-brunei-dusk | 44.1s |
| `baqarah-2-214.json` | Al-Baqarah 2:214 | vertical | bg5-suleymaniye-night | 52.8s |
| `qazabiri-rahman-55-62-78.json` | Ar-Raḥmān 55:62-78 | vertical | qazabiri-bg (user photo) | 152.3s |
| `isra-17-42-48-r3.json` | Al-Isrā' 17:42-48 | vertical | live footage | 258.4s |
| `nahl-16-10-16.json` | An-Nahl 16:10-16 | **horizontal** | live footage | 150.1s |

Three earlier `isra-17-42-48*` variants (`.json`, `-still`, `-video`) are the
iteration trail: a still-background attempt, then live footage, then `-r3` after a
corrupted render had to be redone from a clean source. `-r3` is the shipped one.

`an-nas-114-1-6`, `az-zumar-39-53-55`, `luqman-31-21` predate this batch.

## Measured reference values

Regression baselines. A size difference *between* clips is expected (the widest
caption sets the shared fit); a change on a **re-run of the same config** is a
regression.

| clip | Arabic | English | block top/height | captions |
|---|---|---|---|---|
| luqman-31-21 | 89px | 64px | 998 / 308 | 4, one 23s pause |
| an-nas-114-1-6 | 95px | 68px | 870 / 325 | 6 |
| al-mulk-67-19-21 | 73px | 52px | 830 / 260 | 8 |
| baqarah-2-187 | 83px | 59px | 812 / 295 | 16 |
| baqarah-2-198-199 | 78px | 56px | 824 / 272 | 9, 1 nudge |
| baqarah-2-208-209 | 95px | 68px | 796 / 327 | 7 |
| baqarah-2-214 | 83px | 59px | 814 / 292 | 6 |
| ar-rahman-55-62-78 | 89px | 64px | 807 / 306 | 19, trimmed |
| isra-17-42-48 | 89px | 64px | 1118 / 315 | 24, live footage |
| nahl-16-10-16 | 77px | 55px | 399 / 281 | 20, horizontal |

Invariants across the vertical batch:

- Frame 1080x1920. Signature ink rows **1801-1833**, centre **x=537** (frame
  centre 540), size 49px at `SIGNATURE_Y_FRAC` 0.945.
- `max_ar_width` = **952px** centered on 1080; `shared_font_size` starts at 196px.
- English/Arabic ratio locked at **0.72** (0.706-0.711 measured after rounding).
- Caption fades 0.4s. Dim 0.35. Accepted backgrounds measure 30-62/255 centre-band
  luma *at that dim*.

Horizontal differs on every count and must not be read against the above:
`max_ar_width` **1516px** (a side-margin block on a 1920-wide frame), signature at
rows **1013-1030 / x=958**, block vertically centered (399 + 281/2 = 539.5).

## The two configs worth reading first

**`baqarah-2-187.json`** — the worked example of the single most important sizing
fact. The original 13-group cut fitted only 63px, the smallest in the batch.
`shared_font_size` is **width-limited, not scale-limited**: it starts at
`height*0.09*arabic_scale` and shrinks until the *widest* caption fits, so raising
`arabic_scale` changes nothing. Splitting the widest groups is the only lever.
Measured staircase, by ranking caption widths at base 196px and splitting the
offenders one at a time:

| groups | fitted Arabic | English |
|---|---|---|
| 13 (original) | 63px | 45px |
| 14 | 73px | 52px |
| 15 | 78px | 56px |
| **16 (shipped)** | **83px** | **59px** |
| 17 | 102px | 73px |

Stopped at 16. 102px would have meant 17 cards in 101.5s, flipping too fast.

**`baqarah-2-198-199.json`** — the `nudge` example, and the reverse use of
`arabic_scale`. The reciter says وَٱذْكُرُوهُ كَمَا هَدَىٰكُمْ **twice**; forced alignment
sees the phrase once in the known-correct text, so it timed the second utterance
at 34.26s and left the first at 29.58s completely uncaptioned. `nudge` pulls the
start back 5.60s into the measured breath (-14.3 dB at 29.42s), covering both.
Note `arabic_scale: 0.4525` — below 1.0 and correct: after splitting removed the
width ceiling, the scale is used as a **cap** to land 78px rather than overshooting
to 95px.

## Schema keys unique to this implementation

Not present in `quran-clips`, which addresses the same needs differently
(`segment.cuts`, waqf-boundary splitting, per-style templates):

- `background_image` + `frame_size` — replace footage with one still for the whole
  reel, scaled to COVER and centre-cropped, never letterboxed. Also permits an
  **audio-only** input.
- `trim: [start, end]` — cut the source before anything else runs. Re-encodes
  rather than stream-copies on purpose: a copy cuts at the nearest keyframe and
  offsets every aligned word against the audio.
- `nudge: [{group, start, end}]` — signed second deltas on one caption, applied
  **last** so gap-filling and silence-clipping cannot undo the hand correction.
- `suppress: [[a, b]]` — leave a time range deliberately uncaptioned.
- `signature` — burn the channel name into the bottom 10%. Rides in the same ASS
  file as the English under its own style, so it costs no extra encode pass.

## Cross-references

Durable procedure now lives in:

- `y-u-s-u-f/quran-clips` — the package, its `.claude/skills/make-post/SKILL.md`,
  and PR #1 (portability, egress routing, decode-verified output).
- The `quran-video-captioning` Hermes skill, rewritten to drive that package.
