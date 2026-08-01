# ASR and alignment: what we use, what we rejected, and why

Written after replacing Whisper's word timings with CTC forced alignment
(commit `80df887`). Records the models that were measured and the trade-offs,
so the next person asking "can't we just use a smaller Quran model?" has the
evidence instead of re-running it.

**Current stack:** `transcribe.py` (Whisper large-v3-turbo) for discovery,
`align.py` (Meta MMS CTC forced alignment) for timing. **Decision: keep it.**
Every alternative measured so far loses something load-bearing.

## Three jobs, not one

Conflating these is what makes "just use a smaller model" sound obvious when
it isn't.

1. **Discovery** — *which verses is this?* Needs rough Arabic text. Feeds
   `quran.search()`, which is IDF + contiguity scoring and tolerates badly
   garbled input. **Skippable entirely**: name `surah`/`ayah_start`/`ayah_end`
   in the config and no ASR runs.
2. **Timing** — *when is each mushaf word spoken?* Done by forced alignment
   against text already known byte-exact. No ASR involved; the model only
   decides WHEN, never WHAT, so it cannot hallucinate.
3. **Repeat detection** — *did the reciter break off and restart?* Needs an
   ASR that transcribes the repeated phrase **twice**. This is the job that
   pins us to Whisper, and the one every alternative fails.

Whisper is only needed for 1 and 3. It has nothing to do with 2 any more.

## Measured comparison

Two clips, both in `sources/`. Discovery is scored through the real
`generate.identify_verse_span()` path (chunked search), not a single
whole-clip search — the chunked path is meaningfully more forgiving, and an
early version of this table wrongly failed fastconformer by not using it.

| Option | Faatir 35:31-34 | Maryam 19:88-95 | Repeat | Disk | Runtime |
|---|---|---|---|---|---|
| **Whisper large-v3-turbo + MMS** (current) | PASS | PASS | **PASS** | 5.1 G | mlx + torch |
| `tarteel-ai/whisper-base-ar-quran` | PASS | PASS | impossible | 3.1 G | torch |
| `TBOGamer22/wav2vec2-quran-phonetics` | FAIL | FAIL | n/a | 2.9 G | torch |
| `whisper-small` (generic) | PASS | FAIL | FAIL | 4.1 G | mlx + torch |
| `Muno459/fastconformer-quran` + MMS | PASS | PASS | FAIL | **2.6 G** | onnxruntime + torch |

**Discovery is not the discriminator — repeat detection is.** Three models
handle discovery fine (turbo, Tarteel, fastconformer). Only the
autoregressive one handles a restart. Anyone optimising this pipeline for
size will find discovery easy to replace and will lose repeats silently
unless they test for it specifically.

Two entries in this table were wrong in an earlier draft because discovery
was scored with a single whole-clip `quran.search()` instead of the chunked
`identify_verse_span()` the pipeline actually uses. That method wrongly
failed both Tarteel and fastconformer on Maryam. Score the real path.

### Whisper large-v3-turbo — current, for discovery + repeats

Not the heavyweight option it looks like: 809M params but only 4 decoder
layers, so it runs near medium speed with large accuracy. `whisper-medium` is
about the same size, so there is no smaller generic model that holds the
accuracy.

Its text is *worse* than the Quran-tuned models (it wrote `وردا` for `ولدا`,
`أصارهم معدهم` for `أحصاهم وعدهم`) and discovery still passes, because
`quran.search` is built for lossy input. Text quality is not the binding
constraint — **being autoregressive is**. Being a sequential decoder is
exactly why it emits a restarted phrase twice.

### `tarteel-ai/whisper-base-ar-quran` — rejected

Excellent text, fully diacritized, and it **passes discovery on both clips**
(`Faatir 35:31-34`, `Maryam 19:90-94`) at 556 MB against turbo's 1.5 G. As a
discovery-only replacement it works.

Rejected for one reason: **no timestamps at all.** The fine-tune stripped the
timestamp tokens — `no_timestamps_token_id` is absent and
`return_timestamps=True` raises. That kills `find_repeats` (which needs timed
segments), `whisper.srt`, and the Needleman-Wunsch fallback. There is no
decoding option that recovers them; the tokens are not in the vocabulary.

Its generation config is also monolingual-legacy, so it rejects the
`language=`/`task=` kwargs modern transformers passes.

**Worth revisiting** if repeat detection moves out of ASR (below): it would
then be a straight 1.5 G → 556 MB win, still on torch.

### `TBOGamer22/wav2vec2-quran-phonetics` — rejected

Unusable. Output was romanized gibberish on Faatir and the single character
`y` on Maryam. The repo ships `rng_state.pth` and `trainer_state.json`, i.e.
a raw training checkpoint. Its vocab is Latin phonemes (`ā ī ū ʿ ḍ ḥ ṣ ṭ ẓ`)
rather than Arabic, so `quran.search` could not consume it without a parallel
romanized index over all 6236 ayat.

