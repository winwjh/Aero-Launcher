# -*- coding: utf-8 -*-
"""
MC 启动器后端
- 从 Mojang 官方源或 BMCLAPI（国内镜像）下载 Minecraft 版本数据
- 下载客户端 jar、依赖库、资源文件、natives
- 构建启动命令并启动游戏（离线登录）
"""
import io
import os
import re
import sys
import glob
import json
import uuid
import time
import queue
import shutil
import hashlib
import platform
import zipfile
import tarfile
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from mc_i18n import tr, load_saved_lang, get_lang

import requests
import error_analyzer
from flask import Flask, jsonify, request, send_file

# 打包成 exe 后：数据(.minecraft)放在 exe 同目录，静态资源在 _MEIPASS
# Android（Chaquopy）：通过环境变量 MC_LAUNCHER_HOME 注入数据目录
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
    # macOS .app：sys.executable 在 AeroLauncher.app/Contents/MacOS 里，
    # 游戏目录应放在 .app 同级（而不是写进包内，否则升级会丢数据）
    if (platform.system() == "Darwin"
            and BASE_DIR.endswith(os.path.join("Contents", "MacOS"))):
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(BASE_DIR)))
    RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.environ.get("MC_LAUNCHER_HOME") \
        or os.path.dirname(os.path.abspath(__file__))
    RES_DIR = BASE_DIR
MC_DIR = os.path.join(BASE_DIR, ".minecraft")
VERSIONS_DIR = os.path.join(MC_DIR, "versions")
LIBRARIES_DIR = os.path.join(MC_DIR, "libraries")
ASSETS_DIR = os.path.join(MC_DIR, "assets")
# 启动器自身文件统一收进 .minecraft/launcher/（对齐 PCL2 的整洁结构）
LAUNCHER_DIR = os.path.join(MC_DIR, "launcher")
LOGS_DIR = os.path.join(LAUNCHER_DIR, "logs")
CONFIG_FILE = os.path.join(LAUNCHER_DIR, "config.json")
ACCOUNTS_FILE = os.path.join(LAUNCHER_DIR, "accounts.json")
AUTHLIB_JAR = os.path.join(LAUNCHER_DIR, "authlib-injector.jar")
LOADER_CACHE = os.path.join(LAUNCHER_DIR, "loader_cache")

