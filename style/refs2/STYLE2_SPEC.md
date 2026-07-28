# STYLE2 — pixel forensics of two IG reels

Sources (both **720 × 1280, H.264 High, yuvj420p, bt709, 30/1 fps, progressive**):

| file | duration | frames | bitrate | bar colour |
|---|---|---|---|---|
| `ig_DNrnY9b2EHe.mp4` | 55.033 s | 1651 | 403 kb/s | rose / maroon |
| `ig_DNon3t8J9je.mp4` | 18.867 s | 566 | 361 kb/s | gold |

Everything below is measured from decoded PNG frames with numpy/PIL. Frame numbers are
1-based at 30 fps. Coordinates are full-frame pixels (origin top-left) unless stated.

---

## A. FRAME GEOMETRY

**Letterbox.** Measured as max-over-12-frames per row; first/last row with any value > 12.

| | rose | gold |
|---|---|---|
| first non-black row | 437 | 437 |
| last non-black row | 842 | 844 (edge bleed) |
| top bar height | **437 px** = 0.3414 H | 437 px |
| bottom bar height | **437 px** = 0.3414 H | 437 px |
| visible band | **x 0–719, y 437.5–842.5 → 720 × 405** | same |

720 × 405 is **exactly 16:9** (720 × 9/16 = 405). The band is exactly centred:
(1280 − 405)/2 = 437.5.

**The bars are pure black.** Sampled pixel values in the letterbox are `(0,0,0)` with
occasional ±2 codec ringing (`(0,0,2)`, `(0,1,1)`). High-frequency σ in the letterbox = **0.00**
vs 1.1–1.6 inside the picture — so they are a flat black matte, not crushed video.

**Crop / zoom of the underlying footage.** The visible region is a clean 16:9 window with no
internal pillar/letterboxing, so the source 16:9 footage is used at full width (scale factor
720/1920 = 0.375 if the master was 1080p). Scale drift was tested by resampling the band at
0.92–1.09 and correlating against frame 2: best scale is **0.990–1.000 at every time point in
both reels** → **no Ken-Burns zoom, no punch-in animation** (< 1 % drift over the whole clip).
An absolute punch-in relative to the original camera framing cannot be recovered without the
source.

---

## B. THE BARS

### B.1 Shape — hard-edged full pill (NOT a blur)

1-px luminance profile across the rose bar at y = 652 (background ≈ 18, bar ≈ 110):

```
x   ... 66  67  68  69  70  71  72 ...  305 306 307 308 309 310 311 ...
R   ...  8  11  20  29  87 108 106 ...  110 103 100  62  38  22  21 ...
```

The step from background to full fill takes **2–3 px** (one antialiased pixel plus codec
softening). Interior is flat within ±3 DN over 240 px. **This is a rounded rect drawn with
antialiasing, not a gaussian blob.** (A heavy-blur blob would take 40–80 px to reach plateau.)

Vertical edge-position vs row (right cap, rose, threshold at the 50 % crossing) matches a
circular arc of radius = h/2 to within 0.8 px at every sampled row:

```
dy from centre   0     ±4    ±7    ±10   ±14
measured x     308.1  307.6 306.7 304.8 301.5
circle r=14.5  308.1  307.7 306.6 304.9 301.5
```

→ **corner radius = height / 2 (a stadium / full pill).**

### B.2 Bounding boxes

Height, pooled over 27 independent measurements across both reels and all phrases:
**h = 29.0 px, σ = 1.0** (range 27.3–30.3). Radius **14.5 px**.

Vertical centres (median over phrases):

| line | rose | gold | pooled | fraction of H | fraction of band |
|---|---|---|---|---|---|
| line 1 | 593.4 | 592.5 | **593.5** | 0.4637 | 0.385 |
| line 2 | 715.2 | 716.8 | **715.7** | 0.5592 | 0.687 |
| single-line phrase | 653.8 | 654.7 | **654.2** | 0.5111 | 0.535 |

* Line pitch (centre → centre) = **122.2 px** = 0.0955 H.
* Gap between line-1 bar bottom (608.0) and line-2 bar top (701.2) = **93.2 px**.
* A one-line phrase sits at the midpoint of the two, **654** (predicted midpoint 654.6 ✓).
* Per-phrase jitter of the centre is ±3 px — the bar is anchored to the rendered text, not to a
  fixed grid.

