@echo off
setlocal enableextensions

REM 用法：run.bat <IP> <端口> [Token] [HWiNFO日志路径]
REM 示例：run.bat 192.168.1.50 8765 your_token "C:\path\hwinfo.csv"

set "HOST_IP=%~1"
set "PORT=%~2"
set "TOKEN=%~3"
set "HWINFO_LOG=%~4"

if "%WINSYSINFO_PROVIDER%"=="" (
  set "WINSYSINFO_PROVIDER=aida64"
)

if "%HOST_IP%"=="" (
  echo 用法：run.bat ^<IP^> ^<端口^> [Token] [HWiNFO日志路径]
  echo 示例：run.bat 192.168.1.50 8765 your_token "C:\path\hwinfo.csv"
  exit /b 1
)

if "%PORT%"=="" (
  echo 用法：run.bat ^<IP^> ^<端口^> [Token] [HWiNFO日志路径]
  echo 示例：run.bat 192.168.1.50 8765 your_token "C:\path\hwinfo.csv"
  exit /b 1
)

if not "%TOKEN%"=="" (
  set "WINSYSINFO_TOKEN=%TOKEN%"
)

if not "%HWINFO_LOG%"=="" (
  set "WINSYSINFO_HWINFO_LOG=%HWINFO_LOG%"
)

python -m uvicorn agent:app --host %HOST_IP% --port %PORT%
