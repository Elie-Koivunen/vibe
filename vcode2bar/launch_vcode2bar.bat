@echo off
rem Start vcode2bar on Windows by double-clicking this file (opens the GUI).
rem Arguments are passed through, e.g.   launch_vcode2bar.bat --deps
rem First time only:                     py -m pip install -r requirements.txt
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 vcode2bar.py %*
) else (
    python vcode2bar.py %*
)
rem keep the window open if something went wrong, so the message can be read
if errorlevel 1 pause
