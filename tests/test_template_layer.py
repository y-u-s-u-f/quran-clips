"""Tests for the template/config layer that `check` and the renderers share.

Two things are load-bearing and easy to break silently:

  * `load_yaml` is CACHED (it was 50% of `qc check` runtime). A cache that misses
    an edit, or that hands out a shared mutable object, is far worse than the
    parse cost it saves.
  * the CANVAS and the FONTS come from the style template. They used to be
    hardcoded in `scripts/render_text.py` while `qc/check.py` read the template,
    so the renderer and the checker disagreed about the frame whenever a template
    was edited.
"""
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import render_text as rt                                       # noqa: E402


class YamlCache(unittest.TestCase):
    def setUp(self):
        rt._YAML_CACHE.clear()

    def _write(self, text):
        fh = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        fh.write(text)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_repeat_reads_are_equal(self):
        p = self._write("a: 1\nb: [2, 3]\n")
        self.assertEqual(rt.load_yaml(p), rt.load_yaml(p))

    def test_second_read_is_served_from_cache(self):
        p = self._write("a: 1\n")
        rt.load_yaml(p)
        self.assertEqual(len(rt._YAML_CACHE), 1)
        rt.load_yaml(p)
        self.assertEqual(len(rt._YAML_CACHE), 1)

    def test_an_edit_invalidates_it(self):
        """The edit-then-recheck loop must not see stale values."""
        p = self._write("a: 1\n")
        self.assertEqual(rt.load_yaml(p)["a"], 1)
        time.sleep(0.01)                      # ensure a distinct mtime_ns
        with open(p, "w") as fh:
            fh.write("a: 2\n")
        self.assertEqual(rt.load_yaml(p)["a"], 2)

    def test_caller_mutation_cannot_poison_the_cache(self):
        """The classic failure of a parse cache: one caller mutates the shared
        dict and every later caller sees the damage."""
        p = self._write("a: 1\nnested: {k: v}\n")
        first = rt.load_yaml(p)
        first["a"] = 999
        first["nested"]["k"] = "clobbered"
        second = rt.load_yaml(p)
        self.assertEqual(second["a"], 1)
        self.assertEqual(second["nested"]["k"], "v")

    def test_missing_file_still_raises(self):
        with self.assertRaises(OSError):
            rt.load_yaml(os.path.join(ROOT, "no", "such", "file.yaml"))


class CanvasFromTemplate(unittest.TestCase):
    """The renderer and the checker must agree, and they only can if both read
    the template."""

    def test_shipped_styles_report_their_real_orientation(self):
        default = rt.load_yaml(os.path.join(ROOT, "templates", "style.yaml"))
        bars = rt.load_yaml(os.path.join(ROOT, "templates", "bars.yaml"))
        self.assertEqual(rt.canvas_of(default), (1920, 1080))   # landscape
        self.assertEqual(rt.canvas_of(bars), (1080, 1920))      # vertical

    def test_template_drives_the_canvas(self):
        self.assertEqual(rt.canvas_of({"canvas": {"width": 720, "height": 1280}}),
                         (720, 1280))

    def test_a_missing_or_bad_canvas_falls_back(self):
        for bad in ({}, {"canvas": {}}, {"canvas": {"width": 0, "height": 0}},
                    {"canvas": {"width": "x", "height": "y"}}, None):
            self.assertEqual(rt.canvas_of(bad), rt.DEFAULT_CANVAS)


class FontsFromTemplate(unittest.TestCase):
    def test_defaults_when_the_template_names_a_family(self):
        # templates/style.yaml names "KFGQPC Uthmanic Hafs", a family rather than
        # a file, so the built-in path must stand.
        ar, en = rt.font_paths(rt.load_yaml(
            os.path.join(ROOT, "templates", "style.yaml")))
        self.assertTrue(ar.endswith(".ttf") and os.path.exists(ar))
        self.assertTrue(en.endswith(".ttf") and os.path.exists(en))

    def test_a_template_path_is_honoured(self):
        style = {"arabic": {"font": "assets/fonts/AM_Thulth_Regular_0.1.ttf"}}
        ar, _ = rt.font_paths(style)
        self.assertTrue(ar.endswith("AM_Thulth_Regular_0.1.ttf"))

    def test_a_nonexistent_path_does_not_break_rendering(self):
        ar, _ = rt.font_paths({"arabic": {"font": "assets/fonts/nope.ttf"}})
        self.assertEqual(ar, rt.AR_FONT_DEFAULT)

    def test_no_style_gives_the_defaults(self):
        self.assertEqual(rt.font_paths(None),
                         (rt.AR_FONT_DEFAULT, rt.EN_FONT_DEFAULT))


if __name__ == "__main__":
    unittest.main()