### B.3 Width — hugs the text, does not have a fixed width

Every phrase measured at mid-life (subpixel 50 % crossings):

**rose** (`ig_DNrnY9b2EHe`)

| phrase | line 1 x0..x1 (w) | line 2 x0..x1 (w) | L1 centre | L2 centre |
|---|---|---|---|---|
| 1 | 101.5..309.1 (207.6) | 97.2..323.4 (226.2) | 205.3 | 210.3 |
| 2 | 55.4..335.2 (279.8) | 26.0..355.2 (329.2) | 195.3 | 190.6 |
| 3 | 55.6..345.8 (290.2) | 57.5..339.7 (282.2) | 200.7 | 198.6 |
| 4 | *(single)* 69.1..307.6 (238.5) | — | 188.4 | — |
| 5 | 61.9..313.8 (252.0) | 71.9..297.9 (226.1) | 187.9 | 184.9 |
| 6 | 17.8..377.1 (359.2) | 43.5..339.1 (295.6) | 197.5 | 191.3 |
| 7 | *(single)* 31.3..393.2 (362.0) | — | 212.3 | — |
| 8 | 31.7..365.6 (333.9) | 47.8..343.5 (295.6) | 198.7 | 195.7 |
| 9 | *(single)* 63.6..351.5 (287.9) | — | 207.6 | — |

**gold** (`ig_DNon3t8J9je`)

| phrase | line 1 x0..x1 (w) | line 2 x0..x1 (w) | L1 centre | L2 centre |
|---|---|---|---|---|
| 1 | 45.5..399.3 (353.9) | 47.4..397.0 (349.6) | 222.4 | 221.2 |
| 2 | 85.2..379.2 (294.0) | 81.4..375.4 (294.1) | 232.2 | 228.4 |
| 3 | *(single)* 85.3..389.5 (304.2) | — | 237.4 | — |

Widths span **208 → 362 px** (1.75×) — the pill is sized to the phrase.
Neither the left nor the right edge is fixed, but the **centre is stable**:
rose mean **197.7 px, σ 8.2** (0.2746 W); gold mean **228.3 px, σ 6.4** (0.3171 W).

→ **The text block is horizontally CENTRED about a fixed anchor in the left part of the frame**
(≈ 0.27–0.32 W), not left-aligned to a margin. Line 2 is not deliberately indented; its offset
from line 1 (rose: −5, +5, +2, +3, +6, +3 px; gold: +1, +4 px) is just centring jitter of ±5 px.

### B.4 Padding

Bar edge → outermost glyph ink (white pixels), pooled:

* horizontal: **left 19 px (median), right 17 px** — i.e. bar width ≈ ink width + ~36 px.
  As a fraction: 0.026 W, or ≈ 1.25 × the pill radius.
* vertical: the bar does **not** enclose the text. Glyph ink runs from ≈ **cy − 50** to
  ≈ **cy + 27** (77–90 px tall) while the pill is only 29 px tall. The pill is a *highlight
  band through the letter bodies*, centred ≈ 8 px above the baseline (the row histogram of
  glyph ink peaks at cy + 7…+9, which is the baseline stroke zone).

### B.5 Fill colour and opacity

Median of bar-interior pixels (text and edges excluded), pooled over 5–6 bars per reel:

| reel | RGB | hex | HSL | darkest quartile (least glow-contaminated) |
|---|---|---|---|---|
| rose | (120, 74, 83) | **#784A53** | H 348.3°, S 23.7 %, L 38.0 % | (112, 65, 74) `#70414A`, H 348.5° S 26.6 % L 34.7 % |
| gold | (185, 147, 52) | **#B99334** | H 42.9°, S 56.1 %, L 46.5 % | (181, 142, 45) `#B58E2D`, H 42.8° S 60.2 % L 44.3 % |

**Opacity solved against a bar-absent frame.** Gold phrase 1 covers a background that ranges
from L≈1 (dark pillar) to L≈131 (bright foliage) under the same bar. Regressing bar pixel on
background pixel per channel:

```
R: slope 0.004 -> alpha 0.996    intercept 185.6
G: slope 0.000 -> alpha 1.000    intercept 147.2
B: slope 0.190 -> alpha 0.810    intercept  51.3
```

