# 启动 gateway（源码方式）。给最终用户的是根目录的 灵枢.exe，不需要 Python。
# 由 start.bat 调用，也可以直接：powershell -ExecutionPolicy Bypass -File start.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

# ---- 端口：以 config.yaml 的 gateway.port 为准（和桌面壳、gateway 同一个真源） ----
$port = 8000
$cfgPath = Join-Path $root 'config.yaml'
if (Test-Path -LiteralPath $cfgPath) {
    $inGateway = $false
    foreach ($l in Get-Content -LiteralPath $cfgPath) {
        if ($l -match '^gateway\s*:') { $inGateway = $true; continue }
        if ($inGateway -and $l -match '^\S') { break }
        if ($inGateway -and $l -match '^\s+port\s*:\s*(\d+)') { $port = [int]$Matches[1]; break }
    }
}

$busy = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "端口 $port 已被占用（可能 gateway 已在跑，也可能是别的程序）。" -ForegroundColor Yellow
    Write-Host "查占用：Get-NetTCPConnection -State Listen -LocalPort $port" -ForegroundColor Yellow
    Write-Host "换了端口就改 config.yaml 的 gateway.port。" -ForegroundColor Yellow
    exit 1
}

# ---- Python：不假设 PATH 里第一个 python 就能用（Store 占位符/没装依赖都会在这里拦住） ----
$py = $null
foreach ($name in @('python', 'python3')) {
    $c = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { $py = $c.Source; break }
}
if (-not $py) {
    Write-Host "找不到 Python。装 Python 3.11+ 并勾选 Add to PATH，或直接双击 灵枢.exe（它自带运行时）。" -ForegroundColor Red
    exit 1
}
& $py -c "import uvicorn, fastapi, yaml, httpx, PIL" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "当前 Python（$py）缺少依赖。先执行：" -ForegroundColor Red
    Write-Host "  `"$py`" -m pip install fastapi uvicorn httpx pyyaml pillow" -ForegroundColor Yellow
    exit 1
}

# 硬杀之后 .lock 会残留，不清掉会挡住启动
if (Test-Path -LiteralPath (Join-Path $root '.lock')) {
    Remove-Item -LiteralPath (Join-Path $root '.lock') -Force
}

$logs = Join-Path $root 'logs'
if (-not (Test-Path -LiteralPath $logs)) { New-Item -ItemType Directory -Path $logs | Out-Null }
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$out = Join-Path $logs "start_$stamp.out.log"

# 用 cmd 做重定向，不用 Start-Process 的 -RedirectStandard*：
# 启动器型 python 在直接重定向时日志会一直是 0 字节（实测）。cmd 的 `>` 由 shell 自己落盘。
# 外层一对引号 + 内层 "程序路径" 是 cmd /c 处理"带空格的程序路径"的标准写法。
$line = '/c ""' + $py + '" -u -m uvicorn gateway.app:app --host 127.0.0.1 --port ' + $port + ' > "' + $out + '" 2>&1"'
$cmdExe = Join-Path $env:SystemRoot 'System32\cmd.exe'
Start-Process -FilePath $cmdExe -ArgumentList $line `
    -WorkingDirectory $root -WindowStyle Hidden

Write-Host "已启动（Python: $py），端口 $port，日志（stdout+stderr 合并）：$out"
Write-Host "等几秒后打开 http://127.0.0.1:$port"
