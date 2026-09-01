@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto nopython

python -c "import hid, requests" >nul 2>nul
if errorlevel 1 goto installdeps
goto run

:installdeps
echo Installing dependencies (hidapi, requests) - first run only...
python -m pip install -q hidapi requests
if errorlevel 1 goto pipfailed
goto run

:run
start "" pythonw "%~dp0buzz2vlc_gui.py"
exit /b 0

:nopython
echo Python was not found on PATH. Install it from https://python.org and try again.
pause
exit /b 1

:pipfailed
echo Dependency install failed. See the error above.
pause
exit /b 1
