@echo off
setlocal EnableExtensions

rem Build/install may be launched from a UNC share such as:
rem \\media-pc\Downloads\GetTorrent
rem CMD cannot use a UNC path as its current directory. PUSHd automatically
rem maps it to a temporary drive letter, which also keeps PyInstaller out of
rem C:\Windows.
pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
  echo.
  echo ERROR: Could not open the GetTorrent installer folder:
  echo   %~dp0
  echo.
  pause
  exit /b 1
)

set "TD_WORK_ROOT=%CD%"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0BuildAndInstall.ps1"
set "TD_EXIT=%ERRORLEVEL%"

popd

if not "%TD_EXIT%"=="0" (
  echo.
  echo Build/install failed. See the error above.
  pause
  exit /b %TD_EXIT%
)

exit /b 0
