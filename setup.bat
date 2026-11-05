@echo off
rem Double-click this file to install everything and create the desktop icon.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
