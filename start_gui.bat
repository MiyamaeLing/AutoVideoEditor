@echo off
rem Double-click to start the GUI.
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
  echo .venv not found. Please run the setup in README first.
  pause
  exit /b 1
)
start "" .venv\Scripts\pythonw.exe gui.py
