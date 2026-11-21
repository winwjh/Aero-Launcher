# -*- coding: utf-8 -*-
"""启动器多语言支持。

- 界面文案以简体中文为源语言（key 即中文原文），zh 模式下 tr() 原样返回。
- 繁体中文在 i18n_zh_tw.py、英文在 i18n_en.py，缺词条时回退中文，不会崩。
- 语言选择存 QSettings("WJH", "MCLauncher") 的 language 键，启动时读取。
"""

LANG = "zh"                 # "zh" | "zh_tw" | "en"
_TABLES = {"en": {}, "zh_tw": {}}

# 繁体别名（用户在配置文件里可能写成 zh-TW / zh_Hant / tw 等）
_TW_ALIASES = {"zh_tw", "zh-tw", "zhtw", "zh_hant", "zh-hant", "tw", "hk", "zh_hk"}


def set_lang(lang):
    global LANG
    v = str(lang or "zh").strip().lower()
    if v.startswith("en"):
        LANG = "en"
    elif v in _TW_ALIASES:
        LANG = "zh_tw"
    else:
        LANG = "zh"


def get_lang():
    return LANG


def register(name, table):
    """注册翻译表（由 i18n_en.py / i18n_zh_tw.py 调用）。"""
    _TABLES.setdefault(name, {}).update(table)


def register_en(table):
    """兼容旧调用。"""
    register("en", table)


def tr(s):
    """翻译入口：zh 原样返回；其他语言查表，缺词条回退中文。"""
    if LANG == "zh" or not isinstance(s, str):
        return s
    return _TABLES.get(LANG, {}).get(s, s)


def load_saved_lang():
    """启动早期调用：从 QSettings 读取上次选择的语言并应用。

    独立 import QSettings，避免与主程序导入顺序耦合。
    """
    try:
        from PySide6.QtCore import QSettings
        lang = QSettings("WJH", "MCLauncher").value("language", "zh")
        set_lang(lang)
    except Exception:
        set_lang("zh")


import i18n_en as _en_tbl
import i18n_zh_tw as _tw_tbl

register("en", _en_tbl.EN)
register("zh_tw", _tw_tbl.HANT)
