@echo off
rem Double-click this file to launch VLC Bookmark Studio on Windows.
rem Uses, in order: the project's own .venv, the short-path venv at C:\v (the original
rem workaround for PySide6's Windows long-path install problem -- see README.md), or
rem pythonw.exe / pyw.exe from PATH.
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" -m bookmark_studio
    goto :eof
)
if exist "C:\v\Scripts\pythonw.exe" (
    start "" "C:\v\Scripts\pythonw.exe" -m bookmark_studio
    goto :eof
)
where pyw >nul 2>nul && (
    start "" pyw -3 -m bookmark_studio
    goto :eof
)
where pythonw >nul 2>nul && (
    start "" pythonw -m bookmark_studio
    goto :eof
)
echo Python with the project's dependencies was not found. See README.md ("Setup").
pause
