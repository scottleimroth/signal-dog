"""tools/public_clips.py: public recordings mixed into room noise as audio. A made-up folder stands in for ESC-50.
Needs ffmpeg."""
import contextlib
import importlib.util
import io
import os
import shutil
import tempfile
import unittest

import _pkg
import audio

_pkg.load()

TOOLS = _pkg.ROOT / "tools"
HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def load_tool():
    spec = importlib.util.spec_from_file_location("public_clips", TOOLS / "public_clips.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tool = load_tool()


def run(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = tool.main(list(args))
    return code, out.getvalue()


def csv_of(rows):
    return "filename,fold,target,category,esc10,src_file,take\n" + "".join(
        "%s,1,0,%s,False,1,A\n" % (name, cat) for name, cat in rows)


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class PublicClipsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def folder(self, rows, csv=True):
        for name, kw in rows:
            audio.synth(os.path.join(self.dir, name), 4.0, fan=False, **kw)
        if csv:
            with open(os.path.join(self.dir, "esc50.csv"), "w", encoding="utf-8") as fh:
                fh.write(csv_of([(n, c) for (n, _), c in zip(rows, self.cats)]))

    def test_a_knock_and_an_alarm_are_found_and_the_room_alone_raises_nothing(self):
        self.cats = ["door_wood_knock", "clock_alarm"]
        self.folder([("k.wav", dict(knocks=(0.5,))), ("a.wav", dict(beeps=((0.3, 4, 3600),)))])
        code, text = run(self.dir, "--level", "25")
        self.assertEqual(code, 0)
        self.assertIn("Door knocks that raised a knock alert: 1 of 1", text)
        self.assertIn("Alarm-clock clips that raised a beep alert: 1 of 1", text)
        self.assertIn("The room noise alone raised 0 alerts.", text)
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith((".s16", ".frames"))], [])

    def test_a_setting_changes_what_is_found_and_is_printed(self):
        self.cats = ["door_wood_knock"]
        self.folder([("k.wav", dict(knocks=(0.5,)))])
        code, text = run(self.dir, "--set", "thud_db=60.0")
        self.assertEqual(code, 0)
        self.assertIn("thud_db=60.0", text)
        self.assertIn("Door knocks that raised a knock alert: 0 of 1", text)

    def test_a_folder_with_no_list_or_no_door_knocks_is_refused_with_the_usage(self):
        self.assertEqual(run(self.dir)[0], 2)
        self.cats = ["clock_alarm"]
        self.folder([("a.wav", dict(beeps=((0.3, 4, 3600),)))])
        self.assertEqual(run(self.dir)[0], 2)

    def test_a_row_in_the_list_without_its_file_is_skipped(self):
        self.cats = ["door_wood_knock"]
        self.folder([("k.wav", dict(knocks=(0.5,)))])
        with open(os.path.join(self.dir, "esc50.csv"), "a", encoding="utf-8") as fh:
            fh.write("gone.wav,1,0,door_wood_knock,False,1,A\n")
        self.assertIn("Door knocks that raised a knock alert: 1 of 1", run(self.dir)[1])


if __name__ == "__main__":
    unittest.main()