Binned medians: with the background going 1 → 3 → 15 → 131, the bar reads 183 → 187 → 187 → 186
in R. **The bar is opaque: alpha ≈ 0.95–1.0.** The small blue-channel slope is attributable to
the glow layer riding on top, not to see-through fill. Implement as a solid fill.

### B.6 The glow

Local background removed by a plane fit on an annulus 100–190 px from the pill; excess is the
median at each distance from the pill boundary (px outside the edge):

| d (px) | 1 | 2 | 5 | 10 | 20 | 40 | 60 | 80 |
|---|---|---|---|---|---|---|---|---|
| rose R | 108* | 68* | 32 | 26 | 18 | 8.7 | 4.1 | ~0 |
| rose G | 64* | 42* | 21 | 18 | 11 | 6.0 | 1.3 | ~0 |
| rose B | 72* | 46* | 23 | 19 | 12 | 6.7 | 2.2 | ~0 |
| gold R | 121* | 62* | 51 | 36 | 25 | 13.4 | 8.5 | 5.0 |
| gold G | 91* | 47* | 41 | 31 | 23 | 15.3 | 10.1 | 5.6 |
| gold B | 24* | 19* | 22 | 20 | 16 | 9.0 | 6.6 | 3.7 |

\* d = 1–2 is the antialiased pill edge itself, not glow.

* **Extent: visible to ≈ 60–80 px, effectively zero past 80 px.**
* **Falloff is exponential, not gaussian**: rose R = 32 / 26 / 18 / 8.7 / 4.1 halves roughly
  every 20 px → `exp(−d/28)`. A pure gaussian would die far faster in the tail.
  (measured d40/d5 = 0.27 and d80/d5 ≈ 0.05.)
* **Colour = the bar hue.** rose glow R:G:B at d=5 = 1 : 0.66 : 0.70 vs bar 1 : 0.62 : 0.69.
  gold glow 1 : 0.80 : 0.43 vs bar 1 : 0.79 : 0.28 (glow is slightly desaturated in blue —
  consistent with an additive/screen composite).
* Peak amplitude, extrapolating the exponential back to the edge, is ≈ 35 (rose R) / 60 (gold R)
  DN, i.e. **glow ≈ 0.35–0.45 of the bar's own contrast above background.**

Practical recipe: `glow = screen(bar_colour × gaussian_blur(pill_mask, σ≈14) × 0.45)` plus a
second wide pass `× gaussian_blur(pill_mask, σ≈45) × 0.25` to reproduce the heavy tail — or a
single exponential blur with decay constant 28 px.

---

## C. THE TEXT

**Fill colour.** Median of pixels with min(R,G,B) > 248 (glyph cores):

| reel | RGB | hex |
|---|---|---|
| rose | (255, 251, 252) | **#FFFBFC** — pure white with a whisper of pink |
| gold | (255, 252, 251) core / (255, 252, 243) over the bar | **#FFFCF3** — warm cream |

Both are effectively **white**; the tint is bleed from the bar/glow, not a coloured fill.
Use `#FFFFFF` (optionally `#FFFCF5` for a warm feel).

**Size / weight.**

* Tallest vertical stem (alif) = **34–41 px** (median 38).
* Glyph ink box including marks = **77–90 px** tall (cy − 50 … cy + 27).
* Horizontal stroke run-lengths inside the bar band: median **3–5 px**, p25 = 2, p75 = 6–10,
  max 20–50 (the kashida sweeps). So a **high-contrast pen**: hairlines 2–3 px against
  connectors of 8–12 px — a 4:1 thin/thick ratio.
* From the alif height, nominal font size ≈ **58–66 px** (0.045–0.052 H) for a naskh/thuluth
  face whose alif is ~0.60 em.

**Text glow (separate from the bar glow).** Measured on glyph parts that stick *above* the
pill, background removed:

| d from glyph edge | 3 px | 5 px | 8 px | 12 px | 18 px | 25 px |
|---|---|---|---|---|---|---|
| rose excess R/G/B | 42/31/31 | 31/21/21 | 21/13/13 | 13/8/7 | 6/2/1 | 2/0/−1 |
| gold excess R/G/B | 25/19/6 | 19/12/−2 | 16/11/−1 | 12/8/−3 | 5/3/0 | 1.5/0/0 |