# ---------- 启动器配置 / 版本隔离 ----------
def load_config():
    """启动器配置（版本隔离等）。默认开启版本隔离，对齐 PCL2。"""
    cfg = {"isolate": True, "skip_onboarding": True}
    try:
        if os.path.isfile(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(LAUNCHER_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------- PCL2 / HMCL 版本配置互操作 ----------
# PCL2 : versions/<id>/PCL/Setup.ini（VersionArgumentIndieV2 表示版本隔离），UTF-8 + CRLF
# HMCL : versions/<id>/hmclversion.cfg（JSON，gameDirType=VERSION_FOLDER 表示隔离）
_PCL_LOADER_KEYS = (("VersionFabric", "Fabric"), ("VersionQuilt", "Quilt"),
                    ("VersionNeoForge", "NeoForge"), ("VersionForge", "Forge"),
                    ("VersionOptiFine", "OptiFine"), ("VersionLiteLoader", "LiteLoader"))
_PCL_KEY_BY_LOADER = {v: k for k, v in _PCL_LOADER_KEYS}
_META_CACHE = {}


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _ini_read(path):
    kv = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] in "#;[" or ":" not in line:
                    continue
                k, v = line.split(":", 1)
                kv[k.strip()] = v.strip()
    except OSError:
        pass
    return kv


def read_launcher_meta(vid):
    """读 PCL2 / HMCL 写在版本目录里的配置。

    两个文件都没有时 isolated 返回 None，调用方就按启动器自己的设置走；
    有的话以人家明确的设置优先（只往「隔离」方向靠，不会把隔离实例改成共享目录）。
    """
    vdir = os.path.join(VERSIONS_DIR, vid)
    ini = os.path.join(vdir, "PCL", "Setup.ini")
    hcfg = os.path.join(vdir, "hmclversion.cfg")
    ck = (_mtime(ini), _mtime(hcfg))
    hit = _META_CACHE.get(vid)
    if hit and hit[0] == ck:
        return hit[1]
    out = {"launchers": [], "isolated": None, "game_dir": "",
           "vanilla": "", "loader": "", "loader_version": ""}
    if os.path.isfile(ini):
        kv = _ini_read(ini)
        if kv:
            out["launchers"].append("PCL2")
            ind = (kv.get("VersionArgumentIndieV2")
                   or kv.get("VersionArgumentIndie") or "").strip().lower()
            if ind in ("true", "false"):
                out["isolated"] = ind == "true"
            out["vanilla"] = kv.get("VersionVanillaName") or kv.get("VersionVanilla") or ""
            for k, name in _PCL_LOADER_KEYS:
                if kv.get(k):
                    out["loader"], out["loader_version"] = name, kv[k]
                    break
    if os.path.isfile(hcfg):
        data = {}
        try:
            with open(hcfg, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        if isinstance(data, dict):
            out["launchers"].append("HMCL")
            gdt = str(data.get("gameDirType") or "").upper()
            if gdt == "VERSION_FOLDER":
                out["isolated"] = True
            elif gdt == "CUSTOM":
                out["isolated"] = False
                out["game_dir"] = (data.get("gameDir") or "").strip()
            elif gdt == "ROOT_FOLDER":
                out["isolated"] = False
    _META_CACHE[vid] = (ck, out)
    return out


def _vanilla_of(vid):
    """沿 inheritsFrom 找到最底层的原版版本号"""
    cur, seen = vid, set()
    while cur and cur not in seen:
        seen.add(cur)
        p = os.path.join(VERSIONS_DIR, cur, cur + ".json")
        if not os.path.isfile(p):
            return ""
        try:
            with open(p, "r", encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            return ""
        nxt = j.get("inheritsFrom")
        if not nxt:
            # 自包含版本（realize_version 展开过的）：本体记在 _aerobase 里
            return j.get("_aerobase") or cur
        cur = nxt
    return ""


def _loader_of(vid):
    """沿 inheritsFrom 找加载器（整合包实例自己的 json 只有 inheritsFrom）"""
    cur, seen = vid, set()
    while cur and cur not in seen:
        seen.add(cur)
        p = os.path.join(VERSIONS_DIR, cur, cur + ".json")
        if not os.path.isfile(p):
            break
        try:
            with open(p, "r", encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            break
        kind, ver = _detect_loader(j, cur)
        if kind != "原版":
            return kind, ver
        cur = j.get("inheritsFrom") or ""
    return "原版", ""


def write_launcher_meta(vid, mc="", loader="", loader_version="", overwrite=False):
    """给版本目录补写 PCL2 / HMCL 的配置，让两家打开同一个 .minecraft 时
    能直接认成「隔离实例」，并且知道它是原版几 + 哪个加载器。

    已存在的文件默认不覆盖——那是人家启动器自己写的，用户的设置得留着。
    """
    vdir = os.path.join(VERSIONS_DIR, vid)
    if not os.path.isdir(vdir):
        return []
    if not mc:
        mc = _vanilla_of(vid)
    if not loader:
        loader, loader_version = _loader_of(vid)
    # 已有一方明确表态（隔离 / 不隔离）就跟着它——别把 PCL2 的「不隔离」
    # 版本在 HMCL 里改成隔离，那样同一个版本两家会读不同目录的存档。
    prior = read_launcher_meta(vid)
    isolate = prior.get("isolated")
    if isolate is None:
        isolate = True
    wrote = []
    ini = os.path.join(vdir, "PCL", "Setup.ini")
    if overwrite or not os.path.isfile(ini):
        try:
            os.makedirs(os.path.join(vdir, "PCL"), exist_ok=True)
            lines = [f"VersionArgumentIndieV2:{'True' if isolate else 'False'}"]
            if mc:
                lines += [f"VersionVanilla:{mc}", f"VersionVanillaName:{mc}"]
            key = _PCL_KEY_BY_LOADER.get(loader)
            if key and loader_version:
                lines.append(f"{key}:{loader_version}")
            info = (tr("原版 ") + mc) if mc else tr("自定义版本")
            if loader and loader != "原版" and loader_version:
                info += f", {loader} {loader_version}"
            lines.append(f"Info:{info}")
            with open(ini, "w", encoding="utf-8", newline="") as f:
                f.write("\r\n".join(lines) + "\r\n")
            wrote.append("PCL2")
        except OSError as e:
            log(f"{tr('写入 PCL2 配置失败: ')}{e}")
    hcfg = os.path.join(vdir, "hmclversion.cfg")
    if overwrite or not os.path.isfile(hcfg):
        try:
            with open(hcfg, "w", encoding="utf-8") as f:
                json.dump({"gameDirType": "VERSION_FOLDER" if isolate else "ROOT_FOLDER",
                           "usesGlobal": False,
                           "gameDir": "", "javaPath": "", "javaArgs": "",
                           "maxMemory": 2048, "minMemory": 512, "permSize": 256,
                           "windowWidth": 854, "windowHeight": 480,
                           "fullscreen": False, "serverIp": "", "proxyType": "DIRECT"},
                          f, ensure_ascii=False, indent=2)
            wrote.append("HMCL")
        except OSError as e:
            log(f"{tr('写入 HMCL 配置失败: ')}{e}")
    if wrote:
        _META_CACHE.pop(vid, None)
    return wrote


def game_dir_for(vid):
    """版本隔离开启时，每个版本使用 versions/<版本>/ 作为独立游戏目录。

    PCL2 / HMCL 在自己的配置里明确把这个版本设成隔离（或指定了自定义目录）时按他们的来，
    同一个 .minecraft 在三个启动器之间切来切去，读的还是同一份存档和 mods。
    反过来不会把隔离实例降级成共享目录——那会让玩家的存档「凭空消失」。
    """
    meta = read_launcher_meta(vid) if vid else {}
    custom = (meta or {}).get("game_dir") or ""
    if custom and os.path.isdir(custom):
        return custom
    if (meta or {}).get("isolated") is True or load_config().get("isolate", True):
        d = os.path.join(VERSIONS_DIR, vid)
    else:
        d = MC_DIR
    os.makedirs(d, exist_ok=True)
    return d


def seed_options(vid):
    """新实例首次启动前补一份最小 options.txt。

    原版在 options.txt 不存在时会弹「Welcome to Minecraft!」无障碍引导页，
    而且语言默认 en_us（这就是新下载的版本第一次进去是英文的原因）。
    PCL2 之所以看不到这个页面，就是因为它会预写 onboardAccessibility:false。

    已有 options.txt 时只把 onboardAccessibility:true 改成 false，其它一个字不动，
    玩家自己调过的画质、按键、语言全部保留。
    """
    if not load_config().get("skip_onboarding", True):
        return
    try:
        gdir = game_dir_for(vid)
        p = os.path.join(gdir, "options.txt")
        if os.path.isfile(p):
            # 按字节替换：游戏写的是 CRLF，走文本模式会把整个文件的行尾改掉
            raw = open(p, "rb").read()
            if b"onboardAccessibility:true" not in raw:
                return
            open(p, "wb").write(raw.replace(b"onboardAccessibility:true",
                                            b"onboardAccessibility:false"))
            return
        lang = {"en": "en_us", "zh_tw": "zh_tw"}.get(get_lang(), "zh_cn")
        open(p, "wb").write(("lang:%s\r\nonboardAccessibility:false\r\n" % lang).encode("utf-8"))
    except Exception as e:
        log(f"{tr('初始化游戏配置失败(不影响启动): ')}{e}")


def migrate_layout():
    """把旧版散落在 .minecraft 根目录的启动器文件迁移到 launcher/ 子目录"""
    os.makedirs(LAUNCHER_DIR, exist_ok=True)
    file_moves = [
        (os.path.join(MC_DIR, "launcher_accounts.json"), ACCOUNTS_FILE),
        (os.path.join(MC_DIR, "authlib-injector.jar"), AUTHLIB_JAR),
    ]
    dir_moves = [
        (os.path.join(MC_DIR, "loader_cache"), LOADER_CACHE),
        (os.path.join(MC_DIR, "launcher_logs"), LOGS_DIR),
    ]
    for src, dst in file_moves:
        if os.path.isfile(src) and not os.path.exists(dst):
            try:
                shutil.move(src, dst)
            except Exception:
                pass
    for src, dst in dir_moves:
        if os.path.isdir(src) and not os.path.exists(dst):
            try:
                shutil.move(src, dst)
            except Exception:
                pass


migrate_layout()

MIRRORS = {
    "official": {
        "name": tr("Mojang 官方源"),
        "manifest": "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json",
        "replace": [],
    },
    "bmclapi": {
        "name": tr("BMCLAPI 国内镜像"),
        "manifest": "https://bmclapi2.bangbang93.com/mc/game/version_manifest_v2.json",
        "replace": [
            ("https://piston-meta.mojang.com/", "https://bmclapi2.bangbang93.com/"),
            ("https://launchermeta.mojang.com/", "https://bmclapi2.bangbang93.com/"),
            ("https://launcher.mojang.com/", "https://bmclapi2.bangbang93.com/"),
            ("https://resources.download.minecraft.net/", "https://bmclapi2.bangbang93.com/assets/"),
            ("https://libraries.minecraft.net/", "https://bmclapi2.bangbang93.com/maven/"),
            ("https://repo1.maven.org/maven2/", "https://bmclapi2.bangbang93.com/maven/"),
            ("https://piston-data.mojang.com/", "https://bmclapi2.bangbang93.com/"),
        ],
    },
}

LAUNCHER_NAME = "Aero Launcher"
LAUNCHER_VERSION = "1.2"

# ---------- 跨平台 ----------
SYSTEM_NAME = {"Windows": "windows", "Darwin": "osx", "Linux": "linux"}.get(platform.system(), "windows")


def _nowin():
    """窗口程序（打包后没有控制台）里起 java 等控制台子进程时，别让 Windows 弹黑窗口"""
    if SYSTEM_NAME != "windows":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

NATIVES_CLASSIFIER = f"natives-{SYSTEM_NAME}"
NATIVES_DIRNAME = f"natives-{SYSTEM_NAME}"
LIB_EXT = {"windows": ".dll", "osx": ".dylib", "linux": ".so"}[SYSTEM_NAME]

app = Flask(__name__, static_folder="static", static_url_path="")

# ---------- 全局状态 ----------
_state_lock = threading.Lock()
_state = {
    "installing": False,
    "cancel": False,
    "phase": "idle",          # idle / downloading / done / error
    "message": "",
    "done_files": 0,
    "total_files": 0,
    "percent": 0,
    "log": [],
    "installed": [],
}
_log_queue = queue.Queue(maxsize=1000)

# ---------- 下载任务表（支持多个任务同时进行） ----------
# 每个下载线程在开头 new_task() 把自己绑到线程局部，之后 set_progress / is_cancelled
# 自动作用到本线程的任务上；_state 仍做一份镜像，兼容旧的单任务读取方。
MAX_TASKS = 5
_TASKS = {}
_TASK_SEQ = [0]
_TASK_KEEP_DONE = 8          # 已完成任务最多保留几条（给胶囊弹层回看）
_tlocal = threading.local()


def cur_task():
    """当前线程绑定的下载任务（没有则 None，例如启动游戏这类非下载流程）"""
    return getattr(_tlocal, "task", None)


def cur_phase():
    t = cur_task()
    return (t or _state).get("phase", "idle")


def _trim_tasks_locked():
    done = sorted([t for t in _TASKS.values() if not t.get("installing")],
                  key=lambda x: x.get("ended", 0))
    for t in done[:-_TASK_KEEP_DONE] if len(done) > _TASK_KEEP_DONE else []:
        _TASKS.pop(t["id"], None)


def new_task(title, sub="", key=""):
    """登记一个新下载任务并绑定到当前线程；同目标已在跑则返回 None"""
    with _state_lock:
        if key and any(t.get("key") == key and t.get("installing")
                       for t in _TASKS.values()):
            return None
        _TASK_SEQ[0] += 1
        t = {"id": f"t{_TASK_SEQ[0]}", "title": title, "message": sub or "",
             "phase": "downloading", "percent": 0, "done_files": 0,
             "total_files": 0, "cancel": False, "installing": True,
             "key": key, "started": time.time(), "ended": 0}
        _TASKS[t["id"]] = t
        _state["cancel"] = False     # 新任务重置全局取消位（各任务自己的 cancel 互不影响）
        _trim_tasks_locked()
    _tlocal.task = t
    record_event("task", f"{tr('开始 ')}{title}")
    return t


def end_task(phase="done", message=""):
    """收尾当前线程的任务（done / error / cancel）"""
    t = cur_task()
    if t is None:
        set_progress(phase=phase, installing=False, message=message)
        return
    set_progress(phase=phase, installing=False,
                 message=message or t.get("message", ""),
                 **({"percent": 100} if phase == "done" else {}))
    with _state_lock:
        t["installing"] = False
        t["ended"] = time.time()
    _tlocal.task = None
    record_event("task", f"{phase}: {message or t.get('message', '')}")


def active_tasks():
    with _state_lock:
        return [t for t in _TASKS.values() if t.get("installing")]


def task_busy(key):
    with _state_lock:
        return any(t.get("key") == key and t.get("installing")
                   for t in _TASKS.values())


def _too_many():
    """并发闸门：允许同时跑 MAX_TASKS 个任务"""
    if len(active_tasks()) >= MAX_TASKS:
        return jsonify({"ok": False,
                        "error": tr("同时下载的任务太多（最多 ") + str(MAX_TASKS) +
                                 tr(" 个），等一个完成再来")})
    return None



def log(msg):
    line = time.strftime("[%H:%M:%S] ") + str(msg)
    with _state_lock:
        _state["log"].append(line)
        _state["log"] = _state["log"][-300:]
    try:
        _log_queue.put_nowait(line)
    except queue.Full:
        pass


def set_progress(**kw):
    """写进度：有任务就写本线程的任务，同时镜像一份到全局 _state（兼容旧逻辑）"""
    t = cur_task()
    with _state_lock:
        _state.update({k: v for k, v in kw.items() if k in _state})
        if t is not None:
            t.update({k: v for k, v in kw.items() if k in t})


class Cancelled(Exception):
    """用户取消下载/安装"""


def is_cancelled():
    t = cur_task()
    if t is not None:
        return bool(t.get("cancel"))
    with _state_lock:
        return _state["cancel"]


def report_error(prefix, e):
    """线程收尾：用户取消 → 平静结束；其它异常 → 报错"""
    if isinstance(e, Cancelled):
        log(tr("已取消"))
        end_task("cancel", tr("已取消"))
    else:
        log(f"{prefix}: {e}")
        end_task("error", f"{prefix}: {e}")


# ---------- 会话记录（崩溃检测：上次在干什么、为什么停） ----------
# 每次启动在 .minecraft/launcher/sessions/ 写一个 session-*.json：
# 运行期间心跳线程每 5 秒刷新一次（当前任务快照 + 日志尾部），正常关闭时补上
# exit 标记。下次启动发现最新会话没有 exit，就是崩溃 / 被杀 / 断电，前端弹窗
# 并把残留日志交给 error_analyzer 分析。
SESSIONS_DIR = os.path.join(LAUNCHER_DIR, "sessions")
_session_file = {"path": None, "data": None}
_heartbeat_stop = threading.Event()


def _session_tasks_snapshot():
    with _state_lock:
        return [{"title": t.get("title", ""), "phase": t.get("phase", ""),
                 "percent": t.get("percent", 0), "message": t.get("message", "")}
                for t in _TASKS.values() if t.get("installing")]


def _write_session():
    """原子写：先写 .tmp 再 replace，崩在半路也不会把文件写花"""
    p, d = _session_file["path"], _session_file["data"]
    if not p or d is None:
        return
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:
        pass


def record_event(kind, detail=""):
    """往当前会话里记一笔「在干什么」。任何线程都能调。"""
    d = _session_file["data"]
    if d is None:
        return
    d.setdefault("events", []).append(
        {"t": time.strftime("%H:%M:%S"), "kind": kind, "detail": str(detail)[:300]})
    d["events"] = d["events"][-80:]
    d["tasks"] = _session_tasks_snapshot()
    _write_session()


def _heartbeat_loop():
    while not _heartbeat_stop.wait(5):
        d = _session_file["data"]
        if d is None:
            return
        d["heartbeat"] = time.time()
        d["tasks"] = _session_tasks_snapshot()
        with _state_lock:
            d["log_tail"] = list(_state["log"][-50:])
        _write_session()


def start_session(version=""):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    old = sorted(glob.glob(os.path.join(SESSIONS_DIR, "session-*.json")))
    for p in old[:-20]:
        try:
            os.remove(p)
        except Exception:
            pass
    d = {"version": version, "pid": os.getpid(),
         "started": time.time(),
         "started_str": time.strftime("%Y-%m-%d %H:%M:%S"),
         "events": [], "tasks": [], "log_tail": [], "exit": None}
    p = os.path.join(SESSIONS_DIR, "session-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
    _session_file["path"], _session_file["data"] = p, d
    _heartbeat_stop.clear()
    _write_session()
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    log(f"{tr('会话记录已开启: ')}{os.path.basename(p)}")


def close_session(reason="normal", detail=""):
    """正常关闭时补 exit 标记；崩溃时这个不会被调到，下次启动就能查出来"""
    d = _session_file["data"]
    _heartbeat_stop.set()
    if d is not None:
        d["exit"] = {"reason": reason, "at": time.time(), "detail": str(detail)[:300]}
        d["tasks"] = _session_tasks_snapshot()
        _write_session()
        _session_file["data"] = None


def load_last_session():
    """读上一次会话：没有 exit 标记 = 异常退出。返回给前端弹窗用。"""
    try:
        files = [f for f in sorted(glob.glob(os.path.join(SESSIONS_DIR, "session-*.json")))
                 if f != _session_file["path"]]
        if not files:
            return {"abnormal": False}
        with open(files[-1], encoding="utf-8") as f:
            d = json.load(f)
        if d.get("exit"):
            return {"abnormal": False, "reason": d["exit"].get("reason", "normal")}
        tasks = [t for t in (d.get("tasks") or []) if t.get("phase") != "done"]
        last_ev = (d.get("events") or [{}])[-1]
        # 崩溃证据优先级：游戏崩溃日志（上次在玩游戏时崩的）> 启动器日志尾部
        crash_log = d.get("crash_log") or ""
        text = crash_log or "\n".join(d.get("log_tail") or [])
        if not text.strip():
            try:
                gl = sorted(glob.glob(os.path.join(LOGS_DIR, "game_*.log")), key=os.path.getmtime)
                if gl:
                    with open(gl[-1], encoding="utf-8", errors="replace") as f:
                        text = f.read()[-8000:]
            except Exception:
                pass
        res = error_analyzer.analyze(text)
        context = tr("上次游戏崩溃") if crash_log else tr("上次启动器异常退出")
        return {"abnormal": True,
                "context": context,
                "started": d.get("started_str", ""),
                "last_seen": time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(d.get("heartbeat") or d.get("started") or 0)),
                "tasks": tasks,
                "last_event": f"{last_ev.get('t', '')} {last_ev.get('detail', '')}".strip(),
                "analysis": res,
                "key_line": error_analyzer.key_line(text) if text.strip() else "",
                "raw": text[-1500:]}
    except Exception as e:
        log(f"{tr('读取上次会话失败: ')}{e}")
        return {"abnormal": False}


@app.route("/api/session/last", methods=["GET"])
def api_session_last():
    return jsonify(load_last_session())


@app.route("/api/session/close", methods=["POST"])
def api_session_close():
    d = request.get_json(silent=True) or {}
    close_session(d.get("reason") or "normal", d.get("detail") or "")
    return jsonify({"ok": True})


def refresh_installed():
    result = []
    if os.path.isdir(VERSIONS_DIR):
        for vid in sorted(os.listdir(VERSIONS_DIR)):
            jp = os.path.join(VERSIONS_DIR, vid, vid + ".json")
            if not os.path.isfile(jp):
                continue
            if os.path.isfile(os.path.join(VERSIONS_DIR, vid, vid + ".jar")):
                result.append(vid)
                continue
            # 加载器版本：自身无 jar，但沿 inheritsFrom 链找到的原版 jar 存在即可启动
            try:
                with open(jp, "r", encoding="utf-8") as f:
                    j = json.load(f)
                cur, seen = j.get("inheritsFrom"), set()
                ok = False
                while cur and cur not in seen:
                    seen.add(cur)
                    if os.path.isfile(os.path.join(VERSIONS_DIR, cur, cur + ".jar")):
                        ok = True
                        break
                    pj = os.path.join(VERSIONS_DIR, cur, cur + ".json")
                    if not os.path.isfile(pj):
                        break
                    with open(pj, "r", encoding="utf-8") as f2:
                        cur = json.load(f2).get("inheritsFrom")
                if ok:
                    result.append(vid)
                elif j.get("downloads", {}).get("client", {}).get("url"):
                    # 只带 json 的导入版本（PCL2/HMCL/BCL2 导出常见）：客户端能补下载，
                    # 先列出来，启动时会自动补齐缺的文件，而不是干脆藏起来
                    result.append(vid)
            except Exception:
                pass
    with _state_lock:
        _state["installed"] = result
    return result


# ---------- 版本详情（游戏版本页用） ----------
_SIZE_CACHE = {}


def _dir_size(path):
    """版本目录占用字节数（带缓存：目录 mtime 变了才重算）"""
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return 0
    if key in _SIZE_CACHE:
        return _SIZE_CACHE[key]
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    for k in [k for k in list(_SIZE_CACHE) if k[0] == path]:
        _SIZE_CACHE.pop(k, None)
    _SIZE_CACHE[key] = total
    return total


LOADER_RULES = (
    ("net.fabricmc:fabric-loader", "Fabric"),
    ("org.quiltmc:quilt-loader", "Quilt"),
    ("net.neoforged:neoforge", "NeoForge"),
    ("net.minecraftforge:fmlloader", "Forge"),
    ("net.minecraftforge:forge", "Forge"),
    ("optifine:optifine", "OptiFine"),
)


def _detect_loader(j, vid):
    """判断版本是原版还是某个加载器，并取出加载器版本号"""
    libs = [(l.get("name") or "").lower() for l in (j.get("libraries") or [])]
    for prefix, kind in LOADER_RULES:
        for n in libs:
            if n.startswith(prefix):
                v = n.split(":")[-1]
                return kind, (v.split("-")[-1] if "-" in v else v)
    low = vid.lower()
    for kw, kind in (("neoforge", "NeoForge"), ("forge", "Forge"), ("fabric", "Fabric"),
                     ("optifine", "OptiFine"), ("quilt", "Quilt")):
        if kw in low:
            return kind, low.split(kw)[-1].strip("-_")
    return "原版", ""


def _dependents(vid):
    """列出沿 inheritsFrom 依赖 vid 的其他版本（这些版本存在时不能改名/删除）"""
    deps = []
    if not os.path.isdir(VERSIONS_DIR):
        return deps
    for name in os.listdir(VERSIONS_DIR):
        if name == vid or not os.path.isdir(os.path.join(VERSIONS_DIR, name)):
            continue
        jf = os.path.join(VERSIONS_DIR, name, name + ".json")
        if not os.path.isfile(jf):
            continue
        try:
            with open(jf, "r", encoding="utf-8") as f:
                j = json.load(f)
            cur, seen = name, set()
            while j.get("inheritsFrom") and cur not in seen:
                seen.add(cur)
                if j["inheritsFrom"] == vid:
                    deps.append(name)
                    break
                cur = j["inheritsFrom"]
                pf = os.path.join(VERSIONS_DIR, cur, cur + ".json")
                if not os.path.isfile(pf):
                    break
                with open(pf, "r", encoding="utf-8") as f:
                    j = json.load(f)
        except Exception:
            pass
    return deps


def version_meta(vid):
    """单个版本的展示信息：加载器 / Java / 占用 / 上次启动时间"""
    vdir = os.path.join(VERSIONS_DIR, vid)
    j = {}
    try:
        with open(os.path.join(vdir, vid + ".json"), "r", encoding="utf-8") as f:
            j = json.load(f)
    except Exception:
        pass
    kind, lver = _detect_loader(j, vid)
    base = j.get("inheritsFrom") or j.get("_aerobase") or ""
    # 整合包/实例自身 json 只有 inheritsFrom，加载器信息继承自父版本
    cur, seen = base, {vid}
    while kind == "原版" and cur and cur not in seen:
        seen.add(cur)
        pj = os.path.join(VERSIONS_DIR, cur, cur + ".json")
        if not os.path.isfile(pj):
            break
        try:
            with open(pj, "r", encoding="utf-8") as f:
                pj_data = json.load(f)
        except Exception:
            break
        k2, v2 = _detect_loader(pj_data, cur)
        if k2 != "原版":
            kind, lver = k2, v2
            break
        cur = pj_data.get("inheritsFrom") or ""
    jv = (j.get("javaVersion") or {}).get("majorVersion")
    cfg = load_config()
    lmeta = read_launcher_meta(vid)
    isolated = lmeta.get("isolated")
    if isolated is None:
        isolated = bool(cfg.get("isolate", True))
    return {
        "id": vid,
        "isolated": bool(isolated),
        "launchers": lmeta.get("launchers") or [],
        "game_dir": lmeta.get("game_dir") or "",
        "base": base or (vid if kind == "原版" else ""),
        "vanilla": _vanilla_of(vid) or "",
        "loader": kind,
        "loader_version": lver,
        "java": f"Java {jv}" if jv else "",
        "size_mb": round(_dir_size(vdir) / 1048576, 1),
        "last_played": (cfg.get("last_played") or {}).get(vid, 0),
        "mods": len([n for n in os.listdir(os.path.join(vdir, "mods"))
                     if n.lower().endswith(".jar")])
        if os.path.isdir(os.path.join(vdir, "mods")) else 0,
    }


def selected_version(ids=None):
    """当前选中的版本：优先 config 里记的，其次列表第一个"""
    if ids is None:
        ids = refresh_installed()
    sel = (load_config().get("version") or "").strip()
    if sel in ids:
        return sel
    return ids[0] if ids else ""


# ---------- 下载工具 ----------
def mirror_url(url, mirror="official"):
    for a, b in MIRRORS.get(mirror, {}).get("replace", []):
        url = url.replace(a, b)
    return url


def http_get(url, mirror="official", timeout=30, retries=3):
    last = None
    for i in range(retries):
        try:
            r = requests.get(mirror_url(url, mirror), timeout=timeout, stream=True)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            time.sleep(1 + i)
    raise RuntimeError(f"{tr('下载失败 ')}{url}: {last}")


def download_file(url, dest, mirror="official", expected_size=None, sha1=None):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    r = http_get(url, mirror, timeout=60)
    h = hashlib.sha1()
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(256 * 1024):
            if is_cancelled():
                f.close()
                try:
                    r.close()
                except Exception:
                    pass
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                raise Cancelled()
            f.write(chunk)
            h.update(chunk)
    if sha1 and h.hexdigest() != sha1:
        os.remove(tmp)
        raise RuntimeError(f"{tr('校验失败 ')}{dest}")
    if expected_size and os.path.getsize(tmp) != expected_size:
        os.remove(tmp)
        raise RuntimeError(f"{tr('大小不符 ')}{dest}")
    if os.path.exists(dest):
        os.remove(dest)
    os.replace(tmp, dest)


def fetch_json(url, mirror="official"):
    return http_get(url, mirror).json()


# ---------- 版本列表 ----------
def get_version_list(mirror):
    data = fetch_json(MIRRORS[mirror]["manifest"], mirror)
    out = []
    for v in data.get("versions", []):
        out.append({
            "id": v["id"],
            "type": v["type"],          # release / snapshot / old_alpha / old_beta
            "releaseTime": v.get("releaseTime", ""),
            "url": v["url"],
        })
    return out


# ---------- 资讯：Minecraft 官方动态 ----------
# 只做「读」：最新正式版 / 最新快照 / 最近版本时间线 + 官方链接（更新日志、下载、
# 商店、官方频道）。所有跳转都交给系统浏览器，启动器里不放内嵌浏览器。
NEWS_SITES = {
    "en": {
        "home": "https://www.minecraft.net/en-us",
        "download": "https://www.minecraft.net/en-us/download",
        "store": "https://www.minecraft.net/en-us/store/minecraft-java-bedrock-edition-pc",
        "article": "https://www.minecraft.net/en-us/article/minecraft-java-edition-",
    },
    "zh": {
        "home": "https://www.minecraft.net/zh-hans",
        "download": "https://www.minecraft.net/zh-hans/download",
        "store": "https://www.minecraft.net/zh-hans/store/minecraft-java-bedrock-edition-pc",
        "article": "https://www.minecraft.net/zh-hans/article/minecraft-java-edition-",
    },
}
NEWS_CHANNEL = "https://www.youtube.com/@Minecraft"
_NEWS_CACHE = {"ts": 0.0, "data": None}
_NEWS_TTL = 1800
_NEWS_FILE = os.path.join(LAUNCHER_DIR, "news.json")


def _news_site():
    return NEWS_SITES["en"] if get_lang() == "en" else NEWS_SITES["zh"]


def _news_changelog(ver_id, vtype, site):
    """更新日志地址：正式版有稳定的官网文章路径；快照没有，退到官网下载页"""
    vid = ver_id or ""
    if vtype == "release" and re.fullmatch(r"\d+(?:\.\d+)+", vid):
        return site["article"] + vid.replace(".", "-")
    return site["download"]


def build_news(force=False):
    """拉官方版本清单，拼出资讯页需要的数据（30 分钟缓存 + 落盘离线兜底）"""
    now = time.time()
    if not force and _NEWS_CACHE["data"] and now - _NEWS_CACHE["ts"] < _NEWS_TTL:
        return _NEWS_CACHE["data"]
    site = _news_site()
    data = fetch_json(MIRRORS["official"]["manifest"], "official")
    allv = data.get("versions", [])
    by_id = {v.get("id"): v for v in allv}

    def pack(vid, vtype):
        v = by_id.get(vid) or {}
        return {"id": vid or "", "type": vtype,
                "date": (v.get("releaseTime") or "")[:10],
                "changelog": _news_changelog(vid, vtype, site)}

    latest = data.get("latest") or {}
    recent = [v for v in allv if v.get("type") in ("release", "snapshot")][:10]
    out = {
        "ok": True,
        "fetched": int(now),
        "latest": {"release": pack(latest.get("release"), "release"),
                   "snapshot": pack(latest.get("snapshot"), "snapshot")},
        "versions": [pack(v.get("id"), v.get("type")) for v in recent],
        "links": dict(site, channel=NEWS_CHANNEL),
    }
    _NEWS_CACHE.update(ts=now, data=out)
    try:
        os.makedirs(LAUNCHER_DIR, exist_ok=True)
        with open(_NEWS_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except Exception:
        pass
    return out


# ---------- 规则判断 ----------
def rules_allow(rules):
    if not rules:
        return True
    allowed = False
    for rule in rules:
        if "features" in rule:
            continue  # 不启用可选特性，忽略此类规则
        os_rule = rule.get("os") or {}
        if os_rule.get("name") and os_rule["name"] != SYSTEM_NAME:
            continue
        if os_rule.get("arch") and os_rule["arch"] != platform.architecture()[0]:
            continue
        allowed = (rule.get("action") == "allow")
    return allowed


def lib_coords_path(name):
    parts = name.split(":")
    group, artifact, ver = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) > 3 else ""
    ext = parts[4] if len(parts) > 4 else "jar"
    p = f"{group.replace('.', '/')}/{artifact}/{ver}/{artifact}-{ver}"
    if classifier:
        p += f"-{classifier}"
    return p + f".{ext}", artifact, ver, classifier


def collect_version_files(vjson, mirror):
    """返回 [(url, dest, size, sha1)] 与 natives jars 列表"""
    files = []
    natives_jars = []
    client = vjson.get("downloads", {}).get("client")
    vid = vjson["id"]
    if client and client.get("url"):
        files.append((client["url"],
                      os.path.join(VERSIONS_DIR, vid, vid + ".jar"),
                      client.get("size", 0), client.get("sha1")))

    for lib in vjson.get("libraries", []):
        if not rules_allow(lib.get("rules")):
            continue
        dl = lib.get("downloads", {})
        artifact = dl.get("artifact")
        if artifact and artifact.get("url"):
            path = artifact["path"] or lib_coords_path(lib["name"])[0]
            files.append((artifact["url"], os.path.join(LIBRARIES_DIR, path),
                          artifact.get("size", 0), artifact.get("sha1")))
        elif not dl:  # 老版本没有 downloads，用坐标推
            path, _, _, _ = lib_coords_path(lib["name"])
            base = lib.get("url", "")
            if base:
                files.append((base + path, os.path.join(LIBRARIES_DIR, path), 0, None))
        natives = lib.get("natives") or {}
        cls = natives.get(SYSTEM_NAME)
        if cls:
            cls = cls.replace("${arch}", "64" if platform.architecture()[0] == "64bit" else "32")
            na = dl.get("classifiers", {}).get(cls)
            if na:
                dest = os.path.join(LIBRARIES_DIR, na["path"])
                natives_jars.append(dest)
                files.append((na["url"], dest, na.get("size", 0), na.get("sha1")))

    # 资源索引
    ai = vjson.get("assetIndex")
    if ai and ai.get("url"):
        files.append((ai["url"], os.path.join(ASSETS_DIR, "indexes", ai["id"] + ".json"),
                      ai.get("size", 0), ai.get("sha1")))
    return files, natives_jars


def collect_asset_files(vjson, mirror):
    """下载资源文件（音效/语言等），返回文件列表"""
    ai = vjson.get("assetIndex")
    if not ai:
        return []
    index_name = ai["id"]
    index_path = os.path.join(ASSETS_DIR, "indexes", index_name + ".json")
    if not os.path.isfile(index_path):
        download_file(ai["url"], index_path, mirror, ai.get("size"), ai.get("sha1"))
    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)
    files = []
    for key, obj in index.get("objects", {}).items():
        h = obj["hash"]
        dest = os.path.join(ASSETS_DIR, "objects", h[:2], h)
        if os.path.isfile(dest) and os.path.getsize(dest) == obj.get("size"):
            continue
        url = f"https://resources.download.minecraft.net/{h[:2]}/{h}"
        files.append((url, dest, obj.get("size", 0), h))
    return files


def _url_fallbacks(url, mirror):
    """镜像失败时的回退链：镜像 -> 阿里云 Maven -> 官方"""
    chain = [mirror_url(url, mirror)]
    if mirror == "bmclapi":
        if url.startswith("https://libraries.minecraft.net/"):
            rel = url.split("libraries.minecraft.net/", 1)[1]
            chain.append("https://maven.aliyun.com/repository/public/" + rel)
        chain.append(url)
    return chain


def download_any(item, mirror):
    """带多源回退的下载"""
    url, dest, size, sha1 = item
    last = None
    for u in _url_fallbacks(url, mirror):
        try:
            download_file(u, dest, "official", size, sha1)
            return
        except Cancelled:
            raise
        except Exception as e:
            last = e
    raise RuntimeError(str(last))


def download_all(files, mirror):
    """并行下载，更新进度"""
    total = len(files)
    set_progress(total_files=total, done_files=0, percent=0)
    failed = []

    def do_one(item):
        url, dest, size, sha1 = item
        if is_cancelled():
            return ("cancel", dest, None)
        if os.path.isfile(dest) and (size == 0 or os.path.getsize(dest) == size):
            return ("skip", dest, None)
        try:
            download_any(item, mirror)
            return ("ok", dest, None)
        except Cancelled:
            return ("cancel", dest, None)
        except Exception as e:
            return ("fail", dest, e)

    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(do_one, item) for item in files]
        for fut in as_completed(futures):
            status, dest, err = fut.result()
            done += 1
            if status == "fail":
                failed.append((dest, str(err)))
                log(f"{tr('下载失败: ')}{os.path.basename(dest)} ({err})")
            elif status == "ok":
                set_progress(done_files=done)
            if done % 20 == 0 or status == "cancel":
                set_progress(done_files=done, percent=int(done * 100 / total) if total else 100)
            if status == "cancel":
                break
    set_progress(done_files=done, percent=int(done * 100 / total) if total else 100)
    if failed:
        # 重试一轮失败的
        log(f"{tr('重试 ')}{len(failed)}{tr(' 个失败文件…')}")
        retry = []
        for dest, _ in failed:
            for item in files:
                if item[1] == dest:
                    retry.append(item)
        failed2 = 0
        for item in retry:
            try:
                if is_cancelled():
                    break
                download_any(item, mirror)
            except Cancelled:
                break
            except Exception as e:
                failed2 += 1
                log(f"{tr('重试仍失败: ')}{os.path.basename(item[1])} ({e})")
        return failed2 == 0
    return True


def _base_version_id(vid):
    """沿 inheritsFrom 一路找到最底层的原版 id（客户端 jar 放在它目录里）"""
    cur, seen = vid, {vid}
    while cur:
        p = os.path.join(VERSIONS_DIR, cur, cur + ".json")
        if not os.path.isfile(p):
            return cur
        try:
            with open(p, "r", encoding="utf-8") as f:
                nxt = json.load(f).get("inheritsFrom")
        except Exception:
            return cur
        if not nxt or nxt in seen:
            return cur
        cur = nxt
        seen.add(cur)
    return cur


def needed_version_files(vid, vjson):
    """启动这个版本真正需要落盘的文件（依赖库 / natives / 资源索引 / 客户端 jar）。

    从别的启动器导入的版本常只带一份 json，libraries 还在人家的目录里，
    启动时就会「找不到主类」——照这份清单补齐即可。
    """
    files = []
    for lib in vjson.get("libraries", []):
        if not rules_allow(lib.get("rules")):
            continue
        dl = lib.get("downloads", {})
        art = dl.get("artifact")
        if art and art.get("url"):
            files.append((art["url"], os.path.join(LIBRARIES_DIR, art["path"]),
                          art.get("size", 0), art.get("sha1")))
        elif not dl:
            path, _, _, _ = lib_coords_path(lib["name"])
            base = lib.get("url", "")
            if base:
                files.append((base + path, os.path.join(LIBRARIES_DIR, path), 0, None))
        natives = lib.get("natives") or {}
        cls = natives.get(SYSTEM_NAME)
        if cls:
            cls = cls.replace("${arch}", "64" if platform.architecture()[0] == "64bit" else "32")
            na = (dl.get("classifiers") or {}).get(cls)
            if na and na.get("url"):
                files.append((na["url"], os.path.join(LIBRARIES_DIR, na["path"]),
                              na.get("size", 0), na.get("sha1")))
    ai = vjson.get("assetIndex")
    if ai and ai.get("url"):
        files.append((ai["url"], os.path.join(ASSETS_DIR, "indexes", ai["id"] + ".json"),
                      ai.get("size", 0), ai.get("sha1")))
    # 客户端 jar：始终放在最底层原版目录里
    base_id = _base_version_id(vid)
    jar = os.path.join(VERSIONS_DIR, base_id, base_id + ".jar")
    if not os.path.isfile(jar):
        client = vjson.get("downloads", {}).get("client")
        if client and client.get("url"):
            files.append((client["url"], jar, client.get("size", 0), client.get("sha1")))
    return files


def _file_ok(dest, size):
    if not os.path.isfile(dest):
        return False
    return not size or os.path.getsize(dest) == size


def ensure_version_files(vid, vjson, mirror="official"):
    """启动前补全缺失的依赖库/客户端 jar，返回补了几个文件。

    导入来的版本（PCL2 / HMCL / BCL2 等）只拷了版本目录，依赖库还在原来那套
    libraries 里，直接启动就是 ClassNotFoundException。这里按依赖清单补齐。
    """
    files = needed_version_files(vid, vjson)
    miss = [f for f in files if not _file_ok(f[1], f[2])]
    if not miss:
        return 0
    log(f"{vid}{tr(' 缺少 ')}{len(miss)}{tr(' 个依赖文件，正在补全…')}")
    for f in miss[:5]:
        log(f"    + {os.path.basename(f[1])}")
    if len(miss) > 5:
        log(f"    + {tr('等 ')}{len(miss)}{tr(' 个')}")
    set_progress(message=f"{vid}{tr(' 补全依赖文件 ')}{len(miss)}{tr(' 个…')}")
    if not download_all(miss, mirror):
        raise RuntimeError(
            f"{vid}{tr(' 的依赖文件下载失败（')}{len(miss)}"
            f"{tr(' 个），请检查网络后重试')}")
    log(f"{tr('依赖已补全，继续启动 ')}{vid}")
    return len(miss)


def extract_natives(natives_jars, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    for jar in natives_jars:
        if not os.path.isfile(jar):
            continue
        with zipfile.ZipFile(jar) as z:
            for info in z.infolist():
                name = info.filename
                if name.startswith("META-INF"):
                    continue
                if not name.lower().endswith(LIB_EXT):
                    continue
                try:
                    z.extract(info, dest_dir)
                except Exception:
                    pass


# ---------- 启动命令 ----------
def find_java():
    java_bin = "javaw" if SYSTEM_NAME == "windows" else "java"
    exe = shutil.which(java_bin) or shutil.which("java")
    if exe:
        return exe
    candidates = []
    if SYSTEM_NAME == "windows":
        roots = [os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")]
        subs = ["Java", "Eclipse Adoptium", "BellSoft", "AdoptOpenJDK", "Zulu"]
        for root in roots:
            if not root:
                continue
            for sub in subs:
                d = os.path.join(root, sub)
                if os.path.isdir(d):
                    for dirpath, _, filenames in os.walk(d):
                        if "javaw.exe" in filenames:
                            candidates.append(os.path.join(dirpath, "javaw.exe"))
    elif SYSTEM_NAME == "osx":
        jvm_root = "/Library/Java/JavaVirtualMachines"
        if os.path.isdir(jvm_root):
            for name in sorted(os.listdir(jvm_root)):
                exe2 = os.path.join(jvm_root, name, "Contents/Home/bin/java")
                if os.path.isfile(exe2):
                    candidates.append(exe2)
    else:  # linux
        for d in ["/usr/lib/jvm", os.path.expanduser("~/.jdks")]:
            if os.path.isdir(d):
                for name in sorted(os.listdir(d)):
                    exe2 = os.path.join(d, name, "bin/java")
                    if os.path.isfile(exe2):
                        candidates.append(exe2)
    return candidates[-1] if candidates else None


_JAVA_INFO_CACHE = {}         # exe -> ((mtime_ns, size), info dict)
_JAVA_VER_LOCK = threading.Lock()
_JAVA_LIST_CACHE = {"ts": 0.0, "items": []}
_JAVA_LIST_LOCK = threading.Lock()
_JAVA_LIST_TTL = 300          # 秒；新装 Java 后界面点「重新扫描」走 force


def _probe_exe(java_exe):
    """探测用的 exe。javaw.exe 是 GUI 版，-version 根本不往 stderr 写，
    以前拿它探测一律得到 0，导致「明明装了 Java 却说没装」。
    同目录有 java.exe 就换它探测（只影响探测，启动仍用传进来的路径）。"""
    d, name = os.path.split(java_exe)
    if name.lower() == "javaw.exe":
        alt = os.path.join(d, "java.exe")
        if os.path.isfile(alt):
            return alt
    return java_exe


_JAVA_VENDORS = (
    ("temurin", "Temurin"), ("adoptium", "Temurin"), ("adoptopenjdk", "AdoptOpenJDK"),
    ("zulu", "Zulu"), ("liberica", "Liberica"), ("corretto", "Corretto"),
    ("graalvm", "GraalVM"), ("dragonwell", "Dragonwell"), ("semeru", "Semeru"),
    ("openj9", "OpenJ9"), ("oracle", "Oracle"), ("java(tm)", "Oracle"),
    ("jetbrains", "JetBrains"), ("jbr", "JetBrains"), ("microsoft", "Microsoft"),
)


def _vendor_of(text, path):
    low = (text or "").lower()
    for key, name in _JAVA_VENDORS:
        if key in low:
            return name
    low2 = os.path.normpath(path or "").lower()
    for key, name in _JAVA_VENDORS:
        if key in low2:
            return name
    return "OpenJDK" if ("openjdk" in low or "jdk" in low2) else "Java"


def java_info(java_exe, force=False):
    """Java 详情 {path, major, full, vendor, bits, runtime}。
    按 exe 的修改时间缓存：状态接口每 2.5 秒问一轮，不能每次都起进程探测。"""
    key = None
    try:
        st = os.stat(java_exe)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        pass
    if key is not None and not force:
        with _JAVA_VER_LOCK:
            hit = _JAVA_INFO_CACHE.get(java_exe)
        if hit and hit[0] == key:
            return dict(hit[1])
    text, major, full, bits = "", 0, "", 0
    try:
        # CREATE_NO_WINDOW + 改用 java.exe 探测：打包后的窗口版不会再闪黑框
        out = subprocess.run([_probe_exe(java_exe), "-version"], capture_output=True,
                             text=True, timeout=15, **_nowin())
        text = (out.stderr or "") + (out.stdout or "")
        m = re.search(r'version "([^"]+)"', text)
        if m:
            full = m.group(1)
            mm = re.match(r"(\d+)(?:\.(\d+))?", full)
            if mm:
                # Java 8 报 "1.8.0_xxx"，取第二段
                major = int(mm.group(2)) if (mm.group(1) == "1" and mm.group(2)) \
                    else int(mm.group(1))
        if "64-Bit" in text:
            bits = 64
        elif "32-Bit" in text:
            bits = 32
    except Exception:
        pass
    low = os.path.normpath(java_exe).lower()
    info = {
        "path": java_exe,
        "major": major,
        "full": full,
        "vendor": _vendor_of(text, java_exe),
        "bits": bits,
        # 启动器 / 官方启动器自己下的运行时，专门给某个 MC 版本配的，最稳
        "runtime": low.startswith(os.path.normpath(RUNTIME_DIR).lower())
                   or (".minecraft" + os.sep + "runtime") in low,
    }
    if key is not None:
        with _JAVA_VER_LOCK:
            _JAVA_INFO_CACHE[java_exe] = (key, dict(info))
    return info


def java_version(java_exe):
    """java 大版本号（老接口，保留给调用方）"""
    return java_info(java_exe).get("major") or 0


# ---------- 自动下载对应版本的 Java ----------
RUNTIME_DIR = os.path.join(MC_DIR, "runtime")


def required_java_major(vjson):
    """该版本需要的 Java 大版本：优先取版本 JSON 的 javaVersion（沿继承链找根），
    缺失时按发布日期推断。1.17 官方标 16，统一用 17。"""
    seen, cur = set(), vjson
    while True:
        jv = (cur.get("javaVersion") or {}).get("majorVersion")
        if jv:
            m = int(jv)
            return 17 if m == 16 else m
        parent = cur.get("inheritsFrom")
        if not parent or parent in seen:
            break
        seen.add(parent)
        try:
            cur = load_version_json(parent)
        except Exception:
            break
    t = str(cur.get("releaseTime") or vjson.get("releaseTime") or "")[:10]
    if t >= "2024-04-23":
        return 21
    if t >= "2021-06-01":
        return 17
    return 8


def _java_matches(have_major, need):
    """Java 8 只能跑老版本；need>=17 时 17/21 都可"""
    if not have_major:
        return False
    if need <= 8:
        return have_major == 8
    return have_major >= need


def _registry_java_homes():
    """注册表里的 JavaHome（Oracle / Adoptium / Microsoft 装完都会写）"""
    out = []
    if SYSTEM_NAME != "windows":
        return out
    try:
        import winreg
    except Exception:
        return out
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\Java Development Kit"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\Java Runtime Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\JDK"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\JRE"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\JavaSoft\Java Development Kit"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\JavaSoft\Java Runtime Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Eclipse Adoptium\JDK"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Eclipse Foundation\JDK"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\JDK"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\JavaSoft\Java Development Kit"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Eclipse Adoptium\JDK"),
    ]

    def read_home(k):
        try:
            v, _ = winreg.QueryValueEx(k, "JavaHome")
            return v or ""
        except OSError:
            return ""

    for root, sub in roots:
        try:
            with winreg.OpenKey(root, sub) as k:
                home = read_home(k)          # 有些安装直接把 JavaHome 写在这一层
                if home:
                    out.append(home)
                for i in range(40):
                    try:
                        name = winreg.EnumKey(k, i)   # 否则下一层是版本号
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(k, name) as sk:
                            home = read_home(sk)
                            if home:
                                out.append(home)
                    except OSError:
                        pass
        except OSError:
            continue
    return out


def _scan_java_root(root, max_depth=6, found=None):
    """在 root 下找 bin/java(.exe)，限制深度，别把整块盘扫穿"""
    if found is None:
        found = []
    binname = "java.exe" if SYSTEM_NAME == "windows" else "java"
    root = os.path.abspath(root)
    base = root.rstrip("\\/").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        if dirpath.count(os.sep) - base >= max_depth:
            dirnames[:] = []
        if os.path.basename(dirpath) == "bin" and binname in filenames:
            found.append(os.path.join(dirpath, binname))
            dirnames[:] = []
    return found


def _java_scan_dirs():
    """候选根目录（只返回存在的），覆盖三大启动器 + 常见发行版 + 包管理器"""
    home = os.path.expanduser("~")
    ep = os.environ.get
    if SYSTEM_NAME == "windows":
        cands = [
            os.path.join(MC_DIR, "runtime"),
            os.path.join(ep("APPDATA", "") or "", ".minecraft", "runtime"),
            os.path.join(ep("LOCALAPPDATA", "") or "", "Programs", "Eclipse Adoptium"),
            os.path.join(ep("LOCALAPPDATA", "") or "", "Programs", "Java"),
            os.path.join(ep("LOCALAPPDATA", "") or "", "Programs", "Microsoft"),
            os.path.join(ep("LOCALAPPDATA", "") or "", "Programs", "Zulu"),
            os.path.join(home, ".jdks"),
            os.path.join(home, ".hmcl", "java"),
            os.path.join(home, ".pcl2", "java"),
            os.path.join(ep("ProgramFiles", "") or "", "Java"),
            os.path.join(ep("ProgramFiles", "") or "", "Eclipse Adoptium"),
            os.path.join(ep("ProgramFiles", "") or "", "BellSoft"),
            os.path.join(ep("ProgramFiles", "") or "", "Zulu"),
            os.path.join(ep("ProgramFiles", "") or "", "Amazon Corretto"),
            os.path.join(ep("ProgramFiles", "") or "", "Microsoft"),
            os.path.join(ep("ProgramFiles(x86)", "") or "", "Java"),
            os.path.join(home, "scoop", "apps", "openjdk"),
            r"C:\ProgramData\chocolatey\lib\openjdk",
        ]
    elif SYSTEM_NAME == "osx":
        cands = [
            "/Library/Java/JavaVirtualMachines",
            os.path.join(home, "Library", "Java", "JavaVirtualMachines"),
            os.path.join(MC_DIR, "runtime"),
            os.path.join(home, ".sdkman", "candidates", "java"),
            os.path.join(home, "Library", "Application Support", "minecraft", "runtime"),
            os.path.join(home, "Library", "Application Support", "modrinth", "java"),
        ]
    else:
        cands = [
            "/usr/lib/jvm", "/usr/java", "/opt/java",
            os.path.join(home, ".jdks"),
            os.path.join(home, ".sdkman", "candidates", "java"),
            os.path.join(MC_DIR, "runtime"),
            os.path.join(home, ".minecraft", "runtime"),
            os.path.join(home, ".local", "share", "PrismLauncher", "java"),
        ]
    out, seen = [], set()
    for d in cands:
        if not d:
            continue
        k = os.path.normcase(os.path.abspath(d))
        if k in seen:
            continue
        seen.add(k)
        if os.path.isdir(d):
            out.append(d)
    return out


def all_javas(force=False):
    """本机所有 Java（PATH + 注册表 + 常见安装目录 + 启动器自带 runtime）。
    结果缓存 5 分钟：状态接口每 2.5 秒问一次，不能每次都扫盘。"""
    now = time.time()
    if not force:
        with _JAVA_LIST_LOCK:
            if _JAVA_LIST_CACHE["items"] and now - _JAVA_LIST_CACHE["ts"] < _JAVA_LIST_TTL:
                return list(_JAVA_LIST_CACHE["items"])

    raw = []

    def add(p):
        if not p:
            return
        d, n = os.path.split(p)
        if n.lower() == "javaw.exe":
            alt = os.path.join(d, "java.exe")
            if os.path.isfile(alt):
                p = alt          # 统一用控制台版：日志能拿到输出，-version 也能读
        if os.path.isfile(p):
            raw.append(p)

    if SYSTEM_NAME == "windows":
        add(shutil.which("javaw") or shutil.which("java"))
    else:
        add(shutil.which("java"))
    for home in _registry_java_homes():
        add(os.path.join(home, "bin", "java.exe" if SYSTEM_NAME == "windows" else "java"))
    for d in _java_scan_dirs():
        for p in _scan_java_root(d):
            add(p)

    # 同一个 bin 目录算同一个 Java（javaw/java 并存、注册表与目录重复、
    # C:\Program Files\Java\latest\jdk-25 这类 junction 都靠这个去重）
    out, seen = [], set()
    for p in raw:
        d = os.path.dirname(os.path.abspath(p))
        try:
            d = os.path.realpath(d) or d
        except Exception:
            pass
        k = os.path.normcase(d)
        if k in seen:
            continue
        seen.add(k)
        out.append(p)

    with _JAVA_LIST_LOCK:
        _JAVA_LIST_CACHE["items"] = list(out)
        _JAVA_LIST_CACHE["ts"] = now
    return out


def _find_java_in(root):
    """在目录里找 bin/java(.exe)，Unix 下顺手加执行权限"""
    if not os.path.isdir(root):
        return None
    binname = "java.exe" if SYSTEM_NAME == "windows" else "java"
    for dirpath, dirnames, filenames in os.walk(root):
        if binname in filenames and os.path.basename(dirpath) == "bin":
            p = os.path.join(dirpath, binname)
            if SYSTEM_NAME != "windows":
                try:
                    os.chmod(p, 0o755)
                except Exception:
                    pass
            return p
    return None


def find_suitable_java(need):
    """最合适的 Java：版本最接近的优先（need=17 时 17 胜过 21/25，
    因为高版本跑老 mod 偶尔会炸），同版本优先启动器自带的 runtime。"""
    cands = []
    for exe in all_javas():
        info = java_info(exe)
        m = info.get("major") or 0
        if _java_matches(m, need):
            cands.append((m, bool(info.get("runtime")), exe))
    if not cands:
        return None
    cands.sort(key=lambda x: (abs(x[0] - need), not x[1], x[0], x[2].lower()))
    return cands[0][2]


def ensure_java(major):
    """本机没有就自动下载对应大版本的 Java 运行时
    （先 Mojang 官方运行时，不行换 BMCLAPI 镜像，都不行再用 Adoptium 打包兜底）"""
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    dest_root = os.path.join(RUNTIME_DIR, f"java-{major}")
    existing = _find_java_in(dest_root)
    if existing and _java_matches(java_version(existing), major):
        return existing

    def ok(p):
        return bool(p) and _java_matches(java_version(p), major)

    java_exe = _ensure_java_mojang(major, dest_root)
    # Mojang 的运行时只有 8/16/17/21 几档，要 25 时会给个 21 —— 装完必须核对，
    # 不对就用 Adoptium 下真正那个大版本，否则启动时版本不匹配还是炸
    if not ok(java_exe):
        if java_exe:
            log(f"{tr('Mojang 运行时给的是 Java ')}{java_version(java_exe)}"
                f"{tr('，不是要的 ')}{major}{tr('，改用 Adoptium')}")
        else:
            log(f"{tr('Mojang / BMCLAPI 都不可用，改用 Adoptium 兜底')}")
        java_exe = _ensure_java_adoptium(major)
    if java_exe:
        all_javas(force=True)          # 新装好的立刻能被自动选择看到
        log(f"Java {major}{tr(' 安装完成: ')}{java_exe}")
        set_progress(message=f"Java {major}{tr(' 就绪')}")
    return java_exe


# ---------- Mojang 官方 Java 运行时 ----------
# 固定哈希的运行时总清单（Mojang 官方启动器也在用这份）
MOJANG_RUNTIME_INDEX = ("https://piston-meta.mojang.com/v1/products/java-runtime/"
                        "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json")
_RUNTIME_COMPONENT = {8: "jre-legacy", 16: "java-runtime-alpha",
                      17: "java-runtime-gamma", 21: "java-runtime-delta"}
_RUNTIME_MAJOR = {"jre-legacy": 8, "java-runtime-alpha": 16, "java-runtime-beta": 17,
                  "java-runtime-gamma": 17, "java-runtime-delta": 21,
                  "java-runtime-epsilon": 21}


def _mojang_platform_key():
    """Mojang 运行时清单里的平台键名"""
    m = platform.machine().upper()
    if SYSTEM_NAME == "windows":
        if m in ("ARM64", "AARCH64"):
            return "windows-arm64"
        if m in ("X86", "I386", "I686"):
            return "windows-x86"
        return "windows-x64"
    if SYSTEM_NAME == "osx":
        return "mac-os-arm64" if platform.machine() == "arm64" else "mac-os"
    if m in ("ARM64", "AARCH64"):
        return "linux-arm64"
    if m in ("X86", "I386", "I686"):
        return "linux-i386"
    return "linux"


def _pick_mojang_component(plat_data, major):
    """挑最合适的大版本组件：优先精确匹配，其次选差得最近的"""
    comp = _RUNTIME_COMPONENT.get(major)
    if comp and plat_data.get(comp):
        return comp
    best, gap = None, 999
    for name, entries in plat_data.items():
        cm = _RUNTIME_MAJOR.get(name)
        if not cm or not entries:
            continue
        if abs(cm - major) < gap:
            best, gap = name, abs(cm - major)
    return best


def _ensure_java_mojang(major, dest_root):
    """下载 Mojang 官方 Java 运行时到 dest_root。
    official 失败自动换 bmclapi 镜像（同一份清单，只换域名）。成功返回 java 路径。"""
    plat = _mojang_platform_key()
    last = None
    for mirror in ("official", "bmclapi"):
        try:
            idx = fetch_json(MOJANG_RUNTIME_INDEX, mirror)
            plat_data = idx.get(plat) or {}
            comp = _pick_mojang_component(plat_data, major)
            if not comp:
                raise RuntimeError(tr("清单里没有适配本机平台的 Java 运行时"))
            mf_url = plat_data[comp][-1]["manifest"]["url"]
            mf = fetch_json(mf_url, mirror)
            files = []
            for rel, obj in (mf.get("files") or {}).items():
                if obj.get("type") != "file":
                    continue
                raw = obj.get("downloads", {}).get("raw") or {}
                if not raw.get("url"):
                    continue
                dest = os.path.join(dest_root, *rel.split("/"))
                files.append((mirror_url(raw["url"], mirror), dest,
                              raw.get("size", 0), raw.get("sha1")))
            if not files:
                raise RuntimeError(tr("清单里没有可下载的文件"))
            log(f"Java {major} ({comp}) [{MIRRORS[mirror]['name']}] "
                f"{len(files)}{tr(' 个文件')}: {mirror_url(mf_url, mirror)}")
            set_progress(message=f"{tr('正在下载 Java ')}{major}{tr(' 运行时…')}", percent=0,
                         total_files=len(files), done_files=0)
            if not download_all(files, mirror):
                raise RuntimeError(tr("部分文件下载失败"))
            java_exe = _find_java_in(dest_root)
            if not java_exe:
                raise RuntimeError(tr("安装目录里未找到 Java 可执行文件"))
            return java_exe
        except Cancelled:
            raise
        except Exception as e:
            last = e
            log(f"{tr('Java 源不可用(')}{MIRRORS[mirror]['name']}): {e}")
    return None


def _ensure_java_adoptium(major):
    """兜底：Adoptium 打包下载（清华镜像优先，Adoptium 官方 API 兜底）"""
    dest_root = os.path.join(RUNTIME_DIR, f"java-{major}")
    if SYSTEM_NAME == "windows":
        sysname, arch, ext = "windows", "x64", "zip"
    elif SYSTEM_NAME == "osx":
        sysname = "mac"
        arch = "aarch64" if platform.machine() == "arm64" else "x64"
        ext = "tar.gz"
    else:
        sysname, arch, ext = "linux", "x64", "tar.gz"
    img = "jdk" if major <= 8 else "jre"

    links = []
    # 1) 清华 TUNA 镜像（国内快）：列目录取最新的包
    it_types = ["jdk"] if major <= 8 else ["jre", "jdk"]
    for it in it_types:
        idx = f"https://mirrors.tuna.tsinghua.edu.cn/Adoptium/{major}/{it}/{arch}/{sysname}/"
        try:
            html = requests.get(idx, timeout=20).text
            zips = re.findall(r'href="([^"]+\.' + re.escape(ext) + r')"', html)
            if zips:
                links.append("https://mirrors.tuna.tsinghua.edu.cn" + zips[-1])
        except Exception as e:
            log(f"{tr('镜像列表获取失败(')}{idx}): {e}")
    # 2) Adoptium 官方 API（自动 302 到最近节点）
    links.append(f"https://api.adoptium.net/v3/binary/latest/{major}/ga/"
                 f"{sysname}/{arch}/{img}/hotspot/normal/eclipse?project=jdk")

    archive = os.path.join(RUNTIME_DIR, f"java-{major}.{ext}")
    last_err = None
    for url in links:
        try:
            log(f"{tr('下载 Java ')}{major}: {url}")
            set_progress(message=f"{tr('正在下载 Java ')}{major}{tr(' 运行时…')}", percent=0)
            with requests.get(url, stream=True, timeout=60, allow_redirects=True) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0) or 0)
                got = 0
                with open(archive, "wb") as f:
                    for chunk in r.iter_content(256 * 1024):
                        if is_cancelled():
                            f.close()
                            try:
                                os.remove(archive)
                            except Exception:
                                pass
                            raise Cancelled()
                        if chunk:
                            f.write(chunk)
                            got += len(chunk)
                            set_progress(message=f"{tr('正在下载 Java ')}{major}{tr(' 运行时…')}",
                                         percent=int(got * 100 / total) if total else 0,
                                         downloaded=got, total=total)
            break
        except Cancelled:
            raise
        except Exception as e:
            last_err = e
            log(f"{tr('下载失败: ')}{e}")
    else:
        raise RuntimeError(f"Java {major}{tr(' 自动下载失败: ')}{last_err}")

    set_progress(message=f"{tr('正在解压 Java ')}{major}…")
    tmp = os.path.join(RUNTIME_DIR, f"_extract_{major}")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        if ext == "zip":
            with zipfile.ZipFile(archive) as z:
                for m in z.infolist():
                    name = os.path.normpath(m.filename)
                    if name.startswith("..") or os.path.isabs(name):
                        continue  # 防 zip 路径穿越
                    z.extract(m, tmp)
        else:
            with tarfile.open(archive) as t:
                t.extractall(tmp)  # noqa: S202 仅解压自 Adoptium 官方包
    finally:
        try:
            os.remove(archive)
        except Exception:
            pass

    if not _find_java_in(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(tr("解压后未找到 Java 可执行文件"))
    if os.path.isdir(dest_root):
        shutil.rmtree(dest_root, ignore_errors=True)
    tops = os.listdir(tmp)
    src = os.path.join(tmp, tops[0]) if len(tops) == 1 else tmp
    shutil.move(src, dest_root)
    shutil.rmtree(tmp, ignore_errors=True)
    java_exe = _find_java_in(dest_root)
    log(f"Java {major}{tr(' 安装完成: ')}{java_exe}")
    set_progress(message=f"Java {major}{tr(' 就绪')}")
    return java_exe


def build_launch_command(vjson, vid, username, ram_mb, java_exe, account=None):
    cp_sep = ";" if os.name == "nt" else ":"
    # 沿 inheritsFrom 找到最底层原版（客户端 jar 所在）
    base_id = vid
    _cur, _j, _seen = vid, vjson, set()
    while _j.get("inheritsFrom") and _cur not in _seen:
        _seen.add(_cur)
        _cur = _j["inheritsFrom"]
        _p = os.path.join(VERSIONS_DIR, _cur, _cur + ".json")
        if not os.path.isfile(_p):
            break
        try:
            with open(_p, "r", encoding="utf-8") as _f:
                _j = json.load(_f)
        except Exception:
            break
        base_id = _cur
    vid_dir = os.path.join(VERSIONS_DIR, vid)
    base_dir = os.path.join(VERSIONS_DIR, base_id)
    client_jar = os.path.join(base_dir, base_id + ".jar")
    if not os.path.isfile(client_jar):
        # 自包含版本复制 jar 失败时的兜底：回到本体目录里拿
        _ab = vjson.get("_aerobase") or ""
        if _ab and os.path.isfile(os.path.join(VERSIONS_DIR, _ab, _ab + ".jar")):
            client_jar = os.path.join(VERSIONS_DIR, _ab, _ab + ".jar")
            base_dir = os.path.join(VERSIONS_DIR, _ab)
    # natives 优先用父版本目录（原版安装时解压的）
    if os.path.isdir(os.path.join(base_dir, NATIVES_DIRNAME)):
        natives_dir = os.path.join(base_dir, NATIVES_DIRNAME)
    else:
        natives_dir = os.path.join(vid_dir, NATIVES_DIRNAME)

    classpath = [client_jar]
    for lib in vjson.get("libraries", []):
        if not rules_allow(lib.get("rules")):
            continue
        artifact = lib.get("downloads", {}).get("artifact")
        natives = lib.get("natives") or {}
        if natives.get(SYSTEM_NAME):
            continue  # natives 不进 classpath
        if artifact:
            classpath.append(os.path.join(LIBRARIES_DIR, artifact["path"]))
        elif not lib.get("downloads"):
            path, _, _, _ = lib_coords_path(lib["name"])
            if lib.get("url"):
                classpath.append(os.path.join(LIBRARIES_DIR, path))
    classpath_str = cp_sep.join(classpath)

    is_msa = bool(account and account.get("type") == "msa"
                  and account.get("uuid") and account.get("accessToken"))
    is_third = bool(account and account.get("type") == "authlib"
                    and account.get("uuid") and account.get("accessToken"))
    if is_msa or is_third:
        auth_uuid = str(account["uuid"]).replace("-", "")
        auth_token = account["accessToken"]
        auth_name = account.get("name") or username
        auth_type = "msa" if is_msa else "mojang"
    else:
        auth_uuid = _offline_uuid(username)
        auth_token = "0"
        auth_type = "legacy"
        auth_name = username

    subs = {
        "natives_directory": natives_dir,
        "classpath": classpath_str,
        "classpath_separator": cp_sep,
        "library_directory": LIBRARIES_DIR,
        "launcher_name": LAUNCHER_NAME,
        "launcher_version": LAUNCHER_VERSION,
        "auth_player_name": auth_name,
        "auth_uuid": auth_uuid,
        "auth_access_token": auth_token,
        "auth_xuid": "0",
        "clientid": "0",
        "user_type": auth_type,
        "version_name": vid,
        "version_type": vjson.get("type", "release"),
        "game_directory": game_dir_for(vid),
        "assets_root": ASSETS_DIR,
        "game_assets": ASSETS_DIR,
        "assets_index_name": vjson.get("assets", "legacy"),
        "user_properties": "{}",
        "resolution_width": "854",
        "resolution_height": "480",
    }
    # log4j 配置
    logcfg = vjson.get("logging", {}).get("client", {})
    if logcfg.get("file", {}).get("url"):
        f = logcfg["file"]
        log_path = os.path.join(ASSETS_DIR, "log_configs", f["id"])
        if not os.path.isfile(log_path):
            try:
                download_file(f["url"], log_path, "official", f.get("size"), f.get("sha1"))
            except Exception as e:
                log(f"{tr('log4j 配置下载失败(不影响启动): ')}{e}")
        if os.path.isfile(log_path):
            subs["path"] = log_path

    def sub(s):
        for k, v in subs.items():
            s = s.replace("${" + k + "}", str(v))
        return s

    cmd = [java_exe, f"-Xmx{ram_mb}m", f"-Xms{max(512, ram_mb // 2)}m"]
    if is_third:
        # 第三方登录：通过 authlib-injector 注入认证服务器
        try:
            jar = ensure_authlib_injector()
            cmd.append(f"-javaagent:{jar}={account['server']}")
            log(f"{tr('已启用第三方登录：')}{account.get('server')}")
        except Exception as e:
            log(f"{tr('authlib-injector 加载失败，将以离线身份启动: ')}{e}")
    args = vjson.get("arguments", {})
    if args.get("jvm"):
        for a in args["jvm"]:
            if isinstance(a, str):
                if "${path}" in a and "path" not in subs:
                    continue
                cmd.append(sub(a))
            elif rules_allow(a.get("rules")):
                vals = a.get("value", [])
                vals = vals if isinstance(vals, list) else [vals]
                for v in vals:
                    cmd.append(sub(v))
    else:
        cmd += ["-Djava.library.path=" + natives_dir, "-cp", classpath_str]

    cmd.append(vjson["mainClass"])

    if args.get("game"):
        for a in args["game"]:
            if isinstance(a, str):
                if "${" in sub(a) and "${" in a:
                    continue  # 无法解析的占位符直接跳过
                cmd.append(sub(a))
            elif rules_allow(a.get("rules")):
                vals = a.get("value", [])
                vals = vals if isinstance(vals, list) else [vals]
                for v in vals:
                    s = sub(v)
                    if "${" not in s or "${" not in v:
                        cmd.append(s)
    else:
        cmd += sub(vjson.get("minecraftArguments", "")).split(" ")

    return cmd


def _offline_uuid(name):
    data = hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest()
    b = bytearray(data)
    b[6] = (b[6] & 0x0F) | 0x30
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(b)))


