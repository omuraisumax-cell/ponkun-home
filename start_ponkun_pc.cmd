@echo off
setlocal
cd /d "%~dp0"

if defined PONKUN_PYTHON_EXE (
  set "PYTHON_EXE=%PONKUN_PYTHON_EXE%"
) else if exist "%~dp0.venv\Scripts\python.exe" (
  set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
) else (
  set "PYTHON_EXE=py -3.12"
)
set "PYTHONPATH=%~dp0;.venv\Lib\site-packages;%PYTHONPATH%"
set "PONKUN_HUB_URL=http://127.0.0.1:8788"
set "PONKUN_OPENAI_WEB_ENABLED=1"

if "%PYTHON_EXE%"=="py -3.12" (
  py -3.12 "%~dp0ponkun_pc.py"
) else (
  "%PYTHON_EXE%" "%~dp0ponkun_pc.py"
)
set "EXIT_CODE=%ERRORLEVEL%"
echo Ponkun PC exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
