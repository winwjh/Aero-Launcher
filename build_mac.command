#!/bin/bash
# ============================================================
#  Aero Launcher macOS 一键构建脚本
#  用法：把整个 mc-launcher 文件夹拷到 Mac 上，然后在终端执行：
#      cd mc-launcher
#      bash build_mac.command
#  完成后在「AeroLauncher-发布版-mac」文件夹得到成品
# ============================================================
set -e
cd "$(dirname "$0")"
echo "==> [1/6] 检查 Python3 ..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3，请先安装：https://www.python.org/downloads/ 或执行 xcode-select --install"
  exit 1
fi
python3 --version

echo "==> [2/6] 创建虚拟环境并安装依赖（首次约需几分钟）..."
python3 -m venv .venv-mac
./.venv-mac/bin/pip install --upgrade pip -q
# 优先用清华镜像，失败则用官方源
./.venv-mac/bin/pip install PySide6 flask requests pyinstaller \
  -i https://pypi.tuna.tsinghua.edu.cn/simple -q \
  || ./.venv-mac/bin/pip install PySide6 flask requests pyinstaller -q

echo "==> [3/6] 生成 macOS 图标（icon.icns）..."
APP_ICON_ARG=""
if command -v sips >/dev/null 2>&1 && [ -f icon_src_backup.ico ]; then
  rm -rf icon.iconset && mkdir icon.iconset
  for s in 16 32 128 256 512; do
    sips -z $s $s icon_src_backup.ico --out icon.iconset/icon_${s}x${s}.png >/dev/null 2>&1 || true
    d=$((s*2))
    sips -z $d $d icon_src_backup.ico --out icon.iconset/icon_${s}x${s}@2x.png >/dev/null 2>&1 || true
  done
  if iconutil -c icns icon.iconset -o icon.icns 2>/dev/null; then
    APP_ICON_ARG="--icon=icon.icns"
    echo "    已生成 icon.icns（Dock/访达将显示 MCL 图标）"
  else
    echo "    图标转换失败，使用默认图标（不影响功能）"
  fi
  rm -rf icon.iconset
fi

echo "==> [4/6] 打包 AeroLauncher.app ..."
./.venv-mac/bin/pyinstaller --noconfirm --windowed \
  $APP_ICON_ARG \
  --add-data "static:static" \
  --add-data "icon_src_backup.ico:." \
  --name AeroLauncher --distpath dist --workpath build --specpath . \
  mc_desktop.py

echo "==> [5/6] 去除隔离属性（否则首次打开会被 Gatekeeper 拦截）..."
xattr -cr dist/AeroLauncher.app 2>/dev/null || true

echo "==> [6/6] 整理干净发布目录 ..."
PUB="../AeroLauncher-发布版-mac"
rm -rf "$PUB"
mkdir -p "$PUB"
cp -R dist/AeroLauncher.app "$PUB/"
# 打成 zip 方便传给别人（zip 会保留结构，解压即用）
cd "$PUB" && zip -qry AeroLauncher-mac.zip AeroLauncher.app

echo ""
echo "✅ 构建完成！"
echo "   成品在：$PUB"
echo "     ├── AeroLauncher.app        ← 双击运行，可拖入「应用程序」"
echo "     └── AeroLauncher-mac.zip    ← 发给别人就用这个"
echo "   游戏文件存放在 AeroLauncher.app 同目录的 .minecraft 中"
echo "  （建议把 app 拷到一个固定文件夹再运行）。"
