"""Tests for the tool-resolution layer (`qc.env`, `qc.config`, `qc.author.asr`).

These are the pieces that decide which binary and which interpreter every stage
shells out to. They used to be `/opt/homebrew/...` literals, so the whole render
path and the golden suite were unreachable off Apple silicon; the point of these
tests is that the ORDER of resolution stays as documented, since a regression
there is invisible on the machine that happens to have everything on PATH.

No ffmpeg is invoked and nothing is downloaded.
"""
import os
import platform
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qc import config, env                                    # noqa: E402
from qc.author import asr                                     # noqa: E402


class EnvVarNames(unittest.TestCase):
    def test_simple_tool(self):
        self.assertEqual(env.env_var("ffmpeg"), "QC_FFMPEG")

    def test_hyphenated_tool_uses_a_shell_exportable_name(self):
        # QC_YT-DLP is not a legal shell identifier, so it must be aliased.
        self.assertEqual(env.env_var("yt-dlp"), "QC_YT_DLP")
        self.assertNotIn("-", env.env_var("yt-dlp"))


class Resolution(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        config.reset()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        config.reset()

    def test_env_var_wins_over_path(self):
        os.environ["QC_FFMPEG"] = "/custom/ffmpeg"
        self.assertEqual(env.find("ffmpeg"), "/custom/ffmpeg")

    def test_env_var_pointing_nowhere_is_an_error_not_a_fallback(self):
        # Silently falling through to a different ffmpeg would change the encode
        # under a pinned build and drift the golden md5s for no visible reason.
        os.environ["QC_FFMPEG"] = "/nonexistent/ffmpeg"
        with self.assertRaises(SystemExit) as cm:
            env.require("ffmpeg")
        self.assertIn("does not exist", str(cm.exception))

    def test_config_beats_path_and_env_beats_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write('[tools]\nffmpeg = "/from/config"\n')
            cfg = fh.name
        try:
            os.environ["QC_CONFIG"] = cfg
            config.reset()
            self.assertEqual(env.find("ffmpeg"), "/from/config")
            os.environ["QC_FFMPEG"] = "/from/env"
            self.assertEqual(env.find("ffmpeg"), "/from/env")
        finally:
            os.unlink(cfg)

    def test_missing_tool_message_names_the_knobs(self):
        os.environ["PATH"] = ""
        with self.assertRaises(SystemExit) as cm:
            env.require("definitely-not-a-real-tool")
        msg = str(cm.exception)
        self.assertIn("QC_DEFINITELY_NOT_A_REAL_TOOL", msg)
        self.assertIn("PATH", msg)

    def test_interpreter_falls_back_to_the_running_python(self):
        # A single-venv install has no tools/<n>-venv; it must still resolve to
        # something executable rather than a path that does not exist.
        got = env.interpreter("render")
        self.assertTrue(os.path.exists(got), got)

    def test_interpreter_override(self):
        os.environ["QC_RENDER_PYTHON"] = "/opt/weird/python"
        self.assertEqual(env.interpreter("render"), "/opt/weird/python")

    def test_describe_covers_every_tool_and_interpreter(self):
        names = [r[0] for r in env.describe()]
        for expect in ("ffmpeg", "ffprobe", "yt-dlp", "claude",
                       "render python", "author python", "asr python"):
            self.assertIn(expect, names)


class Config(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        config.reset()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        config.reset()

    def test_absent_config_is_empty_not_an_error(self):
        os.environ["QC_CONFIG"] = "/nonexistent/qc.toml"
        config.reset()
        self.assertEqual(config.load(), {})
        self.assertIsNone(config.get("tools", "ffmpeg"))

    def test_malformed_config_fails_loudly(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write("[tools\nbroken")
            bad = fh.name
        try:
            os.environ["QC_CONFIG"] = bad
            config.reset()
            with self.assertRaises(SystemExit):
                config.load()
        finally:
            os.unlink(bad)


class AsrBackend(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        config.reset()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        config.reset()

    def test_default_tracks_the_platform(self):
        expect = "mlx" if (platform.system() == "Darwin"
                           and platform.machine() == "arm64") else "faster"
        self.assertEqual(asr.default_backend(), expect)

    def test_env_override(self):
        os.environ["QC_ASR_BACKEND"] = "faster"
        self.assertEqual(asr.backend(), "faster")

    def test_unknown_backend_is_rejected_with_the_known_set(self):
        os.environ["QC_ASR_BACKEND"] = "wav2vec"
        with self.assertRaises(SystemExit) as cm:
            asr.backend()
        self.assertIn("mlx", str(cm.exception))
        self.assertIn("faster", str(cm.exception))

    def test_every_backend_has_a_model_and_a_snippet(self):
        for name in asr.MODELS:
            self.assertIn(name, asr._SNIPPETS)
            self.assertTrue(asr.model_for(name))

    def test_snippets_emit_the_contract_keys(self):
        # align's DP reads words[].w/t0/t1 and the backend tag; a snippet that
        # stops writing one of those breaks alignment far from here.
        for name, src in asr._SNIPPETS.items():
            for key in ('"w"', '"t0"', '"t1"', '"backend"'):
                self.assertIn(key, src, "%s snippet lacks %s" % (name, key))

    def test_model_override_applies_to_any_backend(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write('[asr]\nmodel = "my/own-ct2"\n')
            cfg = fh.name
        try:
            os.environ["QC_CONFIG"] = cfg
            config.reset()
            self.assertEqual(asr.model_for("faster"), "my/own-ct2")
        finally:
            os.unlink(cfg)


class NoHardcodedPrefixes(unittest.TestCase):
    """The regression this whole layer exists to prevent.

    A new `/opt/homebrew/...` literal in an executable path re-pins the repo to
    one machine. Prose may mention it (the README documents the macOS install),
    so only code is scanned, and `qc/env.py` legitimately holds it as a hint.
    """

    def test_no_absolute_homebrew_paths_in_code(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        allowed = {os.path.join(root, "qc", "env.py")}
        offenders = []
        for sub in ("qc", "scripts", "bin"):
            for dirpath, _, files in os.walk(os.path.join(root, sub)):
                if "__pycache__" in dirpath:
                    continue
                for fn in files:
                    p = os.path.join(dirpath, fn)
                    if p in allowed or not fn.endswith((".py", "")):
                        continue
                    try:
                        with open(p, encoding="utf-8") as fh:
                            text = fh.read()
                    except (UnicodeDecodeError, IsADirectoryError):
                        continue
                    for n, line in enumerate(text.splitlines(), 1):
                        if "/opt/homebrew" not in line:
                            continue
                        stripped = line.strip()
                        # A comment or docstring line explaining the history is
                        # fine; an assignment or an argv entry is not.
                        if stripped.startswith("#"):
                            continue
                        if "=" in stripped and "/opt/homebrew" in stripped.split("=", 1)[1]:
                            offenders.append("%s:%d %s" % (os.path.relpath(p, root), n, stripped))
        self.assertEqual(offenders, [], "hardcoded Homebrew paths:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
