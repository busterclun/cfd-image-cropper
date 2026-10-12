"""离线批量裁剪：所有图片始终使用同一组原始像素坐标。"""

from pathlib import Path
import json
import math
import os
import queue
import re
import threading
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageTk
from gif_export import create_gif


EXTENSIONS = {".jpg", ".jpeg", ".png"}
SELECTION_COLOR = "#4169E1"  # 宝蓝色
ASPECT_PRESETS = {"4:3": (4, 3), "16:9": (16, 9), "1:1": (1, 1)}


def default_preset_path():
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return base / "CFDImageCropper" / "saved_region.json"


def validate_saved_region(data):
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("保存的区域格式不正确")
    size, box = data.get("image_size"), data.get("box")
    if (not isinstance(size, (list, tuple)) or len(size) != 2
            or not isinstance(box, (list, tuple)) or len(box) != 4
            or any(type(v) is not int for v in (*size, *box))):
        raise ValueError("保存的区域必须使用整数像素坐标")
    width, height = size
    left, top, right, bottom = box
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError("保存的区域超出图片范围或尺寸无效")
    result = {"version": 1, "image_size": tuple(size), "box": tuple(box)}
    if "aspect_ratio" in data:
        ratio = data["aspect_ratio"]
        if (not isinstance(ratio, (list, tuple)) or len(ratio) != 2
                or any(type(v) is not int or v <= 0 for v in ratio)):
            raise ValueError("保存的纵横比无效")
        result["aspect_ratio"] = tuple(ratio)
    return result


def resize_with_aspect(box, handle, dx, dy, image_size, ratio):
    """角点固定对角；边点固定对边中点。先限制比例尺寸，再转整数像素。"""
    left, top, right, bottom = box
    iw, ih = image_size
    cx, cy = (left + right) / 2, (top + bottom) / 2
    r = ratio[0] / ratio[1]
    wanted_w = right - left + (dx if "e" in handle else -dx)
    wanted_h = bottom - top + (dy if "s" in handle else -dy)
    max_w = iw - left if "e" in handle else right if "w" in handle else 2 * min(cx, iw - cx)
    max_h = ih - top if "s" in handle else bottom if "n" in handle else 2 * min(cy, ih - cy)
    if len(handle) == 2:
        target_w = (wanted_w * r * r + wanted_h * r) / (r * r + 1)
    elif handle in ("e", "w"):
        target_w = wanted_w
    else:
        target_w = wanted_h * r
    max_width = min(max_w, max_h * r)
    if max_width <= 0:
        return box
    width = min(max_width, max(max(1, r), target_w))
    w, h = max(1, round(width)), max(1, round(width / r))
    x = left if "e" in handle else right - w if "w" in handle else round(cx - w / 2)
    y = top if "s" in handle else bottom - h if "n" in handle else round(cy - h / 2)
    x, y = max(0, min(iw - w, x)), max(0, min(ih - h, y))
    return x, y, x + w, y + h


def write_saved_region(path, data):
    data = validate_saved_region(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # 同目录临时文件 + 原子替换，写入失败时保留上次保存的区域。
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="region_", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def natural_key(path):
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r"(\d+)", path.name))


def find_images(folder):
    # 只读取当前层，不读取之前生成的结果子文件夹。
    return sorted((p for p in Path(folder).iterdir()
                   if p.is_file() and p.suffix.lower() in EXTENSIONS),
                  key=lambda p: (natural_key(p), p.name))


def create_output_folder(folder):
    index = 1
    while True:
        name = "裁剪结果" if index == 1 else f"裁剪结果_{index}"
        output = Path(folder) / name
        try:
            output.mkdir()  # 原子创建，已有目录或文件均不覆盖。
            return output
        except FileExistsError:
            index += 1


def fit_image(size, available):
    width, height = size
    area_w, area_h = available
    scale = min(area_w / width, area_h / height, 1.0)
    return max(1, round(width * scale)), max(1, round(height * scale))


def display_to_pixel(x, y, offset, display_size, original_size):
    # 分别使用实际显示宽、高换算，避免缩略图取整造成一像素偏移。
    ox, oy = offset
    dw, dh = display_size
    width, height = original_size
    px = math.floor((x - ox) * width / dw + 0.5)
    py = math.floor((y - oy) * height / dh + 0.5)
    return max(0, min(width, px)), max(0, min(height, py))


def batch_crop(folder, files, baseline_size, box, progress=None):
    left, top, right, bottom = box
    width, height = baseline_size
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError("裁剪区域无效，请重新选择。")

    # 先检查所有图片尺寸；之后逐张读取像素，不将整批图片留在内存。
    issues, valid = [], []
    total = len(files)
    for index, path in enumerate(files, 1):
        try:
            with Image.open(path) as source:
                if source.format not in {"JPEG", "PNG"}:
                    raise ValueError("文件内容不是 JPG / PNG 图片")
                if source.size != baseline_size:
                    issues.append((path.name, f"尺寸不同：{source.width} × {source.height}，基准 {width} × {height}"))
                else:
                    valid.append(path)
        except Exception as exc:
            issues.append((path.name, f"无法读取：{exc}"))
        if progress:
            progress("检查尺寸", index, total)

    output = create_output_folder(folder)
    success = 0
    for index, path in enumerate(valid, 1):
        destination = output / path.name
        try:
            with Image.open(path) as source:
                if source.size != baseline_size:
                    raise ValueError("检查后图片尺寸发生变化")
                source.load()
                with source.crop(box) as cropped:
                    options = {}
                    if source.info.get("icc_profile"):
                        options["icc_profile"] = source.info["icc_profile"]
                    if source.info.get("dpi"):
                        options["dpi"] = source.info["dpi"]
                    # PNG 无损保存；JPEG 需重新编码，使用高质量且关闭色度抽样。
                    fmt = "PNG" if path.suffix.lower() == ".png" else "JPEG"
                    if fmt == "JPEG":
                        options.update(quality=95, subsampling=0)
                        if cropped.mode not in {"RGB", "L", "CMYK"}:
                            raise ValueError("JPG 文件的像素模式不受支持")
                    # x 模式确保即使目标意外已存在，也不会覆盖它。
                    created = False
                    try:
                        with destination.open("xb") as stream:
                            created = True
                            cropped.save(stream, format=fmt, **options)
                    except Exception:
                        if created:
                            destination.unlink(missing_ok=True)
                        raise
            success += 1
        except Exception as exc:
            issues.append((path.name, f"处理失败：{exc}"))
        if progress:
            progress("正在裁剪", index, len(valid))
    return output, success, issues


