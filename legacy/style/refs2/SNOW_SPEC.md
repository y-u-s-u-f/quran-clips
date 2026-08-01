# SNOW_SPEC — what Node Video's "Snow" actually is, and how we reproduce it

Supersedes the "floating gold bokeh particles" model in `fx/particles.py` and in
the first Pillow port inside `scripts/render_bars.py`. That model was wrong in
kind, not just in tuning.

Two independent lines of evidence agree, so this is not a guess:

1. **The shipped shader**, recovered from the app binary.
2. **Pixel forensics** on the two reference reels, `ig_DNon3t8J9je.mp4` (gold)
   and `ig_DNrnY9b2EHe.mp4` (rose).

---

## A. The effect is a procedural shader, not a particle system

Node Video (`com.shallwaystudio.nodevideo`, v8.8.0) is a **Unity** app. Its
effect tree (`assets/bin/Data/data.unity3d`, TextAsset `prop`) maps
`/_NewProp/_AssetStore/_Nature/_Snow` to the shader **`NodeVideo/AssetStore/snow`**.
The compiled GLSL ES 3.0 was LZ4-decompressed out of the shader blob. It is a
**single full-screen pass** (`ZTest Always, ZWrite Off, Cull Off`) — no geometry,
no sprites, no `ParticleSystem`, no flake atlas. In full:

```glsl
p     = rotate(uv*viewport_ratio - 0.5, -_direction * PI/180);
scale = _size  * 9.5 + 0.5;          // 0.5 .. 10.0
expo  = _amount * -49 + 50;          // 50 .. 1
q     = p / scale + 0.5;
sway  = sin(0.1 * t * _speed);
uv1   = (q.x - 0.5000*sway, q.y + 0.1000*t*_speed) * 0.6 - 0.003*t*_flicker;
uv2   = (q.x + 0.1111*sway, q.y + 0.0714*t*_speed) * 0.9 + 0.002*t*_flicker;
v     = pow( texture(_texture0, uv1).r * texture(_texture0, uv2).b , expo );
out   = src + _color.rgb * v * _intensity * 18.0;      // ADDITIVE
```

`_texture0` is a bundled **256×256 RGBA8 white-noise** Texture2D, bilinear
filter, repeat wrap. We verified it is genuinely uncorrelated white noise
(uniform histogram, lag-1 spatial autocorrelation 0.0004 / −0.0036, R–B channel
correlation 0.004), which is why our port **generates its own seeded noise tile
rather than shipping an extracted app asset** — statistically it is the same
object.

So the "snowflakes" are simply **the rare coincidences where two heavily
magnified, bilinearly-interpolated noise fields are both near white at the same
pixel**, after a high power crushes everything else to zero. That is the whole
mechanism.

### Parameter semantics (read off the shader)

| UI | Uniform | Effect |
|---|---|---|
| Amount | `_amount` | Sets the exponent `50 − 49·amount`. A density/threshold knob, **not** a count. |
| Size | `_size` | Divides uv by `size·9.5+0.5` — magnifies the noise. Bigger flakes ⇒ proportionally fewer. |
| Direction | `_direction` | **A single float in degrees** that rotates the field. The `0 x 0` two-field UI is a wrapper; the shader only ever receives one angle. |
| Intensity | `_intensity` | Brightness multiplier, ×18 internally, applied before the additive blend. |
| Speed | `_speed` | Vertical scroll of both layers (0.1 and 0.0714 uv/s — deliberately different so they decorrelate) plus the horizontal `sway`. |
| Flicker | `_flicker` | A tiny **opposed** uv drift per layer (−0.003 and +0.002 /s). Sliding the two multiplied fields against each other is what makes flakes twinkle in and out. It does not touch brightness directly. |
| Color | `_color` | RGB tint of the additive term. |

There is no Blend row on Snow (Glow Scan has one; Snow does not), and no
opacity row. It can only lighten.

### Two neighbours it is easy to confuse it with

* **`NodeVideo/AssetStore/snowflake`** (Effects ▸ Nature ▸ *SnowFlake*, the tile
  immediately next to Snow) is a completely different, much richer shader — a
  per-cell grid instancer with a depth loop that draws rotating, feathered flake
  *geometry* (`_gravity, _turbulence, _flake_thickness, _flake_shape_adjust1/2,
  _rotation_over_lifetime, …`). If you ever want recognisable flakes, that is
  the effect, not this one.
