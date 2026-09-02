@echo off
rem Double-click this file to launch VLC Bookmark Studio.
rem Uses the project's venv at C:\v (created there, not under this repo, to avoid a
rem Windows long-path issue when installing PySide6 -- see README.md).
cd /d "%~dp0"
set PYTHONPATH=%~dp0src
start "" "C:\v\Scripts\pythonw.exe" -m bookmark_studio
