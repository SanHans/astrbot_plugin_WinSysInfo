@echo off
setlocal enableextensions

REM 用法：run.bat <IP> <端口> [Token]
REM 示例：run.bat 192.168.1.50 8765 your_token

set "HOST_IP=%~1"
set "PORT=%~2"
set "TOKEN=%~3"

if "%HOST_IP%"=="" (
  echo 用法：run.bat ^<IP^> ^<端口^> [Token]
  echo 示例：run.bat 192.168.1.50 8765 your_token
  exit /b 1
)

if "%PORT%"=="" (
  echo 用法：run.bat ^<IP^> ^<端口^> [Token]
  echo 示例：run.bat 192.168.1.50 8765 your_token
  exit /b 1
)

if not "%TOKEN%"=="" (
  set "WINSYSINFO_TOKEN=%TOKEN%"
)

python -m uvicorn agent:app --host %HOST_IP% --port %PORT%