# ---------- 安装流程 ----------
def resolve_inherits(j, depth=0):
    """处理 inheritsFrom 继承（Fabric/Forge/OptiFine 版本 json）"""
    pid = j.get("inheritsFrom")
    if not pid or depth > 3:
        return j
    pp = os.path.join(VERSIONS_DIR, pid, pid + ".json")
    if not os.path.isfile(pp):
        return j
    with open(pp, "r", encoding="utf-8") as f:
        parent = json.load(f)
    parent = resolve_inherits(parent, depth + 1)
    merged = dict(parent)
    for k, v in j.items():
        if k == "libraries":
            merged["libraries"] = parent.get("libraries", []) + v
        elif k == "arguments" and isinstance(v, dict) and isinstance(merged.get("arguments"), dict):
            args = dict(merged["arguments"])
            for ak, av in v.items():
                if isinstance(av, list) and isinstance(args.get(ak), list):
                    args[ak] = args[ak] + av
                else:
                    args[ak] = av
            merged["arguments"] = args
        else:
            merged[k] = v
    merged["id"] = j.get("id", merged.get("id"))
    return merged


def load_version_json(vid):
    p = os.path.join(VERSIONS_DIR, vid, vid + ".json")
    if not os.path.isfile(p):
        raise RuntimeError(f"{vid}{tr(' 未安装')}")
    with open(p, "r", encoding="utf-8") as f:
        return resolve_inherits(json.load(f))


# ---------- PCL2 风格的自包含版本 ----------
# PCL2 装完加载器不会留一个「薄壳」，而是把 inheritsFrom 展开成一份完整 json，
# 再把客户端 jar 和 natives 复制进自己的目录，最后重命名成 1.20.1-Fabric_0.15.7。
# 这样版本列表里只有一条、名字能看懂，删掉原版也照样能启动。
LOADER_TAG = {"fabric": "Fabric", "quilt": "Quilt", "forge": "Forge",
              "neoforge": "NeoForge", "optifine": "OptiFine"}


def loader_dir_name(mc, loader, ver):
    """PCL2 的版本命名：<原版>-<加载器>_<加载器版本>，Forge 的 ver 常带 mc 前缀，去掉"""
    v = ver or ""
    if mc and v.startswith(mc + "-"):
        v = v[len(mc) + 1:]
    return f"{mc}-{LOADER_TAG.get(loader, loader.title())}_{v}"


def _lib_key(name):
    """去重键：带分类器的库（xxx:natives-windows 之类）必须按全名算，
    只按 组:名 去重会把 natives 当成主 jar 的替换品，LWJGL/jtracy 的主 jar
    会被顶掉，游戏直接 NoClassDefFoundError。"""
    parts = (name or "").split(":")
    # g:a:v[:classifier[:…]] —— 没有分类器才允许子版本按 组:名 覆盖父版本
    return name if len(parts) >= 4 else ":".join(parts[:2])


def _merge_libraries(libs):
    """同一 坐标 只留最后一份（子版本声明的库版本覆盖父版本的）；分类器变体互不覆盖"""
    idx, out = {}, []
    for lib in libs:
        key = _lib_key(lib.get("name") or "")
        if key and key in idx:
            out[idx[key]] = lib
        elif key:
            idx[key] = len(out)
            out.append(lib)
        else:
            out.append(lib)
    return out


