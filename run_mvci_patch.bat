@echo off
REM ===================================================================
REM run_mvci_patch.bat
REM Double-click to run the MVCI32 hotfix as Administrator.
REM It self-elevates (UAC prompt), then runs mvci_patch.py, which must
REM sit in the SAME folder as this file. No typing required.
REM ===================================================================

REM --- Self-elevate: re-launch this file as Administrator if needed ---
net session >nul 2>&1
if %errorlevel% NEQ 0 (
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

REM --- Run from this file's own folder ---
cd /d "%~dp0"

REM --- Locate Python (py launcher preferred, then python on PATH) ---
set "PY=python"
where py >nul 2>&1 && set "PY=py"

if not exist "%~dp0mvci_patch.py" (
    echo ERROR: mvci_patch.py was not found next to this file.
    echo Put run_mvci_patch.bat in the same folder as mvci_patch.py.
    echo.
    pause >nul
    exit /b 1
)

echo Starting the MVCI32 hotfix.
echo Keep THIS window open, then launch Techstream and open Data List.
echo Press Ctrl+C here to stop.
echo.

%PY% "%~dp0mvci_patch.py"

echo.
echo The hotfix has stopped. Press any key to close this window.
pause >nul
