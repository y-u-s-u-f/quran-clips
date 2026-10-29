"""Authoring checks that must fail before backend imports or config writes."""
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "pipeline"))
import align
import generate as G


class ConfigChecks(unittest.TestCase):
    def test_discovered_group_total_before_write(self):
        raw = ("input: /unused.mp4\ntrim: [0, 1]\n"
               "groups: [{n_words: 1}]\n")
        with tempfile.TemporaryDirectory(prefix="zzreva-config-") as tmp:
            path = pathlib.Path(tmp) / "zzreva-discovered.yaml"
            path.write_text(raw)
            with patch.object(G, "load_whisper_window", return_value=([], None)), \
                 patch.object(G, "require_whisper", return_value="unused"), \
                 patch.object(G, "identify_verse_span", return_value={
                     "surah": 49, "ayah_start": 4, "ayah_end": 5}), \
                 patch.object(align, "aligner", side_effect=AssertionError(
                     "validation must precede backend loading")):
                with self.assertRaisesRegex(ValueError, "groups cover"):
                    align.align(str(path))
            self.assertEqual(path.read_text(), raw)


if __name__ == "__main__":
    unittest.main()
