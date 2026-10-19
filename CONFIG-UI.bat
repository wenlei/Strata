@echo off
rem Strata's settings page: install and switch models, network access, start and stop.
setlocal
title Strata Settings
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\strata_config_ui.py %*
) else (
  python tools\strata_config_ui.py %*
)
if errorlevel 1 pause
