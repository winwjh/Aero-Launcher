# Aero Launcher（电脑版）

Aero Launcher 桌面版的完整源代码：一个用 **PySide6（Qt Widgets）+ Flask + PyInstaller** 做的 Minecraft Java 版启动器，界面为纯 Qt 原生绘制，无 WebView。

> 对应的安卓版源码在 [aero-launcher-mobile](../aero-launcher-mobile)。

## 功能一览

- 下载与启动 Minecraft（原版 / Forge / Fabric / NeoForge / Quilt），BMCLAPI 镜像加速
- 版本隔离：每个版本独立 `saves / mods / config`，与 PCL2 习惯一致；模组加载器版本「实体化」为自包含目录
- 整合包一键安装：Modrinth `.mrpack` 与 CurseForge `manifest` 两种格式，支持本地文件导入
- 资源中心：Modrinth + CurseForge 搜索下载模组 / 整合包 / 资源包 / 光影 / 数据包（CurseForge 需自备免费 API Key）
- 模组管理：图标、简介、版本检测、一键更新、禁用 / 删除（进回收目录可恢复）
- Java 自动管理：全盘扫描（注册表、常见目录、PCL2 / HMCL / 官方启动器 runtime），按「版本最接近」自动选用，缺了自动下载
- 联机开服：内置服务端下载 / 启动、UPnP 映射、P2P 连房
- 崩溃黑匣子：会话心跳记录 + 异常退出检测 + 启动时弹窗分析崩溃原因
- 资讯页、赞助页、简体 / 繁體 / English 多语言

## 目录结构

```
mc_desktop.py      桌面界面（PySide6，主入口）
server.py          后端（Flask，本地 HTTP API，所有业务逻辑）
mc_i18n.py         多语言框架（tr()）
i18n_en.py         英文词条
i18n_zh_tw.py      繁體词条
i18n_tool.py       词条覆盖率校验工具（--check）
error_analyzer.py  崩溃日志规则分析
mc_upnp.py         UPnP 端口映射
make_icon.py       生成侧栏矢量图标
image_to_icon.py   由 assets/icon-source.jpg 生成应用图标
build_win.ps1      Windows 打包脚本（PyInstaller）
build_mac.command  macOS 打包脚本
AeroLauncher.spec  PyInstaller 配置
assets/            图标与素材
```

## 构建电脑版

Windows：

```powershell
# 依赖：Python 3.11+，pip install PySide6 flask requests pyinstaller
powershell -ExecutionPolicy Bypass -File build_win.ps1
# 产物：dist/AeroLauncher.exe，可用 --selftest 自检
```

macOS：`./build_mac.command`

直接源码运行：

```bash
python mc_desktop.py
```

## 说明

- 首次运行会在程序目录/用户目录生成 `.minecraft` 与 `launcher/`（配置、缓存、日志都在里面），不写入注册表
- CurseForge API Key 在「设置」页填入，免费申请：https://console.curseforge.com/#/api-keys
- 更新检查指向 `mclauncher-update.pages.dev`（自己的发布渠道，可按需修改 `mc_desktop.py` 里的 `DEFAULT_UPDATE_URL`）

## 许可证

[GPL-3.0](LICENSE)
