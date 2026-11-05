@echo off
rem Drag and drop video file(s) onto this file.
chcp 65001 > nul
cd /d "%~dp0"
call .venv\Scripts\activate.bat
echo.
echo   1 = talk    (review / explanation)
echo   2 = craft   (keeps silent work scenes)
echo   3 = outdoor (markers only, no auto cut)
set /p M="Mode [1-3]: "
set MODE=talk
if "%M%"=="2" set MODE=craft
if "%M%"=="3" set MODE=outdoor
python roughcut.py %* --mode %MODE%
echo.
pause
