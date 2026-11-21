"""由六边形母版生成启动器图标（icon.png / icon.ico / brand.png）。

母版：assets/icon-glyph.png —— 透明底六边形标志，由 image_to_icon.py 从原图抠出。
输出：
  icon.png  1024x1024 米白底方形（窗口图标 / 网站图标）
  icon.ico  16~256 多尺寸透明底（PyInstaller --icon、任务栏、资源管理器）
  assets/brand.png  512x512 透明底标志（界面侧栏顶部的品牌标记）

注意：标志右侧带立体描边/投影，按包围盒居中会整体偏左 2~3px。
这里统一按「不透明区域的重心」居中（center_glyph），图标、多尺寸帧、品牌标记都基于它。

运行：python make_icon.py
"""
import os

import numpy as np
from PIL import Image

BASE = os.path.dirname(os.path.abspath(__file__))
GLYPH = os.path.join(BASE, "assets", "icon-glyph.png")
PNG_OUT = os.path.join(BASE, "icon.png")
ICO_OUT = os.path.join(BASE, "icon.ico")
BRAND_OUT = os.path.join(BASE, "assets", "brand.png")

CREAM = (245, 241, 222, 255)
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def center_glyph(img):
    """把图形放进正方形画布，使不透明部分的重心正好在画布中心。"""
    a = np.asarray(img.getchannel("A")).astype(np.float64)
    total = a.sum()
    h, w = a.shape
    if total <= 0:
        return img
    ys, xs = np.indices(a.shape)
    cx = float((xs * a).sum() / total)
    cy = float((ys * a).sum() / total)
    # 画布边长要保证四个方向都装得下
    side = int(max(2 * cx, 2 * (w - cx), 2 * cy, 2 * (h - cy))) + 4
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.alpha_composite(img, (round(side / 2 - cx), round(side / 2 - cy)))
    return canvas


def build():
    glyph = center_glyph(Image.open(GLYPH).convert("RGBA"))

    def fit(h, src=glyph):
        return src.resize((max(1, round(src.width * h / src.height)), h), Image.LANCZOS)

    # 方形图标：米白底 + 图形占 74% 高度
    S = 1024
    sq = Image.new("RGBA", (S, S), CREAM)
    g = fit(int(S * 0.74))
    sq.alpha_composite(g, ((S - g.width) // 2, (S - g.height) // 2))
    sq.save(PNG_OUT)

    # ico：透明底；小尺寸多留边，避免贴边
    frames = []
    for s in ICO_SIZES:
        ratio = 0.86 if s <= 24 else 0.92
        f = fit(int(s * ratio))
        c = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        c.alpha_composite(f, ((s - f.width) // 2, (s - f.height) // 2))
        frames.append(c)
    frames[-1].save(ICO_OUT, format="ICO", sizes=[(s, s) for s in ICO_SIZES],
                    append_images=frames[:-1])

    # 界面内品牌标志：正方形透明底，缩放后仍居中（侧栏顶部用）
    mark = fit(512)
    brand = Image.new("RGBA", (mark.width, mark.width), (0, 0, 0, 0))
    brand.alpha_composite(mark, (0, (mark.width - mark.height) // 2))
    brand.save(BRAND_OUT)
    print(f"icon.png {sq.size}, icon.ico {ICO_SIZES}, brand.png {brand.size}")


if __name__ == "__main__":
    build()
