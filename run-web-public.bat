@echo off
setlocal
pushd "%~dp0"

rem Read the access password from a local file (not tracked by git).
set "PWFILE=%LOCALAPPDATA%\cloudflared\.pw"
if not exist "%PWFILE%" goto :nopw

set "PASSWORD="
set /p PASSWORD=<"%PWFILE%"
if "%PASSWORD%"=="" goto :nopw

set "PYCMD="
where py >nul 2>nul
if %errorlevel%==0 set "PYCMD=py -3"
if defined PYCMD goto :run
where python >nul 2>nul
if %errorlevel%==0 set "PYCMD=python"

:run
if not defined PYCMD goto :nopython

echo.
echo  KernelSU LKM patcher - public mode (password protected)
echo  Password file: %PWFILE%
echo  Tunnel host:   see %USERPROFILE%\.cloudflared\config.yml
echo.
%PYCMD% web\server.py --password "%PASSWORD%" %*
if errorlevel 1 goto :failed

popd
exit /b 0

:failed
echo.
echo [ERROR] Server stopped with an error. See messages above.
popd
pause
exit /b 1

:nopw
echo.
echo [ERROR] Password file not found or empty:
echo         %PWFILE%
echo         Put your access password in that file (single line).
popd
pause
exit /b 1

:nopython
echo.
echo [ERROR] Python 3 was not found.
echo         Install Python 3.10+ from https://www.python.org/downloads/
popd
pause
exit /b 1
