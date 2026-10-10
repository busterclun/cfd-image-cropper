"""逐帧编码 GIF，避免将几百张图片同时保存在内存中。"""
from pathlib import Path

from PIL import GifImagePlugin, Image


def create_gif(folder, files, duration=100, output_width=960, progress=None):
    if type(duration) is not int or not 20 <= duration <= 10000:
        raise ValueError("每帧时长必须为 20～10000 毫秒的整数。")
    if type(output_width) is not int or not 0 <= output_width <= 65535:
        raise ValueError("输出宽度必须为 0～65535 的整数，0 表示原尺寸。")
    # GIF 时长以 10 毫秒为单位。
    duration = max(20, round(duration / 10) * 10)
    folder = Path(folder)
    index = 1
    while True:
        output = folder / ("动画.gif" if index == 1 else f"动画_{index}.gif")
        try:
            stream = output.open("xb")
            break
        except FileExistsError:
            index += 1
    count, issues, baseline, target = 0, [], None, None
    try:
        with stream:
            for index, path in enumerate(files, 1):
                try:
                    with Image.open(path) as source:
                        if source.format not in {"JPEG", "PNG"}:
                            raise ValueError("文件内容不是 JPG / PNG 图片")
                        source.load()
                        if baseline is not None and source.size != baseline:
                            raise ValueError(f"尺寸不同：{source.width} × {source.height}，基准 {baseline[0]} × {baseline[1]}")
                        if baseline is None:
                            width = min(source.width, output_width) if output_width else source.width
                            target = (width, max(1, round(source.height * width / source.width)))
                            if max(target) > 65535:
                                raise ValueError("图片尺寸超出 GIF 限制，请减小输出宽度")
                        # GIF 透明度有限，统一在白底上合成，避免 PPT 上出现黑底。
                        with source.convert("RGBA") as rgba:
                            with Image.new("RGB", source.size, "white") as rgb:
                                rgb.paste(rgba, mask=rgba.getchannel("A"))
                                with rgb.resize(target, Image.Resampling.LANCZOS) as scaled:
                                    frame = scaled.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
                        with frame:
                            header = GifImagePlugin.getheader(frame, info={"loop": 0})[0] if count == 0 else []
                            data = GifImagePlugin.getdata(frame, duration=duration, disposal=2,
                                                          include_color_table=count > 0)
                        source_size = source.size
                except Exception as exc:
                    issues.append((path.name, str(exc)))
                else:
                    # 写入错误是任务级错误，不能留下半帧后继续写下一帧。
                    for chunk in (*header, *data):
                        stream.write(chunk)
                    baseline = source_size
                    count += 1
                if progress:
                    progress("正在生成 GIF", index, len(files))
            if count < 2:
                detail = "\n".join(f"{name}：{reason}" for name, reason in issues[:8])
                raise ValueError("至少需要 2 张可读取且尺寸相同的图片才能生成动图。" + ("\n" + detail if detail else ""))
            stream.write(b";")  # GIF 文件结束标记
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output, count, issues
