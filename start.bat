@echo off
setlocal
cd /d "%~dp0"

rem 系统路径用 %SystemRoot%：Windows 不装在 C: 的机器上也不会失效
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if errorlevel 1 (
  pause
  exit /b 1
)
echo.
echo     停止：看根目录 .lock 里的 PID，或 Get-NetTCPConnection -State Listen -LocalPort ^<端口^>
echo     端口以 config.yaml 的 gateway.port 为准（默认 8000）
"%SystemRoot%\System32\PING.EXE" -n 5 127.0.0.1 >nul
rem 纯 bat 读不了 yaml：默认开 8000，改过端口就手动打开对应地址
start "" http://127.0.0.1:8000
