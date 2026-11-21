# -*- coding: utf-8 -*-
"""Aero Launcher 桌面版 v3.0
极简图标侧栏 + 全屏渐变背景 + 右下角分体启动按钮（Windows / macOS / Linux）
内嵌 server.py 后端，界面通过本地 HTTP API 通信。
"""
import sys
import os
import re
import math
import random
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache

import requests

import server as backend
import error_analyzer
from mc_i18n import tr, load_saved_lang, get_lang

APP_VERSION = "3.4.56"
# 默认更新源（Cloudflare Pages 托管的 update.json，可在设置里改）
DEFAULT_UPDATE_URL = "https://mclauncher-update.pages.dev/update.json"
# 赞助入口：主用微信赞赏码（本地图片，离线可用），网页作为备用打开方式
SPONSOR_URL = "https://mclauncher-update.pages.dev/#sponsor"


def _free_port(start):
    for p in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0


PORT = _free_port(8849) or 8849
BASE = f"http://127.0.0.1:{PORT}"


_last_session = None


def start_backend():
    from werkzeug.serving import make_server
    global _last_session
    os.makedirs(backend.MC_DIR, exist_ok=True)
    backend.refresh_installed()
    try:
        backend.repair_merged_versions()
    except Exception:
        pass
    backend.log(f"{backend.LAUNCHER_NAME}{tr(' 桌面版已启动，游戏目录: ')}{backend.MC_DIR}")
    # 后台先扫一遍本机 Java：第一次扫盘要几百毫秒，别等到用户点进设置页才做
    threading.Thread(target=lambda: backend.all_javas(force=True), daemon=True).start()
    # 先查上次会话再开新会话，不然会把「自己正在跑」当成「上次没退干净」
    try:
        _last_session = backend.load_last_session()
    except Exception:
        _last_session = None
    try:
        backend.start_session(APP_VERSION)
    except Exception:
        _last_session = None
    srv = make_server("127.0.0.1", PORT, backend.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


from PySide6.QtCore import (Qt, QThread, QObject, Signal, QTimer, QRect, QRectF, QSize, QSettings,
                            QPoint, QPointF, QUrl, QProcess, QPropertyAnimation, QEasingCurve,
                            QParallelAnimationGroup, QEvent, QVariantAnimation, QAbstractAnimation)
from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QLinearGradient, QRadialGradient,
    QPen, QPainterPath, QShortcut, QKeySequence, QDesktopServices, QCursor,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QLineEdit, QComboBox, QProgressBar, QListWidget, QListWidgetItem,
    QPlainTextEdit, QStackedWidget, QFrame, QMessageBox, QMenu, QGraphicsDropShadowEffect,
    QListView,
    QScrollArea, QCheckBox, QColorDialog, QSlider, QFileDialog, QDialog,
    QProgressDialog, QInputDialog, QSizePolicy, QGraphicsOpacityEffect,
    QStyledItemDelegate,
)

ACCENT = "#1fa04a"
ACCENT_HOVER = "#25b354"
TEXT = "#22282d"
SUB = "#7b858d"

# ---------------- 可自定义外观（设置页可改，存 QSettings） ----------------
DEFAULT_THEME = {"accent": "#1fa04a", "bg": "#fdfaf3", "opacity": 1.0,
                 "bg_image": "", "wp": "off"}
THEME = dict(DEFAULT_THEME)


def _mix(c1, c2, t):
    """两色混合，t=0 取 c1，t=1 取 c2"""
    a, b = QColor(c1), QColor(c2)
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t)).name()


def theme_colors():
    """由「主题色 + 背景色」派生出整套配色"""
    ac = THEME.get("accent") or DEFAULT_THEME["accent"]
    bg = THEME.get("bg") or DEFAULT_THEME["bg"]
    return {
        "accent": ac,
        "accent_hover": _mix(ac, "#ffffff", 0.16),
        "accent_soft": _mix(ac, "#ffffff", 0.90),   # 选中行浅底
        "accent_soft2": _mix(ac, "#ffffff", 0.84),  # 次级浅底
        "accent_dark": _mix(ac, "#000000", 0.30),   # 浅底上的深字
        "accent_line": _mix(ac, "#ffffff", 0.58),   # 浅底按钮的描边
        "bg": bg,
        "bg_top": _mix(bg, "#ffffff", 0.55),
        "bg_bottom": _mix(bg, "#000000", 0.035),
        "rail": _mix(bg, "#ffffff", 0.35),
        "line": _mix(bg, "#000000", 0.14),
        "line_soft": _mix(bg, "#000000", 0.09),
        "surface": _mix(bg, "#ffffff", 0.62),
    }

# 程序图标（打包后从 _MEIPASS 取，开发时取脚本同目录）
if getattr(sys, "frozen", False):
    _ICON_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
else:
    _ICON_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(_ICON_DIR, "icon.ico")
ICON_PNG = os.path.join(_ICON_DIR, "icon.png")
# 赞助码图片（assets/wechat-reward.png；打包后随 _MEIPASS 一起释放）
SPONSOR_QR_PATH = os.path.join(_ICON_DIR, "assets", "wechat-reward.png")


def app_icon():
    """窗口/任务栏图标：依次找 ico/icns，缺失时退回矢量图标"""
    for _name in ("icon.ico", "icon.icns", "icon_src_backup.ico"):
        _p = os.path.join(_ICON_DIR, _name)
        if os.path.isfile(_p):
            return QIcon(_p)
    return QIcon(make_icon("home", 32, ACCENT))


def brand_pixmap(size=44):
    """界面里的品牌标志（透明底母版优先，其次方形 png / ico）"""
    for _p in (os.path.join(_ICON_DIR, "assets", "brand.png"), ICON_PNG, ICON_PATH):
        if os.path.isfile(_p):
            pm = QPixmap(_p)
            if not pm.isNull():
                return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return QPixmap(make_icon("home", size, ACCENT))


# ---------------- 矢量图标 ----------------
def make_icon(name, size=22, color="#2b3238", width=1.9):
    dpr = 2
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    s = size
    if name == "plus":
        p.drawLine(int(s * 0.5), int(s * 0.22), int(s * 0.5), int(s * 0.78))
        p.drawLine(int(s * 0.22), int(s * 0.5), int(s * 0.78), int(s * 0.5))
    elif name == "home":
        path = QPainterPath()
        path.moveTo(s * 0.14, s * 0.47)
        path.lineTo(s * 0.5, s * 0.15)
        path.lineTo(s * 0.86, s * 0.47)
        p.drawPath(path)
        path2 = QPainterPath()
        path2.moveTo(s * 0.24, s * 0.43)
        path2.lineTo(s * 0.24, s * 0.84)
        path2.lineTo(s * 0.76, s * 0.84)
        path2.lineTo(s * 0.76, s * 0.43)
        p.drawPath(path2)
    elif name == "download":
        p.drawLine(int(s * 0.5), int(s * 0.14), int(s * 0.5), int(s * 0.62))
        path = QPainterPath()
        path.moveTo(s * 0.30, s * 0.44)
        path.lineTo(s * 0.5, s * 0.65)
        path.lineTo(s * 0.70, s * 0.44)
        p.drawPath(path)
        path2 = QPainterPath()
        path2.moveTo(s * 0.17, s * 0.70)
        path2.lineTo(s * 0.17, s * 0.85)
        path2.lineTo(s * 0.83, s * 0.85)
        path2.lineTo(s * 0.83, s * 0.70)
        p.drawPath(path2)
    elif name == "person":
        p.drawEllipse(QRectF(s * 0.33, s * 0.16, s * 0.34, s * 0.34))
        path = QPainterPath()
        path.moveTo(s * 0.17, s * 0.86)
        path.cubicTo(s * 0.17, s * 0.60, s * 0.83, s * 0.60, s * 0.83, s * 0.86)
        p.drawPath(path)
    elif name == "gear":
        p.save()
        p.translate(s / 2, s / 2)
        for i in range(8):
            p.save()
            p.rotate(i * 45)
            p.drawLine(0, int(-s * 0.30), 0, int(-s * 0.42))
            p.restore()
        p.drawEllipse(QRectF(-s * 0.30, -s * 0.30, s * 0.60, s * 0.60))
        p.drawEllipse(QRectF(-s * 0.12, -s * 0.12, s * 0.24, s * 0.24))
        p.restore()
    elif name == "chevron":
        path = QPainterPath()
        path.moveTo(s * 0.32, s * 0.40)
        path.lineTo(s * 0.5, s * 0.60)
        path.lineTo(s * 0.68, s * 0.40)
        p.drawPath(path)
    elif name == "back":
        path = QPainterPath()
        path.moveTo(s * 0.62, s * 0.30)
        path.lineTo(s * 0.38, s * 0.50)
        path.lineTo(s * 0.62, s * 0.70)
        p.drawPath(path)
    elif name == "close":
        p.drawLine(int(s * 0.26), int(s * 0.26), int(s * 0.74), int(s * 0.74))
        p.drawLine(int(s * 0.74), int(s * 0.26), int(s * 0.26), int(s * 0.74))
    elif name == "grid":
        # 游戏版本：四个方块（对应"多个版本实例"）
        for dx, dy in ((0.15, 0.15), (0.53, 0.15), (0.15, 0.53), (0.53, 0.53)):
            p.drawRoundedRect(QRectF(s * dx, s * dy, s * 0.31, s * 0.31),
                              s * 0.07, s * 0.07)
    elif name == "heart":
        path = QPainterPath()
        path.moveTo(s * 0.5, s * 0.80)
        path.cubicTo(s * 0.10, s * 0.55, s * 0.16, s * 0.20, s * 0.36, s * 0.20)
        path.cubicTo(s * 0.45, s * 0.20, s * 0.50, s * 0.28, s * 0.50, s * 0.33)
        path.cubicTo(s * 0.50, s * 0.28, s * 0.55, s * 0.20, s * 0.64, s * 0.20)
        path.cubicTo(s * 0.84, s * 0.20, s * 0.90, s * 0.55, s * 0.50, s * 0.80)
        p.drawPath(path)
    elif name == "news":
        # 资讯：报纸（外框 + 头条块 + 两行正文）
        p.drawRoundedRect(QRectF(s * 0.15, s * 0.20, s * 0.70, s * 0.60),
                          s * 0.07, s * 0.07)
        p.drawRoundedRect(QRectF(s * 0.24, s * 0.30, s * 0.24, s * 0.16),
                          s * 0.03, s * 0.03)
        for y in (0.53, 0.64):
            p.drawLine(int(s * 0.24), int(s * y), int(s * 0.76), int(s * y))
        p.drawLine(int(s * 0.55), int(s * 0.30), int(s * 0.76), int(s * 0.30))
        p.drawLine(int(s * 0.55), int(s * 0.39), int(s * 0.76), int(s * 0.39))
    elif name == "server":
        # 机架式服务器：两层机箱 + 指示灯
        p.drawRoundedRect(QRectF(s * 0.14, s * 0.16, s * 0.72, s * 0.27),
                          s * 0.08, s * 0.08)
        p.drawRoundedRect(QRectF(s * 0.14, s * 0.57, s * 0.72, s * 0.27),
                          s * 0.08, s * 0.08)
        p.drawEllipse(QRectF(s * 0.22, s * 0.245, s * 0.10, s * 0.10))
        p.drawEllipse(QRectF(s * 0.22, s * 0.655, s * 0.10, s * 0.10))
        for x in (0.46, 0.58, 0.70):
            p.drawLine(int(s * x), int(s * 0.295), int(s * (x + 0.08)), int(s * 0.295))
            p.drawLine(int(s * x), int(s * 0.705), int(s * (x + 0.08)), int(s * 0.705))
    elif name == "search":
        p.drawEllipse(QRectF(s * 0.16, s * 0.16, s * 0.48, s * 0.48))
        p.drawLine(int(s * 0.61), int(s * 0.61), int(s * 0.85), int(s * 0.85))
    elif name == "folder":
        path = QPainterPath()
        path.moveTo(s * 0.13, s * 0.79)
        path.lineTo(s * 0.13, s * 0.24)
        path.lineTo(s * 0.40, s * 0.24)
        path.lineTo(s * 0.48, s * 0.37)
        path.lineTo(s * 0.87, s * 0.37)
        path.lineTo(s * 0.87, s * 0.79)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "info":
        p.drawEllipse(QRectF(s * 0.15, s * 0.15, s * 0.70, s * 0.70))
        p.drawPoint(QPointF(s * 0.5, s * 0.35))
        p.drawLine(int(s * 0.5), int(s * 0.47), int(s * 0.5), int(s * 0.66))
    elif name == "minus":
        p.drawEllipse(QRectF(s * 0.15, s * 0.15, s * 0.70, s * 0.70))
        p.drawLine(int(s * 0.33), int(s * 0.5), int(s * 0.67), int(s * 0.5))
    elif name == "plus_dot":
        p.drawEllipse(QRectF(s * 0.15, s * 0.15, s * 0.70, s * 0.70))
        p.drawLine(int(s * 0.33), int(s * 0.5), int(s * 0.67), int(s * 0.5))
        p.drawLine(int(s * 0.5), int(s * 0.33), int(s * 0.5), int(s * 0.67))
    elif name == "trash":
        p.drawLine(int(s * 0.16), int(s * 0.30), int(s * 0.84), int(s * 0.30))
        p.drawLine(int(s * 0.39), int(s * 0.30), int(s * 0.39), int(s * 0.19))
        p.drawLine(int(s * 0.61), int(s * 0.30), int(s * 0.61), int(s * 0.19))
        path = QPainterPath()
        path.moveTo(s * 0.25, s * 0.30)
        path.lineTo(s * 0.31, s * 0.84)
        path.lineTo(s * 0.69, s * 0.84)
        path.lineTo(s * 0.75, s * 0.30)
        p.drawPath(path)
        p.drawLine(int(s * 0.43), int(s * 0.43), int(s * 0.45), int(s * 0.72))
        p.drawLine(int(s * 0.57), int(s * 0.43), int(s * 0.55), int(s * 0.72))
    elif name == "refresh":
        p.drawArc(QRectF(s * 0.16, s * 0.16, s * 0.68, s * 0.68), 35 * 16, 285 * 16)
        path = QPainterPath()
        path.moveTo(s * 0.70, s * 0.12)
        path.lineTo(s * 0.88, s * 0.20)
        path.lineTo(s * 0.72, s * 0.33)
        p.drawPath(path)
    elif name == "mod":
        # 模组：拼图块（圆角方块 + 上方凸起 + 右侧凹口）
        p.drawRoundedRect(QRectF(s * 0.16, s * 0.32, s * 0.66, s * 0.52), s * 0.08, s * 0.08)
        p.drawArc(QRectF(s * 0.38, s * 0.14, s * 0.24, s * 0.24), 0, 180 * 16)
        p.drawArc(QRectF(s * 0.74, s * 0.48, s * 0.20, s * 0.20), 90 * 16, 180 * 16)
    p.end()
    return pm


def diamond_pixmap(size=18, color="#1fa04a"):
    """实心菱形（当前版本卡左侧的标识）"""
    dpr = 2
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    s = size
    path = QPainterPath()
    path.moveTo(s * 0.5, s * 0.07)
    path.lineTo(s * 0.93, s * 0.5)
    path.lineTo(s * 0.5, s * 0.93)
    path.lineTo(s * 0.07, s * 0.5)
    path.closeSubpath()
    p.drawPath(path)
    p.end()
    return pm


# ---------------- 全局样式（模板，主题色/背景色可动态替换） ----------------
QSS_TEMPLATE = """
* {{ font-family: "Microsoft YaHei UI", "PingFang SC", "Helvetica Neue", sans-serif; }}
#Rail {{ background: transparent; border: none; }}
#RailPlus {{ background: transparent; border: 1px solid {LINE}; border-radius: 12px; }}
#RailPlus:hover {{ background: {SURFACE}; }}
#RailLogo {{ background: transparent; border: none; }}
#RailLogo:hover {{ background: {SURFACE}; border-radius: 14px; }}
#RailBtn {{ background: transparent; border: none; border-radius: 12px; }}
#RailBtn:hover {{ background: {SURFACE}; }}
#RailBtn:checked {{ background: {ACCENT_SOFT}; }}

#Glass {{
  background: rgba(255,255,255,0.78);
  border: 1px solid rgba(255,255,255,0.9);
  border-radius: 18px;
}}
#Title {{ color: {TEXT}; font-size: 17px; font-weight: bold; }}
#Hint {{ color: {SUB}; font-size: 12px; }}
#FieldLabel {{ color: {SUB}; font-size: 12px; }}
#RowName {{ color: {TEXT}; font-size: 14px; font-weight: bold; }}
#RowSub {{ color: {SUB}; font-size: 12px; }}
#Badge {{
  color: {ACCENT_DARK}; font-size: 11px; font-weight: bold;
  background: {ACCENT_SOFT}; border-radius: 8px; padding: 2px 8px;
}}
#NewsBig {{ color: {TEXT}; font-size: 25px; font-weight: bold; }}
#NewsRow {{ background: transparent; border: none; border-radius: 9px; }}
#NewsRow:hover {{ background: rgba(255,255,255,0.8); }}
#LinkBtn {{ background: transparent; border: none; color: #4a7fb5; font-size: 12px; padding: 2px 4px; }}
#LinkBtn:hover {{ text-decoration: underline; }}

#VerRow {{
  background: rgba(255,255,255,0.62); border: 1px solid {LINE_SOFT};
  border-radius: 14px;
}}
#VerRow:hover {{ background: rgba(255,255,255,0.94); }}

#JavaRow {{
  background: rgba(255,255,255,0.62); border: 1px solid {LINE_SOFT};
  border-radius: 12px;
}}
#JavaRow:hover {{ background: rgba(255,255,255,0.95); }}
#JavaTag {{
  color: {ACCENT_DARK}; font-size: 11px; font-weight: bold;
  background: {ACCENT_SOFT}; border-radius: 8px; padding: 3px 9px;
}}
#JavaAuto {{
  color: #1d6b3a; font-size: 11px; font-weight: bold;
  background: rgba(46,160,90,0.12); border-radius: 8px; padding: 2px 8px;
}}
#VerRowOn {{
  background: {ACCENT_SOFT}; border: 1px solid {ACCENT_SOFT2};
  border-radius: 14px;
}}
#ChipOn {{
  background: {ACCENT}; color: #ffffff; border: none; border-radius: 9px;
  padding: 5px 13px; font-size: 12px;
}}
#ChipOff {{
  background: transparent; color: #5d676e; border: 1px solid {LINE_SOFT};
  border-radius: 9px; padding: 5px 13px; font-size: 12px;
}}
#ChipOff:hover {{ background: rgba(255,255,255,0.85); color: {TEXT}; }}
#DotsBtn {{
  background: transparent; border: none; color: {SUB};
  font-size: 15px; font-weight: bold;
}}
#DotsBtn:hover {{ background: {SURFACE}; border-radius: 8px; color: {TEXT}; }}

QLineEdit, QComboBox {{
  background: #f4f2ea; border: 1px solid {LINE}; border-radius: 10px;
  padding: 9px 12px; color: {TEXT}; font-size: 13px;
}}
QLineEdit:hover, QComboBox:hover {{ background: #efede4; }}
QLineEdit:focus, QComboBox:focus {{
  background: #ffffff; border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{
  image: url("{ARROW_ICON}"); width: 11px; height: 11px; margin-right: 9px;
}}
/* 下拉弹层样式由 polish_combo() 逐次套用（画在 viewport 上），这里不设全局规则 */

#PrimaryBtn {{
  background: {ACCENT}; color: white; border: none; border-radius: 10px;
  padding: 9px 20px; font-size: 13px; font-weight: bold;
}}
#PrimaryBtn:hover {{ background: {ACCENT_HOVER}; }}
#PrimaryBtn:disabled {{ background: {ACCENT_SOFT2}; }}
#GhostBtn {{
  background: rgba(255,255,255,0.85); color: #465056;
  border: 1px solid {LINE}; border-radius: 10px; padding: 8px 16px; font-size: 13px;
}}
#GhostBtn:hover {{ background: #ffffff; }}
#SearchBtn {{
  background: {ACCENT_SOFT}; color: {ACCENT_DARK};
  border: 1px solid {ACCENT_LINE}; border-radius: 9px; font-size: 13px;
  font-weight: bold;
}}
#SearchBtn:hover {{ background: {ACCENT_SOFT2}; border-color: {ACCENT}; }}
#SearchBtn:pressed {{ background: {ACCENT_SOFT2}; border-color: {ACCENT}; }}
#DangerBtn {{
  background: transparent; color: #c8503f; border: none; font-size: 12px; padding: 4px 8px;
}}
#DangerBtn:hover {{ text-decoration: underline; }}

QListWidget {{
  background: rgba(255,255,255,0.55); border: 1px solid {LINE_SOFT}; border-radius: 14px;
  padding: 4px; font-size: 13px; color: {TEXT};
}}
QListWidget::item {{ padding: 9px 10px; border-radius: 9px; margin: 2px; }}
QListWidget::item:hover {{ background: rgba(255,255,255,0.9); }}
QListWidget::item:selected {{ background: {ACCENT_SOFT2}; color: {ACCENT_DARK}; }}

QProgressBar {{
  background: rgba(0,0,0,0.07); border: none; border-radius: 4px;
  height: 8px; text-align: center; color: transparent;
}}
QProgressBar::chunk {{ border-radius: 4px; background: {ACCENT}; }}

QSlider::groove:horizontal {{
  height: 6px; background: rgba(0,0,0,0.09); border-radius: 3px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::add-page:horizontal {{ background: rgba(0,0,0,0.09); border-radius: 3px; }}
QSlider::handle:horizontal {{
  width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;
  background: #ffffff; border: 2px solid {ACCENT};
}}
QCheckBox {{ color: #465056; font-size: 13px; spacing: 8px; }}
QCheckBox::indicator {{
  width: 16px; height: 16px; border-radius: 6px;
  border: 1px solid {LINE}; background: #ffffff;
}}
QCheckBox::indicator:checked {{
  background: {ACCENT}; border: 1px solid {ACCENT};
  image: url("{CHECK_ICON}");
}}

#LogView {{
  background: rgba(255,255,255,0.82); color: #3d4a44; border: 1px solid {LINE_SOFT};
  border-radius: 14px; padding: 12px; font-family: Consolas, Menlo, monospace; font-size: 12px;
  selection-background-color: {ACCENT_SOFT2}; selection-color: {ACCENT_DARK};
}}
#VerLabel {{ color: {SUB}; font-size: 13px; }}
#SplitLeft {{
  background: {ACCENT}; color: white; border: none;
  border-top-left-radius: 12px; border-bottom-left-radius: 12px;
  padding: 0 30px; font-size: 17px; font-weight: bold;
}}
#SplitLeft:hover {{ background: {ACCENT_HOVER}; }}
#SplitRight {{
  background: {ACCENT}; color: white; border: none; border-left: 1px solid rgba(255,255,255,0.35);
  border-top-right-radius: 12px; border-bottom-right-radius: 12px;
}}
#SplitRight:hover {{ background: {ACCENT_HOVER}; }}
#SegOn {{
  background: white; color: {TEXT}; border: none; border-radius: 9px;
  padding: 6px 18px; font-size: 13px; font-weight: bold;
}}
#SegOff {{
  background: transparent; color: #5d676e; border: none; border-radius: 9px;
  padding: 6px 18px; font-size: 13px;
}}
#SegOff:hover {{ color: {TEXT}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(0,0,0,0.16); border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
#Toast {{ background: white; border-radius: 16px; border: 1px solid {LINE_SOFT}; }}
#ToastTitle {{ color: {TEXT}; font-size: 13px; font-weight: bold; }}
#ToastSub {{ color: {SUB}; font-size: 12px; }}
#DLPopup {{ background: rgba(255,255,255,0.985); border-radius: 14px;
           border: 1px solid {LINE_SOFT}; }}
#DLHead {{ color: {TEXT}; font-size: 12.5px; font-weight: bold; }}
#DLRow {{ background: {SURFACE}; border-radius: 10px; border: 1px solid {LINE_SOFT}; }}
#DLRowTitle {{ color: {TEXT}; font-size: 12.5px; font-weight: 600; }}
#DLRow QProgressBar {{ background: rgba(0,0,0,0.07); border: none; border-radius: 3px; }}
#DLRow QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
#DLCancel {{ background: transparent; border: none; border-radius: 10px; }}
#DLCancel:hover {{ background: rgba(0,0,0,0.07); }}
QMenu {{
  background: rgba(255,255,255,0.99); border: 1px solid {LINE_SOFT};
  border-radius: 14px; padding: 6px;
}}
QMenu::item {{ padding: 9px 26px 9px 14px; border-radius: 9px; color: {TEXT}; font-size: 13px; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT_DARK}; }}
QMenu::separator {{ height: 1px; background: {LINE_SOFT}; margin: 5px 10px; }}
"""


def build_qss():
    """按当前主题生成 QSS（改颜色后调用即可整体换肤）"""
    c = theme_colors()
    return QSS_TEMPLATE.format(
        ACCENT=c["accent"], ACCENT_HOVER=c["accent_hover"],
        ACCENT_SOFT=c["accent_soft"], ACCENT_SOFT2=c["accent_soft2"],
        ACCENT_DARK=c["accent_dark"], ACCENT_LINE=c["accent_line"],
        LINE=c["line"], LINE_SOFT=c["line_soft"], SURFACE=c["surface"],
        CHECK_ICON=_ensure_check_icon(),
        ARROW_ICON=_ensure_arrow_icon(),
        TEXT=TEXT, SUB=SUB,
    )


_CHECK_CACHE = {}


def _ensure_check_icon():
    """画一个白色对勾 PNG（QSS 里复选框选中态用），返回正斜杠路径"""
    path = os.path.join(tempfile.gettempdir(), "mcl_check.png")
    if _CHECK_CACHE.get("path") == path and os.path.isfile(path):
        return path.replace("\\", "/")
    try:
        from PySide6.QtGui import QImage
        img = QImage(36, 36, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#ffffff"), 4.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawLine(9, 19, 15, 25)
        p.drawLine(15, 25, 27, 11)
        p.end()
        img.save(path, "PNG")
        _CHECK_CACHE["path"] = path
        return path.replace("\\", "/")
    except Exception:
        return ""


def _ensure_arrow_icon():
    """画一个灰色下拉箭头 PNG（QSS 里 QComboBox::down-arrow 用）"""
    path = os.path.join(tempfile.gettempdir(), "mcl_arrow.png")
    if _CHECK_CACHE.get("arrow") == path and os.path.isfile(path):
        return path.replace("\\", "/")
    try:
        from PySide6.QtGui import QImage
        img = QImage(32, 32, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#8d979f"), 3.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawLine(10, 13, 16, 19)
        p.drawLine(16, 19, 22, 13)
        p.end()
        img.save(path, "PNG")
        _CHECK_CACHE["arrow"] = path
        return path.replace("\\", "/")
    except Exception:
        return ""


QSS = build_qss()


# ---------------- HTTP 封装 ----------------
class Api:
    @staticmethod
    def get(path, timeout=60):
        return requests.get(BASE + path, timeout=timeout)

    @staticmethod
    def post(path, payload=None, timeout=60):
        return requests.post(BASE + path, json=payload or {}, timeout=timeout)


class StatePoller(QThread):
    state = Signal(dict)

    def __init__(self):
        super().__init__()
        self._run = True

    def stop(self):
        """退出前必须停掉：QThread 还在跑就被析构，Qt 会直接 abort
        （异常代码 0xC0000409，Windows 弹"AeroLauncher.exe 已停止工作"）"""
        self._run = False
        if self.isRunning():
            self.wait(3000)

    def run(self):
        import time
        while self._run:
            try:
                self.state.emit(Api.get("/api/state", timeout=10).json())
            except Exception:
                pass
            for _ in range(7):          # 0.7s 分片等待，停止时能立刻退出
                if not self._run:
                    return
                time.sleep(0.1)


_ALIVE_THREADS = set()

# 所有退出时可能仍在运行的 QThread（更新检查等）
_TRACKED_THREADS = set()


def _wait_or_kill(th, ms=1500):
    """退出阶段收尾：先等，等不到就 terminate。
    QThread 在运行中被析构，Qt 会 fail-fast（0xC0000409），
    Windows 就会弹"AeroLauncher.exe 已停止工作"。"""
    try:
        if not th.isRunning():
            return
        if not th.wait(ms):
            th.terminate()
            th.wait(500)
    except Exception:
        pass


class ApiThread(QThread):
    """通用后台请求（GET/POST），结果回主线程"""
    done = Signal(object)

    def __init__(self, method, path, payload=None, timeout=90):
        super().__init__()
        self.method, self.path, self.payload, self.timeout = method, path, payload, timeout
        # 关键：持有运行中线程的引用，防止对象被 GC（QThread 运行中被析构会让整个进程 abort，
        # 症状就是"添加账户后窗口直接关闭"）。finished 后再释放。
        _ALIVE_THREADS.add(self)
        self.finished.connect(lambda: _ALIVE_THREADS.discard(self))

    def run(self):
        try:
            if self.method == "GET":
                r = Api.get(self.path, timeout=self.timeout)
            else:
                r = Api.post(self.path, self.payload, timeout=self.timeout)
            self.done.emit(r.json())
        except Exception as e:
            self.done.emit({"ok": False, "error": str(e)})


def _ver_tuple(v):
    """'3.4.1-beta' -> (3, 4, 1)，用于比较版本大小"""
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4])


class UpdateCheck(QThread):
    """检查更新：GET update.json（内测通道读 beta.json，失败回退 update.json）"""
    got = Signal(dict)

    def __init__(self, url, fallback=""):
        super().__init__()
        self.url = url
        self.fallback = fallback
        _TRACKED_THREADS.add(self)
        self.finished.connect(lambda: _TRACKED_THREADS.discard(self))

    def run(self):
        try:
            r = requests.get(self.url, timeout=8)
            d = r.json()
            if not r.ok or not d.get("version"):
                raise RuntimeError(r.status_code)
            d.setdefault("ok", True)
            self.got.emit(d)
            return
        except Exception as e:
            err = str(e)
        if self.fallback and self.fallback != self.url:
            try:
                r = requests.get(self.fallback, timeout=8)
                d = r.json()
                d.setdefault("ok", r.ok)
                self.got.emit(d)
                return
            except Exception as e:
                err = str(e)
        self.got.emit({"ok": False, "error": err})


class UpdateDownload(QThread):
    """下载新版 exe（流式，带进度，支持取消）"""
    progress = Signal(int, int)   # 已下载字节, 总字节
    done = Signal(str)            # 本地文件路径
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, url, dest, sha256=""):
        super().__init__()
        self.url, self.dest, self.sha256 = url, dest, sha256
        self._cancel = False
        _TRACKED_THREADS.add(self)
        self.finished.connect(lambda: _TRACKED_THREADS.discard(self))

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            with requests.get(self.url, stream=True, timeout=30) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0) or 0)
                got = 0
                with open(self.dest, "wb") as f:
                    for chunk in r.iter_content(64 * 1024):
                        if self._cancel:
                            raise _UpdateCancelled()
                        if chunk:
                            f.write(chunk)
                            got += len(chunk)
                            self.progress.emit(got, total)
            if self._cancel:
                raise _UpdateCancelled()
            if self.sha256:
                import hashlib
                h = hashlib.sha256()
                with open(self.dest, "rb") as f:
                    for blk in iter(lambda: f.read(1024 * 512), b""):
                        if self._cancel:
                            raise _UpdateCancelled()
                        h.update(blk)
                if h.hexdigest().lower() != self.sha256.lower():
                    os.remove(self.dest)
                    self.failed.emit(tr("SHA256 校验失败，下载文件不完整或被篡改"))
                    return
            self.done.emit(self.dest)
        except _UpdateCancelled:
            try:
                if os.path.isfile(self.dest):
                    os.remove(self.dest)
            except Exception:
                pass
            self.cancelled.emit()
        except Exception as e:
            if self._cancel:
                try:
                    if os.path.isfile(self.dest):
                        os.remove(self.dest)
                except Exception:
                    pass
                self.cancelled.emit()
            else:
                self.failed.emit(str(e))


def _friendly_net_error(err):
    """requests 的异常串又长又英文，弹给用户之前先翻译成人话；原文进日志"""
    s = str(err)
    low = s.lower()
    keys = ("max retries", "proxyerror", "proxy", "connection", "timed out",
            "timeout", "getaddrinfo", "name or service", "ssl", "certificate",
            "unreachable", "10054", "10060", "10061")
    if any(k in low for k in keys):
        return tr("网络连接失败，请检查网络后重试")
    # 同时匹配中英文的校验错误（后端报错文本会随语言变化）
    if ("sha256" in low or "校验失败" in s
            or "checksum" in low or "sha1" in low or "sha-1" in low):
        return tr("下载的文件校验不通过，请稍后重试")
    return s[:120]


def _log_update_error(err):
    """完整报错写进启动器日志，用户界面只看得到简短提示"""
    try:
        backend.log(f"{tr('[更新] 失败详情: ')}{err}")
    except Exception:
        pass


# onefile 引导进程注入给子进程的变量：告诉子进程"别解压，直接用这个目录"
_PYI_ENV_KEYS = ("_PYI_APPLICATION_HOME_DIR", "_PYI_PARENT_PROCESS_LEVEL",
                 "_PYI_ARCHIVE_FILE", "_PYI_SPLASH_IPC", "_MEIPASS2")


def _clean_child_env():
    """拉起新实例前必须去掉 _PYI_* 环境变量。

    这些变量会把"我已经解压好了、python3xx.dll 在这个目录"一路继承给 cmd 和
    新实例；新实例于是跳过解压，去加载旧进程那个早已被删掉的 _MEI 目录里的
    python3xx.dll，弹出 "Failed to load Python DLL ... LoadLibrary: 找不到
    指定的模块"。自动更新重启、改语言重启都会踩，必须清掉。
    """
    env = dict(os.environ)
    for k in _PYI_ENV_KEYS:
        env.pop(k, None)
    return env


class _UpdateCancelled(Exception):
    """内部用：更新下载被用户取消"""


class _ComboItemDelegate(QStyledItemDelegate):
    """固定下拉项高度（QSS 的 padding 算不准行高，这里直接定死）"""

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        return QSize(s.width(), 36)


class _ComboListView(QListView):
    """下拉列表：白卡圆角自己画。
    列表滚动时 Qt 只位移内容像素、不重绘 viewport 背景，交给 QSS 的
    border-radius 会被擦成直角，所以这里每帧自绘一层圆角卡片打底。"""

    card_line = "#e8e2d4"

    def paintEvent(self, ev):
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.viewport().rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(self.card_line), 1))
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(r, 12, 12)
        p.end()
        super().paintEvent(ev)


def polish_combo(combo):
    """下拉弹层：无边框透明窗口 + 白色圆角卡片（卡片由 _ComboListView 自绘）。"""
    c = theme_colors()
    try:
        view = combo.view()
        win = view.window()
        # 容器默认会用 palette 底色填满整块矩形，把圆角四角盖住 → 直角"白框"。
        # 必须显式设成透明；ID 选择器避免级联到同为 QFrame 的 QListView。
        win.setObjectName("ComboPopup")
        win.setStyleSheet("QFrame#ComboPopup { background: transparent;"
                          " border: none; }")
        win.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint
                           | Qt.NoDropShadowWindowHint)
        win.setAttribute(Qt.WA_TranslucentBackground)
        win.setAttribute(Qt.WA_NoSystemBackground)
        view.setFrameShape(QFrame.NoFrame)
        # 滚动条会被画到卡片外面，长列表靠滚轮/方向键翻，不显示滑块
        view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 逐像素滚动：整块重绘，底部圆角不会被滚动位移擦成直角
        view.setVerticalScrollMode(QListView.ScrollPerPixel)
        view.setItemDelegate(_ComboItemDelegate(view))
        view.setStyleSheet(
            "QListView { background: transparent; border: none; outline: 0; }"
            f"QListView::item {{ padding: 0px 11px; margin: 1px 4px;"
            f" border-radius: 8px; color: {TEXT}; }}"
            f"QListView::item:selected {{ background: {c['accent_soft']};"
            f" color: {c['accent_dark']}; }}")
        if isinstance(view, _ComboListView):
            view.card_line = c["line"]
        vp = view.viewport()
        vp.setAttribute(Qt.WA_StyledBackground, True)
        vp.setStyleSheet("background: transparent; border: none;")
        view.setAttribute(Qt.WA_TranslucentBackground)
        vp.setAutoFillBackground(False)
    except Exception:
        pass


class RoundCombo(QComboBox):
    """下拉框：圆角弹层卡片（每次弹出前重新套样式，Qt 会重建容器窗口）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setView(_ComboListView(self))
        polish_combo(self)

    def showPopup(self):
        polish_combo(self)
        super().showPopup()


def polish_menu(menu):
    """让 QMenu 弹层也是圆角：无边框 + 透明窗口，圆角由 QSS 画出"""
    try:
        menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint
                            | Qt.NoDropShadowWindowHint)
        menu.setAttribute(Qt.WA_TranslucentBackground)
        menu.setAttribute(Qt.WA_NoSystemBackground)
    except Exception:
        pass
    return menu


def round_menu(parent=None):
    """创建一个圆角 QMenu"""
    return polish_menu(QMenu(parent))


# ---------------- 像素头像（Steve / Alex） ----------------
_AVATAR_COLORS = {
    "steve": {"H": "#3f2b1c", "S": "#bd8b67", "W": "#ffffff",
              "I": "#4a52a8", "N": "#9a6a4e", "M": "#6f4a35"},
    "alex": {"H": "#cf7f45", "S": "#f0c6a0", "W": "#ffffff",
             "I": "#4f7a3a", "N": "#d5a87f", "M": "#b57e5c"},
}
_AVATAR_GRID = {
    "steve": ["HHHHHHHH",
              "HHHHHHHH",
              "HSSSSSSH",
              "SSSSSSSS",
              "SWISSIWS",
              "SSNNNNSS",
              "SSSMMSSS",
              "SSSSSSSS"],
    "alex": ["HHHHHHHH",
             "HHHHHHHH",
             "HSSSSSSH",
             "HSSSSSSH",
             "SWISSIWS",
             "SSNNNNSS",
             "SSSMMSSS",
             "SSSSSSSS"],
}


def avatar_pixmap(name, size=34):
    """名字长度为 2 的倍数 → Steve；3 的倍数 → Alex；都不是 → None（用字母头像）"""
    n = len(name or "")
    if n % 2 == 0:
        kind = "steve"
    elif n % 3 == 0:
        kind = "alex"
    else:
        return None
    dpr = 2
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    p.setClipPath(clip)
    colors = _AVATAR_COLORS[kind]
    grid = _AVATAR_GRID[kind]
    cell = size / 8.0
    for r in range(8):
        for c in range(8):
            p.fillRect(int(c * cell), int(r * cell),
                       int(cell) + 1, int(cell) + 1,
                       QColor(colors[grid[r][c]]))
    p.end()
    return pm


# ---------------- 背景 ----------------
class GradientBG(QWidget):
    """内容区容器（背景统一由主窗口绘制并裁剪圆角，这里保持透明）"""

    def paintEvent(self, _e):
        pass


# 窗口整体背景绘制参数
WIN_RADIUS = 16

# ---------------- 动态壁纸 ----------------
WP_KINDS = ("off", "cubes", "aurora", "stars")
WP_LABELS = {"off": "关闭", "cubes": "漂浮方块", "aurora": "流动光晕", "stars": "星屑闪烁"}

# 场景元素用固定种子生成，保证每次启动布局一致（存的是 0~1 的相对坐标，随窗口缩放）
_wp_rng = random.Random(20261110)
WP_CUBES = [dict(x=_wp_rng.uniform(0.03, 0.97), y=_wp_rng.random(),
                 s=_wp_rng.uniform(13, 30), v=_wp_rng.uniform(0.012, 0.032),
                 ph=_wp_rng.uniform(0, 6.283), sw=_wp_rng.uniform(0.15, 0.55),
                 a=_wp_rng.uniform(22, 44)) for _ in range(13)]
WP_STARS = [dict(x=_wp_rng.random(), y=_wp_rng.uniform(0.02, 0.94),
                 r=_wp_rng.uniform(1.0, 2.4), v=_wp_rng.uniform(0.004, 0.012),
                 ph=_wp_rng.uniform(0, 6.283), tw=_wp_rng.uniform(0.5, 1.6),
                 a=_wp_rng.uniform(65, 130), big=_wp_rng.random() < 0.14)
            for _ in range(62)]
WP_AURORA = [dict(col="#bcd3ea", phx=_wp_rng.uniform(0, 6.283),
                  phy=_wp_rng.uniform(0, 6.283), rad=_wp_rng.uniform(0.40, 0.66),
                  a=_wp_rng.uniform(34, 50)) for _ in range(4)] + \
            [dict(col="accent", phx=_wp_rng.uniform(0, 6.283),
                  phy=_wp_rng.uniform(0, 6.283), rad=_wp_rng.uniform(0.34, 0.52),
                  a=_wp_rng.uniform(20, 32)) for _ in range(2)]


def _wp_cube(p, cx, cy, s, accent, alpha):
    """2.5D 像素小方块：正方形正面 + 顶面（亮）+ 右侧面（暗），随主题色着色"""
    d = s * 0.38
    top = QColor(_mix(accent, "#ffffff", 0.45))
    side = QColor(_mix(accent, "#000000", 0.38))
    front = QColor(_mix(accent, "#ffffff", 0.12))
    for col in (top, side, front):
        col.setAlpha(alpha)
    p.setPen(Qt.NoPen)
    # 顶面（平行四边形）
    p.setBrush(top)
    pts = [QPointF(cx, cy), QPointF(cx + s, cy),
           QPointF(cx + s + d, cy - d), QPointF(cx + d, cy - d)]
    p.drawPolygon(pts)
    # 右侧面
    p.setBrush(side)
    pts = [QPointF(cx + s, cy), QPointF(cx + s + d, cy - d),
           QPointF(cx + s + d, cy - d + s), QPointF(cx + s, cy + s)]
    p.drawPolygon(pts)
    # 正面
    p.setBrush(front)
    p.drawRect(QRectF(cx, cy, s, s))


def paint_wallpaper(p, w, h, kind, t):
    """在背景之上绘制动态壁纸（浅底上压得很淡，保证前景可读）"""
    accent = theme_colors()["accent"]
    if kind == "cubes":
        p.setPen(Qt.NoPen)
        for c in WP_CUBES:
            yy = (c["y"] - t * c["v"]) % 1.18 - 0.09
            xx = c["x"] + c["sw"] * 0.05 * math.sin(t * 0.30 + c["ph"])
            _wp_cube(p, xx * w, yy * h, c["s"], accent, int(c["a"]))
    elif kind == "aurora":
        p.setPen(Qt.NoPen)
        for i, b in enumerate(WP_AURORA):
            col = QColor(accent if b["col"] == "accent" else b["col"])
            col.setAlpha(int(b["a"] * (0.75 + 0.25 * math.sin(t * 0.5 + i))))
            rad = w * b["rad"] * (0.92 + 0.08 * math.sin(t * 0.23 + i * 1.9))
            cx = w * (0.5 + 0.30 * math.sin(t * 0.11 + b["phx"]))
            cy = h * (0.48 + 0.26 * math.cos(t * 0.09 + b["phy"]))
            g = QRadialGradient(cx, cy, max(rad, 1))
            g.setColorAt(0.0, col)
            g.setColorAt(1.0, QColor(col.red(), col.green(), col.blue(), 0))
            p.setBrush(g)
            p.drawEllipse(QRectF(cx - rad, cy - rad, rad * 2, rad * 2))
    elif kind == "stars":
        cool = QColor("#9fb8d8")
        for s in WP_STARS:
            xx = (s["x"] + t * s["v"]) % 1.02
            tw = 0.5 + 0.5 * math.sin(t * s["tw"] + s["ph"])
            col = QColor(_mix(str(cool.name()), accent, 0.25))
            col.setAlpha(int(s["a"] * (0.30 + 0.70 * tw)))
            rr = s["r"] * (0.8 + 0.4 * tw)
            if s["big"]:
                p.setPen(QPen(col, 1.2))
                p.drawLine(int(xx * w - rr * 2.4), int(s["y"] * h),
                           int(xx * w + rr * 2.4), int(s["y"] * h))
                p.drawLine(int(xx * w), int(s["y"] * h - rr * 2.4),
                           int(xx * w), int(s["y"] * h + rr * 2.4))
            else:
                p.setPen(Qt.NoPen)
                p.setBrush(col)
                p.drawEllipse(QRectF(xx * w - rr, s["y"] * h - rr, rr * 2, rr * 2))


def paint_window_bg(win, extra_border=True):
    """在圆角裁剪内绘制：奶白渐变底 + 柔光斑 + 左侧栏 + 圆角描边"""
    c = theme_colors()
    p = QPainter(win)
    p.setRenderHint(QPainter.Antialiasing)
    r = win.rect()
    w, h = max(r.width(), 1), max(r.height(), 1)
    path = QPainterPath()
    path.addRoundedRect(QRectF(r), WIN_RADIUS, WIN_RADIUS)
    p.setClipPath(path)

    # 底色：奶白（或自定义背景色），上浅下略深
    base = QLinearGradient(r.topLeft(), r.bottomRight())
    base.setColorAt(0.0, QColor(c["bg_top"]))
    base.setColorAt(0.55, QColor(c["bg"]))
    base.setColorAt(1.0, QColor(c["bg_bottom"]))
    p.fillRect(r, base)

    # 自定义背景图片：铺满整个窗口（等比放大裁剪），覆盖底色与光斑
    img_path = THEME.get("bg_image") or ""
    bg_pm = None
    if img_path and os.path.isfile(img_path):
        key = (img_path, w, h)
        bg_pm = getattr(win, "_bg_pm", None)
        if bg_pm is None or getattr(win, "_bg_pm_key", None) != key:
            src = QPixmap(img_path)
            if not src.isNull():
                bg_pm = src.scaled(w, h, Qt.KeepAspectRatioByExpanding,
                                   Qt.SmoothTransformation)
                win._bg_pm, win._bg_pm_key = bg_pm, key
        if bg_pm is not None and not bg_pm.isNull():
            p.drawPixmap((w - bg_pm.width()) // 2, (h - bg_pm.height()) // 2, bg_pm)

    if bg_pm is None:
        # 柔光斑：主题色/冷调/暖奶色各一层，压得很淡，保持奶白为主
        warm = QColor(c["accent"])
        warm.setAlpha(16)
        cool = QColor("#bcd3ea")
        cool.setAlpha(18)
        cream = QColor("#ffe9c9")
        cream.setAlpha(46)
        blobs = [(0.88, 0.10, 0.58, cool), (0.46, 0.86, 0.72, cream),
                 (0.02, 0.62, 0.50, warm), (1.02, 0.94, 0.50, cream),
                 (0.20, 0.04, 0.62, cream)]
        p.setPen(Qt.NoPen)
        for cx, cy, rad, col in blobs:
            g = QRadialGradient(w * cx, h * cy, w * rad)
            g.setColorAt(0.0, col)
            g.setColorAt(0.55, QColor(col.red(), col.green(), col.blue(),
                                      int(col.alpha() * 0.40)))
            g.setColorAt(1.0, QColor(col.red(), col.green(), col.blue(), 0))
            p.setBrush(g)
            p.drawEllipse(QRectF(w * cx - w * rad, h * cy - w * rad,
                                 w * rad * 2, w * rad * 2))

    # 动态壁纸：叠在底色 / 背景图片之上，随时间流动
    wp = str(THEME.get("wp") or "off")
    if wp in WP_KINDS and wp != "off":
        paint_wallpaper(p, w, h, wp, time.monotonic())

    # 左侧图标栏底色 + 分隔线
    p.fillRect(QRectF(0, 0, 84, h), QColor(c["rail"]))
    p.setPen(QPen(QColor(c["line_soft"]), 1))
    p.drawLine(84, 0, 84, h)
    # 圆角描边
    p.setPen(QPen(QColor(0, 0, 0, 26), 1))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    p.end()


# ---------------- 组件 ----------------
def fade_in(w, dur=190):
    """控件淡入：先清掉旧的动画/特效再重来，快速连点也不会停在半透明"""
    old = getattr(w, "_fade_anim", None)
    if old is not None:
        old.stop()
        w._fade_anim = None
    w.setGraphicsEffect(None)
    eff = QGraphicsOpacityEffect(w)
    eff.setOpacity(0.0)
    w.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", w)
    anim.setDuration(dur)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)

    def _done():
        # 特效用完即弃，不常驻（整页离屏渲染有开销）
        w.setGraphicsEffect(None)
        w._fade_anim = None

    anim.finished.connect(_done)
    w._fade_anim = anim
    anim.start(QPropertyAnimation.DeleteWhenStopped)


def pop_in_window(dlg, dy=18, dur=210):
    """顶层无边框弹窗入场：透明度 0→1 + 从下方 dy 像素上浮，OutCubic。
    用户按下鼠标拖动时立即停掉位移动画，避免和拖拽打架。"""
    old = getattr(dlg, "_pop_anim", None)
    if old is not None:
        old.stop()
        dlg._pop_anim = None
    dlg.setWindowOpacity(0.0)
    end = dlg.pos()

    a_op = QPropertyAnimation(dlg, b"windowOpacity", dlg)
    a_op.setDuration(dur)
    a_op.setStartValue(0.0)
    a_op.setEndValue(1.0)
    a_op.setEasingCurve(QEasingCurve.OutCubic)

    a_pos = QPropertyAnimation(dlg, b"pos", dlg)
    a_pos.setDuration(dur)
    a_pos.setStartValue(end + QPoint(0, dy))
    a_pos.setEndValue(end)
    a_pos.setEasingCurve(QEasingCurve.OutCubic)

    grp = QParallelAnimationGroup(dlg)

    def _done():
        dlg.setWindowOpacity(1.0)
        dlg._pop_anim = None

    grp.finished.connect(_done)
    grp.addAnimation(a_op)
    grp.addAnimation(a_pos)
    dlg._pop_anim = grp
    grp.start(QParallelAnimationGroup.DeleteWhenStopped)


class RailButton(QPushButton):
    def __init__(self, icon_name, parent=None):
        super().__init__(parent)
        self.setObjectName("RailBtn")
        self.setFixedSize(46, 46)
        self.setIcon(QIcon(make_icon(icon_name)))
        self.setIconSize(QSize(22, 22))
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)


class DLBall(QWidget):
    """浮在内容区左下角的下载胶囊：小圆球显示下载总进度，点开看任务列表"""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(52, 52)
        self.setCursor(Qt.PointingHandCursor)
        self._state = "idle"          # idle / run / done / error
        self._pct = 0
        self._count = 0
        self.setToolTip(tr("下载任务"))
        sh = QGraphicsDropShadowEffect(self)
        sh.setBlurRadius(20)
        sh.setOffset(0, 4)
        sh.setColor(QColor(20, 30, 40, 80))
        self.setGraphicsEffect(sh)

    def set_state(self, state, pct=0, count=0):
        pct = max(0, min(100, int(pct)))
        if (state, pct, count) == (self._state, self._pct, self._count):
            return
        self._state, self._pct, self._count = state, pct, count
        if state == "run":
            tip = f"{tr('下载中')} · {pct}%"
            if count > 1:
                tip += f" · {count}{tr(' 个任务')}"
        elif state == "server":
            tip = f"{tr('服务器运行中')} · {tr('内存')} {pct}%"
        elif state == "done":
            tip = tr("下载完成")
        elif state == "error":
            tip = tr("有下载失败，点开查看")
        else:
            tip = tr("下载任务")
        self.setToolTip(tip)
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)

    def paintEvent(self, _e):
        c = theme_colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(3.5, 3.5, -3.5, -3.5)
        cx, cy = self.width() / 2, self.height() / 2

        # 底盘：小药片似的圆球，浅白半透明
        p.setPen(Qt.NoPen)
        bg = QColor("#ffffff")
        bg.setAlpha(212)
        p.setBrush(bg)
        p.drawEllipse(r)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(c["line_soft"]), 1))
        p.drawEllipse(r)

        # 进度环：进行中按百分比画弧，完成/失败画整圈
        ring = QColor(c["accent"])
        if self._state == "error":
            ring = QColor("#d9534f")
        elif self._state == "done":
            ring = QColor("#2f9e5f")
        pen = QPen(ring, 3)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        pct = self._pct if self._state in ("run", "server") else 100
        if self._state == "run":
            pct = max(3, pct)
        p.drawArc(r, 90 * 16, int(-max(1, pct) * 3.6 * 16))

        if self._state == "done":
            pen = QPen(QColor("#2f9e5f"), 2.2)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            path = QPainterPath()
            path.moveTo(cx - 5.5, cy + 0.5)
            path.lineTo(cx - 1.5, cy + 4.5)
            path.lineTo(cx + 6, cy - 4.5)
            p.drawPath(path)
        elif self._state == "error":
            pen = QPen(QColor("#c0392b"), 2.2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(int(cx), int(cy - 6), int(cx), int(cy + 2))
            p.drawPoint(int(cx), int(cy + 6))
        else:
            # 上半：图标（下载 / 服务器）；下半：百分比——进度就摆在球里面，多个任务是平均值
            pm = make_icon("server" if self._state == "server" else "download",
                           13, c["accent"], 1.9)
            p.drawPixmap(int(cx - 6), 9, pm)
            p.setPen(QColor(c["accent_dark"]))
            f = p.font()
            f.setPointSizeF(7.6)
            f.setBold(True)
            p.setFont(f)
            p.drawText(QRectF(0, 23, self.width(), 15), Qt.AlignCenter,
                       f"{self._pct}%")

        # 角标：多任务时显示数量（放右上角，别压住下载图标）
        if self._count > 1:
            br = QRectF(self.width() - 17, 0, 15, 15)
            bc = QColor("#d9534f" if self._state == "error" else c["accent"])
            p.setPen(QPen(QColor("#ffffff"), 1.4))
            p.setBrush(bc)
            p.drawEllipse(br)
            p.setPen(QColor("#ffffff"))
            f = p.font()
            f.setPointSizeF(6.6)
            f.setBold(True)
            p.setFont(f)
            p.drawText(br, Qt.AlignCenter, str(self._count))


class DLPopup(QWidget):
    """下载任务浮层：从胶囊里“长”出来（苹果那种连通展开），列出每个任务与进度"""

    MARGIN = 14          # 给阴影留的余量
    RADIUS = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        # 不是独立窗口，就浮在内容区上（点空白处 / 再点胶囊收起）
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 真正的卡片：圆角、背景、阴影都画在这一层，外层只负责“揭幕”动画
        self.card = QFrame(self)
        self.card.setObjectName("DLPopup")
        self.card.setAttribute(Qt.WA_StyledBackground, True)
        sh = QGraphicsDropShadowEffect(self.card)
        sh.setBlurRadius(24)
        sh.setOffset(0, 6)
        sh.setColor(QColor(20, 30, 40, 70))
        self.card.setGraphicsEffect(sh)

        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(13, 11, 13, 11)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.head = QLabel(tr("下载任务"))
        self.head.setObjectName("DLHead")
        self.count = QLabel("")
        self.count.setObjectName("Hint")
        head.addWidget(self.head)
        head.addWidget(self.count)
        head.addStretch()
        lay.addLayout(head)

        self.empty = QLabel(tr("暂无下载任务"))
        self.empty.setObjectName("Hint")
        lay.addWidget(self.empty)
        self.rows_lay = QVBoxLayout()
        self.rows_lay.setSpacing(7)
        lay.addLayout(self.rows_lay)
        self._rows = {}
        self._dismissed = set()
        self._ball = None
        self._from_rect = QRect(0, 0, 44, 44)
        self._to_rect = QRect(0, 0, 358, 200)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(280)
        self._anim.valueChanged.connect(lambda v: self._tick(float(v)))
        self._anim.finished.connect(self._anim_done)
        self._closing = False

    # ---------- 展开动画：像苹果那样，从胶囊那一点“长”成整块卡片 ----------
    def _measure(self):
        """量出卡片在 330 宽下的高度，然后放开固定尺寸（几何由动画逐帧给）"""
        self.card.setFixedWidth(330)
        self.card.adjustSize()
        h = self.card.height()
        self.card.setMinimumSize(0, 0)
        self.card.setMaximumSize(16777215, 16777215)
        return h

    def _tick(self, t):
        """t=0 起步于胶囊中心的小方块，t=1 就是完整的浮层"""
        m0, m1 = 2.0, float(self.MARGIN)
        a, b = self._from_rect, self._to_rect
        x = a.x() + (b.x() - a.x()) * t
        y = a.y() + (b.y() - a.y()) * t
        w = a.width() + (b.width() - a.width()) * t
        h = a.height() + (b.height() - a.height()) * t
        self.setGeometry(int(x), int(y), max(8, int(w)), max(8, int(h)))
        mm = int(m0 + (m1 - m0) * t)
        self.card.setGeometry(mm, mm, max(4, int(w) - mm * 2), max(4, int(h) - mm * 2))

    def _anim_done(self):
        if self._closing:
            self._closing = False
            self.hide()

    def show_above(self, ball):
        self._closing = False
        self.place_above(ball)
        self.show()
        self.raise_()
        self.card.show()
        self._anim.stop()
        self._anim.setDuration(280)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._tick(0.0)
        self._anim.start()

    def hide_animated(self):
        if not self.isVisible() or self._closing:
            return
        self._closing = True
        self._anim.stop()
        self._anim.setDuration(170)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.setEasingCurve(QEasingCurve.InCubic)
        self._anim.start()

    def _row(self, tid):
        w = QFrame()
        w.setObjectName("DLRow")
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 8, 8, 9)
        v.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(6)
        title = QLabel("")
        title.setObjectName("DLRowTitle")
        pct = QLabel("")
        pct.setObjectName("Hint")
        pct.setFixedWidth(40)
        pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        # 旁边一个叉号：进行中 = 取消这一项；已完成 = 从列表里划掉
        cancel = QPushButton()
        cancel.setObjectName("DLCancel")
        cancel.setFixedSize(20, 20)
        cancel.setIcon(QIcon(make_icon("close", 11, "#7d878e", 1.9)))
        cancel.setIconSize(QSize(11, 11))
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setToolTip(tr("取消"))
        cancel.clicked.connect(lambda _=False, i=tid: self._on_x(i))
        top.addWidget(title, 1)
        top.addWidget(pct)
        top.addWidget(cancel)
        v.addLayout(top)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        bar.setFixedHeight(4)
        v.addWidget(bar)
        msg = QLabel("")
        msg.setObjectName("Hint")
        msg.setWordWrap(True)
        v.addWidget(msg)
        self.rows_lay.addWidget(w)
        row = {"w": w, "title": title, "pct": pct, "msg": msg,
               "bar": bar, "cancel": cancel}
        self._rows[tid] = row
        return row

    def _on_x(self, tid):
        row = self._rows.get(tid)
        if row is not None and row["cancel"].property("running"):
            try:
                Api.post("/api/cancel", {"id": tid}, timeout=10)
            except Exception:
                pass
            return
        self._dismissed.add(tid)
        if row is not None:
            row["w"].hide()

    def populate(self, tasks, legacy=None, server=None):
        """tasks: /api/state 返回的任务列表；legacy: 非任务型进度（检查/更新 Mod）
        server: 开服信息（内存占用），永远排最后，没有叉号"""
        items = [t for t in list(tasks) if t.get("id") not in self._dismissed]
        if server:
            items.append(server)
        if legacy:
            items.append(legacy)
        if not items:
            self.empty.show()
        else:
            self.empty.hide()
        running = [t for t in items if t.get("installing")]
        self.count.setText(f"· {len(running)} {tr('进行中')}" if running else "")

        seen = set()
        for t in items[:7]:
            tid = t.get("id") or "legacy"
            seen.add(tid)
            row = self._rows.get(tid) or self._row(tid)
            phase = t.get("phase", "downloading")
            title = t.get("title") or t.get("message") or ""
            fm = row["title"].fontMetrics()
            row["title"].setText(fm.elidedText(title, Qt.ElideMiddle, 208))
            row["title"].setToolTip(title)
            pct = int(t.get("percent") or 0)
            is_run = bool(t.get("installing"))
            if phase == "done":
                pct = 100
            row["bar"].setValue(pct)
            if phase == "server":
                # 服务器行没有叉号（要停服去「开服务器」页，或直接关启动器）
                row["cancel"].setVisible(False)
            else:
                row["cancel"].setVisible(True)
                row["cancel"].setProperty("running", is_run)
                row["cancel"].setToolTip(tr("取消") if is_run else tr("移除"))
            if is_run:
                row["pct"].setText(f"{pct}%")
                row["msg"].setText(t.get("message") or "")
            else:
                row["pct"].setText({"done": tr("完成"), "error": tr("失败"),
                                    "cancel": tr("已取消")}.get(phase, f"{pct}%"))
                row["msg"].setText(t.get("message") or "")
            row["w"].show()
        for tid in list(self._rows):
            if tid not in seen:
                row = self._rows.pop(tid)
                row["w"].setParent(None)
                row["w"].deleteLater()
        # 行顺序跟着 items 走：下载在前、服务器行永远垫底（行控件不会重建，位置得手动排）
        for i, t in enumerate(items[:7]):
            row = self._rows.get(t.get("id") or "legacy")
            if row is not None:
                self.rows_lay.removeWidget(row["w"])
                self.rows_lay.insertWidget(i, row["w"])
        # 任务增减导致高度变化时，跟着重新贴一次（开着的时候才动）
        if self.isVisible() and self._ball is not None:
            self.place_above(self._ball)

    def place_above(self, ball):
        """贴着胶囊上方展开；起点缩到胶囊圆心那一点，动画就从这儿长出来"""
        self._ball = ball
        h = self._measure()
        m = self.MARGIN
        w = 330 + m * 2
        full_h = h + m * 2
        x = ball.x() - 10 - m
        y = ball.y() - full_h - 6 + m
        par = self.parentWidget()
        if par is not None:
            x = max(8, min(x, par.width() - w - 8))
            y = max(8, y)
        cx = ball.x() + ball.width() // 2
        cy = ball.y() + ball.height() // 2
        self._to_rect = QRect(int(x), int(y), int(w), int(full_h))
        # 起点：从胶囊正上方冒出的小舌头，胶囊本身始终露着
        self._from_rect = QRect(cx - 11, ball.y() - 20, 22, 20)
        # 已经展开着（任务增减导致重新量高）就直接落到终态，别重播动画
        if self.isVisible() and self._anim.state() != QAbstractAnimation.Running:
            self._tick(1.0)


class Toast(QFrame):
    """全局下载提示：切到任何页面都能看到"""
    clicked = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("Toast")
        # 宽度固定、高度按内容长（长提示要换行，不能裁掉）
        self.setFixedWidth(340)
        self.setMinimumHeight(78)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(6)
        self.title = QLabel(tr("正在下载"))
        self.title.setObjectName("ToastTitle")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFixedHeight(8)
        self.bar.setTextVisible(False)
        self.sub = QLabel("")
        self.sub.setObjectName("ToastSub")
        self.sub.setWordWrap(True)
        lay.addWidget(self.title)
        lay.addWidget(self.bar)
        lay.addWidget(self.sub)
        sh = QGraphicsDropShadowEffect(self)
        sh.setBlurRadius(28)
        sh.setOffset(0, 6)
        sh.setColor(QColor(40, 60, 80, 80))
        self.setGraphicsEffect(sh)
        self.setCursor(Qt.PointingHandCursor)
        # 提示自己会消失：以前靠每个调用点自己 QTimer.singleShot(hide)，
        # 漏一处就一直挂在屏幕上（开服务器那条就漏了）
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self._is_fail = False
        self._fail_text = ""
        self._pos_anim = None
        self.hide()

    def mouseReleaseEvent(self, _e):
        self.clicked.emit()

    def _slide_in(self):
        """从上方 18px 滑入：只动位置，避免和投影特效抢 GraphicsEffect"""
        target = self.pos()
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(220)
        anim.setStartValue(QPoint(target.x(), max(6, target.y() - 18)))
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        self._pos_anim = anim
        anim.start(QPropertyAnimation.DeleteWhenStopped)

    def _appear(self):
        """显示提示；从隐藏到出现才播放滑入，进度刷新不重播"""
        was = self.isVisible()
        self.show()
        self.raise_()
        if not was:
            self._slide_in()

    def show_progress(self, title, sub, pct):
        self._hide_timer.stop()      # 进度条要一直显示到结束
        self.title.setText(title)
        self.sub.setText(f"{sub}{tr('　·　按 ESC 取消')}" if sub else tr("按 ESC 取消"))
        self.bar.setValue(int(pct))
        self.bar.show()
        self._fit()
        self._appear()

    def show_result(self, ok, msg, ms=0):
        self.title.setText(tr("✔ 完成") if ok else tr("✘ 失败"))
        self.sub.setText(msg)
        self.bar.hide()
        # 失败的提示点开看分析：记住原始报错文本，等主窗口取用
        self._is_fail = not ok
        self._fail_text = msg if not ok else ""
        self._hide_timer.start(ms or (3600 if ok else 5600))
        self._fit()
        self._appear()

    def show_info(self, title, sub, ms=4200):
        """既不是成功也不是失败的提示（例如“正在启动服务器”）"""
        self.title.setText(title)
        self.sub.setText(sub)
        self.bar.hide()
        self._hide_timer.start(ms)
        self._fit()
        self._appear()

    def _fit(self):
        """按当前文字算出需要的高度（可能从 78 长到 100+）"""
        self.layout().activate()
        h = max(78, self.layout().heightForWidth(self.width()))
        if h != self.height():
            self.setFixedHeight(h)


class SplitLaunch(QWidget):
    """右下角分体按钮：启动游戏 | ⌄ 选版本"""
    launch = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.btn_main = QPushButton(tr("启动游戏"))
        self.btn_main.setObjectName("SplitLeft")
        self.btn_main.setFixedHeight(44)
        self.btn_main.setCursor(Qt.PointingHandCursor)
        self.btn_main.clicked.connect(self.launch.emit)
        self.btn_more = QPushButton()
        self.btn_more.setObjectName("SplitRight")
        self.btn_more.setFixedSize(42, 44)
        self.btn_more.setIcon(QIcon(make_icon("chevron", 18, "#ffffff", 2.2)))
        self.btn_more.setIconSize(QSize(18, 18))
        self.btn_more.setCursor(Qt.PointingHandCursor)
        self.btn_more.setToolTip(tr("选择版本"))
        lay.addWidget(self.btn_main)
        lay.addWidget(self.btn_more)


# ---------------- 页面 ----------------
class HomePage(QWidget):
    account_clicked = Signal()

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 26, 32, 8)
        lay.setSpacing(10)
        self.chip = QPushButton(tr("  ＋  添加账户"))
        self.chip.setObjectName("GhostBtn")
        self.chip.setCursor(Qt.PointingHandCursor)
        self.chip.setFixedHeight(38)
        self.chip.clicked.connect(self.account_clicked.emit)
        row = QHBoxLayout()
        row.addWidget(self.chip)
        row.addStretch()
        lay.addLayout(row)
        lay.addStretch()

    def set_account(self, acc):
        if not acc:
            self.chip.setText(tr("  ＋  添加账户"))
            self.chip.setIcon(QIcon())
        else:
            tag = {"offline": tr("离线"), "msa": tr("正版")}.get(acc.get("type"), tr("第三方"))
            self.chip.setText(f"  {acc.get('name')}   ·  {tag}")
            pm = avatar_pixmap(acc.get("name", ""), 26)
            self.chip.setIcon(QIcon(pm) if pm else QIcon())
            self.chip.setIconSize(QSize(26, 26))


# ---------------- 资源中心：分类汉化 / 数字时间格式化 / 富列表项 ----------------
RES_CAT_ZH = {
    "optimization": "性能优化", "utility": "实用", "decoration": "装饰", "technology": "科技",
    "adventure": "冒险", "magic": "魔法", "storage": "存储", "worldgen": "世界生成",
    "library": "支持库", "management": "管理", "equipment": "装备", "mobs": "生物",
    "food": "食物", "game-mechanics": "游戏机制", "transportation": "运输",
    "cursed": "诅咒", "minigame": "小游戏", "economy": "经济", "combat": "战斗",
    "challenge": "挑战", "challenging": "挑战", "hardcore": "硬核",
    "kitchen-sink": "大杂烩", "lightweight": "轻量",
    "multiplayer": "多人", "quests": "任务", "themed": "主题", "tweaks": "微调",
    "vanilla-like": "原版风", "realistic": "写实", "medieval": "中世纪",
    "modern": "现代", "anime": "动漫", "fantasy": "幻想", "simplistic": "简约",
    "audio": "音效", "blocks": "方块", "environment": "环境", "fonts": "字体",
    "gui": "界面", "items": "物品", "locale": "语言", "models": "模型",
    "shaders": "光影", "cartoon": "卡通", "semi-realistic": "半写实",
    "entities": "实体", "minecraft": "原版", "atmosphere": "氛围",
    "core-shaders": "核心光影", "modded": "模组", "vanilla": "原版",
    "performance": "优化", "potato": "低配", "low": "低配", "medium": "中等",
    "high": "高配", "iris": "Iris", "optifine": "OptiFine",
    "colored-lighting": "彩色光照", "path-tracing": "路径追踪",
    "reflections": "反射", "ssao": "SSAO", "bloom": "泛光", "shadows": "阴影",
}
RES_LOADER_NAME = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge",
                   "quilt": "Quilt", "liteloader": "LiteLoader", "rift": "Rift"}


def _cat_label(cat):
    if get_lang() == "en":
        return cat
    return RES_CAT_ZH.get(cat, cat)


def _fmt_count(n):
    n = int(n or 0)
    if get_lang() == "en":
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)
    if n >= 100_000_000:
        return f"{n / 100_000_000:.2f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def _rel_time(iso):
    """ISO 时间 -> 「3 小时前」这类相对时间"""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        sec = (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return iso[:10]
    if sec < 60:
        return tr("刚刚")
    if sec < 3600:
        return f"{int(sec // 60)}{tr(' 分钟前')}"
    if sec < 86400:
        return f"{int(sec // 3600)}{tr(' 小时前')}"
    if sec < 86400 * 30:
        return f"{int(sec // 86400)}{tr(' 天前')}"
    if sec < 86400 * 365:
        return f"{int(sec // (86400 * 30))}{tr(' 个月前')}"
    return f"{int(sec // (86400 * 365))}{tr(' 年前')}"


def _round_icon(pm, size=46, radius=10):
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    p.setClipPath(path)
    scaled = pm.scaled(size, size, Qt.KeepAspectRatioByExpanding,
                       Qt.SmoothTransformation)
    p.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    p.end()
    return out


RES_MC_VERSIONS = ["1.21.4", "1.21.1", "1.20.6", "1.20.4", "1.20.1", "1.19.4",
                   "1.19.2", "1.18.2", "1.17.1", "1.16.5", "1.12.2", "1.8.9"]
RES_CATS = {
    "mod": ["optimization", "utility", "decoration", "technology", "magic",
            "adventure", "storage", "worldgen", "library", "management",
            "equipment", "mobs", "food", "game-mechanics"],
    "modpack": ["adventure", "challenge", "combat", "kitchen-sink", "lightweight",
                "magic", "multiplayer", "optimization", "quests", "technology"],
    "datapack": ["adventure", "decoration", "economy", "equipment", "food",
                 "game-mechanics", "library", "magic", "minigame", "mobs",
                 "optimization", "storage", "technology", "transportation",
                 "utility", "worldgen"],
    "resourcepack": ["16x", "32x", "64x", "128x", "256x", "512x", "anime",
                     "realistic", "medieval", "modern", "simplistic", "themed",
                     "vanilla-like", "fonts", "gui", "audio"],
    "shader": ["shaders", "realistic", "semi-realistic", "vanilla-like",
               "fantasy", "cartoon", "performance", "low", "medium", "high",
               "iris", "optifine", "reflections", "bloom", "shadows"],
}


class _IconSignal(QObject):
    got = Signal(str, bytes)


class _ResItem(QWidget):
    """搜索结果的一行：图标 + 标题 + 分类/描述 + 版本·下载量·更新时间·来源"""

    def __init__(self, h):
        super().__init__()
        self.h = h
        self.icon_url = h.get("icon", "")
        self._desc_text = h.get("desc", "")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 16, 10)
        lay.setSpacing(12)

        self.icon = QLabel()
        self.icon.setFixedSize(46, 46)
        self.icon.setPixmap(self._placeholder())
        lay.addWidget(self.icon, 0, Qt.AlignTop)

        box = QVBoxLayout()
        box.setContentsMargins(0, 2, 0, 2)
        box.setSpacing(8)

        self.title = QLabel(h.get("title", ""))
        self.title.setStyleSheet(f"color:{TEXT}; font-size:14px; font-weight:bold;")
        box.addWidget(self.title)

        desc_row = QHBoxLayout()
        desc_row.setContentsMargins(0, 0, 0, 0)
        desc_row.setSpacing(8)
        cats = h.get("categories") or []
        if cats:
            self.tags = QLabel("  ".join(_cat_label(x) for x in cats[:3]))
            self.tags.setStyleSheet(f"color:{theme_colors()['accent_dark']};"
                                    " font-size:11px;")
            desc_row.addWidget(self.tags, 0)
        self.desc = QLabel(self._desc_text)
        self.desc.setStyleSheet(f"color:{SUB}; font-size:12px;")
        self.desc.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        desc_row.addWidget(self.desc, 1)
        box.addLayout(desc_row)

        meta_parts = []
        loaders = [RES_LOADER_NAME.get(x, x.title()) for x in (h.get("loaders") or [])][:2]
        vs = h.get("versions") or []
        vtext = (vs[0] + ("+" if len(vs) > 1 else "")) if vs else ""
        head = " ".join(x for x in ["/".join(loaders), vtext] if x)
        if head:
            meta_parts.append(head)
        meta_parts.append(f"↓ {_fmt_count(h.get('downloads'))}")
        if h.get("updated"):
            meta_parts.append(f"↑ {_rel_time(h['updated'])}")
        meta_parts.append("CurseForge" if h.get("source") == "cf" else "Modrinth")
        self._meta_text = "     ".join(meta_parts)
        self.meta = QLabel(self._meta_text)
        self.meta.setStyleSheet("color:#8a949c; font-size:11px;")
        self.meta.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        box.addWidget(self.meta)

        lay.addLayout(box, 1)

    def _placeholder(self):
        col = self.h.get("color") or 0
        if not col:
            col = (hash(self.h.get("title", "")) & 0x7F7F7F) | 0x606060
        pm = QPixmap(46, 46)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, 46, 46), 10, 10)
        p.fillPath(path, QColor(f"#{col & 0xFFFFFF:06x}"))
        p.setPen(QColor("#ffffff"))
        f = p.font()
        f.setPixelSize(22)
        f.setBold(True)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignCenter, (self.h.get("title") or "?")[:1].upper())
        p.end()
        return pm

    def set_icon(self, pm):
        self.icon.setPixmap(_round_icon(pm, 46, 10))

    def resizeEvent(self, ev):
        fm = self.desc.fontMetrics()
        self.desc.setText(fm.elidedText(self._desc_text, Qt.ElideRight,
                                       max(60, self.desc.width())))
        fm2 = self.meta.fontMetrics()
        self.meta.setText(fm2.elidedText(self._meta_text, Qt.ElideRight,
                                        max(120, self.width() - 90)))
        super().resizeEvent(ev)


CF_KEY_PAGE = "https://console.curseforge.com/?#/api-keys"


class _CFKeyGuide(QWidget):
    """选 CurseForge 但还没配 Key 时的引导卡：申请 → 粘贴 → 校验，一步不离开当前页"""

    def __init__(self, on_ok=None):
        super().__init__()
        self._on_ok = on_ok
        self._t = None
        c = theme_colors()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(10)

        title = QLabel(tr("CurseForge 需要一把免费的 API Key"))
        title.setStyleSheet(f"color:{TEXT}; font-size:14px; font-weight:bold;")
        lay.addWidget(title)

        body = QLabel(tr("这是 CurseForge 官方的规矩：不开放匿名搜索，每把 Key 绑在申请人的账号上，所以只能由你本人申请一次（免费，一两分钟，长期有效）。拿到后粘到下面，启动器会先验证再保存。"))
        body.setWordWrap(True)
        body.setObjectName("Hint")
        lay.addWidget(body)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.Password)
        self.edit.setPlaceholderText(tr("粘贴 API Key（$2a$10$ 开头的那一长串）"))
        self.edit.returnPressed.connect(self._submit)
        row.addWidget(self.edit, 1)

        self.b_ok = QPushButton(tr("保存并验证"))
        self.b_ok.setObjectName("AccentGhost")
        self.b_ok.setCursor(Qt.PointingHandCursor)
        self.b_ok.clicked.connect(self._submit)
        row.addWidget(self.b_ok)

        b_site = QPushButton(tr("去申请"))
        b_site.setObjectName("GhostBtn")
        b_site.setCursor(Qt.PointingHandCursor)
        b_site.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(CF_KEY_PAGE)))
        row.addWidget(b_site)
        lay.addLayout(row)

        self.state = QLabel(tr("申请地址：console.curseforge.com → API Keys → 新建 Key"))
        self.state.setStyleSheet(f"color:{SUB}; font-size:11px;")
        self.state.setWordWrap(True)
        lay.addWidget(self.state)

    def _submit(self):
        key = self.edit.text().strip()
        if not key:
            self.state.setText(tr("先把 Key 粘进来"))
            return
        self.b_ok.setEnabled(False)
        self.b_ok.setText(tr("验证中…"))
        self.state.setStyleSheet(f"color:{SUB}; font-size:11px;")
        self.state.setText(tr("正在向 CurseForge 校验这把 Key…"))
        t = ApiThread("POST", "/api/cf/key", {"key": key}, timeout=40)
        t.done.connect(self._done)
        t.start()
        self._t = t

    def _done(self, d):
        self.b_ok.setEnabled(True)
        self.b_ok.setText(tr("保存并验证"))
        if d.get("ok") and d.get("valid"):
            c = theme_colors()
            self.state.setStyleSheet(f"color:{c['accent_dark']}; font-size:11px;")
            self.state.setText(tr("✔ 验证通过，已经保存。正在重新搜索…"))
            if self._on_ok:
                QTimer.singleShot(120, self._on_ok)
            return
        self.state.setStyleSheet("color:#d9534f; font-size:11px;")
        self.state.setText(f"{tr('✘ 没通过：')}{d.get('error') or tr('未知错误')}")


# ---------------- 资源详情页：文件分组 / 文件行 / 版本筛选 ----------------
VER_TYPE_LABEL = {"release": "正式版", "beta": "测试版", "alpha": "内测版"}
VER_TYPE_LETTER = {"release": "R", "beta": "B"}


def _ver_type_label(t):
    return tr(VER_TYPE_LABEL.get(t or "release", "正式版"))


def _ver_badge(kind, col, size=30):
    """文件行左侧徽标：正式版 R / 测试版 B / 内测版 A，绿-琥珀-灰"""
    letter = VER_TYPE_LETTER.get(kind, "A")
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), 8, 8)
    p.fillPath(path, QColor(col))
    p.setPen(QColor("#ffffff"))
    f = p.font()
    f.setPixelSize(int(size * 0.5))
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, letter)
    p.end()
    return pm


def _mc_base(ver):
    """26.3-rc-1 -> 26.3（版本筛选里把预发布归到正式版本下）"""
    return re.sub(r"[-_ ](rc|pre|preview|snapshot|beta|alpha).*$", "", ver or "",
                  flags=re.I)


def _mc_sort_key(ver):
    """版本号排序：26.3 > 26.3-rc-2，1.21.4 > 1.21"""
    nums = [int(x) for x in re.findall(r"\d+", _mc_base(ver))][:4]
    nums += [0] * (4 - len(nums))
    pre = 0 if _mc_base(ver) != (ver or "") else 1
    return tuple(nums) + (pre,)


def _ph_icon(h, size):
    """没下到图标前的占位：主题色圆角块 + 首字母"""
    col = h.get("color") or 0
    if not col:
        col = (hash(h.get("title", "")) & 0x7F7F7F) | 0x606060
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    p.fillPath(path, QColor(f"#{col & 0xFFFFFF:06x}"))
    p.setPen(QColor("#ffffff"))
    f = p.font()
    f.setPixelSize(int(size * 0.45))
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, (h.get("title") or "?")[:1].upper())
    p.end()
    return pm


def _clear_layout(lay):
    while lay.count():
        it = lay.takeAt(0)
        w = it.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        else:
            sub = it.layout()
            if sub is not None:
                _clear_layout(sub)


class _GroupBar(QWidget):
    """分组标题栏：标题 + 右侧折叠箭头，整行可点"""

    clicked = Signal()

    def __init__(self, title):
        super().__init__()
        self.setObjectName("ResGroupBar")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(44)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 16, 0)
        self.title = QLabel(title)
        self.title.setStyleSheet(
            f"color:{TEXT}; font-size:13px; font-weight:bold;"
            " background: transparent;")
        self.chev = QLabel("⌃")
        self.chev.setStyleSheet(f"color:{SUB}; font-size:13px; background: transparent;")
        lay.addWidget(self.title)
        lay.addStretch(1)
        lay.addWidget(self.chev)

    def set_expanded(self, on):
        self.chev.setText("⌃" if on else "⌄")

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(ev)


class _ResGroupCard(QWidget):
    """一组文件（同一个加载器 + 同一个游戏版本），标题可折叠"""

    def __init__(self, title, rows, expanded=True):
        super().__init__()
        self.setObjectName("ResGroupCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.bar = _GroupBar(title)
        lay.addWidget(self.bar)
        self.body = QWidget()
        self.body.setObjectName("ResGroupBody")
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(8, 0, 8, 8)
        bl.setSpacing(2)
        for w in rows:
            bl.addWidget(w)
        lay.addWidget(self.body)
        self.bar.clicked.connect(self._toggle)
        self.set_expanded(expanded)

    def _toggle(self):
        self.set_expanded(not self.body.isVisible())

    def set_expanded(self, on):
        self.body.setVisible(on)
        self.bar.set_expanded(on)


class _ResFileRow(QWidget):
    """详情页里的一行文件：徽标 + 「Sodium 0.9.3-beta.4 for NeoForge 26.3」+ 元信息"""

    def __init__(self, v, on_pick=None):
        super().__init__()
        self.v = v
        self._on_pick = on_pick
        self._sub_text = self._subtitle(v)
        self.setObjectName("ResFileRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(46)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 12, 6)
        lay.setSpacing(10)

        kind = v.get("type") or "release"
        col = {"release": theme_colors()["accent"],
               "beta": "#d9a24a"}.get(kind, "#8b93a3")
        self.badge = QLabel()
        self.badge.setFixedSize(30, 30)
        self.badge.setPixmap(_ver_badge(kind, col))
        self.badge.setStyleSheet("background: transparent; border: none;")
        lay.addWidget(self.badge, 0, Qt.AlignVCenter)

        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(1)
        self.t1 = QLabel(v.get("name") or v.get("number") or "")
        self.t1.setStyleSheet(f"color:{TEXT}; font-size:13px; font-weight:bold;"
                              " background: transparent;")
        self.t2 = QLabel(self._sub_text)
        self.t2.setStyleSheet(f"color:{SUB}; font-size:11px; background: transparent;")
        for w in (self.t1, self.t2):
            w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        box.addWidget(self.t1)
        box.addWidget(self.t2)
        lay.addLayout(box, 1)

        self.set_selected(False)

    def _subtitle(self, v):
        parts = []
        lds = [RES_LOADER_NAME.get(x, x.title()) for x in (v.get("loaders") or [])]
        if lds:
            parts.append(" / ".join(lds))
        if v.get("filename"):
            parts.append(v["filename"])
        if v.get("downloads"):
            parts.append(f"{tr('下载 ')}{v['downloads']}{tr(' 次')}")
        if v.get("date_full"):
            parts.append(f"{tr('更新于 ')}{_rel_time(v['date_full'])}")
        parts.append(_ver_type_label(v.get("type")))
        return "，".join(parts)

    def set_selected(self, on):
        self.setProperty("sel", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._on_pick:
            self._on_pick(self.v, self)
        super().mousePressEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._on_pick:
            self._on_pick(self.v, self, True)
        super().mouseDoubleClickEvent(ev)

    def resizeEvent(self, ev):
        w = max(80, self.width() - 80)
        fm = self.t1.fontMetrics()
        self.t1.setText(fm.elidedText(self.v.get("name") or self.v.get("number") or "",
                                      Qt.ElideRight, w))
        fm2 = self.t2.fontMetrics()
        self.t2.setText(fm2.elidedText(self._sub_text, Qt.ElideRight, w))
        super().resizeEvent(ev)


# ---------------- 游戏版本页：最新版本卡 / 折叠分组 / 加载器行 ----------------
FOOLS_HINTS = ("15w14a", "rv-pre", "shareware", "oneblockatatime", "infinite",
               "or_b", "potato", "craftmine")


def _is_fools(v):
    """愚人节快照靠 id 关键字识别（Mojang 没有专门标记）"""
    sid = (v.get("id") or "").lower()
    return any(h in sid for h in FOOLS_HINTS)


def _fmt_ver_time(iso):
    """ISO 时间 -> 2026/09/15 19:23（转本地时区）"""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y/%m/%d %H:%M")
    except Exception:
        return iso[:10].replace("-", "/")


def _fmt_ver_date(iso):
    return _fmt_ver_time(iso)[:10]


@lru_cache(maxsize=24)
def _mc_block_icon(kind="grass", size=36):
    """像素风方块图标：按 16x16 网格逐格填色，草方块 / 命令方块"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0.5, 0.5, size - 1, size - 1),
                        size * 0.2 * 0.5 + 3, size * 0.2 * 0.5 + 3)
    p.setClipPath(path)
    cell = (size - 1) / 16.0
    if kind == "grass":
        ground = ("#8a6744", "#7d5c3c", "#96724d", "#846240")
        grass = ("#6cb03a", "#5f9d33", "#78bd46", "#69a938")
        for gy in range(16):
            for gx in range(16):
                if gy < 4:
                    col = grass[(gx * 3 + gy * 5) % 4]
                elif gy == 4:
                    col = grass[(gx * 7) % 4] if gx % 3 else ground[(gx * 5) % 4]
                else:
                    col = ground[(gx * 5 + gy * 3) % 4]
                p.fillRect(QRectF(0.5 + gx * cell, 0.5 + gy * cell,
                                  cell + 0.4, cell + 0.4), QColor(col))
    else:
        base = ("#b07a3c", "#9c6a32", "#c08a4a", "#a67438")
        for gy in range(16):
            for gx in range(16):
                p.fillRect(QRectF(0.5 + gx * cell, 0.5 + gy * cell,
                                  cell + 0.4, cell + 0.4),
                           QColor(base[(gx * 7 + gy * 3) % 4]))
        p.setPen(QPen(QColor("#6b4520"), max(1.0, cell * 1.2)))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(1.5 + cell * 3, 1.5 + cell * 3, cell * 10, cell * 10))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#6b4520"))
        for dx, dy in ((6, 6), (9, 6), (6, 9), (9, 9)):
            p.drawRect(QRectF(0.5 + dx * cell, 0.5 + dy * cell, cell, cell))
    p.end()
    return pm


class _VerRow(QFrame):
    """最新版本卡里的一行：方块图标 + 版本号 + 说明"""

    def __init__(self, v, sub, on_click):
        super().__init__()
        self.v = v
        self._on_click = on_click
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("VerRow")
        self.setStyleSheet(
            "#VerRow { background: transparent; border: none; border-radius: 10px; }"
            "#VerRow:hover { background: #f5f8f4; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 6, 8, 6)
        lay.setSpacing(12)
        ic = QLabel()
        ic.setFixedSize(36, 36)
        ic.setPixmap(_mc_block_icon("grass" if v.get("type") == "release"
                                    else "command", 36))
        lay.addWidget(ic, 0, Qt.AlignVCenter)
        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)
        t = QLabel(v.get("id", ""))
        t.setStyleSheet(f"color:{TEXT}; font-size:15px; font-weight:bold;")
        box.addWidget(t)
        sl = QLabel(sub)
        sl.setStyleSheet("color:#8a949c; font-size:11px;")
        box.addWidget(sl)
        lay.addLayout(box, 1)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._on_click:
            self._on_click(self.v)


class _FoldBar(QPushButton):
    """折叠分组条：白卡 + 左标题 + 右 chevron（自绘，避免字体缺字形）"""

    def __init__(self, text):
        super().__init__(text)
        self.setCursor(Qt.PointingHandCursor)
        self.setCheckable(True)
        self.setFixedHeight(52)
        self.setStyleSheet(
            "QPushButton { background: #ffffff; border: 1px solid #ece7da;"
            " border-radius: 12px; text-align: left; padding-left: 18px;"
            f" font-size: 13px; font-weight: bold; color: {TEXT}; }}"
            "QPushButton:hover { background: #fbfdfa; }")

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor("#9aa1a6"), 1.7, Qt.SolidLine, Qt.RoundCap))
        cx = self.width() - 24.0
        cy = self.height() / 2.0
        if self.isChecked():
            p.drawLine(int(cx - 5), int(cy + 2), int(cx), int(cy - 3))
            p.drawLine(int(cx), int(cy - 3), int(cx + 5), int(cy + 2))
        else:
            p.drawLine(int(cx - 5), int(cy - 3), int(cx), int(cy + 2))
            p.drawLine(int(cx), int(cy + 2), int(cx + 5), int(cy - 3))
        p.end()


class _FoldGroup(QWidget):
    """折叠分组：标题条 + 展开后的版本列表"""

    def __init__(self, title):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.bar = _FoldBar(title)
        lay.addWidget(self.bar)
        self.list = QListWidget()
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setMaximumHeight(240)
        self.list.setVisible(False)
        c = theme_colors()
        self.list.setStyleSheet(
            "QListWidget { background: #ffffff; border: 1px solid #ece7da;"
            " border-radius: 12px; padding: 6px; outline: 0; }"
            "QListWidget::item { padding: 0px 12px; margin: 1px 2px;"
            f" border-radius: 8px; color: {TEXT}; }}"
            f"QListWidget::item:selected {{ background: {c['accent_soft']};"
            f" color: {c['accent_dark']}; }}")
        lay.addWidget(self.list)


class _LoaderRow(QFrame):
    """详情页的加载器行：名称 + 状态 + chevron"""

    clicked = Signal(str)

    def __init__(self, kind, label):
        super().__init__()
        self.kind = kind
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("LoaderRow")
        self.setFixedHeight(52)
        self.setStyleSheet(
            "#LoaderRow { background: #ffffff; border: 1px solid #ece7da;"
            " border-radius: 12px; }"
            "#LoaderRow:hover { background: #fbfdfa; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 0, 30, 0)
        lay.setSpacing(14)
        self.name = QLabel(label)
        self.name.setStyleSheet(f"color:{TEXT}; font-size:13px; font-weight:bold;")
        self.name.setFixedWidth(92)
        lay.addWidget(self.name)
        self.status = QLabel("")
        lay.addWidget(self.status, 1)
        self.set_status(tr("正在获取…"))

    def set_status(self, text, kind="idle"):
        self.status.setText(text)
        color = {"idle": "#8a949c", "ok": theme_colors()["accent_dark"],
                 "none": "#b8b2a4", "pick": theme_colors()["accent_dark"]}.get(kind, "#8a949c")
        weight = "bold" if kind == "pick" else "normal"
        self.status.setStyleSheet(f"color:{color}; font-size:12px; font-weight:{weight};")

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor("#b3aca0"), 1.7, Qt.SolidLine, Qt.RoundCap))
        cx = self.width() - 18.0
        cy = self.height() / 2.0
        p.drawLine(int(cx - 3), int(cy - 5), int(cx + 2), int(cy))
        p.drawLine(int(cx + 2), int(cy), int(cx - 3), int(cy + 5))
        p.end()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.clicked.emit(self.kind)


class _LoaderPickDialog(QDialog):
    """挑某个加载器的具体版本：白卡 + 搜索 + 列表"""

    def __init__(self, parent, title, items):
        super().__init__(parent)
        self.value = ""
        self._all = list(items)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(440, 430)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("PickCard")
        card.setStyleSheet(
            "#PickCard { background: #ffffff; border: 1px solid #e8e2d4;"
            " border-radius: 16px; }")
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)

        head = QLabel(title)
        head.setStyleSheet(f"color:{TEXT}; font-size:14px; font-weight:bold;")
        lay.addWidget(head)

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("搜索版本号"))
        self.search.textChanged.connect(self._filter)
        lay.addWidget(self.search)

        self.lst = QListWidget()
        self.lst.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        c = theme_colors()
        self.lst.setStyleSheet(
            "QListWidget { background: #fbfaf5; border: 1px solid #ece7da;"
            " border-radius: 10px; padding: 4px; outline: 0; }"
            "QListWidget::item { padding: 8px 10px; margin: 1px 2px;"
            f" border-radius: 8px; color: {TEXT}; }}"
            f"QListWidget::item:selected {{ background: {c['accent_soft']};"
            f" color: {c['accent_dark']}; }}")
        self.lst.itemDoubleClicked.connect(lambda _i: self._accept())
        lay.addWidget(self.lst, 1)
        self._filter("")

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        cancel = QPushButton(tr("取消"))
        cancel.setObjectName("GhostBtn")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        ok = QPushButton(tr("使用此版本"))
        ok.setObjectName("PrimaryBtn")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self._accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def _filter(self, text):
        text = (text or "").strip().lower()
        self.lst.clear()
        for name in self._all:
            if text and text not in name.lower():
                continue
            self.lst.addItem(QListWidgetItem(name))
        if self.lst.count():
            self.lst.setCurrentRow(0)

    def _accept(self):
        it = self.lst.currentItem()
        if it:
            self.value = it.text()
            self.accept()


class DownloadPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.versions = []
        self.loader_data = {}
        self._threads = []
        self.detail_ver = None
        self.loader_choice = {}
        self.detail_loaders = {}
        self.mirror = "bmclapi"
        self._icon_cache = {}
        self._icon_busy = set()
        self._icon_sig = _IconSignal()
        self._icon_sig.got.connect(self._on_icon)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 26, 32, 8)
        lay.setSpacing(12)

        seg_wrap = QWidget()
        seg_wrap.setObjectName("SegWrap")
        seg_wrap.setFixedHeight(40)
        seg_wrap.setStyleSheet(
            "#SegWrap { background: rgba(255,255,255,0.62); border-radius: 12px; }")
        seg_lay = QHBoxLayout(seg_wrap)
        seg_lay.setContentsMargins(4, 4, 4, 4)
        seg_lay.setSpacing(4)
        self.seg_btns = []
        seg_defs = [tr("游戏版本"), tr("模组"), tr("整合包"),
                    tr("数据包"), tr("资源包"), tr("光影包")]
        for i, text in enumerate(seg_defs):
            b = QPushButton(text)
            b.setCursor(Qt.PointingHandCursor)
            b.setObjectName("SegOn" if i == 0 else "SegOff")
            b.clicked.connect(lambda _=False, idx=i: self.switch_seg(idx))
            seg_lay.addWidget(b)
            self.seg_btns.append(b)
        seg_lay.addStretch()
        seg_row = QHBoxLayout()
        seg_row.addWidget(seg_wrap)
        seg_row.addStretch()
        lay.addLayout(seg_row)

        self.inner = QStackedWidget()
        self.res = {}
        self.inner.addWidget(self.make_vanilla())
        self.inner.addWidget(self.make_resource("mod", tr("Mod")))
        self.inner.addWidget(self.make_resource("modpack", tr("整合包")))
        self.inner.addWidget(self.make_resource("datapack", tr("数据包")))
        self.inner.addWidget(self.make_resource("resourcepack", tr("资源包")))
        self.inner.addWidget(self.make_resource("shader", tr("光影包")))
        lay.addWidget(self.inner, 1)

        self.load_versions()

    RES_ORDER = ("mod", "modpack", "datapack", "resourcepack", "shader")

    def switch_seg(self, idx):
        self.inner.setCurrentIndex(idx)
        fade_in(self.inner.currentWidget(), 150)
        for i, b in enumerate(self.seg_btns):
            b.setObjectName("SegOn" if i == idx else "SegOff")
            b.style().unpolish(b)
            b.style().polish(b)
        # 资源页：切到即自动拉一次热门列表，不用再点「搜索」
        if 1 <= idx < 1 + len(self.RES_ORDER):
            self._res_ensure(self.RES_ORDER[idx - 1])

    def make_vanilla(self):
        """游戏版本页：列表视图（最新版本 + 四类折叠） / 详情视图（加载器 + 下载）"""
        self.ver_stack = QStackedWidget()
        self.ver_stack.addWidget(self._make_ver_list())
        self.ver_stack.addWidget(self._make_ver_detail())
        return self.ver_stack

    def _make_ver_list(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 8, 0)
        v.setSpacing(10)

        # ---- 最新版本卡 ----
        card = QFrame()
        card.setObjectName("LatestCard")
        card.setStyleSheet(
            "#LatestCard { background: #ffffff; border: 1px solid #ece7da;"
            " border-radius: 14px; }")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 12)
        cl.setSpacing(6)
        h = QLabel(tr("最新版本"))
        h.setStyleSheet(f"color:{TEXT}; font-size:13px; font-weight:bold;")
        cl.addWidget(h)
        self.latest_box = QVBoxLayout()
        self.latest_box.setContentsMargins(0, 0, 0, 0)
        self.latest_box.setSpacing(2)
        cl.addLayout(self.latest_box)
        self.latest_hint = QLabel(tr("正在获取版本列表…"))
        self.latest_hint.setStyleSheet("color:#8a949c; font-size:12px;")
        cl.addWidget(self.latest_hint)
        v.addWidget(card)

        # ---- 四个折叠分组 ----
        self.fold_groups = {}
        for title, key in ((tr("正式版"), "release"), (tr("预览版"), "snapshot"),
                           (tr("远古版"), "old"), (tr("愚人节版"), "fools")):
            g = _FoldGroup(title)
            g.bar.clicked.connect(lambda _=False, k=key: self._toggle_fold(k))
            g.list.itemClicked.connect(self._on_fold_click)
            v.addWidget(g)
            self.fold_groups[key] = g

        v.addStretch(1)
        scroll.setWidget(host)
        lay.addWidget(scroll, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        tip = QLabel(tr("点版本查看详情并下载 · 已安装的版本前面会标出"))
        tip.setStyleSheet("color:#8a949c; font-size:11px;")
        row.addWidget(tip, 1)
        refresh = QPushButton(tr("刷新"))
        refresh.setObjectName("GhostBtn")
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.clicked.connect(self.load_versions)
        row.addWidget(refresh)
        lay.addLayout(row)
        return page

    def _make_ver_detail(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        head = QFrame()
        head.setStyleSheet(
            "QFrame#DetailHead { background: #ffffff; border: 1px solid #ece7da;"
            " border-radius: 14px; }")
        head.setObjectName("DetailHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(12, 10, 14, 10)
        hl.setSpacing(12)
        back = QPushButton("←")
        back.setCursor(Qt.PointingHandCursor)
        back.setFixedSize(30, 30)
        back.setStyleSheet(
            "QPushButton { background: transparent; border: none;"
            " font-size: 17px; color: #6d767d; border-radius: 8px; }"
            "QPushButton:hover { background: #f2f5f1; }")
        back.clicked.connect(self.close_detail)
        hl.addWidget(back)
        self.detail_icon = QLabel()
        self.detail_icon.setFixedSize(34, 34)
        hl.addWidget(self.detail_icon)
        self.detail_id = QLineEdit()
        self.detail_id.setReadOnly(True)
        self.detail_id.setStyleSheet(
            "QLineEdit { background: #ffffff; border: 1px solid #dcd9d0;"
            " border-radius: 8px; padding: 6px 10px; font-size: 14px; }")
        hl.addWidget(self.detail_id, 1)
        lay.addWidget(head)

        self.loader_rows = {}
        for kind, label in (("forge", "Forge"), ("neoforge", "NeoForge"),
                            ("fabric", "Fabric"), ("optifine", "OptiFine")):
            r = _LoaderRow(kind, label)
            r.clicked.connect(self._pick_loader)
            lay.addWidget(r)
            self.loader_rows[kind] = r

        lay.addStretch(1)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        self.dl_btn = QPushButton(tr("开始下载"))
        self.dl_btn.setObjectName("PrimaryBtn")
        self.dl_btn.setCursor(Qt.PointingHandCursor)
        self.dl_btn.setMinimumWidth(150)
        self.dl_btn.setFixedHeight(38)
        self.dl_btn.clicked.connect(self.start_download)
        bottom.addStretch(1)
        bottom.addWidget(self.dl_btn)
        bottom.addStretch(1)
        lay.addLayout(bottom)
        return page

    # ---------- 列表视图逻辑 ----------
    def _toggle_fold(self, key):
        g = self.fold_groups[key]
        opening = not g.list.isVisible()
        for k, other in self.fold_groups.items():
            other.bar.setChecked(False)
            if other.list.isVisible():
                other.list.setVisible(False)
        if opening:
            g.bar.setChecked(True)
            self._fill_fold(key)
            g.list.setVisible(True)

    def _versions_of(self, key):
        vs = self.versions
        if key == "release":
            return [v for v in vs if v["type"] == "release"]
        if key == "old":
            return [v for v in vs if v["type"] in ("old_alpha", "old_beta")]
        if key == "fools":
            return [v for v in vs if v["type"] == "snapshot" and _is_fools(v)]
        return [v for v in vs if v["type"] == "snapshot" and not _is_fools(v)]

    def _fill_fold(self, key):
        g = self.fold_groups[key]
        g.list.clear()
        items = self._versions_of(key)
        if not items:
            it = QListWidgetItem(tr("（暂无版本）"))
            it.setFlags(Qt.NoItemFlags)
            g.list.addItem(it)
            return
        installed = set(getattr(self.win, "installed", set()))
        for v in items[:800]:
            mark = "✔  " if v["id"] in installed else ""
            it = QListWidgetItem(f"{mark}{v['id']}    {_fmt_ver_date(v['releaseTime'])}")
            it.setSizeHint(QSize(0, 34))
            it.setData(Qt.UserRole, v)
            g.list.addItem(it)

    def _on_fold_click(self, item):
        v = item.data(Qt.UserRole)
        if v:
            self.open_version(v)

    def _refresh_latest(self):
        while self.latest_box.count():
            it = self.latest_box.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        rel = next((v for v in self.versions if v["type"] == "release"), None)
        snap = next((v for v in self.versions if v["type"] == "snapshot"
                     and not _is_fools(v)), None)
        shown = 0
        for v, label in ((rel, tr("最新正式版")), (snap, tr("最新预览版"))):
            if not v:
                continue
            sub = f"{label}{tr('，发布于 ')}{_fmt_ver_time(v['releaseTime'])}"
            self.latest_box.addWidget(_VerRow(v, sub, self.open_version))
            shown += 1
        self.latest_hint.setVisible(shown == 0)

    # ---------- 详情视图逻辑 ----------
    def open_version(self, v):
        self.detail_ver = v
        self.loader_choice = {}
        self.detail_loaders = {}
        self.detail_id.setText(v.get("id", ""))
        self.detail_icon.setPixmap(
            _mc_block_icon("grass" if v.get("type") == "release" else "command", 34))
        for row in self.loader_rows.values():
            row.set_status(tr("正在获取…"))
        self.ver_stack.setCurrentIndex(1)
        fade_in(self.ver_stack.currentWidget(), 150)
        from urllib.parse import quote
        th = ApiThread("GET", f"/api/loaders?mc={quote(v.get('id', ''))}", timeout=120)
        th.done.connect(self._on_detail_loaders)
        self._bg(th)

    def close_detail(self):
        self.ver_stack.setCurrentIndex(0)
        fade_in(self.ver_stack.currentWidget(), 150)

    def _bg(self, th):
        """线程对象留在列表里，避免被 GC（QThread destroyed while running）"""
        self._threads.append(th)
        th.start()

    def _on_detail_loaders(self, d):
        if not d.get("ok"):
            for row in self.loader_rows.values():
                row.set_status(tr("获取失败"), "none")
            return
        self.detail_loaders = d
        for kind, row in self.loader_rows.items():
            items = d.get(kind) or []
            row.set_status(tr("可以添加") if items else tr("无"),
                           "idle" if items else "none")

    def _pick_loader(self, kind):
        label = {"forge": "Forge", "neoforge": "NeoForge",
                 "fabric": "Fabric", "optifine": "OptiFine"}.get(kind, kind)
        items = (self.detail_loaders or {}).get(kind) or []
        mc = self.detail_ver.get("id", "") if self.detail_ver else ""
        if not items:
            info_card(self, tr("提示"),
                      f"{label}{tr(' 没有适用于 ')}{mc}{tr(' 的版本')}")
            return
        names = [(e.get("name") if isinstance(e, dict) else str(e)) for e in items]
        dlg = _LoaderPickDialog(self, f"{label} · {mc}", names)
        if dlg.exec() != QDialog.Accepted or not dlg.value:
            return
        self.loader_choice = {kind: dlg.value}
        for k, row in self.loader_rows.items():
            if k == kind:
                row.set_status(dlg.value, "pick")
            else:
                its = (self.detail_loaders or {}).get(k) or []
                row.set_status(tr("可以添加") if its else tr("无"),
                               "idle" if its else "none")

    def start_download(self):
        v = self.detail_ver
        if not v:
            return
        if self.loader_choice:
            kind, ver = next(iter(self.loader_choice.items()))
            d = Api.post("/api/install_loader",
                         {"type": kind, "mc": v["id"], "ver": ver,
                          "mirror": self.mirror}).json()
        else:
            d = Api.post("/api/install",
                         {"version": v["id"], "url": v["url"],
                          "mirror": self.mirror}).json()
        if d.get("ok"):
            self.ver_stack.setCurrentIndex(0)
            info_card(self, tr("已开始下载"),
                      f"{v['id']}{tr(' 的安装任务已启动，进度见底部状态栏')}")
        else:
            info_card(self, tr("提示"), d.get("error", tr("下载启动失败")))

    # --- 资源中心（模组/整合包/数据包/资源包/光影包，Modrinth） ---
    def make_resource(self, rtype, label):
        c = theme_colors()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        st = {"type": rtype, "label": label, "results": [],
              "loaded": False, "loading": False,
              "mod": None, "sel_ver": None, "files": [],
              "cur_mc": "", "_ver_cache": {}}
        self.res[rtype] = st

        # 列表页 / 详情页 两个视图
        stack = QStackedWidget()
        lay.addWidget(stack, 1)
        list_page = QWidget()
        plain = QVBoxLayout(list_page)
        plain.setContentsMargins(0, 0, 0, 0)
        plain.setSpacing(10)

        # ---- 搜索卡片 ----
        card = QFrame()
        card.setObjectName("ResSearchCard")
        card.setStyleSheet(
            "#ResSearchCard { background: #ffffff; border: 1px solid "
            f"{c['line']}; border-radius: 14px; }}"
            "#ResSearchCard QLineEdit, #ResSearchCard QComboBox {"
            " background: #ffffff; border: 1px solid #dcd9d0; border-radius: 8px; }"
            f"#ResSearchCard QLineEdit:focus, #ResSearchCard QComboBox:focus"
            f" {{ border: 1px solid {c['accent']}; }}")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 15, 20, 15)
        cl.setSpacing(12)

        head = QLabel(f"{tr('搜索')} {label}")
        head.setStyleSheet(f"color: {c['accent_dark']}; font-size: 14px;"
                           " font-weight: bold;")
        cl.addWidget(head)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        q_edit = QLineEdit()
        q_edit.setPlaceholderText(tr("输入关键词，留空则按热门排序"))
        q_edit.returnPressed.connect(lambda: self.res_search(rtype))

        mc_edit = RoundCombo()
        mc_edit.setEditable(True)
        mc_edit.addItem(tr("全部（也可自行输入）"), "")
        for v in RES_MC_VERSIONS:
            mc_edit.addItem(v, v)
        mc_edit.lineEdit().setPlaceholderText(tr("全部（也可自行输入）"))

        src_combo = RoundCombo()
        src_combo.addItem(tr("全部"), "all")
        src_combo.addItem("Modrinth", "modrinth")
        src_combo.addItem("CurseForge", "cf")

        cat_combo = RoundCombo()
        cat_combo.addItem(tr("全部"), "")
        for key in RES_CATS.get(rtype, []):
            cat_combo.addItem(_cat_label(key), key)

        def field(text, widget, row, col):
            lab = QLabel(text)
            lab.setObjectName("FieldLabel")
            grid.addWidget(lab, row, col)
            grid.addWidget(widget, row, col + 1)

        field(tr("名称"), q_edit, 0, 0)
        field(tr("来源"), src_combo, 0, 2)
        field(tr("版本"), mc_edit, 1, 0)
        field(tr("类型"), cat_combo, 1, 2)
        grid.setColumnMinimumWidth(0, 42)
        grid.setColumnMinimumWidth(2, 42)
        grid.setColumnStretch(1, 11)
        grid.setColumnStretch(3, 4)
        cl.addLayout(grid)

        brow = QHBoxLayout()
        brow.setSpacing(10)
        search_btn = QPushButton(tr("搜索"))
        search_btn.setObjectName("SearchBtn")
        search_btn.setCursor(Qt.PointingHandCursor)
        search_btn.setMinimumWidth(170)
        search_btn.setFixedHeight(34)
        search_btn.clicked.connect(lambda: self.res_search(rtype))
        reset_btn = QPushButton(tr("重置条件"))
        reset_btn.setObjectName("GhostBtn")
        reset_btn.setCursor(Qt.PointingHandCursor)
        reset_btn.setMinimumWidth(170)
        reset_btn.setFixedHeight(34)
        reset_btn.clicked.connect(lambda: self.res_reset(rtype))
        brow.addWidget(search_btn)
        brow.addWidget(reset_btn)
        if rtype == "modpack":
            local_btn = QPushButton(tr("安装已有整合包"))
            local_btn.setObjectName("GhostBtn")
            local_btn.setCursor(Qt.PointingHandCursor)
            local_btn.setMinimumWidth(170)
            local_btn.setFixedHeight(34)
            local_btn.clicked.connect(self.install_local_modpack)
            brow.addWidget(local_btn)
        brow.addStretch(1)
        cl.addLayout(brow)
        plain.addWidget(card)

        # ---- 搜索结果（富列表） ----
        rl = QListWidget()
        rl.setObjectName("ResList")
        rl.setStyleSheet(
            "QListWidget#ResList { background: #ffffff; border: 1px solid "
            f"{c['line']}; border-radius: 14px; padding: 6px; }}"
            "QListWidget#ResList::item { border: none; border-radius: 10px; }"
            f"QListWidget#ResList::item:selected {{ background: {c['accent_soft']}; }}"
            "QListWidget#ResList::item:hover { background: #f6f8f7; }")
        rl.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        rl.itemClicked.connect(lambda item, k=rtype: self.res_open_detail(k, item))
        rl.itemDoubleClicked.connect(lambda item, k=rtype: self.res_open_detail(k, item))
        plain.addWidget(rl, 1)

        row = QHBoxLayout()
        st["hint"] = QLabel(tr("点一个资源查看它的全部文件版本；")
                            + (tr("整合包会自动安装原版/加载器并创建独立实例")
                               if rtype == "modpack" else
                               tr("数据包会装到游戏目录的 datapacks，进存档时手动启用")
                               if rtype == "datapack" else
                               tr("文件会装到当前选中版本的对应文件夹")))
        st["hint"].setObjectName("Hint")
        plain.addLayout(row)
        row.addWidget(st["hint"], 1)

        stack.addWidget(list_page)
        detail_page = QWidget()
        dl = QVBoxLayout(detail_page)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(0)
        stack.addWidget(detail_page)

        st.update(q_edit=q_edit, mc_edit=mc_edit, cat_combo=cat_combo,
                  src_combo=src_combo, result_list=rl,
                  stack=stack, detail_page=detail_page)
        return page

    def _res_query(self, rtype):
        st = self.res[rtype]
        mc = st["mc_edit"].currentText().strip()
        if mc.startswith(tr("全部")):
            mc = ""
        cat = st["cat_combo"].currentData() or ""
        src = st["src_combo"].currentData() or "all"
        if src == "all":          # 「全部」按 Modrinth 算（CurseForge 需要明确选它）
            src = "modrinth"
        return st, mc, cat, src

    def _res_ensure(self, rtype, prefetch=True):
        """该类型还没加载过就自动搜一次（空关键词 = 按下载量热门）"""
        st = self.res.get(rtype)
        if not st or st.get("loaded") or st.get("loading"):
            return
        self.res_search(rtype)
        if prefetch and not getattr(self, "_res_prefetched", False):
            self._res_prefetched = True
            for t in self.RES_ORDER:
                if t != rtype:
                    QTimer.singleShot(150, lambda k=t: self._res_ensure(k, False))

    def res_search(self, rtype):
        from urllib.parse import quote
        st, mc, cat, src = self._res_query(rtype)
        st["loading"] = True
        st["src"] = src
        q = st["q_edit"].text().strip()
        st["result_list"].clear()
        it = QListWidgetItem(tr("正在搜索…"))
        it.setFlags(Qt.NoItemFlags)
        st["result_list"].addItem(it)
        path = (f"/api/resource/search?type={rtype}&q={quote(q)}"
                f"&mc={quote(mc)}&category={quote(cat)}&src={src}")
        tok = (st.get("_tok") or 0) + 1
        st["_tok"] = tok
        st["_rt"] = ApiThread("GET", path, timeout=90)
        st["_rt"].done.connect(lambda d, k=rtype, t=tok: self.res_on_search(k, d, t))
        st["_rt"].start()

    def res_on_search(self, rtype, d, tok=None):
        st = self.res[rtype]
        if tok is not None and tok != st.get("_tok"):
            return          # 上一次搜索迟到的回包，丢掉，别把新结果盖掉
        st = self.res[rtype]
        st["loading"] = False
        rl = st["result_list"]
        rl.clear()
        if not d.get("ok"):
            if d.get("code") == "cf_no_key":
                guide = _CFKeyGuide(on_ok=lambda k=rtype: self.res_search(k))
                it = QListWidgetItem()
                it.setFlags(Qt.NoItemFlags)
                it.setSizeHint(QSize(0, max(178, guide.sizeHint().height())))
                rl.addItem(it)
                rl.setItemWidget(it, guide)
                return
            it = QListWidgetItem(f"{tr('✘ 搜索失败：')}{d.get('error', tr('未知错误'))}")
            it.setFlags(Qt.NoItemFlags)
            rl.addItem(it)
            return
        st["results"] = d.get("items", [])
        _src = d.get("source") or "modrinth"
        for h in st["results"]:
            h.setdefault("source", _src)
        st["loaded"] = bool(st["results"])
        if not st["results"]:
            it = QListWidgetItem(tr("（没有找到相关资源，换个关键词试试）"))
            it.setFlags(Qt.NoItemFlags)
            rl.addItem(it)
            return
        for h in st["results"]:
            it = QListWidgetItem()
            it.setData(Qt.UserRole, h)
            it.setSizeHint(QSize(0, 96))
            rl.addItem(it)
            rl.setItemWidget(it, _ResItem(h))
        self._load_icons(st["results"])

    # --- 图标异步加载（先占位色块，下好一张换一张） ---
    def _load_icons(self, items):
        urls = [h.get("icon") for h in items if h.get("icon")]
        todo = [u for u in dict.fromkeys(urls)
                if u not in self._icon_cache and u not in self._icon_busy]
        if not todo:
            self._apply_icons()
            return
        self._icon_busy.update(todo)

        def work():
            for u in todo:
                try:
                    r = requests.get(u, timeout=15,
                                     headers={"User-Agent": "AeroLauncher"})
                    if r.ok and r.content:
                        self._icon_sig.got.emit(u, r.content)
                except Exception:
                    pass
                finally:
                    self._icon_busy.discard(u)

        threading.Thread(target=work, daemon=True).start()

    def _on_icon(self, url, data):
        pm = QPixmap()
        if pm.loadFromData(data):
            self._icon_cache[url] = pm
            self._apply_icons()

    def _apply_icons(self):
        for st in self.res.values():
            rl = st.get("result_list")
            if rl:
                for i in range(rl.count()):
                    w = rl.itemWidget(rl.item(i))
                    if isinstance(w, _ResItem):
                        pm = self._icon_cache.get(w.icon_url)
                        if pm is not None:
                            w.set_icon(pm)
            ic = st.get("card_icon")
            mod = st.get("mod") or {}
            if ic is not None and mod.get("icon") in self._icon_cache:
                try:
                    ic.setPixmap(_round_icon(self._icon_cache[mod["icon"]], 64, 14))
                except RuntimeError:
                    pass

    def res_reset(self, rtype):
        st = self.res[rtype]
        st["q_edit"].clear()
        st["src_combo"].setCurrentIndex(0)
        st["src"] = "modrinth"
        st["mc_edit"].setCurrentIndex(0)
        st["cat_combo"].setCurrentIndex(0)
        st["result_list"].clear()
        st["mod"] = None
        st["sel_ver"] = None
        st["files"] = []
        st["cur_mc"] = ""
        st["stack"].setCurrentIndex(0)
        st["results"] = []
        st["loaded"] = False
        self._res_ensure(rtype, prefetch=False)

    def install_local_modpack(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("选择整合包文件"), "", tr("整合包 (*.mrpack *.zip)"))
        if not path:
            return
        name = os.path.splitext(os.path.basename(path))[0]
        d = Api.post("/api/resource/install_modpack_local",
                     {"path": path, "name": name}, timeout=30).json()
        if d.get("ok"):
            info_card(self, tr("开始安装"),
                      f"{tr('正在安装整合包：')}{name}{tr('，进度见底部提示')}")
        else:
            info_card(self, tr("提示"), d.get("error", tr("安装失败")))

    # --- 资源详情页：资源卡 + 版本筛选 + 文件分组 ---
    def res_open_detail(self, rtype, item):
        from urllib.parse import quote
        st = self.res[rtype]
        h = item.data(Qt.UserRole) if item else None
        if not h:
            return
        st["mod"] = h
        st["sel_ver"] = None
        st["files"] = []
        st["cur_mc"] = ""
        self._res_build_detail(rtype)
        st["stack"].setCurrentIndex(1)
        slug = h.get("slug", "")
        cached = st["_ver_cache"].get(slug)
        if cached is not None:
            self._res_render_versions(rtype, cached)
            return
        if (st.get("_rv_slug") == slug and st.get("_rv") is not None
                and st["_rv"].isRunning()):
            return
        st["_rv_slug"] = slug
        src = (st.get("mod") or {}).get("source") or st.get("src") or "modrinth"
        path = f"/api/resource/versions?slug={quote(slug)}&src={src}"
        st["_rv"] = ApiThread("GET", path, timeout=90)
        st["_rv"].done.connect(lambda d, k=rtype, sl=slug: self.res_on_versions(k, d, sl))
        st["_rv"].start()

    def _res_detail_style(self):
        c = theme_colors()
        return f"""
            QWidget#ResDetail {{ background: transparent; }}
            #ResModCard {{ background: #ffffff; border: 1px solid {c['line']};
                            border-radius: 14px; }}
            #ResModCard QLabel {{ background: transparent; border: none; }}
            #ResGroupCard {{ background: #ffffff; border: 1px solid {c['line']};
                             border-radius: 12px; }}
            #ResGroupCard QLabel {{ background: transparent; border: none; }}
            #ResGroupBar {{ background: transparent; }}
            #ResFileRow {{ background: transparent; border-radius: 9px; }}
            #ResFileRow:hover {{ background: #f6f8f7; }}
            #ResFileRow[sel="true"] {{ background: {c['accent_soft']}; }}
            #ResTabOn {{ background: {c['accent']}; color: #ffffff; border: none;
                         border-radius: 15px; padding: 5px 15px; font-size: 12px; }}
            #ResTabOff {{ background: transparent; color: {c['accent_dark']};
                          border: none; border-radius: 15px; padding: 5px 15px;
                          font-size: 12px; }}
            #ResTabOff:hover {{ background: {c['accent_soft']}; }}
            #AccentGhost {{ background: transparent; color: {c['accent_dark']};
                            border: 1px solid {c['accent']}; border-radius: 8px;
                            padding: 5px 16px; font-size: 12px; }}
            #AccentGhost:hover {{ background: {c['accent_soft']}; }}
        """

    def _mod_meta(self, h):
        parts = []
        lds = [RES_LOADER_NAME.get(x, x.title()) for x in (h.get("loaders") or [])][:2]
        vs = h.get("versions") or []
        vtext = (vs[0] + ("+" if len(vs) > 1 else "")) if vs else ""
        head = " ".join(x for x in [" / ".join(lds), vtext] if x)
        if head:
            parts.append(head)
        parts.append(f"↓ {_fmt_count(h.get('downloads'))}")
        if h.get("updated"):
            parts.append(f"↑ {_rel_time(h['updated'])}")
        parts.append("CurseForge" if h.get("source") == "cf" else "Modrinth")
        return "      ".join(parts)

    def _res_build_detail(self, rtype):
        from urllib.parse import quote
        c = theme_colors()
        st = self.res[rtype]
        h = st["mod"] or {}
        page = st["detail_page"]
        page.setObjectName("ResDetail")
        page.setStyleSheet(self._res_detail_style())
        lay = page.layout()
        _clear_layout(lay)
        lay.setSpacing(10)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        back = QPushButton(tr("‹ 返回列表"))
        back.setObjectName("GhostBtn")
        back.setCursor(Qt.PointingHandCursor)
        back.setFixedHeight(32)
        back.setMinimumWidth(110)
        back.clicked.connect(lambda: st["stack"].setCurrentIndex(0))
        top.addWidget(back)
        top.addStretch(1)
        lay.addLayout(top)

        # ---- 资源卡 ----
        card = QFrame()
        card.setObjectName("ResModCard")
        cl = QHBoxLayout(card)
        cl.setContentsMargins(18, 16, 18, 16)
        cl.setSpacing(16)

        icon = QLabel()
        icon.setFixedSize(64, 64)
        pm = self._icon_cache.get(h.get("icon", ""))
        icon.setPixmap(_round_icon(pm, 64, 14) if pm is not None else _ph_icon(h, 64))
        if pm is None and h.get("icon"):
            self._load_icons([h])
        st["card_icon"] = icon
        cl.addWidget(icon, 0, Qt.AlignTop)

        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)
        name = QLabel(h.get("title", ""))
        name.setStyleSheet(f"color:{TEXT}; font-size:17px; font-weight:bold;"
                           " background: transparent;")
        box.addWidget(name)
        desc = QLabel(h.get("desc", ""))
        desc.setStyleSheet(f"color:{SUB}; font-size:12px; background: transparent;")
        desc.setWordWrap(True)
        box.addWidget(desc)
        meta = QLabel(self._mod_meta(h))
        meta.setStyleSheet("color:#8a949c; font-size:11px; background: transparent;")
        box.addWidget(meta)

        btns = QHBoxLayout()
        btns.setContentsMargins(0, 4, 0, 0)
        btns.setSpacing(10)
        if h.get("source") == "cf":
            url = h.get("website") or \
                f"https://www.curseforge.com/minecraft/search?searchFilter={quote(h.get('title', ''))}"
            b1 = QPushButton(tr("转到 CurseForge"))
        else:
            url = f"https://modrinth.com/{rtype}/{h.get('slug', '')}"
            b1 = QPushButton(tr("转到 Modrinth"))
        b1.setObjectName("AccentGhost")
        b1.setCursor(Qt.PointingHandCursor)
        b1.setFixedHeight(32)
        b1.setMinimumWidth(150)
        b1.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        b2 = QPushButton(tr("转到 MC 百科"))
        b2.setObjectName("GhostBtn")
        b2.setCursor(Qt.PointingHandCursor)
        b2.setFixedHeight(32)
        b2.setMinimumWidth(150)
        b2.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl("https://search.mcmod.cn/s?key=" + h.get("title", ""))))
        b3 = QPushButton(tr("复制名称"))
        b3.setObjectName("GhostBtn")
        b3.setCursor(Qt.PointingHandCursor)
        b3.setFixedHeight(32)
        b3.setMinimumWidth(130)

        def copy_name():
            QApplication.clipboard().setText(h.get("title", ""))
            b3.setText(tr("已复制"))
            QTimer.singleShot(1200, lambda: b3.setText(tr("复制名称")))

        b3.clicked.connect(copy_name)
        for b in (b1, b2, b3):
            btns.addWidget(b)
        btns.addStretch(1)
        box.addLayout(btns)
        cl.addLayout(box, 1)
        lay.addWidget(card)

        # ---- 版本筛选 chips ----
        chip_bar = QWidget()
        chl = QHBoxLayout(chip_bar)
        chl.setContentsMargins(2, 0, 0, 0)
        chl.setSpacing(6)
        lay.addWidget(chip_bar)
        st["chip_lay"] = chl

        # ---- 文件分组滚动区 ----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }"
                             "QScrollArea > QWidget > QWidget { background: transparent; }")
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 8, 0)
        bl.setSpacing(8)
        loading = QLabel(tr("正在获取文件列表…"))
        loading.setObjectName("Hint")
        bl.addWidget(loading)
        bl.addStretch(1)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)

        bot = QHBoxLayout()
        st["detail_hint"] = QLabel(tr("点一行选中文件，双击直接下载"))
        st["detail_hint"].setObjectName("Hint")
        dl_btn = QPushButton(tr("安装整合包") if rtype == "modpack" else tr("下载选中项"))
        dl_btn.setObjectName("PrimaryBtn")
        dl_btn.setCursor(Qt.PointingHandCursor)
        dl_btn.clicked.connect(lambda: self.res_install(rtype))
        bot.addWidget(st["detail_hint"], 1)
        bot.addWidget(dl_btn)
        lay.addLayout(bot)

        st["dbody_lay"] = bl
        st["rows"] = []

    def _res_render_versions(self, rtype, vs):
        st = self.res[rtype]
        st["files"] = vs or []
        # 版本 chips
        chl = st["chip_lay"]
        _clear_layout(chl)
        vers = []
        for v in st["files"]:
            for g in (v.get("game_versions") or []):
                b = _mc_base(g)
                if b not in vers:
                    vers.append(b)
        vers.sort(key=_mc_sort_key, reverse=True)
        vers = vers[:12]
        st["tabs"] = {}

        def add_tab(text, val):
            b = QPushButton(text)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(30)
            b.setObjectName("ResTabOn" if val == st["cur_mc"] else "ResTabOff")
            b.clicked.connect(lambda _=False, x=val: self._res_filter_mc(rtype, x))
            chl.addWidget(b)
            st["tabs"][val] = b

        add_tab(tr("全部"), "")
        for gv in vers:
            add_tab(gv, gv)
        chl.addStretch(1)
        self._res_render_groups(rtype)

    def _res_filter_mc(self, rtype, mc):
        st = self.res[rtype]
        st["cur_mc"] = mc or ""
        for val, b in st.get("tabs", {}).items():
            b.setObjectName("ResTabOn" if val == (mc or "") else "ResTabOff")
            b.style().unpolish(b)
            b.style().polish(b)
        self._res_render_groups(rtype)

    def _res_render_groups(self, rtype):
        st = self.res[rtype]
        bl = st["dbody_lay"]
        _clear_layout(bl)
        st["rows"] = []
        sel_mc = st["cur_mc"]
        groups = {}
        for v in st["files"]:
            gvs = v.get("game_versions") or [""]
            mc = gvs[0] if gvs else ""
            if sel_mc and sel_mc not in [_mc_base(g) for g in gvs]:
                continue
            kind = "release" if (v.get("type") or "release") == "release" else "preview"
            lds = v.get("loaders") or []
            groups.setdefault((mc, kind, lds[0] if lds else ""), []).append(v)

        if not groups:
            lab = QLabel(tr("（没有匹配当前版本的文件，换个版本看看）"))
            lab.setObjectName("Hint")
            bl.addWidget(lab)
            bl.addStretch(1)
            return

        keys = list(groups)
        keys.sort(key=lambda k: (k[2], 0 if k[1] == "release" else 1))
        keys.sort(key=lambda k: _mc_sort_key(k[0]), reverse=True)

        for mc, kind, loader in keys:
            files = sorted(groups[(mc, kind, loader)],
                           key=lambda v: v.get("date_full") or v.get("date") or "",
                           reverse=True)
            lname = RES_LOADER_NAME.get(loader, loader.title()) if loader else ""
            title = " ".join(x for x in [lname, mc or tr("通用")] if x)
            if kind != "release":
                title += f" {tr('预览版')}"
            rows = []
            sel = st.get("sel_ver") or {}
            for v in files:
                r = _ResFileRow(v, on_pick=lambda vv, w, dbl=False, k=rtype:
                                self._res_pick(k, vv, w, dbl))
                if sel and v.get("id") and v.get("id") == sel.get("id"):
                    r.set_selected(True)
                rows.append(r)
                st["rows"].append(r)
            bl.addWidget(_ResGroupCard(title, rows, expanded=True))
        bl.addStretch(1)

    def _res_pick(self, rtype, v, row, dbl=False):
        st = self.res[rtype]
        st["sel_ver"] = v
        for r in st.get("rows", []):
            r.set_selected(r is row)
        hint = st.get("detail_hint")
        if hint is not None:
            hint.setText(f"{tr('已选：')}{v.get('name', '')}{tr('（')}"
                         f"{v.get('filename', '')}{tr('）')}")
        if dbl:
            self.res_install(rtype)

    def res_on_versions(self, rtype, d, slug=""):
        st = self.res[rtype]
        # 用户可能已经返回并打开了另一个资源，旧请求的结果直接丢掉
        if slug and slug != (st.get("mod") or {}).get("slug", ""):
            return
        if not d.get("ok"):
            bl = st["dbody_lay"]
            _clear_layout(bl)
            lab = QLabel(f"{tr('✘ 获取失败：')}{d.get('error', tr('未知错误'))}")
            lab.setObjectName("Hint")
            bl.addWidget(lab)
            bl.addStretch(1)
            return
        vs = d.get("versions", [])
        slug = (st.get("mod") or {}).get("slug", "")
        if slug:
            st["_ver_cache"][slug] = vs
        self._res_render_versions(rtype, vs)

    def res_install(self, rtype):
        st = self.res[rtype]
        v = st.get("sel_ver")
        if not v:
            info_card(self, tr("提示"), tr("请先点选一个文件版本"))
            return
        if not v.get("url"):
            info_card(self, tr("提示"), tr("该文件没有下载地址"))
            return
        if rtype == "modpack":
            name = (st.get("mod") or {}).get("title", "Modpack")
            d = Api.post("/api/resource/install_modpack",
                         {"url": v["url"], "filename": v["filename"], "name": name},
                         timeout=30).json()
        else:
            if not self.win.version:
                info_card(
                    self, tr("提示"), tr("请先在「游戏版本」页安装一个版本（或主页 ⌄ 选择版本），"
                                 "资源会装进该版本的对应文件夹"))
                return
            d = Api.post("/api/resource/install",
                         {"type": rtype, "url": v["url"], "filename": v["filename"],
                          "vid": self.win.version}, timeout=30).json()
        if not d.get("ok"):
            info_card(self, tr("提示"), d.get("error", tr("下载启动失败")))

    # --- 数据 ---
    def load_versions(self):
        self.latest_hint.setVisible(True)
        self.latest_hint.setText(tr("正在获取版本列表…"))
        th = ApiThread("GET", f"/api/versions?mirror={self.mirror}")
        th.done.connect(self.on_versions)
        self._bg(th)

    def refresh_marks(self):
        """已安装列表变化后，刷新最新版本卡与已展开分组的 ✔ 标记"""
        if not self.versions:
            return
        self._refresh_latest()
        for k, g in self.fold_groups.items():
            if g.list.isVisible():
                self._fill_fold(k)

    def on_versions(self, d):
        self.versions = d.get("versions", []) if d.get("ok") else []
        if not self.versions:
            self.latest_hint.setVisible(True)
            self.latest_hint.setText(
                f"{tr('✘ 加载失败：')}{d.get('error', tr('未知错误'))}")
            return
        self.latest_hint.setVisible(False)
        self._refresh_latest()
        for k, g in self.fold_groups.items():
            if g.list.isVisible():
                self._fill_fold(k)


class AccountsPage(QWidget):
    ok = Signal()

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.accounts = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 24, 32, 8)
        outer.setSpacing(12)

        title = QLabel(tr("账户"))
        title.setObjectName("Title")
        outer.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.list_host = QWidget()
        self.list_host.setObjectName("AccHost")
        self.list_host.setStyleSheet("#AccHost { background: transparent; }")
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(8)
        scroll.setWidget(self.list_host)
        outer.addWidget(scroll, 1)

        # 正版登录（微软）
        msa = QFrame()
        msa.setObjectName("Glass")
        ml = QHBoxLayout(msa)
        ml.setContentsMargins(16, 12, 16, 12)
        ml.setSpacing(8)
        m_lab = QLabel(tr("正版登录"))
        m_lab.setObjectName("FieldLabel")
        self.msa_btn = QPushButton(tr("微软账户登录"))
        self.msa_btn.setObjectName("PrimaryBtn")
        self.msa_btn.setCursor(Qt.PointingHandCursor)
        self.msa_btn.clicked.connect(self.msa_start)
        self.msa_state = QLabel("")
        self.msa_state.setObjectName("Hint")
        self.msa_state.setWordWrap(True)
        ml.addWidget(m_lab)
        ml.addWidget(self.msa_state, 1)
        ml.addWidget(self.msa_btn)
        outer.addWidget(msa)
        self._msa_timer = QTimer(self)
        self._msa_timer.setInterval(2000)
        self._msa_timer.timeout.connect(self.msa_poll)

        # 离线账户
        off = QFrame()
        off.setObjectName("Glass")
        ol = QHBoxLayout(off)
        ol.setContentsMargins(16, 12, 16, 12)
        ol.setSpacing(8)
        lab = QLabel(tr("离线登录"))
        lab.setObjectName("FieldLabel")
        self.offline_name = QLineEdit()
        self.offline_name.setPlaceholderText(tr("用户名（最多 16 字符）"))
        btn_off = QPushButton(tr("添加"))
        btn_off.setObjectName("GhostBtn")
        btn_off.setCursor(Qt.PointingHandCursor)
        btn_off.clicked.connect(self.add_offline)
        self.offline_name.returnPressed.connect(self.add_offline)
        ol.addWidget(lab)
        ol.addWidget(self.offline_name, 2)
        ol.addWidget(btn_off)
        outer.addWidget(off)

        # 第三方登录
        third = QFrame()
        third.setObjectName("Glass")
        tl = QVBoxLayout(third)
        tl.setContentsMargins(16, 14, 16, 14)
        tl.setSpacing(8)
        t_head = QHBoxLayout()
        t_title = QLabel(tr("第三方登录（authlib-injector 外置登录）"))
        t_title.setObjectName("RowName")
        self.server_state = QLabel("")
        self.server_state.setObjectName("Hint")
        t_head.addWidget(t_title)
        t_head.addStretch()
        t_head.addWidget(self.server_state)
        tl.addLayout(t_head)

        self.server_edit = QLineEdit()
        self.server_edit.setPlaceholderText(
            tr("认证服务器，如 https://littleskin.cn（也可填 .../api/yggdrasil）"))
        self.third_user = QLineEdit()
        self.third_user.setPlaceholderText(tr("邮箱 / 用户名"))
        self.third_pwd = QLineEdit()
        self.third_pwd.setPlaceholderText(tr("密码"))
        self.third_pwd.setEchoMode(QLineEdit.Password)
        tl.addWidget(self.server_edit)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.third_user, 1)
        row.addWidget(self.third_pwd, 1)
        self.third_btn = QPushButton(tr("登录并添加"))
        self.third_btn.setObjectName("PrimaryBtn")
        self.third_btn.setCursor(Qt.PointingHandCursor)
        self.third_btn.clicked.connect(self.add_third)
        self.third_pwd.returnPressed.connect(self.add_third)
        row.addWidget(self.third_btn)
        tl.addLayout(row)

        hint = QLabel(tr("支持 LittleSkin、Blessing Skin 等所有 Yggdrasil 协议外置登录服务器。"
                      "登录后启动游戏会自动注入 authlib-injector，进入相应服务器时使用该账户身份。"))
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        tl.addWidget(hint)
        outer.addWidget(third)

        self.refresh()

    def refresh(self):
        t = ApiThread("GET", "/api/accounts")
        t.done.connect(self.on_accounts)
        t.start()
        self._t = t

    def on_accounts(self, d):
        while self.list_lay.count():
            item = self.list_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.accounts = d.get("accounts", [])
        sel = d.get("selected")
        if not self.accounts:
            lab = QLabel(tr("还没有账户，请在下方添加离线账户或登录第三方账户。"))
            lab.setObjectName("Hint")
            self.list_lay.addWidget(lab)
        for acc in self.accounts:
            self.list_lay.addWidget(self.make_row(acc, acc.get("id") == sel))
        self.list_lay.addStretch()
        self.win.on_accounts_updated(self.accounts, sel)

    def make_row(self, acc, active):
        row = QFrame()
        row.setObjectName("Glass")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(10)

        name = acc.get("name", "")
        pm = avatar_pixmap(name, 34)
        if pm is not None:
            avatar = QLabel()
            avatar.setPixmap(pm)
            avatar.setFixedSize(34, 34)
        else:
            avatar = QLabel(name[:1].upper())
            avatar.setAlignment(Qt.AlignCenter)
            avatar.setStyleSheet(
                "background:#9aa5ad;color:white;"
                "border-radius:17px;font-weight:bold;font-size:15px;")
        avatar.setFixedSize(34, 34)
        lay.addWidget(avatar)

        box = QVBoxLayout()
        box.setSpacing(2)
        name = QLabel(acc.get("name", ""))
        name.setObjectName("RowName")
        if acc.get("type") == "authlib":
            host = acc.get("server", "").replace("https://", "").replace("http://", "")
            host = host.split("/")[0]
            sub_text = f"{tr('第三方 · ')}{host}"
        elif acc.get("type") == "msa":
            sub_text = tr("正版 · 微软账户")
        else:
            sub_text = tr("离线登录")
        sub = QLabel(sub_text)
        sub.setObjectName("RowSub")
        box.addWidget(name)
        box.addWidget(sub)
        lay.addLayout(box, 1)

        if active:
            badge = QLabel(tr("使用中"))
            badge.setObjectName("Badge")
            lay.addWidget(badge)
        else:
            use = QPushButton(tr("使用"))
            use.setObjectName("GhostBtn")
            use.setCursor(Qt.PointingHandCursor)
            use.clicked.connect(lambda _=False, i=acc.get("id"): self.select(i))
            lay.addWidget(use)

        dele = QPushButton(tr("删除"))
        dele.setObjectName("DangerBtn")
        dele.setCursor(Qt.PointingHandCursor)
        dele.clicked.connect(lambda _=False, i=acc.get("id"): self.delete(i))
        lay.addWidget(dele)
        return row

    def add_offline(self):
        name = self.offline_name.text().strip()
        if not name:
            return
        d = Api.post("/api/accounts/offline", {"name": name}).json()
        if not d.get("ok"):
            info_card(self, tr("提示"), d.get("error", tr("添加失败")))
            return
        self.offline_name.clear()
        self.refresh()

    # --- 微软正版登录（设备码流程） ---
    def msa_start(self):
        d = Api.post("/api/accounts/msa_start", {}).json()
        if not d.get("ok"):
            info_card(self, tr("提示"), d.get("error", tr("无法开始正版登录")))
            return
        self._msa_opened = False
        self.msa_btn.setEnabled(False)
        self.msa_state.setText(tr("正在获取登录代码…"))
        self._msa_timer.start()

    def msa_poll(self):
        try:
            d = Api.get("/api/accounts/msa_poll", timeout=8).json()
        except Exception:
            return
        phase = d.get("phase", "idle")
        if phase == "waiting":
            uri = d.get("verification_uri", "")
            self.msa_state.setText(
                f"{tr('请在浏览器打开 ')}{uri}{tr(' 并输入代码：')}"
                f"{d.get('user_code', '')}{tr('（登录你的微软账户）')}")
            if uri and not getattr(self, "_msa_opened", False):
                self._msa_opened = True
                try:
                    import webbrowser
                    webbrowser.open(uri)
                except Exception:
                    pass
        elif phase == "done":
            self._msa_timer.stop()
            self.msa_btn.setEnabled(True)
            self.msa_state.setText(f"{tr('正版登录成功：')}{d.get('name', '')}")
            self.refresh()
        elif phase == "error":
            self._msa_timer.stop()
            self.msa_btn.setEnabled(True)
            self.msa_state.setText(f"{tr('登录失败：')}{d.get('error', tr('未知错误'))}")

    def add_third(self):
        server = self.server_edit.text().strip()
        user = self.third_user.text().strip()
        pwd = self.third_pwd.text()
        if not (server and user and pwd):
            info_card(self, tr("提示"), tr("请填写服务器地址、邮箱/用户名和密码"))
            return
        self.third_btn.setEnabled(False)
        self.server_state.setText(tr("正在登录…"))
        t = ApiThread("POST", "/api/accounts/third",
                      {"server": server, "username": user, "password": pwd}, timeout=60)
        t.done.connect(self.on_third_done)
        t.start()
        self._tt = t

    def on_third_done(self, d):
        self.third_btn.setEnabled(True)
        if not d.get("ok"):
            self.server_state.setText("")
            warn_card(self, tr("登录失败"), d.get("error", tr("未知错误")))
            return
        self.third_pwd.clear()
        acc = d.get("account", {})
        self.server_state.setText(f"{tr('已登录：')}{acc.get('name', '')} @ {acc.get('server', '')}")
        self.refresh()

    def select(self, acc_id):
        Api.post("/api/accounts/select", {"id": acc_id})
        self.refresh()

    def delete(self, acc_id):
        Api.post("/api/accounts/delete", {"id": acc_id})
        self.refresh()


class SettingsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 24, 32, 8)
        lay.setSpacing(12)

        title = QLabel(tr("设置"))
        title.setObjectName("Title")
        lay.addWidget(title)

        # 设置项较多，内容超出窗口高度时用滚动区域承载（避免控件被压扁重叠）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        # 只让滚动区域自身与视口透明，不能用无选择器规则（会清空所有子控件底色）
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        host = QWidget()
        host.setObjectName("SetHost")
        hl = QVBoxLayout(host)
        hl.setContentsMargins(0, 0, 6, 8)
        hl.setSpacing(12)
        scroll.setWidget(host)
        lay.addWidget(scroll, 1)

        card = QFrame()
        card.setObjectName("Glass")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 16, 18, 16)
        cl.setSpacing(8)

        def field(label, w, tip=""):
            box = QVBoxLayout()
            box.setSpacing(5)
            lab = QLabel(label)
            lab.setObjectName("FieldLabel")
            box.addWidget(lab)
            box.addWidget(w)
            if tip:
                lab.setToolTip(tip)
                w.setToolTip(tip)
            return box

        self.java_edit = QLineEdit()
        self.java_edit.setPlaceholderText(tr("留空自动检测（按游戏版本自动下载 Java 8/17/21）"))
        self._java_need = 0
        self.ram_combo = RoundCombo()
        self.ram_combo.addItems(["1 GB", "2 GB", "4 GB", "8 GB", "12 GB", "16 GB"])

        g1 = QGridLayout()
        g1.setContentsMargins(0, 0, 0, 0)
        g1.setHorizontalSpacing(12)
        g1.setVerticalSpacing(6)
        g1.setColumnStretch(0, 3)
        g1.setColumnStretch(1, 1)
        g1.addLayout(field(tr("Java 路径"), self.java_edit), 0, 0)
        g1.addLayout(field(tr("游戏内存"), self.ram_combo), 0, 1)

        self.java_state = QLabel("")
        self.java_state.setObjectName("Hint")
        self.java_state.setWordWrap(True)
        g1.addWidget(self.java_state, 1, 0, 1, 2)
        cl.addLayout(g1)

        # ---- 自动检测到的 Java 列表 ----
        jh = QHBoxLayout()
        jh.setSpacing(8)
        self.java_title = QLabel(tr("自动检测到的 Java"))
        self.java_title.setObjectName("FieldLabel")
        jh.addWidget(self.java_title)
        jh.addStretch()
        self.java_rescan = QPushButton(tr("重新扫描"))
        self.java_rescan.setObjectName("GhostBtn")
        self.java_rescan.setCursor(Qt.PointingHandCursor)
        self.java_rescan.setToolTip(tr("重新扫描本机 Java（装了新版本后点这里）"))
        self.java_rescan.clicked.connect(lambda: self.load_java_list(force=True))
        jh.addWidget(self.java_rescan)
        cl.addLayout(jh)

        self.java_list_host = QWidget()
        self.java_list_box = QVBoxLayout(self.java_list_host)
        self.java_list_box.setContentsMargins(0, 0, 0, 0)
        self.java_list_box.setSpacing(6)
        cl.addWidget(self.java_list_host)

        dl_row = QHBoxLayout()
        dl_row.setSpacing(8)
        dl_tip = QLabel(tr("本机没有对应版本？现在下："))
        dl_tip.setObjectName("Hint")
        dl_row.addWidget(dl_tip)
        for _mj in (8, 17, 21):
            _b = QPushButton(f"Java {_mj}")
            _b.setObjectName("GhostBtn")
            _b.setCursor(Qt.PointingHandCursor)
            _b.setToolTip(f"{tr('下载 Java ')}{_mj}{tr(' 运行时到启动器目录（不动系统环境）')}")
            _b.clicked.connect(lambda _=False, mj=_mj: self.download_java(mj))
            dl_row.addWidget(_b)
        dl_row.addStretch()
        cl.addLayout(dl_row)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background: rgba(0,0,0,0.06); border: none;")
        cl.addWidget(divider)

        # ---- 更新：频道 / 更新源 / 检查更新 ----
        self.update_edit = QLineEdit()
        self.update_edit.setPlaceholderText(f"{tr('默认 ')}{DEFAULT_UPDATE_URL}")
        self.channel_combo = RoundCombo()
        self.channel_combo.addItems([tr("稳定版"), tr("内测版（抢先体验）")])
        self.channel_combo.setToolTip(tr("内测版会读取 beta.json，比稳定版更早收到测试更新"))
        b_check = QPushButton(tr("检查更新"))
        b_check.setObjectName("GhostBtn")
        b_check.setCursor(Qt.PointingHandCursor)
        b_check.clicked.connect(lambda: self.win.check_update(silent=False))

        u_row = QHBoxLayout()
        u_row.setSpacing(12)
        u_row.addLayout(field(tr("更新频道"), self.channel_combo), 1)
        u_row.addLayout(field(tr("更新源"), self.update_edit,
                              f"{tr('默认 ')}{DEFAULT_UPDATE_URL}"), 3)
        u_row.addWidget(b_check, 0, Qt.AlignBottom)
        cl.addLayout(u_row)

        # ---- 界面语言 / Language ----
        self.lang_combo = RoundCombo()
        self.lang_combo.addItem("简体中文", "zh")
        self.lang_combo.addItem("繁體中文", "zh_tw")
        self.lang_combo.addItem("English", "en")
        # 没存过设置时按当前实际语言显示，避免下拉和界面对不上
        _saved_lang = QSettings("WJH", "MCLauncher").value("language") or get_lang()
        _lang_idx = self.lang_combo.findData(_saved_lang)
        if _lang_idx >= 0:
            self.lang_combo.setCurrentIndex(_lang_idx)
        self.lang_combo.currentIndexChanged.connect(self.on_lang_changed)
        lang_row = QHBoxLayout()
        lang_row.setSpacing(12)
        lang_row.addLayout(field(tr("界面语言"), self.lang_combo,
                                 tr("切换界面显示语言，重启启动器后完全生效")), 1)
        lang_row.addStretch(2)
        cl.addLayout(lang_row)

        # ---- CurseForge API Key（资源中心选 CurseForge 来源时用） ----
        self.cf_edit = QLineEdit()
        self.cf_edit.setPlaceholderText(tr("粘贴 CurseForge API Key（选填）"))
        self.cf_edit.setEchoMode(QLineEdit.Password)
        b_cf_apply = QPushButton(tr("保存并验证"))
        b_cf_apply.setObjectName("GhostBtn")
        b_cf_apply.setCursor(Qt.PointingHandCursor)
        b_cf_apply.clicked.connect(self.save_cf_key)
        self.b_cf_apply = b_cf_apply
        b_cf_site = QPushButton(tr("去申请"))
        b_cf_site.setObjectName("GhostBtn")
        b_cf_site.setCursor(Qt.PointingHandCursor)
        b_cf_site.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(CF_KEY_PAGE)))
        cf_row = QHBoxLayout()
        cf_row.setSpacing(12)
        cf_row.addLayout(field(tr("CurseForge API Key"), self.cf_edit,
                               tr("资源中心选 CurseForge 搜索下载时需要。"
                                  "developer.curseforge.com 注册账号后免费申请")), 3)
        cf_row.addWidget(b_cf_apply, 0, Qt.AlignBottom)
        cf_row.addWidget(b_cf_site, 0, Qt.AlignBottom)
        cl.addLayout(cf_row)

        # 版本隔离
        self.isolate_cb = QCheckBox(tr("版本隔离 —— 每个版本独立存放存档 / 模组 / 资源包 / 配置（推荐，与 PCL2 一致）"))
        self.isolate_cb.setCursor(Qt.PointingHandCursor)
        self.isolate_cb.setStyleSheet("color:#465056; font-size:13px;")
        cl.addWidget(self.isolate_cb)
        iso_hint = QLabel(tr("开启后：versions/1.20.1/ 下会有独立的 saves、mods、options.txt 等；"
                          "关闭则所有版本共用 .minecraft 根目录。"))
        iso_hint.setObjectName("Hint")
        iso_hint.setWordWrap(True)
        cl.addWidget(iso_hint)

        # 跳过原版首次启动引导
        self.onboard_cb = QCheckBox(tr("跳过首次启动引导 —— 直接进游戏，语言跟随界面语言"))
        self.onboard_cb.setCursor(Qt.PointingHandCursor)
        self.onboard_cb.setStyleSheet("color:#465056; font-size:13px;")
        cl.addWidget(self.onboard_cb)
        ob_hint = QLabel(tr("原版第一次启动会先弹一个「Welcome to Minecraft!」的无障碍设置页，"
                            "而且默认英文。关掉这个开关就能看到它。"))
        ob_hint.setObjectName("Hint")
        ob_hint.setWordWrap(True)
        cl.addWidget(ob_hint)
        t_cfg = ApiThread("GET", "/api/settings")
        t_cfg.done.connect(self.on_settings)
        t_cfg.start()
        self._tcfg = t_cfg
        self.isolate_cb.toggled.connect(self.save_isolate)
        self.onboard_cb.toggled.connect(self.save_onboard)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        b_folder = QPushButton(tr("打开游戏目录"))
        b_folder.setObjectName("GhostBtn")
        b_folder.clicked.connect(lambda: Api.post("/api/open_folder"))
        b_log = QPushButton(tr("查看运行日志"))
        b_log.setObjectName("GhostBtn")
        b_log.clicked.connect(lambda: win.goto(4))
        b_quit = QPushButton(tr("退出启动器"))
        b_quit.setObjectName("GhostBtn")
        b_quit.clicked.connect(self.win.close)
        for b in (b_folder, b_log, b_quit):
            b.setCursor(Qt.PointingHandCursor)
            btns.addWidget(b)
        btns.addStretch()
        cl.addLayout(btns)
        hl.addWidget(card)

        # ---- 外观（自定义颜色 / 背景 / 透明度）----
        look = QFrame()
        look.setObjectName("Glass")
        ll = QVBoxLayout(look)
        ll.setContentsMargins(18, 16, 18, 16)
        ll.setSpacing(10)

        ltitle = QLabel(tr("外观"))
        ltitle.setObjectName("RowName")
        ll.addWidget(ltitle)
        lhint = QLabel(tr("自定义主题色 / 背景色 / 背景图片 / 动态壁纸 / 窗口透明度，改完立即生效并自动保存。"))
        lhint.setObjectName("Hint")
        lhint.setWordWrap(True)
        ll.addWidget(lhint)

        self.accent_btn = QPushButton()
        self.accent_btn.setFixedSize(52, 30)
        self.accent_btn.setCursor(Qt.PointingHandCursor)
        self.accent_btn.clicked.connect(self.pick_accent)
        self.bg_btn = QPushButton()
        self.bg_btn.setFixedSize(52, 30)
        self.bg_btn.setCursor(Qt.PointingHandCursor)
        self.bg_btn.clicked.connect(self.pick_bg)

        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(60, 100)
        self.opacity_slider.setSingleStep(1)
        self.opacity_slider.valueChanged.connect(self.on_opacity)
        self.opacity_val = QLabel("100%")
        self.opacity_val.setObjectName("Hint")
        self.opacity_val.setFixedWidth(40)
        self.opacity_val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        crow = QHBoxLayout()
        crow.setSpacing(14)
        crow.addLayout(field(tr("主题色"), self.accent_btn))
        crow.addLayout(field(tr("背景色"), self.bg_btn))
        op_box = QVBoxLayout()
        op_box.setSpacing(4)
        op_lab = QLabel(tr("窗口透明度"))
        op_lab.setObjectName("FieldLabel")
        op_box.addWidget(op_lab)
        op_row = QHBoxLayout()
        op_row.setSpacing(8)
        op_row.addWidget(self.opacity_slider, 1)
        op_row.addWidget(self.opacity_val)
        op_box.addLayout(op_row)
        crow.addLayout(op_box, 1)
        self.reset_theme_btn = QPushButton(tr("恢复默认"))
        self.reset_theme_btn.setObjectName("GhostBtn")
        self.reset_theme_btn.setCursor(Qt.PointingHandCursor)
        self.reset_theme_btn.clicked.connect(self.reset_theme)
        crow.addWidget(self.reset_theme_btn, 0, Qt.AlignBottom)
        ll.addLayout(crow)

        # 自定义背景图片：按钮行 + 状态行（状态单独占一行，避免挤压变形）
        self.img_btn = QPushButton(tr("选择图片…"))
        self.img_btn.setObjectName("GhostBtn")
        self.img_btn.setCursor(Qt.PointingHandCursor)
        self.img_btn.clicked.connect(self.pick_bg_image)
        self.img_clear_btn = QPushButton(tr("清除"))
        self.img_clear_btn.setObjectName("GhostBtn")
        self.img_clear_btn.setCursor(Qt.PointingHandCursor)
        self.img_clear_btn.clicked.connect(self.clear_bg_image)

        img_row = QHBoxLayout()
        img_row.setSpacing(10)
        img_row.addLayout(field(tr("背景图片"), self.img_btn))
        img_row.addWidget(self.img_clear_btn, 0, Qt.AlignBottom)
        img_row.addStretch()
        ll.addLayout(img_row)

        self.img_state = QLabel("")
        self.img_state.setObjectName("Hint")
        self.img_state.setWordWrap(True)
        ll.addWidget(self.img_state)

        # 动态壁纸：三种内置动效（背景图之上叠加，浅色压制保证可读）
        self.wp_combo = RoundCombo()
        for _k in WP_KINDS:
            self.wp_combo.addItem(tr(WP_LABELS[_k]), _k)
        _wp_idx = self.wp_combo.findData(str(THEME.get("wp") or "off"))
        self.wp_combo.setCurrentIndex(max(0, _wp_idx))
        self.wp_combo.currentIndexChanged.connect(self.on_wp_changed)
        wp_row = QHBoxLayout()
        wp_row.setSpacing(14)
        wp_row.addLayout(field(tr("动态壁纸"), self.wp_combo))
        wp_row.addStretch()
        ll.addLayout(wp_row)
        hl.addWidget(look)

        about = QFrame()
        about.setObjectName("Glass")
        al = QVBoxLayout(about)
        al.setContentsMargins(18, 14, 18, 14)
        al.setSpacing(4)
        info = QLabel(f"Aero Launcher v{APP_VERSION} · {backend.SYSTEM_NAME}{tr('\n游戏目录：')}{backend.MC_DIR}")
        info.setObjectName("Hint")
        info.setWordWrap(True)
        al.addWidget(info)
        hl.addWidget(about)
        hl.addStretch()

        self.settings = QSettings("WJH", "MCLauncher")
        self.ram_combo.setCurrentText(self.settings.value("ram", "2 GB"))
        self.java_edit.setText(self.settings.value("java", ""))
        self.update_edit.setText(self.settings.value("update/url", ""))
        self.update_edit.textChanged.connect(
            lambda t: self.settings.setValue("update/url", t))
        self.channel_combo.setCurrentIndex(
            1 if self.settings.value("update/channel", "stable") == "beta" else 0)
        self.channel_combo.currentIndexChanged.connect(
            lambda i: self.settings.setValue(
                "update/channel", "beta" if i == 1 else "stable"))
        self.ram_combo.currentTextChanged.connect(
            lambda t: self.settings.setValue("ram", t))
        self.java_edit.textChanged.connect(
            lambda t: self.settings.setValue("java", t))

        self.load_java_list()
        self.sync_theme_widgets()

    # ---------- 外观 ----------
    def _swatch(self, btn, color):
        btn.setStyleSheet(
            f"background: {color}; border: 1px solid rgba(0,0,0,0.16); border-radius: 10px;")

    def sync_theme_widgets(self):
        self._swatch(self.accent_btn, THEME["accent"])
        self._swatch(self.bg_btn, THEME["bg"])
        self.accent_btn.setToolTip(f"{tr('主题色 ')}{THEME['accent']}{tr('（点击修改）')}")
        self.bg_btn.setToolTip(f"{tr('背景色 ')}{THEME['bg']}{tr('（点击修改）')}")
        p = str(THEME.get("bg_image") or "")
        self.img_state.setText(
            (os.path.basename(p) if p else tr("未设置（将使用纯色背景）")))
        self.opacity_slider.blockSignals(True)
        self.opacity_slider.setValue(int(round(THEME["opacity"] * 100)))
        self.opacity_slider.blockSignals(False)
        self.opacity_val.setText(f"{int(round(THEME['opacity'] * 100))}%")
        _wp_i = self.wp_combo.findData(str(THEME.get("wp") or "off"))
        self.wp_combo.blockSignals(True)
        self.wp_combo.setCurrentIndex(max(0, _wp_i))
        self.wp_combo.blockSignals(False)

    def pick_accent(self):
        col = QColorDialog.getColor(QColor(THEME["accent"]), self, tr("选择主题色"))
        if col.isValid():
            self.win.apply_theme(accent=col.name())
            self.sync_theme_widgets()

    def pick_bg(self):
        col = QColorDialog.getColor(QColor(THEME["bg"]), self, tr("选择背景色"))
        if col.isValid():
            self.win.apply_theme(bg=col.name())
            self.sync_theme_widgets()

    def pick_bg_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("选择背景图片"), "",
            tr("图片文件 (*.png *.jpg *.jpeg *.bmp *.webp *.gif);;所有文件 (*)"))
        if not path:
            return
        # 拷贝一份到启动器目录，避免原文件移动/删除后背景失效
        try:
            ext = os.path.splitext(path)[1].lower() or ".png"
            dst = os.path.join(backend.BASE_DIR, ".minecraft", "launcher",
                               "bg_image" + ext)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(path, "rb") as fsrc, open(dst, "wb") as fdst:
                fdst.write(fsrc.read())
            path = dst
        except Exception:
            pass  # 拷贝失败则直接引用原路径
        self.win.apply_theme(bg_image=path)
        self.sync_theme_widgets()

    def clear_bg_image(self):
        self.win.apply_theme(bg_image="")
        self.sync_theme_widgets()

    def on_wp_changed(self, i):
        self.win.apply_theme(wp=self.wp_combo.itemData(i))

    def on_opacity(self, v):
        self.opacity_val.setText(f"{v}%")
        self.win.apply_theme(opacity=v / 100.0)

    def reset_theme(self):
        self.win.apply_theme(**DEFAULT_THEME)
        self.sync_theme_widgets()

    def on_settings(self, d):
        self.isolate_cb.blockSignals(True)
        self.isolate_cb.setChecked(bool(d.get("isolate", True)))
        self.isolate_cb.blockSignals(False)
        self.onboard_cb.blockSignals(True)
        self.onboard_cb.setChecked(bool(d.get("skip_onboarding", True)))
        self.onboard_cb.blockSignals(False)
        self.cf_edit.setText(d.get("cf_api_key") or "")

    def save_cf_key(self):
        key = self.cf_edit.text().strip()
        if not key:
            Api.post("/api/cf/key", {"key": ""})
            info_card(self, tr("已保存"), tr("已清空 CurseForge Key"))
            return
        self.b_cf_apply.setEnabled(False)
        self.b_cf_apply.setText(tr("验证中…"))
        t = ApiThread("POST", "/api/cf/key", {"key": key}, timeout=40)
        t.done.connect(self._cf_saved)
        t.start()
        self._tcf = t

    def _cf_saved(self, d):
        self.b_cf_apply.setEnabled(True)
        self.b_cf_apply.setText(tr("保存并验证"))
        if d.get("ok") and d.get("valid"):
            info_card(self, tr("验证通过"),
                      tr("这把 Key 可以用，已经保存。去资源中心把「来源」切成 CurseForge 就能搜了。"))
        else:
            warn_card(self, tr("Key 没通过校验"),
                      (d.get("error") or tr("未知错误"))
                      + "\n\n" + tr("确认复制完整、没有多余空格；刚申请的 Key 可能需要等几分钟审核。"))

    def save_isolate(self, checked):
        Api.post("/api/settings", {"isolate": bool(checked)})

    def save_onboard(self, checked):
        Api.post("/api/settings", {"skip_onboarding": bool(checked)})

    def on_lang_changed(self, i):
        lang = self.lang_combo.itemData(i)
        if not lang or lang == get_lang():
            return
        QSettings("WJH", "MCLauncher").setValue("language", lang)
        box = QMessageBox(self)
        box.setWindowTitle(tr("界面语言"))
        box.setIcon(QMessageBox.Question)
        box.setText(tr("界面语言已切换，需要重启启动器才会全部生效。"))
        box.setInformativeText(tr("现在重启吗？（如果服务器正在运行，会先停止服务器）"))
        yes = box.addButton(tr("现在重启"), QMessageBox.YesRole)
        box.addButton(tr("稍后，下次启动生效"), QMessageBox.NoRole)
        box.setDefaultButton(yes)
        box.exec()
        if box.clickedButton() is yes:
            self.window().restart_app()

    def load_java_list(self, force=False):
        """拉本机 Java 清单；顺手带上当前启动版本，好标出「这个版本会自动用哪个」"""
        from urllib.parse import quote
        vid = getattr(self.win, "version", "") or ""
        if force:
            self.java_title.setText(tr("自动检测到的 Java（扫描中…）"))
        t = ApiThread("GET", f"/api/java/list?vid={quote(vid)}"
                             + ("&force=1" if force else ""))
        t.done.connect(self.on_java_list)
        t.start()
        self._jt = t

    def _clear_java_rows(self):
        while self.java_list_box.count():
            it = self.java_list_box.takeAt(0)
            w = it.widget()
            if w:
                w.setParent(None)
                w.deleteLater()

    def on_java_list(self, d):
        d = d or {}
        if not d.get("ok", True):
            self.java_title.setText(tr("自动检测到的 Java"))
            self.java_state.setText(d.get("error") or tr("Java 扫描失败"))
            return
        items = d.get("javas") or []
        need = d.get("need") or 0
        picked = d.get("picked") or ""
        self._java_need = need
        vid = getattr(self.win, "version", "") or ""
        self.java_title.setText(f"{tr('自动检测到的 Java')}（{len(items)}）")

        self._clear_java_rows()
        if not items:
            lbl = QLabel(tr("没找到 Java。启动游戏时会按版本自动下载（Java 8 / 17 / 21），"
                            "也可以用下面的按钮现在下。"))
            lbl.setObjectName("Hint")
            lbl.setWordWrap(True)
            self.java_list_box.addWidget(lbl)
            self.java_state.setText(
                tr("未检测到 Java —— 留空即可，启动时自动下载所需版本"))
            return

        for it in items:
            self.java_list_box.addWidget(self._java_row(it, picked))

        if picked:
            info = next((x for x in items
                         if os.path.normcase(os.path.normpath(x["path"])) ==
                         os.path.normcase(os.path.normpath(picked))), {})
            tail = f" → {tr('自动选用')} Java {info.get('major')}（{info.get('vendor') or 'Java'}）" \
                if info else ""
            self.java_state.setText(
                f"{vid}{tr(' 需要 Java ')}{need}{tail}"
                f"{tr('。路径留空就是这套自动逻辑，填了就完全听你的。')}")
        else:
            self.java_state.setText(
                tr("启动时会按每个版本的要求自动挑 Java；一个都不满足就自动下载，"
                   "不用自己找路径。"))

    def _java_row(self, it, picked):
        path = it.get("path") or ""
        row = QFrame()
        row.setObjectName("JavaRow")
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 7, 10, 7)
        h.setSpacing(10)

        tag = QLabel(f"Java {it.get('major') or '?'}")
        tag.setObjectName("JavaTag")
        tag.setAlignment(Qt.AlignCenter)
        h.addWidget(tag)

        meta = QLabel(f"{it.get('vendor') or 'Java'}"
                      + (f" · {it.get('bits')} 位" if it.get("bits") else ""))
        meta.setObjectName("RowSub")
        meta.setFixedWidth(130)
        h.addWidget(meta)

        pl = QLabel(path)
        pl.setObjectName("Hint")
        pl.setToolTip(path)
        pl.setMinimumWidth(60)
        pl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        if self._java_need and not it.get("match"):
            pl.setStyleSheet("color: #b3aca3;")
            pl.setToolTip(f"{path}\n{tr('不适配当前版本的 Java 需求 ')}"
                          f"{self._java_need}")
        h.addWidget(pl, 1)

        _k = lambda p: os.path.normcase(os.path.normpath(p or ""))
        cur = self.java_edit.text().strip()
        if cur and _k(cur) == _k(path):
            used = QLabel(tr("使用中"))
            used.setObjectName("JavaAuto")
            h.addWidget(used)
        else:
            use = QPushButton(tr("使用"))
            use.setObjectName("GhostBtn")
            use.setCursor(Qt.PointingHandCursor)
            use.setFixedWidth(54)
            use.clicked.connect(lambda _=False, pth=path: self.use_java(pth))
            h.addWidget(use)

        if picked and _k(picked) == _k(path):
            auto = QLabel(tr("自动选用"))
            auto.setObjectName("JavaAuto")
            h.addWidget(auto)
        return row

    def use_java(self, path):
        """手动钉死某个 Java：写进设置，之后启动都用它（版本不符会明确报错）"""
        self.java_edit.setText(path)
        self.settings.setValue("java", path)
        self.load_java_list()

    def download_java(self, major):
        try:
            d = Api.post("/api/java/download", {"major": major}).json()
        except Exception as e:
            d = {"ok": False, "error": str(e)}
        if d.get("ok"):
            self.java_state.setText(
                f"{tr('已开始下载 Java ')}{major}{tr('，进度看右下角；装好会自动出现在列表里。')}")
            QTimer.singleShot(2500, lambda: self.load_java_list(force=True))
        else:
            self.java_state.setText(d.get("error") or tr("下载启动失败"))


LOADER_STYLE = {
    "原版": ("#5f5e5a", "#f1efe8", tr("原")),
    "Forge": ("#854f0b", "#faeeda", "FG"),
    "Fabric": ("#185fa5", "#e6f1fb", "FA"),
    "NeoForge": ("#993c1d", "#faece7", "NF"),
    "OptiFine": ("#173404", "#eaf3de", "OF"),
    "Quilt": ("#534ab7", "#eeedfe", "QT"),
}


def fmt_ago(ts):
    """上次启动时间的口语化显示"""
    try:
        ts = float(ts or 0)
    except Exception:
        ts = 0
    if ts <= 0:
        return tr("未启动过")
    d = time.time() - ts
    if d < 300:
        return tr("刚刚")
    if d < 3600:
        return f"{int(d // 60)}{tr(' 分钟前')}"
    if d < 86400:
        return f"{int(d // 3600)}{tr(' 小时前')}"
    if d < 86400 * 30:
        return f"{int(d // 86400)}{tr(' 天前')}"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def fmt_size(mb):
    mb = float(mb or 0)
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def _elide(s, n=40):
    """超长文件名截断加省略号（QLabel 不会自己省略）"""
    s = str(s or "")
    return s if len(s) <= n else s[:n - 1] + "…"


class VersionsPage(QWidget):
    """游戏版本：选启动版本 + 基础管理（打开目录 / 重命名 / 删除）"""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.items = []
        self.current = ""
        self.chip = "全部"     # 代码值（与后端 loader 字段一致），显示时才 tr
        self.q = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 24, 32, 8)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        tb = QVBoxLayout()
        tb.setSpacing(3)
        title = QLabel(tr("游戏版本"))
        title.setObjectName("Title")
        sub = QLabel(tr("版本来自 .minecraft/versions，选中的版本就是主页「启动」用的"))
        sub.setObjectName("Hint")
        tb.addWidget(title)
        tb.addWidget(sub)
        head.addLayout(tb, 1)
        self.btn_mp = QPushButton(tr("导入整合包…"))
        self.btn_mp.setObjectName("GhostBtn")
        self.btn_mp.setCursor(Qt.PointingHandCursor)
        self.btn_mp.setToolTip(
            tr("从本地文件导入整合包（.mrpack / .zip），装好就是一个新版本"))
        self.btn_mp.clicked.connect(self.import_modpack)
        self.btn_add = QPushButton(tr("＋ 添加版本"))
        self.btn_add.setObjectName("PrimaryBtn")
        self.btn_add.setCursor(Qt.PointingHandCursor)
        self.btn_add.clicked.connect(lambda: self.win.goto(1))
        self.btn_folder = QPushButton(tr("打开游戏目录"))
        self.btn_folder.setObjectName("GhostBtn")
        self.btn_folder.setCursor(Qt.PointingHandCursor)
        self.btn_folder.clicked.connect(self.open_mc_dir)
        self.btn_sync = QPushButton(tr("同步到 PCL2 / HMCL"))
        self.btn_sync.setObjectName("GhostBtn")
        self.btn_sync.setCursor(Qt.PointingHandCursor)
        self.btn_sync.setToolTip(
            tr("给每个版本补上 PCL2 和 HMCL 的配置，两家启动器打开同一个 .minecraft 就能直接认这些实例"))
        self.btn_sync.clicked.connect(self.sync_launchers)
        head.addWidget(self.btn_add)
        head.addWidget(self.btn_mp)
        head.addWidget(self.btn_sync)
        head.addWidget(self.btn_folder)
        lay.addLayout(head)

        card = QFrame()
        card.setObjectName("Glass")
        cl = QHBoxLayout(card)
        cl.setContentsMargins(16, 13, 16, 13)
        cl.setSpacing(12)
        self.cur_tile = QLabel()
        self.cur_tile.setFixedSize(44, 44)
        self.cur_tile.setAlignment(Qt.AlignCenter)
        self.cur_tile.setStyleSheet(
            "background: rgba(31,160,74,0.12); border-radius: 13px;")
        cl.addWidget(self.cur_tile)
        box = QVBoxLayout()
        box.setSpacing(4)
        line = QHBoxLayout()
        line.setSpacing(8)
        self.cur_name = QLabel(tr("还没有安装版本"))
        self.cur_name.setObjectName("RowName")
        self.cur_badge = QLabel(tr("当前使用"))
        self.cur_badge.setObjectName("Badge")
        line.addWidget(self.cur_name)
        line.addWidget(self.cur_badge)
        line.addStretch()
        box.addLayout(line)
        self.cur_sub = QLabel(tr("去「下载游戏」装一个，回来后这里就能选了"))
        self.cur_sub.setObjectName("RowSub")
        box.addWidget(self.cur_sub)
        cl.addLayout(box, 1)
        self.btn_launch = QPushButton(tr("启动"))
        self.btn_launch.setObjectName("PrimaryBtn")
        self.btn_launch.setFixedSize(78, 34)
        self.btn_launch.setCursor(Qt.PointingHandCursor)
        self.btn_launch.setToolTip(tr("用这个版本启动游戏"))
        self.btn_launch.clicked.connect(self.win.do_launch)
        cl.addWidget(self.btn_launch)
        lay.addWidget(card)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.chips = {}
        for name in ("全部", "原版", "Forge", "Fabric", "其他"):
            b = QPushButton(tr(name))
            b.setCursor(Qt.PointingHandCursor)
            b.setObjectName("ChipOn" if name == "全部" else "ChipOff")
            b.clicked.connect(lambda _=False, n=name: self.set_chip(n))
            bar.addWidget(b)
            self.chips[name] = b
        bar.addStretch()
        self.foot = QLabel("")
        self.foot.setObjectName("Hint")
        bar.addWidget(self.foot)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("搜版本号"))
        self.search.setFixedWidth(170)
        self.search.textChanged.connect(self.on_search)
        bar.addWidget(self.search)
        lay.addLayout(bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }")
        self.host = QWidget()
        self.rows = QVBoxLayout(self.host)
        self.rows.setContentsMargins(0, 0, 6, 2)
        self.rows.setSpacing(8)
        self.rows.addStretch()
        self.scroll.setWidget(self.host)
        lay.addWidget(self.scroll, 1)
        lay.addSpacing(2)

    # ---------- 数据 ----------
    def _get(self, path, timeout=60):
        t = ApiThread("GET", path, timeout=timeout)
        t.done.connect(self.on_detail)
        t.start()

    def reload(self):
        self._get("/api/versions_detail")

    def on_detail(self, d):
        if not isinstance(d, dict) or not d.get("ok"):
            self.foot.setText(tr("读取版本列表失败：") + str((d or {}).get("error", tr("后端无响应"))))
            return
        self.items = d.get("versions", [])
        self.current = d.get("selected") or ""
        self.render()

    def set_current(self, vid):
        self.current = vid
        self.render()

    def on_search(self, text):
        self.q = (text or "").strip().lower()
        self.render()

    def set_chip(self, name):
        self.chip = name
        for n, b in self.chips.items():
            b.setObjectName("ChipOn" if n == name else "ChipOff")
            b.style().unpolish(b)
            b.style().polish(b)
        self.render()

    def visible_items(self):
        out = []
        for it in self.items:
            if self.q and self.q not in it["id"].lower():
                continue
            ld = it.get("loader") or "原版"
            if self.chip == "原版" and ld != "原版":
                continue
            if self.chip == "Forge" and ld not in ("Forge", "NeoForge"):
                continue
            if self.chip == "Fabric" and ld not in ("Fabric", "Quilt"):
                continue
            if self.chip == "其他" and ld in ("原版", "Forge", "NeoForge", "Fabric", "Quilt"):
                continue
            out.append(it)
        return out

    # ---------- 渲染 ----------
    def render(self):
        while self.rows.count() > 1:
            it = self.rows.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        cur = next((x for x in self.items if x["id"] == self.current), None)
        if cur:
            self.cur_tile.setPixmap(diamond_pixmap(18, "#1fa04a"))
            self.cur_name.setText(cur["id"])
            self.cur_badge.show()
            self.btn_launch.show()
            bits = [cur.get("java") or tr("Java 自动"), fmt_size(cur.get("size_mb"))]
            if cur.get("vanilla") and cur.get("vanilla") != cur["id"]:
                bits.insert(0, tr("原版 ") + cur["vanilla"])
            if cur.get("mods"):
                bits.append(f"{cur['mods']}{tr(' 个模组')}")
            bits.append(tr("上次 ") + fmt_ago(cur.get("last_played"))
                        if cur.get("last_played") else tr("未启动过"))
            self.cur_sub.setText("　·　".join(bits))
        else:
            self.cur_tile.clear()
            self.cur_name.setText(tr("还没有安装版本"))
            self.cur_badge.hide()
            self.btn_launch.hide()
            self.cur_sub.setText(tr("去「下载游戏」装一个，回来后这里就能选了"))

        shown = self.visible_items()
        for it in shown:
            self.rows.insertWidget(self.rows.count() - 1, self.make_row(it))

        if not shown:
            tip = QLabel(tr("没有匹配的版本") if self.items else tr("还没有安装任何版本"))
            tip.setObjectName("Hint")
            tip.setAlignment(Qt.AlignCenter)
            tip.setMinimumHeight(80)
            self.rows.insertWidget(0, tip)

        total = sum(x.get("size_mb", 0) for x in self.items) / 1024
        self.foot.setText(f"{len(self.items)}{tr(' 个版本　·　')}{total:.1f} GB")

    def make_row(self, it):
        on = (it["id"] == self.current)
        row = QFrame()
        row.setObjectName("VerRowOn" if on else "VerRow")
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 9, 12, 9)
        h.setSpacing(10)

        fg, bg, ab = LOADER_STYLE.get(it.get("loader") or "原版", LOADER_STYLE["原版"])
        tile = QLabel(tr(ab))
        tile.setFixedSize(34, 34)
        tile.setAlignment(Qt.AlignCenter)
        tile.setStyleSheet(
            f"background: {bg}; color: {fg}; border-radius: 10px;"
            f" font-size: 11px; font-weight: bold;")
        h.addWidget(tile)

        info = QVBoxLayout()
        info.setSpacing(3)
        name = QLabel(it["id"])
        name.setObjectName("RowName")
        info.addWidget(name)
        bits = []
        if it.get("vanilla") and it.get("vanilla") != it["id"]:
            bits.append(tr("原版 ") + it["vanilla"])      # PCL2 那样：版本名下面写清本体是谁
        bits.append(it.get("java") or tr("Java 自动"))
        if it.get("mods"):
            bits.append(f"{it['mods']}{tr(' 个模组')}")
        bits.append(fmt_size(it.get("size_mb")))
        bits.append(tr("版本隔离") if it.get("isolated") else tr("共享目录"))
        bits.append(tr("上次 ") + fmt_ago(it.get("last_played"))
                    if it.get("last_played") else tr("未启动过"))
        meta = QLabel("　·　".join(bits))
        meta.setObjectName("RowSub")
        info.addWidget(meta)
        h.addLayout(info, 1)

        ld = it.get("loader") or "原版"
        lab = tr(ld) + (f" {it['loader_version']}" if it.get("loader_version") else "")
        badge = QLabel(lab)
        badge.setStyleSheet(
            f"color: {fg}; background: {bg}; border-radius: 7px;"
            f" padding: 2px 8px; font-size: 11px; font-weight: bold;")
        h.addWidget(badge)

        src = it.get("launchers") or []
        if src:
            tag = QLabel(" + ".join(src))
            tag.setStyleSheet(
                "color: #6d767d; background: #f1f2ee; border-radius: 7px;"
                " padding: 2px 7px; font-size: 11px;")
            tag.setToolTip(tr("这个版本目录里有它自己写的配置"))
            h.addWidget(tag)

        if on:
            mark = QLabel(tr("✓ 使用中"))
            mark.setObjectName("Badge")
            h.addWidget(mark)
        else:
            use = QPushButton(tr("选用"))
            use.setObjectName("ChipOff")
            use.setCursor(Qt.PointingHandCursor)
            use.setFixedHeight(27)
            use.setToolTip(tr("设为下次启动使用的版本"))
            use.clicked.connect(lambda _=False, v=it["id"]: self.select(v))
            h.addWidget(use)

        more = QPushButton("···")
        more.setObjectName("DotsBtn")
        more.setFixedSize(34, 27)
        more.setCursor(Qt.PointingHandCursor)
        more.clicked.connect(lambda _=False, v=it["id"], b=more: self.show_menu(v, b))
        h.addWidget(more)
        return row

    # ---------- 操作 ----------
    def select(self, vid, silent=False):
        try:
            r = Api.post("/api/version/select", {"version": vid}, timeout=15).json()
        except Exception as e:
            warn_card(self, tr("切换失败"), f"{tr('后端无响应：')}{e}")
            return False
        if not r.get("ok"):
            warn_card(self, tr("切换失败"), r.get("error") or tr("未知错误"))
            return False
        self.win.set_version(r.get("selected") or vid)
        if not silent:
            self.render()
        return True

    def launch(self, vid):
        if vid != self.current and not self.select(vid, silent=True):
            return
        self.win.do_launch()

    def import_modpack(self):
        """从本地文件导入整合包（.mrpack / .zip），后端装好后版本列表自动刷新"""
        path, _f = QFileDialog.getOpenFileName(
            self, tr("选择整合包文件"), "", tr("整合包 (*.mrpack *.zip)"))
        if not path:
            return
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            d = Api.post("/api/resource/install_modpack_local",
                         {"path": path, "name": name}, timeout=30).json()
        except Exception as e:
            warn_card(self, tr("导入失败"), f"{tr('后端无响应：')}{e}")
            return
        if d.get("ok"):
            info_card(self, tr("开始安装"),
                      f"{tr('正在安装整合包：')}{name}{tr('，进度见底部提示')}")
        else:
            info_card(self, tr("导入失败"), d.get("error", tr("未知错误")))

    def sync_launchers(self):
        """把已装版本补齐成 PCL2 / HMCL 也能直接用的样子（已有配置不覆盖）"""
        self.btn_sync.setEnabled(False)
        try:
            r = Api.post("/api/launcher_meta/sync", {}, timeout=90).json()
        except Exception as e:
            warn_card(self, tr("同步失败"), f"{tr('后端无响应：')}{e}")
            self.btn_sync.setEnabled(True)
            return
        self.btn_sync.setEnabled(True)
        if not r.get("ok"):
            warn_card(self, tr("同步失败"), r.get("error") or tr("未知错误"))
            return
        if r.get("written"):
            info_card(self, tr("同步完成"),
                      f"{r.get('written')}{tr(' 个版本已补齐 PCL2 / HMCL 配置')}")
        else:
            info_card(self, tr("同步完成"), tr("所有版本都已有配置，不用再补"))
        self.reload()

    def open_mc_dir(self):
        try:
            Api.post("/api/open_folder", {}, timeout=15)
        except Exception:
            pass

    def open_folder(self, vid):
        try:
            r = Api.post("/api/version/open", {"version": vid}, timeout=15).json()
        except Exception as e:
            warn_card(self, tr("打开失败"), f"{tr('后端无响应：')}{e}")
            return
        if not r.get("ok"):
            warn_card(self, tr("打开失败"), r.get("error") or tr("未知错误"))

    # ---------- Mod：导入 / 更新 ----------
    def import_mods(self, vid):
        paths, _f = QFileDialog.getOpenFileNames(
            self, tr("选择要导入的 Mod（可多选）"), "",
            f"{tr('Minecraft Mod')} (*.jar *.zip);;{tr('所有文件')} (*.*)")
        if not paths:
            return
        try:
            r = Api.post("/api/mod/import", {"version": vid, "paths": paths},
                         timeout=180).json()
        except Exception as e:
            warn_card(self, tr("导入失败"), f"{tr('后端无响应：')}{e}")
            return
        if not r.get("ok"):
            warn_card(self, tr("导入失败"), r.get("error") or tr("未知错误"))
            return
        imp = r.get("imported") or []
        skip = r.get("skipped") or []
        msg = f"{tr('已导入')}{len(imp)}{tr(' 个 Mod 到「')}{vid}{tr('」的 mods/ 文件夹')}"
        if imp:
            msg += "\n\n" + "\n".join("•  " + n for n in imp)
        if skip:
            msg += f"\n\n{tr('跳过')} {len(skip)} {tr('个')}：" + \
                   "；".join(f"{s['name']}（{s['reason']}）" for s in skip[:5])
        info_card(self, tr("导入 Mod"), msg)
        self.reload()

    def check_mod_updates(self, vid):
        ModUpdateDialog(self, vid).exec()

    def goto_mods(self, vid):
        """直接进这个版本的模组管理页（模组页会自动把版本切到它）"""
        self.win.open_mods(vid)

    def rename(self, vid):
        new, ok = QInputDialog.getText(self, tr("重命名版本"),
                                       f"{tr('给「')}{vid}{tr('」起个新名字：')}", text=vid)
        new = (new or "").strip()
        if not ok or not new or new == vid:
            return
        try:
            r = Api.post("/api/version/rename", {"old": vid, "new": new}, timeout=60).json()
        except Exception as e:
            warn_card(self, tr("改名失败"), f"{tr('后端无响应：')}{e}")
            return
        if not r.get("ok"):
            warn_card(self, tr("改名失败"), r.get("error") or tr("未知错误"))
            return
        if vid == self.current:
            self.win.set_version(r.get("version") or new)
        self.win.refresh_installed()
        self.reload()

    def delete(self, vid):
        extra = ""
        if any(x.get("base") == vid for x in self.items):
            extra = tr("\n\n注意：有别的版本依赖它，删除会失败。")
        if QMessageBox.question(
                self, tr("删除版本"),
                f"{tr('确定删除「')}{vid}{tr('」？\n\n这个版本目录里的存档、模组、配置会一起删掉，无法恢复。')}{extra}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            r = Api.post("/api/delete_version", {"version": vid}, timeout=120).json()
        except Exception as e:
            warn_card(self, tr("删除失败"), f"{tr('后端无响应：')}{e}")
            return
        if not r.get("ok"):
            warn_card(self, tr("删除失败"), r.get("error") or tr("未知错误"))
            return
        self.win.refresh_installed()
        self.reload()

    def show_menu(self, vid, btn):
        m = round_menu(self)
        if vid == self.current:
            m.addAction(tr("立即启动"), lambda: self.launch(vid))
        else:
            m.addAction(tr("设为启动版本"), lambda: self.select(vid))
            m.addAction(tr("立即启动"), lambda: self.launch(vid))
        m.addSeparator()
        m.addAction(tr("打开游戏目录"), lambda: self.open_folder(vid))
        m.addAction(tr("模组管理…"), lambda: self.goto_mods(vid))
        m.addAction(tr("导入 Mod…"), lambda: self.import_mods(vid))
        m.addAction(tr("检查 Mod 更新"), lambda: self.check_mod_updates(vid))
        m.addSeparator()
        m.addAction(tr("重命名…"), lambda: self.rename(vid))
        m.addSeparator()
        m.addAction(tr("删除版本"), lambda: self.delete(vid))
        m.exec(btn.mapToGlobal(QPoint(0, btn.height() + 4)))


class _NewsRow(QFrame):
    """最近版本的一行：整行可点，点开跳系统浏览器看更新日志"""

    clicked = Signal()

    def __init__(self, item):
        super().__init__()
        self.setObjectName("NewsRow")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(34)
        self.setToolTip(item.get("changelog") or "")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 7, 8, 7)
        lay.setSpacing(10)
        name = QLabel(item.get("id") or "")
        name.setObjectName("RowName")
        lay.addWidget(name)
        kind = QLabel(tr("正式版") if item.get("type") == "release" else tr("快照"))
        kind.setObjectName("Badge")
        lay.addWidget(kind)
        lay.addStretch()
        date = QLabel(item.get("date") or "")
        date.setObjectName("RowSub")
        lay.addWidget(date)
        tip = QLabel(tr("更新日志 ›"))
        tip.setObjectName("Hint")
        lay.addWidget(tip)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class NewsPage(QWidget):
    """资讯：Minecraft 官方版本动态 + 官方频道 / 正版购买入口（一律跳系统浏览器）"""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self._data = None
        self._links = {}
        self._loading = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 24, 32, 8)
        lay.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(10)
        tb = QVBoxLayout()
        tb.setSpacing(3)
        title = QLabel(tr("资讯"))
        title.setObjectName("Title")
        sub = QLabel(tr("Minecraft 官方动态：最新版本、更新说明，以及官方频道和正版购买入口"))
        sub.setObjectName("Hint")
        tb.addWidget(title)
        tb.addWidget(sub)
        head.addLayout(tb, 1)
        self.btn_site = QPushButton(tr("打开官网"))
        self.btn_site.setObjectName("GhostBtn")
        self.btn_site.setCursor(Qt.PointingHandCursor)
        self.btn_site.clicked.connect(lambda: self._open(self._links.get("home")))
        self.btn_refresh = QPushButton(tr("刷新"))
        self.btn_refresh.setObjectName("GhostBtn")
        self.btn_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_refresh.clicked.connect(lambda: self.refresh(force=True))
        head.addWidget(self.btn_site)
        head.addWidget(self.btn_refresh)
        lay.addLayout(head)

        # 最新正式版 / 最新快照
        row = QHBoxLayout()
        row.setSpacing(12)
        self.card_release = self._version_card(tr("最新正式版"), "release", primary=True)
        self.card_snapshot = self._version_card(tr("最新快照"), "snapshot")
        row.addWidget(self.card_release["frame"], 1)
        row.addWidget(self.card_snapshot["frame"], 1)
        lay.addLayout(row)

        # 最近版本列表
        list_card = QFrame()
        list_card.setObjectName("Glass")
        lc = QVBoxLayout(list_card)
        lc.setContentsMargins(14, 12, 14, 10)
        lc.setSpacing(2)
        lh = QHBoxLayout()
        lh.setSpacing(8)
        lt = QLabel(tr("最近版本"))
        lt.setObjectName("RowName")
        lh.addWidget(lt)
        lh.addStretch()
        self.btn_all = QPushButton(tr("在官网查看全部"))
        self.btn_all.setObjectName("LinkBtn")
        self.btn_all.setCursor(Qt.PointingHandCursor)
        self.btn_all.clicked.connect(lambda: self._open(self._links.get("download")))
        lh.addWidget(self.btn_all)
        lc.addLayout(lh)
        self.rows_box = QVBoxLayout()
        self.rows_box.setContentsMargins(0, 0, 0, 0)
        self.rows_box.setSpacing(0)
        self.empty_label = QLabel(tr("正在获取官方版本信息…"))
        self.empty_label.setObjectName("Hint")
        self.rows_box.addWidget(self.empty_label)
        self.rows_host = QWidget()
        self.rows_host.setLayout(self.rows_box)
        self.rows_scroll = QScrollArea()
        self.rows_scroll.setWidgetResizable(True)
        self.rows_scroll.setFrameShape(QFrame.NoFrame)
        self.rows_scroll.setMinimumHeight(150)
        self.rows_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { width: 8px; background: transparent; }"
            "QScrollBar::handle:vertical { background: rgba(0,0,0,0.16); border-radius: 4px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }")
        self.rows_scroll.setWidget(self.rows_host)
        lc.addWidget(self.rows_scroll, 1)
        lay.addWidget(list_card, 1)

        # 官方影片 / 购买正版
        bot = QHBoxLayout()
        bot.setSpacing(12)
        bot.addWidget(self._link_card(
            tr("官方影片"),
            tr("Mojang 官方频道发布的最新预告与更新影片，点击后用浏览器观看"),
            tr("打开官方频道"), "channel"), 1)
        bot.addWidget(self._link_card(
            tr("购买正版"),
            tr("支持开发者，获得正版皮肤、官方服务器与多人联机；国际版 Java & 基岩版在官网购买"),
            tr("前往官网购买"), "store", primary=True), 1)
        lay.addLayout(bot)

        self.status = QLabel("")
        self.status.setObjectName("Hint")
        lay.addWidget(self.status)
        lay.addStretch()

        self.refresh()

    # ---------- 组件 ----------
    def _version_card(self, title, kind, primary=False):
        card = QFrame()
        card.setObjectName("Glass")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 15, 18, 15)
        v.setSpacing(3)
        t = QLabel(title)
        t.setObjectName("Hint")
        v.addWidget(t)
        name = QLabel("—")
        name.setObjectName("NewsBig")
        v.addWidget(name)
        date = QLabel("")
        date.setObjectName("RowSub")
        v.addWidget(date)
        v.addSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
        btn_log = QPushButton(tr("更新日志"))
        btn_log.setObjectName("PrimaryBtn" if primary else "GhostBtn")
        btn_log.setCursor(Qt.PointingHandCursor)
        btn_log.clicked.connect(lambda: self._open_card(kind, "changelog"))
        btn_dl = QPushButton(tr("立即下载"))
        btn_dl.setObjectName("GhostBtn")
        btn_dl.setCursor(Qt.PointingHandCursor)
        btn_dl.clicked.connect(lambda: self._open(self._links.get("download")))
        row.addWidget(btn_log)
        row.addWidget(btn_dl)
        row.addStretch()
        v.addLayout(row)
        return {"frame": card, "name": name, "date": date}

    def _link_card(self, title, sub, btn_text, key, primary=False):
        card = QFrame()
        card.setObjectName("Glass")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(4)
        t = QLabel(title)
        t.setObjectName("RowName")
        v.addWidget(t)
        s = QLabel(sub)
        s.setObjectName("Hint")
        s.setWordWrap(True)
        v.addWidget(s)
        v.addSpacing(4)
        b = QPushButton(btn_text)
        b.setObjectName("PrimaryBtn" if primary else "GhostBtn")
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(lambda: self._open(self._links.get(key)))
        r = QHBoxLayout()
        r.addWidget(b)
        r.addStretch()
        v.addLayout(r)
        return card

    # ---------- 数据 ----------
    def refresh(self, force=False):
        import time as _t
        if self._loading:
            return
        if not force and self._data and _t.time() - self._data.get("_ts", 0) < 600:
            return
        self._loading = True
        self.status.setText(tr("正在获取官方版本信息…"))
        th = ApiThread("GET", "/api/news" + ("?force=1" if force else ""), timeout=30)
        th.done.connect(self._on_data)
        th.start()

    def _on_data(self, d):
        self._loading = False
        if not isinstance(d, dict):
            self.status.setText(tr("获取失败，可以点「刷新」重试"))
            return
        if not d.get("ok"):
            msg = d.get("error") or ""
            self.status.setText(f"{tr('获取失败，可以点「刷新」重试')}{(' · ' + msg) if msg else ''}")
            return
        d["_ts"] = time.time()
        self._data = d
        self._links = d.get("links") or {}
        latest = d.get("latest") or {}
        for kind, card in (("release", self.card_release), ("snapshot", self.card_snapshot)):
            info = latest.get(kind) or {}
            card["name"].setText(info.get("id") or "—")
            date = info.get("date") or ""
            card["date"].setText(f"{tr('发布于 ')}{date}" if date else "")
        # 版本列表重建
        while self.rows_box.count():
            it = self.rows_box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        items = d.get("versions") or []
        if not items:
            lab = QLabel(tr("暂时拿不到版本列表，稍后再试"))
            lab.setObjectName("Hint")
            self.rows_box.addWidget(lab)
        for it in items:
            r = _NewsRow(it)
            r.clicked.connect(lambda u=it.get("changelog"): self._open(u))
            self.rows_box.addWidget(r)
        if d.get("stale"):
            self.status.setText(tr("网络不可用，下面是上次缓存的数据"))
        else:
            self.status.setText("")

    # ---------- 跳浏览器 ----------
    def _open(self, url):
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _open_card(self, kind, field):
        latest = (self._data or {}).get("latest") or {}
        self._open((latest.get(kind) or {}).get(field))


# ---------------- 模组管理 ----------------
MOD_QSS = """
#ModCard {{ background: rgba(255,255,255,0.80); border: 1px solid rgba(255,255,255,0.9);
            border-radius: 18px; }}
#ModRow {{ background: transparent; border: 1px solid transparent; border-radius: 10px; }}
#ModRow:hover {{ background: rgba(255,255,255,0.95); border: 1px solid {line_soft}; }}
#ModName {{ color: #22282d; font-size: 13px; font-weight: bold; background: transparent; }}
#ModFile {{ color: #99a2a9; font-size: 11px; background: transparent; }}
#ModDesc {{ color: #7d868d; font-size: 11px; background: transparent; }}
#ModTag {{ color: #2b6ea8; background: rgba(43,110,168,0.10); border-radius: 6px;
          padding: 1px 6px; font-size: 10px; }}
#ModTagOff {{ color: #9a6a4e; background: rgba(154,106,78,0.12); border-radius: 6px;
             padding: 1px 6px; font-size: 10px; }}
#ModAct {{ background: transparent; border: none; border-radius: 7px; }}
#ModAct:hover {{ background: rgba(0,0,0,0.06); }}
#ModActDel {{ background: transparent; border: none; border-radius: 7px; }}
#ModActDel:hover {{ background: rgba(200,80,63,0.13); }}
#ModUpd {{ color: #1a7f4b; background: rgba(31,160,74,0.13); border: none; border-radius: 7px;
          padding: 1px 7px; font-size: 10px; font-weight: bold; }}
#ModUpd:hover {{ background: rgba(31,160,74,0.24); }}
#ModTabOn {{ background: {accent}; color: #ffffff; border: none; border-radius: 9px;
            padding: 5px 14px; font-size: 12px; font-weight: bold; }}
#ModTabOff {{ background: transparent; color: #5d676e; border: none; border-radius: 9px;
             padding: 5px 14px; font-size: 12px; }}
#ModTabOff:hover {{ background: rgba(0,0,0,0.045); }}
#ModBatch {{ background: rgba(255,255,255,0.96); border: 1px solid {line}; border-radius: 12px; }}
#ModBtn {{ background: rgba(255,255,255,0.86); color: #465056; border: 1px solid {line};
          border-radius: 10px; padding: 8px 15px; font-size: 12.5px; }}
#ModBtn:hover {{ background: #ffffff; }}
#ModBtnOn {{ background: {accent_soft}; color: {accent_dark}; border: 1px solid {accent_line};
            border-radius: 10px; padding: 8px 15px; font-size: 12.5px; }}
#ModBtnDanger {{ background: rgba(255,255,255,0.86); color: #c8503f; border: 1px solid {line};
                border-radius: 10px; padding: 8px 15px; font-size: 12.5px; }}
#ModBtnDanger:hover {{ background: #fdf3f1; border-color: #e7b9b1; }}
#ModSearch {{ background: rgba(255,255,255,0.92); border: 1px solid {line}; border-radius: 12px;
             padding: 10px 14px; font-size: 13px; }}
#ModSearch:focus {{ border: 1px solid {accent}; background: #ffffff; }}
#ModBack {{ background: rgba(255,255,255,0.86); border: 1px solid {line}; border-radius: 15px; }}
#ModBack:hover {{ background: #ffffff; border-color: {accent_line}; }}
"""


class _ElideLabel(QLabel):
    """按当前宽度自动加省略号的单行灰字"""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = text or ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._apply()

    def set_full(self, text):
        self._full = text or ""
        self._apply()

    def _apply(self):
        fm = self.fontMetrics()
        avail = max(24, self.width() - 2)
        super().setText(fm.elidedText(self._full, Qt.ElideRight, avail))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply()


def _round_pixmap(pm, size=36, radius=9):
    """把 Mod 图标裁成圆角小方块"""
    dpr = 2
    side = size * dpr
    src = pm.scaled(side, side, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    out = QPixmap(side, side)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, side, side), radius * dpr, radius * dpr)
    p.setClipPath(clip)
    p.drawPixmap(int((side - src.width()) / 2), int((side - src.height()) / 2), src)
    p.end()
    out.setDevicePixelRatio(dpr)
    return out


def _tile_color(seed):
    """没有图标的 Mod 用首字母色块兜底，颜色由名字决定（同一个 Mod 永远同色）"""
    palette = ["#5b8def", "#27ae60", "#e08a3c", "#9b59b6", "#16a085",
               "#e05c5c", "#3f7d9c", "#8a7d3c"]
    return palette[sum(ord(c) for c in str(seed or "?")) % len(palette)]


class _ModIconThread(QThread):
    """本地 HTTP 取图标（走 QThread，别卡界面）"""

    got = Signal(str, bytes)

    def __init__(self, vid, files):
        super().__init__()
        self.vid = vid
        self.files = list(files)

    def run(self):
        from urllib.parse import quote
        for f in self.files:
            try:
                r = Api.get(f"/api/mods/icon?version={quote(self.vid)}&file={quote(f)}",
                            timeout=15)
                if r.status_code == 200 and r.content:
                    self.got.emit(f, r.content)
            except Exception:
                continue


class _ModRow(QFrame):
    """Mod 列表的一行：图标 + 名称 / 文件 / 标签 / 描述，鼠标移上去右边冒出操作按钮"""

    del_clicked = Signal(str)
    toggle_clicked = Signal(str, bool)
    info_clicked = Signal(str)
    folder_clicked = Signal(str)
    upd_clicked = Signal(str)
    check_changed = Signal()

    ACT_W = 4 * 26 + 3 * 2

    def __init__(self, item, vid):
        super().__init__()
        self.item = dict(item)
        self.vid = vid
        self._hov = None
        self.setObjectName("ModRow")
        self.setFixedHeight(54)

        h = QHBoxLayout(self)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(9)

        # 勾选框外面套一层固定宽度容器：悬停时出现也不挤动右边的文字
        self.cbwrap = QWidget()
        self.cbwrap.setFixedWidth(16)
        cbw = QVBoxLayout(self.cbwrap)
        cbw.setContentsMargins(0, 0, 0, 0)
        self.cb = QCheckBox()
        self.cb.hide()
        self.cb.toggled.connect(lambda _=False: self.check_changed.emit())
        cbw.addWidget(self.cb, 0, Qt.AlignVCenter | Qt.AlignLeft)
        h.addWidget(self.cbwrap, 0, Qt.AlignVCenter)

        self.tile = QLabel()
        self.tile.setFixedSize(36, 36)
        self.tile.setAlignment(Qt.AlignCenter)
        h.addWidget(self.tile, 0, Qt.AlignVCenter)

        mid = QVBoxLayout()
        mid.setSpacing(1)
        mid.setContentsMargins(0, 0, 0, 0)

        line1 = QHBoxLayout()
        line1.setSpacing(8)
        line1.setContentsMargins(0, 0, 0, 0)
        self.lb_name = QLabel()
        self.lb_name.setObjectName("ModName")
        line1.addWidget(self.lb_name, 0)
        self.lb_file = QLabel()
        self.lb_file.setObjectName("ModFile")
        line1.addWidget(self.lb_file, 0)
        self.lb_ver = QLabel()
        self.lb_ver.setObjectName("ModFile")
        line1.addWidget(self.lb_ver, 0)
        self.btn_upd = QPushButton()
        self.btn_upd.setObjectName("ModUpd")
        self.btn_upd.setCursor(Qt.PointingHandCursor)
        self.btn_upd.setFixedHeight(19)
        self.btn_upd.clicked.connect(lambda: self.upd_clicked.emit(self.item["file"]))
        self.btn_upd.hide()
        line1.addWidget(self.btn_upd, 0)
        line1.addStretch(1)
        mid.addLayout(line1)

        line2 = QHBoxLayout()
        line2.setSpacing(6)
        line2.setContentsMargins(0, 0, 0, 0)
        self.tag_box = QHBoxLayout()
        self.tag_box.setSpacing(4)
        self.tag_box.setContentsMargins(0, 0, 0, 0)
        line2.addLayout(self.tag_box, 0)
        self.lb_desc = _ElideLabel(item.get("desc") or "")
        self.lb_desc.setObjectName("ModDesc")
        line2.addWidget(self.lb_desc, 1)
        mid.addLayout(line2)
        h.addLayout(mid, 1)

        # 操作区固定占位，避免 hover 时文字左右乱跳
        self.acts = QWidget()
        self.acts.setFixedWidth(self.ACT_W)
        ah = QHBoxLayout(self.acts)
        ah.setContentsMargins(0, 0, 0, 0)
        ah.setSpacing(2)

        def mk(icon, tip, slot, obj="ModAct"):
            b = QPushButton()
            b.setObjectName(obj)
            b.setFixedSize(26, 26)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.setIcon(QIcon(make_icon(icon, 15, "#5d676e", 1.7)))
            b.setIconSize(QSize(15, 15))
            b.clicked.connect(slot)
            b.hide()
            ah.addWidget(b)
            return b

        self.btn_info = mk("info", tr("详情"),
                           lambda: self.info_clicked.emit(self.item["file"]))
        self.btn_folder = mk("folder", tr("打开 mods 文件夹"),
                             lambda: self.folder_clicked.emit(self.item["file"]))
        self.btn_toggle = mk("minus", tr("禁用这个 Mod"), self._ask_toggle)
        self.btn_del = mk("trash", tr("删除这个 Mod"),
                          lambda: self.del_clicked.emit(self.item["file"]), "ModActDel")
        h.addWidget(self.acts, 0, Qt.AlignVCenter)

        for w in self.findChildren(QWidget):
            w.installEventFilter(self)
        self.installEventFilter(self)
        self.apply_item(item)

    # ---------- 悬停 ----------
    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Enter:
            self._hover(True)
        elif t == QEvent.Leave:
            # 在子控件之间移动也会发 Leave，只有真的离开整行才收起
            if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
                self._hover(False)
        return super().eventFilter(obj, ev)

    def _hover(self, on):
        if self._hov == on:
            return
        self._hov = on
        for b in (self.btn_info, self.btn_folder, self.btn_toggle, self.btn_del):
            b.setVisible(on)
        self.cb.setVisible(on or self.cb.isChecked())

    # ---------- 数据 ----------
    def apply_item(self, item):
        self.item.update(item)
        title = self.item.get("title") or self.item["name"]
        self.lb_name.setText(_elide(title, 22))
        self.lb_name.setToolTip(title)
        self.lb_file.setText(_elide(self.item.get("name") or self.item["file"], 32))
        self.lb_file.setToolTip(self.item.get("name") or self.item["file"])
        ver = self.item.get("version") or ""
        self.lb_ver.setText(_elide(ver, 18))
        desc = self.item.get("desc") or ""
        self.lb_desc.set_full(desc)
        self.lb_desc.setToolTip(desc)
        if desc:
            self.lb_desc.show()
        else:
            self.lb_desc.hide()

        # 标签
        while self.tag_box.count():
            it = self.tag_box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        tags = list(self.item.get("tags") or [])
        if not self.item.get("enabled", True):
            tags.insert(0, tr("已禁用"))
        for t in tags[:3]:
            lab = QLabel(t)
            lab.setObjectName("ModTagOff" if t == tr("已禁用") else "ModTag")
            self.tag_box.addWidget(lab)

        # 可否更新
        up = self.item.get("status") == "update"
        self.btn_upd.setVisible(up)
        if up:
            ver = self.item.get("latest_number") or ""
            self.btn_upd.setText(("↑ " + _elide(ver, 14)) if ver else tr("↑ 可更新"))
            self.btn_upd.setToolTip(f"{tr('更新到 ')}{ver}"
                                   f"{('（' + self.item.get('latest_date', '') + '）') if self.item.get('latest_date') else ''}")
        # 禁用状态：按钮换成「启用」
        if self.item.get("enabled", True):
            self.btn_toggle.setIcon(QIcon(make_icon("minus", 15, "#5d676e", 1.7)))
            self.btn_toggle.setToolTip(tr("禁用这个 Mod"))
        else:
            self.btn_toggle.setIcon(QIcon(make_icon("plus_dot", 15, "#1fa04a", 1.7)))
            self.btn_toggle.setToolTip(tr("启用这个 Mod"))
        self._paint_tile()

    def _paint_tile(self):
        if getattr(self, "_has_icon", False):
            return              # 图标已经贴上了，别被后续的远程信息刷新覆盖掉
        title = self.item.get("title") or self.item["name"]
        self.tile.setPixmap(QPixmap())
        self.tile.setText((title or "?")[:1].upper())
        self.tile.setStyleSheet(
            f"background: {_tile_color(title)}; color: #ffffff; border-radius: 9px;"
            f" font-size: 15px; font-weight: bold;")

    def set_icon(self, pm):
        self._has_icon = True
        self.tile.setText("")
        self.tile.setStyleSheet("background: transparent;")
        self.tile.setPixmap(_round_pixmap(pm, 36, 9))

    def _ask_toggle(self):
        self.toggle_clicked.emit(self.item["file"], not self.item.get("enabled", True))


class ModsPage(QWidget):
    """模组管理：一页管完搜索 / 启停 / 删除 / 更新"""

    def __init__(self, win):
        super().__init__()
        c = theme_colors()
        self.setStyleSheet(MOD_QSS.format(**c))
        self.win = win
        self.vid = ""
        self.mc = ""
        self.loader = ""
        self.versions = []
        self.items = []
        self._rows = {}
        self._q = ""
        self.tab = "all"
        self._busy = False
        self._icon_th = None
        self._want = ""     # 外部指定要管理的版本（版本列表菜单进来时用）

        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 24, 32, 8)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        self.btn_back = QPushButton()
        self.btn_back.setObjectName("ModBack")
        self.btn_back.setFixedSize(30, 30)
        self.btn_back.setCursor(Qt.PointingHandCursor)
        self.btn_back.setToolTip(tr("返回版本列表"))
        self.btn_back.setIcon(QIcon(make_icon("back", 15, "#465056", 2.0)))
        self.btn_back.setIconSize(QSize(15, 15))
        self.btn_back.clicked.connect(lambda: self.win.goto(5))
        head.addWidget(self.btn_back, 0, Qt.AlignVCenter)
        tb = QVBoxLayout()
        tb.setSpacing(3)
        title = QLabel(tr("模组管理"))
        title.setObjectName("Title")
        self.sub = QLabel(tr("选一个版本，管理它 mods 文件夹里的 Mod"))
        self.sub.setObjectName("Hint")
        tb.addWidget(title)
        tb.addWidget(self.sub)
        head.addLayout(tb, 1)
        self.combo = RoundCombo()
        self.combo.setFixedWidth(250)
        self.combo.currentIndexChanged.connect(self._on_pick)
        head.addWidget(self.combo)
        self.btn_reload = QPushButton(tr("刷新"))
        self.btn_reload.setObjectName("ModBtn")
        self.btn_reload.setCursor(Qt.PointingHandCursor)
        self.btn_reload.setIcon(QIcon(make_icon("refresh", 15, "#465056", 1.8)))
        self.btn_reload.clicked.connect(lambda: self.load(force=True))
        head.addWidget(self.btn_reload)
        lay.addLayout(head)

        self.search = QLineEdit()
        self.search.setObjectName("ModSearch")
        self.search.setPlaceholderText(tr("搜索 Mod 名称 / 描述 / 标签"))
        self.search.setClearButtonEnabled(True)
        self.search.addAction(QIcon(make_icon("search", 15, "#8a949c", 1.8)),
                              QLineEdit.LeadingPosition)
        self.search.textChanged.connect(self._on_search)
        lay.addWidget(self.search)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_folder = QPushButton(tr("打开文件夹"))
        self.btn_folder.setObjectName("ModBtn")
        self.btn_folder.setCursor(Qt.PointingHandCursor)
        self.btn_folder.clicked.connect(self.open_folder)
        self.btn_import = QPushButton(tr("从文件安装"))
        self.btn_import.setObjectName("ModBtn")
        self.btn_import.setCursor(Qt.PointingHandCursor)
        self.btn_import.clicked.connect(self.import_files)
        self.btn_download = QPushButton(tr("下载新 Mod"))
        self.btn_download.setObjectName("ModBtn")
        self.btn_download.setCursor(Qt.PointingHandCursor)
        self.btn_download.clicked.connect(self.goto_download)
        self.btn_all = QPushButton(tr("全选"))
        self.btn_all.setObjectName("ModBtn")
        self.btn_all.setCursor(Qt.PointingHandCursor)
        self.btn_all.clicked.connect(self.toggle_select_all)
        for b in (self.btn_folder, self.btn_import, self.btn_download, self.btn_all):
            bar.addWidget(b)
        bar.addStretch(1)
        self.stat = QLabel("")
        self.stat.setObjectName("Hint")
        bar.addWidget(self.stat)
        lay.addLayout(bar)

        card = QFrame()
        card.setObjectName("ModCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 10)
        cl.setSpacing(4)

        tabs = QHBoxLayout()
        tabs.setSpacing(2)
        self.tab_all = QPushButton("")
        self.tab_all.setObjectName("ModTabOn")
        self.tab_all.setCursor(Qt.PointingHandCursor)
        self.tab_all.clicked.connect(lambda: self.set_tab("all"))
        self.tab_upd = QPushButton("")
        self.tab_upd.setObjectName("ModTabOff")
        self.tab_upd.setCursor(Qt.PointingHandCursor)
        self.tab_upd.clicked.connect(lambda: self.set_tab("update"))
        tabs.addWidget(self.tab_all)
        tabs.addWidget(self.tab_upd)
        tabs.addStretch(1)
        self.btn_upd_all = QPushButton("")
        self.btn_upd_all.setObjectName("ModBtnOn")
        self.btn_upd_all.setCursor(Qt.PointingHandCursor)
        self.btn_upd_all.clicked.connect(self.update_all)
        self.btn_upd_all.hide()
        tabs.addWidget(self.btn_upd_all)
        cl.addLayout(tabs)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }")
        self.host = QWidget()
        self.rows = QVBoxLayout(self.host)
        self.rows.setContentsMargins(0, 2, 6, 2)
        self.rows.setSpacing(1)
        self.rows.addStretch(1)
        self.scroll.setWidget(self.host)
        cl.addWidget(self.scroll, 1)

        self.batch = QFrame()
        self.batch.setObjectName("ModBatch")
        bl = QHBoxLayout(self.batch)
        bl.setContentsMargins(12, 7, 12, 7)
        bl.setSpacing(8)
        self.batch_lab = QLabel("")
        self.batch_lab.setObjectName("RowName")
        bl.addWidget(self.batch_lab)
        bl.addStretch(1)
        self.btn_b_enable = QPushButton(tr("启用"))
        self.btn_b_enable.setObjectName("ModBtn")
        self.btn_b_enable.clicked.connect(lambda: self.batch_toggle(True))
        self.btn_b_disable = QPushButton(tr("禁用"))
        self.btn_b_disable.setObjectName("ModBtn")
        self.btn_b_disable.clicked.connect(lambda: self.batch_toggle(False))
        self.btn_b_del = QPushButton(tr("删除"))
        self.btn_b_del.setObjectName("ModBtnDanger")
        self.btn_b_del.clicked.connect(self.batch_delete)
        self.btn_b_clear = QPushButton(tr("取消"))
        self.btn_b_clear.setObjectName("ModBtn")
        self.btn_b_clear.clicked.connect(lambda: self.check_all(False))
        for b in (self.btn_b_enable, self.btn_b_disable, self.btn_b_del, self.btn_b_clear):
            b.setCursor(Qt.PointingHandCursor)
            bl.addWidget(b)
        self.batch.hide()
        cl.addWidget(self.batch)

        lay.addWidget(card, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_remote)

    # ---------- 版本 / 数据 ----------
    def focus_version(self, vid):
        """由外部指定要管理的版本：下次 refresh 优先定位到它，不管当前下拉框选的是谁"""
        self._want = vid or ""
        if vid:
            self.vid = ""       # 强制重新拉一次该版本的 mod 列表

    def refresh(self):
        t = ApiThread("GET", "/api/versions_detail", None, 60)
        t.done.connect(self._on_versions)
        t.start()

    def _on_versions(self, d):
        if not isinstance(d, dict) or not d.get("ok"):
            self.stat.setText(tr("读取版本列表失败"))
            return
        self.versions = d.get("versions") or []
        if not self.versions:
            self.vid = ""
            self._want = ""
            self.items = []
            self.combo.blockSignals(True)
            self.combo.clear()
            self.combo.blockSignals(False)
            self.render()
            self.stat.setText(tr("还没有安装任何版本"))
            return
        want = self._want or self.combo.currentData()
        self._want = ""
        if not any(v["id"] == want for v in self.versions):
            want = self.combo.currentData()
            if not any(v["id"] == want for v in self.versions):
                want = d.get("selected") or ""
        if not want and self.versions:
            want = max(self.versions,
                       key=lambda v: (v.get("loader") not in ("原版", ""),
                                      v.get("mods") or 0))["id"]
        self.combo.blockSignals(True)
        self.combo.clear()
        for v in self.versions:
            self.combo.addItem(f"{v['id']}　·　{tr(v.get('loader') or '原版')}", v["id"])
        idx = self.combo.findData(want)
        if idx >= 0:
            self.combo.setCurrentIndex(idx)
            want = self.combo.currentData()
        self.combo.blockSignals(False)
        if want != self.vid or not self.items:
            self.vid = want
            self.load()

    def _on_pick(self, _i):
        vid = self.combo.currentData()
        if vid and vid != self.vid:
            self.vid = vid
            self.load()

    def load(self, force=False):
        if not self.vid:
            return
        self.stat.setText(tr("正在读取 Mod 列表…"))
        t = ApiThread("POST", "/api/mods/scan", {"version": self.vid}, 60)
        t.done.connect(self._on_scan)
        t.start()

    def _on_scan(self, d):
        if not isinstance(d, dict) or not d.get("ok"):
            self.stat.setText(tr("读取失败：") + str((d or {}).get("error", tr("未知错误"))))
            return
        if d.get("version") != self.vid:
            return
        self.mc = d.get("mc") or ""
        self.loader = d.get("loader") or "原版"
        self.items = d.get("mods") or []
        self.render()
        self._stat_summary()
        self._load_icons()
        if self.items:
            files = [m["file"] for m in self.items]
            t = ApiThread("POST", "/api/mods/enrich",
                          {"version": self.vid, "files": files}, 30)
            t.done.connect(lambda _r: self._timer.start(700))
            t.start()
        else:
            self._timer.stop()

    def _load_icons(self):
        files = [m["file"] for m in self.items if m.get("icon")]
        if not files:
            return
        self._icon_th = _ModIconThread(self.vid, files)
        self._icon_th.got.connect(self._set_icon)
        self._icon_th.start()

    def _set_icon(self, f, data):
        row = self._rows.get(f)
        if row is None or not data:
            return
        pm = QPixmap()
        if pm.loadFromData(data):
            row.set_icon(pm)

    def _poll_remote(self):
        if self._busy:
            return
        self._busy = True
        t = ApiThread("GET", "/api/mods/enrich_status", None, 30)
        t.done.connect(self._on_remote)
        t.start()

    def _on_remote(self, d):
        self._busy = False
        if not isinstance(d, dict) or not d.get("ok"):
            return
        items = d.get("items") or {}
        if d.get("version") not in ("", self.vid):
            return
        for f, it in items.items():
            row = self._rows.get(f)
            if row is None:
                continue
            if it != row.item.get("_remote"):
                row.item["_remote"] = it
                row.apply_item(it)
            m = next((x for x in self.items if x["file"] == f), None)
            if m is not None:
                m.update(it)
        st = d.get("status")
        if st == "running":
            self.stat.setText(f"{tr('正在获取 Mod 信息')}　{d.get('done', 0)}/{d.get('total', 0)}")
        elif st in ("done", "error"):
            self._timer.stop()
            self._update_tabs()
            if self.tab == "update":
                self.render()
            self._stat_summary()

    def _stat_summary(self):
        n = len(self.items)
        n_upd = sum(1 for m in self.items if m.get("status") == "update")
        ld = self.loader if self.loader == "原版" else self.loader
        bits = [f"{tr(ld)}", f"Minecraft {self.mc}", f"{n}{tr(' 个 Mod')}"]
        if n_upd:
            bits.append(f"{n_upd}{tr(' 个可更新')}")
        self.stat.setText("　·　".join(bits))
        self.sub.setText(f"{self.vid}　·　" + "　·　".join(bits[:2]))

    # ---------- 渲染 ----------
    def visible_items(self):
        out = []
        for m in self.items:
            if self.tab == "update" and m.get("status") != "update":
                continue
            if self._q:
                hay = " ".join([m.get("title", ""), m.get("name", ""),
                                m.get("desc", ""), m.get("mod_id", ""),
                                " ".join(m.get("tags") or [])]).lower()
                if self._q not in hay:
                    continue
            out.append(m)
        return out

    def render(self):
        while self.rows.count() > 1:
            it = self.rows.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._rows.clear()
        shown = self.visible_items()
        for m in shown:
            row = _ModRow(m, self.vid)
            row.del_clicked.connect(self._del_one)
            row.toggle_clicked.connect(self._toggle_one)
            row.info_clicked.connect(self._info_one)
            row.folder_clicked.connect(lambda _f: self.open_folder())
            row.upd_clicked.connect(self._upd_one)
            row.check_changed.connect(self._recount)
            self.rows.insertWidget(self.rows.count() - 1, row)
            self._rows[m["file"]] = row
        if not shown:
            if not self.items:
                tip_txt = (tr("这个版本的 mods 文件夹是空的，用「从文件安装」或「下载新 Mod」加几个")
                           if self.loader != "原版" else
                           tr("原版没有 Mod 可管，先装 Forge / Fabric / NeoForge 再回来"))
            elif self.tab == "update":
                tip_txt = tr("太好了，所有 Mod 都是最新的")
            else:
                tip_txt = tr("没有匹配的 Mod")
            tip = QLabel(tip_txt)
            tip.setObjectName("Hint")
            tip.setAlignment(Qt.AlignCenter)
            tip.setMinimumHeight(120)
            self.rows.insertWidget(0, tip)
        self._update_tabs()
        self._recount()

    def _update_tabs(self):
        n_upd = sum(1 for m in self.items if m.get("status") == "update")
        self.tab_all.setText(f"{tr('全部')} ({len(self.items)})")
        self.tab_upd.setText(f"{tr('可更新')} ({n_upd})")
        self.btn_upd_all.setText(f"{tr('一键更新')} ({n_upd})")
        self.btn_upd_all.setVisible(bool(n_upd))
        for b, on in ((self.tab_all, self.tab == "all"), (self.tab_upd, self.tab == "update")):
            b.setObjectName("ModTabOn" if on else "ModTabOff")
            b.style().unpolish(b)
            b.style().polish(b)

    def set_tab(self, tab):
        if self.tab == tab:
            return
        self.tab = tab
        self.render()

    def _on_search(self, text):
        self._q = (text or "").strip().lower()
        self.render()

    # ---------- 选择 ----------
    def _checked(self):
        return [f for f, r in self._rows.items() if r.cb.isChecked()]

    def _recount(self):
        files = self._checked()
        n = len(files)
        self.batch.setVisible(n > 0)
        self.batch_lab.setText(f"{tr('已选 ')}{n}{tr(' 个 Mod')}")
        total = len(self._rows)
        if total and n == total:
            self.btn_all.setText(tr("取消全选"))
        else:
            self.btn_all.setText(tr("全选"))

    def toggle_select_all(self):
        n = len(self._checked())
        self.check_all(n < len(self._rows))

    def check_all(self, on):
        for r in self._rows.values():
            r.cb.setChecked(on)
            r.cb.setVisible(on or r._hov)
        self._recount()

    # ---------- 操作 ----------
    def open_folder(self):
        if not self.vid:
            return
        t = ApiThread("POST", "/api/mod/open_folder", {"version": self.vid}, 20)
        t.done.connect(lambda r: None if (isinstance(r, dict) and r.get("ok"))
                       else warn_card(self, tr("打开失败"),
                                      str((r or {}).get("error", tr("未知错误")))))
        t.start()

    def import_files(self):
        if not self.vid:
            return
        paths, _f = QFileDialog.getOpenFileNames(
            self, tr("选择要安装的 Mod（可多选）"), "",
            f"{tr('Minecraft Mod')} (*.jar);;{tr('所有文件')} (*.*)")
        if not paths:
            return
        t = ApiThread("POST", "/api/mod/import", {"version": self.vid, "paths": paths}, 300)
        t.done.connect(self._after_import)
        t.start()

    def _after_import(self, r):
        if not isinstance(r, dict) or not r.get("ok"):
            warn_card(self, tr("安装失败"), str((r or {}).get("error", tr("未知错误"))))
            return
        n = len(r.get("imported") or [])
        self.stat.setText(f"{tr('已安装 ')}{n}{tr(' 个 Mod')}")
        self.load()

    def goto_download(self):
        try:
            self.win.download.switch_seg(1)
        except Exception:
            pass
        self.win.goto(1)

    def _confirm(self, title, text):
        return QMessageBox.question(self, title, text, QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) == QMessageBox.Yes

    def _del_one(self, f):
        self._delete([f])

    def _delete(self, files):
        if not files:
            return
        tip = (f"{tr('确定删除这 ')}{len(files)}{tr(' 个 Mod？')}" if len(files) > 1
               else f"{tr('确定删除「')}{files[0]}{tr('」？')}")
        if not self._confirm(tr("删除 Mod"),
                             f"{tip}\n\n{tr('文件会先挪到 launcher/mod_trash，后悔了还能找回来。')}"):
            return
        t = ApiThread("POST", "/api/mod/delete", {"version": self.vid, "files": files}, 180)
        t.done.connect(self._after_change)
        t.start()

    def _toggle_one(self, f, enabled):
        self._post_toggle([f], enabled)

    def batch_toggle(self, enabled):
        files = self._checked()
        if files:
            self._post_toggle(files, enabled)

    def _post_toggle(self, files, enabled):
        t = ApiThread("POST", "/api/mod/toggle",
                      {"version": self.vid, "files": files, "enabled": enabled}, 120)
        t.done.connect(self._after_change)
        t.start()

    def batch_delete(self):
        self._delete(self._checked())

    def _after_change(self, r):
        if not isinstance(r, dict) or not r.get("ok"):
            warn_card(self, tr("操作失败"), str((r or {}).get("error", tr("未知错误"))))
            return
        failed = r.get("failed") or []
        if failed:
            head = f"{len(failed)}{tr(' 个没处理成功')}："
            detail = "；".join(f"{x.get('name', '')}（{x.get('reason', '')}）"
                               for x in failed[:3])
            self.stat.setText(head + detail)
        self.load()

    def _info_one(self, f):
        m = next((x for x in self.items if x["file"] == f), None)
        if not m:
            return
        lines = [f"{tr('名称')}：{m.get('title') or m.get('name')}"]
        if m.get("mod_id"):
            lines.append(f"Mod ID：{m['mod_id']}")
        lines.append(f"{tr('版本')}：{m.get('version') or tr('未知')}")
        lines.append(f"{tr('文件')}：{m.get('name') or m['file']}"
                     f"（{fmt_size((m.get('size') or 0) / 1048576)}）")
        if m.get("tags"):
            lines.append(f"{tr('标签')}：{' / '.join(m['tags'])}")
        if m.get("desc"):
            lines.append(f"{tr('说明')}：{m['desc']}")
        if m.get("status") == "update":
            lines.append(f"\n{tr('可更新到 ')}{m.get('latest_number', '')}"
                         f"（{m.get('latest_date', '')}）")
        elif m.get("status") == "up_to_date":
            lines.append(f"\n{tr('已是最新版本')}")
        elif m.get("status") == "unknown":
            lines.append(f"\n{tr('Modrinth 上没有适配当前版本的文件')}")
        elif m.get("status") == "not_found":
            lines.append(f"\n{tr('没在 Modrinth 上找到这个 Mod')}")
        lines.append(f"\n{tr('状态')}："
                     + (tr("已启用") if m.get("enabled", True) else tr("已禁用")))
        info_card(self, m.get("title") or m.get("name") or tr("Mod 详情"), "\n".join(lines))

    def _upd_one(self, f):
        m = next((x for x in self.items if x["file"] == f), None)
        if not m or not m.get("latest_url"):
            return
        self._do_update([{"file": m["file"], "url": m["latest_url"],
                          "filename": m["latest_filename"]}])

    def update_all(self):
        ups = [m for m in self.items if m.get("status") == "update" and m.get("latest_url")]
        if not ups:
            return
        if not self._confirm(tr("更新 Mod"),
                             f"{tr('把这 ')}{len(ups)}{tr(' 个 Mod 更新到最新版本？')}"):
            return
        self._do_update([{"file": m["file"], "url": m["latest_url"],
                          "filename": m["latest_filename"]} for m in ups])

    def _do_update(self, updates):
        # 同一份文件只下一次（两个旧 jar 追同一个新版本时别重复下载）
        seen, uniq = set(), []
        for u in updates:
            fn = (u.get("filename") or "").lower()
            if not fn or fn in seen:
                continue
            seen.add(fn)
            uniq.append(u)
        if not uniq:
            return
        updates = uniq
        t = ApiThread("POST", "/api/mod/update", {"version": self.vid, "updates": updates}, 30)
        t.done.connect(self._after_update)
        t.start()

    def _after_update(self, r):
        if not isinstance(r, dict) or not r.get("ok"):
            warn_card(self, tr("更新失败"), str((r or {}).get("error", tr("未知错误"))))
            return
        self.stat.setText(tr("已开始更新，进度看左下角的小球"))
        QTimer.singleShot(4000, lambda: self.load())


class LogPage(QWidget):
    def __init__(self, win):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 24, 32, 8)
        lay.setSpacing(10)
        head = QHBoxLayout()
        title = QLabel(tr("运行日志"))
        title.setObjectName("Title")
        back = QPushButton(tr("返回设置"))
        back.setObjectName("GhostBtn")
        back.setCursor(Qt.PointingHandCursor)
        back.clicked.connect(lambda: win.goto(3))
        analyze = QPushButton(tr("分析日志"))
        analyze.setObjectName("GhostBtn")
        analyze.setCursor(Qt.PointingHandCursor)
        analyze.clicked.connect(lambda: win.show_error_analysis(
            self.view.toPlainText(), context=tr("运行日志")))
        head.addWidget(title)
        head.addStretch()
        head.addWidget(analyze)
        head.addWidget(back)
        lay.addLayout(head)
        self.view = QPlainTextEdit()
        self.view.setObjectName("LogView")
        self.view.setReadOnly(True)
        lay.addWidget(self.view, 1)

    def set_text(self, text):
        if text != self.view.toPlainText():
            self.view.setPlainText(text)
            sb = self.view.verticalScrollBar()
            sb.setValue(sb.maximum())


# ---------------- 报错自动分析 ----------------
class ErrorReportDialog(QDialog):
    """报错自动分析弹窗：结论 + 原因 + 建议 + 原始报错，一键复制"""

    QSS = """
#ErrCard {{ background: #ffffff; border: 1px solid {line}; border-radius: 14px; }}
#ErrTitle {{ color: {accent}; font-size: 20px; font-weight: bold; background: transparent; }}
#ErrRule {{ background: {accent}; border: none; }}
#ErrVerdict {{ color: #c0392b; font-size: 16px; font-weight: bold; background: transparent; }}
#ErrReason {{ color: #4a545b; font-size: 13px; background: transparent; }}
#ErrTip {{ color: #4a545b; font-size: 13px; background: transparent; }}
#ErrCtx {{ color: #8a949c; font-size: 12px; background: transparent; }}
#ErrRaw {{
  background: {surface}; border: 1px solid {line_soft}; border-radius: 10px;
  color: #4a545b; font-size: 11px;
}}
#ErrCopy {{
  background: #ffffff; color: {accent}; border: 1px solid {accent};
  border-radius: 8px; padding: 7px 16px; font-size: 13px;
}}
#ErrCopy:hover {{ background: {accent_soft}; }}
#ErrClose {{
  background: {accent}; color: #ffffff; border: none;
  border-radius: 8px; padding: 7px 22px; font-size: 13px;
}}
#ErrClose:hover {{ background: {accent_dark}; }}
"""

    def __init__(self, parent, res, raw, context=""):
        super().__init__(parent)
        c = theme_colors()
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setStyleSheet(self.QSS.format(**c))
        self.setMinimumWidth(500)
        self._drag = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        card = QFrame()
        card.setObjectName("ErrCard")
        sh = QGraphicsDropShadowEffect(self)
        sh.setBlurRadius(40)
        sh.setOffset(0, 10)
        sh.setColor(QColor(20, 30, 40, 70))
        card.setGraphicsEffect(sh)
        outer.addWidget(card)

        cl = QVBoxLayout(card)
        cl.setContentsMargins(28, 22, 28, 18)
        cl.setSpacing(0)

        head = QHBoxLayout()
        title = QLabel(tr("错误分析"))
        title.setObjectName("ErrTitle")
        head.addWidget(title)
        head.addStretch()
        if context:
            ctx = QLabel(f"{tr('场景：')}{tr(context)}")
            ctx.setObjectName("ErrCtx")
            head.addWidget(ctx)
        head_w = QWidget()
        head_w.setLayout(head)
        cl.addWidget(head_w)
        cl.addSpacing(10)

        rule = QFrame()
        rule.setObjectName("ErrRule")
        rule.setFixedHeight(2)
        cl.addWidget(rule)
        cl.addSpacing(14)

        if res:
            verdict = QLabel(f"{tr('大概率是：')}{tr(res['title'])}")
            verdict.setObjectName("ErrVerdict")
            verdict.setWordWrap(True)
            cl.addWidget(verdict)
            cl.addSpacing(8)
            reason = QLabel(tr(res["reason"]))
            reason.setObjectName("ErrReason")
            reason.setWordWrap(True)
            cl.addWidget(reason)
            cl.addSpacing(10)
            for tip in res["tips"]:
                tip_l = QLabel(f"•  {tr(tip)}")
                tip_l.setObjectName("ErrTip")
                tip_l.setWordWrap(True)
                cl.addWidget(tip_l)
                cl.addSpacing(4)
        else:
            verdict = QLabel(tr("这段提示比较普通，没有触发任何已知错误模式"))
            verdict.setObjectName("ErrVerdict")
            verdict.setWordWrap(True)
            cl.addWidget(verdict)

        cl.addSpacing(12)
        raw_label = QLabel(tr("原始报错"))
        raw_label.setObjectName("ErrCtx")
        cl.addWidget(raw_label)
        cl.addSpacing(4)
        view = QPlainTextEdit()
        view.setObjectName("ErrRaw")
        view.setReadOnly(True)
        view.setPlainText((raw or "").strip()[:4000])
        view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # 日志按原样一行一行显示，长行横向滚动，比强行折行好读
        view.setLineWrapMode(QPlainTextEdit.NoWrap)
        # 短报错别留一大片空白，长的封顶 130
        _lines = (raw or "").strip().count("\n") + 1
        view.setFixedHeight(max(72, min(130, 24 + 16 * min(6, _lines))))
        cl.addWidget(view)

        cl.addSpacing(16)
        row = QHBoxLayout()
        row.addStretch()
        copy_btn = QPushButton(tr("复制分析"))
        copy_btn.setObjectName("ErrCopy")
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.clicked.connect(self._copy)
        row.addWidget(copy_btn)
        row.addSpacing(8)
        close_btn = QPushButton(tr("知道了"))
        close_btn.setObjectName("ErrClose")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        cl.addLayout(row)

        self._report = self._build_text(res, raw, context)

    def _build_text(self, res, raw, context):
        lines = []
        if context:
            lines.append(f"{tr('场景：')}{tr(context)}")
        if res:
            lines.append(f"{tr('大概率是：')}{tr(res['title'])}")
            lines.append(tr(res["reason"]))
            lines.append(tr("建议："))
            for tip in res["tips"]:
                lines.append(f"  - {tr(tip)}")
        lines.append("")
        lines.append(f"{tr('原始报错：')}")
        lines.append((raw or "").strip()[:4000])
        return "\n".join(lines)

    def _copy(self):
        QApplication.clipboard().setText(self._report)
        self.accept()
        win = self.parent()
        while win is not None and not hasattr(win, "toast"):
            win = win.parent()
        if win is not None:
            win.toast.show_info(tr("已复制"), tr("分析报告已复制，去粘贴给帮你的人吧"), 2600)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
            e.accept()

    def mouseReleaseEvent(self, _e):
        self._drag = None


# ---------------- Mod 更新检查 ----------------
class ModUpdateDialog(QDialog):
    """检查所选版本的 Mod 更新：逐个比对 Modrinth，勾选后一键更新"""

    QSS = """
#ErrCard {{ background: #ffffff; border: 1px solid {line}; border-radius: 14px; }}
#ErrTitle {{ color: {accent}; font-size: 20px; font-weight: bold; background: transparent; }}
#ErrRule {{ background: {accent}; border: none; }}
#ErrVerdict {{ color: #c0392b; font-size: 15px; font-weight: bold; background: transparent; }}
#ErrReason {{ color: #4a545b; font-size: 13px; background: transparent; }}
#ErrCtx {{ color: #8a949c; font-size: 12px; background: transparent; }}
#ModRow {{ background: {surface}; border: 1px solid {line_soft}; border-radius: 10px; }}
#ModName {{ color: #22282d; font-size: 13px; font-weight: bold; background: transparent; }}
#ModFile {{ color: #8a949c; font-size: 11px; background: transparent; }}
#ModState {{ color: #4a545b; font-size: 12px; background: transparent; }}
#ModStateUp {{ color: #1fa04a; font-size: 12px; font-weight: bold; background: transparent; }}
#ModStateNew {{ color: #d97706; font-size: 12px; font-weight: bold; background: transparent; }}
#ModScroll {{ border: none; background: transparent; }}
#ModGo {{
  background: {accent}; color: #ffffff; border: none;
  border-radius: 8px; padding: 7px 18px; font-size: 13px;
}}
#ModGo:hover {{ background: {accent_dark}; }}
#ModGo:disabled {{ background: {line_soft}; color: #ffffff; }}
#ModClose {{
  background: #ffffff; color: #4a545b; border: 1px solid {line};
  border-radius: 8px; padding: 7px 18px; font-size: 13px;
}}
#ModClose:hover {{ background: #f7f5ee; }}
"""

    def __init__(self, parent, vid):
        super().__init__(parent)
        c = theme_colors()
        self.vid = vid
        self._finished = False
        self._rows = []
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setStyleSheet(self.QSS.format(**c))
        self.setMinimumSize(640, 470)
        self._drag = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        card = QFrame()
        card.setObjectName("ErrCard")
        sh = QGraphicsDropShadowEffect(self)
        sh.setBlurRadius(40)
        sh.setOffset(0, 10)
        sh.setColor(QColor(20, 30, 40, 70))
        card.setGraphicsEffect(sh)
        outer.addWidget(card)

        cl = QVBoxLayout(card)
        cl.setContentsMargins(28, 22, 28, 18)
        cl.setSpacing(0)

        title = QLabel(f"{tr('Mod 更新检查')}　·　{vid}")
        title.setObjectName("ErrTitle")
        cl.addWidget(title)
        cl.addSpacing(10)
        rule = QFrame()
        rule.setObjectName("ErrRule")
        rule.setFixedHeight(2)
        cl.addWidget(rule)
        cl.addSpacing(10)

        self.status = QLabel(tr("正在检查…"))
        self.status.setObjectName("ErrCtx")
        cl.addWidget(self.status)
        cl.addSpacing(8)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("ModScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.host = QWidget()
        self.list_lay = QVBoxLayout(self.host)
        self.list_lay.setContentsMargins(0, 0, 4, 0)
        self.list_lay.setSpacing(6)
        self.list_lay.addStretch()
        self.scroll.setWidget(self.host)
        cl.addWidget(self.scroll, 1)
        cl.addSpacing(12)

        row = QHBoxLayout()
        self.btn_go = QPushButton(tr("更新所选"))
        self.btn_go.setObjectName("ModGo")
        self.btn_go.setCursor(Qt.PointingHandCursor)
        self.btn_go.setEnabled(False)
        self.btn_go.clicked.connect(self._do_update)
        row.addWidget(self.btn_go)
        row.addStretch()
        btn_close = QPushButton(tr("关闭"))
        btn_close.setObjectName("ModClose")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(self.reject)
        row.addWidget(btn_close)
        cl.addLayout(row)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(700)
        self._start_check()

    def _start_check(self):
        try:
            r = Api.post("/api/mod/check_updates", {"version": self.vid}, timeout=15).json()
        except Exception as e:
            r = {"ok": False, "error": str(e)}
        if not r.get("ok"):
            self._finished = True
            self.status.setText(f"{tr('检查失败：')}{r.get('error') or tr('未知错误')}")

    def _poll(self):
        if self._finished:
            return
        th = ApiThread("GET", "/api/mod/check_status", timeout=30)
        th.done.connect(self._on_status)
        th.start()

    def _poll(self):
        if self._finished:
            return
        th = ApiThread("GET", "/api/mod/check_status", timeout=30)
        th.done.connect(self._on_status)
        th.start()

    def _on_status(self, d):
        if self._finished or not isinstance(d, dict):
            return
        if not d.get("ok") and d.get("error"):
            self._finished = True
            self._timer.stop()
            self.status.setText(f"{tr('检查失败：')}{d.get('error')}")
            return
        st = d.get("status")
        if st == "running":
            self.status.setText(f"{tr('正在检查…')}  {d.get('progress', '')}")
            return
        if st not in ("done", "error"):
            return          # 任务还没被后端接上，继续等下一轮
        self._finished = True
        self._timer.stop()
        if st == "error":
            self.status.setText(f"{tr('检查失败：')}{d.get('error', '')}")
            return
        self._render(d.get("mods") or [], d.get("mc") or "", d.get("loader") or "")

    def _render(self, mods, mc, loader):
        lname = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge",
                 "quilt": "Quilt"}.get((loader or "").lower(), tr(loader or "未知加载器"))
        head = f"{lname} · Minecraft {mc} · {len(mods)} {tr('个 Mod')}"
        self.status.setText(head)
        lay = self.list_lay
        while lay.count() > 1:
            it = lay.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        updatable = 0
        for m in mods:
            row = QFrame()
            row.setObjectName("ModRow")
            h = QHBoxLayout(row)
            h.setContentsMargins(14, 9, 14, 9)
            h.setSpacing(10)
            cb = QCheckBox()
            is_update = m.get("status") == "update"
            cb.setVisible(is_update)
            cb.setChecked(is_update)
            cb.toggled.connect(self._recount)
            box = QVBoxLayout()
            box.setSpacing(2)
            nm = f"{m.get('title') or m.get('file', '')}"
            if m.get("version"):
                nm += f"　{m['version']}"
            name = QLabel(_elide(nm, 30))
            name.setObjectName("ModName")
            name.setToolTip(m.get("file", ""))
            file_l = QLabel(_elide(m.get("file", ""), 42))
            file_l.setObjectName("ModFile")
            box.addWidget(name)
            box.addWidget(file_l)
            h.addWidget(cb, 0, Qt.AlignVCenter)
            h.addLayout(box, 1)
            if is_update:
                size = m.get("latest_size") or 0
                st_txt = (f"{tr('可更新到')} {m.get('latest_number', '')}\n"
                          f"（{m.get('latest_date', '')}"
                          f"{(' · ' + fmt_size(size / 1048576)) if size else ''}）")
                st = QLabel(st_txt)
                st.setObjectName("ModStateNew")
                st.setWordWrap(True)
                st.setFixedWidth(210)
                st.setToolTip(f"{m.get('latest_filename', '')}")
                updatable += 1
            elif m.get("status") == "up_to_date":
                st = QLabel(tr("✔ 已是最新"))
                st.setObjectName("ModStateUp")
                st.setFixedWidth(210)
            else:
                st = QLabel(tr("未在 Modrinth 找到对应项目"))
                st.setObjectName("ModState")
                st.setWordWrap(True)
                st.setFixedWidth(210)
            st.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            h.addWidget(st, 0, Qt.AlignVCenter)
            self._rows.append((cb, m))
            lay.insertWidget(lay.count() - 1, row)
        if not mods:
            empty = QLabel(tr("这个版本的 mods/ 文件夹是空的"))
            empty.setObjectName("ModState")
            lay.insertWidget(lay.count() - 1, empty)
        self.btn_go.setText(f"{tr('更新所选')} ({updatable})")
        self.btn_go.setEnabled(updatable > 0)

    def _recount(self):
        n = sum(1 for cb, _ in self._rows if cb.isChecked())
        self.btn_go.setText(f"{tr('更新所选')} ({n})")
        self.btn_go.setEnabled(n > 0)

    def _do_update(self):
        updates = [{"file": m.get("file"), "url": m.get("latest_url"),
                    "filename": m.get("latest_filename")}
                   for cb, m in self._rows if cb.isChecked()
                   and m.get("latest_url") and m.get("latest_filename")]
        if not updates:
            return
        try:
            r = Api.post("/api/mod/update", {"version": self.vid, "updates": updates},
                         timeout=30).json()
        except Exception as e:
            warn_card(self, tr("更新失败"), f"{tr('后端无响应：')}{e}")
            return
        if not r.get("ok"):
            warn_card(self, tr("更新失败"), r.get("error") or tr("未知错误"))
            return
        # 进度交给右下角提示卡（和下载/安装共用一套），对话框直接关
        self.accept()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
            e.accept()

    def mouseReleaseEvent(self, _e):
        self._drag = None


class JoinRoomDialog(QDialog):
    """无边框「加入房间」弹窗：房间号 + 密码一次填完，样式与启动器一致"""

    QSS = """
#JoinCard {{ background: #ffffff; border: 1px solid {line}; border-radius: 14px; }}
#JoinTitle {{ color: {accent}; font-size: 19px; font-weight: bold; background: transparent; }}
#JoinHint {{ color: #7b858c; font-size: 12px; background: transparent; }}
#JoinLabel {{ color: #465056; font-size: 13px; background: transparent; }}
#JoinInput {{
  background: {surface}; border: 1px solid {line_soft}; border-radius: 9px;
  padding: 9px 11px; font-size: 13px; color: #2f383d;
}}
#JoinInput:hover {{ background: #f7f5ee; }}
#JoinInput:focus {{ background: #ffffff; border: 1px solid {accent}; }}
#JoinGo {{
  background: {accent}; color: #ffffff; border: none;
  border-radius: 8px; padding: 8px 22px; font-size: 13px; font-weight: bold;
}}
#JoinGo:hover {{ background: {accent_hover}; }}
#JoinGo:disabled {{ background: #dbd8ce; color: #ffffff; }}
#JoinCancel {{
  background: #ffffff; color: #4a545b; border: 1px solid {line};
  border-radius: 8px; padding: 8px 18px; font-size: 13px;
}}
#JoinCancel:hover {{ background: #f7f5ee; }}
"""

    def __init__(self, parent=None):
        super().__init__(parent)
        c = theme_colors()
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setStyleSheet(self.QSS.format(**c))
        self._drag = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)

        card = QFrame()
        card.setObjectName("JoinCard")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(20, 30, 40, 70))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        cl = QVBoxLayout(card)
        cl.setContentsMargins(26, 22, 26, 18)
        cl.setSpacing(0)

        title = QLabel(tr("加入房间"))
        title.setObjectName("JoinTitle")
        cl.addWidget(title)
        cl.addSpacing(6)
        hint = QLabel(tr("房间号和密码问开服的人要"))
        hint.setObjectName("JoinHint")
        hint.setWordWrap(True)
        cl.addWidget(hint)
        cl.addSpacing(16)

        lab_room = QLabel(tr("房间号（问开服的人要）："))
        lab_room.setObjectName("JoinLabel")
        cl.addWidget(lab_room)
        cl.addSpacing(6)
        self.room = QLineEdit()
        self.room.setObjectName("JoinInput")
        self.room.setPlaceholderText("mc1e3f9ca9")
        cl.addWidget(self.room)
        cl.addSpacing(12)

        lab_sec = QLabel(tr("密码："))
        lab_sec.setObjectName("JoinLabel")
        cl.addWidget(lab_sec)
        cl.addSpacing(6)
        self.sec = QLineEdit()
        self.sec.setObjectName("JoinInput")
        cl.addWidget(self.sec)

        cl.addSpacing(20)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        self.go = QPushButton(tr("加入"))
        self.go.setObjectName("JoinGo")
        self.go.setCursor(Qt.PointingHandCursor)
        self.go.setDefault(True)
        self.go.clicked.connect(self.accept)
        cancel = QPushButton(tr("取消"))
        cancel.setObjectName("JoinCancel")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(self.go)
        row.addWidget(cancel)
        cl.addLayout(row)

        self.setFixedWidth(430)
        self.room.textChanged.connect(self._sync)
        self.sec.textChanged.connect(self._sync)
        self._sync()
        self.adjustSize()
        self._center_on_parent()
        self.room.setFocus()

    def _sync(self):
        self.go.setEnabled(bool(self.room.text().strip()) and bool(self.sec.text().strip()))

    def values(self):
        return self.room.text().strip(), self.sec.text().strip()

    def _center_on_parent(self):
        p = self.parentWidget()
        if p is None:
            return
        g = p.window().frameGeometry()
        self.move(g.center().x() - self.width() // 2,
                  g.center().y() - self.height() // 2)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            anim = getattr(self, "_pop_anim", None)
            if anim is not None:
                anim.stop()
                self._pop_anim = None
                self.setWindowOpacity(1.0)
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag = None
        super().mouseReleaseEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        self._center_on_parent()
        pop_in_window(self)


# ---------------- 更新提示弹窗 ----------------
class MessageDialog(QDialog):
    """无边框消息卡：图标 + 标题 + 正文 + 确定。
    替代 QMessageBox.information / warning，去掉系统标题栏和边框。"""

    QSS = """
#MsgCard {{ background: #ffffff; border: 1px solid {line}; border-radius: 14px; }}
#MsgTitle {{ color: {title_col}; font-size: 19px; font-weight: bold; background: transparent; }}
#MsgBody {{ color: #4a545b; font-size: 14px; background: transparent; }}
#MsgIcon {{ color: #ffffff; font-size: 20px; font-weight: bold; background: {icon_bg};
            border-radius: 17px; }}
#MsgText {{ background: {surface}; border: 1px solid {line_soft}; border-radius: 10px;
            padding: 8px 10px; color: #4a545b; font-size: 12px; }}
#MsgOK {{
  background: #ffffff; color: {accent}; border: 1px solid {accent};
  border-radius: 8px; padding: 7px 26px; font-size: 13px;
}}
#MsgOK:hover {{ background: {accent_soft}; }}
#MsgOK:pressed {{ background: {accent_soft2}; }}
"""

    def __init__(self, parent, title, text, kind="info"):
        super().__init__(parent)
        c = theme_colors()
        c["icon_bg"] = c["accent"] if kind == "info" else "#d98324"
        c["title_col"] = c["accent"] if kind == "info" else "#a8641a"
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setStyleSheet(self.QSS.format(**c))
        self._drag = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)

        card = QFrame()
        card.setObjectName("MsgCard")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(20, 30, 40, 70))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        cl = QVBoxLayout(card)
        cl.setContentsMargins(26, 22, 26, 18)
        cl.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(12)
        ic = QLabel("i" if kind == "info" else "!")
        ic.setObjectName("MsgIcon")
        ic.setFixedSize(34, 34)
        ic.setAlignment(Qt.AlignCenter)
        head.addWidget(ic, 0, Qt.AlignTop)
        ttl = QLabel(title)
        ttl.setObjectName("MsgTitle")
        head.addWidget(ttl, 0, Qt.AlignVCenter)
        head.addStretch(1)
        cl.addLayout(head)

        cl.addSpacing(14)
        body = (text or "").strip()
        if len(body) > 260:
            # 长错误信息给个可滚动区，避免窗口高到出屏
            view = QPlainTextEdit()
            view.setObjectName("MsgText")
            view.setReadOnly(True)
            view.setPlainText(body)
            view.setFixedHeight(170)
            view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            cl.addWidget(view)
        else:
            lbl = QLabel(body)
            lbl.setObjectName("MsgBody")
            lbl.setWordWrap(True)
            lbl.setMinimumWidth(360)
            cl.addWidget(lbl)

        cl.addSpacing(18)
        row = QHBoxLayout()
        row.addStretch(1)
        ok = QPushButton(tr("确定"))
        ok.setObjectName("MsgOK")
        ok.setCursor(Qt.PointingHandCursor)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        cl.addLayout(row)

        self.setFixedWidth(460)
        self.adjustSize()
        self._center_on_parent()

    def _center_on_parent(self):
        p = self.parentWidget()
        if p is None:
            return
        g = p.window().frameGeometry()
        self.move(g.center().x() - self.width() // 2,
                  g.center().y() - self.height() // 2)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):
            self.accept()
            e.accept()
            return
        super().keyPressEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            anim = getattr(self, "_pop_anim", None)
            if anim is not None:
                anim.stop()
                self._pop_anim = None
                self.setWindowOpacity(1.0)
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag = None
        super().mouseReleaseEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        self._center_on_parent()
        pop_in_window(self)


def info_card(parent, title, text):
    """无边框提示卡，替代 QMessageBox.information"""
    return MessageDialog(parent, title, text, "info").exec()


def warn_card(parent, title, text):
    """无边框警告卡，替代 QMessageBox.warning"""
    return MessageDialog(parent, title, text, "warn").exec()


class UpdateDialog(QDialog):
    """自绘「发现更新！」弹窗：标题 + 主题色分隔线 + 版本变化 + 更新内容 + 更新！冲！/取消"""

    QSS = """
#UpdateCard {{ background: #ffffff; border: 1px solid {line}; border-radius: 14px; }}
#UpdateTitle {{ color: {accent}; font-size: 21px; font-weight: bold; background: transparent; }}
#UpdateRule {{ background: {accent}; border: none; }}
#UpdateBody {{ color: #4a545b; font-size: 14px; background: transparent; }}
#UpdateVer {{ color: {accent_dark}; font-size: 12px; background: transparent; }}
#UpdateNotes {{
  background: {surface}; border: 1px solid {line_soft}; border-radius: 10px;
  padding: 8px 10px; color: #4a545b; font-size: 12px;
}}
#UpdateGo {{
  background: #ffffff; color: {accent}; border: 1px solid {accent};
  border-radius: 8px; padding: 7px 18px; font-size: 13px;
}}
#UpdateGo:hover {{ background: {accent_soft}; }}
#UpdateGo:pressed {{ background: {accent_soft2}; }}
#UpdateCancel {{
  background: #ffffff; color: #4a545b; border: 1px solid {line};
  border-radius: 8px; padding: 7px 18px; font-size: 13px;
}}
#UpdateCancel:hover {{ background: #f7f5ee; }}
"""

    def __init__(self, parent, latest, current, notes="", tag=""):
        super().__init__(parent)
        c = theme_colors()
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setStyleSheet(self.QSS.format(**c))
        self._drag = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)

        card = QFrame()
        card.setObjectName("UpdateCard")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(20, 30, 40, 70))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        cl = QVBoxLayout(card)
        cl.setContentsMargins(28, 22, 28, 18)
        cl.setSpacing(0)

        title = QLabel(tr("发现更新！"))
        title.setObjectName("UpdateTitle")
        cl.addWidget(title)
        cl.addSpacing(12)

        rule = QFrame()
        rule.setObjectName("UpdateRule")
        rule.setFixedHeight(2)
        cl.addWidget(rule)
        cl.addSpacing(18)

        body = QLabel(tr("发现启动器更新，是否立即下载？"))
        body.setObjectName("UpdateBody")
        cl.addWidget(body)
        cl.addSpacing(8)

        ver = QLabel(f"{tr('当前 v')}{current}{tr('　→　最新 v')}{latest}{('　' + tag) if tag else ''}")
        ver.setObjectName("UpdateVer")
        cl.addWidget(ver)

        notes = (notes or "").strip()
        if notes:
            cl.addSpacing(12)
            view = QPlainTextEdit()
            view.setObjectName("UpdateNotes")
            view.setReadOnly(True)
            view.setPlainText(notes)
            view.setFixedHeight(min(132, 26 + 18 * min(6, notes.count("\n") + 1)))
            view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            cl.addWidget(view)

        cl.addSpacing(18)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        go = QPushButton(tr("更新！冲！"))
        go.setObjectName("UpdateGo")
        go.setCursor(Qt.PointingHandCursor)
        go.setDefault(True)
        go.clicked.connect(self.accept)
        cancel = QPushButton(tr("取消"))
        cancel.setObjectName("UpdateCancel")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(go)
        row.addWidget(cancel)
        cl.addLayout(row)

        self.setFixedWidth(500)
        self.adjustSize()
        self._center_on_parent()

    def _center_on_parent(self):
        p = self.parentWidget()
        if p is None:
            return
        g = p.window().frameGeometry()
        self.move(g.center().x() - self.width() // 2,
                  g.center().y() - self.height() // 2)

    # 无边框窗口：按住卡片任意空白处可拖动
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            anim = getattr(self, "_pop_anim", None)
            if anim is not None:
                anim.stop()
                self._pop_anim = None
                self.setWindowOpacity(1.0)
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag = None
        super().mouseReleaseEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        self._center_on_parent()
        pop_in_window(self)


class DownloadProgressDialog(QDialog):
    """无边框「下载更新」小窗：进度条 + 已下载体积 + 取消，样式与启动器一致"""

    cancelled = Signal()

    QSS = """
#DlCard {{ background: #ffffff; border: 1px solid {line}; border-radius: 14px; }}
#DlTitle {{ color: {accent}; font-size: 19px; font-weight: bold; background: transparent; }}
#DlVer {{ color: #8a9590; font-size: 12px; background: transparent; }}
#DlPct {{ color: {accent_dark}; font-size: 13px; font-weight: bold; background: transparent; }}
#DlSize {{ color: #7b858c; font-size: 12px; background: transparent; }}
#DlBar {{
  background: {surface}; border: none; border-radius: 7px;
}}
#DlBar::chunk {{ background: {accent}; border-radius: 7px; }}
#DlCancel {{
  background: #ffffff; color: #4a545b; border: 1px solid {line};
  border-radius: 8px; padding: 7px 20px; font-size: 13px;
}}
#DlCancel:hover {{ background: #f7f5ee; }}
#DlCancel:disabled {{ color: #b9c0bb; border-color: {line_soft}; }}
"""

    def __init__(self, parent, current="", latest=""):
        super().__init__(parent)
        c = theme_colors()
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(self.QSS.format(**c))
        self._drag = None
        self._done = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)

        card = QFrame()
        card.setObjectName("DlCard")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(20, 30, 40, 70))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        cl = QVBoxLayout(card)
        cl.setContentsMargins(26, 22, 26, 18)
        cl.setSpacing(0)

        title = QLabel(tr("正在下载更新…"))
        title.setObjectName("DlTitle")
        cl.addWidget(title)

        if current and latest:
            cl.addSpacing(6)
            ver = QLabel(f"v{current}　→　v{latest}")
            ver.setObjectName("DlVer")
            cl.addWidget(ver)

        cl.addSpacing(16)
        self.bar = QProgressBar()
        self.bar.setObjectName("DlBar")
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(14)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        cl.addWidget(self.bar)

        cl.addSpacing(10)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.pct = QLabel("0%")
        self.pct.setObjectName("DlPct")
        self.size_txt = QLabel(tr("准备中…"))
        self.size_txt.setObjectName("DlSize")
        self.size_txt.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.pct)
        row.addStretch(1)
        row.addWidget(self.size_txt)
        cl.addLayout(row)

        cl.addSpacing(18)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.cancel = QPushButton(tr("取消"))
        self.cancel.setObjectName("DlCancel")
        self.cancel.setCursor(Qt.PointingHandCursor)
        self.cancel.clicked.connect(self._cancel)
        btns.addWidget(self.cancel)
        cl.addLayout(btns)

        self.setFixedWidth(430)
        self.adjustSize()
        self._center_on_parent()

    # ---- 外部接口 ----
    def set_progress(self, got, total):
        if total:
            pct = max(0, min(100, int(got * 100 / total)))
            if self.bar.maximum() != 100:
                self.bar.setRange(0, 100)
            self.bar.setValue(pct)
            self.pct.setText(f"{pct}%")
            self.size_txt.setText(f"{got / 1048576:.1f} / {total / 1048576:.1f} MB")
        else:
            # 未知总长：忙碌指示，只报已下载体积
            if self.bar.maximum() != 0:
                self.bar.setRange(0, 0)
            self.pct.setText("")
            self.size_txt.setText(f"{got / 1048576:.1f} MB")

    def mark_done(self):
        """正常结束（成功/失败）时先标记，避免关闭被当成用户取消"""
        self._done = True

    def _cancel(self):
        if self._done:
            return
        self._done = True
        self.cancel.setEnabled(False)
        self.cancel.setText(tr("正在取消…"))
        self.cancelled.emit()

    def keyPressEvent(self, e):
        # Esc = 取消下载（不能直接关窗，要等线程收尾后由调用方关）
        if e.key() == Qt.Key_Escape:
            self._cancel()
            e.accept()
            return
        super().keyPressEvent(e)

    def closeEvent(self, e):
        if not self._done:
            # 直接关窗也当成取消：先留住窗口，等下载线程停下再关
            e.ignore()
            self._cancel()
            return
        super().closeEvent(e)

    def _center_on_parent(self):
        p = self.parentWidget()
        if p is None:
            return
        g = p.window().frameGeometry()
        self.move(g.center().x() - self.width() // 2,
                  g.center().y() - self.height() // 2)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            anim = getattr(self, "_pop_anim", None)
            if anim is not None:
                anim.stop()
                self._pop_anim = None
                self.setWindowOpacity(1.0)
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag = None
        super().mouseReleaseEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        self._center_on_parent()
        pop_in_window(self)


class SponsorDialog(QDialog):
    """赞助弹窗：展示微信赞赏码 + 说明，无需联网"""

    QSS = """
#SpCard {{ background: #ffffff; border: 1px solid {line}; border-radius: 14px; }}
#SpTitle {{ color: {accent}; font-size: 19px; font-weight: bold; background: transparent; }}
#SpRule {{ background: {accent}; border: none; }}
#SpBody {{ color: #4a545b; font-size: 13px; background: transparent; }}
#SpTip {{ color: #8a958e; font-size: 12px; background: transparent; }}
#SpNote {{
  background: {surface}; border: 1px solid {line_soft}; border-radius: 10px;
  padding: 8px 12px; color: #6b7770; font-size: 12px;
}}
#SpClose {{
  background: {accent}; color: #ffffff; border: none;
  border-radius: 8px; padding: 8px 22px; font-size: 13px;
}}
#SpClose:hover {{ background: {accent_hover}; }}
#SpWeb {{
  background: #ffffff; color: #4a545b; border: 1px solid {line};
  border-radius: 8px; padding: 8px 16px; font-size: 13px;
}}
#SpWeb:hover {{ background: #f7f5ee; }}
"""

    def __init__(self, parent):
        super().__init__(parent)
        c = theme_colors()
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setStyleSheet(self.QSS.format(**c))
        self._drag = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)

        card = QFrame()
        card.setObjectName("SpCard")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(20, 30, 40, 70))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        cl = QVBoxLayout(card)
        cl.setContentsMargins(26, 20, 26, 18)
        cl.setSpacing(0)

        title = QLabel(tr("赞助支持"))
        title.setObjectName("SpTitle")
        cl.addWidget(title)
        cl.addSpacing(10)

        rule = QFrame()
        rule.setObjectName("SpRule")
        rule.setFixedHeight(2)
        cl.addWidget(rule)
        cl.addSpacing(16)

        body = QLabel(tr("Aero Launcher 完全免费，没有广告、没有内购、没有功能锁。\n"
                      "如果它帮你省了折腾的时间，可以扫码请我喝杯咖啡。"))
        body.setObjectName("SpBody")
        body.setWordWrap(True)
        cl.addWidget(body)
        cl.addSpacing(14)

        qr = QLabel()
        qr.setAlignment(Qt.AlignCenter)
        qr.setFixedSize(264, 264)          # 固定尺寸，避免被布局压缩裁掉二维码
        qr.setScaledContents(False)
        pm = QPixmap(SPONSOR_QR_PATH)
        if pm.isNull():
            qr.setText(tr("赞赏码图片缺失"))
            qr.setStyleSheet("color:#8a958e;font-size:12px;")
        else:
            qr.setPixmap(pm.scaled(264, 264, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        cl.addWidget(qr, 0, Qt.AlignHCenter)
        cl.addSpacing(6)

        tip = QLabel(tr("微信扫一扫 · 长按图片可识别 · 金额随意"))
        tip.setObjectName("SpTip")
        tip.setAlignment(Qt.AlignCenter)
        cl.addWidget(tip)
        cl.addSpacing(14)

        note = QLabel(tr("赞赏完全自愿，不换取任何游戏内容、特权或优先支持；\n款项只用于服务器续费与后续开发维护。"))
        note.setObjectName("SpNote")
        note.setWordWrap(True)
        cl.addWidget(note)
        cl.addSpacing(16)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        web = QPushButton(tr("在网页打开"))
        web.setObjectName("SpWeb")
        web.setCursor(Qt.PointingHandCursor)
        web.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(SPONSOR_URL)))
        close = QPushButton(tr("关闭"))
        close.setObjectName("SpClose")
        close.setCursor(Qt.PointingHandCursor)
        close.setDefault(True)
        close.clicked.connect(self.accept)
        row.addWidget(web)
        row.addWidget(close)
        cl.addLayout(row)

        self.setFixedWidth(400)
        self.adjustSize()
        self._center_on_parent()

    def _center_on_parent(self):
        p = self.parentWidget()
        if p is None:
            return
        g = p.window().frameGeometry()
        self.move(g.center().x() - self.width() // 2,
                  g.center().y() - self.height() // 2)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            anim = getattr(self, "_pop_anim", None)
            if anim is not None:
                anim.stop()
                self._pop_anim = None
                self.setWindowOpacity(1.0)
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag = None
        super().mouseReleaseEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        self._center_on_parent()
        pop_in_window(self)


# ---------------- 开服务器页面 ----------------
SRV_QSS = """
#SrvCard { background: #ffffff; border: 1px solid rgba(0,0,0,0.10); border-radius: 14px; }
#SrvConsole { background: #1b211e; color: #cfe8d6; border: none; border-radius: 12px;
              padding: 10px; font-family: Consolas, 'Courier New', monospace; font-size: 12px; }
#SrvCmd { background: #f4f6f2; border: 1px solid rgba(0,0,0,0.12); border-radius: 10px;
          padding: 6px 10px; }
"""


class SrvConsole(QPlainTextEdit):
    """控制台：窗口变矮时优先压它，而不是把按钮文字挤掉（sizeHint 给最小值）"""

    def sizeHint(self):
        s = super().sizeHint()
        return QSize(s.width(), max(105, self.minimumHeight()))


class ServerPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._state = {}
        self._versions_loaded = False
        self.setStyleSheet(SRV_QSS)

        # 外层：标题固定、卡片可滚动、控制台与指令栏固定在下面
        # 这样窗口变矮时不会把按钮压扁（文字被切掉），而是滚动卡片区
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 22, 26, 8)
        outer.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel(tr("开服务器"))
        title.setObjectName("Title")
        head.addWidget(title)
        head.addStretch()
        tip = QLabel(tr("启动器只管下载、内存和启停，其余配置改 server 文件夹"))
        tip.setObjectName("Hint")
        tip.setWordWrap(True)
        tip.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        tip.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        head.addWidget(tip, 1)
        head.setContentsMargins(0, 0, 0, 0)
        headw = QWidget()
        headw.setLayout(head)
        headw.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._headw = headw
        outer.addWidget(headw)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }")
        body = QWidget()
        self._body = body
        scroll.setWidget(body)
        scroll.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        outer.addWidget(scroll)

        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(10)

        # ---- 卡片 1：安装与启动 ----
        c1 = QFrame()
        c1.setObjectName("SrvCard")
        l1 = QVBoxLayout(c1)
        l1.setContentsMargins(18, 12, 18, 12)
        l1.setSpacing(7)

        row1 = QHBoxLayout()
        lb = QLabel(tr("服务端版本"))
        lb.setObjectName("FieldLabel")
        row1.addWidget(lb)
        self.combo = RoundCombo()
        self.combo.setMinimumWidth(160)
        row1.addWidget(self.combo)
        self.btn_refresh_ver = QPushButton(tr("刷新"))
        self.btn_refresh_ver.setObjectName("GhostBtn")
        self.btn_refresh_ver.setCursor(Qt.PointingHandCursor)
        self.btn_refresh_ver.clicked.connect(self.load_versions)
        row1.addWidget(self.btn_refresh_ver)
        self.status = QLabel(tr("正在获取状态…"))
        self.status.setObjectName("Hint")
        row1.addStretch()
        row1.addWidget(self.status)
        l1.addLayout(row1)

        row2 = QHBoxLayout()
        lb2 = QLabel(tr("内存分配"))
        lb2.setObjectName("FieldLabel")
        row2.addWidget(lb2)
        self.ram = QSlider(Qt.Horizontal)
        self.ram.setRange(512, 8192)
        self.ram.setSingleStep(256)
        self.ram.setPageStep(1024)
        self.ram.setValue(2048)
        row2.addWidget(self.ram, 1)
        self.ram_label = QLabel("2048 MB")
        self.ram_label.setMinimumWidth(80)
        row2.addWidget(self.ram_label)
        l1.addLayout(row2)
        self.ram.valueChanged.connect(
            lambda v: self.ram_label.setText(f"{v} MB ({v / 1024:.1f} GB)" if v >= 1024
                                             else f"{v} MB"))

        row3 = QHBoxLayout()
        self.eula = QCheckBox(tr("我已阅读并同意 Minecraft EULA（必须同意才能开服）"))
        self.eula.setCursor(Qt.PointingHandCursor)
        eula_link = QPushButton(tr("查看 EULA"))
        eula_link.setObjectName("GhostBtn")
        eula_link.setCursor(Qt.PointingHandCursor)
        eula_link.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl("https://aka.ms/MinecraftEULA")))
        row3.addWidget(self.eula)
        row3.addWidget(eula_link)
        row3.addStretch()
        l1.addLayout(row3)

        row4 = QHBoxLayout()
        self.btn_install = QPushButton(tr("下载服务端"))
        self.btn_install.setObjectName("PrimaryBtn")
        self.btn_install.setCursor(Qt.PointingHandCursor)
        self.btn_install.clicked.connect(self.do_install)
        row4.addWidget(self.btn_install)
        self.btn_start = QPushButton(tr("启动服务器"))
        self.btn_start.setObjectName("PrimaryBtn")
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.clicked.connect(self.do_start)
        row4.addWidget(self.btn_start)
        self.btn_stop = QPushButton(tr("停止"))
        self.btn_stop.setObjectName("DangerBtn")
        self.btn_stop.setCursor(Qt.PointingHandCursor)
        self.btn_stop.clicked.connect(self.do_stop)
        row4.addWidget(self.btn_stop)
        self.btn_folder = QPushButton(tr("打开 server 文件夹"))
        self.btn_folder.setObjectName("GhostBtn")
        self.btn_folder.setCursor(Qt.PointingHandCursor)
        self.btn_folder.clicked.connect(lambda: ApiThread("POST", "/api/server/open_folder").start())
        row4.addWidget(self.btn_folder)
        row4.addStretch()
        l1.addLayout(row4)
        self._c1 = c1
        lay.addWidget(c1)

        # ---- 卡片 2：联机（内网穿透） ----
        c2 = QFrame()
        c2.setObjectName("SrvCard")
        l2 = QVBoxLayout(c2)
        l2.setContentsMargins(18, 12, 18, 12)
        l2.setSpacing(7)
        l2t = QHBoxLayout()
        t2 = QLabel(tr("让外网玩家进来"))
        t2.setObjectName("RowName")
        l2t.addWidget(t2)
        l2t.addStretch()
        l2.addLayout(l2t)

        row5 = QHBoxLayout()
        self.btn_upnp = QPushButton(tr("自动映射端口 (UPnP)"))
        self.btn_upnp.setObjectName("GhostBtn")
        self.btn_upnp.setCursor(Qt.PointingHandCursor)
        self.btn_upnp.clicked.connect(self.do_upnp)
        row5.addWidget(self.btn_upnp)
        self.upnp_status = QLabel(tr("把路由器端口映射到本机，延迟最低；路由器需开启 UPnP 且宽带有公网 IP"))
        self.upnp_status.setObjectName("Hint")
        row5.addWidget(self.upnp_status, 1)
        l2.addLayout(row5)

        row6 = QHBoxLayout()
        self.btn_playit = QPushButton(tr("启动 playit.gg 穿透"))
        self.btn_playit.setObjectName("GhostBtn")
        self.btn_playit.setCursor(Qt.PointingHandCursor)
        self.btn_playit.clicked.connect(self.do_playit)
        row6.addWidget(self.btn_playit)
        self.playit_status = QLabel(tr("UPnP 不可用时的兜底：免费、只需邮箱注册，自动分配一个外网地址"))
        self.playit_status.setObjectName("Hint")
        row6.addWidget(self.playit_status, 1)
        l2.addLayout(row6)

        row6b = QHBoxLayout()
        self.btn_p2p = QPushButton(tr("开启 P2P 直连"))
        self.btn_p2p.setObjectName("GhostBtn")
        self.btn_p2p.setCursor(Qt.PointingHandCursor)
        self.btn_p2p.clicked.connect(self.do_p2p)
        row6b.addWidget(self.btn_p2p)
        self.btn_p2p_join = QPushButton(tr("加入房间…"))
        self.btn_p2p_join.setObjectName("GhostBtn")
        self.btn_p2p_join.setCursor(Qt.PointingHandCursor)
        self.btn_p2p_join.clicked.connect(self.do_p2p_join)
        row6b.addWidget(self.btn_p2p_join)
        self.btn_p2p_copy = QPushButton(tr("复制邀请"))
        self.btn_p2p_copy.setObjectName("GhostBtn")
        self.btn_p2p_copy.setCursor(Qt.PointingHandCursor)
        self.btn_p2p_copy.clicked.connect(self.do_p2p_copy)
        self.btn_p2p_copy.setVisible(False)
        row6b.addWidget(self.btn_p2p_copy)
        self.p2p_status = QLabel(tr("朋友也装了启动器就走这个：打洞直连，延迟比 playit 低"))
        self.p2p_status.setObjectName("Hint")
        self.p2p_status.setWordWrap(True)
        row6b.addWidget(self.p2p_status, 1)
        l2.addLayout(row6b)
        self._c2 = c2
        lay.addWidget(c2)
        lay.addStretch()

        # ---- 控制台 ----
        self.console = SrvConsole()
        self.console.setObjectName("SrvConsole")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(500)
        outer.addWidget(self.console)

        row7 = QHBoxLayout()
        self.cmd = QLineEdit()
        self.cmd.setObjectName("SrvCmd")
        self.cmd.setPlaceholderText(tr("向服务器发送指令，如 whitelist add 玩家名 / stop"))
        self.cmd.returnPressed.connect(self.do_command)
        row7.addWidget(self.cmd, 1)
        self.btn_send = QPushButton(tr("发送"))
        self.btn_send.setObjectName("GhostBtn")
        self.btn_send.setCursor(Qt.PointingHandCursor)
        self.btn_send.clicked.connect(self.do_command)
        row7.addWidget(self.btn_send)
        outer.addLayout(row7)

        # 防止窗口变矮时布局把控件压到文字都装不下
        self.console.setMinimumHeight(105)
        self.cmd.setMinimumHeight(34)
        self.combo.setMinimumHeight(34)
        for b in self.findChildren(QPushButton):
            b.setMinimumHeight(b.sizeHint().height())

        # 状态轮询
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_state)
        self._timer.start(2500)
        self._last_log_len = -1

    # ---------- 布局 ----------
    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_console()
        # 首次 resize 时卡片还没量好高，下一轮事件循环再校正一次
        QTimer.singleShot(0, self._fit_console)

    def _fit_console(self):
        """控制台吃掉多余高度；空间不够时先压控制台，还不行就让卡片区滚动"""
        body = getattr(self, "_body", None)
        if body is None or not hasattr(self, "console"):
            return
        ol = self.layout()
        m = ol.contentsMargins()
        fixed = m.top() + m.bottom() + ol.spacing() * max(0, ol.count() - 1)
        fixed += self._headw.sizeHint().height() + self.cmd.sizeHint().height()
        # 用两张卡片自己的尺寸算，别用 body.sizeHint()：它随滚动条有无而变，会算歪
        cards = (self._c1.sizeHint().height() + self._c2.sizeHint().height()
                 + 2 * body.layout().spacing())
        target = max(105, self.height() - cards - fixed)
        if self.console.maximumHeight() != target:
            self.console.setFixedHeight(target)

    # ---------- 数据 ----------
    def refresh_state(self):
        for path, slot in (("/api/server/state", self._apply_state),
                           ("/api/server/log", self._apply_log)):
            th = ApiThread("GET", path, timeout=8)
            th.done.connect(slot)
            th.start()

    def load_versions(self):
        def done(d):
            if d.get("ok"):
                cur = self.combo.currentText()
                self.combo.clear()
                self.combo.addItems(d.get("versions", []))
                if cur:
                    self.combo.setCurrentText(cur)
                if not self._versions_loaded:
                    self._versions_loaded = True
                    # 默认选中已安装的版本，否则选最新正式版
                    v = (self._state or {}).get("version")
                    if v:
                        self.combo.setCurrentText(v)
        th = ApiThread("GET", "/api/server/versions", timeout=20)
        th.done.connect(done)
        th.start()

    def _apply_state(self, d):
        if not d.get("ok"):
            return
        self._state = d
        running = d.get("running")
        installing = d.get("installing")
        installed = d.get("installed")
        if running:
            txt = f"{tr('● 运行中 · ')}{d.get('version') or ''}{tr(' · 端口 ')}{d.get('port')}"
        elif installing:
            txt = tr("● 正在下载服务端…")
        elif installed:
            jar_mb = d.get("jar_size", 0) / 1048576
            txt = f"{tr('○ 已安装 ')}{d.get('version') or ''}（{jar_mb:.0f}{tr(' MB）· 未运行')}"
        else:
            txt = tr("○ 还没有安装服务端，选好版本点「下载服务端」")
        self.status.setText(txt)
        self.status.setStyleSheet("color:#1fa04a;" if running else "")

        # EULA：没同意过就把勾选框保持用户操作；已同意则强制勾上
        if d.get("eula") and not self.eula.isChecked():
            self.eula.setChecked(True)

        if not self.ram.isSliderDown():
            self.ram.setValue(int(d.get("ram") or 2048))

        self.btn_install.setEnabled(not running and not installing)
        self.btn_install.setText(tr("正在下载…") if installing else tr("下载服务端"))
        self.btn_start.setEnabled(bool(installed) and not running and not installing)
        self.btn_stop.setEnabled(bool(running))
        self.btn_stop.setText(tr("停止") if running else tr("已停止"))
        self.btn_send.setEnabled(bool(running))
        self.cmd.setEnabled(bool(running))

        # UPnP 状态
        up = d.get("upnp") or ""
        if up == "ok":
            self.upnp_status.setText(
                f"{tr('✓ 已映射：外网玩家用 ')}{d.get('upnp_ip')}:{d.get('upnp_port')}{tr(' 连接')}")
            self.upnp_status.setStyleSheet("color:#1fa04a;")
            self.btn_upnp.setEnabled(False)
        elif up == "failed":
            self.upnp_status.setText(
                tr("✗ UPnP 不可用（路由器未开或无公网 IP），建议用下面的 playit.gg"))
            self.upnp_status.setStyleSheet("color:#c0392b;")
            self.btn_upnp.setEnabled(True)
        else:
            self.upnp_status.setText(
                tr("把路由器端口映射到本机，延迟最低；路由器需开启 UPnP 且宽带有公网 IP"))
            self.upnp_status.setStyleSheet("")
            self.btn_upnp.setEnabled(True)

        # P2P 直连状态（放在 playit 之前：playit 下载中会 return，别让它跳过这里）
        p2p_on = bool(d.get("p2p_running"))
        p2p_role = d.get("p2p_role") or ""
        p2p_online = bool(d.get("p2p_online"))
        p2p_mode = d.get("p2p_mode") or ""
        room = d.get("p2p_room") or ""
        sec = d.get("p2p_secret") or ""
        pdl = d.get("p2p_dl") or {}
        self.btn_p2p.setEnabled(True)
        self.btn_p2p_join.setEnabled(True)
        if pdl.get("got"):
            got = pdl["got"] / 1048576
            tot = (pdl.get("total") or 0) / 1048576
            self.p2p_status.setText(
                f"{tr('正在下载 P2P 组件：')}{got:.1f} / {tot:.1f} MB" if tot
                else f"{tr('正在下载 P2P 组件：')}{got:.1f} MB")
            self.p2p_status.setStyleSheet("")
            self.btn_p2p.setEnabled(False)
            self.btn_p2p.setText(tr("下载中…"))
            self.btn_p2p_copy.setVisible(False)
        elif p2p_on and p2p_role == "host":
            self.btn_p2p.setText(tr("停止 P2P"))
            self.btn_p2p_copy.setVisible(True)
            if p2p_online:
                tag = tr("直连（P2P）") if p2p_mode == "p2p" else tr("经共享节点中转")
                self.p2p_status.setText(f"{tr('● 已连通 · ')}{tag}{tr(' · 房间 ')}{room}")
                self.p2p_status.setStyleSheet("color:#1fa04a;")
            else:
                self.p2p_status.setText(
                    f"{tr('○ 已开启，等朋友进来 · 房间 ')}{room}{tr(' · 密码 ')}{sec}")
                self.p2p_status.setStyleSheet("")
        elif p2p_on and p2p_role == "join":
            self.btn_p2p.setText(tr("开启 P2P 直连"))
            self.btn_p2p_copy.setVisible(False)
            lp = d.get("p2p_local_port") or d.get("port") or 25565
            if p2p_online:
                tag = tr("直连（P2P）") if p2p_mode == "p2p" else tr("经共享节点中转")
                self.p2p_status.setText(
                    f"{tr('● 已连上房主 · ')}{tag}{tr(' · 游戏里连 127.0.0.1:')}{lp}")
                self.p2p_status.setStyleSheet("color:#1fa04a;")
            else:
                self.p2p_status.setText(tr("○ 正在连房主…（房间号或密码不对会一直连不上）"))
                self.p2p_status.setStyleSheet("")
        else:
            self.btn_p2p.setText(tr("开启 P2P 直连"))
            self.btn_p2p_copy.setVisible(False)
            self.p2p_status.setText(tr("朋友也装了启动器就走这个：打洞直连，延迟比 playit 低"))
            self.p2p_status.setStyleSheet("")

        # playit 状态
        dl = d.get("playit_dl") or {}
        if dl.get("got"):
            got = dl["got"] / 1048576
            tot = (dl.get("total") or 0) / 1048576
            self.playit_status.setText(
                f"{tr('正在下载 playit 代理：')}{got:.1f} / {tot:.1f} MB" if tot
                else f"{tr('正在下载 playit 代理：')}{got:.1f} MB")
            self.playit_status.setStyleSheet("")
            self.btn_playit.setEnabled(False)
            self.btn_playit.setText(tr("下载中…"))
            return
        self.btn_playit.setEnabled(True)
        if d.get("playit_running"):
            claim = d.get("playit_claim") or ""
            if claim:
                self.playit_status.setText(f"{tr('等待绑定，点击打开：')}{claim}")
                self._claim_url = claim
                if not self._claim_connected:
                    self.playit_status.linkActivated.connect(
                        lambda u: QDesktopServices.openUrl(QUrl(u)))
                    self.playit_status.setText(
                        f"{tr('等待绑定，')}<a style='color:#1fa04a' href='{claim}'>{tr('打开绑定链接')}</a>")
                    self._claim_connected = True
            else:
                self.playit_status.setText(tr("● playit 已启动，在 playit.gg 网站上给服务器添加 Minecraft 隧道"))
                self.playit_status.setStyleSheet("color:#1fa04a;")
            self.btn_playit.setText(tr("停止 playit"))
        else:
            self.playit_status.setText(
                tr("UPnP 不可用时的兜底：免费、只需邮箱注册，自动分配一个外网地址"))
            self.playit_status.setStyleSheet("")
            self.btn_playit.setText(tr("启动 playit.gg 穿透"))

    _claim_connected = False
    _claim_url = ""

    def _apply_log(self, d):
        if not d.get("ok"):
            return
        text = d.get("log") or ""
        if not text:
            text = tr("（服务器控制台）启动服务器后，这里会实时显示服务端输出。")
        if text == self.console.toPlainText():
            return
        self.console.setPlainText(text)
        sb = self.console.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._check_log_errors(text)

    def _check_log_errors(self, text):
        """扫一眼控制台尾部：发现新报错就在 toast 上挂一条，点击看分析"""
        tail = text[-6000:]
        # 只认真正的错误级别，普通 WARN 里带个 error 字样不算
        if not re.search(r"ERROR|Exception|FATAL|Caused by", tail):
            return
        res = error_analyzer.analyze(tail)
        if not res:
            return
        if res["title"] == getattr(self, "_last_err_title", None):
            return
        self._last_err_title = res["title"]
        self.win._pending_analysis = (tail, tr("服务器日志"))
        self.win.toast.show_info(
            tr("服务器日志有新报错"),
            f"{tr(res['title'])}{tr('　·　点这条提示看分析')}",
            6000)

    # ---------- 动作 ----------
    def do_install(self):
        vid = self.combo.currentText().strip()
        if not vid:
            self.win.toast.show_result(False, tr("请先选一个版本"))
            return
        ApiThread("POST", "/api/server/install", {"version": vid}, timeout=15).start()
        self.btn_install.setEnabled(False)
        self.btn_install.setText(tr("正在下载…"))

    def do_start(self):
        self._last_err_title = None   # 新一轮启动，重新允许报错提示
        th = ApiThread("POST", "/api/server/start",
                       {"ram": self.ram.value(),
                        "accept_eula": self.eula.isChecked()},
                       timeout=120)
        th.done.connect(self._start_done)
        th.start()
        self.btn_start.setEnabled(False)

    def _start_done(self, d):
        if not d.get("ok"):
            self.win.toast.show_result(False, d.get("error", tr("启动失败")))
        else:
            # 接口只是把 java 拉起来就返回了，这时还没进游戏世界，说“完成”会误导
            self.win.toast.show_info(
                tr("正在启动服务器"),
                tr("首次启动要生成世界，稍等一会儿；状态变成「运行中」就好了"))
        self.refresh_state()

    def do_stop(self):
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText(tr("正在停止…"))
        self.status.setText(tr("● 正在保存世界并停止…"))
        th = ApiThread("POST", "/api/server/stop", {}, timeout=40)
        th.done.connect(lambda _d: self.refresh_state())
        th.start()

    def do_upnp(self):
        self.btn_upnp.setEnabled(False)
        self.upnp_status.setText(tr("正在尝试映射…"))
        ApiThread("POST", "/api/server/upnp", {}, timeout=10).start()
        QTimer.singleShot(1500, self.refresh_state)

    def do_playit(self):
        if self.btn_playit.text().startswith(tr("停止")):
            ApiThread("POST", "/api/server/playit/stop", {}, timeout=10).start()
        else:
            self.btn_playit.setEnabled(False)
            self.playit_status.setText(tr("正在准备 playit（首次要下载程序）…"))
            ApiThread("POST", "/api/server/playit/start", {}, timeout=15).start()
        QTimer.singleShot(1800, self.refresh_state)

    # ---------- P2P 直连 ----------
    def do_p2p(self):
        if (self._state or {}).get("p2p_running"):
            self.btn_p2p.setEnabled(False)
            th = ApiThread("POST", "/api/server/p2p/stop", {}, timeout=15)
            th.done.connect(lambda _d: self.refresh_state())
            th.start()
            return
        self.btn_p2p.setEnabled(False)
        self.p2p_status.setText(tr("正在准备 P2P 连接（首次要下载约 30 MB 组件）…"))
        th = ApiThread("POST", "/api/server/p2p/start", {"role": "host"}, timeout=20)
        th.done.connect(self._p2p_started)
        th.start()

    def _p2p_started(self, d):
        if not d.get("ok"):
            self.win.toast.show_result(False, d.get("error", tr("P2P 启动失败")))
        self.refresh_state()

    def do_p2p_join(self):
        if (self._state or {}).get("p2p_running"):
            self.win.toast.show_result(False, tr("先停止当前的 P2P 连接"))
            return
        dlg = JoinRoomDialog(self)
        if not dlg.exec():
            return
        room, sec = dlg.values()
        if not room or not sec:
            return
        self.btn_p2p_join.setEnabled(False)
        self.p2p_status.setText(tr("正在连接房主…"))
        th = ApiThread("POST", "/api/server/p2p/start",
                       {"role": "join", "room": room.strip(), "secret": sec.strip()},
                       timeout=20)
        th.done.connect(self._p2p_joined)
        th.start()

    def _p2p_joined(self, d):
        self.btn_p2p_join.setEnabled(True)
        if not d.get("ok"):
            self.win.toast.show_result(False, d.get("error", tr("加入失败")))
        elif d.get("local_port") and not d.get("downloading"):
            self.win.toast.show_result(
                True, f"{tr('已加入，游戏里连 127.0.0.1:')}{d['local_port']}")
        self.refresh_state()

    def do_p2p_copy(self):
        st = self._state or {}
        room = st.get("p2p_room") or ""
        sec = st.get("p2p_secret") or ""
        port = st.get("port") or 25565
        if not room or not sec:
            self.win.toast.show_result(False, tr("还没有房间信息"))
            return
        txt = (tr("来我的世界服务器玩：\n"
                  "1. 打开 Aero Launcher →「开服务器」→ 点「加入房间…」\n")
               + f"{tr('2. 房间号：')}{room}\n"
               + f"{tr('3. 密码：')}{sec}\n"
               + f"{tr('4. 连上后游戏里连 127.0.0.1:')}{port}\n"
               + tr("（第一次会自动下载约 30 MB 的连接组件）"))
        QApplication.clipboard().setText(txt)
        self.win.toast.show_result(True, tr("邀请信息已复制，发给朋友就行"))

    def do_command(self):
        cmd = self.cmd.text().strip()
        if not cmd:
            return
        self.cmd.clear()
        ApiThread("POST", "/api/server/command", {"command": cmd}, timeout=8).start()


# ---------------- 主窗口 ----------------
class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Aero Launcher")
        # 默认开得够高，页面内容才不会挤在一起（小屏幕按可用桌面尺寸收一下）
        scr = QApplication.primaryScreen()
        avail = scr.availableGeometry() if scr else None
        dw, dh = 1160, 760
        if avail is not None:
            dw = min(dw, max(900, avail.width() - 60))
            dh = min(dh, max(600, avail.height() - 70))
        self.resize(dw, dh)
        self.setMinimumSize(880, 560)
        self.setStyleSheet(QSS)
        self.settings = QSettings("WJH", "MCLauncher")
        self.load_theme()
        self.setStyleSheet(build_qss())
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)  # 圆角外露出透明
        self.setWindowIcon(app_icon())

        self.installed = []
        self.version = ""
        self.accounts = []
        self.selected_account = None
        self._drag = None
        self._last_log_len = -1

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 左侧图标栏 ----
        rail = QWidget()
        rail.setObjectName("Rail")
        rail.setFixedWidth(84)
        rl = QVBoxLayout(rail)
        rl.setContentsMargins(18, 18, 18, 16)
        rl.setSpacing(8)

        # 品牌标志：点了回主页（无边框窗口，标题栏没有名字，这里给个可识别的标记）
        self.btn_logo = QPushButton()
        self.btn_logo.setObjectName("RailLogo")
        self.btn_logo.setFixedSize(46, 46)   # 与 RailButton 同宽，保证竖向对齐
        self.btn_logo.setIcon(QIcon(brand_pixmap(38)))
        self.btn_logo.setIconSize(QSize(38, 38))
        self.btn_logo.setCursor(Qt.PointingHandCursor)
        self.btn_logo.setToolTip(f"Aero Launcher v{APP_VERSION}")
        self.btn_logo.clicked.connect(lambda: self.goto(0))
        rl.addWidget(self.btn_logo)
        rl.addSpacing(4)

        self.btn_plus = QPushButton()
        self.btn_plus.setObjectName("RailPlus")
        self.btn_plus.setFixedSize(44, 44)
        self.btn_plus.setIcon(QIcon(make_icon("plus", 20, "#31373d", 1.7)))
        self.btn_plus.setIconSize(QSize(20, 20))
        self.btn_plus.setCursor(Qt.PointingHandCursor)
        self.btn_plus.setToolTip(tr("添加或选择账户"))
        self.btn_plus.clicked.connect(lambda: self.goto(2))
        self.btn_plus.hide()  # 有账户时隐藏（on_accounts_updated 控制显隐）
        rl.addWidget(self.btn_plus)

        rl.addSpacing(26)
        self.nav = []
        self.nav_pages = []
        for icon_name, idx, tip in [("home", 0, tr("主页")), ("download", 1, tr("下载游戏")),
                                    ("grid", 5, tr("游戏版本")), ("server", 6, tr("开服务器")),
                                    ("news", 7, tr("资讯"))]:
            b = RailButton(icon_name)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, i=idx: self.goto(i))
            rl.addWidget(b)
            self.nav.append(b)
            self.nav_pages.append(idx)
        # 赞助入口（爱发电）
        self.btn_sponsor = RailButton("heart")
        self.btn_sponsor.setCheckable(False)
        self.btn_sponsor.setToolTip(tr("赞助支持（微信赞赏码）"))
        self.btn_sponsor.clicked.connect(self.show_sponsor)
        rl.addWidget(self.btn_sponsor)
        self.btn_account = RailButton("person")
        self.btn_account.setToolTip(tr("账户"))
        self.btn_account.clicked.connect(lambda: self.goto(2))
        rl.addStretch()
        rl.addWidget(self.btn_account)
        self.btn_settings = RailButton("gear")
        self.btn_settings.setToolTip(tr("设置"))
        self.btn_settings.clicked.connect(lambda: self.goto(3))
        rl.addWidget(self.btn_settings)
        root.addWidget(rail)

        # ---- 右侧内容（渐变背景）----
        self.content = GradientBG()
        cl = QVBoxLayout(self.content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        top = QHBoxLayout()
        top.setContentsMargins(0, 10, 10, 0)
        top.addStretch()
        self.btn_min = QPushButton()
        self.btn_min.setFixedSize(30, 30)
        self.btn_min.setIcon(QIcon(self._dash_pixmap()))
        self.btn_min.setIconSize(QSize(16, 16))
        self.btn_min.setStyleSheet(
            "background: rgba(255,255,255,0.62); border:none; border-radius:15px;")
        self.btn_min.setCursor(Qt.PointingHandCursor)
        self.btn_min.setToolTip(tr("最小化"))
        self.btn_min.clicked.connect(self.showMinimized)
        top.addWidget(self.btn_min)
        self.btn_close = QPushButton()
        self.btn_close.setFixedSize(30, 30)
        self.btn_close.setIcon(QIcon(make_icon("close", 16, "#4a545c", 1.7)))
        self.btn_close.setIconSize(QSize(16, 16))
        self.btn_close.setStyleSheet(
            "background: rgba(255,255,255,0.62); border:none; border-radius:15px;")
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.clicked.connect(self.close)
        top.addWidget(self.btn_close)
        cl.addLayout(top)

        self.stack = QStackedWidget()
        # 只作用于 stack 自身，不能直接写 background（会传染给所有子控件）
        self.stack.setStyleSheet("QStackedWidget { background: transparent; }")
        self.home = HomePage(self)
        self.home.account_clicked.connect(lambda: self.goto(2))
        self.download = DownloadPage(self)
        self.accounts_page = AccountsPage(self)
        self.accounts_page.ok.connect(self.refresh_accounts)
        self.settings_page = SettingsPage(self)
        self.log_page = LogPage(self)
        self.versions_page = VersionsPage(self)
        self.server_page = ServerPage(self)
        self.news_page = NewsPage(self)
        self.mods_page = ModsPage(self)
        for w in (self.home, self.download, self.accounts_page,
                  self.settings_page, self.log_page, self.versions_page,
                  self.server_page, self.news_page, self.mods_page):
            self.stack.addWidget(w)
        cl.addWidget(self.stack, 1)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(32, 0, 28, 22)
        bottom.addStretch()
        self.ver_label = QLabel(tr("未选择版本"))
        self.ver_label.setObjectName("VerLabel")
        bottom.addWidget(self.ver_label)
        bottom.addSpacing(10)
        self.split = SplitLaunch()
        self.split.launch.connect(self.do_launch)
        self.split.btn_more.clicked.connect(self.show_version_menu)
        bottom.addWidget(self.split)
        cl.addLayout(bottom)

        self.toast = Toast(self.content)
        self.toast.clicked.connect(self._toast_clicked)

        # 下载胶囊：直接浮在内容区左下角，不占任何布局格子；没有下载任务时整个隐藏
        self.dl_ball = DLBall(self.content)
        self.dl_ball.clicked.connect(self.toggle_dl_popup)
        self.dl_ball.hide()

        # 下载胶囊的浮层（就浮在内容区上，点空白处收起）
        self.dl_popup = DLPopup(self.content)
        self.dl_popup.hide()
        self._dl_tasks = []
        self._dl_legacy = None
        self._dl_prev_active = set()

        # ESC 取消下载/安装
        self._installing = False
        self._prog = None
        self._esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._esc.activated.connect(self.on_esc_cancel)

        root.addWidget(self.content, 1)
        self._place_dl_ball()
        # 点窗口别处 → 收起浮层（判断放在应用级事件过滤器里，子控件上的点击也能收到）
        QApplication.instance().installEventFilter(self)

        self.poller = StatePoller()
        self.poller.state.connect(self.on_state)
        self.poller.start()

        self.goto(0, animate=False)
        self.refresh_installed()
        self.refresh_accounts()

        # 所有下拉框统一去掉方形弹层外框
        for cb in self.findChildren(QComboBox):
            polish_combo(cb)
        self.setWindowOpacity(THEME["opacity"])
        # 启动后静默检查一次更新
        QTimer.singleShot(2500, lambda: self.check_update(silent=True))

    # ---------- 主题 ----------
    def load_theme(self):
        """从 QSettings 读出上次保存的主题（主题色 / 背景色 / 透明度）"""
        try:
            THEME["accent"] = self.settings.value("theme/accent", DEFAULT_THEME["accent"])
            THEME["bg"] = self.settings.value("theme/bg", DEFAULT_THEME["bg"])
            THEME["opacity"] = float(self.settings.value("theme/opacity",
                                                         DEFAULT_THEME["opacity"]))
            THEME["bg_image"] = str(self.settings.value("theme/bg_image", "") or "")
            THEME["wp"] = str(self.settings.value("theme/wp", "off") or "off")
            if THEME["wp"] not in WP_KINDS:
                THEME["wp"] = "off"
        except Exception:
            THEME.update(DEFAULT_THEME)

        # 动态壁纸：开启动效时用低频定时器驱动重绘（25fps，够顺滑且省 CPU）
        self._wp_timer = QTimer(self)
        self._wp_timer.setInterval(40)
        self._wp_timer.timeout.connect(self.update)
        self._sync_wp_timer()

    def _sync_wp_timer(self):
        on = str(THEME.get("wp") or "off") in WP_KINDS[1:]
        if on and not self._wp_timer.isActive():
            self._wp_timer.start()
        elif not on and self._wp_timer.isActive():
            self._wp_timer.stop()

    def apply_theme(self, accent=None, bg=None, opacity=None, bg_image=None,
                    wp=None, save=True):
        """即时换肤：主题色 / 背景色 / 背景图片 / 动态壁纸 / 窗口透明度"""
        if accent:
            THEME["accent"] = accent
        if bg:
            THEME["bg"] = bg
        if bg_image is not None:
            THEME["bg_image"] = bg_image
        if wp is not None and wp in WP_KINDS:
            THEME["wp"] = wp
        if opacity is not None:
            THEME["opacity"] = max(0.55, min(1.0, float(opacity)))
        self.setStyleSheet(build_qss())
        self.setWindowOpacity(THEME["opacity"])
        for cb in self.findChildren(QComboBox):
            polish_combo(cb)
        sp = getattr(self, "settings_page", None)
        if sp is not None:
            sp.sync_theme_widgets()
        self.update()
        self._sync_wp_timer()
        if save:
            self.settings.setValue("theme/accent", THEME["accent"])
            self.settings.setValue("theme/bg", THEME["bg"])
            self.settings.setValue("theme/bg_image", THEME.get("bg_image", ""))
            self.settings.setValue("theme/wp", THEME.get("wp", "off"))
            self.settings.setValue("theme/opacity", THEME["opacity"])

    # ---------- 更新 ----------
    def update_url(self):
        return (self.settings.value("update/url", "") or DEFAULT_UPDATE_URL).strip()

    def is_beta_channel(self):
        return self.settings.value("update/channel", "stable") == "beta"

    def check_update(self, silent=True):
        base = self.update_url().rsplit("/", 1)
        stable = base[0] + "/update.json" if len(base) == 2 else DEFAULT_UPDATE_URL
        if self.is_beta_channel():
            beta = base[0] + "/beta.json" if len(base) == 2 else \
                DEFAULT_UPDATE_URL.rsplit("/", 1)[0] + "/beta.json"
            self._uc = UpdateCheck(beta, fallback=stable)
        else:
            self._uc = UpdateCheck(stable)
        self._uc.got.connect(lambda d, s=silent: self.on_update_info(d, s))
        self._uc.start()

    def on_update_info(self, d, silent):
        if not d.get("version"):
            if not silent:
                info_card(self, tr("检查更新"),
                                        f"{tr('检查失败：')}{_friendly_net_error(d.get('error', tr('更新源无响应')))}")
            return
        latest = str(d.get("version"))
        if _ver_tuple(latest) <= _ver_tuple(APP_VERSION):
            if not silent:
                info_card(self, tr("检查更新"),
                                        f"{tr('已经是最新版本（v')}{APP_VERSION}）")
            return
        notes = str(d.get("notes") or "").strip()
        # Mac 用户使用清单里的 mac_url（mac 包与 exe 分开发布）
        if backend.SYSTEM_NAME == "osx" and not d.get("mac_url"):
            if not silent:
                info_card(
                    self, tr("检查更新"),
                    f"v{latest}{tr(' 暂未提供 Mac 更新包，请稍后再试或到官网下载。')}")
            return
        tag = tr("（内测通道）") if self.is_beta_channel() else ""
        dlg = UpdateDialog(self, latest=latest, current=APP_VERSION,
                           notes=notes, tag=tag)
        if dlg.exec() == QDialog.Accepted:
            if backend.SYSTEM_NAME == "osx":
                self.start_update_download(d.get("mac_url"),
                                           d.get("mac_sha256", ""), latest)
            else:
                self.start_update_download(d.get("url", ""),
                                           d.get("sha256", ""), latest)

    def start_update_download(self, url, sha256="", latest=""):
        if not getattr(sys, "frozen", False):
            info_card(self, tr("提示"),
                                    f"{tr('开发模式不自动更新，请手动获取\n')}{url}")
            return
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        suffix = ".exe" if os.name == "nt" else ""
        dest = os.path.join(exe_dir, "_update_download" + suffix)
        # 无边框进度小窗：下载中显示百分比与体积，可点取消
        self._prog = DownloadProgressDialog(self, current=APP_VERSION, latest=latest)
        self._prog.setWindowModality(Qt.WindowModal)
        self._prog.cancelled.connect(self.on_update_cancel)
        self._prog.show()
        self._ud = UpdateDownload(url, dest, sha256)
        self._ud.progress.connect(self.on_update_progress)
        self._ud.done.connect(self.on_update_ready)
        self._ud.failed.connect(self.on_update_failed)
        self._ud.cancelled.connect(self.on_update_cancelled)
        self._ud.start()

    def on_update_progress(self, got, total):
        prog = getattr(self, "_prog", None)
        if prog is None:
            return
        prog.set_progress(got, total)

    def on_update_cancel(self):
        ud = getattr(self, "_ud", None)
        if ud is not None and ud.isRunning():
            ud.cancel()
        else:
            # 线程其实已经结束（取消点得太晚），别把窗口留在屏幕上
            self._close_update_prog()

    def _close_update_prog(self):
        prog = getattr(self, "_prog", None)
        self._prog = None
        if prog is not None:
            prog.mark_done()
            prog.close()
            prog.deleteLater()

    def on_update_cancelled(self):
        self._close_update_prog()
        self.toast.show_result(False, tr("已取消更新"))
        QTimer.singleShot(2600, self.toast.hide)

    def on_update_failed(self, err):
        self._close_update_prog()
        self.toast.show_result(False, f"{tr('更新下载失败：')}{_friendly_net_error(err)}")
        _log_update_error(err)
        QTimer.singleShot(6000, self.toast.hide)

    # ---------- 更新安装 ----------
    def _pending_update_file(self):
        """上一次更新没装完时，_update_download.exe 会留在 exe 旁边"""
        if not getattr(sys, "frozen", False):
            return None
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        suffix = ".exe" if os.name == "nt" else ""
        p = os.path.join(exe_dir, "_update_download" + suffix)
        return p if os.path.isfile(p) else None

    def _check_pending_update(self):
        """启动后若发现上次更新没装完，问一句要不要现在装（静默失败过一次的事不能再静默）"""
        p = self._pending_update_file()
        if not p or getattr(self, "_shutting", False):
            return
        ret = QMessageBox.question(
            self, tr("更新未完成"),
            tr("上次下载的新版本没有安装成功，现在重启并完成安装吗？\n\n"
               "（选「否」则继续用当前版本，下次启动还会询问）"),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ret == QMessageBox.Yes:
            self.toast.show_result(True, tr("正在完成上次的更新…"))
            QTimer.singleShot(900, lambda: self._install_new_exe(p))
        else:
            QTimer.singleShot(2600, self.toast.hide)

    def _install_new_exe(self, new_file):
        """生成收尾批处理：等本进程退出 → 替换 exe（带重试）→ 重启 → 自删"""
        exe = os.path.abspath(sys.executable)
        if os.name == "nt":
            bat = os.path.join(os.path.dirname(exe), "_mcl_update.bat")
            # 关键点：
            #  1. 用 ping 代替 timeout（timeout 在无控制台输入时会直接报错）
            #  2. move 带重试——onefile 的引导进程在子进程退出后还要清临时目录，
            #     这期间 exe 仍被占用，不重试就会永远装不上（_update_download.exe 残留的根源）
            #  3. 只有 move 成功才 start 新版，失败则留下安装包等下次启动再问
            clear_env = "".join(f'set "{k}="\r\n' for k in _PYI_ENV_KEYS)
            script = (
                "@echo off\r\n"
                + clear_env
                + f":wait_{os.getpid()}\r\n"
                f'tasklist /FI "PID eq {os.getpid()}" 2>nul | find /I "{os.getpid()}" >nul\r\n'
                "if not errorlevel 1 (ping -n 2 127.0.0.1 >nul & goto "
                f"wait_{os.getpid()})\r\n"
                "set /a n=0\r\n"
                ":move\r\n"
                f'move /y "{new_file}" "{exe}" >nul 2>&1\r\n'
                "if not errorlevel 1 goto run\r\n"
                "set /a n+=1\r\n"
                "if %n% geq 30 goto giveup\r\n"
                "ping -n 2 127.0.0.1 >nul\r\n"
                "goto move\r\n"
                ":run\r\n"
                f'start "" "{exe}"\r\n'
                'del "%~f0"\r\n'
                "exit\r\n"
                ":giveup\r\n"
                'del "%~f0"\r\n'
            )
            with open(bat, "w", encoding="gbk", errors="ignore") as f:
                f.write(script)
            subprocess.Popen(["cmd", "/c", bat], env=_clean_child_env(),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            # macOS / Linux：等当前进程退出 → 替换二进制 → 重启 → 自删
            sh = os.path.join(tempfile.gettempdir(), f"_mcl_update_{os.getpid()}.sh")
            script = (
                "#!/bin/bash\n"
                f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 1; done\n"
                "for i in $(seq 1 30); do\n"
                f'  mv -f "{new_file}" "{exe}" 2>/dev/null && break\n'
                "  sleep 1\n"
                "done\n"
                f'[ -f "{new_file}" ] && exit 0\n'
                f'chmod +x "{exe}"\n'
                f'"{exe}" &\n'
                'rm -f -- "$0"\n'
            )
            with open(sh, "w", encoding="utf-8") as f:
                f.write(script)
            os.chmod(sh, 0o755)
            subprocess.Popen(["/bin/bash", sh], env=_clean_child_env(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        self.close()

    def on_update_ready(self, new_file):
        self._close_update_prog()
        self.toast.show_result(True, tr("更新包下载完成，正在重启安装…"))
        QTimer.singleShot(600, lambda: self._install_new_exe(new_file))

    # ---------- 导航 ----------
    def _dash_pixmap(self):
        pm = QPixmap(32, 32)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#4a545c"), 1.8))
        p.drawLine(9, 16, 23, 16)
        p.end()
        return pm

    def paintEvent(self, _e):
        paint_window_bg(self)

    def showEvent(self, e):
        super().showEvent(e)
        # 启动淡入：只播一次（从托盘/最小化恢复不再闪）
        if not getattr(self, "_fade_shown", False):
            self._fade_shown = True
            self.setWindowOpacity(0.0)
            anim = QPropertyAnimation(self, b"windowOpacity", self)
            anim.setDuration(260)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.start(QPropertyAnimation.DeleteWhenStopped)

    def show_sponsor(self):
        """打开赞助弹窗（微信赞赏码，本地图片，离线可用）"""
        SponsorDialog(self).exec()


    def goto(self, idx, animate=True):
        self.stack.setCurrentIndex(idx)
        if animate:
            fade_in(self.stack.currentWidget())
        # 模组管理已经不再是侧栏项，它算「游戏版本」的子页面，高亮挂在游戏版本上
        hl = 5 if idx == 8 else idx
        for b, pg in zip(self.nav, self.nav_pages):
            b.setChecked(pg == hl)
        self.btn_account.setChecked(idx == 2)
        self.btn_settings.setChecked(idx == 3)
        if idx == 5 and hasattr(self, "versions_page"):
            self.versions_page.reload()
        if idx == 6 and hasattr(self, "server_page"):
            self.server_page.refresh_state()
        if idx == 3 and hasattr(self, "settings_page"):
            # 进设置页重算一次：这时才知道当前启动版本要哪个 Java，
            # 才能把「自动选用」标在正确的行上
            try:
                self.settings_page.load_java_list()
            except Exception:
                pass
            if not self.server_page._versions_loaded:
                self.server_page.load_versions()
        if idx == 7 and hasattr(self, "news_page"):
            self.news_page.refresh()
        if idx == 8 and hasattr(self, "mods_page"):
            self.mods_page.refresh()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "toast"):
            pa = getattr(self.toast, "_pos_anim", None)
            if pa is not None:
                pa.stop()
            cw = self.content.width()
            self.toast.move((cw - self.toast.width()) // 2, 26)
        self._place_dl_ball()

    def mousePressEvent(self, e):
        pos = e.position()
        # 拖动窗口：左侧图标栏 或 顶部空白条
        if e.button() == Qt.LeftButton and (pos.x() < 84 or pos.y() < 48):
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _e):
        self._drag = None

    # ---------- 数据 ----------
    def refresh_installed(self):
        t = ApiThread("GET", "/api/installed")
        t.done.connect(self.on_installed)
        t.start()
        self._it = t

    def on_installed(self, d):
        self.installed = d.get("versions", []) if isinstance(d, dict) else []
        sel = (d.get("selected") or "") if isinstance(d, dict) else ""
        if sel in self.installed:
            self.version = sel
        elif self.version not in self.installed:
            self.version = self.installed[0] if self.installed else ""
        self.ver_label.setText(self.version or tr("未选择版本"))
        self.download.refresh_marks()
        if hasattr(self, "versions_page"):
            self.versions_page.reload()

    def refresh_accounts(self):
        self.accounts_page.refresh()

    def on_accounts_updated(self, accounts, selected):
        self.accounts = accounts
        self.selected_account = next((a for a in accounts if a.get("id") == selected),
                                     accounts[0] if accounts else None)
        self.home.set_account(self.selected_account)
        # 加号只在没有任何账户时出现
        self.btn_plus.setVisible(not self.accounts)

    def show_version_menu(self):
        menu = round_menu(self)
        if not self.installed:
            act = menu.addAction(tr("（暂无版本，先去下载）"))
            act.setEnabled(False)
        for v in self.installed:
            act = menu.addAction(("●  " if v == self.version else "     ") + v)
            act.triggered.connect(lambda _=False, name=v: self.set_version(name))
        menu.adjustSize()
        anchor = self.split.btn_more.mapToGlobal(QPoint(0, -6))
        menu.exec(QPoint(anchor.x() - menu.width() + self.split.btn_more.width(),
                         anchor.y() - menu.height()))

    def open_mods(self, vid=""):
        """打开模组管理页；给了 vid 就定位到那个版本（版本列表菜单用）"""
        if hasattr(self, "mods_page"):
            self.mods_page.focus_version(vid)
        self.goto(8)

    def set_version(self, name):
        """切换启动版本（主页下拉菜单 / 游戏版本页共用），并记住选择"""
        self.version = name or ""
        self.ver_label.setText(self.version or tr("未选择版本"))
        if hasattr(self, "versions_page"):
            self.versions_page.set_current(self.version)
        if self.version:
            t = ApiThread("POST", "/api/version/select", {"version": self.version}, timeout=15)
            t.start()

    # ---------- 启动 ----------
    def do_launch(self):
        if not self.version:
            info_card(self, tr("提示"), tr("还没有安装任何版本，请先到「下载游戏」下载"))
            self.goto(1)
            return
        if not self.selected_account:
            info_card(self, tr("提示"), tr("请先添加一个账户（左上角 ＋ 或账户页）"))
            self.goto(2)
            return
        ram = int(self.settings_page.ram_combo.currentText().split(" ")[0]) * 1024
        java = self.settings_page.java_edit.text().strip()
        d = Api.post("/api/launch", {"version": self.version, "ram": ram, "java": java,
                                     "account_id": self.selected_account.get("id")},
                     timeout=30).json()
        if not d.get("ok"):
            self.show_error_analysis(d.get("error", tr("未知错误")), context=tr("启动游戏"))
            return
        self.toast.show_result(True, f"{tr('正在启动 ')}{self.version}…")
        QTimer.singleShot(2600, self.toast.hide)
        self._start_crash_watch()

    # ---------- 状态 ----------
    def _place_dl_ball(self):
        """把小胶囊摆在内容区左下角（左侧栏前边那片空白的最底下）；只在有任务时露出"""
        if not hasattr(self, "dl_ball") or not hasattr(self, "content"):
            return
        c, b = self.content, self.dl_ball
        b.move(18, max(10, c.height() - b.height() - 16))
        b.raise_()
        if self.dl_popup.isVisible():
            self.dl_popup.place_above(b)

    def _sync_ball_visible(self, visible):
        """没有下载任务时整颗球都不出现；有任务才浮出来"""
        if not hasattr(self, "dl_ball"):
            return
        if visible == self.dl_ball.isVisible():
            return
        if visible:
            self._place_dl_ball()
            self.dl_ball.show()
            self.dl_ball.raise_()
        else:
            self.dl_ball.hide()
            if self.dl_popup.isVisible():
                self.dl_popup.hide_animated()

    def eventFilter(self, obj, ev):
        # 浮层开着时点别处（不在浮层里、也不在胶囊上）→ 收起
        # 关窗/退出时控件会先被销毁，这里必须吞掉异常，否则每条事件都刷一次错误
        try:
            if ev.type() == QEvent.MouseButtonPress and self.dl_popup.isVisible():
                try:
                    gp = ev.globalPosition().toPoint()
                except Exception:
                    gp = ev.globalPos()
                if not self._hit(self.dl_popup, gp) and not self._hit(self.dl_ball, gp):
                    self.dl_popup.hide_animated()
        except (RuntimeError, AttributeError):
            pass
        return super().eventFilter(obj, ev)

    @staticmethod
    def _hit(w, gp):
        try:
            return w.isVisible() and w.rect().contains(w.mapFromGlobal(gp))
        except RuntimeError:
            return False

    def toggle_dl_popup(self):
        """点胶囊：开/关下载任务浮层"""
        if self.dl_popup.isVisible():
            self.dl_popup.hide_animated()
            return
        self._place_dl_ball()
        self.dl_popup.populate(self._dl_tasks, self._dl_legacy,
                               getattr(self, "_dl_server", None))
        self.dl_popup.show_above(self.dl_ball)

    def _update_dl(self, tasks, s):
        """把任务表折算成胶囊状态：多个任务取平均进度，单个就是它自己的"""
        active = [t for t in tasks if t.get("installing")]
        legacy = None
        if not active and s.get("phase") == "downloading":
            # Mod 检查/更新这类不是任务型的流程，仍按单条显示
            legacy = {"id": "legacy", "title": tr("正在下载 / 安装"),
                      "message": s.get("message", ""),
                      "percent": int(s.get("percent") or 0),
                      "phase": "downloading", "installing": True}
        self._dl_tasks, self._dl_legacy = tasks, legacy
        self._installing = bool(active) or legacy is not None

        # 开服信息：排到浮层最后，显示内存占用（java 进程 / 分配内存）
        srv = s.get("server") or {}
        srv_running = bool(srv.get("running"))
        self._dl_server = None
        if srv_running:
            self._dl_server = {"id": "server", "title": tr("服务器运行中"),
                               "message": f"{tr('内存')} {srv.get('mem_mb', 0)} MB / {srv.get('ram', 0)} MB",
                               "percent": int(srv.get("mem_pct") or 0),
                               "phase": "server", "installing": False}

        if active:
            pct = int(round(sum(int(t.get("percent") or 0) for t in active) / len(active)))
            self._sync_ball_visible(True)
            self.dl_ball.set_state("run", pct, len(active))
        elif legacy:
            self._sync_ball_visible(True)
            self.dl_ball.set_state("run", int(legacy["percent"]), 1)
        elif srv_running:
            # 没在下载但服务器开着：球换成服务器图标，环里显示内存占用率
            self._sync_ball_visible(True)
            self.dl_ball.set_state("server", int(srv.get("mem_pct") or 0), 0)
        else:
            # 全部结束 → 球和浮层都收起来
            self._sync_ball_visible(False)
            self.dl_ball.set_state("idle", 0, 0)


        # 任务从"进行中"掉出来时给一次结果提示（每个任务只提示一次）
        now_active = {t["id"] for t in active}
        for t in tasks:
            tid = t.get("id")
            if tid in self._dl_prev_active and tid not in now_active:
                phase = t.get("phase")
                if phase == "done":
                    self.toast.show_result(True, t.get("message") or tr("下载完成"))
                    QTimer.singleShot(3200, self.toast.hide)
                    self.refresh_installed()
                elif phase == "error":
                    self.toast.show_result(False, t.get("message") or tr("下载失败"))
                    QTimer.singleShot(5600, self.toast.hide)
        self._dl_prev_active = now_active

        if self.dl_popup.isVisible():
            self.dl_popup.populate(tasks, legacy, self._dl_server)

    def on_state(self, s):
        self._update_dl(s.get("tasks") or [], s)

        # 日志页跟着后端日志刷新（原来这段被误塞进 on_esc_cancel 里，按 ESC 会 NameError）
        logs = s.get("log", [])
        if len(logs) != self._last_log_len:
            self.log_page.set_text("\n".join(logs))
            self._last_log_len = len(logs)

    # ---------- 报错自动分析 ----------
    def _start_crash_watch(self):
        """游戏拉起来后盯一会儿 crash-reports：闪退也能拿到崩溃报告做分析"""
        self._launch_at = time.time()
        self._crash_seen = ""
        self._crash_left = 6
        if not hasattr(self, "_crash_timer"):
            self._crash_timer = QTimer(self)
            self._crash_timer.setSingleShot(True)
            self._crash_timer.timeout.connect(self._check_crash)
        self._crash_timer.start(12000)

    def _check_crash(self):
        launch_at = getattr(self, "_launch_at", 0)

        def done(d):
            try:
                if not d.get("ok") or not d.get("found"):
                    return
                if float(d.get("mtime", 0)) < launch_at - 5:
                    return          # 是启动之前的旧崩溃报告，别拿出来吓人
                name = d.get("name", "")
                if name == getattr(self, "_crash_seen", ""):
                    return
                self._crash_seen = name
                self._pending_analysis = (d.get("tail") or "", tr("游戏崩溃"))
                self.toast.show_info(tr("游戏刚刚崩溃了"),
                                     tr("点这条提示看原因分析和处理建议"), 8000)
            except Exception:
                pass

        th = ApiThread("GET", "/api/crash_report", timeout=10)
        th.done.connect(done)
        th.start()
        self._crash_left = getattr(self, "_crash_left", 1) - 1
        if self._crash_left > 0:
            self._crash_timer.start(12000)

    def _toast_clicked(self):
        """点失败提示 → 直接看错误分析；有待看的服务器报错 → 看那个；否则跳日志页"""
        if getattr(self.toast, "_is_fail", False) and self.toast._fail_text:
            self.show_error_analysis(self.toast._fail_text)
            return
        pend = getattr(self, "_pending_analysis", None)
        if pend:
            self._pending_analysis = None
            self.show_error_analysis(pend[0], context=pend[1])
            return
        self.goto(4)

    def show_error_analysis(self, text, context=""):
        """把错误文本交给分析器：命中已知模式弹分析卡片，普通提示仍走老消息框"""
        text = (text or "").strip()
        if not text:
            return
        res = error_analyzer.analyze(text)
        if res is None:
            info_card(self, tr("提示"), text)
            return
        ErrorReportDialog(self, res, text, context=context).exec()

    def show_last_session(self):
        """启动 2 秒后检查上次会话：崩溃 / 被杀 / 断电就弹窗分析，正常退出不打扰"""
        last = _last_session
        if not isinstance(last, dict) or not last.get("abnormal"):
            return
        lines = []
        if last.get("started"):
            lines.append(tr("上次运行开始于 ") + last["started"])
        if last.get("last_seen"):
            lines.append(tr("最后有动静的时间：") + last["last_seen"])
        tasks = last.get("tasks") or []
        if tasks:
            bits = [f"· {t.get('title', '')}（{t.get('percent', 0)}%）"
                    for t in tasks[:6] if t.get("title")]
            if bits:
                lines.append(tr("中断时还在进行的任务：") + "\n" + "\n".join(bits))
        if last.get("last_event"):
            lines.append(tr("最后记录的动作：") + last["last_event"])
        summary = "\n".join(lines)
        res = last.get("analysis") or {
            "title": tr("没有匹配的已知错误"),
            "reason": tr("上一次启动器不是正常关闭的（可能是崩溃、被强制结束或断电）。"
                         "下面是从残留日志里挑出的最可疑的几行。"),
            "tips": "",
        }
        try:
            ErrorReportDialog(self, res, last.get("key_line") or summary,
                              context=last.get("context") or tr("上次异常退出")).exec()
        except Exception:
            if summary:
                warn_card(self, tr("上次启动器没有正常关闭"), summary)

    def on_esc_cancel(self):
        """ESC：先收起下载浮层，没有再取消进行中的任务"""
        if self.dl_popup.isVisible():
            self.dl_popup.hide_animated()
            return
        if not self._installing:
            return
        try:
            Api.post("/api/cancel", {}, timeout=10)
        except Exception:
            pass
        self.dl_ball.setToolTip(tr("正在取消…"))

    def restart_app(self):
        """重启自身（改语言后需要重建界面）。先走正常退出流程停服，再拉新进程。"""
        exe = os.path.abspath(sys.executable)
        args = [] if getattr(sys, "frozen", False) else [os.path.abspath(__file__)]
        self.shutdown()          # 先停服释放端口，免得新实例又抢一个端口
        # QProcess 静态方法只继承当前进程环境，得先把 _PYI_* 摘掉再拉起，
        # 否则新实例会去已经删掉的 _MEI 目录找 python3xx.dll（启动报错）
        saved = {k: os.environ.pop(k) for k in _PYI_ENV_KEYS if k in os.environ}
        try:
            ok, _pid = QProcess.startDetached(exe, args, os.path.dirname(exe) or None)
        finally:
            os.environ.update(saved)
        if not ok:
            warn_card(
                self, tr("提示"),
                tr("自动重启失败，请手动关闭启动器再重新打开，语言就会生效。"))
            return
        self.close()

    def shutdown(self):
        """关窗/退出时收尾：停轮询线程、等后台线程结束。
        QThread 还在跑就被析构，Qt 会 fail-fast（0xC0000409），
        Windows 随即弹出"AeroLauncher.exe 已停止工作"——这就是之前反复弹的报错。"""
        if getattr(self, "_shutting", False):
            return
        self._shutting = True
        # 先给会话记录盖上「正常关闭」的章——就算后面某一步卡住，
        # 下次启动也不会把这次误报成崩溃
        try:
            backend.close_session("normal")
        except Exception:
            pass
        try:
            QApplication.instance().removeEventFilter(self)
        except Exception:
            pass
        # 关启动器 = 什么都停：先取消所有下载任务，再停服务器（存档退出）
        try:
            requests.post(BASE + "/api/cancel", json={}, timeout=10)
        except Exception:
            pass
        # 服务器跟着启动器一起关：优雅 stop（存档退出，最多等 15 秒），再收 playit
        try:
            requests.post(BASE + "/api/server/stop", json={}, timeout=25)
        except Exception:
            pass
        try:
            requests.post(BASE + "/api/server/playit/stop", json={}, timeout=5)
        except Exception:
            pass
        try:
            self.poller.stop()
        except Exception:
            pass
        ud = getattr(self, "_ud", None)
        if ud is not None and ud.isRunning():
            try:
                ud.cancel()
            except Exception:
                pass
        for th in list(_ALIVE_THREADS) + list(_TRACKED_THREADS):
            _wait_or_kill(th, 1200)

    def closeEvent(self, e):
        # 先摘掉应用级事件过滤器，退出过程中控件销毁后别再被回调
        try:
            QApplication.instance().removeEventFilter(self)
        except Exception:
            pass
        self.shutdown()
        super().closeEvent(e)


def main():
    load_saved_lang()          # 任何界面构建之前先定语言
    start_backend()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(app_icon())
    win = MainWindow()
    app.aboutToQuit.connect(win.shutdown)
    if "--selftest" in sys.argv:
        QTimer.singleShot(2800, app.quit)
    win.show()
    # 上次更新没装完的收尾（_update_download.exe 残留检测）
    QTimer.singleShot(1600, win._check_pending_update)
    # 上次会话异常退出（崩溃 / 被杀 / 断电）→ 弹分析卡片
    QTimer.singleShot(2400, win.show_last_session)
    rc = app.exec()
    if "--selftest" in sys.argv:
        print("SELFTEST OK")
    sys.exit(rc)


if __name__ == "__main__":
    main()
