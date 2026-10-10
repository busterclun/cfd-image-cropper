from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image

from gif_export import create_gif
from main import CropperApp, find_images


class GifTests(unittest.TestCase):
    def test_order_timing_resize_loop_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            colors = {"1.png": "red", "2.png": "green", "10.jpg": "blue"}
            for name, color in colors.items():
                with Image.new("RGB", (100, 60), color) as im:
                    im.save(folder / name)
            before = {p.name: p.read_bytes() for p in find_images(folder)}
            (folder / "动画.gif").write_bytes(b"previous result")
            output, count, issues = create_gif(folder, find_images(folder), 105, 50)
            self.assertEqual((count, issues), (3, []))
            self.assertEqual(output.name, "动画_2.gif")
            with Image.open(output) as gif:
                self.assertEqual(gif.n_frames, 3)
                self.assertEqual(gif.size, (50, 30))
                self.assertEqual(gif.info["loop"], 0)
                for index, channel in enumerate((0, 1, 2)):
                    gif.seek(index)
                    self.assertEqual(gif.info["duration"], 100)
                    with gif.convert("RGB") as rgb:
                        color = rgb.getpixel((0, 0))
                        self.assertEqual(color.index(max(color)), channel)
            self.assertEqual((folder / "动画.gif").read_bytes(), b"previous result")
            self.assertEqual(before, {p.name: p.read_bytes() for p in find_images(folder)})

    def test_bad_frames_transparency_and_identical_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "0.png").write_bytes(b"bad")
            for name in ("1.png", "2.png"):
                with Image.new("RGBA", (40, 20), (0, 0, 0, 0)) as im:
                    im.save(folder / name)
            with Image.new("RGB", (20, 20)) as im:
                im.save(folder / "3.png")
            output, count, issues = create_gif(folder, find_images(folder), 200, 960)
            self.assertEqual(count, 2)
            self.assertEqual([name for name, _ in issues], ["0.png", "3.png"])
            with Image.open(output) as gif:
                self.assertEqual(gif.n_frames, 2)
                self.assertEqual(gif.size, (40, 20))
                for i in range(2):
                    gif.seek(i)
                    with gif.convert("RGB") as rgb:
                        self.assertEqual(rgb.getpixel((0, 0)), (255, 255, 255))

    def test_insufficient_frames_and_invalid_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with Image.new("RGB", (50, 40)) as im:
                im.save(folder / "1.png")
            for duration, width in ((0, 960), (100, -1), (100, 0)):
                with self.assertRaises(ValueError):
                    create_gif(folder, find_images(folder), duration, width)
                self.assertEqual(list(folder.glob("*.gif")), [])

    def test_gui_background_gif(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for i, color in enumerate(("red", "blue")):
                with Image.new("RGB", (80, 60), color) as im:
                    im.save(folder / f"{i}.png")
            root = tk.Tk()
            app = CropperApp(root, preset_path=folder / "settings.json")
            try:
                root.update()
                with patch.object(app, "show_result") as result:
                    app.start_gif(folder, find_images(folder), 100, 0)
                    deadline = time.monotonic() + 10
                    while app.busy and time.monotonic() < deadline:
                        root.update()
                        time.sleep(.01)
                    self.assertFalse(app.busy)
                    self.assertEqual(result.call_args.kwargs, {"gif": True})
                    self.assertTrue(result.call_args.args[0].exists())
                app.show_result(folder / "动画.gif", 2, [], gif=True)
                root.update()
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
