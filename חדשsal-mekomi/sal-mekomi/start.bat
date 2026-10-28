@echo off
cd /d "%~dp0"
if not exist "%~dp0sal-mekomi-host.exe" (
  echo The local Windows component is missing: sal-mekomi-host.exe
  pause
  exit /b 1
)
"%~dp0sal-mekomi-host.exe"
if errorlevel 1 pause