→ **a tight halo, radius ≈ 15–20 px, half-life ≈ 5–6 px** (i.e. gaussian σ ≈ 6–8 px),
amplitude ≈ 40 DN at the glyph edge. Its hue is **warm neutral in rose** (R:G:B = 1:0.73:0.73 —
notably *not* the rose bar hue, which has B > G) and **gold-tinted in gold** (1:0.76:0.24).
There is no hard outline or offset drop shadow; gold shows a weak negative ring (−8…−17 DN in
G/B at 5–18 px) which may be a subtle dark shadow but is confounded with the background
gradient — see Uncertain.

**Left margin.** Because the block is centred (§B.3), there is no fixed left margin: the
leftmost glyph ink lands anywhere from x = 35 to x = 200 depending on phrase length. The
*anchor* is the centre at 0.275 W (rose) / 0.317 W (gold).

**Typeface.** A **display Thuluth**, not Naskh. Driving features seen at 3× zoom:

* extreme stroke modulation (2–3 px hairlines vs 10–12 px stems, ~4:1);
* deeply scooped, low-slung final bowls (ن, ي, ح) that dive well below the baseline and sweep
  back left under the preceding letters — a Thuluth signature;
* pointed, pen-cut terminals with a strong ~45° pen angle;
* long flat kashida connectors, and letters stacked/overlapped vertically (ملائكته, النبي);
* large, separately-positioned Qur'anic vowel marks (fatha/damma/shadda) drawn at near-body
  scale, well above the letters.

Against the named candidates: **not** Amiri, Scheherazade, Lateef or KFGQPC Uthmanic Hafs
(all Naskh — shallower bowls, lower contrast, tighter mark placement, no diving finals).
It is closest to the **Aref Ruqaa / DecoType-Thuluth class**; Aref Ruqaa Bold is the nearest
widely available free match, though the finals here are longer than stock Aref Ruqaa, so the
original may be a commercial thuluth face (e.g. an "AGA"/"DecoType Thuluth" family).

---

## C2. COLOUR RELATIONSHIP (video → bar)

Sampled over the whole video band, excluding the overlay box (x < 460, y 520–790), every 7th
frame:

| statistic | rose video | gold video |
|---|---|---|
| mean RGB | (61.0, 47.8, 49.6) `#3D2F31` — H 351.8°, S 12.2 %, L 21.3 % | (81.9, 72.9, 52.3) `#514834` — H 41.9°, S 22.1 %, L 26.3 % |
| luminance²-weighted dominant | (141.9, 111.1, 113.0) `#8D6F70` — H 356.2°, S 12.2 %, L 49.6 % | (129.3, 120.2, 97.4) `#817861` — H 42.8°, S 14.0 %, L 44.5 % |
| median of saturated pixels (S>0.25, L>40) | (106, 70, 72) `#6A4648` — H 356.7°, S 20.5 %, L 34.5 % | (85, 69, 40) `#554528` — H 38.7°, S 36.0 %, L 24.5 % |
| **BAR** | `#784A53` — **H 348.3°, S 23.7 %, L 38.0 %** | `#B99334` — **H 42.9°, S 56.1 %, L 46.5 %** |

**Hue: unchanged.** rose bar 348.3° vs video 351.8/356.2/356.7° (Δ 3–8°);
gold bar 42.9° vs video 41.9/42.8/38.7° (Δ 0–4°). The bar simply takes the clip's dominant hue.

**Lightness: a remarkably consistent ×1.78 of the frame mean.**
rose 21.3 → 38.0 = ×1.78; gold 26.3 → 46.5 = ×1.77.

**Saturation: boosted ~2–2.5× the frame mean** (rose 12.2 → 23.7 = ×1.94; gold 22.1 → 56.1 =
×2.54), or ×1.15–1.6 relative to the *saturated-pixel* median.

Auto-derivation rule that reproduces both:

```
H_bar = hue of the clip's luminance-weighted dominant colour   (unchanged, ±5°)
L_bar = clamp(1.78 * L_mean(clip), 0.34, 0.48)
S_bar = clamp(2.2 * S_mean(clip),  0.22, 0.60)
```

---

## D. TIMING / ANIMATION