class CropperApp:
    def __init__(self, root, preset_path=None):
        self.root = root
        self.preset_path = Path(preset_path) if preset_path is not None else default_preset_path()
        self.saved_region = None
        preset_error = None
        try:
            self.saved_region = validate_saved_region(json.loads(self.preset_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            preset_error = str(exc)
        root.title("批量图片固定区域裁剪工具")
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1000, screen_w - 60)}x{min(750, screen_h - 100)}")
        root.minsize(min(720, screen_w - 60), min(540, screen_h - 100))
        self.folder = None
        self.files = []
        self.original = None
        self.photo = None
        self.box = None
        self.anchor = None
        self.move_box = None
        self.resize_box = None
        self.resize_handle = None
        self.shade_photo = None
        self.aspect_locked = tk.BooleanVar(value=False)
        self.aspect_ratio = None
        self.aspect_text = tk.StringVar(value="锁定纵横比")
        self.aspect_choice = tk.StringVar(value="当前选框比例")
        self.offset = (0, 0)
        self.display_size = (1, 1)
        self.busy = False
        self.events = queue.Queue()
        self.resize_job = None

        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TButton", padding=(12, 7))
        outer = ttk.Frame(root, padding=16)
        outer.pack(fill="both", expand=True)
        row = ttk.Frame(outer)
        row.pack(fill="x")
        ttk.Label(row, text="图片文件夹：").pack(side="left")
        self.path_text = tk.StringVar(value="请选择存放 JPG / PNG 图片的文件夹")
        ttk.Entry(row, textvariable=self.path_text, state="readonly").pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.folder_button = ttk.Button(row, text="选择图片文件夹", command=self.select_folder)
        self.folder_button.pack(side="right")
        self.info_text = tk.StringVar(value="图片数量：0　　第一张图片：未加载")
        ttk.Label(outer, textvariable=self.info_text).pack(anchor="w", pady=(10, 6))
        ttk.Label(outer, text="拖动画框；拖动边框或控制点调整大小；框内拖动可移动，框外拖动可重画。", foreground="#555555").pack(anchor="w", pady=(0, 8))
        self.canvas = tk.Canvas(outer, width=1, height=1, bg="#e9edf1", highlightthickness=1, highlightbackground="#bac2ca", cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self.schedule_render)
        self.canvas.bind("<ButtonPress-1>", self.start_selection)
        self.canvas.bind("<B1-Motion>", self.drag_selection)
        self.canvas.bind("<ButtonRelease-1>", self.end_selection)
        self.canvas.bind("<Motion>", self.update_cursor)
        self.region_text = tk.StringVar(value="裁剪区域：尚未选择")
        self.size_text = tk.StringVar(value="输出尺寸：—")
        self.width_text = tk.StringVar()
        self.height_text = tk.StringVar()
        self.size_syncing = False
        self.size_dirty = False
        self.last_size_field = "width"
        self.width_text.trace_add("write", lambda *_: self.mark_size_edited("width"))
        self.height_text.trace_add("write", lambda *_: self.mark_size_edited("height"))
        ttk.Label(outer, textvariable=self.region_text).pack(anchor="w", pady=(12, 4))
        size_row = ttk.Frame(outer)
        size_row.pack(fill="x")
        ttk.Label(size_row, text="输出尺寸：宽").pack(side="left")
        self.width_entry = ttk.Entry(size_row, textvariable=self.width_text, width=8)
        self.width_entry.pack(side="left", padx=(4, 8))
        ttk.Label(size_row, text="× 高").pack(side="left")
        self.height_entry = ttk.Entry(size_row, textvariable=self.height_text, width=8)
        self.height_entry.pack(side="left", padx=(4, 8))
        ttk.Label(size_row, text="px").pack(side="left")
        self.apply_size_button = ttk.Button(size_row, text="应用尺寸", command=self.apply_typed_size)
        self.apply_size_button.pack(side="left", padx=(12, 8))
        ttk.Label(size_row, text="输入后按回车也可应用", foreground="#555555").pack(side="left")
        self.width_entry.bind("<Return>", lambda event: self.apply_typed_size())
        self.height_entry.bind("<Return>", lambda event: self.apply_typed_size())
        self.width_entry.bind("<KP_Enter>", lambda event: self.apply_typed_size())
        self.height_entry.bind("<KP_Enter>", lambda event: self.apply_typed_size())
        presets = ttk.Frame(outer)
        presets.pack(fill="x", pady=(8, 0))
        self.save_region_button = ttk.Button(presets, text="保存裁剪区域", command=self.save_crop_region)
        self.save_region_button.pack(side="left")
        self.apply_region_button = ttk.Button(presets, text="应用已保存区域", command=self.apply_saved_region)
        self.apply_region_button.pack(side="left", padx=10)
        self.aspect_button = ttk.Checkbutton(presets, textvariable=self.aspect_text,
                                            variable=self.aspect_locked, command=self.toggle_aspect)
        self.aspect_button.pack(side="left", padx=(8, 0))
        self.aspect_combo = ttk.Combobox(presets, textvariable=self.aspect_choice,
                                        values=("当前选框比例", *ASPECT_PRESETS),
                                        state="readonly", width=14)
        self.aspect_combo.pack(side="left", padx=(10, 0))
        self.aspect_combo.bind("<<ComboboxSelected>>", self.select_aspect_preset)
        self.preset_text = tk.StringVar()
        ttk.Label(outer, textvariable=self.preset_text, foreground="#555555", wraplength=650).pack(anchor="w", pady=(4, 0))
        self.update_preset_text()
        if preset_error:
            self.preset_text.set("已保存区域读取失败，可重新框选并保存。原因：" + preset_error)
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=12)
        self.reset_button = ttk.Button(actions, text="重新选择区域", command=self.reset_selection)
        self.reset_button.pack(side="left")
        self.preview_button = ttk.Button(actions, text="预览裁剪结果", command=self.preview_crop)
        self.preview_button.pack(side="left", padx=10)
        self.gif_button = ttk.Button(actions, text="图片转 GIF", command=self.select_gif_folder)
        self.gif_button.pack(side="left")
        self.reference_button = ttk.Button(actions, text="导入参考图片", command=self.import_reference_image)
        self.reference_button.pack(side="left", padx=10)
        self.start_button = ttk.Button(actions, text="开始批量裁剪", command=self.start_batch)
        self.start_button.pack(side="right")
        self.progress = ttk.Progressbar(outer, maximum=100)
        self.progress.pack(fill="x")
        self.status_text = tk.StringVar(value="就绪。结果将保存到新的“裁剪结果”子文件夹，原图片保持不变。")
        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(6, 0))
        ttk.Label(footer, text="开发者：无春夏", foreground="#657080").pack(side="right", padx=(12, 0))
        ttk.Label(footer, textvariable=self.status_text).pack(side="left", fill="x", expand=True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_job = root.after(100, self.poll_events)

    def select_folder(self):
        if self.busy:
            return
        selected = filedialog.askdirectory(title="选择图片文件夹", parent=self.root)
        if not selected:
            return
        try:
            files = find_images(selected)
        except OSError as exc:
            messagebox.showerror("无法读取文件夹", str(exc), parent=self.root)
            return
        self.folder, self.files = Path(selected), files
        self.path_text.set(selected)
        if self.original:
            self.original.close()
        self.original = None
        self.reset_selection()
        self.progress["value"] = 0
        self.status_text.set("请在图片上拖动框选裁剪区域。" if files else "当前文件夹中没有可处理的图片。")
        self.info_text.set(f"图片数量：{len(files)}　　第一张图片：{files[0].name if files else '无'}")
        self.render_image()
        if not files:
            messagebox.showinfo("没有图片", "当前文件夹中没有可处理的图片。", parent=self.root)
            return
        # 第一张若损坏，明确说明并使用下一张可读图片设置选区。
        unreadable = []
        for path in files:
            try:
                with Image.open(path) as source:
                    if source.format not in {"JPEG", "PNG"}:
                        raise ValueError("文件内容不是 JPG / PNG")
                    source.load()
                    self.original = source.copy()
                self.info_text.set(f"图片数量：{len(files)}　　选区基准：{path.name}　　原图：{self.original.width} × {self.original.height} px")
                self.status_text.set("请在图片上拖动框选裁剪区域。")
                self.render_image()
                if self.saved_region:
                    if self.original.size == self.saved_region["image_size"]:
                        self.apply_saved_region()
                    else:
                        self.status_text.set("当前原图尺寸与保存区域的基准不同，未自动应用；请重新框选。")
                if unreadable:
                    messagebox.showwarning("部分图片无法读取", f"前 {len(unreadable)} 张图片无法读取，改用 {path.name} 设置区域。\n批量完成后会列出异常文件。", parent=self.root)
                return
            except Exception as exc:
                unreadable.append((path.name, str(exc)))
        messagebox.showerror("无法读取图片", "文件夹中的图片均无法读取。\n" + "\n".join(name for name, _ in unreadable[:10]), parent=self.root)
        self.status_text.set("文件夹中的图片均无法读取，请重新选择文件夹。")

    def schedule_render(self, event=None):
        if self.resize_job:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(70, self.render_image)

    def render_image(self):
        if self.resize_job:
            self.root.after_cancel(self.resize_job)
        self.resize_job = None
        self.canvas.delete("all")
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if not self.original:
            self.canvas.create_text(cw / 2, ch / 2, text="选择文件夹后，在此处框选裁剪区域", fill="#697582", font=("Microsoft YaHei UI", 13))
            return
        self.display_size = fit_image(self.original.size, (max(1, cw - 20), max(1, ch - 20)))
        dw, dh = self.display_size
        self.offset = ((cw - dw) / 2, (ch - dh) / 2)
        with self.original.resize(self.display_size, Image.Resampling.LANCZOS) as thumb:
            self.photo = ImageTk.PhotoImage(thumb)
        self.canvas.create_image(*self.offset, image=self.photo, anchor="nw")
        self.draw_selection(sync_fields=not self.size_dirty)

    def pixel_at(self, event):
        return display_to_pixel(event.x, event.y, self.offset, self.display_size, self.original.size)

    def selection_coords(self):
        left, top, right, bottom = self.box
        ox, oy = self.offset
        dw, dh = self.display_size
        width, height = self.original.size
        return (ox + left * dw / width, oy + top * dh / height,
                ox + right * dw / width, oy + bottom * dh / height)

    def handle_positions(self):
        left, top, right, bottom = self.selection_coords()
        cx, cy = (left + right) / 2, (top + bottom) / 2
        return {"nw": (left, top), "ne": (right, top),
                "sw": (left, bottom), "se": (right, bottom),
                "n": (cx, top), "s": (cx, bottom),
                "w": (left, cy), "e": (right, cy)}

    def hit_resize_handle(self, event):
        if not self.original or not self.box:
            return None
        # 命中范围使用屏幕像素，缩放后控制点仍容易拖动。
        handles = self.handle_positions()
        nearest = min(handles, key=lambda key: (handles[key][0] - event.x) ** 2
                      + (handles[key][1] - event.y) ** 2)
        hx, hy = handles[nearest]
        if abs(event.x - hx) <= 7 and abs(event.y - hy) <= 7:
            return nearest
        left, top, right, bottom = self.selection_coords()
        if left <= event.x <= right:
            if abs(event.y - top) <= 5:
                return "n"
            if abs(event.y - bottom) <= 5:
                return "s"
        if top <= event.y <= bottom:
            if abs(event.x - left) <= 5:
                return "w"
            if abs(event.x - right) <= 5:
                return "e"
        return None

    def inside_selection(self, event):
        if not self.original or not self.box:
            return False
        ox, oy = self.offset
        dw, dh = self.display_size
        if not (ox <= event.x <= ox + dw and oy <= event.y <= oy + dh):
            return False
        x, y = self.pixel_at(event)
        left, top, right, bottom = self.box
        return left <= x <= right and top <= y <= bottom

    def update_cursor(self, event):
        handle = self.resize_handle or self.hit_resize_handle(event)
        moving = self.move_box is not None or self.inside_selection(event)
        cursors = {"n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
                   "w": "sb_h_double_arrow", "e": "sb_h_double_arrow",
                   "nw": "size_nw_se", "se": "size_nw_se",
                   "ne": "size_ne_sw", "sw": "size_ne_sw"}
        cursor = cursors[handle] if handle else ("fleur" if moving else "crosshair")
        self.canvas.configure(cursor="crosshair" if self.busy else cursor)

    def start_selection(self, event):
        if self.busy or not self.original:
            return
        ox, oy = self.offset
        dw, dh = self.display_size
        handle = self.hit_resize_handle(event)
        if not handle and not (ox <= event.x <= ox + dw and oy <= event.y <= oy + dh):
            return
        self.anchor = self.pixel_at(event)
        self.resize_handle = handle
        self.resize_box = self.box if handle else None
        self.move_box = self.box if not handle and self.inside_selection(event) else None
        if self.move_box is None and self.resize_box is None:
            self.box = None
        self.update_cursor(event)
        self.draw_selection()

    def drag_selection(self, event):
        if self.busy or self.anchor is None:
            return
        x, y = self.pixel_at(event)
        ax, ay = self.anchor
        if self.resize_box is not None:
            left, top, right, bottom = self.resize_box
            width, height = self.original.size
            dx, dy = x - ax, y - ay
            # 对边固定，最小保留 1 像素，避免越过对边造成矩形翻转。
            if self.aspect_locked.get():
                self.box = resize_with_aspect(self.resize_box, self.resize_handle,
                                              dx, dy, self.original.size, self.aspect_ratio)
                self.draw_selection()
                return
            if "w" in self.resize_handle:
                left = max(0, min(right - 1, left + dx))
            if "e" in self.resize_handle:
                right = min(width, max(left + 1, right + dx))
            if "n" in self.resize_handle:
                top = max(0, min(bottom - 1, top + dy))
            if "s" in self.resize_handle:
                bottom = min(height, max(top + 1, bottom + dy))
            self.box = (left, top, right, bottom)
        elif self.move_box is not None:
            # 从按下时的原图选区计算位移，限制整个矩形，不改变宽高。
            left, top, right, bottom = self.move_box
            width, height = self.original.size
            dx = max(-left, min(width - right, x - ax))
            dy = max(-top, min(height - bottom, y - ay))
            self.box = (left + dx, top + dy, right + dx, bottom + dy)
        else:
            if self.aspect_locked.get() and (x != ax or y != ay):
                handle = ("s" if y >= ay else "n") + ("e" if x >= ax else "w")
                self.box = resize_with_aspect((ax, ay, ax, ay), handle,
                                              x - ax, y - ay, self.original.size, self.aspect_ratio)
            else:
                self.box = (min(ax, x), min(ay, y), max(ax, x), max(ay, y))
        self.draw_selection()

    def end_selection(self, event):
        self.drag_selection(event)
        self.anchor = None
        self.move_box = None
        self.resize_box = self.resize_handle = None
        if self.box and (self.box[0] == self.box[2] or self.box[1] == self.box[3]):
            self.box = None
            self.draw_selection()
        self.update_cursor(event)

    def draw_selection(self, sync_fields=True):
        self.canvas.delete("selection")
        self.shade_photo = None
        if not self.box:
            self.region_text.set("裁剪区域：尚未选择")
            self.size_text.set("输出尺寸：—")
            if sync_fields:
                self.sync_size_fields()
            return
        left, top, right, bottom = self.box
        ox, oy = self.offset
        dw, dh = self.display_size
        coords = self.selection_coords()
        # 仅在 GUI 上覆盖约 22% 的黑色透明层；原图和输出像素不受影响。
        with Image.new("RGBA", (dw, dh), (0, 0, 0, 56)) as shade:
            x1, y1, x2, y2 = (round(coords[0] - ox), round(coords[1] - oy),
                              round(coords[2] - ox), round(coords[3] - oy))
            if x2 > x1 and y2 > y1:
                ImageDraw.Draw(shade).rectangle((x1, y1, x2 - 1, y2 - 1), fill=(0, 0, 0, 0))
            self.shade_photo = ImageTk.PhotoImage(shade)
        self.canvas.create_image(ox, oy, image=self.shade_photo, anchor="nw", tags="selection")
        self.canvas.create_rectangle(*coords, outline=SELECTION_COLOR, width=2, tags="selection")
        for hx, hy in self.handle_positions().values():
            self.canvas.create_rectangle(hx - 4, hy - 4, hx + 4, hy + 4,
                                         fill="white", outline=SELECTION_COLOR,
                                         width=2, tags="selection")
        self.region_text.set(f"原图像素坐标：左上角 ({left}, {top})　　右下角 ({right}, {bottom})")
        self.size_text.set(f"输出尺寸：{right - left} × {bottom - top} px")
        if sync_fields:
            self.sync_size_fields()

    def mark_size_edited(self, field):
        if not self.size_syncing:
            self.last_size_field = field
            self.size_dirty = True

    def sync_size_fields(self):
        self.size_syncing = True
        try:
            self.width_text.set(str(self.box[2] - self.box[0]) if self.box else "")
            self.height_text.set(str(self.box[3] - self.box[1]) if self.box else "")
            self.size_dirty = False
        finally:
            self.size_syncing = False

    def apply_typed_size(self):
        if self.busy:
            return False
        if not self.original:
            messagebox.showinfo("提示", "请先选择图片文件夹并加载图片。", parent=self.root)
            return False
        def read_pixels(value):
            text = value.strip()
            if not re.fullmatch(r"[0-9]{1,9}", text) or int(text) <= 0:
                raise ValueError("宽度和高度必须是大于 0 的整数像素值。")
            return int(text)
        try:
            if self.aspect_locked.get() and self.aspect_ratio:
                rw, rh = self.aspect_ratio
                if self.last_size_field == "height":
                    height = read_pixels(self.height_text.get())
                    width = max(1, round(height * rw / rh))
                else:
                    width = read_pixels(self.width_text.get())
                    height = max(1, round(width * rh / rw))
            else:
                width = read_pixels(self.width_text.get())
                height = read_pixels(self.height_text.get())
            iw, ih = self.original.size
            if width > iw or height > ih:
                raise ValueError(f"所需尺寸 {width} × {height} px 超出原图 {iw} × {ih} px。\n请减小尺寸，程序不会自动缩小您输入的尺寸。")
        except ValueError as exc:
            messagebox.showwarning("尺寸无效", str(exc), parent=self.root)
            return False
        # 尽量保持左上角；右边或下边空间不足时平移，确保输出尺寸准确。
        left, top = self.box[:2] if self.box else ((iw - width) // 2, (ih - height) // 2)
        left, top = min(left, iw - width), min(top, ih - height)
        self.box = (left, top, left + width, top + height)
        self.anchor = self.move_box = self.resize_box = self.resize_handle = None
        self.draw_selection()
        self.status_text.set(f"已应用尺寸：{width} × {height} px。请确认选框位置后开始裁剪。")
        return True

    def reset_selection(self):
        if self.busy:
            return
        self.box = self.anchor = self.move_box = None
        self.resize_box = self.resize_handle = None
        self.aspect_locked.set(False)
        self.aspect_ratio = None
        self.aspect_text.set("锁定纵横比")
        self.aspect_choice.set("当前选框比例")
        self.canvas.configure(cursor="crosshair")
        self.draw_selection()

    def select_aspect_preset(self, event=None):
        if self.busy:
            return
        ratio = ASPECT_PRESETS.get(self.aspect_choice.get())
        if ratio is None:
            self.aspect_locked.set(bool(self.box))
            self.toggle_aspect()
            return
        self.aspect_ratio = ratio
        self.aspect_locked.set(True)
        self.aspect_text.set(f"锁定纵横比（{ratio[0]}:{ratio[1]}）")
        self.anchor = self.move_box = self.resize_box = self.resize_handle = None
        if self.box and self.original:
            left, top, right, bottom = self.box
            cx, cy = (left + right) / 2, (top + bottom) / 2
            iw, ih = self.original.size
            r = ratio[0] / ratio[1]
            # 保持选框中心，尽量保持宽度；空间不足时等比例缩小。
            width = min(right - left, 2 * min(cx, iw - cx), 2 * min(cy, ih - cy) * r)
            w, h = max(1, round(width)), max(1, round(width / r))
            x, y = round(cx - w / 2), round(cy - h / 2)
            x, y = max(0, min(iw - w, x)), max(0, min(ih - h, y))
            self.box = (x, y, x + w, y + h)
            self.draw_selection()
        self.status_text.set(f"已选择 {ratio[0]}:{ratio[1]} 固定比例。可拖动画框，或拖动控制点等比例调整。")

    def toggle_aspect(self):
        if self.aspect_locked.get():
            if self.aspect_choice.get() in ASPECT_PRESETS:
                self.select_aspect_preset()
                return
            if not self.box or self.box[2] <= self.box[0] or self.box[3] <= self.box[1]:
                self.aspect_locked.set(False)
                messagebox.showinfo("提示", "请先框选裁剪区域，再锁定纵横比。", parent=self.root)
                return
            w, h = self.box[2] - self.box[0], self.box[3] - self.box[1]
            divisor = math.gcd(w, h)
            self.aspect_ratio = (w // divisor, h // divisor)
            self.aspect_text.set(f"锁定纵横比（{self.aspect_ratio[0]}:{self.aspect_ratio[1]}）")
            self.status_text.set("已锁定当前选框比例，拖动四角或四边可等比例调整大小。")
        else:
            self.aspect_ratio = None
            self.aspect_text.set("锁定纵横比")
            self.status_text.set("已取消比例锁定，可自由调整宽度和高度。")

    def update_preset_text(self):
        if not self.saved_region:
            self.preset_text.set("尚未保存区域；保存后，换文件夹时自动应用到同尺寸图片。")
            return
        left, top, right, bottom = self.saved_region["box"]
        width, height = self.saved_region["image_size"]
        self.preset_text.set(f"已保存：X {left}～{right}，Y {top}～{bottom}；输出 {right-left} × {bottom-top} px；基准 {width} × {height} px")

    def import_reference_image(self):
        if self.busy:
            return
        if not self.original:
            messagebox.showinfo("提示", "请先选择待裁剪的图片文件夹，再导入参考图片。", parent=self.root)
            return
        selected = filedialog.askopenfilename(title="选择参考图片（读取尺寸与比例）", parent=self.root,
                                             filetypes=[("JPG / PNG 图片", "*.jpg *.jpeg *.png"), ("所有文件", "*.*")])
        if not selected:
            return
        try:
            with Image.open(selected) as reference:
                if reference.format not in {"JPEG", "PNG"}:
                    raise ValueError("请选择 JPG / JPEG / PNG 图片。")
                rw, rh = reference.size
                reference.load()
        except Exception as exc:
            messagebox.showerror("参考图片读取失败", str(exc), parent=self.root)
            return
        iw, ih = self.original.size
        scale = min(1, iw / rw, ih / rh)
        width, height = max(1, round(rw * scale)), max(1, round(rh * scale))
        left, top = self.box[:2] if self.box else ((iw - width) // 2, (ih - height) // 2)
        left, top = min(left, iw - width), min(top, ih - height)
        self.box = (left, top, left + width, top + height)
        self.anchor = self.move_box = self.resize_box = self.resize_handle = None
        divisor = math.gcd(rw, rh)
        self.aspect_ratio = (rw // divisor, rh // divisor)
        self.aspect_locked.set(True)
        self.aspect_choice.set(next((name for name, ratio in ASPECT_PRESETS.items()
                                     if ratio == self.aspect_ratio), "当前选框比例"))
        ratio_text = f"{self.aspect_ratio[0]}:{self.aspect_ratio[1]}"
        self.aspect_text.set(f"锁定纵横比（{ratio_text}）")
        self.draw_selection()
        saved = self.save_crop_region()
        summary = f"参考图：{rw} × {rh} px，比例 {ratio_text}。\n选框：{width} × {height} px。"
        if scale < 1:
            summary += "\n参考图尺寸超过当前原图，选框已等比例缩小。"
        summary += ("\n已自动保存，替换之前保存的固定区域。" if saved
                    else "\n当前选框已应用，但未能保存，请稍后点击“保存裁剪区域”重试。")
        summary += "\n可在框内拖动调整位置；调整后点击“保存裁剪区域”更新。"
        self.status_text.set(f"参考图 {rw} × {rh} → 选框 {width} × {height} px；" + ("已保存。" if saved else "未保存。"))
        messagebox.showinfo("已导入参考图片", summary, parent=self.root)

    def save_crop_region(self):
        if self.busy or not self.check_ready():
            return
        data = {"version": 1, "image_size": self.original.size, "box": self.box}
        if self.aspect_locked.get():
            data["aspect_ratio"] = self.aspect_ratio
        try:
            write_saved_region(self.preset_path, data)
        except (OSError, ValueError) as exc:
            messagebox.showerror("保存失败", f"无法保存裁剪区域，上次保存的区域保持不变。\n{exc}", parent=self.root)
            return
        self.saved_region = data
        self.update_preset_text()
        self.status_text.set("已保存当前区域。换文件夹或重启后，加载同尺寸图片时会自动恢复。")
        return True

    def apply_saved_region(self):
        if self.busy:
            return
        if not self.original:
            messagebox.showinfo("提示", "请先选择图片文件夹并加载图片。", parent=self.root)
            return
        if not self.saved_region:
            messagebox.showinfo("提示", "尚未保存裁剪区域，请先框选后点击“保存裁剪区域”。", parent=self.root)
            return
        size = self.saved_region["image_size"]
        if self.original.size != size:
            messagebox.showwarning("原图尺寸不同", f"保存区域的基准：{size[0]} × {size[1]} px\n当前原图：{self.original.width} × {self.original.height} px\n\n为保持位置对应，未应用保存区域。请重新框选。", parent=self.root)
            return
        self.anchor = self.move_box = self.resize_box = self.resize_handle = None
        self.box = self.saved_region["box"]
        self.aspect_ratio = self.saved_region.get("aspect_ratio")
        self.aspect_locked.set(self.aspect_ratio is not None)
        self.aspect_choice.set(next((name for name, ratio in ASPECT_PRESETS.items()
                                     if self.aspect_ratio and ratio[0] * self.aspect_ratio[1] == ratio[1] * self.aspect_ratio[0]),
                                    "当前选框比例"))
        self.aspect_text.set(f"锁定纵横比（{self.aspect_ratio[0]}:{self.aspect_ratio[1]}）" if self.aspect_ratio else "锁定纵横比")
        self.canvas.configure(cursor="crosshair")
        self.draw_selection()
        self.status_text.set("已应用保存的固定区域，原图坐标和输出尺寸完全一致。可直接开始批量裁剪。")

    def check_ready(self):
        if not self.folder:
            messagebox.showinfo("提示", "请先选择图片文件夹。", parent=self.root)
        elif not self.files:
            messagebox.showinfo("提示", "当前文件夹中没有可处理的图片。", parent=self.root)
        elif not self.original:
            messagebox.showinfo("提示", "请先在第一张图片上选择裁剪区域。", parent=self.root)
        else:
            if self.size_dirty and not self.apply_typed_size():
                return False
            if not self.box:
                messagebox.showinfo("提示", "请先在第一张图片上选择裁剪区域。", parent=self.root)
                return False
            return True
        return False

    def preview_crop(self):
        if not self.check_ready():
            return
        popup = tk.Toplevel(self.root)
        popup.title("裁剪结果预览")
        popup.transient(self.root)
        with self.original.crop(self.box) as cropped:
            size = fit_image(cropped.size, (min(1000, self.root.winfo_screenwidth() - 100), min(700, self.root.winfo_screenheight() - 180)))
            with cropped.resize(size, Image.Resampling.LANCZOS) as thumb:
                photo = ImageTk.PhotoImage(thumb)
            ttk.Label(popup, text=f"输出尺寸：{cropped.width} × {cropped.height} px（预览按比例显示）", padding=12).pack()
        label = ttk.Label(popup, image=photo)
        label.image = photo
        label.pack(padx=12, pady=(0, 12))
        ttk.Button(popup, text="关闭预览", command=popup.destroy).pack(pady=(0, 12))

    def start_batch(self):
        if self.busy or not self.check_ready():
            return
        try:
            files = find_images(self.folder)
        except OSError as exc:
            messagebox.showerror("无法读取文件夹", str(exc), parent=self.root)
            return
        if not files:
            messagebox.showinfo("没有图片", "当前文件夹中没有可处理的图片。", parent=self.root)
            return
        self.set_busy(True)
        self.progress["value"] = 0
        args = (self.folder, files, self.original.size, self.box)

        def worker():
            try:
                result = batch_crop(*args, progress=lambda *p: self.events.put(("progress", p)))
                self.events.put(("done", result))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def set_busy(self, busy):
        self.busy = busy
        self.aspect_combo.configure(state="disabled" if busy else "readonly")
        for button in (self.folder_button, self.reset_button, self.preview_button, self.start_button,
                       self.save_region_button, self.apply_region_button, self.aspect_button,
                       self.width_entry, self.height_entry, self.apply_size_button, self.gif_button,
                       self.reference_button):
            button.configure(state="disabled" if busy else "normal")

    def poll_events(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "progress":
                    phase, current, total = data
                    self.status_text.set(f"{phase}：{current} / {total}")
                    self.progress["value"] = 100 * current / max(1, total)
                elif kind == "done":
                    self.set_busy(False)
                    self.progress["value"] = 100
                    output, success, issues = data
                    self.status_text.set(f"处理完成：成功 {success} 张，跳过 / 失败 {len(issues)} 张。")
                    self.show_result(output, success, issues)
                elif kind == "gif_done":
                    self.set_busy(False)
                    self.progress["value"] = 100
                    output, success, issues = data
                    self.status_text.set(f"GIF 已生成：{success} 帧，跳过 {len(issues)} 张。")
                    self.show_result(output, success, issues, gif=True)
                elif kind == "error":
                    self.set_busy(False)
                    self.status_text.set("任务未完成，请查看错误提示。")
                    messagebox.showerror("处理失败", data, parent=self.root)
        except queue.Empty:
            pass
        self.poll_job = self.root.after(100, self.poll_events)

    def select_gif_folder(self):
        if self.busy:
            return
        folder = filedialog.askdirectory(title="选择用于生成 GIF 的图片文件夹", parent=self.root)
        if folder:
            self.open_gif_dialog(Path(folder))

    def open_gif_dialog(self, folder):
        if self.busy:
            return
        try:
            files = find_images(folder)
        except OSError as exc:
            messagebox.showerror("无法读取文件夹", str(exc), parent=self.root)
            return
        if len(files) < 2:
            messagebox.showinfo("图片不足", "至少需要 2 张 JPG / JPEG / PNG 图片才能生成动图。", parent=self.root)
            return
        popup = tk.Toplevel(self.root)
        popup.title("生成 GIF 动图")
        popup.geometry(f"580x{min(680, self.root.winfo_screenheight() - 100)}")
        popup.minsize(520, 500)
        popup.transient(self.root)
        popup.grab_set()
        frame = ttk.Frame(popup, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"图片文件夹：{folder}\n检测到 {len(files)} 张图片，按文件名自然顺序播放。", wraplength=480).pack(anchor="w")
        selection_row = ttk.Frame(frame)
        selection_row.pack(fill="x", pady=(10, 5))
        selected = [True] * len(files)
        selection_text = tk.StringVar()
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True)
        list_style = ttk.Style(popup)
        list_style.configure("GifImages.Treeview", rowheight=26)
        list_style.map("GifImages.Treeview", background=[("selected", "#eef3ff")],
                       foreground=[("selected", "#17212b")])
        images = ttk.Treeview(list_frame, columns=("filename",), show="tree headings",
                              selectmode="browse", height=1, name="gif_images", style="GifImages.Treeview")
        images.heading("#0", text="选择")
        images.heading("filename", text="图片名称（宝蓝色：已选；白色：未选）")
        images.column("#0", width=58, stretch=False)
        images.column("filename", width=380, minwidth=180)
        # 用图片绘制实心色块，避免不同 Windows 字体的勾选符号过小。
        icons = {}
        for checked in (True, False):
            with Image.new("RGBA", (20, 20), (0, 0, 0, 0)) as icon:
                ImageDraw.Draw(icon).rectangle((1, 1, 18, 18),
                                                fill=SELECTION_COLOR if checked else "white",
                                                outline=SELECTION_COLOR if checked else "#8b95a5", width=2)
                icons[checked] = ImageTk.PhotoImage(icon)
        images.selection_icons = icons  # 保留引用直到窗口关闭。
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=images.yview)
        images.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        images.pack(side="left", fill="both", expand=True)
        for index, path in enumerate(files):
            images.insert("", "end", iid=str(index), image=icons[True], values=(path.name,))

        def update_selection():
            selection_text.set(f"已选择 {sum(selected)} / {len(files)} 张")

        def select_all(value):
            for index, path in enumerate(files):
                selected[index] = value
                images.item(str(index), image=icons[value])
            update_selection()

        def toggle_image(event):
            item = images.focus() if event.keysym == "space" else images.identify_row(event.y)
            if item:
                index = int(item)
                selected[index] = not selected[index]
                images.item(item, image=icons[selected[index]])
                images.focus(item)
                images.selection_set(item)
                images.focus_set()
                update_selection()
                return "break"

        ttk.Button(selection_row, text="全选", command=lambda: select_all(True)).pack(side="left")
        ttk.Button(selection_row, text="取消全选", command=lambda: select_all(False)).pack(side="left", padx=8)
        ttk.Label(selection_row, textvariable=selection_text).pack(side="right")
        images.bind("<Button-1>", toggle_image)
        images.bind("<space>", toggle_image)
        update_selection()
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(16, 8))
        duration_var = tk.StringVar(value="100")
        width_var = tk.StringVar(value="960")
        ttk.Label(row, text="每帧时长（毫秒）：").pack(side="left")
        ttk.Entry(row, textvariable=duration_var, width=8).pack(side="left")
        row2 = ttk.Frame(frame)
        row2.pack(fill="x", pady=(0, 12))
        ttk.Label(row2, text="输出宽度（像素）：").pack(side="left")
        ttk.Entry(row2, textvariable=width_var, width=8).pack(side="left")
        ttk.Label(frame, text="100 毫秒 = 每秒 10 帧；数值越小播放越快。\n时长范围 20～10000 毫秒，按 10 毫秒取整。\n宽度 0 表示原尺寸；否则等比缩小，不放大。\nGIF 循环播放；每帧最多 256 色，透明区域使用白底。\n只合成此文件夹中的图片，不套用主界面的裁剪框。", foreground="#555555").pack(anchor="w")
        error_text = tk.StringVar()
        ttk.Label(frame, textvariable=error_text, foreground="#b42318", wraplength=480).pack(anchor="w", pady=8)

        def start():
            chosen_files = [path for path, checked in zip(files, selected) if checked]
            if len(chosen_files) < 2:
                error_text.set("请至少选择 2 张图片；可点击“全选”或逐张勾选。")
                return
            try:
                duration = int(duration_var.get())
                width = int(width_var.get())
                if not 20 <= duration <= 10000 or not 0 <= width <= 65535:
                    raise ValueError()
            except ValueError:
                error_text.set("请输入整数：每帧时长 20～10000 毫秒，宽度 0～65535 像素。")
                return
            popup.destroy()
            self.start_gif(folder, chosen_files, duration, width)

        ttk.Button(frame, text="生成 GIF", command=start).pack(side="right")
        ttk.Button(frame, text="取消", command=popup.destroy).pack(side="left")

    def start_gif(self, folder, files, duration, width):
        if self.busy:
            return
        self.set_busy(True)
        self.progress["value"] = 0
        self.status_text.set("正在准备 GIF 图片…")

        def worker():
            try:
                result = create_gif(folder, files, duration, width,
                                    progress=lambda *p: self.events.put(("progress", p)))
                self.events.put(("gif_done", result))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def show_result(self, output, success, issues, gif=False):
        popup = tk.Toplevel(self.root)
        popup.title("GIF 已生成" if gif else "处理完成")
        popup.geometry("650x420" if issues else "650x230")
        popup.transient(self.root)
        popup.grab_set()
        frame = ttk.Frame(popup, padding=16)
        frame.pack(fill="both", expand=True)
        summary = f"已生成 GIF：{success} 帧" if gif else f"成功裁剪：{success} 张"
        ttk.Label(frame, text=f"{summary}\n跳过 / 失败：{len(issues)} 张", font=("Microsoft YaHei UI", 11), justify="left").pack(anchor="w")
        ttk.Label(frame, text="输出位置：").pack(anchor="w", pady=(12, 4))
        path_var = tk.StringVar(value=str(output))
        entry = ttk.Entry(frame, textvariable=path_var, state="readonly")
        entry.pack(fill="x")
        if issues:
            ttk.Label(frame, text="未处理的图片及原因（可选中复制）：").pack(anchor="w", pady=(12, 4))
            area = ttk.Frame(frame)
            area.pack(fill="both", expand=True)
            detail = tk.Text(area, height=8, wrap="word")
            scroll = ttk.Scrollbar(area, command=detail.yview)
            detail.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            detail.pack(side="left", fill="both", expand=True)
            detail.insert("1.0", "\n".join(f"{name}：{reason}" for name, reason in issues))
            detail.configure(state="disabled")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))

        def open_output():
            try:
                os.startfile(output.parent if gif else output)
            except OSError as exc:
                messagebox.showerror("无法打开文件夹", str(exc), parent=popup)

        ttk.Button(buttons, text="打开结果文件夹", command=open_output).pack(side="left")
        if not gif and success >= 2:
            def make_gif():
                popup.destroy()
                self.open_gif_dialog(output)
            ttk.Button(buttons, text="用裁剪结果生成 GIF", command=make_gif).pack(side="left", padx=10)
        ttk.Button(buttons, text="关闭", command=popup.destroy).pack(side="right")

    def close(self):
        if self.busy:
            messagebox.showinfo("正在处理", "正在保存裁剪结果，请等待处理完成后关闭。", parent=self.root)
            return
        if self.original:
            self.original.close()
        if self.resize_job:
            self.root.after_cancel(self.resize_job)
        self.root.after_cancel(self.poll_job)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = CropperApp(root)
    root.mainloop()