* **Particle ▸ Nature ▸ Snow Right/Down/Up/…** (New Node ▸ 3D ▸ Particle) is a
  real Unity `ParticleSystem` with `Snowflake` (128×128) and `Snowflakes`
  (128×512 atlas) sprites. Also not this.

### Origin — unresolved, stated plainly

The shader sits under `NodeVideo/AssetStore/...` alongside ~70 siblings whose
names read like ported Shadertoy work (`rain_drop_pro`, `turbulent_distortion`,
`water_caustics`, `heat_wave`, `fire_flies`). There is **no attribution string,
licence, or author name** anywhere in the blob. It is **definitively not** the
widely-copied "Just Snow" by Andrew Baldwin (Shadertoy `ldsGDn`) or the
gerardogc2378 "Let it snow!!!" variant — both are multi-layer parallax particle
engines with a per-layer loop, and this shader has no loop at all, just two
texture fetches. The `pow(noise_a·noise_b, k)` trick is generic enough that it
may be original. **Treat "it is a port of a named Shadertoy" as unproven.**
There is also no published documentation for this effect anywhere — nodevideo.com's
guide has no effects reference at all, and searches in English, Arabic,
Indonesian and Portuguese turned up nothing showing it close up.

---

## B. The tutorial's settings, read off the parameter panel

Tutorial `4104297192421293586.MP4`; the Snow segment runs **t ≈ 446–463.5 s**.

* **t ≈ 449.5–452.5** — effect browser, Nature tab. The Snow tile is a *live
  preview of the user's own clip*, not stock art: his glowing Arabic text with a
  handful of tiny white specks. No snowflake icon, no blizzard.
* **t ≈ 453.4** — inserted with defaults **Amount 0.80, Size 0.10,
  Direction 0 × 0, Intensity 1.00, Speed −1.00, Flicker 1.00, Color white.**
* **t ≈ 456.5–458.0** — he drags **Amount 0.80 → 0.70** and touches nothing else.
* **t ≈ 458.5–460** — taps the Color swatch, eyedroppers his own glow colour;
  the swatch settles on a warm cream, sampled **RGB(205,189,163) ≈ `#CDBDA3`**.
* **t ≈ 487.6–492** (playback) — particles are **~2–3 px round-ish dots with a
  1-px soft falloff**, very sparse, **moving straight UP** (tracked centroids:
  y 41 → 39.5 → 37.8 → 35.6 → 33.0 with x constant), brightness flickering in
  and out. No sprite structure, no streaks.

Note the direction: with Speed −1.00 the field **rises**. (The shader's v axis
is GL-oriented, which flips the sign you would predict from the source above.)

---

## C. Pixel forensics on the reference reels

Band = 720×404 at y=438 in the 720×1280 refs.

**Isolation method.** Per-pixel temporal median over a 15-frame window gives the
background (particles move ~3 px/frame so they never survive the median);
particles are the positive residual. That alone still catches the reciter's
motion edges and the caption text, so it is intersected with a spatial high-pass
(frame − 7×7 median). The caption bbox is excluded by hand. Contact sheets of
48 isolated particles at 14× are in `fx/snow/gold_sheet_iso.png` and
`fx/snow/rose_sheet_iso.png`.

> Gotcha that cost an hour: `crop=720:405` on a `yuv420p` stream **silently
> rounds to 404**, so a naive raw-video read shifts by one row per frame. Put
> `format=gbrp` before `crop`, or crop to an even height. (Same trap FX_RECIPE
> already flags for `blend`.)

### Measured