Phrase presence detected at 30 fps from the count of near-white low-chroma pixels in the
overlay crop (threshold 20 px). "on" = first/last frame above threshold.

### rose — `ig_DNrnY9b2EHe` (9 phrases)

| # | frames | t on → off | duration | lines |
|---|---|---|---|---|
| 1 | 8–123 | 0.23 → 4.07 s | 3.87 s | 2 |
| 2 | 141–343 | 4.67 → 11.40 s | 6.77 s | 2 |
| 3 | 366–575 | 12.17 → 19.13 s | 7.00 s | 2 |
| 4 | 594–675 | 19.77 → 22.47 s | 2.73 s | 1 |
| 5 | 693–815 | 23.07 → 27.13 s | 4.10 s | 2 |
| 6 | 837–1094 | 27.87 → 36.43 s | 8.60 s | 2 |
| 7 | 1116–1237 | 37.17 → 41.20 s | 4.07 s | 1 |
| 8 | 1259–1491 | 41.93 → 49.67 s | 7.77 s | 2 |
| 9 | 1513–1636 | 50.40 → 54.50 s | 4.13 s | 1 |

Blank gap between phrases: **17–22 frames (0.57–0.73 s)** measured on this threshold; the true
"nothing on screen" window is ~4–8 frames because the ramps overlap the threshold.

### gold — `ig_DNon3t8J9je` (3 phrases)

| # | frames | t on → off | duration | lines |
|---|---|---|---|---|
| 1 | 14–254 | 0.43 → 8.43 s | 8.03 s | 2 |
| 2 | 273–413 | 9.07 → 13.73 s | 4.70 s | 2 |
| 3 | 432–557 | 14.37 → 18.53 s | 4.20 s | 1 |

### D.1 Enter / exit — the two reels use DIFFERENT transitions

**gold = a right-to-left linear WIPE.** Tracking the pill's subpixel edges frame by frame while
the opposite edge stays pinned:

```
enter (final box x 43 .. 397):
  f8  x 263.8..399.2      f16  x 146.0..397.2      f26  x  61.1..395.5
  f12 x 193.9..397.6      f20  x 103.8..395.9      f60  x  43.6..396.0  (settled)

exit:
  f236 x 43.1..395.0   f244 x 43.4..355.2   f252 x 44.0..271.4   f258 x 43.9..167.0
```

The right edge is fixed during the enter and the **left edge travels 400 → 43 px**; on exit the
left edge is fixed and the **right edge travels 395 → 43 px**. Both fronts sweep **right → left**
(matching the RTL reading direction).

* enter duration ≈ **26–30 frames (0.87–1.00 s)**, front speed ≈ **293 px/s**, roughly linear.
* exit duration ≈ **22–24 frames (0.73–0.80 s)**, front speed ≈ **437 px/s** (exit is ~1.5×
  faster than enter).
* the wipe front is **feathered over ~12–14 px** (measured 1-px profile at f16 rises 25 → 76 DN
  between x = 134 and x = 146).
* there is **no global opacity ramp** — pixels behind the front are already at full value.

**rose = a uniform CROSS-FADE.** The same 1-px row profile shows every x position rising
together (f826 all ≈ 3, f830 all ≈ 11–13, f834 all ≈ 18–20, f854 ≈ final) and the detected box
grows symmetrically at both caps.

* fade-in ≈ **28–30 frames (0.93–1.00 s)**, near-linear with a small step to 100 % in the last
  2 frames.
* fade-out ≈ **28–29 frames (0.93–0.97 s)**, near-linear.

Both reels show a **3–7 % overshoot** in bar brightness in the last ~5 frames before the exit
begins (gold p1 peaks at 1.04–1.07 of plateau at f236–240; rose p6 at 1.01–1.03 at f1069–1076) —
a small "flash" cue before the swap.

### D.2 No pulse during a phrase

Normalised bar chroma-excess sampled every frame across a whole phrase (e.g. rose p6, frames
854–1068, 215 frames): values stay in **0.97–1.01**, i.e. **< ±2 % variation, no periodicity**.
Same for gold p1 (0.98–1.00 over 200 frames). The residual drift tracks underlying video
luminance. **There is no brightness/opacity pulse — do not add one.**

### D.3 Line 1 vs line 2 are simultaneous — zero stagger

Per-line normalised curves, frame-aligned:

