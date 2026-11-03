@echo off
REM ===================================================================
REM build_mvci_exe.bat
REM Double-click ONCE to build a standalone mvci_patch.exe.
REM Needs Python + an internet connection. Keep this file in the same
REM folder as mvci_patch.py. The result is dist\mvci_patch.exe, which
REM already asks for Administrator when you run it.
REM ===================================================================

cd /d "%~dp0"

set "PY=python"
where py >nul 2>&1 && set "PY=py"

if not exist "%~dp0mvci_patch.py" (
    echo ERROR: mvci_patch.py was not found next to this file.
    echo Put build_mvci_exe.bat in the same folder as mvci_patch.py.
    echo.
    pause >nul
    exit /b 1
)

echo Installing PyInstaller (first run only, needs internet)...
%PY% -m pip install --upgrade pyinstaller || goto :err

echo.
echo Building mvci_patch.exe ...
%PY% -m PyInstaller --onefile --uac-admin --name mvci_patch "%~dp0mvci_patch.py" || goto :err

echo.
echo ===================================================================
echo Done. Your file is:  %~dp0dist\mvci_patch.exe
echo Double-click it (it will ask for Administrator), then start Techstream.
echo ===================================================================
echo.
pause >nul
exit /b

:err
echo.
echo Build FAILED. Make sure Python is installed and you have internet.
echo.
pause >nul
exit /b 1
