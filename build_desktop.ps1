# 把 AI Study 打成 Windows 客户端（目录版，双击 dist\AI Study\AI Study.exe）
#
#   powershell -ExecutionPolicy Bypass -File build_desktop.ps1
#
# 为什么是目录版（--onedir）而不是单文件（--onefile）：
# 这个程序带着 onnxruntime（OCR）和 PyMuPDF，单文件版每次启动都要先把几百 MB 解到临时目录，
# 冷启动要十几秒，杀毒软件还会盯着那个解压动作。目录版启动就是秒开，代价是分发时要连目录一起拷。

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "找不到 $py，先把虚拟环境建好（Python 3.12）" }

Write-Host '== 1/3 检查依赖' -ForegroundColor Cyan
& $py -c "import PyInstaller, webview, onnxruntime, pymupdf" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '  缺打包依赖，装一下：' -ForegroundColor Yellow
    Write-Host '  .\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt'
    throw '依赖不全'
}

Write-Host '== 2/3 清理旧产物' -ForegroundColor Cyan
foreach ($d in 'build', 'dist') { if (Test-Path $d) { Remove-Item $d -Recurse -Force } }

Write-Host '== 3/3 打包（几分钟，onnxruntime 很大）' -ForegroundColor Cyan
& $py -m PyInstaller `
    --noconfirm --clean --windowed `
    --name 'AI Study' `
    --icon 'app\desktop.ico' `
    --version-file 'version_info.txt' `
    --add-data 'app\web;app\web' `
    --collect-all webview `
    --collect-all pymupdf `
    --collect-all rapidocr_onnxruntime `
    --collect-all onnxruntime `
    --hidden-import cv2 `
    --hidden-import pyclipper `
    --hidden-import shapely `
    run_desktop.py
if ($LASTEXITCODE -ne 0) { throw '打包失败' }

$exe = Join-Path $root 'dist\AI Study\AI Study.exe'
if (-not (Test-Path $exe)) { throw "没生成 $exe" }
$size = [math]::Round(((Get-ChildItem (Join-Path $root 'dist\AI Study') -Recurse |
    Measure-Object Length -Sum).Sum / 1MB), 1)

Write-Host ''
Write-Host "好了：$exe" -ForegroundColor Green
Write-Host "整个目录 $size MB。拷给别人时连目录一起拷；数据（out/、uploads/、settings.json）会在 exe 旁边生成。" -ForegroundColor Gray
