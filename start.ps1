# 起本地服务。双击、或者在 PowerShell 里 ./start.ps1
# 服务只监听 127.0.0.1:8765，不对外网开放。

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host '没有找到 .venv。先按 README 的「第一次」跑一遍：' -ForegroundColor Yellow
    Write-Host '  python -m venv .venv'
    Write-Host '  .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
    Write-Host '（onnxruntime 还没有 Python 3.15 的轮子，venv 要用 3.12 / 3.13。）'
    exit 1
}

if (-not (Test-Path (Join-Path $PSScriptRoot '.env'))) {
    Write-Host '还没有 .env —— 复制 .env.example 填上 key 再跑。' -ForegroundColor Yellow
    Write-Host '不填也能起服务：能上传、能解析、能看原文，只有「生成讲义」会失败。'
}

$busy = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $pids = ($busy | Select-Object -ExpandProperty OwningProcess -Unique) -join ', '
    Write-Host "8765 已经被占用（PID $pids）。先停掉它，或者改 app/serve.py 里的 PORT。" -ForegroundColor Red
    exit 1
}

Write-Host 'AI Study 服务：http://127.0.0.1:8765   （Ctrl+C 结束）' -ForegroundColor Cyan
Write-Host '日志：out\server.log'
& $py -m app.serve
