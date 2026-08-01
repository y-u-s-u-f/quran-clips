#!/usr/bin/env python3
"""
Unified transcription CLI: routes to the right ASR model by language.

Models:
  --lang en       -> faster-whisper large-v3-turbo (English)
  --lang ar       -> CohereLabs/cohere-transcribe-arabic-07-2026 (general Arabic)
  --lang quran    -> tarteel-ai/whisper-base-ar-quran + verse lookup (Qur'an recitation)

Usage:
  python3 transcribe.py --lang en audio.wav
  python3 transcribe.py --lang ar audio.wav
  python3 transcribe.py --lang quran audio.wav
"""
import argparse
import sys
from pathlib import Path


def transcribe_english(audio_path: str) -> str:
    from faster_whisper import WhisperModel
    model = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")
    segments, info = model.transcribe(audio_path, language="en")
    return " ".join(seg.text.strip() for seg in segments)


def transcribe_arabic(audio_path: str) -> str:
    from transformers import AutoProcessor, CohereAsrForConditionalGeneration
    from transformers.audio_utils import load_audio

    processor = AutoProcessor.from_pretrained("CohereLabs/cohere-transcribe-arabic-07-2026")
    model = CohereAsrForConditionalGeneration.from_pretrained(
        "CohereLabs/cohere-transcribe-arabic-07-2026", device_map="auto"
    )
    audio = load_audio(audio_path, sampling_rate=16000)
    inputs = processor(audio, sampling_rate=16000, return_tensors="pt", language="ar")
    inputs.to(model.device, dtype=model.dtype)
    outputs = model.generate(**inputs, max_new_tokens=256)
    return processor.decode(outputs, skip_special_tokens=True)


def transcribe_quran(audio_path: str, window_ayahs: int = 2):
    from transformers import WhisperProcessor, WhisperForConditionalGeneration
    import librosa

    sys.path.insert(0, str(Path.home() / ".hermes" / "data" / "quran"))
    from verse_lookup import match_verse

    model_id = "tarteel-ai/whisper-base-ar-quran"
    processor = WhisperProcessor.from_pretrained(model_id)
    model = WhisperForConditionalGeneration.from_pretrained(model_id)

    audio, sr = librosa.load(audio_path, sr=16000)
    inputs = processor(audio, sampling_rate=16000, return_tensors="pt")
    predicted_ids = model.generate(inputs.input_features, max_new_tokens=256)
    raw_text = processor.batch_decode(predicted_ids, skip_special_tokens=True)[0]
    # strip special tokens like <|ar|><|transcribe|><|notimestamps|> if present
    text = raw_text.split("|>")[-1].strip() if "|>" in raw_text else raw_text.strip()

    matches = match_verse(text, top_k=3, window_ayahs=window_ayahs)
    return text, matches


def main():
    parser = argparse.ArgumentParser(description="Unified ASR transcription CLI")
    parser.add_argument("audio", help="Path to audio file (wav/mp3/etc)")
    parser.add_argument("--lang", required=True, choices=["en", "ar", "quran"], help="Language/model to use")
    parser.add_argument("--window-ayahs", type=int, default=2, help="Quran mode only: max ayahs to try matching as a span")
    args = parser.parse_args()

    if not Path(args.audio).exists():
        print(f"Error: file not found: {args.audio}", file=sys.stderr)
        sys.exit(1)

    if args.lang == "en":
        text = transcribe_english(args.audio)
        print(text)
    elif args.lang == "ar":
        text = transcribe_arabic(args.audio)
        print(text)
    elif args.lang == "quran":
        text, matches = transcribe_quran(args.audio, window_ayahs=args.window_ayahs)
        print(f"Transcript: {text}\n")
        print("Verse matches:")
        for m in matches:
            print(f"  [{m['score']:.3f}] {m['span']} ({m['surah_name_en']}): {m['text']}")


if __name__ == "__main__":
    main()