| quantity | gold ref | rose ref |
|---|---|---|
| residual RGB at peak, particles > 60 DN | 96.1 / 96.4 / 93.4 | 92.7 / 92.5 / 92.5 |
| residual RGB at peak, particles > 100 DN | 134.6 / 134.7 / 131.8 | 132.8 / 132.6 / 132.6 |
| normalised radial profile, r = 0,1,2,3 px | 1.00 / 0.40 / 0.11 / 0.05 | 1.00 / 0.39 / 0.07 / 0.02 |
| blob width, w=1 px : w≥4 px | 3150 : 648 | 1443 : 676 |
| median h/w aspect | 1.0 | 1.0 |
| corr(blob area, peak brightness) | **+0.80** | **+0.76** |
| coherent vertical drift (phase correlation of the isolated layer) | −3.3 px/frame | −3.5 px/frame |
| y–t streak slope, gaps 1/3/5 frames | −3.0 / −2.7 / −2.7 | −3.0 / −2.3 / −3.0 |
| correlation surviving to gap 10 frames | none | none |
| brightness pulse along the trajectory | rise over ~3 frames, peak, gone by +5; repeats every ~13–26 frames | same |
| blobs/band above 40 DN / above 60 DN | ~24 / ~12 | ~24 / ~12 |

### What that says

* **The specks are NEUTRAL WHITE — in the two reference reels.** R = G = B to
  within 3 % in *both* the gold and the rose reel. They are **not** tinted with
  the bar colour there. (The tutorial's author does eyedropper a cream `#CDBDA3`
  into Color, but `#CDBDA3` is itself only ~20 % off white, and the refs measure
  less than that.)

  **SUPERSEDED for new work, 2026-07-30.** The owner asked for the snow to carry
  the bar colour, and `fx.snow.tint_mix` in `templates/bars.yaml` moved `0.10 →
  1.00` account-wide. The measurement above stands as evidence of what the two
  reference reels did; it is no longer what the account ships. Reels rendered
  before that date still have white specks and are not retro-fitted. The
  golden `at-tawbah-128-128` was re-blessed for the change.
* **They are near-point sources.** Profile 1.00 / 0.40 / 0.09 falls to a tenth
  by r = 2 px. Read as 1–3 px dots with a faint bloom, never as 10-px blobs.
* **Big ⇒ bright, not big ⇒ dim.** The +0.8 area/brightness correlation rules
  out a bokeh model outright.
