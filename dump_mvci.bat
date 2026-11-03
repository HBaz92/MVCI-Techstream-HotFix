@echo off
REM ===================================================================
REM dump_mvci.bat
REM Double-click to save the unpacked MVCI32.dll image to a file.
REM REQUIREMENT: Techstream must already be OPEN and CONNECTED to the
REM car (so the driver is loaded in memory) before you run this.
REM It self-elevates (UAC) and finds Python on its own.
REM ===================================================================

REM --- Self-elevate ---
net session >nul 2>&1
if %errorlevel% NEQ 0 (
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"

REM --- Locate Python (py launcher preferred, python alias is unreliable) ---
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
    echo ERROR: Python was not found. Install it from python.org and tick
    echo "Add Python to PATH" during setup, then run this again.
    echo.
    pause >nul
    exit /b 1
)

if not exist "%~dp0mvci_patch.py" (
    echo ERROR: mvci_patch.py was not found next to this file.
    echo Put dump_mvci.bat in the same folder as mvci_patch.py.
    echo.
    pause >nul
    exit /b 1
)

echo Make sure Techstream is OPEN and CONNECTED to the car first.
echo Then press any key to dump the driver image...
pause >nul

%PY% "%~dp0mvci_patch.py" --dump "%~dp0mvci_unpacked.bin"

echo.
echo If the line above said "wrote ... bytes", send me this file:
echo    %~dp0mvci_unpacked.bin
echo.
pause >nul
