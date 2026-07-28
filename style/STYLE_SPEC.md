# Quran-Clip Style Spec (Agent A output)

Derived **only** from the three reference reels below (downloaded to
`~/quran-clips/style/refs/`). No other library was consulted.

| Ref | File | Native res | fps | Dur | Audio | Content |
|-----|------|-----------|-----|-----|-------|---------|
| DZcVeORugrZ | ig_DZcVeORugrZ.mp4 | 1920×960 (2:1) | 30 | 21.9s | AAC 44.1k stereo | Al-Kahf 18:107–108, elderly sheikh, indoor, blue-lit wall |
| DXQZ2JFEvvR | ig_DXQZ2JFEvvR.mp4 | 1280×718 (16:9) | 30 | 19.5s | AAC 44.1k stereo | As-Saffat 37:180–182, Prophet's Mosque (green/gold) |
| DXLVk82Eqyj | ig_DXLVk82Eqyj.mp4 | 1276×720 (16:9) | 30 | 29.2s | AAC 44.1k stereo | Ar-Ra'd 13:28, mosque podium w/ gold mics |

> **Important framing fact.** All three references are **landscape** (16:9 / 2:1)
> single-camera source clips — these are feed `/p/` posts, not native vertical
> reels. The style is fundamentally a *landscape composition* with text laid over
> the emptier side of the frame. There is **no** split-screen compositing, no
> reciter cut-out, no picture-in-picture — the "blurred left half" in
> DZcVeORugrZ is just the shallow-depth-of-field background of the same room.
>
> The account's editing style is fully defined below in **frame-fraction** terms
> (resolution-independent, authoritative). `templates/style.yaml` additionally
> projects those onto a **1080×1920** canvas using the standard blurred-pad
> vertical reframing, since that is what was requested — but reproducing the clip
> **natively landscape** is the higher-fidelity option and both are supported.

---

## Canvas & source framing
- **Native:** landscape, 30 fps, ~20–30 s. Source is a stable single shot; the
  reciter sits/stands to one side of frame.
- **Framing rule:** text always goes on the **side of the frame opposite the
  reciter's face**, over the less-busy / darker area.
  - DZcVeORugrZ: reciter on **right**, text on **left** (block center ≈ x0.31).
  - DXQZ2JFEvvR & DXLVk82Eqyj: reciter on **left**, text on **right**
    (block center ≈ x0.68–0.72).
- **For a 1080×1920 reel** (reframing recommendation): scale the source to full
  width (1080 → 16:9 band = 1080×608) and center it as a horizontal band; fill
  the top/bottom margins with a **blurred, darkened copy** of the same footage
  (Gaussian blur ≈ sigma 40, brightness ≈ −45 %). No synthetic zoom/pan.

## Color treatment
- Minimal color grading — scene colors kept largely natural (DZcVeORugrZ reads
  cool/blue from its practical lighting; the mosque clips read warm/neutral).
- **Overall slight dim** + **soft vignette** (edges ≈ −30 to −40 % luminance) to
  seat the white text.
- Where the text sits over a bright background (DXLVk82Eqyj), a **mild dark
  gradient** (~−25 %) sits behind the text region for legibility. This is subtle,
  not a hard box.

## Arabic ayah text
- **Font:** KFGQPC Uthmanic Hafs (v22) — confirmed by the Uthmani glyph shapes,
  full tashkeel, and the ayah-end medallion glyph. Sanity-check ✅.