* **The field rises**, coherently, at ~2.9–3.3 px/frame ≈ 87–99 px/s on the
  405-tall band. Confirmed three ways (phase correlation, y–t streak slope at
  multiple frame gaps, and the tutorial's own preview playback).
* **Nothing persists.** Cross-frame correlation is dead by a 10-frame gap and
  the per-particle brightness is a short pulse ~5 frames wide recurring every
  13–26 frames. That is exactly the signature of two noise fields sliding past
  each other — coincidences bloom and die — not of particles with a lifetime.

---

## D. What the old generator got wrong

`fx/particles.py` and its Pillow port drew soft gaussian discs on closed
elliptical orbits. Specifically:

| | old | measured |
|---|---|---|
| model | ~50 sprite-ish gaussian blobs | procedural noise², no discrete particles at all |
| radius | `size*22` px ⇒ σ ≈ 3.3 px @1080, footprint ~20 px | core equivalent σ ≈ 0.8 px @720 |
| colour | multiplied by the bar tint (gold `#C9A227` / rose `#B0576B`) | **neutral white** |
| brightness vs size | `amp ∝ 1/(1+0.35r)` — big ones dim (bokeh) | big ones **brighter** (+0.8) |
| motion | closed ellipses, no net travel, ~18 px/s tangential | a coherent **rise** at ~90 px/s |
| flicker | gentle sinusoid, 1–3 cycles per 6 s loop | sharp ~5-frame pulses every 13–26 frames |
| lifecycle | immortal, orbiting forever | appear/vanish; nothing survives 10 frames |

Three of those (fat soft blobs, gold tint, orbiting-in-place) are exactly the
things that read as "wrong" in motion: gold cotton-wool drifting in circles
instead of white dust rising.

---

## E. The new generator

`render_snow()` / `_SnowField` in `scripts/render_bars.py`. It is the shader
above, evaluated with Pillow only — **no numpy**, so it runs under
`/opt/homebrew/bin/python3`, which is the pipeline interpreter.

Per frame, four C-speed Pillow calls:

1. two `Image.transform(..., AFFINE, resample=BILINEAR)` samples of a tiled
   noise image — an affine magnify + sub-texel translate is *exactly* a
   bilinear texture fetch with repeat wrap;
2. one `ImageChops.multiply`;
3. one `.point(lut)` carrying `pow(v, expo) · gain`, and one per channel for
   the tint.

**240 frames at 1080×608 in 3.0 s**, 1.0 MB at CRF 8. Cached in the clip's
`work/` keyed on band size, tint and every snow parameter, as before.

### Seamless loop

Required, because `build_bars.py` feeds it with `-stream_loop -1`.

* Each layer gets its **own noise tile whose height in texels equals its exact
  travel over one loop**, so one loop advances it by precisely one tile.
* The horizontal sway is one whole sinusoid per loop.
* The flicker cross-drift is folded into the (integer) vertical travel rather
  than applied to u — the real shader drifts u linearly, which cannot close a
  loop at any sane tile width. This is the **one deliberate deviation** from the
  shader; since the twinkle is dominated by the two layers' *differing vertical*
  rates anyway, the character is preserved.

Verified: the y–t shift across the loop boundary (frames 232–248 of a
`-stream_loop 1` decode) is a flat −3 px/frame, identical to mid-loop.

### Calibration

Node's own UI values are kept verbatim — `amount 0.70`, `size 0.10`,
`speed -1.0`, `flicker 1.0`, `intensity 1.0`. Two values are ours, because the
shader's viewport-ratio convention and its additive `×18` do not survive the
translation to a band-confined `blend=screen` composite:

* `texel_scale: 72.0` — `texel_px = size · texel_scale · band_h/405`, i.e. 7.2 px
  per noise texel on the 405-band. (The shader's own arithmetic brackets this
  between 6.8 and 12.1 depending on which way `viewport_ratio` normalises, which
  we could not determine from the blob; 7.2 is fitted.)
* `gain: 7.0` — replaces `intensity·18`.
* `tint_mix: 0.10` — a whisper of the bar hue over white. Set 0.0 for the
  strictly-measured neutral; ~0.35 reproduces the tutorial's `#CDBDA3` cream.

Fitted on the finished 1080 layer **downscaled to 720**, since the refs went the
same way through Instagram:

| | ours | refs |
|---|---|---|
| apparent rise | 3.0 px/frame | 2.9 px/frame |
| blobs/band > 40 DN | 22.5 | ~24 |
| blobs/band > 60 DN | 12.1 | ~12 |
| radial profile r=0..3 | 1.00 / 0.45 / 0.13 / 0.04 | 1.00 / 0.40 / 0.09 / 0.04 |
| colour | white + 10 % tint | neutral white |

Our blobs remain ~10 % softer at r=2 than the refs measure. That residual is
within the uncertainty of measuring a 1–3 px feature through h.264, and pushing
`texel_scale` lower to close it starts to slow the rise below the measured
value.

### Comparison images

`fx/snow/` — left half of each is the **real reference frame**, right half is
the **new layer screened over that same frame's snow-free temporal median**:

* `fx/snow/comp_gold.png`, `fx/snow/comp_rose.png` — full band, 1:1.
* `fx/snow/compz_gold.png`, `fx/snow/compz_rose.png` — 4× zoom on a matched dark
  region.
* `fx/snow/sbs_full.png` — the four isolated particle layers stacked
  (ref gold, ref rose, new gold, new rose) at 3× exposure.
* `fx/snow/gold_sheet_iso.png`, `fx/snow/rose_sheet_iso.png` — 48 isolated
  reference particles at 14×, the images that settled the profile and the
  colour.

### Composite

Unchanged: `blend=all_mode=screen` onto the band, inside the crop, so nothing
reaches the letterbox. (Node adds rather than screens; over a band graded to
mean luma 0.15–0.32 the two are close, and `gain` absorbs the difference.)

### Contradicted QA note

An independent QA pass on the *old* layer reported the references as
"near-static, 3–8 px/s" and "neither reference pulses", and suggested
`speed → -0.35`, `flicker → 0.35`. Direct measurement disagrees on both counts:
the rise is ~90 px/s by three independent methods, and the per-particle
brightness is a hard on/off pulse. The QA figures look like they came from
nearest-neighbour blob tracking, which on a field of short-lived specks
preferentially links *stationary* false positives (compression flicker on the
reciter's texture) and reports their zero velocity.
