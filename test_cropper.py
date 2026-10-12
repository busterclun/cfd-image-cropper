"""运行：python -m unittest -v。所有测试图片均在临时目录中生成。"""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import tkinter as tk

from PIL import Image, ImageTk
from main import CropperApp, batch_crop, display_to_pixel, find_images, fit_image, write_saved_region, resize_with_aspect


class CropTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.preset_path = Path(temporary.name) / "settings" / "saved_region.json"
        preset_patch = patch("main.default_preset_path", return_value=self.preset_path)
        preset_patch.start()
        self.addCleanup(preset_patch.stop)

    def test_coordinates(self):
        self.assertEqual(display_to_pixel(100, 100, (0, 0), (960, 540), (1920, 1080)), (200, 200))
        self.assertEqual(display_to_pixel(500, 400, (0, 0), (960, 540), (1920, 1080)), (1000, 800))
        self.assertEqual(display_to_pixel(-100, 900, (10, 20), (960, 540), (1920, 1080)), (0, 1080))
        size = (3841, 2161)
        shown = fit_image(size, (937, 501))
        self.assertEqual(display_to_pixel(shown[0] + 10, shown[1] + 20, (10, 20), shown, size), size)

    def test_reference_image_dimensions_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            reference = Path(tmp) / "reference.png"
            root = tk.Tk()
            app = CropperApp(root)
            try:
                root.update()
                app.original = Image.new("RGB", (1920, 1080))
                app.folder, app.files = Path(tmp), [reference]
                app.render_image()
                for size, expected in (((640, 480), (640, 480)), ((3840, 2160), (1920, 1080))):
                    with Image.new("RGB", size) as im:
                        im.save(reference)
                    with patch("main.filedialog.askopenfilename", return_value=str(reference)), patch("main.messagebox.showinfo") as info:
                        app.import_reference_image()
                    self.assertEqual((app.box[2]-app.box[0], app.box[3]-app.box[1]), expected)
                    self.assertEqual(app.saved_region["box"], app.box)
                    self.assertTrue(app.aspect_locked.get())
                    self.assertEqual(app.aspect_choice.get(), "4:3" if size[0] == 640 else "16:9")
                    if size[0] == 3840:
                        self.assertIn("等比例缩小", info.call_args.args[1])
                before = self.preset_path.read_bytes()
                reference.write_bytes(b"broken")
                with patch("main.filedialog.askopenfilename", return_value=str(reference)), patch("main.messagebox.showerror") as error:
                    app.import_reference_image()
                    error.assert_called_once()
                self.assertEqual(self.preset_path.read_bytes(), before)
            finally:
                app.close()
            root = tk.Tk()
            app = CropperApp(root)
            try:
                app.original = Image.new("RGB", (1920, 1080))
                app.render_image()
                app.apply_saved_region()
                self.assertEqual(app.box, (0, 0, 1920, 1080))
                self.assertEqual(app.aspect_ratio, (16, 9))
            finally:
                app.close()

    def test_editable_dimensions_and_ratio(self):
        root = tk.Tk()
        app = CropperApp(root)
        try:
            root.update()
            app.original = Image.new("RGB", (1920, 1080))
            app.folder, app.files = Path("."), [Path("1.png")]
            app.box = (1500, 800, 1800, 1000)
            app.render_image()
            self.assertEqual((app.width_text.get(), app.height_text.get()), ("300", "200"))
            app.width_text.set("800")
            app.height_text.set("600")
            root.geometry("800x600")
            root.update()
            app.render_image()
            self.assertEqual(app.width_text.get(), "800")
            self.assertTrue(app.apply_typed_size())
            self.assertEqual(app.box, (1120, 480, 1920, 1080))
            app.aspect_choice.set("16:9")
            app.select_aspect_preset()
            app.width_text.set("640")
            self.assertTrue(app.apply_typed_size())
            self.assertEqual((app.width_text.get(), app.height_text.get()), ("640", "360"))
            app.height_text.set("450")
            self.assertTrue(app.check_ready())
            self.assertEqual((app.width_text.get(), app.height_text.get()), ("800", "450"))
            app.aspect_button.invoke()
            before = app.box
            for invalid in ("", "0", "-10", "12.5", "abc", "2000"):
                app.width_text.set(invalid)
                with patch("main.messagebox.showwarning") as warning:
                    self.assertFalse(app.apply_typed_size())
                    warning.assert_called_once()
                self.assertEqual(app.box, before)
            app.reset_selection()
            app.width_text.set("400")
            app.height_text.set("300")
            self.assertTrue(app.apply_typed_size())
            self.assertEqual(app.box, (760, 390, 1160, 690))
        finally:
            app.close()

    def test_typed_dimensions_used_for_batch_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with Image.new("RGB", (80, 60), "white") as source:
                source.save(folder / "1.png")
            root = tk.Tk()
            app = CropperApp(root)
            try:
                root.update()
                with patch("main.filedialog.askdirectory", return_value=tmp):
                    app.select_folder()
                app.width_text.set("31")
                app.height_text.set("23")
                with patch.object(app, "show_result") as result:
                    app.start_batch()
                    deadline = time.monotonic() + 10
                    while app.busy and time.monotonic() < deadline:
                        root.update()
                        time.sleep(.01)
                    self.assertFalse(app.busy)
                    result.assert_called_once()
                    output, success, issues = result.call_args.args
                    self.assertEqual((success, issues), (1, []))
                    with Image.open(output / "1.png") as cropped:
                        self.assertEqual(cropped.size, (31, 23))
            finally:
                app.close()

    def test_fixed_aspect_presets(self):
        root = tk.Tk()
        app = CropperApp(root)
        try:
            root.update()
            app.original = Image.new("RGB", (1920, 1080))
            app.folder, app.files = Path("."), [Path("1.png")]
            app.render_image()
            for choice, expected in (("4:3", (400, 100, 1200, 700)),
                                     ("16:9", (400, 175, 1200, 625)),
                                     ("1:1", (400, 0, 1200, 800))):
                app.box = (400, 200, 1200, 600)
                app.aspect_choice.set(choice)
                app.select_aspect_preset()
                self.assertTrue(app.aspect_locked.get())
                self.assertEqual(app.box, expected)
            # 空选区先选比例，再画框。
            app.reset_selection()
            app.aspect_choice.set("4:3")
            app.select_aspect_preset()
            def event(x, y):
                ox, oy = app.offset
                dw, dh = app.display_size
                return type("Event", (), {"x": ox+x*dw/1920, "y": oy+y*dh/1080})()
            app.start_selection(event(400, 200))
            app.end_selection(event(800, 500))
            self.assertEqual(app.box, (400, 200, 800, 500))
            app.save_crop_region()
            app.reset_selection()
            app.apply_saved_region()
            self.assertEqual(app.aspect_choice.get(), "4:3")
            self.assertEqual(app.aspect_ratio, (4, 3))
            app.box = (0, 0, 1000, 200)
            app.aspect_choice.set("1:1")
            app.select_aspect_preset()
            self.assertEqual(app.box, (400, 0, 600, 200))
            app.set_busy(True)
            self.assertEqual(str(app.aspect_combo.cget("state")), "disabled")
            app.set_busy(False)
            self.assertEqual(str(app.aspect_combo.cget("state")), "readonly")
        finally:
            app.set_busy(False)
            app.close()

    def test_batch_100_png_and_original_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            original = Image.new("RGB", (80, 60))
            original.putdata([(x * 3, y * 4, (x + y) % 256) for y in range(60) for x in range(80)])
            for i in range(1, 101):
                original.save(folder / f"{i}.png")
            files = find_images(folder)
            self.assertEqual([p.name for p in files[:3]], ["1.png", "2.png", "3.png"])
            self.assertEqual(files[9].name, "10.png")
            hashes = {p.name: hashlib.sha256(p.read_bytes()).digest() for p in files}
            (folder / "裁剪结果").mkdir()
            (folder / "裁剪结果_2").mkdir()
            out, count, issues = batch_crop(folder, files, (80, 60), (7, 9, 62, 48))
            self.assertEqual(out.name, "裁剪结果_3")
            self.assertEqual((count, issues), (100, []))
            expected = original.crop((7, 9, 62, 48))
            for p in files:
                self.assertEqual(hashlib.sha256(p.read_bytes()).digest(), hashes[p.name])
                with Image.open(out / p.name) as result:
                    self.assertEqual(result.size, (55, 39))
                    self.assertEqual(result.tobytes(), expected.tobytes())
            self.assertEqual(len(find_images(folder)), 100)
            original.close()
            expected.close()

    def test_jpeg_4k_mismatch_and_bad_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with Image.new("RGB", (3840, 2160), "#305090") as im:
                for name in ("1.jpg", "2.jpeg", "3.JPG"):
                    im.save(folder / name)
            with Image.new("RGB", (100, 80)) as im:
                im.save(folder / "4.png")
            (folder / "5.png").write_bytes(b"broken image")
            events = []
            out, count, issues = batch_crop(folder, find_images(folder), (3840, 2160), (320, 140, 1540, 890), lambda *event: events.append(event))
            self.assertEqual(count, 3)
            self.assertEqual({name for name, _ in issues}, {"4.png", "5.png"})
            self.assertTrue(events)
            for name in ("1.jpg", "2.jpeg", "3.JPG"):
                with Image.open(out / name) as result:
                    self.assertEqual(result.format, "JPEG")
                    self.assertEqual(result.size, (1220, 750))

    def test_rgba_and_invalid_crop(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with Image.new("RGBA", (30, 20), (50, 100, 150, 80)) as im:
                im.save(folder / "透明.png")
            files = find_images(folder)
            with self.assertRaises(ValueError):
                batch_crop(folder, files, (30, 20), (0, 0, 31, 20))
            self.assertFalse((folder / "裁剪结果").exists())
            out, count, issues = batch_crop(folder, files, (30, 20), (0, 0, 30, 20))
            with Image.open(out / "透明.png") as result:
                self.assertEqual(result.getpixel((10, 10)), (50, 100, 150, 80))

    def test_gui_selection_resize_preview_and_validation(self):
        root = tk.Tk()
        app = CropperApp(root)
        try:
            root.update()
            with patch("main.messagebox.showinfo") as info:
                app.start_batch()
                self.assertIn("请先选择图片文件夹", info.call_args.args[1])
            app.original = Image.new("RGB", (1920, 1080), "#387588")
            app.folder = Path(".")
            app.files = [Path("test.png")]
            app.render_image()
            ox, oy = app.offset
            dw, dh = app.display_size
            def event(x, y):
                return type("Event", (), {"x": x, "y": y})()
            app.start_selection(event(ox + dw * .75, oy + dh * .8))
            app.end_selection(event(ox + dw * .25, oy + dh * .2))
            self.assertEqual(app.box, (480, 216, 1440, 864))
            root.geometry("800x600")
            root.update()
            app.render_image()
            self.assertEqual(app.box, (480, 216, 1440, 864))
            app.preview_crop()
            root.update()
            self.assertTrue(any(isinstance(w, tk.Toplevel) for w in root.winfo_children()))
            app.reset_selection()
            self.assertIsNone(app.box)
        finally:
            app.close()

    def test_gui_folder_loading_and_background_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "0.png").write_bytes(b"bad")
            for number in (1, 2, 10):
                with Image.new("RGB", (192, 108), (number, 30, 70)) as im:
                    im.save(folder / f"{number}.png")
            root = tk.Tk()
            app = CropperApp(root)
            try:
                root.update()
                with patch("main.filedialog.askdirectory", return_value=tmp), patch("main.messagebox.showwarning") as warning:
                    app.select_folder()
                    warning.assert_called_once()
                self.assertIn("1.png", app.info_text.get())
                with patch("main.messagebox.showinfo") as info:
                    app.start_batch()
                    self.assertIn("选择裁剪区域", info.call_args.args[1])
                app.box = (10, 20, 150, 90)
                with patch.object(app, "show_result") as result:
                    app.start_batch()
                    deadline = time.monotonic() + 10
                    while app.busy and time.monotonic() < deadline:
                        root.update()
                        time.sleep(.01)
                    self.assertFalse(app.busy)
                    result.assert_called_once()
                    output, success, issues = result.call_args.args
                    self.assertEqual(success, 3)
                    self.assertEqual(issues[0][0], "0.png")
                    with Image.open(output / "10.png") as im:
                        self.assertEqual(im.size, (140, 70))
                app.show_result(output, success, issues)
                root.update()
                self.assertEqual(float(app.progress["value"]), 100)
            finally:
                app.close()

    def test_move_rectangle_on_scaled_image(self):
        root = tk.Tk()
        app = CropperApp(root)
        try:
            root.update()
            app.original = Image.new("RGB", (1920, 1080))
            app.render_image()

            def event(x, y):
                ox, oy = app.offset
                dw, dh = app.display_size
                return type("Event", (), {"x": ox + x * dw / 1920,
                                           "y": oy + y * dh / 1080})()

            app.start_selection(event(200, 100))
            app.end_selection(event(1000, 600))
            app.start_selection(event(400, 300))
            app.drag_selection(event(500, 350))
            app.end_selection(event(600, 400))
            self.assertEqual(app.box, (400, 200, 1200, 700))
            self.assertIn("800 × 500", app.size_text.get())
            self.assertEqual(app.canvas.cget("cursor"), "fleur")
            # 到达边缘后保持尺寸；继续反向拖动仍以按下位置为基准。
            app.start_selection(event(600, 400))
            app.drag_selection(event(-100, -100))
            self.assertEqual(app.box, (0, 0, 800, 500))
            app.end_selection(event(2100, 1300))
            self.assertEqual(app.box, (1120, 580, 1920, 1080))
            root.geometry("800x600")
            root.update()
            app.render_image()
            app.start_selection(event(1300, 700))
            app.end_selection(event(1200, 600))
            self.assertEqual(app.box, (1020, 480, 1820, 980))
            app.start_selection(event(100, 100))
            app.end_selection(event(500, 300))
            self.assertEqual(app.box, (100, 100, 500, 300))
            app.start_selection(event(200, 200))
            app.end_selection(event(200, 200))
            self.assertEqual(app.box, (100, 100, 500, 300))
        finally:
            app.close()

    def test_resize_handles_and_display_only_shading(self):
        root = tk.Tk()
        app = CropperApp(root)
        try:
            root.update()
            app.original = Image.new("RGB", (1920, 1080), (180, 210, 240))
            original_bytes = app.original.tobytes()
            app.render_image()

            def event(x, y):
                ox, oy = app.offset
                dw, dh = app.display_size
                return type("Event", (), {"x": ox + x * dw / 1920,
                                           "y": oy + y * dh / 1080})()

            cases = [((200, 200), (220, 230, 1000, 700)),
                     ((1000, 200), (200, 230, 1020, 700)),
                     ((200, 700), (220, 200, 1000, 730)),
                     ((1000, 700), (200, 200, 1020, 730)),
                     ((600, 200), (200, 230, 1000, 700)),
                     ((600, 700), (200, 200, 1000, 730)),
                     ((200, 450), (220, 200, 1000, 700)),
                     ((1000, 450), (200, 200, 1020, 700))]
            for (x, y), expected in cases:
                with self.subTest(handle=(x, y)):
                    app.box = (200, 200, 1000, 700)
                    app.draw_selection()
                    app.start_selection(event(x, y))
                    app.end_selection(event(x + 20, y + 30))
                    self.assertEqual(app.box, expected)
            # 可拖动控制点以外的边框；越过对边时最小保留 1 像素。
            app.box = (200, 200, 1000, 700)
            app.start_selection(event(200, 350))
            app.end_selection(event(1800, 350))
            self.assertEqual(app.box, (999, 200, 1000, 700))
            app.box = (200, 200, 1000, 700)
            app.start_selection(event(1000, 700))
            app.end_selection(event(2400, 1400))
            self.assertEqual(app.box, (200, 200, 1920, 1080))
            # 图像外半边的控制点也能按下，缩放后仍能调节。
            root.geometry("800x600")
            root.update()
            app.render_image()
            app.start_selection(event(1925, 1085))
            app.end_selection(event(1700, 900))
            self.assertEqual(app.box, (200, 200, 1700, 900))
            with ImageTk.getimage(app.shade_photo) as shade:
                self.assertEqual(shade.getpixel((0, 0))[3], 56)
                self.assertEqual(shade.getpixel((shade.width // 2, shade.height // 2))[3], 0)
            self.assertEqual(app.original.tobytes(), original_bytes)
            with app.original.crop(app.box) as cropped:
                self.assertEqual(cropped.size, (1500, 700))
                self.assertEqual(cropped.getpixel((0, 0)), (180, 210, 240))
            app.reset_selection()
            self.assertIsNone(app.shade_photo)
            self.assertFalse(app.canvas.find_withtag("selection"))
        finally:
            app.close()

    def test_saved_region_across_folders_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            folders = [Path(tmp) / name for name in ("case1", "case2", "different")]
            for index, folder in enumerate(folders):
                folder.mkdir()
                with Image.new("RGB", (80, 60) if index < 2 else (90, 60)) as im:
                    im.putdata([(x * 2, y * 3, (x + y) % 256)
                                for y in range(im.height) for x in range(im.width)])
                    im.save(folder / "1.png")
            root = tk.Tk()
            app = CropperApp(root)
            fixed = (7, 9, 62, 48)

            def select(folder):
                with patch("main.filedialog.askdirectory", return_value=str(folder)):
                    app.select_folder()

            try:
                root.update()
                select(folders[0])
                app.box = fixed
                app.save_crop_region()
                self.assertTrue(self.preset_path.is_file())
                saved_bytes = self.preset_path.read_bytes()
                select(folders[1])
                self.assertEqual(app.box, fixed)
                self.assertIn("55 × 39", app.size_text.get())
                results = []
                for folder in folders[:2]:
                    out, count, issues = batch_crop(folder, find_images(folder), app.original.size, app.box)
                    self.assertEqual((count, issues), (1, []))
                    with Image.open(out / "1.png") as result:
                        results.append(result.tobytes())
                self.assertEqual(results[0], results[1])
                app.box = (0, 0, 20, 20)
                app.apply_saved_region()
                self.assertEqual(app.box, fixed)
                select(folders[2])
                self.assertIsNone(app.box)
                with patch("main.messagebox.showwarning") as warning:
                    app.apply_saved_region()
                    warning.assert_called_once()
                self.assertEqual(self.preset_path.read_bytes(), saved_bytes)
            finally:
                app.close()
            # 新应用实例从磁盘读取；无须再次框选或保存。
            root = tk.Tk()
            app = CropperApp(root)
            try:
                root.update()
                select(folders[1])
                self.assertEqual(app.box, fixed)
                app.box = (10, 10, 70, 50)
                app.save_crop_region()
                self.assertEqual(json.loads(self.preset_path.read_text())["box"], [10, 10, 70, 50])
            finally:
                app.close()

    def test_corrupt_saved_region_does_not_block_startup(self):
        self.preset_path.parent.mkdir()
        for text in ('not json', '{"version":1,"image_size":[80,60],"box":[0,0,90,60]}'):
            self.preset_path.write_text(text, encoding="utf-8")
            root = tk.Tk()
            app = CropperApp(root)
            try:
                root.update()
                self.assertIsNone(app.saved_region)
                self.assertIn("读取失败", app.preset_text.get())
                self.assertEqual(self.preset_path.read_text(), text)
            finally:
                app.close()

    def test_failed_preset_write_keeps_previous_region(self):
        old = {"version": 1, "image_size": (80, 60), "box": (7, 9, 62, 48)}
        write_saved_region(self.preset_path, old)
        before = self.preset_path.read_bytes()
        root = tk.Tk()
        app = CropperApp(root)
        try:
            app.folder = Path(".")
            app.files = [Path("1.png")]
            app.original = Image.new("RGB", (80, 60))
            app.box = (0, 0, 40, 30)
            with patch("main.Path.replace", side_effect=PermissionError("test")), patch("main.messagebox.showerror") as error:
                app.save_crop_region()
                error.assert_called_once()
            self.assertEqual(self.preset_path.read_bytes(), before)
            self.assertEqual(app.saved_region, old)
            self.assertEqual(list(self.preset_path.parent.glob("*.tmp")), [])
        finally:
            app.close()

    def test_aspect_resize_handles_and_image_boundaries(self):
        for handle in ("nw", "ne", "sw", "se", "n", "s", "w", "e"):
            for delta in ((120, 70), (-100, -40), (4000, 3000), (-4000, -3000)):
                with self.subTest(handle=handle, delta=delta):
                    box = resize_with_aspect((400, 200, 1200, 650), handle, *delta, (1920, 1080), (16, 9))
                    l, t, r, b = box
                    self.assertTrue(0 <= l < r <= 1920 and 0 <= t < b <= 1080)
                    # 原图使用整数像素，宽高四舍五入误差各不超过半像素。
                    self.assertLessEqual(abs((r-l)*9 - (b-t)*16), 12.5)
                    if "e" in handle:
                        self.assertEqual(l, 400)
                    if "w" in handle:
                        self.assertEqual(r, 1200)
                    if "n" in handle:
                        self.assertEqual(b, 650)
                    if "s" in handle:
                        self.assertEqual(t, 200)

    def test_aspect_lock_gui_and_saved_region(self):
        root = tk.Tk()
        app = CropperApp(root)
        try:
            root.update()
            app.aspect_locked.set(True)
            with patch("main.messagebox.showinfo"):
                app.toggle_aspect()
            self.assertFalse(app.aspect_locked.get())
            app.original = Image.new("RGB", (1920, 1080))
            app.folder, app.files = Path("."), [Path("1.png")]
            app.box = (300, 200, 1100, 600)
            app.render_image()
            app.aspect_button.invoke()
            self.assertEqual(app.aspect_ratio, (2, 1))

            def event(x, y):
                ox, oy = app.offset
                dw, dh = app.display_size
                return type("Event", (), {"x": ox+x*dw/1920, "y": oy+y*dh/1080})()

            app.start_selection(event(1100, 400))
            app.end_selection(event(1300, 400))
            self.assertEqual(app.box, (300, 150, 1300, 650))
            app.save_crop_region()
            app.reset_selection()
            self.assertFalse(app.aspect_locked.get())
            app.apply_saved_region()
            self.assertTrue(app.aspect_locked.get())
            self.assertEqual(app.aspect_ratio, (2, 1))
            # 锁定后重画仍用原比例。
            app.start_selection(event(10, 10))
            app.end_selection(event(210, 110))
            self.assertEqual(app.box, (10, 10, 210, 110))
            app.aspect_button.invoke()
            app.start_selection(event(210, 60))
            app.end_selection(event(310, 60))
            self.assertEqual(app.box, (10, 10, 310, 110))
        finally:
            app.close()
        root = tk.Tk()
        app = CropperApp(root)
        try:
            root.update()
            app.original = Image.new("RGB", (1920, 1080))
            app.render_image()
            app.apply_saved_region()
            self.assertTrue(app.aspect_locked.get())
            self.assertEqual(app.aspect_ratio, (2, 1))
            self.assertEqual(app.box, (300, 150, 1300, 650))
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
