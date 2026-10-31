@echo off
setlocal

for %%I in ("%~dp0..") do set "ROOT=%%~fI"
if defined PONKUN_PYTHON (
  set "PYTHON=%PONKUN_PYTHON%"
) else if exist "%ROOT%\.venv\Scripts\python.exe" (
  set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
) else (
  set "PYTHON=py -3.12"
)
cd /d "%ROOT%" || exit /b 1

rem Mobile uses its own Credential Manager entry; never inherit a Strata key.
set "PONKUN_MOBILE_AUTH_TOKEN="
set "PONKUN_STRATA_API_KEY="
set "PONKUN_STRATA_URL="
set "PONKUN_MOBILE_AUTH_REQUIRED=1"
set "PONKUN_MOBILE_HUB_URL=http://127.0.0.1:8788"
set "PONKUN_MOBILE_HUB_ONLY=1"
set "PONKUN_INFERENCE_BACKEND=strata"
set "PONKUN_WEB_SEARCH_ENABLED=1"
set "PONKUN_WEB_SEARCH_ENGINE=duckduckgo"
set "PYTHONPATH=%ROOT%;%ROOT%\.venv\Lib\site-packages;%ROOT%\.venv-gpu\Lib\site-packages"

for /f "delims=" %%S in ('"%PYTHON%" -B -m ponkun_hub.secure_credentials --status-mobile') do set "MOBILE_STATUS=%%S"
if /I not "%MOBILE_STATUS%"=="present-valid" (
  echo Mobile credential status: %MOBILE_STATUS%
  echo Mobile was not started. Register the Mobile credential first.
  exit /b 2
)

echo Mobile credential status: present-valid
echo Starting Ponkun Mobile on http://127.0.0.1:8002
echo Press Ctrl+C in this window to stop this Mobile process.
"%PYTHON%" -B -m uvicorn ponkun_mobile:app --host 127.0.0.1 --port 8002 --log-level warning
endlocal
