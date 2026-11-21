import os
import sys

from PIL import Image, ImageDraw

BASE = r"H:\2026-10-21-12-29-43\mc-launcher"
GLYPH = os.path.join(BASE, "assets", "icon-glyph.png")   # 透明底六边形，由 image_to_icon.py 生成
RES = r"H:\2026-10-21-12-29-43\android-build\zalith-src\ZalithLauncher\src\main\res"

CREAM = (245, 241, 222, 255)
DENS = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}

sys.path.insert(0, BASE)
from make_icon import center_glyph   # 与桌面版同一套重心居中，避免六边形偏左

glyph = center_glyph(Image.open(GLYPH).convert("RGBA"))


def fit(img_h):
    """按高度缩放，保持六边形长宽比"""
    w = round(glyph.width * img_h / glyph.height)
    return glyph.resize((w, img_h), Image.LANCZOS)


def on_cream(size, ratio):
    """米白底方形图标，图形占 ratio 高度"""
    canvas = Image.new("RGBA", (size, size), CREAM)
    g = fit(int(size * ratio))
    canvas.alpha_composite(g, ((size - g.width) // 2, (size - g.height) // 2))
    return canvas


def save(img, dens, name):
    d = os.path.join(RES, f"mipmap-{dens}")
    os.makedirs(d, exist_ok=True)
    img.save(os.path.join(d, name))


for dens, scale in DENS.items():
    s = int(48 * scale)

    # 普通图标（旧设备 / 不做遮罩的启动器）
    save(on_cream(s, 0.78), dens, "ic_launcher.png")

    # 圆形图标
    base = on_cream(s, 0.74)
    mask = Image.new("L", (s * 4, s * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, s * 4 - 1, s * 4 - 1), fill=255)
    mask = mask.resize((s, s), Image.LANCZOS)
    rnd = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    rnd.paste(base, (0, 0), mask)
    save(rnd, dens, "ic_launcher_round.png")

    # 自适应前景：108dp 画布，六边形占 52%（72dp 可视区内约 78%，落在安全区）
    f = int(108 * scale)
    canvas = Image.new("RGBA", (f, f), (0, 0, 0, 0))
    g = fit(int(f * 0.52))
    canvas.alpha_composite(g, ((f - g.width) // 2, (f - g.height) // 2))
    save(canvas, dens, "ic_launcher_foreground.png")

print("icons done")
