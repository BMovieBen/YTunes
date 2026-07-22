@echo off
if not defined IS_MINIMIZED (
    set IS_MINIMIZED=1
    start "" /min "%~dpnx0" %*
    exit /b
)

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate.bat
    echo Installing dependencies...
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)

for /f "delims=" %%P in ('venv\Scripts\python.exe -c "import json;print(json.load(open('config.json')).get('web_port',8090))"') do set WEB_PORT=%%P
if not defined WEB_PORT set WEB_PORT=8090

start "" /min cmd /c "timeout /t 3 /nobreak >nul

venv\Scripts\python.exe scripts\app.py
