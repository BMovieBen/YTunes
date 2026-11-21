@echo off
rem YTunes launcher. Options are passed through to app.py, e.g.:
rem   ytunes.bat --no-browser

if not defined IS_MINIMIZED (
    set IS_MINIMIZED=1
    start "" /min "%~dpnx0" %*
    exit /b
)

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv venv
)

rem (Re)install dependencies when requirements.txt has changed since the last install.
fc /b requirements.txt venv\requirements.installed >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    venv\Scripts\python.exe -m pip install -r requirements.txt && copy /y requirements.txt venv\requirements.installed >nul
)

rem Put the venv's yt-dlp on PATH.
set "PATH=%~dp0venv\Scripts;%PATH%"

venv\Scripts\python.exe scripts\app.py %*
