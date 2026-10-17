@echo off
setlocal
pushd "%~dp0"

set "PYCMD="
where py >nul 2>nul
if %errorlevel%==0 set "PYCMD=py -3"

if defined PYCMD goto :run
where python >nul 2>nul
if %errorlevel%==0 set "PYCMD=python"

:run
if not defined PYCMD goto :nopython

%PYCMD% web\server.py %*
if errorlevel 1 goto :failed

popd
exit /b 0

:failed
echo.
echo [ERROR] Web server failed to start. Please read the messages above.
popd
pause
exit /b 1

:nopython
echo.
echo [ERROR] Python 3 was not found.
echo         Install Python 3.10+ from https://www.python.org/downloads/
echo         and check "Add python.exe to PATH" during installation.
popd
pause
exit /b 1
