@echo off
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
if not exist config.json copy config.example.json config.json >nul
echo Installation complete. Edit config.json, then run start.bat.
pause
exit /b 0
:fail
echo Installation failed. Check Python and internet connection.
pause
exit /b 1