- **Color:** white, very slightly warm off-white (~#F7F5EF).
- **Position:** vertically near the middle of the frame; block vertical center
  ≈ **y 0.48–0.52 H**. Horizontal center on the text side (≈ x0.31 or x0.68–0.72).
- **Size:** base glyph height ≈ **0.047 H** (≈ 0.023 W); the Arabic line runs
  ~0.45–0.55 W wide for a typical 5–7 word phrase. Single line per phrase (never
  wrapped — phrase length is chosen to fit one line).
- **Line handling / timing granularity:** **per-phrase / per-ayah swap.** One
  meaningful segment shows at a time — sometimes a partial ayah (DZcVeORugrZ
  splits 18:107 across two phrases), sometimes a whole short ayah (37:181). Text
  is synced to the recitation and swaps at the next phrase.
- **Ayah-end medallion:** the decorative end-of-verse circle with Arabic-Indic
  numerals (۱۸۰, ۲۸, …) is shown **only when a full ayah completes** at the end of
  that phrase; mid-ayah phrases have no number. It is part of the mushaf glyph
  run, placed inline at the end (leading edge) of the Arabic line.
- **No** outline/stroke, **no** box. Subtle **drop shadow** only (see below).

## English translation
- **Font:** Albertus MT Lt, rendered in **small-caps** (full-height initial cap
  per word + petite caps for the rest), slightly loose tracking. Sanity-check ✅
  (glyphic flared terminals match Albertus).
- **Position:** directly **below** the Arabic, **center-aligned to the same
  vertical axis** as the Arabic block. Gap ≈ 0.02 H.
- **Size:** cap height ≈ **0.023 H** (≈ half the Arabic base height).
- **Color:** same white as Arabic; often rendered a touch dimmer/lighter weight.
- **Casing:** all small-caps. Uses typographic em-dashes ("LORD—THE LORD") and a
  macron on ALLĀH.
- **Chunking:** matches the Arabic phrase; wraps to **1–2 centered lines** as
  needed (2 lines when long, e.g. "WHEREIN THEY ABIDE ETERNALLY; …").

## Reciter photo / name
- **None.** The reciter is simply the live footage. No circular inset, no border,
  **no reciter-name caption** anywhere.

## Other overlays
- **Surah:ayah reference caption:** none (no "18:107"-style text; only the native
  mushaf ayah medallion).
- **Watermark / handle:** none visible.
- **Progress bar / borders / emoji:** none.

## Motion
- **Text enter/exit:** **crossfade / dissolve**, ~**0.40–0.50 s**. The outgoing
  phrase fades out while the incoming fades in, **at a fixed position** (no slide,
  no scale, no per-word reveal). Verified in the DZcVeORugrZ 6.8→7.6 s and
  DXLVk82Eqyj 9.0→9.4 s bursts.
- **Background motion:** native footage only — stable framing, no Ken-Burns /
  zoompan added.
- **Text shadow:** soft drop shadow, offset ≈ 2 px, blur ≈ 5 px, ~60 % black.

## Audio
- **Integrated loudness:** measured **−14.2 / −14.2 / −14.4 LUFS** across the
  three refs → clearly mastered to the **−14 LUFS** streaming/IG target.
- **True peak:** varies (−0.9 to −6.9 dBTP); keep ceiling ≤ **−1 dBTP**.
- **LRA:** 1.1–7.8 LU (narrow, typical of recitation).
- **Reverb/EQ:** natural room/mosque acoustics only — **no artificial
  reverb/echo** and no lo-fi/telephone EQ detected. Full-range, natural tone.
  Preserve source; do not add echo.
- **Fades:** short fade-in ≈ 0.3 s, fade-out ≈ 0.5 s at clip boundaries.

## Fixed template vs per-clip variables
**Fixed:** fonts (KFGQPC Uthmanic Hafs + Albertus MT Lt small-caps); white text;
English centered below Arabic; per-phrase crossfade (~0.45 s); soft drop shadow,
no outline/box; dim + vignette; −14 LUFS audio; no watermark/name/reference/
progress bar.
**Per-clip:** the source footage; **text side** (left vs right, opposite the
reciter); exact vertical nudge (≈ ±0.04 H); number of English lines (1–2);
whether an ayah medallion appears (only on full-ayah completion); native color
tone of the footage.

---

## ⇒ Word-level karaoke vs per-phrase swap?
**Per-phrase (per-segment) text swap with crossfades is SUFFICIENT.**
None of the three references use word-level highlighting/karaoke — the whole
phrase appears and swaps as a unit. Agent B does **not** need libass/full-ffmpeg
karaoke rendering; simple timed overlay swaps with ~0.45 s opacity crossfades
reproduce the style.
