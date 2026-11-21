# -*- coding: utf-8 -*-
"""报错自动分析：把一条错误消息 / 一段日志翻译成人话。

- analyze(text) -> dict 或 None
  返回 {"title", "reason", "tips": [...], "hit", "rule_id"}
  title / reason / tips 均为中文原文，展示层统一过 tr() 翻译。
- 规则按优先级排列，命中第一条即返回；全部未命中返回 None。
- 供界面使用：错误提示卡片、服务器控制台、日志页都能把文本丢进来。
"""
import re


def _r(keys, title, reason, tips):
    return {"keys": keys, "title": title, "reason": reason, "tips": tips}


# ---------------------------------------------------------------- 规则库
# keys：正则列表（IGNORECASE），命中任意一条即认为匹配该规则。
RULES = [
    # 1. Java 版本不匹配
    _r([r"unsupported\s+class\s+file\s+major\s+version", r"unsupportedclassversionerror",
        r"has\s+been\s+compiled\s+by\s+a\s+more\s+recent\s+version", r"bad\s+class\s+file"],
       "Java 版本不匹配",
       "这个版本的游戏需要另一个大版本的 Java（1.16.5 及以前用 Java 8，1.17 用 Java 16/17，"
       "1.20.5 及以上用 Java 21），当前用的 Java 跑不动它。",
       ["打开「设置」页，把 Java 路径清空让启动器自动选择合适的 Java",
        "没装的话启动器会自动下载对应版本的 Java，下载完再启动一次"]),

    # 2. 找不到 Java
    _r([r"java\w*\s+(is\s+not|was\s+not|could\s+not\s+be)\s+(found|located|installed)",
        r"(找不到|没有找到|未找到|找不到).{0,12}(java|虚拟机)", r"java\.exe.{0,20}(not\s+exist|不存在)",
        r"no\s+java\s+runtime", r"jvm\s+not\s+found"],
       "没找到可用的 Java",
       "机器上没有能用来跑这个版本的游戏的 Java，或者设置里填的 Java 路径已经失效（比如被删掉或移动了）。",
       ["打开「设置」页，把 Java 路径清空，启动器会自动选一个能用的",
        "如果列表是空的，启动器会自动下载合适的 Java 运行时"]),

    # 3. 内存不足
    _r([r"outofmemoryerror", r"could\s+not\s+reserve\s+enough\s+space",
        r"cannot\s+(allocate|create)\s+(memory|space)", r"unable\s+to\s+allocate",
        r"内存不足", r"heap\s+space"],
       "内存不够用",
       "游戏或服务器申请内存失败了：要么分配的内存太小，要么机器上同时开的东西太多，物理内存不够。",
       ["把「设置」页里的游戏内存调小一点再试",
        "关掉浏览器、聊天软件等占内存的程序",
        "如果还报错，检查是否给 32 位 Java 分配了超过 1.5 GB 的内存"]),

    # 4. 账户验证失败
    _r([r"invalid\s+session", r"failed\s+to\s+verify\s+username", r"invalid\s+token",
        r"session\s+servers?\s+are\s+(down|unavailable)", r"会话无效", r"验证失败",
        r"invalid\s+profile", r"authentication\s+failed"],
       "账户验证没通过",
       "进带正版验证的服务器时，启动器没能证明你的登录身份：离线账户进不了正版服务器，第三方/正版登录的令牌也可能过期了。",
       ["离线账户进不了开正版验证的服务器，这是正常限制，不是故障",
        "第三方/正版账户重新登录一次，刷新令牌后再进",
        "如果是自己的服务器，可以在服务器配置里关掉 online-mode"]),

    # 5. 端口被占用
    _r([r"failed\s+to\s+bind\s+to\s+port", r"address\s+already\s+in\s+use",
        r"port\s+\d+.{0,20}(already|占用|in\s+use)", r"端口.{0,10}占用", r"bind\(\)\s+failed"],
       "端口被占用了",
       "服务器要用的端口（默认 25565）已经开着一个程序——多半是之前没退干净的服务器进程还活着。",
       ["点「停止服务器」等它完全停下来，再重新启动",
        "关不掉就在任务管理器里结束所有 java.exe 进程",
        "还占用的话换个端口：编辑服务器的 server.properties 改 server-port"]),

    # 6. EULA 未同意
    _r([r"you\s+need\s+to\s+agree\s+to\s+the\s+eula", r"eula\.txt.{0,40}(eula\s*=\s*false|agree)",
        r"需要同意.{0,6}eula"],
       "还没同意 EULA",
       "Minecraft 服务器第一次启动前必须同意官方的用户协议，现在的 eula.txt 还是 false，服务器会拒绝启动。",
       ["在「开服务器」页勾选「我已阅读并同意 EULA」再启动",
        "勾选后启动器会自动把 eula.txt 写成 true"]),

    # 7. 网络连不上
    _r([r"timed?\s*out", r"connection\s+(refused|reset|timed)", r"unknownhostexception",
        r"getaddrinfo\s+failed", r"sslhandshakeexception", r"failed\s+to\s+connect",
        r"unable\s+to\s+(resolve|connect)", r"网络(错误|异常|连接失败)", r"连接(超时|被拒绝|失败)"],
       "网络连不上",
       "启动器访问下载源、登录服务或游戏服务器时连接失败了，可能是网络波动、代理/防火墙拦截，或目标服务暂时不可用。",
       ["检查网络是否正常，断了的话重连后再试",
        "用了代理/VPN 的话先关掉，或在设置里清空代理",
        "稍等几分钟再重试，多半是对方服务临时抽风"]),

    # 8. 下载失败（HTTP 状态码）
    _r([r"http\s*[45]\d\d", r"\b40[34]\b.{0,20}(下载|forbidden|not\s+found)",
        r"下载失败", r"download\s+failed", r"checksum.{0,20}(mismatch|不匹配)", r"sha[15].{0,30}(不匹配|mismatch)"],
       "下载失败",
       "从下载源拉文件失败了：常见是下载源抽风、文件被删，或国内直连被墙导致超时/403。",
       ["直接重试一次，很多是临时抽风",
        "反复失败的话，换个网络环境（比如换热点）再试",
        "Java 下载会自动尝试 Mojang → BMCLAPI 镜像 → Adoptium 三个源"]),

    # 9. 存档/世界损坏
    _r([r"corrupt(ed)?", r"failed\s+to\s+load\s+level", r"level\.dat", r"region\s+file",
        r"存档(损坏|损坏)", r"world.{0,20}corrupt", r"exception\s+loading.{0,20}(level|world|region)"],
       "存档可能损坏了",
       "读取世界存档时出错：可能是上次游戏/服务器没保存完就被强退（断电、杀进程），损坏的那几个区块读不出来。",
       ["先别覆盖：把存档文件夹整个复制一份备份",
        "损坏的多半是个别区块，进游戏删掉报错位置附近有问题的区块就能继续玩",
        "实在不行就从备份恢复旧存档"]),

    # 10. 模组问题
    _r([r"missing\s+or\s+unsupported\s+mandatory\s+dependencies", r"the\s+mod\s+file.{0,60}missing",
        r"missing\s+mods", r"incompatible\s+mod", r"mixin\s+apply\s+failed", r"mod.{0,30}incompatible",
        r"forge.{0,30}fatal\s+errors", r"fml.{0,20}(error|fatal)", r"模组.{0,16}(缺失|冲突|不兼容|不匹配)"],
       "模组有问题",
       "模组加载失败了：通常是模组之间版本不匹配（缺依赖、装了错误前置、Forge/Fabric 版本对不上），或者模组和游戏版本不兼容。",
       ["看原始报错里提到的模组名，把它删掉或换成匹配的版本",
        "确认模组是给这个游戏版本 + 这个加载器（Forge/Fabric）做的",
        "缺依赖的把报错里列出的前置模组一起装上"]),

    # 11. 显卡/OpenGL
    _r([r"opengl", r"pixel\s+format\s+not\s+accelerated", r"glx\s+error", r"dxdiag",
        r"显卡.{0,16}(驱动|不支持|失败)", r"driver\s+version", r"hwaccel"],
       "显卡或驱动问题",
       "游戏初始化 3D 渲染失败了：显卡驱动太旧、不支持要求的 OpenGL 版本，或者机器在用核显跑 demanding 的版本。",
       ["更新显卡驱动到最新版（NVIDIA/AMD/Intel 官网下载）",
        "双显卡笔记本在显卡设置里强制游戏用独立显卡",
        "很老的核显跑不动新版本光影，可以把游戏版本降低或关掉光影"]),

    # 12. 磁盘满了
    _r([r"no\s+space\s+left\s+on\s+device", r"磁盘空间不足", r"insufficient\s+disk\s+space",
        r"disk\s+full"],
       "磁盘满了",
       "游戏目录所在硬盘没有剩余空间了，存档、下载或世界生成写不进去。",
       ["清理一下游戏目录所在盘的空间，至少留几个 GB",
        "把不玩的版本删掉也能腾出不少"]),

    # 13. 权限不足
    _r([r"access\s+is\s+denied", r"permission\s+denied", r"拒绝访问", r"没有权限",
        r"forbidden.{0,20}(write|create|delete)"],
       "权限不够",
       "启动器没有权限读写游戏目录或某个文件——常见于游戏目录放在 C 盘 Program Files 等受保护位置，或文件被别的程序占用。",
       ["把游戏目录挪到普通位置（比如 D 盘或文档目录）",
        "关掉正在占用文件的程序（杀毒软件、网盘同步）再试",
        "必要时右键「以管理员身份运行」启动器"]),

    # 14. 文件/路径不存在
    _r([r"the\s+system\s+cannot\s+find\s+the\s+file", r"系统找不到指定的文件",
        r"no\s+such\s+file\s+or\s+directory", r"路径.{0,10}(不存在|无效)", r"filenotfounderror"],
       "文件或路径不存在",
       "要用的文件（版本、Java、资源包等）不在它该在的位置：可能被删了、移动了，或者路径里有特殊字符没被正确处理。",
       ["在「版本管理」里确认版本还在，不在就重新下载",
        "检查「设置」页的 Java 路径和游戏目录是否有效",
        "游戏目录路径尽量别带中文或空格"]),

    # 15. 服务器运行中崩溃
    _r([r"exception\s+in\s+server\s+tick\s+loop", r"ticking\s+(entity|block)", r"server\s+thread.{0,20}crash",
        r"watchdog.{0,20}(timeout|timed\s+out)", r"server\s+crashed"],
       "服务器运行中崩了",
       "服务器主循环里抛了异常导致崩溃，通常是某个区块/实体数据有问题，或某个插件/模组在服务端报错。",
       ["先备份整个服务器目录再动手",
        "看报错定位到的问题区块/实体，在游戏里删掉它",
        "反复崩就看看最近加的服务端模组/插件，先移除试试"]),

    # 16. 兜底：有异常但没认出具体原因
    _r([r"\bexception\b", r"\berror\b", r"错误", r"失败", r"\bfatal\b", r"crash"],
       "没认出具体原因",
       "报错信息超出了内置规则的覆盖范围，暂时只能定位到“确实出错了”。原始报错已经放在下面，可以复制去搜索或反馈。",
       ["复制下方原始报错，到搜索引擎或社区搜一下",
        "带上报错信息到项目群里反馈，很快会有人帮你看"]),
]

