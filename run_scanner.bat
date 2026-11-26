@echo off
rem Launch the scanner from a source checkout (Windows).
cd /d "%~dp0"
if exist .venv\Scripts\pythonw.exe (
    start "" .venv\Scripts\pythonw.exe -m sdrscanner %*
) else (
    start "" pythonw -m sdrscanner %*
)
