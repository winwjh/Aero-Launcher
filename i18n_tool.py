# -*- coding: utf-8 -*-
"""多语言维护工具。

用法：
    python i18n_tool.py --check        # 校验翻译覆盖率（不需要联网/第三方库）
    python i18n_tool.py --gen-hant     # 补全 i18n_zh_tw.py 缺失词条（需 opencc-python-reimplemented）

说明：
- key 是简体中文原文，来自 mc_desktop.py / server.py 里所有 tr("...") 的常量实参。
- --gen-hant 只**追加**缺失词条，已有译文不会被覆盖（方便人工修正后不被冲掉）。
- --check 会把「代码里有、表里没有」和「表里有、代码里没有」两侧都列出来。
"""
import ast
import io
import os
import sys

SOURCES = ["mc_desktop.py", "server.py"]

# 这些是「显示文字即数据」的取值：由 tr(变量) 传入，代码里没有字面量 tr("全部")
# 例如版本页筛选 chip、加载器名，因此表里有它们属于正常，不算孤儿。
DYNAMIC_KEYS = {"全部", "原版", "其他", "Forge", "Fabric"}
CJK = lambda s: any("\u4e00" <= ch <= "\u9fff" for ch in s)


def collect_keys():
    """收集两个源文件里所有 tr() 的字符串常量实参（含报错分析规则文案）。"""
    keys = set()
    for f in SOURCES:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), f)
        if not os.path.isfile(p):
            continue
        tree = ast.parse(io.open(p, encoding="utf-8").read())
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "tr" and n.args
                    and isinstance(n.args[0], ast.Constant)
                    and isinstance(n.args[0].value, str)):
                keys.add(n.args[0].value)
    # 报错分析的规则文案是纯字符串数据，界面用 tr() 展示，这里一并纳入校验
    try:
        import error_analyzer
        for r in error_analyzer.RULES:
            keys.add(r["title"])
            keys.add(r["reason"])
            keys.update(r["tips"])
    except Exception:
        pass
    return {k for k in keys if CJK(k)}


def load_table(mod_name, attr):
    mod = __import__(mod_name)
    return set(getattr(mod, attr))


def check():
    keys = collect_keys()
    en = load_table("i18n_en", "EN")
    tw = load_table("i18n_zh_tw", "HANT")

    def report(name, table):
        miss = sorted(keys - table)
        orphan = sorted(k for k in table if k not in keys and CJK(k) and k not in DYNAMIC_KEYS)
        print("== %s ==" % name)
        print("  tr() 词条: %d | 表内词条: %d" % (len(keys), len(table)))
        print("  缺词 %d 条" % len(miss))
        for k in miss[:15]:
            print("    - %r" % k[:70])
        print("  孤儿 %d 条" % len(orphan))
        for k in orphan[:10]:
            print("    + %r" % k[:70])
        return not miss

    ok_en = report("English / i18n_en.py", en)
    ok_tw = report("繁體中文 / i18n_zh_tw.py", tw)
    print("\n结论:", "全部覆盖" if (ok_en and ok_tw) else "存在缺词，请补充")
    return 0 if (ok_en and ok_tw) else 1


def gen_hant():
    try:
        import opencc
    except ImportError:
        print("缺少 opencc，请先执行：pip install opencc-python-reimplemented")
        return 2

    import i18n_zh_tw
    cc = opencc.OpenCC("s2twp")          # 台灣正體 + 詞彙轉換
    have = i18n_zh_tw.HANT
    keys = collect_keys()
    missing = sorted(k for k in keys if k not in have)
    if not missing:
        print("繁体表已完整（%d 条），无需补全" % len(have))
        return 0

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n_zh_tw.py")
    src = io.open(path, encoding="utf-8").read()
    add = "".join("    %r: %r,\n" % (k, cc.convert(k)) for k in missing)
    # 插到最后一个 } 之前
    head, sep, tail = src.rpartition("\n}")
    if not sep:
        print("i18n_zh_tw.py 结构异常，未修改")
        return 1
    io.open(path, "w", encoding="utf-8", newline="\n").write(head + "\n" + add + "}\n")
    print("已补入 %d 条繁体词条：%s" % (len(missing), path))
    for k in missing[:10]:
        print("  + %r -> %r" % (k[:50], cc.convert(k)[:50]))
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--check"
    if arg == "--check":
        sys.exit(check())
    elif arg == "--gen-hant":
        sys.exit(gen_hant())
    else:
        print(__doc__)
        sys.exit(2)
