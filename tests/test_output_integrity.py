"""Tests for the output integrity check (`qc.check._decode_errors`).

The premise, demonstrated rather than asserted: ffprobe METADATA survives
corruption of the media payload. A file whose H.264 data has been overwritten
still reports its original dimensions, frame rate and duration, so every
metadata-based assertion in `check --output` passes on a file that will not
play. `_decode_errors` is the only check that sees it.

This matters because the failure mode is real and silent: two processes writing
one output path produce exactly this file, and a reel shipped that way looks
fine in every report until someone presses play.

Needs ffmpeg + ffprobe; skipped when they are not resolvable.
"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qc import env                                            # noqa: E402

HAVE_FFMPEG = bool(env.find("ffmpeg")) and bool(env.find("ffprobe"))


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg + ffprobe")
class DecodeIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qc import check
        cls.check = check
        cls.tmp = tempfile.mkdtemp(prefix="qc-decode-")
        cls.good = os.path.join(cls.tmp, "good.mp4")
        subprocess.run(
            [env.require("ffmpeg"), "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", cls.good],
            check=True)
        # Overwrite a slice of the MEDIA payload and leave the header and moov
        # atom intact -- the shape a second writer produces.
        cls.bad = os.path.join(cls.tmp, "bad.mp4")
        with open(cls.good, "rb") as fh:
            data = bytearray(fh.read())
        start = len(data) // 3
        data[start:start + len(data) // 5] = os.urandom(len(data) // 5)
        with open(cls.bad, "wb") as fh:
            fh.write(bytes(data))

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_clean_file_has_no_decode_errors(self):
        self.assertEqual(self.check._decode_errors(self.good), [])

    def test_corrupt_file_is_caught(self):
        errs = self.check._decode_errors(self.bad)
        self.assertTrue(errs, "corrupt bitstream reported no decode errors")

    def test_metadata_alone_does_not_notice(self):
        """The justification for the whole check: ffprobe still says it is fine."""
        out = subprocess.run(
            [env.require("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", self.bad],
            capture_output=True, text=True).stdout.strip()
        self.assertTrue(out.startswith("320,240"),
                        "expected intact metadata on the corrupt file, got %r" % out)
        # ... and yet:
        self.assertTrue(self.check._decode_errors(self.bad))

    def test_frame_count_reads_decodable_frames(self):
        n = self.check._frame_count(self.good)
        self.assertIsNotNone(n)
        self.assertLessEqual(abs(int(n or 0) - 60), 1)   # 2s x 30fps

    def test_frame_count_survives_a_broken_file_without_raising(self):
        # It may return a number or None; it must not blow up, because
        # check_output calls it while assembling a report.
        self.check._frame_count(self.bad)


if __name__ == "__main__":
    unittest.main()
