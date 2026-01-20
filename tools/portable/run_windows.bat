@echo off
setlocal enabledelayedexpansion

rem Runner for the portable hc-setup-wizard.exe build (falls back to old name).
set SCRIPT_DIR=%~dp0
set BINARY=%SCRIPT_DIR%hc-setup-wizard.exe
if not exist "%BINARY%" set BINARY=%SCRIPT_DIR%hc_setup_wizard.exe

if not exist "%BINARY%" (
  echo Could not find hc_setup_wizard.exe next to this script.
  echo Expected at: %BINARY%
  exit /b 1
)

echo === HC-05 / HC-06 setup (portable) ===
set /p PORT=Serial port (e.g. COM4): 
if "%PORT%"=="" (
  echo Port is required.
  exit /b 1
)

set /p MODULE=Module type [auto/hc05/hc06, default auto]: 
if "%MODULE%"=="" set MODULE=auto

set /p NAME=Device name (optional): 
set /p PIN=PIN (4 digits, optional): 
set /p BAUD=Data-mode baud [default 9600]: 
if "%BAUD%"=="" set BAUD=9600

set /p ROLE=HC-05 role [slave/master, default slave]: 
if "%ROLE%"=="" set ROLE=slave

set CMD="!BINARY!" --port "!PORT!" --baud !BAUD! --role !ROLE! --module !MODULE!
if not "!NAME!"=="" set CMD=!CMD! --name "!NAME!"
if not "!PIN!"=="" set CMD=!CMD! --pin !PIN!

echo Running: !CMD!
!CMD!

endlocal
