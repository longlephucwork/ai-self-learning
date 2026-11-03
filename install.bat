@echo off
rem learning-harness installer for Windows: double-click this file, or run  install.bat [options]
rem It runs install.ps1, which finds Python, then install.py asks before every change.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "rc=%ERRORLEVEL%"
rem keep the window open when started by double-click (no arguments)
if "%~1"=="" pause
exit /b %rc%