```
gold p1 enter   f4    f8    f12   f16   f20   f24   f28
   line 1      0.12  0.44  0.64  0.79  0.88  0.98  1.05
   line 2      0.11  0.40  0.60  0.80  0.88  0.98  1.03

rose p2 enter   f131  f135  f139  f143  f147  f151  f158
   line 1      0.09  0.35  0.58  0.70  0.76  0.84  0.98
   line 2      0.09  0.34  0.58  0.70  0.77  0.86  0.97
```

Difference is ≤ 0.02 at every frame, i.e. **stagger = 0 frames**. Exits likewise
(gold f262: L1 0.20 / L2 0.20).

---

## E. OVERLAYS

### E.1 Bokeh / dust specks

Detected as local maxima ≥ 30 DN above a 13×13 box-mean, area 3–80 px, aspect 0.45–2.2,
restricted to x < 460 (the right half is dominated by subject texture):

| reel | count in left 460 px | size range | median size | peak excess | colour |
|---|---|---|---|---|---|
| rose | 30–63 per frame (≈ **45–95 full-frame**) | 2–11 px | 3–5.5 px | median 42–70, max 195 | (85–135, 71–109, 74–113) — warm off-white |
| gold | 13–56 per frame (≈ **20–85 full-frame**) | 2–11 px | 3–6 px | median 48–116, max 173 | (100–193, 98–181, 72–150) — warm off-white |

Opacity: brightness excess 40–120 DN over local background → roughly **0.15–0.45 alpha white**
for a background at L≈40.

Drift, matching blobs between frames t and t+3 (0.1 s):
median vₓ **−2 … +4 px/s**, median v_y **−3 … +2 px/s**, median |v| **5–30 px/s**
(the higher figures come from mismatches). **Motion is very slow and has no consistent
direction** — the specks are essentially near-static, drifting a few px per second.

### E.2 The geometric / islamic pattern (rose reel)

A **hexagonal honeycomb lattice** is visible in the dark background of the rose reel, upper
right. Contrast-boosted extraction shows it is **occluded by the subject's head and shoulders**
→ it is a **physical set backdrop inside the footage, not a composited overlay**. It does not
move relative to the frame, it has no separate opacity to specify, and it should **not** be
reimplemented as an overlay layer. Cell pitch ≈ 55 px in the 2× crop → ≈ 28 px on the 720-wide
frame; luminance ≈ 6–12 DN over the near-black background.

### E.3 Vignette / edge falloff

Measured on the **per-pixel maximum over time** (which removes scene content bias and leaves any
fixed multiplicative darkening), normalised to mid-band level:

| position | rose | gold |
|---|---|---|
| band top, 13 px in (y 450) | 0.23 | 0.33 |
| band top, 58 px in (y 495) | 0.35 | 0.61 |
| band bottom, 15 px in (y 825) | 0.31 | 0.56 |
| left edge x = 0 | 0.16 | 0.16 |
| left edge x = 60 | 0.63 | 0.50 |
| left edge x = 120 | 0.89 | 0.91 |
| **right edge x = 690** | **0.81** | **0.94 (no falloff)** |

So the darkening is **asymmetric**: strong at the top and bottom edges of the video band
(recovering over ~60 px) and on the **left ~100 px** (down to 0.16 at x = 0), with **essentially
no falloff on the right**. This reads as a soft feather where the picture meets the letterbox
plus a **left-side gradient scrim that buys contrast for the text**, rather than a classic radial
vignette. Corner/centre ratios on the temporal median are not meaningful here because both
scenes put the lit subject on the right.

### E.4 Film grain

High-frequency σ (pixel minus its 4-neighbour mean) in flat regions:

| region | rose | gold |
|---|---|---|
| mid-tone flat inside picture (L≈40–90) | **1.40 / 1.53 / 1.57** | **1.17 / 1.14 / 1.12** |
| dark flat inside picture (L≈1–39) | 0.13–0.60 | 0.36–0.73 |
| letterbox (L = 0) | **0.00** | **0.00** |

σ ≈ 1.1–1.6 DN (≈ 0.5 % of full scale) and it **scales with local luminance** and is **absent in
the matte** — that is the signature of H.264 quantisation noise, **not** added film grain.
**No grain layer is needed**; if you add one, keep σ ≤ 1.5 DN and do not put it on the mattes.