### `whisper-small` (generic) — rejected

The important negative result: it keeps timestamps but **found zero repeats**
on the Faatir clip where turbo found the restart, and lost Maryam
(An-Nisaa 4:171). Shrinking the model breaks repeat detection.

### `Muno459/fastconformer-quran` — the best rejected option

Genuinely well built, and the closest call. NVIDIA
`stt_ar_fastconformer_hybrid_large_pcd_v1.0` fine-tuned on
`tarteel-ai/everyayah` + `tlog` + `muaalem`. Matches turbo on discovery
(`Faatir 35:31-33`, `Maryam 19:90-95` — the same Maryam range turbo gets) at
**131 MB against 1.5 G**, on `onnxruntime` with **no torch and no mlx**. Ships
a numpy `LogMelExtractor` matching NeMo's preprocessor, a SentencePiece
tokenizer over Arabic (1024 tokens), and its own `ctc_forced_align`.
Quantization is free: q8 (131 MB) output was identical to fp32 (458 MB).

Rejected because **greedy CTC structurally cannot see a restart**. On the
Faatir clip `حَرِيرٌ` was assigned 70.08 → **94.64s** — 24.5 seconds during
which it emitted nothing, swallowing the entire first utterance at
76.72–83.66. It transcribes the phrase once. Confirmed on fp32, so it is not
a quantization artifact. It also truncates words (`الْدُ` for `الْحَمْدُ`).

Gated (`gated: auto`) and licensed `npl-1.0`, not a standard OSS licence —
worth reading before any monetised use.

**Still the best candidate if repeat detection moves out of ASR** (below).

### `yayaiu6/Real-Time-Quran-recitation-tracker-System` — not applicable

A memorization trainer, not alignment technology. Its "alignment" is
Levenshtein + Needleman-Wunsch over text, which is what
`generate.align_words()` already does; its word times still come from ASR
timestamps, the exact weakness forced alignment removed. Its "repetition
detection" tracks a live reciter's position on a page, not caption timing.
MIT, so readable for ideas.

## Cost, honestly

`transcribe.py` and `align.py` are **separate processes run at different
times**, so both models are never resident at once. Peak RAM is already one
model. The real cost is disk and install complexity, not memory:

| | venv | weights |
|---|---|---|
| `tools/asr-venv` | 1.1 G | turbo 1.5 G |
| `tools/align-venv` | 1.3 G | MMS 1.2 G |

Naming the verse span in the config already avoids the ASR half at run time.

## The open item: repeat detection without ASR

Repeat detection is the only remaining tie to Whisper. If it moved into the
CTC alignment, Whisper would become purely optional discovery, and
fastconformer's 131 MB (or nothing at all) could replace it.

**Sketch:** after aligning, find a large gap where no reference word was
claimed — on the Faatir clip, `حرير` ends at 70.08 and the next word is not
until 85.40. Re-run CTC alignment of *only the phrase that follows* against
*only that window*. A good score means the phrase really was spoken there, so
it is a restart; pull the caption start back.

**It must re-align acoustically — an energy or silence test will not work.**
Measured on extracted WAVs:

| Window | Level |
|---|---|
| Maryam pause-gap 21.52-25.18 | −20.0 dB |
| Maryam recitation 27.5-31.5 | −20.8 dB |
| Faatir restart-gap 70.5-83.5 | −18.2 dB |
| Faatir recitation 90-100 | −18.7 dB |

A genuine pause and a genuine restart sit at the **same level as recitation**
in these reverberant masjid recordings. `silencedetect` cannot separate them;
only asking "does this phrase's text match this audio?" can.

(Beware `-ss`/`-to` placed before `-i` with `volumedetect` — it silently
measured the whole file during this investigation and produced two wrong
conclusions in a row. Extract the window to a WAV and check its duration.)

Also untested: fastconformer as the *aligner*, replacing MMS. That would be
one 131 MB model with no torch anywhere, ~2.5 G → ~200 MB. The catch is 80 ms
frame resolution against MMS's 20 ms — 4× coarser, though still far tighter
than the 1–2 s errors that started this work.

## Reproducing

Discovery is scored with `generate.identify_verse_span()` over each model's
words; repeat detection by looking for consecutive identical segments
(Whisper) or a repeated n-gram in the word stream (CTC models). The two test
clips are `sources/ahmad-alarabi/` (has an ibtidāʾ restart) and
`sources/ansari-efore-end-maryam/` (no restart; the discriminator for model
quality). Any replacement must pass both, and must reproduce **76.72s** as
the start of card 8 on the Faatir clip.