# 通用兜底（连"错误"字样都没有时，比如纯业务提示"请选择版本"）
FALLBACK_NONE = None


def _hit_line(text, pattern):
    """返回第一个匹配 pattern 的行（截断到 240 字符），找不到返回 ''。"""
    for line in text.splitlines():
        if re.search(pattern, line, re.IGNORECASE):
            return line.strip()[:240]
    return ""


def analyze(text):
    """分析一段错误文本。命中返回 dict，未命中返回 None。"""
    if not text or not text.strip():
        return FALLBACK_NONE
    text = str(text)
    # 超长日志只取尾部 8000 字 + 全文兜底：关键异常通常在最后
    window = text[-8000:] if len(text) > 8000 else text
    for rule in RULES:
        for pat in rule["keys"]:
            hit = _hit_line(text, pat) or (_hit_line(window, pat) if window != text else "")
            if hit or re.search(pat, window, re.IGNORECASE):
                return {
                    "rule_id": rule["title"],
                    "title": rule["title"],
                    "reason": rule["reason"],
                    "tips": rule["tips"],
                    "hit": hit or "",
                }
    return None


def key_line(text):
    """从一段日志里挑出最像"罪魁祸首"的几行（含 Exception/ERROR 的最后几行）。"""
    lines = [ln.strip()[:240] for ln in str(text).splitlines() if ln.strip()]
    marked = [ln for ln in lines
              if re.search(r"exception|error|错误|失败|fatal|caused by|at\s+\w+", ln, re.IGNORECASE)]
    if not marked:
        return "\n".join(lines[-3:])
    return "\n".join(marked[-4:])
