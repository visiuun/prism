@echo off
setlocal
cd /d "%~dp0"
title Prism

where py >nul 2>nul && (py -3 prism.py %* & goto :done)
where python >nul 2>nul && (python prism.py %* & goto :done)

echo Python was not found. Installing Python 3.13 with winget...
winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" prism.py %*
  goto :done
)
echo.
echo Could not install Python automatically. Get it from https://www.python.org/downloads/
pause
exit /b 1

:done
if errorlevel 1 pause
