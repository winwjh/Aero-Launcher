# Aero Launcher（手机版）

Aero Launcher 安卓版的完整源代码：基于 **Chaquopy**（在 App 内嵌 Python 3.11）跑一个 Flask 后端，原生 WebView 之外的界面逻辑全部在 Python 里，与电脑版共用同一套 `server.py` 的核心业务。

> 电脑版源码在 [aero-launcher](../aero-launcher)。

## 目录结构

```
app/src/main/java/com/wjh/mclauncher/MainActivity.java   安卓入口（WebView + 文件选择器等桥接）
app/src/main/python/backend.py    手机端专用路由（状态、前后台、存储路径）
app/src/main/python/server.py     与电脑版同源的后端（手机版裁剪）
app/src/main/assets/static/       内嵌前端页面
app/src/main/res/                 应用图标
gen_icons.py                      生成 res 里的图标
build.gradle / settings.gradle    Gradle 配置
```

## 构建

环境要求：

- Android Studio（或命令行 Gradle）+ JDK 17
- 本机安装 **Python 3.12**（3.13+ 与 Chaquopy 内置 pip 不兼容），构建时 Chaquopy 会用它给 App 内嵌依赖

```bash
# 直接出 APK（debug 签名，可自行安装）
./gradlew assembleDebug

# release 包需要自己的签名，通过环境变量提供，不要把 keystore 提交进仓库：
#   MCL_KEYSTORE / MCL_KEYSTORE_PASSWORD / MCL_KEY_ALIAS / MCL_KEY_PASSWORD
./gradlew assembleRelease
```

`local.properties`（SDK 路径）由 Android Studio 自动生成，不需要提交。

## 说明

- 数据目录在应用私有存储，游戏运行依赖设备提供的 Java 运行环境（与主流手机启动器相同的玩法）
- 网络需要能访问 Mojang / BMCLAPI；如需代理请在**本机** `gradle.properties` 配置，不要提交

## 许可证

[GPL-3.0](LICENSE)