---

## Canonical parameters (fractions of frame)

Canvas W = 720, H = 1280. Fractions let you scale to 1080 × 1920 by ×1.5.

| # | parameter | value (px @720×1280) | fraction | notes |
|---|---|---|---|---|
| 1 | canvas | 720 × 1280 | — | 9:16, 30 fps |
| 2 | letterbox top height | 437 | 0.3414 H | pure `#000000` |
| 3 | letterbox bottom height | 437 | 0.3414 H | pure `#000000` |
| 4 | video band | x 0..720, y 437.5..842.5 | y0 = 0.3418 H, h = 0.3164 H | exactly 720×405 = 16:9 |
| 5 | video scale animation | none | — | scale drift < 1 % |
| 6 | bar height | 29.0 | 0.02266 H / 0.0403 W | σ = 1.0 |
| 7 | bar corner radius | 14.5 | = h/2 | full pill |
| 8 | bar edge | hard, 2–3 px AA | — | not blurred |
| 9 | bar line-1 centre y | 593.5 | 0.4637 H | band-relative 0.385 |
| 10 | bar line-2 centre y | 715.7 | 0.5592 H | band-relative 0.687 |
| 11 | single-line centre y | 654.2 | 0.5111 H | midpoint of 9 & 10 |
| 12 | line pitch | 122.2 | 0.0955 H | |
| 13 | gap between bars | 93.2 | 0.0728 H | 608.0 → 701.2 |
| 14 | text block anchor | centred at x 198 (rose) / 228 (gold) | 0.275 W / 0.317 W | ±8 px jitter; use 0.29 W |
| 15 | line-2 horizontal offset | 0 (±5) | 0 | no deliberate indent |
| 16 | bar h-padding (each side) | 18 | 0.025 W | bar w = ink w + 36 |
| 17 | bar width | 208–362 observed | 0.29–0.50 W | derived from text, never fixed |
| 18 | bar fill (rose) | (120,74,83) `#784A53` | H 348°, S 24 %, L 38 % | |
| 19 | bar fill (gold) | (185,147,52) `#B99334` | H 43°, S 56 %, L 47 % | |
| 20 | bar alpha | 0.95–1.0 (opaque) | — | measured slope ≈ 0 |
| 21 | glow colour | = bar colour | — | screen / additive |
| 22 | glow amplitude at pill edge | ≈ 0.40 × bar contrast | — | 35 DN rose R, 60 DN gold R |
| 23 | glow falloff | `exp(−d/28 px)` | 28 px = 0.039 W | zero past ≈ 80 px (0.11 W) |
| 24 | glow sampled (rose R) | 32@5, 26@10, 18@20, 8.7@40, 4.1@60 DN | — | see §B.6 table |
| 25 | text colour | `#FFFFFF` (`#FFFCF5` warm) | — | measured `#FFFBFC`/`#FFFCF3` |
| 26 | font size (nominal) | 60 ±4 | 0.047 H | alif stem 38 px |
| 27 | glyph ink box height | 77–90 | 0.060–0.070 H | cy−50 .. cy+27 |
| 28 | stroke weight | 3–5 median, 2 min, 10–12 max | — | ~4:1 contrast |
| 29 | text baseline | ≈ bar centre + 8 | 0.006 H below cy | |
| 30 | text glow radius | 15–20 | 0.02–0.03 W | gaussian σ ≈ 6–8 px |
| 31 | text glow amplitude | ≈ 40 DN at glyph edge | ~0.16 alpha | warm white (rose) / gold (gold) |
| 32 | typeface | display **Thuluth** | — | Aref Ruqaa-class; not Amiri/Scheherazade |
| 33 | phrase duration | 2.7–8.6 s (median 4.7) | — | 9 phrases / 55 s, 3 / 18.9 s |
| 34 | inter-phrase blank | 17–22 frames (0.57–0.73 s) | — | full clear between phrases |
| 35 | enter (gold style) | RTL wipe, 26–30 f (0.87–1.00 s) | 293 px/s | front feather 12–14 px |
| 36 | exit (gold style) | RTL wipe, 22–24 f (0.73–0.80 s) | 437 px/s | ~1.5× faster than enter |
| 37 | enter (rose style) | uniform fade, 28–30 f (0.93–1.00 s) | — | linear |
| 38 | exit (rose style) | uniform fade, 28–29 f (0.93–0.97 s) | — | linear |
| 39 | pre-exit flash | +3–7 % for ~5 frames | — | both reels |
| 40 | line stagger | 0 frames | — | lines are simultaneous |
| 41 | bar pulse during phrase | none (< ±2 %) | — | do not add |
| 42 | bokeh count | 45–95 on screen | — | |
| 43 | bokeh size | 2–11 px, median 3–6 | 0.004–0.015 W | round-ish |
| 44 | bokeh colour / alpha | warm off-white, 40–120 DN excess | α ≈ 0.15–0.45 | |
| 45 | bokeh drift | ≈ 3–8 px/s, no preferred direction | — | near-static |
| 46 | edge scrim: top of band | ×0.23–0.33 at 13 px, ×1.0 by 120 px | — | soft feather |
| 47 | edge scrim: bottom of band | ×0.31–0.56 at 15 px | — | |
| 48 | edge scrim: left | ×0.16 at x=0 → ×0.9 by x=120 | 0.167 W | text-contrast scrim |
| 49 | edge scrim: right | none | — | asymmetric, not a radial vignette |
| 50 | film grain | none added (σ ≤ 1.5 DN codec noise) | — | letterbox σ = 0.00 |
| 51 | bar hue rule | H_bar = clip dominant hue | Δ ≤ 5° | see §C2 |
| 52 | bar lightness rule | L_bar = 1.78 × clip mean L | clamp 0.34–0.48 | ×1.78 / ×1.77 measured |
| 53 | bar saturation rule | S_bar = 2.2 × clip mean S | clamp 0.22–0.60 | ×1.94 / ×2.54 measured |

