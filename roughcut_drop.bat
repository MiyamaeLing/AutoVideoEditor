@echo off
rem Drag and drop video file(s) onto this file.
rem The files are sorted by name (C0001, C0002, ...) before they are joined.
chcp 65001 > nul
rem Keep the original command line: a name like "Q&A.mp4" (with & and no space) is cut by cmd.
setlocal EnableDelayedExpansion
set "RC_CMDLINE=!cmdcmdline!"
setlocal DisableDelayedExpansion
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
python roughcut_drop.py --mode %MODE% %*
echo.
pause
endlocal
rem When started by drag and drop, close here so that the rest of a cut name (after &) is not run as a command.
if /i not "!cmdcmdline:%~nx0=!"=="!cmdcmdline!" exit
