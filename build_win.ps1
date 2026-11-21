# ============================================================
#  Aero Launcher Windows 打包脚本
#  用法：powershell -ExecutionPolicy Bypass -File build_win.ps1
#  作用：PyInstaller 打包（带图标）-> 复制到根目录 / 发布版 / update-server\dl\AeroLauncher-<版本>.exe
#  版本号直接读 mc_desktop.py 里的 APP_VERSION，避免手工填错
# ============================================================
param(
    [string]$Python = "C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$m = Select-String -Path "mc_desktop.py" -Pattern '^APP_VERSION = "([^"]+)"'
if (-not $m) { throw "没找到 APP_VERSION" }
$ver = $m.Matches[0].Groups[1].Value
Write-Output "==> 打包 v$ver"

# 每次都按母版重建图标（assets/icon-glyph.png -> icon.png / icon.ico），避免用到旧图标
& $Python make_icon.py
if ($LASTEXITCODE -ne 0) { throw "生成图标失败" }

& $Python -m PyInstaller --noconfirm --onefile --windowed --name AeroLauncher `
    --icon "icon.ico" --add-data "icon.ico;." `
    --add-data "assets/wechat-reward.png;assets" --add-data "assets/brand.png;assets" mc_desktop.py server.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

Copy-Item -Force "dist\AeroLauncher.exe" ".\AeroLauncher.exe"
Copy-Item -Force "dist\AeroLauncher.exe" "..\update-server\dl\AeroLauncher-$ver.exe"
Write-Output "==> 已复制到 dl\AeroLauncher-$ver.exe"

# 发布版目录里的 exe 可能正在运行，占用时只提示不中断
try {
    Copy-Item -Force "dist\AeroLauncher.exe" "..\AeroLauncher-发布版\AeroLauncher.exe"
    Write-Output "==> 已更新发布版目录"
} catch {
    Write-Warning "发布版目录 exe 正在运行，未覆盖（发布时用 -ExePath dist\AeroLauncher.exe）"
}

$hash = (Get-FileHash "dist\AeroLauncher.exe" -Algorithm SHA256).Hash.ToLower()
$size = [math]::Round((Get-Item "dist\AeroLauncher.exe").Length / 1MB, 1)
Write-Output "OK  v$ver  $size MB  sha256=$($hash.Substring(0,12))"