---

## Uncertain

1. **Which transition is canonical.** The two reels genuinely differ: gold uses an RTL wipe,
   rose a uniform cross-fade. Both are ~0.9 s. If you must pick one, the wipe is the more
   distinctive and reads as intentional; the fade is safer.
2. **Text reveal coupling.** In the gold wipe the glyph white-pixel count keeps climbing for
   ~50 frames after the pill finishes filling. It is not resolved whether the text is masked by
   the same wipe plus a slower independent fade-up of the text glow, or whether the text has its
   own longer fade. Treat "text masked by the same wipe" as the default.
3. **A dark shadow behind the text.** Gold shows a negative luminance ring (−8…−17 DN in G/B) at
   5–18 px from glyph edges; rose does not. This may be a soft dark drop shadow under the text,
   or simply the background gradient contaminating the plane fit. Not safe to implement.
4. **Exact horizontal anchor.** Bar centres cluster at 0.275 W (rose) and 0.317 W (gold) with
   σ ≈ 7–8 px — enough spread that the layout may actually be "centre the text inside a fixed
   box" whose box differs slightly between the two edits, rather than a single global anchor.
   0.29 W ± 0.03 reproduces both.
5. **Whether the pill also carries a subtle inner gradient.** Interior samples vary 106 → 124 DN
   across a bar, but the variation correlates with proximity to glyphs (the text glow lands on
   the bar), so a genuine fill gradient cannot be separated from glow contamination. Solid fill
   is the safe implementation.
6. **Bar alpha in the blue channel.** R and G regress to alpha ≈ 1.00 but B to 0.81. Either the
   fill is genuinely opaque and the residual is the glow, or the fill is ~0.9 alpha. Opaque is
   the better bet.
7. **Left-side scrim vs scene lighting.** The left-edge darkening is present in the per-pixel
   temporal maximum of both reels, which argues for a fixed graphic element, but both clips also
   happen to have dark camera-left backgrounds. Add a gentle left scrim only if the underlying
   clip is bright on the left.
8. **Punch-in of the source footage.** The band is exactly 16:9 with no internal bars and no
   zoom animation, so no crop factor can be recovered without the original master.
9. **Bokeh drift velocity.** Blob matching across 3-frame gaps yields median |v| of 5–30 px/s
   with inconsistent direction; the honest statement is "drifts slower than ~10 px/s", the higher
   figures are mismatch noise.
10. **Typeface identity.** The Thuluth classification is confident; the specific family is not.
    Aref Ruqaa is the closest free match but its finals are shorter than what is measured here.