def repair_merged_versions():
    """修复旧版合并时被 natives 顶掉主 jar 的自包含版本（3.4.44~3.4.46 的坑）。

    判据：json 带 _aerobase 且本体原版的某些库（按全名）不在自己的库列表里。
    修复方式：本体库 + 现有库 用新规则重新去重写回。jar 文件本来就在
    libraries/ 里（原版装全了），修 json 即可，不用动文件。"""
    if not os.path.isdir(VERSIONS_DIR):
        return []
    fixed = []
    for vid in list(os.listdir(VERSIONS_DIR)):
        jp = os.path.join(VERSIONS_DIR, vid, vid + ".json")
        if not os.path.isfile(jp):
            continue
        try:
            with open(jp, "r", encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            continue
        base = j.get("_aerobase")
        if not base or not j.get("libraries"):
            continue
        bp = os.path.join(VERSIONS_DIR, base, base + ".json")
        if not os.path.isfile(bp):
            continue
        try:
            with open(bp, "r", encoding="utf-8") as f:
                base_libs = json.load(f).get("libraries") or []
        except Exception:
            continue
        names = {l.get("name") for l in j["libraries"]}
        missing = [l for l in base_libs if l.get("name") and l["name"] not in names]
        if not missing:
            continue
        j["libraries"] = _merge_libraries(base_libs + j["libraries"])
        try:
            with open(jp, "w", encoding="utf-8") as f:
                json.dump(j, f, ensure_ascii=False, indent=2)
            _META_CACHE.pop(vid, None)
            fixed.append(vid)
            log(f"{tr('已修复 ')}{vid}{tr(' 缺失的 ')}{len(missing)}{tr(' 个库')}")
        except Exception as e:
            log(f"{tr('修复 ')}{vid}{tr(' 失败: ')}{e}")
    if fixed:
        _SIZE_CACHE.clear()
    return fixed


def _repoint_dependents(old_vid, new_vid):
    """把 inheritsFrom 指向 old_vid 的版本改指向 new_vid（薄壳目录被清掉之前必须做）"""
    for name in _dependents(old_vid):
        p = os.path.join(VERSIONS_DIR, name, name + ".json")
        try:
            with open(p, "r", encoding="utf-8") as f:
                j = json.load(f)
            if j.get("inheritsFrom") != old_vid:
                continue
            j["inheritsFrom"] = new_vid
            with open(p, "w", encoding="utf-8") as f:
                json.dump(j, f, ensure_ascii=False, indent=2)
            _META_CACHE.pop(name, None)
            log(f"{tr('已把 ')}{name}{tr(' 的依赖改到 ')}{new_vid}")
        except Exception:
            pass


def _copy_file_progress(src, dst):
    """分块复制大文件并刷进度百分比（复制几百 MB 的客户端 jar 时让用户看得到动静）"""
    size = os.path.getsize(src) or 1
    copied = 0
    msg = tr("正在整理版本目录…")
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            buf = fsrc.read(512 * 1024)
            if not buf:
                break
            fdst.write(buf)
            copied += len(buf)
            set_progress(percent=min(99, int(copied * 100 / size)), message=msg)


def realize_version(src_vid, target_vid=None):
    """把 inheritsFrom 薄壳展开成自包含实例（对齐 PCL2 的安装结果）。

    合并后的 json 不带 inheritsFrom，客户端 jar 与 natives 各复制一份到自己的
    目录，所以原版删了也能启动；目录名同时换成 <原版>-<加载器>_<版本>。
    """
    target_vid = target_vid or src_vid
    src_dir = os.path.join(VERSIONS_DIR, src_vid)
    dst_dir = os.path.join(VERSIONS_DIR, target_vid)
    base_id = _vanilla_of(src_vid) or ""
    merged = load_version_json(src_vid)
    merged["id"] = target_vid
    merged.pop("inheritsFrom", None)
    merged["libraries"] = _merge_libraries(merged.get("libraries") or [])
    if base_id and base_id != target_vid:
        merged["_aerobase"] = base_id        # 自己用：记着本体是谁（MC 会忽略未知字段）
    os.makedirs(dst_dir, exist_ok=True)

    set_progress(message=tr("正在整理版本目录…"), percent=0)
    if base_id:
        src_jar = os.path.join(VERSIONS_DIR, base_id, base_id + ".jar")
        dst_jar = os.path.join(dst_dir, target_vid + ".jar")
        if os.path.isfile(src_jar) and not os.path.isfile(dst_jar):
            # 复制而不是硬链接：原版重装时会原地截断，硬链接会连带毁掉这一份
            _copy_file_progress(src_jar, dst_jar)
        nat_src = os.path.join(VERSIONS_DIR, base_id, NATIVES_DIRNAME)
        nat_dst = os.path.join(dst_dir, NATIVES_DIRNAME)
        if os.path.isdir(nat_src) and not os.path.isdir(nat_dst):
            try:
                shutil.copytree(nat_src, nat_dst)
            except Exception as e:
                log(f"{tr('复制 natives 失败（启动时会自动补）: ')}{e}")

    with open(os.path.join(dst_dir, target_vid + ".json"), "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    if os.path.normcase(src_dir) != os.path.normcase(dst_dir) and os.path.isdir(src_dir):
        # 旧目录里的存档 / mods / 设置整包搬过去，不能让人以为档丢了
        skip = {src_vid + ".json", src_vid + ".jar", NATIVES_DIRNAME, src_vid + "-natives"}
        for entry in os.listdir(src_dir):
            if entry in skip:
                continue
            s, d = os.path.join(src_dir, entry), os.path.join(dst_dir, entry)
            if os.path.exists(d):
                continue
            try:
                shutil.move(s, d)
            except Exception as e:
                log(f"{tr('搬迁 ')}{entry}{tr(' 失败: ')}{e}")
        _repoint_dependents(src_vid, target_vid)
        shutil.rmtree(src_dir, ignore_errors=True)
    _SIZE_CACHE.clear()
    refresh_installed()
    set_progress(percent=100, message=tr("版本已整理为 ") + target_vid)
    record_event("tidy", target_vid)
    log(f"{tr('版本已整理为 ')}{target_vid}")
    return target_vid


def normalize_loader_versions():
    """把以前留下的薄壳加载器版本整理成 PCL2 风格（带 jar 的独立副本不动）。

    每个版本登记成一个下载任务，左下角的小球和浮层就能看到整理进度。
    """
    if not os.path.isdir(VERSIONS_DIR) or active_tasks():
        return []
    done = []
    for vid in list(os.listdir(VERSIONS_DIR)):
        jp = os.path.join(VERSIONS_DIR, vid, vid + ".json")
        if not os.path.isfile(jp) or task_busy(f"tidy:{vid}"):
            continue
        try:
            with open(jp, "r", encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            continue
        if not j.get("inheritsFrom"):
            continue                      # 原版，或已经整理过的自包含版本
        if os.path.isfile(os.path.join(VERSIONS_DIR, vid, vid + ".jar")):
            continue                      # 自带 jar 的完整副本（PCL2 装的），不动它
        kind, lver = _detect_loader(j, vid)
        if kind.lower() not in LOADER_TAG or not lver:
            continue
        mc = _vanilla_of(vid)
        target = loader_dir_name(mc, kind.lower(), lver) if mc else ""
        if not mc or target == vid or os.path.isdir(os.path.join(VERSIONS_DIR, target)):
            continue
        own = cur_task() is None
        if own and new_task(f"{tr('整理版本 ')}{vid}", key=f"tidy:{vid}") is None:
            continue
        try:
            set_progress(message=tr("正在整理版本目录…"), percent=0)
            realize_version(vid, target)
            done.append(target)
            if own:
                end_task("done", f"{target}{tr(' 整理完成！')}")
        except Exception as e:
            log(f"{tr('整理 ')}{vid}{tr(' 失败: ')}{e}")
            if own:
                end_task("error", f"{tr('整理 ')}{vid}{tr(' 失败: ')}{e}")
    return done


def install_core(version_id, version_url, mirror):
    log(f"{tr('开始安装 ')}{version_id}（{MIRRORS[mirror]['name']}）")
    vjson = fetch_json(version_url, mirror)
    vid_dir = os.path.join(VERSIONS_DIR, version_id)
    os.makedirs(vid_dir, exist_ok=True)
    with open(os.path.join(vid_dir, version_id + ".json"), "w", encoding="utf-8") as f:
        json.dump(vjson, f, ensure_ascii=False, indent=2)

    if is_cancelled():
        raise Cancelled()

    files, natives_jars = collect_version_files(vjson, mirror)
    log(f"{tr('需要下载 ')}{len(files)}{tr(' 个文件（客户端/依赖库）')}")
    if not download_all(files, mirror):
        raise RuntimeError(tr("存在下载失败的文件，请重试"))
    if is_cancelled():
        raise Cancelled()

    set_progress(message=tr("正在下载资源文件（音效/语言包）…"))
    asset_files = collect_asset_files(vjson, mirror)
    log(f"{tr('资源文件 ')}{len(asset_files)}{tr(' 个')}")
    if asset_files and not download_all(asset_files, mirror):
        log(tr("部分资源文件下载失败，可能影响音效，可重试"))

    set_progress(message=tr("正在解压 natives…"))
    extract_natives(natives_jars, os.path.join(vid_dir, NATIVES_DIRNAME))

    required = os.path.join(vid_dir, version_id + ".jar")
    if not os.path.isfile(required):
        raise RuntimeError(tr("客户端 jar 未下载成功"))
    log(f"{version_id}{tr(' 安装完成')}")


def install_thread(version_id, version_url, mirror):
    if new_task(f"{tr('安装 ')}{version_id}", key=f"ver:{version_id}") is None:
        return
    try:
        set_progress(message=tr("正在获取版本信息…"), percent=0)
        install_core(version_id, version_url, mirror)
        write_launcher_meta(version_id, mc=version_id)
        end_task("done", f"{version_id}{tr(' 安装完成！')}")
        refresh_installed()
    except Exception as e:
        report_error(tr("安装出错"), e)


def launch_thread(vid, username, ram_mb, java_override, account=None):
    try:
        if os.environ.get("MC_LAUNCHER_MOBILE"):
            raise RuntimeError(
                tr("手机版暂不支持在此设备上启动游戏（Java 运行时适配中）。"
                   "本应用目前可用于浏览/下载游戏文件与管理账户，请使用电脑版启动游戏。"))
        set_progress(message=f"{tr('正在启动 ')}{vid}…")
        if account and account.get("type") == "msa":
            log(f"{tr('使用正版账户 ')}{account.get('name')}")
            if not msa_refresh_account(account):
                log(tr("提示: 正版凭证刷新失败，若无法进入正版服务器请重新登录"))
            username = account.get("name") or username
        elif account and account.get("type") == "offline":
            # 离线账户：游戏内名字必须用账户名（此前漏了这一支，
            # 导致离线名字不生效、游戏里一直是默认的 Steve）
            username = (account.get("name") or username).strip() or username
            log(f"{tr('使用离线账户 ')}{username}")
        elif account and account.get("type") == "authlib":
            log(f"{tr('使用第三方账户 ')}{account.get('name')}（{account.get('server')}）")
            if not ygg_refresh_token(account):
                log(tr("提示: 登录凭证可能已过期，若无法进入服务器请重新登录账户"))
            else:
                # 刷新后的 token 写回账户文件
                try:
                    d = load_accounts()
                    for i, a in enumerate(d["accounts"]):
                        if a.get("id") == account.get("id"):
                            d["accounts"][i] = dict(a, **account)
                    save_accounts(d)
                except Exception:
                    pass
            username = account.get("name") or username
        log(f"{tr('启动 ')}{vid}{tr(' 玩家=')}{username}{tr(' 内存=')}{ram_mb}MB")
        seed_options(vid)
        vjson = load_version_json(vid)
        ensure_version_files(vid, vjson, load_config().get("mirror", "official"))

        need_major = required_java_major(vjson)
        log(f"{tr('Java 需求: ')}{vid}{tr(' 需要 Java ')}{need_major}")

        if java_override:
            # 用户手动指定了 Java：尊重选择，但版本不满足时明确报错
            if not os.path.isfile(java_override):
                raise RuntimeError(
                    f"{tr('设置里指定的 Java 路径不存在：')}{java_override}\n"
                    f"{tr('请清空设置里的 Java 路径，交给自动选择。')}")
            java_exe = java_override
            have = java_version(java_exe)
            if not _java_matches(have, need_major):
                raise RuntimeError(
                    f"{vid}{tr(' 需要 Java ')}{need_major}{tr('，当前手动指定的 Java 版本为 ')}"
                    f"{have or '?'}{tr('。请更换路径或清空设置里的 Java 路径让其自动下载。')}")
        else:
            java_exe = find_suitable_java(need_major)
            if java_exe:
                info = java_info(java_exe)
                log(f"{tr('自动选用 Java ')}{info.get('major')}"
                    f"（{info.get('vendor') or 'Java'}）: {java_exe}")
            else:
                log(f"{tr('本机没有合适的 Java ')}{need_major}{tr('，开始自动下载…')}")
                java_exe = ensure_java(need_major)
                if not java_exe:
                    raise RuntimeError(
                        f"{tr('需要 Java ')}{need_major}{tr('，自动下载失败。请检查网络，或在设置里手动指定 Java 路径。')}")
                if not _java_matches(java_version(java_exe), need_major):
                    log(f"{tr('警告：自动下载到的 Java ')}{java_version(java_exe)}"
                        f"{tr(' 不满足需求 ')}{need_major}{tr('，可能启动失败')}")

        # natives 目录为空时自动补解压（防止安装时遗漏）
        base_id = vjson.get("inheritsFrom", vid)
        for d in (vid, base_id):
            nat_dir = os.path.join(VERSIONS_DIR, d, NATIVES_DIRNAME)
            if os.path.isdir(nat_dir) and os.listdir(nat_dir):
                break
        else:
            log(tr("natives 缺失，正在补解压…"))
            _, natives_jars = collect_version_files(vjson, "bmclapi")
            extract_natives(natives_jars, os.path.join(VERSIONS_DIR, vid, NATIVES_DIRNAME))

        cmd = build_launch_command(vjson, vid, username, ram_mb, java_exe, account=account)
        os.makedirs(LOGS_DIR, exist_ok=True)
        logfile = open(os.path.join(LOGS_DIR, f"game_{vid}.log"), "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(cmd, cwd=MC_DIR, stdout=logfile,
                                stderr=subprocess.STDOUT, **_nowin())
        log(f"{tr('游戏进程已启动 PID=')}{proc.pid}{tr('，日志: launcher_logs/game_')}{vid}.log")
        set_progress(message=f"{tr('游戏已启动（PID ')}{proc.pid}{tr('），稍等片刻进入主界面')}")
        record_event("launch", f"{vid} pid={proc.pid}")
        threading.Thread(target=_watch_game, args=(proc, vid), daemon=True).start()
    except Exception as e:
        if isinstance(e, Cancelled):
            log(tr("启动已取消"))
            set_progress(message=tr("启动已取消"))
        else:
            log(f"{tr('启动失败: ')}{e}")
            set_progress(message=f"{tr('启动失败: ')}{e}")


def _watch_game(proc, vid):
    """盯着游戏进程：正常退出记一笔；崩溃（非 0 退出码）把日志尾部存进会话，
    下次启动弹窗分析「上次游戏为什么崩」。"""
    try:
        code = proc.wait()
    except Exception:
        return
    record_event("game_exit", f"{vid} exit={code}")
    if code != 0:
        d = _session_file["data"]
        if d is not None:
            try:
                with open(os.path.join(LOGS_DIR, f"game_{vid}.log"),
                          encoding="utf-8", errors="replace") as f:
                    d["crash_log"] = f.read()[-8000:]
                d["crash_vid"] = vid
                _write_session()
                log(f"{vid}{tr(' 异常退出（exit=')}{code}{tr('），崩溃日志已存入会话记录')}")
            except Exception:
                pass


# ---------- 模组加载器 ----------
FORGE_PROMOS_URL = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
FORGE_MAVEN_MIRRORS = [
    "https://bmclapi2.bangbang93.com/maven/net/minecraftforge/forge/{v}/forge-{v}-installer.jar",
    "https://maven.minecraftforge.net/net/minecraftforge/forge/{v}/forge-{v}-installer.jar",
]
NEOFORGE_META = "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
NEOFORGE_INSTALLER = "https://maven.neoforged.net/releases/net/neoforged/neoforge/{v}/neoforge-{v}-installer.jar"
FABRIC_META = "https://meta.fabricmc.net/v2/versions/loader/{mc}"
FABRIC_PROFILE = "https://meta.fabricmc.net/v2/versions/loader/{mc}/{ver}/profile/json"
OPTIFINE_LIST = "https://bmclapi2.bangbang93.com/optifine/list"
OPTIFINE_DL = "https://bmclapi2.bangbang93.com/optifine/{mc}/{t}/{p}"


def get_loader_options(mc):
    """返回四种加载器的可用版本（互相独立，出错不影响其他）"""
    out = {"forge": {}, "neoforge": [], "fabric": [], "optifine": [], "errors": {}}
    # Forge：官方 promotions（推荐/最新），版本全名 = mc-构建号
    try:
        r = requests.get(FORGE_PROMOS_URL, timeout=15)
        promos = r.json().get("promos", {})
        rec = promos.get(f"{mc}-recommended")
        lat = promos.get(f"{mc}-latest")
        lst = []
        if rec: lst.append({"name": f"{mc}-{rec}", "tag": tr("推荐版")})
        if lat and lat != rec: lst.append({"name": f"{mc}-{lat}", "tag": tr("最新版")})
        out["forge"] = lst
    except Exception as e:
        out["errors"]["forge"] = str(e)
    # NeoForge：maven-metadata，按 MC 版本前缀过滤
    try:
        r = requests.get(NEOFORGE_META, timeout=20)
        vers = re.findall(r"<version>([^<]+)</version>", r.text)
        prefix = mc[2:] if mc.startswith("1.") else mc  # 1.21.1 -> 21.1
        cand = [v for v in vers if v.startswith(prefix + ".") or v.startswith(mc + "-")]
        cand.reverse()
        out["neoforge"] = cand[:30]
    except Exception as e:
        out["errors"]["neoforge"] = str(e)
    # Fabric：官方 meta
    try:
        r = requests.get(FABRIC_META.format(mc=mc), timeout=20)
        out["fabric"] = [e["loader"]["version"] for e in r.json()][:20]
    except Exception as e:
        out["errors"]["fabric"] = str(e)
    # OptiFine：官网下载页解析（BMCLAPI 列表接口已失效）
    try:
        out["optifine"] = get_optifine_versions(mc)
    except Exception as e:
        out["errors"]["optifine"] = str(e)
    return out


def get_optifine_versions(mc):
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "Chrome/120.0 Safari/537.36"}
    r = requests.get("https://optifine.net/downloads", headers=h, timeout=30)
    r.raise_for_status()
    m = re.search(r"<h2>Minecraft\s+" + re.escape(mc) + r"</h2>(.*?)(?:<h2>|</body>)",
                  r.text, re.S)
    if not m:
        return []
    jars = re.findall(r"f=(OptiFine_[\w.]+\.jar)", m.group(1))
    out, seen = [], set()
    for j in jars:
        mm = re.match(r"OptiFine_(\d+(?:\.\d+)*)_(\w+)_(\w+)\.jar", j)
        if not mm or mm.group(1) != mc:
            continue
        name = f"{mm.group(2)}_{mm.group(3)}"
        if name not in seen:
            seen.add(name)
            out.append({"name": name})
    return out


def _download_first(urls, dest, log):
    """依次尝试多个 URL 下载"""
    last = None
    for u in urls:
        try:
            log(f"{tr('下载 ')}{u.split('/')[2]}…")
            download_file(u, dest, "official")
            return
        except Cancelled:
            raise
        except Exception as e:
            last = e
            log(f"{tr('  该源失败: ')}{e}")
    raise RuntimeError(f"{tr('安装器下载失败: ')}{last}")


def _run_installer(jar_path, log, mode="forge", java_need=8):
    # Forge/NeoForge 1.17+ 的安装器自身需要 Java 17+ 才能运行
    java = find_suitable_java(java_need)
    if not java:
        log(f"{tr('本机没有合适的 Java ')}{java_need}{tr('，自动下载中…')}")
        java = ensure_java(java_need)
    if not java:
        raise RuntimeError(f"{tr('安装器需要 Java ')}{java_need}{tr('，自动下载失败，请检查网络')}")
    log(f"{tr('安装器使用 Java ')}{java_version(java)}: {java}")
    log(tr("运行安装器（可能需要几分钟，会联网下载依赖库）…"))
    env = dict(os.environ)
    if mode == "optifine":
        # OptiFine 安装器不支持目录参数：
        # Windows 固定读 %APPDATA%\.minecraft → 注入 APPDATA 重定向；
        # macOS 固定读 ~/Library/Application Support/minecraft → 建符号链接。
        if SYSTEM_NAME == "windows":
            env["APPDATA"] = os.path.dirname(MC_DIR)
        elif SYSTEM_NAME == "osx":
            default_dir = os.path.expanduser("~/Library/Application Support/minecraft")
            if not os.path.islink(default_dir) and not os.path.isdir(default_dir):
                try:
                    os.makedirs(os.path.dirname(default_dir), exist_ok=True)
                    os.symlink(MC_DIR, default_dir)
                    log(f"{tr('已创建符号链接 ')}{default_dir} -> {MC_DIR}")
                except Exception as e:
                    log(f"{tr('符号链接创建失败(不影响安装流程): ')}{e}")
        cmd = [java, "-jar", jar_path, "--install", MC_DIR]
    else:
        # Forge 安装器要求目标目录存在 launcher_profiles.json
        lp = os.path.join(MC_DIR, "launcher_profiles.json")
        if not os.path.isfile(lp):
            with open(lp, "w", encoding="utf-8") as f:
                json.dump({"profiles": {}, "settings": {}, "version": 3}, f)
        cmd = [java, "-jar", jar_path, "--installClient", MC_DIR]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", cwd=MC_DIR, env=env, **_nowin())
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log("  " + line)
        if is_cancelled():
            try:
                proc.kill()
            except Exception:
                pass
            raise Cancelled()
    proc.wait()
    if proc.returncode != 0:
        if is_cancelled():
            raise Cancelled()
        raise RuntimeError(f"{tr('安装器退出码 ')}{proc.returncode}{tr('，请查看日志')}")


def install_loader_thread(loader, mc, ver, mirror):
    tmp = LOADER_CACHE
    os.makedirs(tmp, exist_ok=True)
    # 整合包流程里会嵌套调用本函数：那时已经有任务了，别再建一个
    own = cur_task() is None
    if own and new_task(f"{tr('安装 ')}{loader} {ver}",
                        key=f"loader:{loader}:{mc}:{ver}") is None:
        return
    try:
        set_progress(message=f"{tr('正在安装 ')}{loader} {ver} …", percent=0)
        # 1. 确保原版已安装
        if not os.path.isfile(os.path.join(VERSIONS_DIR, mc, mc + ".jar")):
            set_progress(message=f"{tr('先安装原版 ')}{mc}…")
            vl = get_version_list(mirror)
            url = next((v["url"] for v in vl if v["id"] == mc), None)
            if not url:
                raise RuntimeError(f"{tr('找不到原版 ')}{mc}")
            install_core(mc, url, mirror)
            set_progress(installing=True, message=f"{tr('继续安装 ')}{loader}…")
        if is_cancelled():
            raise Cancelled()

        if loader == "fabric":
            set_progress(message=tr("获取 Fabric 版本数据…"))
            r = requests.get(FABRIC_PROFILE.format(mc=mc, ver=ver), timeout=60)
            r.raise_for_status()
            profile = r.json()
            vid = profile["id"]
            vid_dir = os.path.join(VERSIONS_DIR, vid)
            os.makedirs(vid_dir, exist_ok=True)
            with open(os.path.join(vid_dir, vid + ".json"), "w", encoding="utf-8") as f:
                json.dump(profile, f, ensure_ascii=False, indent=2)
            set_progress(message=tr("下载 Fabric 依赖库…"))
            vjson = load_version_json(vid)
            files, natives_jars = collect_version_files(vjson, mirror)
            # 客户端 jar 已随原版安装，跳过
            files = [x for x in files if not x[1].endswith(vid + ".jar")]
            log(f"{tr('Fabric 依赖库 ')}{len(files)}{tr(' 个')}")
            download_all(files, mirror)
            extract_natives(natives_jars, os.path.join(vid_dir, NATIVES_DIRNAME))
            name = vid
        else:
            if loader == "forge":
                jar = os.path.join(tmp, f"forge-{ver}-installer.jar")
                _download_first([u.format(v=ver) for u in FORGE_MAVEN_MIRRORS], jar, log)
            elif loader == "neoforge":
                jar = os.path.join(tmp, f"neoforge-{ver}-installer.jar")
                _download_first([NEOFORGE_INSTALLER.format(v=ver)], jar, log)
            elif loader == "optifine":
                t, p = ver.rsplit("_", 1)
                jar = os.path.join(tmp, f"OptiFine_{mc}_{ver}.jar")
                _download_first([OPTIFINE_DL.format(mc=mc, t=t, p=p)], jar, log)
            else:
                raise RuntimeError(f"{tr('未知加载器 ')}{loader}")
            _need = required_java_major(load_version_json(mc))
            _run_installer(jar, log, mode=("optifine" if loader == "optifine" else "forge"),
                           java_need=_need)
            name = None
            # 找出安装器新建的版本目录（取最近修改的 json）
            best = 0
            for d in os.listdir(VERSIONS_DIR):
                jp = os.path.join(VERSIONS_DIR, d, d + ".json")
                if os.path.isfile(jp) and os.path.getmtime(jp) > best:
                    best = os.path.getmtime(jp)
                    name = d
            if not name or (loader == "optifine" and "optifine" not in name.lower()):
                raise RuntimeError(tr("安装器运行完成但未生成版本，请查看日志"))

        # PCL2 的做法：装完把薄壳展开成自包含版本（自己带 jar、自己一个目录名）
        try:
            name = realize_version(name, loader_dir_name(mc, loader, ver))
        except Exception as e:
            log(f"{tr('整理版本目录失败，保留原始版本: ')}{e}")
        # 不手工传加载器名/版本，交给 write_launcher_meta 从 json 里认
        # （Forge 安装器生成的版本号和 ver 不一定一致）
        write_launcher_meta(name, mc=mc)
        _msg = f"{name or loader}{tr(' 安装完成！')}"
        if own:
            end_task("done", _msg)
        else:
            set_progress(message=_msg)   # 子流程：只刷进度，不动整合包任务的状态
        log(f"{loader} {ver}{tr(' 安装完成 -> ')}{name}")
        refresh_installed()
    except Exception as e:
        report_error(f"{loader}{tr(' 安装出错')}", e)


# ---------- 账户 / 第三方登录（authlib-injector 外置登录） ----------
AUTHLIB_LATEST = [
    "https://bmclapi2.bangbang93.com/mirrors/authlib-injector/artifact/latest.json",
    "https://authlib-injector.yushi.moe/artifact/latest.json",
]


def load_accounts():
    os.makedirs(LAUNCHER_DIR, exist_ok=True)
    if os.path.isfile(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            d.setdefault("accounts", [])
            d.setdefault("selected", None)
            return d
        except Exception:
            pass
    return {"accounts": [], "selected": None}


def save_accounts(d):
    os.makedirs(LAUNCHER_DIR, exist_ok=True)
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def norm_yggdrasil(url):
    url = (url or "").strip().rstrip("/")
    if not url:
        raise RuntimeError(tr("请填写认证服务器地址"))
    if not url.startswith("http"):
        url = "https://" + url
    if "/api/yggdrasil" not in url:
        url += "/api/yggdrasil"
    return url


def ygg_server_name(server):
    r = requests.get(server, timeout=15, headers={"Accept": "application/json"})
    r.raise_for_status()
    return (r.json().get("meta") or {}).get("serverName") or server


def ygg_authenticate(server, username, password):
    payload = {"agent": {"name": "Minecraft", "version": 1},
               "username": username, "password": password, "requestUser": False}
    r = requests.post(server.rstrip("/") + "/authserver/authenticate", json=payload, timeout=30)
    if r.status_code != 200:
        try:
            j = r.json()
            msg = j.get("errorMessage") or j.get("error") or r.text[:120]
        except Exception:
            msg = r.text[:120]
        raise RuntimeError(f"{tr('登录失败: ')}{msg}")
    d = r.json()
    prof = d.get("selectedProfile") or {}
    if not prof.get("id"):
        raise RuntimeError(tr("登录失败: 该账户没有绑定角色，请先在认证服务器网站上创建角色"))
    return {
        "id": uuid.uuid4().hex[:12],
        "type": "authlib",
        "name": prof.get("name") or username,
        "server": server,
        "uuid": prof.get("id"),
        "accessToken": d.get("accessToken"),
        "clientToken": d.get("clientToken"),
    }


def ygg_refresh_token(acc):
    """校验 token，失效则刷新（返回 True 表示可用）"""
    server, tok = acc.get("server"), acc.get("accessToken")
    if not (server and tok):
        return False
    try:
        r = requests.post(server.rstrip("/") + "/authserver/validate",
                          json={"accessToken": tok,
                                "clientToken": acc.get("clientToken") or ""}, timeout=15)
        if r.status_code == 204:
            return True
        r = requests.post(server.rstrip("/") + "/authserver/refresh",
                          json={"accessToken": tok,
                                "clientToken": acc.get("clientToken") or ""}, timeout=15)
        if r.status_code == 200:
            d = r.json()
            acc["accessToken"] = d.get("accessToken", tok)
            acc["clientToken"] = d.get("clientToken", acc.get("clientToken"))
            return True
    except Exception:
        pass
    return False


def ensure_authlib_injector():
    if os.path.isfile(AUTHLIB_JAR) and os.path.getsize(AUTHLIB_JAR) > 50000:
        return AUTHLIB_JAR
    last = None
    for meta in AUTHLIB_LATEST:
        try:
            d = requests.get(meta, timeout=20).json()
            log(f"{tr('下载 authlib-injector ')}{d.get('version', '')}…")
            download_file(d["download_url"], AUTHLIB_JAR, "official")
            if os.path.getsize(AUTHLIB_JAR) > 50000:
                log(tr("authlib-injector 就绪"))
                return AUTHLIB_JAR
        except Exception as e:
            last = e
    raise RuntimeError(f"{tr('authlib-injector 下载失败: ')}{last}")


def current_account(account_id=None):
    d = load_accounts()
    accs = d.get("accounts") or []
    if account_id:
        for a in accs:
            if a.get("id") == account_id:
                return a
    for a in accs:
        if a.get("id") == d.get("selected"):
            return a
    return accs[0] if accs else None


# ---------- 微软正版登录（设备码流程） ----------
# 使用开源启动器社区（PlaceholderMC/PolyMC）提供的公共 Azure client id
MSA_CLIENT_ID = "c36a9fb6-4f2a-41ff-90bd-ae7cc92031eb"
MSA_SCOPE = "XboxLive.signin offline_access"
MSA_DEVICE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
MSA_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"

_msa = {"phase": "idle"}  # idle / starting / waiting / done / error


def msa_xbox_chain(live_token):
    """微软 access_token → XBL → XSTS → Minecraft access_token"""
    r = requests.post(XBL_AUTH_URL, json={
        "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                       "RpsTicket": "d=" + live_token},
        "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"}, timeout=30)
    r.raise_for_status()
    xbl_token = r.json().get("Token")

    r = requests.post(XSTS_AUTH_URL, json={
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
        "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"}, timeout=30)
    if r.status_code == 401:
        raise RuntimeError(tr("该微软账户没有 Minecraft：Java 版购买记录，无法正版登录"))
    r.raise_for_status()
    xsts = r.json()
    uhs = xsts["DisplayClaims"]["xui"][0]["uhs"]

    r = requests.post(MC_LOGIN_URL,
                      json={"identityToken": f"XBL3.0 x={uhs};{xsts['Token']}"}, timeout=30)
    r.raise_for_status()
    mc = r.json()
    return mc["access_token"], int(mc.get("expires_in", 86400))


def msa_profile(mc_token):
    r = requests.get(MC_PROFILE_URL, headers={"Authorization": "Bearer " + mc_token}, timeout=30)
    if r.status_code == 404:
        raise RuntimeError(tr("该账户没有 Minecraft Java 版档案（可能是纯 Bedrock 账户）"))
    r.raise_for_status()
    p = r.json()
    return p["id"], p["name"]


def msa_save_account(mc_token, expires_in, refresh_token):
    mc_uuid, mc_name = msa_profile(mc_token)
    acc = {"id": uuid.uuid4().hex[:12], "type": "msa", "name": mc_name,
           "uuid": mc_uuid, "accessToken": mc_token, "refreshToken": refresh_token,
           "expiresAt": time.time() + max(60, expires_in - 300)}
    store = load_accounts()
    for i, a in enumerate(store["accounts"]):
        if a.get("type") == "msa" and a.get("uuid") == mc_uuid:
            acc["id"] = a["id"]  # 同一正版账户重复登录则原地更新
            store["accounts"][i] = acc
            break
    else:
        store["accounts"].append(acc)
    store["selected"] = acc["id"]
    save_accounts(store)
    return acc


def msa_login_thread():
    try:
        r = requests.post(MSA_DEVICE_URL,
                          data={"client_id": MSA_CLIENT_ID, "scope": MSA_SCOPE}, timeout=20)
        r.raise_for_status()
        d = r.json()
        _msa.update(phase="waiting", user_code=d.get("user_code", ""),
                    verification_uri=d.get("verification_uri",
                                           "https://www.microsoft.com/link"))
        device_code = d["device_code"]
        interval = max(2, int(d.get("interval", 5)))
        deadline = time.time() + min(int(d.get("expires_in", 900)), 900)
        while time.time() < deadline:
            time.sleep(interval)
            r = requests.post(MSA_TOKEN_URL, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code, "client_id": MSA_CLIENT_ID}, timeout=20)
            resp = r.json()
            if resp.get("error") == "authorization_pending":
                continue
            if resp.get("error") == "slow_down":
                interval += 5
                continue
            if resp.get("error"):
                raise RuntimeError(resp.get("error_description") or resp["error"])
            mc_token, expires_in = msa_xbox_chain(resp["access_token"])
            acc = msa_save_account(mc_token, expires_in, resp.get("refresh_token", ""))
            log(f"{tr('正版登录成功: ')}{acc['name']}")
            _msa.update(phase="done", name=acc["name"], user_code="")
            return
        raise RuntimeError(tr("登录超时：请在 15 分钟内完成浏览器验证"))
    except Exception as e:
        log(f"{tr('正版登录失败: ')}{e}")
        _msa.update(phase="error", error=str(e), user_code="")


def msa_refresh_account(acc):
    """启动前检查/刷新正版凭证（原地更新 acc 并写回账户文件，返回是否可用）"""
    if acc.get("accessToken") and acc.get("expiresAt", 0) > time.time() + 60:
        return True
    rt = acc.get("refreshToken")
    if not rt:
        return False
    try:
        r = requests.post(MSA_TOKEN_URL, data={
            "client_id": MSA_CLIENT_ID, "grant_type": "refresh_token",
            "refresh_token": rt}, timeout=30)
        resp = r.json()
        if resp.get("error"):
            log(f"{tr('正版凭证刷新失败: ')}{resp.get('error_description') or resp['error']}")
            return False
        mc_token, expires_in = msa_xbox_chain(resp["access_token"])
        acc["accessToken"] = mc_token
        acc["refreshToken"] = resp.get("refresh_token", rt)
        acc["expiresAt"] = time.time() + max(60, expires_in - 300)
        try:
            store = load_accounts()
            for i, a in enumerate(store["accounts"]):
                if a.get("id") == acc.get("id"):
                    store["accounts"][i] = dict(a, **acc)
            save_accounts(store)
        except Exception:
            pass
        log(f"{tr('已刷新正版凭证: ')}{acc.get('name')}")
        return True
    except Exception as e:
        log(f"{tr('正版凭证刷新异常: ')}{e}")
        return False


# ---------- API ----------
@app.route("/")
def index():
    return send_file(os.path.join(RES_DIR, "static", "index.html"))


@app.route("/api/quit", methods=["POST"])
def api_quit():
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return jsonify({"ok": True})


@app.route("/api/versions")
def api_versions():
    mirror = request.args.get("mirror", "official")
    try:
        return jsonify({"ok": True, "versions": get_version_list(mirror)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/news")
def api_news():
    """资讯页数据：最新正式版/快照、最近版本、官方链接（网络挂了就回落到上次落盘的缓存）"""
    try:
        return jsonify(build_news(force=request.args.get("force") == "1"))
    except Exception as e:
        try:
            with open(_NEWS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            d["stale"] = True
            d["error"] = str(e)
            return jsonify(d)
        except Exception:
            return jsonify({"ok": False, "error": str(e)})


@app.route("/api/state")
def api_state():
    with _state_lock:
        s = {k: (list(v) if k == "log" else v) for k, v in _state.items()}
        # 前端左侧栏的下载胶囊读这个：按开始时间排序的任务快照
        s["tasks"] = [dict(t) for t in sorted(_TASKS.values(),
                                              key=lambda x: x.get("started", 0))]
    # 开服时给胶囊一行服务器信息：内存占用 = java 进程工作集 / 分配内存
    running = _srv_running()
    if running:
        mem = _srv_mem_mb()
        ram = _srv_cfg()["ram"]
        s["server"] = {"running": True, "mem_mb": mem, "ram": ram,
                       "mem_pct": min(999, int(round(mem * 100.0 / ram))) if ram else 0}
    else:
        s["server"] = {"running": False, "mem_mb": 0, "ram": 0, "mem_pct": 0}
    return jsonify(s)


@app.route("/api/install", methods=["POST"])
def api_install():
    d = request.get_json(force=True)
    vid, url, mirror = d.get("version"), d.get("url"), d.get("mirror", "official")
    if not vid or not url:
        return jsonify({"ok": False, "error": tr("参数缺失")})
    if task_busy(f"ver:{vid}"):
        return jsonify({"ok": False, "error": tr("这个版本正在下载，不用重复点")})
    _full = _too_many()
    if _full:
        return _full
    threading.Thread(target=install_thread, args=(vid, url, mirror), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """ESC / 取消全部：把所有进行中的任务标记取消"""
    d = request.get_json(silent=True) or {}
    tid = d.get("id")
    with _state_lock:
        if tid:
            t = _TASKS.get(tid)
            if t:
                t["cancel"] = True
        else:
            _state["cancel"] = True
            for t in _TASKS.values():
                if t.get("installing"):
                    t["cancel"] = True
    log(tr("用户请求取消下载"))
    return jsonify({"ok": True})


@app.route("/api/launch", methods=["POST"])
def api_launch():
    d = request.get_json(force=True)
    vid = (d.get("version") or load_config().get("version") or "").strip()
    username = (d.get("username") or "Steve").strip() or "Steve"
    ram_mb = int(d.get("ram", 2048))
    java_override = (d.get("java") or "").strip()
    acc = current_account(d.get("account_id"))
    if not vid:
        return jsonify({"ok": False, "error": tr("请选择版本")})
    if active_tasks():
        return jsonify({"ok": False, "error": tr("下载进行中，请稍后再启动")})
    threading.Thread(target=launch_thread,
                     args=(vid, username, ram_mb, java_override, acc), daemon=True).start()
    # 记住这次启动，游戏版本页据此显示"上次启动"
    try:
        cfg = load_config()
        lp = cfg.get("last_played") or {}
        lp[vid] = int(time.time())
        cfg["last_played"] = lp
        if not cfg.get("version"):
            cfg["version"] = vid
        save_config(cfg)
    except Exception:
        pass
    return jsonify({"ok": True})


# ---------- 账户 API ----------
@app.route("/api/accounts", methods=["GET"])
def api_accounts():
    d = load_accounts()
    safe = []
    for a in d["accounts"]:
        safe.append({k: v for k, v in a.items()
                     if k not in ("accessToken", "clientToken", "refreshToken")})
    return jsonify({"ok": True, "accounts": safe, "selected": d.get("selected")})


@app.route("/api/accounts/offline", methods=["POST"])
def api_account_offline():
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": tr("请输入用户名")})
    if len(name) > 16:
        return jsonify({"ok": False, "error": tr("用户名最长 16 个字符")})
    store = load_accounts()
    acc = {"id": uuid.uuid4().hex[:12], "type": "offline", "name": name}
    store["accounts"].append(acc)
    store["selected"] = acc["id"]
    save_accounts(store)
    log(f"{tr('已添加离线账户 ')}{name}")
    return jsonify({"ok": True, "account": acc})


@app.route("/api/accounts/third", methods=["POST"])
def api_account_third():
    d = request.get_json(force=True)
    server_raw, user, pwd = d.get("server"), d.get("username"), d.get("password")
    if not (server_raw and user and pwd):
        return jsonify({"ok": False, "error": tr("请填写服务器地址、邮箱/用户名和密码")})
    try:
        server = norm_yggdrasil(server_raw)
        log(f"{tr('正在登录第三方账户（')}{server}）…")
        acc = ygg_authenticate(server, user.strip(), pwd)
        store = load_accounts()
        # 同一服务器同名账户则更新而非重复添加
        for i, a in enumerate(store["accounts"]):
            if a.get("type") == "authlib" and a.get("server") == server \
                    and a.get("name") == acc["name"]:
                acc["id"] = a["id"]
                store["accounts"][i] = acc
                break
        else:
            store["accounts"].append(acc)
        store["selected"] = acc["id"]
        save_accounts(store)
        log(f"{tr('第三方登录成功: ')}{acc['name']}（{server}）")
        return jsonify({"ok": True, "account": {k: v for k, v in acc.items()
                                                if k not in ("accessToken", "clientToken")}})
    except Exception as e:
        log(f"{tr('第三方登录失败: ')}{e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/accounts/msa_start", methods=["POST"])
def api_msa_start():
    """开始微软正版登录（设备码流程），通过 /api/accounts/msa_poll 轮询状态"""
    if _msa.get("phase") in ("starting", "waiting"):
        return jsonify({"ok": False, "error": tr("已有正版登录正在进行，请先完成或等待超时")})
    _msa.update(phase="starting", error="", user_code="")
    threading.Thread(target=msa_login_thread, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/accounts/msa_poll")
def api_msa_poll():
    safe = {k: v for k, v in _msa.items() if k != "device_code"}
    return jsonify({"ok": True, **safe})


@app.route("/api/delete_version", methods=["POST"])
def api_delete_version():
    """删除已安装版本（versions/<vid> 整个目录，含该版本的存档/模组等）"""
    d = request.get_json(force=True)
    vid = (d.get("version") or "").strip()
    if not vid or vid != os.path.basename(vid):
        return jsonify({"ok": False, "error": tr("版本名不合法")})
    vdir = os.path.join(VERSIONS_DIR, vid)
    if not os.path.isdir(vdir):
        return jsonify({"ok": False, "error": tr("版本不存在")})
    if _state.get("installing"):
        return jsonify({"ok": False, "error": tr("下载进行中，请完成或取消后再删除")})
    # 检查其他实例是否沿 inheritsFrom 依赖该版本
    if not d.get("force"):
        deps = []
        for name in os.listdir(VERSIONS_DIR):
            if name == vid or not os.path.isdir(os.path.join(VERSIONS_DIR, name)):
                continue
            jf = os.path.join(VERSIONS_DIR, name, name + ".json")
            if not os.path.isfile(jf):
                continue
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    j = json.load(f)
                cur, seen = name, set()
                while j.get("inheritsFrom") and cur not in seen:
                    seen.add(cur)
                    if j["inheritsFrom"] == vid:
                        deps.append(name)
                        break
                    cur = j["inheritsFrom"]
                    pf = os.path.join(VERSIONS_DIR, cur, cur + ".json")
                    if not os.path.isfile(pf):
                        break
                    with open(pf, "r", encoding="utf-8") as f:
                        j = json.load(f)
            except Exception:
                pass
        if deps:
            return jsonify({"ok": False, "dependents": deps,
                            "error": f"{tr('版本 ')}{vid}{tr(' 被以下实例依赖：')}{', '.join(deps)}{tr('，请先删除它们')}"})
    shutil.rmtree(vdir, ignore_errors=True)
    refresh_installed()
    log(f"{tr('已删除版本 ')}{vid}")
    return jsonify({"ok": True})


@app.route("/api/accounts/select", methods=["POST"])
def api_account_select():
    d = request.get_json(force=True)
    store = load_accounts()
    if not any(a.get("id") == d.get("id") for a in store["accounts"]):
        return jsonify({"ok": False, "error": tr("账户不存在")})
    store["selected"] = d["id"]
    save_accounts(store)
    return jsonify({"ok": True})


@app.route("/api/accounts/delete", methods=["POST"])
def api_account_delete():
    d = request.get_json(force=True)
    store = load_accounts()
    store["accounts"] = [a for a in store["accounts"] if a.get("id") != d.get("id")]
    if store.get("selected") == d.get("id"):
        store["selected"] = store["accounts"][0]["id"] if store["accounts"] else None
    save_accounts(store)
    return jsonify({"ok": True})


@app.route("/api/accounts/server_info")
def api_account_server_info():
    try:
        server = norm_yggdrasil(request.args.get("server", ""))
        return jsonify({"ok": True, "server": server, "name": ygg_server_name(server)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


def _java_payload(force=False, vid=""):
    """设置页 / 版本页共用的 Java 清单：版本、厂商、位数、是否适配这个游戏版本"""
    need = 0
    if vid:
        try:
            need = required_java_major(load_version_json(vid))
        except Exception:
            need = 0
    items = []
    for exe in all_javas(force=force):
        info = java_info(exe, force=force)
        items.append({
            "path": info.get("path") or exe,
            "major": info.get("major") or 0,
            "full": info.get("full") or "",
            "vendor": info.get("vendor") or "",
            "bits": info.get("bits") or 0,
            "runtime": bool(info.get("runtime")),
            "match": bool(need and _java_matches(info.get("major") or 0, need)),
        })
    items.sort(key=lambda x: (-(x["major"] or 0), x["path"].lower()))
    picked = ""
    if need:
        try:
            picked = find_suitable_java(need) or ""
        except Exception:
            picked = ""
    return {"ok": True, "need": need, "javas": items, "picked": picked,
            "count": len(items)}


@app.route("/api/java", methods=["GET"])
def api_java():
    d = _java_payload(vid=request.args.get("vid", ""))
    if d["javas"]:
        first = d["javas"][0]
        return jsonify({"found": True, "path": first["path"],
                        "version": first["major"], **d})
    return jsonify({"found": False, **d})


@app.route("/api/java/list")
def api_java_list():
    """force=1 走重新扫描（设置页那个「重新扫描」按钮）"""
    force = request.args.get("force") in ("1", "true", "yes")
    return jsonify(_java_payload(force=force, vid=request.args.get("vid", "")))


def _java_prepare_thread(major):
    """后台下载指定大版本的 Java（Mojang 运行时 → BMCLAPI → Adoptium 兜底）"""
    if new_task(f"{tr('下载 Java ')}{major}", key=f"java:{major}") is None:
        return
    try:
        set_progress(message=f"{tr('准备 Java ')}{major}…", percent=5)
        exe = ensure_java(major)
        if not exe:
            raise RuntimeError(tr("下载失败，检查网络后重试（国内网络可在设置里切到 BMCLAPI 镜像）"))
        info = java_info(exe, force=True)
        end_task("done", f"{tr('Java ')}{info.get('major') or major}{tr(' 已就绪，启动时会自动使用')}")
    except Exception as e:
        report_error(tr("Java 下载出错"), e)


@app.route("/api/java/download", methods=["POST"])
def api_java_download():
    d = request.get_json(force=True, silent=True) or {}
    try:
        major = int(d.get("major") or 0)
    except Exception:
        major = 0
    if major not in (8, 17, 21):
        return jsonify({"ok": False, "error": tr("只支持下载 Java 8 / 17 / 21")})
    threading.Thread(target=_java_prepare_thread, args=(major,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/installed", methods=["GET"])
def api_installed():
    ids = refresh_installed()
    return jsonify({"versions": ids, "selected": selected_version(ids)})


@app.route("/api/crash_report", methods=["GET"])
def api_crash_report():
    """最近半小时内有没有新的崩溃报告（客户端 / 服务端都找），给界面做报错分析"""
    best = None
    for base in (MC_DIR, SERVER_DIR):
        d = os.path.join(base, "crash-reports")
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.lower().endswith(".txt"):
                continue
            p = os.path.join(d, name)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if mt < time.time() - 1800:
                continue
            if best is None or mt > best[0]:
                best = (mt, p)
    if not best:
        return jsonify({"ok": True, "found": False})
    mt, p = best
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True, "found": True, "name": os.path.basename(p),
                    "mtime": mt, "size": os.path.getsize(p), "tail": txt[-6000:]})


@app.route("/api/launcher_meta/sync", methods=["POST"])
def api_launcher_meta_sync():
    """把已装版本补齐成 PCL2 / HMCL 也能直接用的样子（已存在的配置不动）"""
    ids = refresh_installed()
    written = 0
    for vid in ids:
        try:
            if write_launcher_meta(vid):
                written += 1
        except Exception as e:
            log(f"{tr('同步失败: ')}{vid} {e}")
    log(f"{tr('已向 PCL2 / HMCL 同步 ')}{written}/{len(ids)}{tr(' 个版本')}")
    return jsonify({"ok": True, "total": len(ids), "written": written})


@app.route("/api/versions_detail", methods=["GET"])
def api_versions_detail():
    """游戏版本页：每个版本的加载器 / Java / 占用 / 上次启动"""
    ids = refresh_installed()
    return jsonify({"ok": True, "selected": selected_version(ids),
                    "versions": [version_meta(v) for v in ids]})


@app.route("/api/version/select", methods=["POST"])
def api_version_select():
    vid = (request.get_json(force=True).get("version") or "").strip()
    if vid not in refresh_installed():
        return jsonify({"ok": False, "error": tr("版本不存在，请刷新后重试")})
    cfg = load_config()
    cfg["version"] = vid
    save_config(cfg)
    log(f"{tr('已选择版本 ')}{vid}")
    return jsonify({"ok": True, "selected": vid})


@app.route("/api/version/rename", methods=["POST"])
def api_version_rename():
    """重命名版本：目录 + 内部 json + jar 一起改名，游戏目录（隔离）保持原样文件名"""
    d = request.get_json(force=True)
    old = (d.get("old") or "").strip()
    new = (d.get("new") or "").strip()
    if not old or not new:
        return jsonify({"ok": False, "error": tr("版本名不能为空")})
    if any(c in new for c in '/\\:*?"<>|') or new in (".", ".."):
        return jsonify({"ok": False, "error": tr("版本名里有不允许的字符")})
    odir = os.path.join(VERSIONS_DIR, old)
    ndir = os.path.join(VERSIONS_DIR, new)
    if not os.path.isdir(odir):
        return jsonify({"ok": False, "error": tr("版本不存在")})
    if os.path.exists(ndir):
        return jsonify({"ok": False, "error": f"{tr('已经有叫「')}{new}{tr('」的版本了')}"})
    if _state.get("installing"):
        return jsonify({"ok": False, "error": tr("下载进行中，请完成或取消后再改名")})
    deps = _dependents(old)
    if deps:
        return jsonify({"ok": False,
                        "error": f"「{old}{tr('」被 ')}{', '.join(deps)}{tr(' 依赖，改名会让它们无法启动')}"})
    try:
        os.rename(odir, ndir)
        old_json = os.path.join(ndir, old + ".json")
        if os.path.isfile(old_json):
            with open(old_json, "r", encoding="utf-8") as f:
                j = json.load(f)
            j["id"] = new
            with open(os.path.join(ndir, new + ".json"), "w", encoding="utf-8") as f:
                json.dump(j, f, ensure_ascii=False, indent=2)
            os.remove(old_json)
        old_jar = os.path.join(ndir, old + ".jar")
        if os.path.isfile(old_jar):
            os.replace(old_jar, os.path.join(ndir, new + ".jar"))
    except Exception as e:
        return jsonify({"ok": False, "error": f"{tr('改名失败：')}{e}"})
    cfg = load_config()
    if cfg.get("version") == old:
        cfg["version"] = new
    lp = cfg.get("last_played") or {}
    if old in lp:
        lp[new] = lp.pop(old)
        cfg["last_played"] = lp
    save_config(cfg)
    refresh_installed()
    log(f"{tr('版本已改名: ')}{old} → {new}")
    return jsonify({"ok": True, "version": new})


@app.route("/api/version/open", methods=["POST"])
def api_version_open():
    """打开某个版本的游戏目录（隔离开启时是 versions/<版本>/）"""
    vid = (request.get_json(force=True).get("version") or "").strip()
    if not vid or vid != os.path.basename(vid):
        return jsonify({"ok": False, "error": tr("版本名不合法")})
    vdir = os.path.join(VERSIONS_DIR, vid)
    if not os.path.isdir(vdir):
        return jsonify({"ok": False, "error": tr("版本不存在")})
    target = vdir if load_config().get("isolate", True) else MC_DIR
    try:
        if SYSTEM_NAME == "windows":
            os.startfile(target)
        elif SYSTEM_NAME == "osx":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except Exception as e:
        return jsonify({"ok": False, "error": f"{tr('打开目录失败：')}{e}"})
    return jsonify({"ok": True, "path": target})


@app.route("/api/loaders")
def api_loaders():
    mc = request.args.get("mc", "")
    if not mc:
        return jsonify({"ok": False, "error": tr("缺少 mc 参数")})
    try:
        return jsonify({"ok": True, **get_loader_options(mc)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/install_loader", methods=["POST"])
def api_install_loader():
    d = request.get_json(force=True)
    loader, mc, ver = d.get("type"), d.get("mc"), d.get("ver")
    mirror = d.get("mirror", "bmclapi")
    if not (loader and mc and ver):
        return jsonify({"ok": False, "error": tr("参数缺失")})
    if task_busy(f"loader:{loader}:{mc}:{ver}"):
        return jsonify({"ok": False, "error": tr("这个加载器正在安装，不用重复点")})
    _full = _too_many()
    if _full:
        return _full
    threading.Thread(target=install_loader_thread,
                     args=(loader, mc, ver, mirror), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/update_check", methods=["GET"])
def api_update_check():
    """网页端"关于"卡用：返回线上最新桌面/安卓版本号"""
    try:
        r = requests.get("https://mclauncher-update.pages.dev/update.json", timeout=15)
        d = r.json()
        apk_ver = ""
        m = re.search(r"android-([\d.]+)\.apk", d.get("apk_url") or "")
        if m:
            apk_ver = m.group(1)
        return jsonify({"ok": True, "version": d.get("version", ""),
                        "apk_version": apk_ver})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        d = request.get_json(force=True)
        cfg = load_config()
        if "isolate" in d:
            cfg["isolate"] = bool(d["isolate"])
        if "skip_onboarding" in d:
            cfg["skip_onboarding"] = bool(d["skip_onboarding"])
        if "cf_api_key" in d:
            cfg["cf_api_key"] = str(d["cf_api_key"] or "").strip()
        save_config(cfg)
        log(f"{tr('设置已更新: 版本隔离=')}{cfg['isolate']}")
    return jsonify({"ok": True, **load_config()})


@app.route("/api/open_folder", methods=["POST"])
def api_open_folder():
    os.makedirs(MC_DIR, exist_ok=True)
    try:
        if SYSTEM_NAME == "windows":
            os.startfile(MC_DIR)
        elif SYSTEM_NAME == "osx":
            subprocess.Popen(["open", MC_DIR])
        else:
            subprocess.Popen(["xdg-open", MC_DIR])
    except Exception as e:
        log(f"{tr('打开目录失败: ')}{e}")
    return jsonify({"ok": True})


# ---------- 资源中心（Modrinth：模组 / 整合包 / 数据包 / 资源包 / 光影包） ----------
MODRINTH_API = "https://api.modrinth.com/v2"
RES_SUBDIR = {"mod": "mods", "shader": "shaderpacks", "resourcepack": "resourcepacks",
              "datapack": "datapacks"}
RES_LOADERS = {"fabric", "forge", "neoforge", "quilt", "liteloader", "rift"}
# 平台名不是给玩家看的分类（数据包常见的 bukkit/paper 等），展示时丢掉
RES_PLATFORMS = {"bukkit", "folia", "paper", "purpur", "spigot", "datapack",
                 "mod", "modpack", "resourcepack", "shader"}


# ---------- CurseForge（官方 API，Key 在设置里填，developer.curseforge.com 免费申请） ----------
CF_API = "https://api.curseforge.com/v1"
CF_CLASS = {"mod": 6, "modpack": 4471, "resourcepack": 12,
            "shader": 6552, "datapack": 6945}
CF_LOADER_TYPE = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
# 项目类别 -> 文件放哪个子目录（整合包安装时按项目 classId 归位）
CF_FILE_SUB = {6: "mods", 12: "resourcepacks", 6552: "shaderpacks", 6945: "datapacks"}


class CFNoKey(RuntimeError):
    """没填 CurseForge Key —— 单独一个类型，前端据此弹引导卡而不是报错"""


def _cf_key():
    return (load_config().get("cf_api_key") or "").strip()


def _cf_headers():
    key = _cf_key()
    if not key:
        raise CFNoKey(tr("没有配置 CurseForge API Key，去「设置」里填一个"
                         "（developer.curseforge.com 免费申请）"))
    return {"x-api-key": key, "Accept": "application/json", "User-Agent": "AeroLauncher"}


def cf_check_key(key):
    """拿这把 Key 打一次最小的搜索，返回 (是否有效, 失败原因)。
    401/403 = Key 错或还在审核；其余状态码原样报出来。"""
    key = (key or "").strip()
    if not key:
        return False, tr("Key 是空的")
    try:
        r = requests.get(f"{CF_API}/mods/search",
                         params={"gameId": 432, "classId": 6, "pageSize": 1},
                         headers={"x-api-key": key, "Accept": "application/json",
                                  "User-Agent": "AeroLauncher"}, timeout=25)
    except Exception as e:
        return False, tr("连不上 CurseForge：") + str(e)
    if r.status_code in (401, 403):
        return False, tr("Key 无效，或者还在审核中（401 / 403）")
    if r.status_code == 429:
        return False, tr("请求太频繁被限流了（429），过一会儿再试")
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    return True, ""


@app.route("/api/cf/key", methods=["GET", "POST"])
def api_cf_key():
    """GET 查有没有配 Key；POST 校验后保存（校验不通过就不落盘，免得留个坏 Key）"""
    cfg = load_config()
    if request.method == "GET":
        return jsonify({"ok": True, "has_key": bool((cfg.get("cf_api_key") or "").strip())})

    key = str((request.get_json(force=True) or {}).get("key") or "").strip()
    if not key:
        cfg["cf_api_key"] = ""
        save_config(cfg)
        log(tr("已清空 CurseForge API Key"))
        return jsonify({"ok": True, "valid": False, "cleared": True})

    valid, why = cf_check_key(key)
    if not valid:
        return jsonify({"ok": False, "valid": False, "error": why})
    cfg["cf_api_key"] = key
    save_config(cfg)
    log(tr("CurseForge API Key 校验通过并已保存"))
    return jsonify({"ok": True, "valid": True})


def _cf_get(path, params=None, timeout=30):
    r = requests.get(CF_API + path, params=params, headers=_cf_headers(), timeout=timeout)
    if r.status_code in (401, 403):
        raise RuntimeError(tr("CurseForge Key 无效或未授权，去「设置」里检查一下"))
    r.raise_for_status()
    d = r.json()
    return d.get("data") if isinstance(d, dict) and "data" in d else d


def cf_search(rtype, q="", mc="", loader="", offset=0, limit=24):
    """CurseForge 搜索，标准化成和 Modrinth 一样的条目结构"""
    params = {
        "gameId": 432, "classId": CF_CLASS[rtype],
        "sortField": 2 if q else 7,        # 有关键词按热度，没有按总下载量
        "sortOrder": "desc", "pageSize": limit, "offset": offset,
    }
    if q:
        params["searchFilter"] = q
    if mc:
        params["gameVersion"] = mc
    if loader and loader in CF_LOADER_TYPE:
        params["modLoaderType"] = CF_LOADER_TYPE[loader]
    r = requests.get(f"{CF_API}/mods/search", params=params,
                     headers=_cf_headers(), timeout=30)
    if r.status_code in (401, 403):
        raise RuntimeError(tr("CurseForge Key 无效或未授权，去「设置」里检查一下"))
    r.raise_for_status()
    d = r.json().get("data", [])
    items = []
    for h in d:
        cats = [c.get("name", "") for c in (h.get("categories") or [])]
        loaders = [x.lower() for x in (h.get("supportedModLoaderTypes") or [])
                   if x.lower() in CF_LOADER_TYPE]
        items.append({
            "slug": str(h.get("id", "")),
            "title": h.get("name", ""),
            "desc": (h.get("summary") or "")[:160],
            "author": ", ".join(a.get("name", "") for a in (h.get("authors") or [])),
            "downloads": h.get("downloadCount", 0),
            "icon": (h.get("logo") or {}).get("thumbnailUrl") or "",
            "categories": [x for x in cats
                           if x.lower() not in CF_LOADER_TYPE
                           and x.lower() not in RES_PLATFORMS],
            "loaders": loaders,
            "versions": [],
            "updated": h.get("dateModified", ""),
            "color": 0,
            "source": "cf",
            "website": (h.get("links") or {}).get("websiteUrl") or "",
        })
    return {"total": len(items),
            "items": items}


def cf_versions(mod_id, mc="", loader=""):
    """CurseForge 文件列表，标准化成和 Modrinth version 一样的结构"""
    params = {"pageSize": 40, "sort": 1}   # 1 = 按上传时间倒序
    if mc:
        params["gameVersion"] = mc
    if loader and loader in CF_LOADER_TYPE:
        params["modLoaderType"] = CF_LOADER_TYPE[loader]
    files = _cf_get(f"/mods/{mod_id}/files", params) or []
    out = []
    for f in files[:40]:
        out.append({
            "id": str(f.get("id", "")),
            "name": f.get("displayName") or f.get("fileName", ""),
            "number": str(f.get("id", "")),
            "date": (f.get("fileDate") or "")[:10],
            "filename": f.get("fileName", ""),
            "url": f.get("downloadUrl") or
                   f"https://www.curseforge.com/api/v1/mods/{mod_id}/files/{f.get('id')}/download",
            "size": f.get("fileLength", 0),
            "loaders": [x.lower() for x in (f.get("loaders") or [])],
            "game_versions": f.get("gameVersions", []),
            "downloads": 0,
            "date_full": f.get("fileDate", ""),
            "type": {2: "release", 3: "alpha", 4: "beta"}.get(
                f.get("fileStatus"), "release"),
        })
    return out


def resource_search(rtype, q="", mc="", loader="", category="", offset=0, limit=24):
    facets = [[f"project_type:{rtype}"]]
    if mc:
        facets.append([f"versions:{mc}"])
    if loader:
        facets.append([f"categories:{loader}"])
    if category:
        facets.append([f"categories:{category}"])
    params = {"limit": limit, "offset": offset, "facets": json.dumps(facets),
              "index": "relevance" if q else "downloads"}
    if q:
        params["query"] = q
    r = requests.get(f"{MODRINTH_API}/search", params=params, timeout=30)
    r.raise_for_status()
    d = r.json()
    items = []
    for h in d.get("hits", []):
        cats = h.get("categories") or []
        items.append({
            "slug": h.get("slug", ""),
            "title": h.get("title", ""),
            "desc": (h.get("description") or "")[:160],
            "author": h.get("author", ""),
            "downloads": h.get("downloads", 0),
            "icon": h.get("icon_url", ""),
            "categories": [x for x in cats
                           if x not in RES_LOADERS and x not in RES_PLATFORMS],
            "loaders": [x for x in cats if x in RES_LOADERS],
            "versions": (h.get("versions") or [])[:40],
            "updated": h.get("date_modified", ""),
            "color": h.get("color") or 0,
            "source": "modrinth",
            "website": f"https://modrinth.com/{rtype}/{h.get('slug', '')}",
        })
    return {"total": d.get("total_hits", 0), "items": items}


def resource_versions(slug, mc="", loader=""):
    params = {}
    if mc:
        params["game_versions"] = json.dumps([mc])
    if loader:
        params["loaders"] = json.dumps([loader])
    r = requests.get(f"{MODRINTH_API}/project/{slug}/version", params=params, timeout=30)
    r.raise_for_status()
    out = []
    for v in r.json()[:40]:
        flist = v.get("files") or []
        f = next((x for x in flist if x.get("primary")), flist[0] if flist else {})
        out.append({
            "id": v.get("id", ""),
            "name": v.get("name") or v.get("version_number", ""),
            "number": v.get("version_number", ""),
            "date": (v.get("date_published") or "")[:10],
            "filename": f.get("filename", ""),
            "url": f.get("url", ""),
            "size": f.get("size", 0),
            "loaders": v.get("loaders", []),
            "game_versions": v.get("game_versions", []),
            "downloads": v.get("downloads", 0),
            "date_full": v.get("date_published", ""),
            "type": v.get("version_type", "release"),
        })
    return out


def _download_with_progress(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    r = http_get(url, "official", timeout=120)
    total = int(r.headers.get("Content-Length") or 0)
    tmp = dest + ".tmp"
    done = 0
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(256 * 1024):
            f.write(chunk)
            done += len(chunk)
            if total:
                set_progress(percent=int(done / total * 100),
                             message=f"{tr('正在下载 ')}{os.path.basename(dest)} "
                                     f"({done // 1024 // 1024}MB/{total // 1024 // 1024}MB)")
            if is_cancelled():
                raise Cancelled()
    os.replace(tmp, dest)


def _newest_installed_version(after_ts):
    best, name = 0, None
    if os.path.isdir(VERSIONS_DIR):
        for d in os.listdir(VERSIONS_DIR):
            jp = os.path.join(VERSIONS_DIR, d, d + ".json")
            if os.path.isfile(jp) and os.path.getmtime(jp) > best:
                best = os.path.getmtime(jp)
                name = d
    return name if name and best > after_ts else None


def _ensure_vanilla(mc):
    """确保原版 mc 已安装（未装则安装）"""
    if os.path.isfile(os.path.join(VERSIONS_DIR, mc, mc + ".jar")):
        return
    set_progress(message=f"{tr('先安装原版 ')}{mc}…")
    vl = get_version_list("bmclapi")
    vurl = next((v["url"] for v in vl if v["id"] == mc), None)
    if not vurl:
        raise RuntimeError(f"{tr('找不到原版 ')}{mc}")
    install_core(mc, vurl, "bmclapi")


def resource_install_thread(rtype, url, filename, vid):
    if new_task(f"{tr('下载 ')}{filename}", sub=tr("准备下载…"),
                key=f"res:{vid}:{filename}") is None:
        return
    try:
        sub = RES_SUBDIR[rtype]
        gdir = game_dir_for(vid)
        dest = os.path.join(gdir, sub, filename)
        log(f"{tr('下载 ')}{filename} -> {sub}/")
        _download_with_progress(url, dest)
        end_task("done", f"{filename}{tr(' 下载完成！已放入 ')}{vid}{tr(' 的 ')}{sub}{tr('/ 文件夹')}")
        log(f"{tr('已保存: ')}{dest}")
    except Exception as e:
        report_error(tr("资源下载出错"), e)


def modpack_install_thread(url, filename, name, local_path=None):
    """安装整合包：自动识别 Modrinth（modrinth.index.json）和 CurseForge（manifest.json）"""
    tmp_zip = os.path.join(LOADER_CACHE, filename)
    if new_task(f"{tr('安装整合包 ')}{name}", key=f"modpack:{name}") is None:
        return
    try:
        os.makedirs(LOADER_CACHE, exist_ok=True)
        if local_path:
            if os.path.abspath(local_path) != os.path.abspath(tmp_zip):
                shutil.copyfile(local_path, tmp_zip)   # 文件本来就在缓存目录里就不用拷
            set_progress(message=f"{tr('读取本地整合包 ')}{name}…", percent=30)
        else:
            set_progress(message=f"{tr('下载整合包 ')}{name}…", percent=0)
            _download_with_progress(url, tmp_zip)
        if is_cancelled():
            raise Cancelled()

        with zipfile.ZipFile(tmp_zip) as z:
            names = z.namelist()
        if "modrinth.index.json" in names:
            inst_id, mc, base_vid = _install_mrpack(tmp_zip, name)
        elif "manifest.json" in names:
            inst_id, mc, base_vid = _install_cf_modpack(tmp_zip, name)
        else:
            raise RuntimeError(tr("不支持的整合包格式"
                                  "（没找到 modrinth.index.json 或 manifest.json）"))

        end_task("done", f"{tr('整合包「')}{inst_id}{tr('」安装完成！去主页 ⌄ 选择它启动')}")
        log(f"{tr('整合包安装完成: ')}{inst_id}{tr(' (基于 ')}{base_vid})")
        refresh_installed()
    except Exception as e:
        report_error(tr("整合包安装出错"), e)


def _create_modpack_instance(name, mc, base_vid, tmp_zip, overrides_dir="overrides"):
    """建整合包实例（薄壳版本 + 版本隔离目录），解压 overrides，返回 (inst_id, 游戏目录)"""
    # 3) 建整合包实例版本（版本隔离目录）
    inst_id = re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "Modpack"
    n, cand = 2, inst_id
    while os.path.exists(os.path.join(VERSIONS_DIR, cand, cand + ".json")):
        cand = f"{inst_id} ({n})"
        n += 1
    inst_id = cand
    vid_dir = os.path.join(VERSIONS_DIR, inst_id)
    os.makedirs(vid_dir, exist_ok=True)
    inst_json = {"id": inst_id, "inheritsFrom": base_vid, "type": "release",
                 "releaseTime": time.strftime("%Y-%m-%dT%H:%M:%S+00:00")}
    with open(os.path.join(vid_dir, inst_id + ".json"), "w", encoding="utf-8") as f:
        json.dump(inst_json, f, ensure_ascii=False, indent=2)
    write_launcher_meta(inst_id, mc=mc)

    # 4) 解压 overrides 到实例目录（版本隔离）
    gdir = game_dir_for(inst_id)
    prefix = overrides_dir.rstrip("/") + "/"
    set_progress(message=tr("解压整合包配置文件…"))
    with zipfile.ZipFile(tmp_zip) as z:
        for n in z.namelist():
            if not n.startswith(prefix):
                continue
            rel = n[len(prefix):]
            if not rel:
                continue
            dest = os.path.join(gdir, rel)
            if n.endswith("/"):
                os.makedirs(dest, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(n) as fsrc, open(dest, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
    return inst_id, gdir


def _install_mrpack(tmp_zip, name):
    """Modrinth 整合包（modrinth.index.json）"""
    with zipfile.ZipFile(tmp_zip) as z:
        idx = json.loads(z.read("modrinth.index.json").decode("utf-8"))
    deps = idx.get("dependencies", {})
    mc = deps.get("minecraft")
    if not mc:
        raise RuntimeError(tr("整合包缺少 minecraft 版本依赖"))
    log(f"{tr('整合包依赖: MC ')}{mc} · " +
        ", ".join(f"{k}={v}" for k, v in deps.items() if k != "minecraft"))

    # 1) 原版
    _ensure_vanilla(mc)

    # 2) 加载器（fabric/forge/neoforge/quilt→按 fabric 失败则报错）
    base_vid = mc
    if deps.get("fabric-loader") or deps.get("quilt-loader"):
        ver = deps.get("fabric-loader") or deps.get("quilt-loader")
        set_progress(message=f"{tr('安装 Fabric ')}{ver}…")
        install_loader_thread("fabric", mc, ver, "bmclapi")
        if cur_phase() == "error":
            raise RuntimeError(tr("Fabric 安装失败，请查看日志"))
        _want = loader_dir_name(mc, "fabric", ver)
        base_vid = _want if os.path.isdir(os.path.join(VERSIONS_DIR, _want)) \
            else f"fabric-loader-{ver}-{mc}"
    elif deps.get("forge"):
        ver = deps["forge"]
        fv = ver if "-" in ver else f"{mc}-{ver}"
        set_progress(message=f"{tr('安装 Forge ')}{fv}…")
        t0 = time.time()
        install_loader_thread("forge", mc, fv, "bmclapi")
        if cur_phase() == "error":
            raise RuntimeError(tr("Forge 安装失败，请查看日志"))
        base_vid = _newest_installed_version(t0) or fv
    elif deps.get("neoforge"):
        ver = deps["neoforge"]
        fv = ver if ver.startswith(mc) else f"{mc}-{ver}"
        set_progress(message=f"{tr('安装 NeoForge ')}{fv}…")
        t0 = time.time()
        install_loader_thread("neoforge", mc, fv, "bmclapi")
        if cur_phase() == "error":
            raise RuntimeError(tr("NeoForge 安装失败，请查看日志"))
        base_vid = _newest_installed_version(t0) or fv
    if is_cancelled():
        raise Cancelled()

    inst_id, gdir = _create_modpack_instance(name, mc, base_vid, tmp_zip)

    # 5) 下载整合包声明的文件（mods/config 等）
    files = []
    for f in idx.get("files", []):
        env = (f.get("env") or {}).get("client", "required")
        if env == "unsupported":
            continue
        dest = os.path.join(gdir, f["path"])
        if os.path.isfile(dest) and os.path.getsize(dest) == f.get("fileSize", -1):
            continue
        dl = f.get("downloads") or []
        if dl:
            files.append((dl[0], dest, f.get("fileSize", 0),
                          (f.get("hashes") or {}).get("sha1")))
    log(f"{tr('整合包依赖文件 ')}{len(files)}{tr(' 个')}")
    if files:
        if not download_all(files, "official"):
            raise RuntimeError(tr("部分依赖文件下载失败，请重试"))
    return inst_id, mc, base_vid


def _install_cf_modpack(tmp_zip, name):
    """CurseForge 整合包（manifest.json）"""
    with zipfile.ZipFile(tmp_zip) as z:
        mf = json.loads(z.read("manifest.json").decode("utf-8"))
    mcd = mf.get("minecraft") or {}
    mc = mcd.get("version")
    if not mc:
        raise RuntimeError(tr("整合包缺少 minecraft 版本依赖"))

    # 1) 原版
    _ensure_vanilla(mc)

    # 2) 加载器（modLoaders: id 形如 forge-41.0.0 / fabric-0.14.21）
    base_vid = mc
    for ml in mcd.get("modLoaders") or []:
        mlid = str(ml.get("id") or "")
        ltype, _, ver = mlid.partition("-")
        ltype = ltype.lower()
        if ltype not in CF_LOADER_TYPE or not ver:
            continue
        fv = ver
        if ltype == "forge":
            fv = ver if "-" in ver else f"{mc}-{ver}"
        elif ltype == "neoforge" and not ver.startswith(mc):
            fv = ver if "-" in ver else f"{mc}-{ver}"
        set_progress(message=f"{tr('安装 ')}{ltype.title()} {ver}…")
        t0 = time.time()
        install_loader_thread(ltype, mc, fv, "bmclapi")
        if cur_phase() == "error":
            raise RuntimeError(tr("加载器安装失败，请查看日志"))
        if ltype == "fabric":
            _want = loader_dir_name(mc, "fabric", ver)
            base_vid = _want if os.path.isdir(os.path.join(VERSIONS_DIR, _want)) \
                else f"fabric-loader-{ver}-{mc}"
        else:
            base_vid = _newest_installed_version(t0) or fv
        break
    if is_cancelled():
        raise Cancelled()

    inst_id, gdir = _create_modpack_instance(
        name, mc, base_vid, tmp_zip,
        (mf.get("overrides") or "overrides").rstrip("/") or "overrides")

    # 3) 按 manifest.files 逐个去 CurseForge 拿下载地址（按项目类别归位目录）
    files = []
    wanted = [f for f in (mf.get("files") or []) if f.get("required", True)
              and f.get("projectID") and f.get("fileID")]
    if wanted:
        set_progress(message=tr("查询 CurseForge 项目信息…"))
        try:
            r = requests.post(f"{CF_API}/mods", json=[{"modId": f["projectID"]}
                                                      for f in wanted],
                              headers=_cf_headers(), timeout=30)
            r.raise_for_status()
            p2class = {m["id"]: m.get("classId")
                       for m in r.json().get("data", [])}
        except Exception:
            p2class = {}
        for i, f in enumerate(wanted):
            if is_cancelled():
                raise Cancelled()
            p, fid = f["projectID"], f["fileID"]
            set_progress(percent=60 + int(i / max(len(wanted), 1) * 35),
                         message=f"{tr('解析整合包文件 ')}{i + 1}/{len(wanted)}…")
            try:
                fi = _cf_get(f"/mods/{p}/file/{fid}") or {}
            except Exception as e:
                log(f"{tr('跳过 ')}{p}/{fid}: {e}")
                continue
            fname = fi.get("fileName") or f"{p}-{fid}.jar"
            url = fi.get("downloadUrl") or \
                f"https://www.curseforge.com/api/v1/mods/{p}/files/{fid}/download"
            sub = CF_FILE_SUB.get(p2class.get(p, 6), "mods")
            dest = os.path.join(gdir, sub, fname)
            if os.path.isfile(dest) and os.path.getsize(dest) == fi.get("fileLength", -1):
                continue
            sha1 = next((h.get("value") for h in (fi.get("hashes") or [])
                         if h.get("algo") == 1), None)
            files.append((url, dest, fi.get("fileLength", 0), sha1))
    log(f"{tr('整合包依赖文件 ')}{len(files)}{tr(' 个')}")
    if files:
        if not download_all(files, "official"):
            raise RuntimeError(tr("部分依赖文件下载失败，请重试"))
    return inst_id, mc, base_vid


@app.route("/api/resource/search")
def api_resource_search():
    try:
        src = request.args.get("src", "modrinth")
        args = dict(type=request.args.get("type", "mod"),
                    q=request.args.get("q", ""),
                    mc=request.args.get("mc", ""),
                    loader=request.args.get("loader", ""),
                    offset=int(request.args.get("offset", 0)))
        if src == "cf":
            data = cf_search(args["type"], q=args["q"], mc=args["mc"],
                             loader=args["loader"], offset=args["offset"])
        else:
            data = resource_search(args["type"], q=args["q"], mc=args["mc"],
                                   loader=args["loader"],
                                   category=request.args.get("category", ""),
                                   offset=args["offset"])
        return jsonify({"ok": True, **data, "source": "cf" if src == "cf" else "modrinth"})
    except CFNoKey as e:
        return jsonify({"ok": False, "code": "cf_no_key", "error": str(e),
                        "source": "cf" if src == "cf" else "modrinth"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "source": "cf" if src == "cf" else "modrinth"})


@app.route("/api/resource/versions")
def api_resource_versions():
    try:
        if request.args.get("src") == "cf":
            vs = cf_versions(request.args.get("slug", ""),
                             mc=request.args.get("mc", ""),
                             loader=request.args.get("loader", ""))
        else:
            vs = resource_versions(request.args.get("slug", ""),
                                   mc=request.args.get("mc", ""),
                                   loader=request.args.get("loader", ""))
        return jsonify({"ok": True, "versions": vs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/resource/install", methods=["POST"])
def api_resource_install():
    d = request.get_json(force=True)
    rtype, url, filename = d.get("type"), d.get("url"), d.get("filename")
    vid = d.get("vid")
    if not (rtype in RES_SUBDIR and url and filename and vid):
        return jsonify({"ok": False, "error": tr("参数缺失（需先在主页选择一个已安装版本）")})
    if task_busy(f"res:{vid}:{filename}"):
        return jsonify({"ok": False, "error": tr("这个文件正在下载，不用重复点")})
    _full = _too_many()
    if _full:
        return _full
    threading.Thread(target=resource_install_thread,
                     args=(rtype, url, filename, vid), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/resource/install_modpack", methods=["POST"])
def api_resource_install_modpack():
    d = request.get_json(force=True)
    url, filename, name = d.get("url"), d.get("filename"), d.get("name")
    if not (url and filename and name):
        return jsonify({"ok": False, "error": tr("参数缺失")})
    if task_busy(f"modpack:{name}"):
        return jsonify({"ok": False, "error": tr("这个整合包正在安装，不用重复点")})
    _full = _too_many()
    if _full:
        return _full
    threading.Thread(target=modpack_install_thread,
                     args=(url, filename, name), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/resource/install_modpack_local", methods=["POST"])
def api_resource_install_modpack_local():
    d = request.get_json(force=True)
    path = d.get("path") or ""
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": tr("文件不存在")})
    name = d.get("name") or os.path.splitext(os.path.basename(path))[0]
    if task_busy(f"modpack:{name}"):
        return jsonify({"ok": False, "error": tr("这个整合包正在安装，不用重复点")})
    _full = _too_many()
    if _full:
        return _full
    threading.Thread(target=modpack_install_thread,
                     args=(None, os.path.basename(path), name, path),
                     daemon=True).start()
    return jsonify({"ok": True})


# ---------- Mod 管理：导入 / 更新 ----------
MODRINTH_LOADERS = {"fabric": "fabric", "forge": "forge", "neoforge": "neoforge", "quilt": "quilt"}

# 检查更新的后台状态（前端轮询 /api/mod/check_status 拿结果）
_mod_check = {"status": "idle", "progress": "", "mods": [], "error": "", "mc": "", "loader": ""}
_mod_check_lock = threading.Lock()
_mod_busy = threading.Lock()   # 检查 / 更新同一时间只跑一个


def _sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_mod_meta(path):
    """从 jar 里解析 Mod 元数据：modId / 名称 / 版本 / 加载器"""
    out = {"id": "", "name": os.path.basename(path)[:-4], "version": "", "loader": ""}
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "fabric.mod.json" in names:
                d = json.loads(z.read("fabric.mod.json").decode("utf-8", "replace"))
                out.update(id=d.get("id", ""), name=d.get("name") or d.get("id") or out["name"],
                           version=str(d.get("version", "")), loader="fabric")
                return out
            if "quilt.mod.json" in names:
                d = json.loads(z.read("quilt.mod.json").decode("utf-8", "replace"))
                q = (d.get("loader") or {}).get("metadata") or {}
                qv = q.get("version") or {}
                v = qv.get("version", "") if isinstance(qv, dict) else str(qv)
                out.update(id=q.get("id", ""), name=q.get("name") or q.get("id") or out["name"],
                           version=str(v), loader="quilt")
                return out
            for cand, ld in (("META-INF/neoforge.mods.toml", "neoforge"),
                             ("META-INF/mods.toml", "forge")):
                if cand in names:
                    txt = z.read(cand).decode("utf-8", "replace")
                    mid = re.search(r'\bmodId\s*=\s*"([^"]+)"', txt)
                    disp = re.search(r'\bdisplayName\s*=\s*"([^"]*)"', txt)
                    ver = re.search(r'\bversion\s*=\s*"([^"]*)"', txt)
                    v = ver.group(1) if ver else ""
                    if "$" in v:          # ${file.jarVersion} 之类，取不到真版本
                        v = ""
                    out.update(id=mid.group(1) if mid else "",
                               name=(disp.group(1) if disp and disp.group(1) else out["name"]),
                               version=v, loader=ld)
                    return out
            if "mcmod.info" in names:
                d = json.loads(z.read("mcmod.info").decode("utf-8", "replace"))
                ml = d if isinstance(d, list) else d.get("modlist") or d.get("mods") or []
                if ml and isinstance(ml[0], dict):
                    m0 = ml[0]
                    out.update(id=m0.get("modid", ""), name=m0.get("name") or m0.get("modid") or out["name"],
                               version=str(m0.get("version", "")), loader="forge")
    except Exception:
        pass
    return out


def _base_mc_version(vid):
    """沿 inheritsFrom 链找根，得到这个实例对应的原版 MC 版本号（如 1.20.1）"""
    cur, seen, result = vid, set(), vid
    while cur and cur not in seen:
        seen.add(cur)
        jp = os.path.join(VERSIONS_DIR, cur, cur + ".json")
        if not os.path.isfile(jp):
            break
        try:
            with open(jp, "r", encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            break
        nxt = j.get("inheritsFrom") or ""
        if nxt:
            result = nxt
            cur = nxt
        else:
            break
    return result


def _modrinth_match(query, mc, loader):
    """在 Modrinth 上搜 Mod 项目：优先 slug/标题与 modId 完全对上的那条"""
    if not query:
        return None
    facets = [["project_type:mod"], [f"versions:{mc}"]]
    if loader:
        facets.append([f"categories:{loader}"])
    r = requests.get(f"{MODRINTH_API}/search",
                     params={"query": query, "limit": 4, "facets": json.dumps(facets)},
                     timeout=20)
    r.raise_for_status()
    hits = (r.json() or {}).get("hits") or []
    if not hits:
        return None
    q = (query or "").lower()
    for h in hits:
        if q in {(h.get("slug") or "").lower(), (h.get("title") or "").lower()}:
            return h
    return hits[0]


def _modrinth_versions_with_hash(slug, mc, loader):
    """拿 Modrinth 某项目的版本列表（原始结构，含文件 sha1）"""
    params = {"game_versions": json.dumps([mc])}
    if loader:
        params["loaders"] = json.dumps([loader])
    r = requests.get(f"{MODRINTH_API}/project/{slug}/version", params=params, timeout=20)
    r.raise_for_status()
    out = []
    for v in (r.json() or []):
        flist = v.get("files") or []
        f = next((x for x in flist if x.get("primary")), flist[0] if flist else None)
        if not f:
            continue
        out.append({
            "number": v.get("version_number", ""),
            "date": (v.get("date_published") or "")[:10],
            "filename": f.get("filename", ""),
            "url": f.get("url", ""),
            "size": f.get("size", 0),
            "sha1": (f.get("hashes") or {}).get("sha1", ""),
        })
    return out


def _mod_check_thread(vid):
    try:
        meta = version_meta(vid)
        loader = MODRINTH_LOADERS.get((meta.get("loader") or "").lower(), "")
        if not loader:
            raise RuntimeError(tr("这个版本不是 Forge / Fabric / NeoForge，没有可检查的 Mod"))
        mc = _base_mc_version(vid)
        mdir = os.path.join(game_dir_for(vid), "mods")
        files = sorted(n for n in os.listdir(mdir) if n.lower().endswith(".jar")) \
            if os.path.isdir(mdir) else []
        if not files:
            raise RuntimeError(tr("mods 文件夹是空的，先去「下载游戏 → 资源」装几个 Mod"))
        results = []
        total = len(files)
        for i, name in enumerate(files, 1):
            if is_cancelled():
                raise Cancelled()
            with _mod_check_lock:
                _mod_check["progress"] = f"{name} ({i}/{total})"
            set_progress(phase="downloading", installing=True,
                         message=f"{tr('正在检查 Mod 更新')} ({i}/{total})…",
                         percent=int((i - 1) / total * 100))
            p = os.path.join(mdir, name)
            info = _read_mod_meta(p)
            try:
                sha = _sha1_of(p)
            except OSError:
                sha = ""
            entry = {"file": name, "title": info["name"], "mod_id": info["id"],
                     "version": info["version"], "jar_loader": info["loader"],
                     "status": "not_found", "latest_number": "", "latest_date": "",
                     "latest_url": "", "latest_filename": "", "latest_size": 0,
                     "slug": "", "sha1": sha}
            results.append(entry)
            try:
                hit = _modrinth_match(info["id"] or info["name"], mc, loader)
                if not hit:
                    continue
                entry["slug"] = hit.get("slug", "")
                entry["title"] = hit.get("title") or entry["title"]
                vers = _modrinth_versions_with_hash(entry["slug"], mc, loader)
                if not vers and loader:          # 放宽 loader 约束再试一次
                    vers = _modrinth_versions_with_hash(entry["slug"], mc, "")
                if not vers:
                    continue
                latest = vers[0]
                entry["latest_number"] = latest["number"]
                entry["latest_date"] = latest["date"]
                entry["latest_url"] = latest["url"]
                entry["latest_filename"] = latest["filename"]
                entry["latest_size"] = latest["size"]
                known = {v["sha1"] for v in vers if v["sha1"]}
                if sha and sha in known:
                    entry["status"] = "up_to_date" if sha == latest["sha1"] else "update"
                else:
                    entry["status"] = "update"
            except Exception:
                continue
        with _mod_check_lock:
            _mod_check.update(status="done", mods=results, progress="", mc=mc, loader=loader)
        set_progress(phase="done", installing=False, percent=100, message=tr("检查完成"))
        log(f"{tr('检查完成')}: {vid} {tr('共')} {total} {tr('个 Mod')}")
    except Cancelled:
        with _mod_check_lock:
            _mod_check.update(status="idle", progress="")
        set_progress(phase="idle", installing=False, message=tr("已取消"))
    except Exception as e:
        with _mod_check_lock:
            _mod_check.update(status="error", error=str(e), progress="")
        report_error(tr("检查 Mod 更新出错"), e)


def _mod_update_thread(vid, updates):
    try:
        mdir = os.path.join(game_dir_for(vid), "mods")
        total = len(updates)
        for i, u in enumerate(updates, 1):
            if is_cancelled():
                raise Cancelled()
            set_progress(phase="downloading", installing=True,
                         message=f"{tr('正在更新 Mod')} {u['file']} → {u['filename']} ({i}/{total})",
                         percent=int((i - 1) / max(total, 1) * 100))
            dest = os.path.join(mdir, u["filename"])
            _download_with_progress(u["url"], dest)
            old = os.path.join(mdir, u["file"])
            if os.path.abspath(old) != os.path.abspath(dest) and os.path.isfile(old):
                os.remove(old)
            log(f"{tr('已更新 ')}{u['file']} -> {u['filename']}")
        set_progress(phase="done", installing=False, percent=100,
                     message=f"{total}{tr(' 个 Mod 更新完成！已放入 ')}{vid}{tr(' 的 mods/ 文件夹')}")
        log(f"{tr('Mod 更新完成')}: {vid}")
    except Cancelled:
        set_progress(phase="idle", installing=False, message=tr("已取消"))
    except Exception as e:
        report_error(tr("更新 Mod 出错"), e)


@app.route("/api/mod/import", methods=["POST"])
def api_mod_import():
    """把本地 .jar 文件导入到指定版本的 mods/ 文件夹，重名自动加 (1) 后缀"""
    try:
        d = request.get_json(force=True) or {}
        vid = d.get("version") or ""
        paths = d.get("paths") or []
        if not vid:
            return jsonify({"ok": False, "error": tr("请选择版本")})
        mdir = os.path.join(game_dir_for(vid), "mods")
        os.makedirs(mdir, exist_ok=True)
        imported, skipped = [], []
        for p in paths:
            p = str(p or "")
            if not os.path.isfile(p) or not p.lower().endswith((".jar", ".zip")):
                skipped.append({"name": os.path.basename(p) or p, "reason": tr("不是 .jar 文件")})
                continue
            base = os.path.basename(p)
            stem, ext = os.path.splitext(base)
            dest = os.path.join(mdir, base)
            n = 1
            while os.path.exists(dest):
                dest = os.path.join(mdir, f"{stem} ({n}){ext}")
                n += 1
            shutil.copy2(p, dest)
            imported.append(os.path.basename(dest))
        log(f"{tr('已导入 Mod 到 ')}{vid}: {', '.join(imported) or '-'}")
        return jsonify({"ok": True, "imported": imported, "skipped": skipped})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/mod/check_updates", methods=["POST"])
def api_mod_check_updates():
    d = request.get_json(force=True) or {}
    vid = d.get("version") or ""
    if not vid:
        return jsonify({"ok": False, "error": tr("请选择版本")})
    if not _mod_busy.acquire(blocking=False):
        return jsonify({"ok": False, "error": tr("已有 Mod 任务正在进行，请稍等")})
    with _mod_check_lock:
        _mod_check.update(status="running", progress=tr("准备中…"), mods=[], error="",
                          mc="", loader="")
    def _run():
        try:
            _mod_check_thread(vid)
        finally:
            _mod_busy.release()
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/mod/check_status")
def api_mod_check_status():
    with _mod_check_lock:
        return jsonify({"ok": True, **_mod_check})


@app.route("/api/mod/update", methods=["POST"])
def api_mod_update():
    d = request.get_json(force=True) or {}
    vid = d.get("version") or ""
    updates = [u for u in (d.get("updates") or [])
               if u.get("file") and u.get("url") and u.get("filename")]
    if not vid:
        return jsonify({"ok": False, "error": tr("请选择版本")})
    if not updates:
        return jsonify({"ok": False, "error": tr("没有勾选要更新的 Mod")})
    if not _mod_busy.acquire(blocking=False):
        return jsonify({"ok": False, "error": tr("已有 Mod 任务正在进行，请稍等")})
    def _run():
        try:
            _mod_update_thread(vid, updates)
        finally:
            _mod_busy.release()
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True})


# ---------- Mod 管理（模组设置页） ----------
MOD_ICON_DIR = os.path.join(LAUNCHER_DIR, "mod_icons")
MOD_TRASH_DIR = os.path.join(LAUNCHER_DIR, "mod_trash")
MOD_CACHE_PATH = os.path.join(LAUNCHER_DIR, "mod_info_cache.json")
MOD_CACHE_TTL = 6 * 3600          # Modrinth 结果缓存 6 小时


def _mods_dir(vid):
    return os.path.join(game_dir_for(vid), "mods")


def _mc_of(vid):
    """这个实例对应的原版 MC 版本号。

    inheritsFrom 链能给就直接用；PCL2 那种自带 jar 的完整副本没有继承关系，
    从版本名里抠（26.2-Fabric 0.19.3 → 26.2）。
    """
    mc = _base_mc_version(vid)
    if mc and mc != vid:
        return mc
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", vid or "")
    return m.group(1) if m else (vid or "")


def _mod_item_name(name):
    """把 foo.jar.disabled 拆成 (真实文件名 foo.jar, 是否启用)"""
    if name.lower().endswith(".disabled"):
        return name[:-9], False
    return name, True


def _mod_tags(keys):
    """Modrinth 分类 → 界面标签（这里集中 tr()，换语言时重建）"""
    m = {
        "optimization": tr("性能优化"), "decoration": tr("装饰"), "utility": tr("实用"),
        "library": tr("支持库"), "adventure": tr("冒险"), "worldgen": tr("世界生成"),
        "mobs": tr("生物"), "equipment": tr("装备"), "food": tr("食物"),
        "magic": tr("魔法"), "technology": tr("科技"), "storage": tr("存储"),
        "transportation": tr("运输"), "game-mechanics": tr("机制"), "management": tr("管理"),
        "minigame": tr("小游戏"), "building": tr("建筑"), "economy": tr("经济"),
        "social": tr("社交"), "cosmetic": tr("外观"), "cursed": tr("黑科技"),
        "world": tr("世界"), "api": tr("API"),
    }
    return [m[k] for k in keys if k in m]


def _mod_icon(jar_path, mod_id=""):
    """从 jar 里抠图标存成 png 缓存，成功返回缓存路径，否则空串"""
    try:
        st = os.stat(jar_path)
    except OSError:
        return ""
    key = hashlib.sha1(
        f"{os.path.abspath(jar_path)}|{st.st_mtime}|{st.st_size}".encode("utf-8")).hexdigest()
    dst = os.path.join(MOD_ICON_DIR, key + ".png")
    if os.path.isfile(dst):
        return dst
    cands = []
    try:
        with zipfile.ZipFile(jar_path) as z:
            for n in z.namelist():
                low = n.lower()
                if not low.endswith((".png", ".jpg", ".jpeg")):
                    continue
                if low.endswith(("icon.png", "icon.jpg", "logo.png", "logo.jpg",
                                 "pack.png", "modicon.png")):
                    if low.count("/") <= 4:
                        cands.append(n)
            if not cands:
                return ""
            mid = (mod_id or "").lower()

            def score(n):
                low = n.lower()
                s = low.count("/")
                if mid and f"/{mid}/" in low:
                    s -= 6
                if low.endswith("icon.png"):
                    s -= 3
                return s

            cands.sort(key=score)
            data = z.read(cands[0])
        os.makedirs(MOD_ICON_DIR, exist_ok=True)
        with open(dst, "wb") as f:
            f.write(data)
        return dst
    except Exception:
        return ""


def scan_mods(vid):
    """扫一个版本的 mods 目录（纯本地，快）"""
    meta = version_meta(vid)
    mdir = _mods_dir(vid)
    files = []
    if os.path.isdir(mdir):
        files = sorted((n for n in os.listdir(mdir)
                        if n.lower().endswith((".jar", ".jar.disabled"))),
                       key=lambda s: s.lower())
    out = []
    total_disabled = 0
    for name in files:
        real, enabled = _mod_item_name(name)
        if not real.lower().endswith(".jar"):
            continue
        p = os.path.join(mdir, name)
        info = _read_mod_meta(p)
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        icon = bool(_mod_icon(p, info["id"]))
        if not enabled:
            total_disabled += 1
        out.append({
            "file": name, "name": real, "enabled": enabled,
            "title": info["name"] or real[:-4], "mod_id": info["id"],
            "version": info["version"], "jar_loader": info["loader"],
            "size": size, "icon": icon,
            # 远程信息（enrich 阶段回填）
            "desc": "", "tags": [], "slug": "", "status": "",
            "latest_number": "", "latest_date": "", "latest_url": "",
            "latest_filename": "", "latest_size": 0,
        })
    return {
        "version": vid, "loader": meta.get("loader") or "原版",
        "loader_version": meta.get("loader_version") or "",
        "mc": _mc_of(vid), "dir": mdir,
        "mods": out, "disabled": total_disabled,
    }


# ---- Modrinth 远程信息（后台跑，边跑边回填） ----
_mods_job = {"status": "idle", "progress": "", "version": "", "done": 0,
             "total": 0, "items": {}, "error": "", "mc": "", "loader": ""}
_mods_job_lock = threading.Lock()


MOD_CACHE_VER = 2          # 匹配规则改了就把版本号 +1，旧缓存自动作废


def _mod_cache_load():
    try:
        with open(MOD_CACHE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or d.get("_ver") != MOD_CACHE_VER:
            return {}
        items = d.get("items")
        return items if isinstance(items, dict) else {}
    except Exception:
        return {}


def _mod_cache_save(d):
    try:
        os.makedirs(os.path.dirname(MOD_CACHE_PATH), exist_ok=True)
        tmp = MOD_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"_ver": MOD_CACHE_VER, "items": d}, f, ensure_ascii=False)
        os.replace(tmp, MOD_CACHE_PATH)
    except Exception:
        pass


def _modrinth_search(query, mc, loader):
    """按 Modrinth 搜：先卡版本+加载器，没结果再逐级放宽"""
    if not query:
        return None
    tries = []
    if mc:
        tries.append([["project_type:mod"], [f"versions:{mc}"]]
                     + ([[f"categories:{loader}"]] if loader else []))
    if loader:
        tries.append([["project_type:mod"], [f"categories:{loader}"]])
    tries.append([["project_type:mod"]])
    for facets in tries:
        try:
            r = requests.get(f"{MODRINTH_API}/search",
                             params={"query": query, "limit": 6,
                                     "facets": json.dumps(facets)},
                             timeout=20)
            r.raise_for_status()
            hits = (r.json() or {}).get("hits") or []
        except Exception:
            continue
        if not hits:
            continue
        q = (query or "").lower()
        for h in hits:
            if q in {(h.get("slug") or "").lower(), (h.get("title") or "").lower()}:
                return h
        return hits[0]
    return None


def _modrinth_latest(slug, mc, loader):
    """拿该项目「适配当前 MC + 加载器」的最新文件。

    只认这两个筛选条件：筛不到就返回 None（界面上标成「没有适配当前版本的文件」），
    绝不能退到「全部版本里挑最新的」——那会给 26.2 推一个 26.3 的 jar，装上去直接崩。
    """
    vers = _modrinth_versions_with_hash(slug, mc, loader)
    if not vers and loader:
        vers = _modrinth_versions_with_hash(slug, mc, "")
    return vers[0] if vers else None


def _norm_name(s):
    """比较项目名用：去掉标点/空格/加载器后缀，全部小写"""
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (s or "").lower())
    for suf in ("neoforge", "fabric", "forge", "quilt"):
        if s.endswith(suf) and len(s) > len(suf) + 3:
            s = s[: -len(suf)]
    return s


def _same_mod(hit, info):
    """Modrinth 搜出来的这个项目，真的是本地这个 Mod 吗？

    同名/近名的项目太多了（voicechat vs voicechatprofiles、optifine vs optifine-capes），
    认错就会给玩家推一个完全不相关的 jar，所以宁可不认。
    """
    from difflib import SequenceMatcher
    mine = {_norm_name(info.get("id")), _norm_name(info.get("name"))}
    mine.discard("")
    his = {_norm_name(hit.get("slug")), _norm_name(hit.get("title"))}
    his.discard("")
    for a in mine:
        for b in his:
            if a == b:
                return True
            if min(len(a), len(b)) >= 5 and SequenceMatcher(None, a, b).ratio() >= 0.86:
                return True
    return False


def _enrich_one(mdir, name, mc, loader, cache):
    """单个 Mod：本地元数据 + Modrinth 项目信息 + 是否可更新"""
    real, enabled = _mod_item_name(name)
    p = os.path.join(mdir, name)
    info = _read_mod_meta(p)
    try:
        sha1 = _sha1_of(p)
    except OSError:
        sha1 = ""
    item = {"title": info["name"] or real[:-4], "version": info["version"],
            "mod_id": info["id"], "desc": "", "tags": [], "slug": "",
            "status": "", "latest_number": "", "latest_date": "", "latest_url": "",
            "latest_filename": "", "latest_size": 0}
    key = sha1 or f"{real}|{os.path.getsize(p) if os.path.isfile(p) else 0}"
    hit_cache = cache.get(key)
    if hit_cache and time.time() - hit_cache.get("ts", 0) < MOD_CACHE_TTL:
        item.update({k: v for k, v in hit_cache.items() if k != "ts"})
        return item
    try:
        hit = None
        for q in (info["id"], info["name"]):
            h = _modrinth_search(q, mc, loader)
            if h and _same_mod(h, info):
                hit = h
                break
        if not hit:
            item["status"] = "not_found"
        else:
            item["title"] = hit.get("title") or item["title"]
            item["desc"] = hit.get("description") or ""
            item["slug"] = hit.get("slug") or ""
            item["tags"] = _mod_tags(hit.get("categories") or [])
            latest = _modrinth_latest(item["slug"], mc, loader)
            if not latest:
                item["status"] = "unknown"
            else:
                item["latest_number"] = latest["number"]
                item["latest_date"] = latest["date"]
                item["latest_url"] = latest["url"]
                item["latest_filename"] = latest["filename"]
                item["latest_size"] = latest["size"]
                same_ver = bool(item["version"]) and item["version"] == latest["number"]
                same_file = bool(sha1) and sha1 == latest["sha1"]
                item["status"] = "up_to_date" if (same_ver or same_file) else "update"
    except Exception:
        item["status"] = ""
    cache[key] = dict(item, ts=time.time())
    return item


def _mods_enrich_thread(vid, files):
    mdir = _mods_dir(vid)
    meta = version_meta(vid)
    loader = MODRINTH_LOADERS.get((meta.get("loader") or "").lower(), "")
    mc = _mc_of(vid)
    cache = _mod_cache_load()
    total = len(files)
    with _mods_job_lock:
        _mods_job.update(status="running", progress="", version=vid, done=0,
                         total=total, items={}, error="", mc=mc, loader=loader)
    try:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_enrich_one, mdir, n, mc, loader, cache): n for n in files}
            for fu in as_completed(futs):
                name = futs[fu]
                try:
                    item = fu.result()
                except Exception:
                    item = None
                with _mods_job_lock:
                    if item is not None:
                        _mods_job["items"][name] = item
                    _mods_job["done"] = len(_mods_job["items"])
                    _mods_job["progress"] = name
        _mod_cache_save(cache)
        with _mods_job_lock:
            _mods_job.update(status="done", progress="")
    except Exception as e:
        with _mods_job_lock:
            _mods_job.update(status="error", error=str(e), progress="")


@app.route("/api/mods/scan", methods=["POST"])
def api_mods_scan():
    d = request.get_json(force=True) or {}
    vid = d.get("version") or ""
    if not vid:
        return jsonify({"ok": False, "error": tr("请选择版本")})
    try:
        return jsonify({"ok": True, **scan_mods(vid)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/mods/enrich", methods=["POST"])
def api_mods_enrich():
    d = request.get_json(force=True) or {}
    vid = d.get("version") or ""
    files = [str(x) for x in (d.get("files") or []) if str(x).endswith((".jar", ".disabled"))]
    if not vid:
        return jsonify({"ok": False, "error": tr("请选择版本")})
    if not files:
        return jsonify({"ok": True, "skipped": True})
    with _mods_job_lock:
        if _mods_job.get("status") == "running" and _mods_job.get("version") == vid:
            return jsonify({"ok": True, "running": True})
    threading.Thread(target=_mods_enrich_thread, args=(vid, files), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/mods/enrich_status")
def api_mods_enrich_status():
    with _mods_job_lock:
        snap = dict(_mods_job)
        snap["items"] = dict(_mods_job["items"])
    return jsonify({"ok": True, **snap})


@app.route("/api/mods/icon")
def api_mods_icon():
    vid = request.args.get("version") or ""
    name = os.path.basename(request.args.get("file") or "")
    if not vid or not name:
        return "", 404
    p = os.path.join(_mods_dir(vid), name)
    if not os.path.isfile(p):
        return "", 404
    icon = _mod_icon(p, _read_mod_meta(p)["id"])
    if not icon or not os.path.isfile(icon):
        return "", 404
    resp = send_file(icon, mimetype="image/png", max_age=86400)
    return resp


@app.route("/api/mod/delete", methods=["POST"])
def api_mod_delete():
    """删除 Mod：先移到 launcher/mod_trash/<时间戳>/，后悔了还能捞回来"""
    d = request.get_json(force=True) or {}
    vid = d.get("version") or ""
    files = [os.path.basename(str(x)) for x in (d.get("files") or []) if str(x).strip()]
    if not vid:
        return jsonify({"ok": False, "error": tr("请选择版本")})
    if not files:
        return jsonify({"ok": False, "error": tr("没有选中任何 Mod")})
    mdir = _mods_dir(vid)
    dest = os.path.join(MOD_TRASH_DIR, time.strftime("%Y%m%d-%H%M%S"))
    done, failed = [], []
    for name in files:
        p = os.path.join(mdir, name)
        if not os.path.isfile(p):
            failed.append({"name": name, "reason": tr("文件不存在")})
            continue
        try:
            os.makedirs(dest, exist_ok=True)
            shutil.move(p, os.path.join(dest, name))
            done.append(name)
        except Exception as e:
            failed.append({"name": name, "reason": str(e)})
    if done:
        log(f"{tr('已删除 ')}{len(done)}{tr(' 个 Mod（备份在 launcher/mod_trash）')}")
    _SIZE_CACHE.clear()
    return jsonify({"ok": True, "deleted": done, "failed": failed,
                    "trash": dest if done else ""})


@app.route("/api/mod/toggle", methods=["POST"])
def api_mod_toggle():
    """启用 / 禁用 Mod：靠文件名后缀 .disabled（和 PCL2、Forge 一致）"""
    d = request.get_json(force=True) or {}
    vid = d.get("version") or ""
    files = [os.path.basename(str(x)) for x in (d.get("files") or []) if str(x).strip()]
    enable = bool(d.get("enabled"))
    if not vid:
        return jsonify({"ok": False, "error": tr("请选择版本")})
    if not files:
        return jsonify({"ok": False, "error": tr("没有选中任何 Mod")})
    mdir = _mods_dir(vid)
    done, failed = [], []
    for name in files:
        real, cur_enabled = _mod_item_name(name)
        if cur_enabled == enable:
            continue
        src = os.path.join(mdir, name)
        dst_name = real if enable else real + ".disabled"
        dst = os.path.join(mdir, dst_name)
        if not os.path.isfile(src):
            failed.append({"name": name, "reason": tr("文件不存在")})
            continue
        if os.path.exists(dst):
            failed.append({"name": name, "reason": tr("目标文件已存在")})
            continue
        try:
            os.rename(src, dst)
            done.append(dst_name)
        except Exception as e:
            failed.append({"name": name, "reason": str(e)})
    if done:
        log(f"{tr('启用') if enable else tr('禁用')}{len(done)}{tr(' 个 Mod')}")
    _META_CACHE.clear()
    return jsonify({"ok": True, "changed": done, "failed": failed})


@app.route("/api/mod/open_folder", methods=["POST"])
def api_mod_open_folder():
    d = request.get_json(force=True) or {}
    vid = d.get("version") or ""
    if not vid:
        return jsonify({"ok": False, "error": tr("请选择版本")})
    mdir = _mods_dir(vid)
    os.makedirs(mdir, exist_ok=True)
    try:
        if platform.system() == "Windows":
            os.startfile(mdir)  # noqa
        else:
            subprocess.Popen(["xdg-open", mdir])
        return jsonify({"ok": True, "dir": mdir})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ---------- 开服务器（本地服务端托管） ----------
# 只负责：装 server.jar、分配内存启动、优雅停止。
# 启动器退出（含崩溃）服务器进程必须跟着退：Windows 下用 Job Object 兜底。
import ctypes
import socket

SERVER_DIR = os.path.join(MC_DIR, "server")
SERVER_JAR = os.path.join(SERVER_DIR, "server.jar")
PLAYIT_DIR = os.path.join(LAUNCHER_DIR, "playit")
PLAYIT_EXE = os.path.join(PLAYIT_DIR, "playit.exe")
# P2P 打洞：用 EasyTier（no-tun 模式免管理员权限，靠共享节点交换地址后直连）
P2P_DIR = os.path.join(LAUNCHER_DIR, "p2p")
P2P_EXE = os.path.join(P2P_DIR, "easytier-core.exe")
P2P_CLI = os.path.join(P2P_DIR, "easytier-cli.exe")
P2P_VERSION = "2.6.4"
# 官方 public.easytier.cn 已下线（NXDOMAIN），这几个是社区公益共享节点，可在设置里替换
P2P_PEERS = [
    "tcp://et.gbc.moe:11010",
    "tcp://boi.de5.net:11010",
    "tcp://easytier.weiai.org.cn:11010",
    "tcp://ros.scpsl.com.cn:11010",
]
P2P_HOST_IP = "10.77.0.1"      # 开服方在虚拟网络里的地址
P2P_JOIN_IP = "10.77.0.2"      # 加入方
P2P_HOST_RPC = 15888
P2P_JOIN_RPC = 15889

_srv_lock = threading.Lock()
_srv = {
    "proc": None,            # java 进程
    "job": None,             # Windows Job Object 句柄
    "installing": False,     # 正在下载 server.jar
    "log": [],               # 服务器控制台输出（环形缓冲）
    "ram": 2048,
    "version": "",           # server.jar 对应的 MC 版本
    "upnp": "",              # "ok" / "failed" / ""（未尝试）
    "upnp_ip": "",
    "upnp_port": 0,
    "playit_proc": None,
    "playit_claim": "",      # 待领取的 claim 链接
    "p2p_proc": None,        # easytier-core 进程
    "p2p_role": "",          # "host" / "join"
    "p2p_room": "",          # 房间号（= network-name）
    "p2p_secret": "",        # 房间密码（= network-secret）
    "p2p_local_port": 0,     # 加入方本地转发端口
    "p2p_online": False,     # 是否已看到对端
    "p2p_mode": "",          # "p2p"（直连）/ "relay"（共享节点中转）
    "p2p_dl": {"got": 0, "total": 0},
}


def _srv_log(msg):
    s = str(msg)
    # 服务端自己输出的行已经带 [HH:MM:SS]，别再加一层
    line = s if re.match(r"^\[\d\d:\d\d:\d\d", s) else time.strftime("[%H:%M:%S] ") + s
    with _srv_lock:
        _srv["log"].append(line)
        _srv["log"] = _srv["log"][-500:]


def _srv_running():
    p = _srv.get("proc")
    return bool(p and p.poll() is None)


def _srv_mem_mb():
    """服务器 java 进程的内存占用（MB，读工作集；读不到返回 0）"""
    proc = _srv.get("proc")
    if not (proc and proc.poll() is None):
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        if ctypes.windll.psapi.GetProcessMemoryInfo(
                int(proc._handle), ctypes.byref(pmc), pmc.cb):
            return int(pmc.WorkingSetSize // (1024 * 1024))
    except Exception:
        pass
    return 0


def _srv_cfg():
    cfg = load_config()
    return {
        "ram": int(cfg.get("server_ram", 2048)),
        "version": str(cfg.get("server_version", "")),
    }


def _srv_required_java(vid):
    """按 MC 版本推算服务端需要的 Java 大版本"""
    try:
        nums = re.findall(r"\d+", vid)
        a = int(nums[0]) if nums else 1
        b = int(nums[1]) if len(nums) > 1 else 0
        tail = int(nums[2]) if len(nums) > 2 else 0
        if a > 1 or b > 20 or (b == 20 and tail >= 5):
            return 21
        if (a, b) >= (1, 18):
            return 17
        if (a, b) >= (1, 17):
            return 16
        return 8
    except Exception:
        return 17


def _srv_java_exe(path):
    """服务端必须用 java.exe：javaw.exe 没有控制台，日志会全部丢失"""
    if not path:
        return path
    low = path.lower()
    if low.endswith("javaw.exe"):
        cand = path[: -len("javaw.exe")] + "java.exe"
        if os.path.isfile(cand):
            return cand
    return path


def _srv_port():
    """从 server.properties 读端口，没有就默认 25565"""
    try:
        p = os.path.join(SERVER_DIR, "server.properties")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                m = re.search(r"^server-port=(\d+)", f.read(), re.M)
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return 25565


def _srv_kill_job():
    job = _srv.get("job")
    if job:
        try:
            ctypes.windll.kernel32.TerminateJobObject(job, 0)
        except Exception:
            pass
        try:
            ctypes.windll.kernel32.CloseHandle(job)
        except Exception:
            pass
    _srv["job"] = None


def _srv_reader(proc):
    try:
        for line in proc.stdout:
            _srv_log(line.rstrip())
    except Exception:
        pass
    code = proc.wait()
    _srv_log(f"{tr('服务器已退出（退出码 ')}{code}）")
    with _srv_lock:
        _srv["proc"] = None
        _srv["job"] = None
    # 进程没了就顺手撤掉端口映射，避免占着路由器
    if _srv.get("upnp") == "ok":
        try:
            import mc_upnp
            mc_upnp.unmap_port(_srv.get("upnp_port") or 25565)
        except Exception:
            pass
        _srv["upnp"] = ""
        _srv["upnp_ip"] = ""


def _srv_assign_job(proc):
    """Windows：把 java 进程挂进 Job Object，启动器崩溃时系统自动回收服务器进程"""
    if SYSTEM_NAME != "windows":
        return None
    import ctypes
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = ctypes.windll.kernel32.CreateJobObjectW(None, None)
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ctypes.windll.kernel32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info))
    ctypes.windll.kernel32.AssignProcessToJobObject(
        job, int(proc._handle))
    return job


# ---------- 服务器 API ----------
# 服务器要用的 Java 只在后台探一次，状态接口不再每次都起 java.exe 探测
# （以前每 2.5 秒探一轮：慢机器上页面像卡死，打包版还会连带闪黑窗口）
_srv_java_pick = {"key": None, "path": "", "busy": False}
_srv_java_lock = threading.Lock()


def _resolve_srv_java(version, need):
    path = ""
    try:
        p = find_suitable_java(need)
        path = _srv_java_exe(p) or "" if p else ""
    except Exception:
        path = ""
    with _srv_java_lock:
        _srv_java_pick.update(key=(version, need), path=path, busy=False)


def _srv_java_cached(version):
    """已知的 java 路径；没探过就丢后台去探，这次先返回空"""
    need = _srv_required_java(version)
    jk = (version, need)
    with _srv_java_lock:
        if _srv_java_pick["key"] == jk:
            return _srv_java_pick["path"]
        start = not _srv_java_pick["busy"]
        if start:
            _srv_java_pick["busy"] = True
    if start:
        threading.Thread(target=_resolve_srv_java, args=(version, need), daemon=True).start()
    return ""


@app.route("/api/server/state")
def api_server_state():
    installed = os.path.isfile(SERVER_JAR)
    running = _srv_running()
    jar_size = os.path.getsize(SERVER_JAR) if installed else 0
    eula = False
    p = os.path.join(SERVER_DIR, "eula.txt")
    if os.path.isfile(p):
        try:
            eula = "eula=true" in open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            pass
    c = _srv_cfg()
    java = _srv_java_cached(c["version"]) if installed else ""
    playit_running = bool(_srv.get("playit_proc") and _srv["playit_proc"].poll() is None)
    return jsonify({
        "ok": True,
        "installed": installed,
        "jar_size": jar_size,
        "running": running,
        "installing": _srv["installing"],
        "eula": eula,
        "ram": c["ram"],
        "version": c["version"],
        "port": _srv_port(),
        "java_path": java or "",
        "upnp": _srv["upnp"],
        "upnp_ip": _srv["upnp_ip"],
        "upnp_port": _srv["upnp_port"],
        "playit_running": playit_running,
        "playit_claim": _srv.get("playit_claim", ""),
        "playit_installed": os.path.isfile(PLAYIT_EXE),
        "playit_dl": _srv.get("playit_dl") or {"got": 0, "total": 0},
        "p2p_running": _p2p_running(),
        "p2p_role": _srv.get("p2p_role") or "",
        "p2p_room": _srv.get("p2p_room") or _p2p_cfg()[0],
        "p2p_secret": _srv.get("p2p_secret") or _p2p_cfg()[1],
        "p2p_online": bool(_srv.get("p2p_online")),
        "p2p_mode": _srv.get("p2p_mode") or "",
        "p2p_local_port": _srv.get("p2p_local_port") or 0,
        "p2p_installed": _p2p_installed(),
        "p2p_dl": _srv.get("p2p_dl") or {"got": 0, "total": 0},
    })


@app.route("/api/server/log")
def api_server_log():
    with _srv_lock:
        return jsonify({"ok": True, "log": "\n".join(_srv["log"][-200:])})


@app.route("/api/server/versions")
def api_server_versions():
    """可用的服务端版本（只给正式版）"""
    try:
        vs = get_version_list(load_config().get("mirror", "official"))
        return jsonify({"ok": True,
                        "versions": [v["id"] for v in vs if v["type"] == "release"][:60]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/server/install", methods=["POST"])
def api_server_install():
    d = request.get_json(force=True)
    vid = (d.get("version") or "").strip()
    if not vid:
        return jsonify({"ok": False, "error": tr("请选择版本")})
    if _srv_running():
        return jsonify({"ok": False, "error": tr("服务器正在运行，先停止再换版本")})
    if _srv["installing"]:
        return jsonify({"ok": False, "error": tr("已在下载中")})
    threading.Thread(target=_srv_install_thread, args=(vid,), daemon=True).start()
    return jsonify({"ok": True})


def _srv_install_thread(vid):
    _srv["installing"] = True
    try:
        os.makedirs(SERVER_DIR, exist_ok=True)
        mirror = load_config().get("mirror", "official")
        vs = get_version_list(mirror)
        meta = next((v for v in vs if v["id"] == vid), None)
        if not meta:
            raise RuntimeError(f"{tr('没有找到版本 ')}{vid}")
        vjson = fetch_json(meta["url"], mirror)
        dl = (vjson.get("downloads") or {}).get("server")
        if not dl:
            raise RuntimeError(tr("该版本没有提供官方服务端"))
        _srv_log(f"{tr('开始下载 ')}{vid}{tr(' 服务端（')}{round(dl.get('size', 0) / 1048576)} MB）…")
        tmp = SERVER_JAR + ".tmp"
        with http_get(dl["url"], mirror, timeout=60, retries=2) as r:
            total = int(r.headers.get("content-length", 0) or dl.get("size", 0))
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    f.write(chunk)
        if total and os.path.getsize(tmp) != total:
            raise RuntimeError(tr("下载不完整，请重试"))
        sha1 = dl.get("sha1", "")
        if sha1:
            h = hashlib.sha1(open(tmp, "rb").read()).hexdigest()
            if h != sha1:
                raise RuntimeError(tr("校验失败，请重试"))
        if os.path.exists(SERVER_JAR):
            os.remove(SERVER_JAR)
        os.replace(tmp, SERVER_JAR)
        # eula 默认 false，必须用户在界面上同意后才写成 true
        eula_p = os.path.join(SERVER_DIR, "eula.txt")
        if not os.path.isfile(eula_p):
            with open(eula_p, "w", encoding="utf-8") as f:
                f.write(tr("# 已阅读并同意 Minecraft EULA 后才会写成 eula=true\n"
                        "# https://aka.ms/MinecraftEULA\neula=false\n"))
        cfg = load_config()
        cfg["server_version"] = vid
        save_config(cfg)
        _srv_log(f"{vid}{tr(' 服务端下载完成')}")
    except Exception as e:
        _srv_log(f"{tr('下载失败：')}{e}")
        try:
            if os.path.exists(SERVER_JAR + ".tmp"):
                os.remove(SERVER_JAR + ".tmp")
        except Exception:
            pass
    finally:
        _srv["installing"] = False


@app.route("/api/server/start", methods=["POST"])
def api_server_start():
    d = request.get_json(force=True) if request.data else {}
    ram = int(d.get("ram", _srv_cfg()["ram"]))
    accept_eula = bool(d.get("accept_eula"))
    ram = max(512, min(ram, 32768))

    if _srv_running():
        return jsonify({"ok": False, "error": tr("服务器已经在运行了（一次只能开一个）")})
    if not os.path.isfile(SERVER_JAR):
        return jsonify({"ok": False, "error": tr("还没有安装服务端")})
    eula_p = os.path.join(SERVER_DIR, "eula.txt")
    if accept_eula:
        with open(eula_p, "w", encoding="utf-8") as f:
            f.write(tr("# 已阅读并同意 Minecraft EULA\n# https://aka.ms/MinecraftEULA\neula=true\n"))
    elif not (os.path.isfile(eula_p)
              and "eula=true" in open(eula_p, encoding="utf-8",
                                      errors="ignore").read()):
        return jsonify({"ok": False, "error": tr("需要先同意 Minecraft EULA")})

    # 端口被其它程序占用时提前报错，别等 java 起来再炸
    port = _srv_port()
    try:
        t = socket.socket()
        t.settimeout(1)
        t.bind(("0.0.0.0", port))
        t.close()
    except OSError:
        return jsonify({"ok": False, "error": f"{tr('端口 ')}{port}{tr(' 已被其它程序占用，')}"
                                              + tr("请改 server.properties 或关闭占用程序")})

    vid = _srv_cfg()["version"]
    need = _srv_required_java(vid)
    java_exe = _srv_java_exe(find_suitable_java(need))
    if not java_exe:
        _srv_log(f"{tr('本机没有 Java ')}{need}{tr('，自动下载运行时…')}")
        try:
            java_exe = _srv_java_exe(ensure_java(need))
        except Exception as e:
            return jsonify({"ok": False, "error": f"{tr('需要 Java ')}{need}{tr('，自动下载失败：')}{e}"})
        if not java_exe:
            return jsonify({"ok": False, "error": f"{tr('需要 Java ')}{need}{tr('，自动下载失败')}"})

    os.makedirs(SERVER_DIR, exist_ok=True)
    cmd = [java_exe, f"-Xms{max(512, ram // 2)}M", f"-Xmx{ram}M",
           "-jar", "server.jar", "nogui"]
    flags = 0
    if SYSTEM_NAME == "windows":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            cmd, cwd=SERVER_DIR, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=flags, bufsize=1)
    except Exception as e:
        return jsonify({"ok": False, "error": f"{tr('启动失败：')}{e}"})

    with _srv_lock:
        _srv["proc"] = proc
        _srv["ram"] = ram
    _srv["job"] = _srv_assign_job(proc)
    threading.Thread(target=_srv_reader, args=(proc,), daemon=True).start()
    _srv_log(f"{tr('启动 ')}{vid or 'Minecraft'}{tr(' 服务器（内存 ')}{ram} MB，Java {need}）")
    record_event("server", f"start ram={ram}MB")
    # 记住这次的内存设置
    cfg = load_config()
    cfg["server_ram"] = ram
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/server/stop", methods=["POST"])
def api_server_stop():
    proc = _srv.get("proc")
    if not proc:
        return jsonify({"ok": True, "stopped": False})
    _srv_log(tr("正在停止服务器（正常存档退出）…"))
    try:
        proc.stdin.write("stop\n")
        proc.stdin.flush()
    except Exception:
        pass
    try:
        proc.wait(timeout=20)
    except Exception:
        _srv_log(tr("20 秒未退出，强制结束"))
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            _srv_kill_job()
    return jsonify({"ok": True, "stopped": True})


@app.route("/api/server/command", methods=["POST"])
def api_server_command():
    """往服务器控制台发指令（stop / whitelist / op 等）"""
    proc = _srv.get("proc")
    cmd = (request.get_json(force=True).get("command") or "").strip()
    if not proc or proc.poll() is not None:
        return jsonify({"ok": False, "error": tr("服务器未在运行")})
    if not cmd:
        return jsonify({"ok": False, "error": tr("指令为空")})
    try:
        proc.stdin.write(cmd + "\n")
        proc.stdin.flush()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/server/open_folder", methods=["POST"])
def api_server_open_folder():
    os.makedirs(SERVER_DIR, exist_ok=True)
    try:
        if SYSTEM_NAME == "windows":
            os.startfile(SERVER_DIR)  # noqa
        elif SYSTEM_NAME == "osx":
            subprocess.Popen(["open", SERVER_DIR])
        else:
            subprocess.Popen(["xdg-open", SERVER_DIR])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ---------- 内网穿透 ----------
@app.route("/api/server/upnp", methods=["POST"])
def api_server_upnp():
    if _srv.get("upnp") == "ok":
        return jsonify({"ok": True, "ip": _srv["upnp_ip"], "port": _srv["upnp_port"]})
    port = _srv_port()

    def worker():
        try:
            import mc_upnp
            ip = mc_upnp.map_port(port)
            with _srv_lock:
                _srv["upnp"] = "ok"
                _srv["upnp_ip"] = ip
                _srv["upnp_port"] = port
            _srv_log(f"{tr('端口映射成功：外网玩家用 ')}{ip}:{port}{tr(' 连接')}")
        except Exception as e:
            with _srv_lock:
                _srv["upnp"] = "failed"
            _srv_log(f"{tr('UPnP 映射失败：')}{e}{tr('（可改用 playit.gg 内网穿透）')}")

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "started": True})


@app.route("/api/server/playit/start", methods=["POST"])
def api_server_playit_start():
    if _srv.get("playit_proc") and _srv["playit_proc"].poll() is None:
        return jsonify({"ok": True, "already": True})
    if SYSTEM_NAME != "windows":
        return jsonify({"ok": False, "error": tr("目前仅支持 Windows")})

    if not os.path.isfile(PLAYIT_EXE):
        def dl():
            os.makedirs(PLAYIT_DIR, exist_ok=True)
            # gh-proxy 镜像排前面：国内直连 GitHub release 只有几十 KB/s
            urls = [
                "https://gh-proxy.com/https://github.com/playit-cloud/playit-agent/releases/latest/download/playit-windows-x86_64.exe",
                "https://github.com/playit-cloud/playit-agent/releases/latest/download/playit-windows-x86_64.exe",
            ]
            tmp = PLAYIT_EXE + ".tmp"
            for u in urls:
                try:
                    _srv_log(tr("正在下载 playit.gg 代理程序…"))
                    with http_get(u, "official", timeout=60, retries=2) as r:
                        total = int(r.headers.get("content-length", 0) or 0)
                        _srv["playit_dl"] = {"got": 0, "total": total}
                        with open(tmp, "wb") as f:
                            for chunk in r.iter_content(256 * 1024):
                                f.write(chunk)
                                _srv["playit_dl"]["got"] += len(chunk)
                    _srv["playit_dl"] = {"got": 0, "total": 0}
                    os.replace(tmp, PLAYIT_EXE)
                    _srv_log(tr("playit 下载完成，正在启动…"))
                    _srv_playit_run()
                    return
                except Exception as e:
                    _srv["playit_dl"] = {"got": 0, "total": 0}
                    _srv_log(f"{tr('playit 下载失败（')}{u.split('/')[2]}）：{e}")
            _srv_log(tr("playit 下载失败，请检查网络"))
        threading.Thread(target=dl, daemon=True).start()
        return jsonify({"ok": True, "downloading": True})
    return jsonify({"ok": True, "started": _srv_playit_run() is not None})


def _srv_playit_run():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            [PLAYIT_EXE], cwd=PLAYIT_DIR, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=flags, bufsize=1)
    except Exception as e:
        _srv_log(f"{tr('playit 启动失败：')}{e}")
        return None

    def reader():
        claim_re = re.compile(r"https://playit\.gg/claim/[A-Za-z0-9]+")
        try:
            for line in proc.stdout:
                line = line.rstrip()
                m = claim_re.search(line)
                if m:
                    _srv["playit_claim"] = m.group(0)
                    _srv_log(f"{tr('请在浏览器打开链接绑定隧道：')}{_srv['playit_claim']}")
                if line:
                    _srv_log("[playit] " + line)
        except Exception:
            pass
        proc.wait()
        _srv["playit_claim"] = ""
        _srv_log(tr("[playit] 代理已退出"))

    _srv["playit_proc"] = proc
    threading.Thread(target=reader, daemon=True).start()
    return proc


@app.route("/api/server/playit/stop", methods=["POST"])
def api_server_playit_stop():
    proc = _srv.get("playit_proc")
    if not proc:
        return jsonify({"ok": True})
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    _srv["playit_proc"] = None
    _srv["playit_claim"] = ""
    return jsonify({"ok": True})


# ---------- P2P 直连（EasyTier 打洞） ----------
# 双方都用 no-tun 模式：不建虚拟网卡、不要管理员权限。
# 开服方把 25565 放行在虚拟网络里，加入方把本地端口转发到 10.77.0.1:25565，
# 一起连同一个共享节点交换地址，能打洞就直连，打不通才走共享节点中转。

def _p2p_cfg():
    cfg = load_config()
    return str(cfg.get("p2p_room") or ""), str(cfg.get("p2p_secret") or "")


def _p2p_ensure_room():
    """没有房间号就生成一个，并持久化（重开启动器不变，朋友不用重新要）"""
    room, sec = _p2p_cfg()
    if not room or not sec:
        room = "mc" + os.urandom(4).hex()
        sec = os.urandom(16).hex()
        cfg = load_config()
        cfg["p2p_room"], cfg["p2p_secret"] = room, sec
        save_config(cfg)
        _srv["p2p_room"], _srv["p2p_secret"] = room, sec
    return room, sec


def _p2p_installed():
    return os.path.isfile(P2P_EXE)


def _p2p_running():
    p = _srv.get("p2p_proc")
    return bool(p and p.poll() is None)


def _p2p_download():
    """下官方 zip，只解出 core / cli 和几个依赖文件"""
    import zipfile
    os.makedirs(P2P_DIR, exist_ok=True)
    zpath = os.path.join(P2P_DIR, "et.zip")
    tag = f"v{P2P_VERSION}"
    name = f"easytier-windows-x86_64-{tag}.zip"
    urls = [
        f"https://gh-proxy.com/https://github.com/EasyTier/EasyTier/releases/download/{tag}/{name}",
        f"https://github.com/EasyTier/EasyTier/releases/download/{tag}/{name}",
    ]
    last = ""
    for u in urls:
        try:
            _srv_log(f"{tr('正在下载 P2P 组件（')}{u.split('/')[2]}）…")
            with http_get(u, "official", timeout=180, retries=2) as r:
                total = int(r.headers.get("content-length", 0) or 0)
                _srv["p2p_dl"] = {"got": 0, "total": total}
                with open(zpath, "wb") as f:
                    for chunk in r.iter_content(256 * 1024):
                        f.write(chunk)
                        _srv["p2p_dl"]["got"] += len(chunk)
            _srv["p2p_dl"] = {"got": 0, "total": 0}
            want = {"easytier-core.exe", "easytier-cli.exe", "wintun.dll",
                    "WinDivert64.sys", "Packet.dll"}
            with zipfile.ZipFile(zpath) as z:
                for n in z.namelist():
                    base = n.rsplit("/", 1)[-1]
                    if base in want and not n.endswith("/"):
                        with z.open(n) as src, open(os.path.join(P2P_DIR, base), "wb") as dst:
                            dst.write(src.read())
            os.remove(zpath)
            if not os.path.isfile(P2P_EXE):
                raise RuntimeError(tr("压缩包里没有 easytier-core.exe"))
            _srv_log(tr("P2P 组件下载完成"))
            return True
        except Exception as e:
            last = str(e)
            _srv["p2p_dl"] = {"got": 0, "total": 0}
            try:
                if os.path.exists(zpath):
                    os.remove(zpath)
            except Exception:
                pass
    _srv_log(f"{tr('P2P 组件下载失败：')}{last}")
    return False


def _p2p_cmd(role, room, sec, server_port, local_port):
    cmd = [P2P_EXE, "--no-tun", "--console-log-level", "warn",
           "--network-name", room, "--network-secret", sec]
    for p in P2P_PEERS[:2]:
        cmd += ["-p", p]
    if role == "host":
        cmd += ["--ipv4", P2P_HOST_IP, "-m", "mclauncher-host",
                "--hostname", "mc-host",
                "--tcp-whitelist", str(server_port),
                "-r", f"127.0.0.1:{P2P_HOST_RPC}"]
    else:
        cmd += ["--ipv4", P2P_JOIN_IP, "-m", "mclauncher-join",
                "--hostname", "mc-join", "--no-listener",
                "--port-forward",
                f"tcp://127.0.0.1:{local_port}/{P2P_HOST_IP}:{server_port}",
                "-r", f"127.0.0.1:{P2P_JOIN_RPC}"]
    return cmd


def _p2p_spawn(role, room, sec, server_port, local_port):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            _p2p_cmd(role, room, sec, server_port, local_port),
            cwd=P2P_DIR, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=flags, bufsize=1)
    except Exception as e:
        _srv_log(f"{tr('P2P 启动失败：')}{e}")
        return None
    with _srv_lock:
        _srv["p2p_proc"] = proc
        _srv["p2p_role"] = role
        _srv["p2p_room"] = room
        _srv["p2p_secret"] = sec
        _srv["p2p_local_port"] = local_port
        _srv["p2p_online"] = False
        _srv["p2p_mode"] = ""
    _srv_log(f"{tr('P2P 已启动（')}{tr('开服方') if role == 'host' else tr('加入方')}{tr(' · 房间 ')}{room}）")

    def reader():
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    _srv_log("[p2p] " + line)
        except Exception:
            pass
        proc.wait()
        _srv["p2p_online"] = False
        _srv["p2p_mode"] = ""
        _srv_log(tr("[p2p] 连接已退出"))

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=_p2p_watch, args=(proc, role), daemon=True).start()
    return proc


def _p2p_watch(proc, role):
    """每 4 秒用 cli 查一次对端，判断有没有打通、是直连还是中转"""
    other = P2P_JOIN_IP if role == "host" else P2P_HOST_IP
    rpc = P2P_HOST_RPC if role == "host" else P2P_JOIN_RPC
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    while proc.poll() is None:
        try:
            out = subprocess.run([P2P_CLI, "-p", f"127.0.0.1:{rpc}", "peer"],
                                 capture_output=True, text=True, timeout=8,
                                 creationflags=flags,
                                 encoding="utf-8", errors="replace").stdout or ""
            online = other in out
            mode = ""
            if online:
                for ln in out.splitlines():
                    if other in ln:
                        mode = "p2p" if "p2p" in ln else "relay"
                        break
            _srv["p2p_online"] = online
            _srv["p2p_mode"] = mode
        except Exception:
            pass
        time.sleep(4)


def _free_port(preferred):
    for p in [preferred] + list(range(preferred + 1, preferred + 30)):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return preferred


@app.route("/api/server/p2p/start", methods=["POST"])
def api_server_p2p_start():
    d = request.get_json(force=True) if request.data else {}
    role = (d.get("role") or "host").strip()
    if role not in ("host", "join"):
        return jsonify({"ok": False, "error": tr("未知模式")})
    if SYSTEM_NAME != "windows":
        return jsonify({"ok": False, "error": tr("P2P 直连目前仅支持 Windows")})
    if _p2p_running():
        return jsonify({"ok": False, "error": tr("P2P 连接已经在运行了")})

    room = (d.get("room") or "").strip()
    sec = (d.get("secret") or "").strip()
    if role == "host":
        if not room or not sec:
            room, sec = _p2p_ensure_room()
        else:
            cfg = load_config()
            cfg["p2p_room"], cfg["p2p_secret"] = room, sec
            save_config(cfg)
            _srv["p2p_room"], _srv["p2p_secret"] = room, sec
    elif not room or not sec:
        return jsonify({"ok": False, "error": tr("请填写房间号和密码")})

    server_port = _srv_port()
    try:
        local_port = int(d.get("local_port") or server_port)
    except Exception:
        local_port = server_port
    if role == "join":
        local_port = _free_port(local_port)

    if not _p2p_installed():
        def dl():
            if _p2p_download():
                _p2p_spawn(role, room, sec, server_port, local_port)
        threading.Thread(target=dl, daemon=True).start()
        return jsonify({"ok": True, "downloading": True, "room": room,
                        "secret": sec, "local_port": local_port})

    proc = _p2p_spawn(role, room, sec, server_port, local_port)
    return jsonify({"ok": proc is not None, "room": room, "secret": sec,
                    "local_port": local_port,
                    "error": "" if proc else tr("启动失败，详情看控制台")})


@app.route("/api/server/p2p/stop", methods=["POST"])
def api_server_p2p_stop():
    proc = _srv.get("p2p_proc")
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    with _srv_lock:
        _srv["p2p_proc"] = None
        _srv["p2p_online"] = False
        _srv["p2p_mode"] = ""
        _srv["p2p_role"] = ""
    return jsonify({"ok": True})


@app.route("/api/server/p2p/room", methods=["POST"])
def api_server_p2p_room():
    """生成 / 重置房间号（重置后旧房间的人就进不来了）"""
    d = request.get_json(force=True) if request.data else {}
    if _p2p_running():
        return jsonify({"ok": False, "error": tr("先停止 P2P 再换房间")})
    if d.get("reset"):
        cfg = load_config()
        cfg["p2p_room"] = "mc" + os.urandom(4).hex()
        cfg["p2p_secret"] = os.urandom(16).hex()
        save_config(cfg)
    room, sec = _p2p_ensure_room()
    return jsonify({"ok": True, "room": room, "secret": sec})


def server_cleanup_on_exit():
    """启动器退出前调用：优雅停服（等最多 15 秒），再收掉 playit / P2P"""
    try:
        if _srv_running():
            proc = _srv["proc"]
            try:
                proc.stdin.write("stop\n")
                proc.stdin.flush()
            except Exception:
                pass
            try:
                proc.wait(timeout=15)
            except Exception:
                _srv_kill_job()
            _srv_log(tr("启动器退出，服务器已停止"))
        if _srv.get("playit_proc") and _srv["playit_proc"].poll() is None:
            try:
                _srv["playit_proc"].terminate()
            except Exception:
                pass
        if _srv.get("p2p_proc") and _srv["p2p_proc"].poll() is None:
            try:
                _srv["p2p_proc"].terminate()
            except Exception:
                pass
    except Exception:
        pass


if __name__ == "__main__":
    load_saved_lang()
    os.makedirs(MC_DIR, exist_ok=True)
    refresh_installed()
    try:
        repair_merged_versions()
    except Exception as e:
        log(f"{tr('修复版本库失败: ')}{e}")
    start_session("server-direct")
    log(f"{LAUNCHER_NAME}{tr(' 已启动，游戏目录: ')}{MC_DIR}")
    print(f"{LAUNCHER_NAME} -> http://127.0.0.1:8848")
    import webbrowser
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:8848")).start()

    def _tidy_later():
        time.sleep(6)
        try:
            if active_tasks():
                return
            done = normalize_loader_versions()
            if done:
                log(f"{tr('已整理 ')}{len(done)}{tr(' 个版本目录')}")
        except Exception as e:
            log(f"{tr('整理版本目录失败: ')}{e}")

    threading.Thread(target=_tidy_later, daemon=True).start()
    app.run(host="127.0.0.1", port=8848, threaded=True)
