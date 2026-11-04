"""把主人画的羽毛笔做成应用图标。

原图是白底黑线（纯白 255，墨色最深 29）。做法：
1. 用亮度反推 alpha —— 抗锯齿的灰边会得到半透明，边缘不会发毛；
2. 墨色统一成 #1f1f1f（本来就是单色线稿，统一后小尺寸更清晰）；
3. 裁到内容框，等比缩进正方形画布（留 8% 边）；
4. 垫一层白底圆角板 —— 任务栏是深色的，纯透明线稿在深底上等于隐形；
5. 导出 png（1024）+ ico（16/24/32/48/64/128/256）+ 网页用的小 png。
"""
from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "app" / "icon-source.png"
INK = (31, 31, 31)
PLATE = (255, 255, 255)
CANVAS = 1024
ART_RATIO = 0.84          # 内容占画布的比例（留白）
RADIUS_RATIO = 0.19       # 圆角半径


def load_art() -> Image.Image:
    im = Image.open(SRC).convert("RGB")
    lum = im.convert("L")
    # 亮度 29（墨）→ alpha 255；255（纸）→ alpha 0，中间线性
    alpha = lum.point(lambda v: max(0, min(255, round((255 - v) * 255 / 226))))
    art = Image.new("RGBA", im.size, INK + (0,))
    art.putalpha(alpha)
    box = alpha.getbbox()
    return art.crop(box)


def compose(art: Image.Image, size: int) -> Image.Image:
    plate = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(plate)
    r = int(size * RADIUS_RATIO)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=r, fill=PLATE + (255,))

    # 线稿是细笔触：小尺寸直接缩会糊成灰雾。先把 alpha 撑粗一档再缩，笔画才立得住。
    src = art
    if size <= 32:
        src = art.copy()
        src.putalpha(art.getchannel("A").filter(ImageFilter.MaxFilter(5)))
    elif size <= 48:
        src = art.copy()
        src.putalpha(art.getchannel("A").filter(ImageFilter.MaxFilter(3)))

    target = int(size * ART_RATIO)
    w, h = src.size
    scale = min(target / w, target / h)
    small = src.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                       Image.LANCZOS)
    if size <= 48:
        # 缩完再把淡掉的 alpha 拉回来：细线缩小时会被平均成 40% 灰，
        # 提上来才是"黑线"，不然 16px 上就是一层雾。
        a = small.getchannel("A").point(lambda v: min(255, round(v * 2.6)))
        small.putalpha(a)
    plate.alpha_composite(small, ((size - small.width) // 2, (size - small.height) // 2))
    return plate


def write_ico(frames: list[Image.Image], path: Path) -> None:
    """自己拼 ICO 容器。

    不用 Pillow 的 `save(sizes=...)`：那是拿一张大图内部再缩，缩法不受控，
    我特意为 16/24/32 调过的"加粗 + 提 alpha"会被丢掉。这里每层都用现成的帧。
    """
    import struct

    blobs = []
    for im in frames:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        blobs.append(buf.getvalue())

    head = struct.pack("<HHH", 0, 1, len(frames))
    offset = len(head) + 16 * len(frames)
    entries, body = b"", b""
    for im, blob in zip(frames, blobs):
        w = 0 if im.width >= 256 else im.width
        h = 0 if im.height >= 256 else im.height
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        body += blob
    path.write_bytes(head + entries + body)


def main() -> None:
    art = load_art()
    big = compose(art, CANVAS)
    big.save(ROOT / "app" / "desktop.png")

    sizes = (16, 24, 32, 48, 64, 128, 256)
    write_ico([compose(art, s) for s in sizes], ROOT / "app" / "desktop.ico")

    # 网页标签页用：浏览器不认 ico 里的 256 层，给两个小 png
    (ROOT / "app" / "web").mkdir(exist_ok=True)
    compose(art, 64).save(ROOT / "app" / "web" / "icon-64.png")
    compose(art, 192).save(ROOT / "app" / "web" / "icon-192.png")

    for name in ("app/desktop.png", "app/desktop.ico", "app/web/icon-64.png", "app/web/icon-192.png"):
        p = ROOT / name
        print(name, p.stat().st_size, "B")


if __name__ == "__main__":
    main()
