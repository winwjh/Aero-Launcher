"""从一张带米白底的方图里抠出六边形标志，产出图标母版。

输入：assets/icon-source.jpg（原图，1920x1920，六边形居中、米白底、右下角有水印）
输出：
  assets/icon-glyph.png  透明底、紧贴六边形的母版（高 1200px）
  icon.png               1024x1024 米白底方形图标（桌面窗口 / 网站 favicon）
  icon.ico               16~256 多尺寸，透明底（PyInstaller --icon）

做法：从四角泛洪填充米白色 -> 得到背景掩膜；边界按到米白的色差做软过渡，
避免缩放后在深色背景上出现一圈浅色描边。水印在六边形 bbox 之外，裁切时自然丢弃。
"""
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "assets", "icon-source.jpg")
GLYPH = os.path.join(BASE, "assets", "icon-glyph.png")
PNG_OUT = os.path.join(BASE, "icon.png")
ICO_OUT = os.path.join(BASE, "icon.ico")

CREAM = (245, 241, 222)          # 图标底色（比源图略暖，和 launcher 的 #F0EDE4 协调）


def detect_bg(im):
    """从四角各取 24x24 的中位数当作背景色，避免每换一张图都要手改常量。"""
    a = np.asarray(im).astype(np.int16)
    k = 24
    patches = [a[:k, :k], a[:k, -k:], a[-k:, :k], a[-k:, -k:]]
    return np.median(np.concatenate([p.reshape(-1, 3) for p in patches]), axis=0)


def cut_glyph():
    im = Image.open(SRC).convert("RGB")
    BG = detect_bg(im)
    print("bg", BG)
    rgb = im.copy()
    # 泛洪填充：把连通的米白区域刷成哨兵色
    sentinel = (255, 0, 255)
    w, h = rgb.size
    img_w, img_h = w, h
    for seed in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        ImageDraw.floodfill(rgb, seed, sentinel, thresh=42)

    arr = np.asarray(rgb).astype(np.int16)
    bg_mask = (arr[:, :, 0] == 255) & (arr[:, :, 1] == 0) & (arr[:, :, 2] == 255)

    src = np.asarray(im).astype(np.int16)
    dist = np.abs(src - BG).max(axis=2)
    soft = np.clip(dist / 70.0, 0.0, 1.0) * 255.0

    # 只在物体外缘 4px 内做软过渡，其余背景直接全透明（背景有噪点，不能整片给软 alpha）
    opaque = (~bg_mask).astype(np.uint8) * 255
    near = np.asarray(
        Image.fromarray(opaque).filter(ImageFilter.MaxFilter(9))
    ) > 0
    alpha = np.where(bg_mask, np.where(near, soft, 0.0), 255.0)
    alpha[alpha < 20] = 0.0

    # 只保留与六边形主体连通的部分：右下角的水印是独立小块，会被丢掉
    probe = Image.fromarray(np.dstack([alpha] * 3).astype(np.uint8), "RGB")
    ImageDraw.floodfill(probe, (img_w // 2, img_h // 2), (255, 0, 0), thresh=0)
    keep = np.asarray(probe)
    linked = (keep[:, :, 0] == 255) & (keep[:, :, 1] == 0) & (keep[:, :, 2] == 0)
    alpha[~linked] = 0.0
    out = np.dstack([np.asarray(im), alpha.astype(np.uint8)]).astype(np.uint8)

    img = Image.fromarray(out, "RGBA")
    bbox = img.getbbox()
    img = img.crop(bbox)
    # 上下各留 2px 透明，避免贴边被裁
    pad = Image.new("RGBA", (img.width + 4, img.height + 4), (0, 0, 0, 0))
    pad.paste(img, (2, 2))
    return pad


def main():
    glyph = cut_glyph()
    # 母版统一高度 1200，保持长宽比
    th = 1200
    tw = round(glyph.width * th / glyph.height)
    glyph = glyph.resize((tw, th), Image.LANCZOS)
    os.makedirs(os.path.dirname(GLYPH), exist_ok=True)
    glyph.save(GLYPH)
    print(f"glyph {glyph.size} -> {GLYPH}")

    # icon.png / icon.ico 统一由 make_icon.py 产出，避免两处逻辑漂移
    from make_icon import build
    build()


if __name__ == "__main__":
    main()
