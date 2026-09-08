@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

title RedCraft

set "ROOT=%~dp0"
cd /d "%ROOT%portal" || goto fail

call :load_local_ai_env
if not defined AIPORT_BASE_URL set "AIPORT_BASE_URL=http://UT-20210713KMWD.local:8801"
call :find_python
if not defined PYTHON (
  echo ERROR: Python 3.9-3.12 not found.
  echo Install Python from https://www.python.org/downloads/
  echo Make sure to check "Add Python to PATH" during installation.
  pause
  exit /b 1
)

"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 9) and sys.version_info[:2] <= (3, 12) else 1)" >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python 3.9-3.12 required. Current:
  "%PYTHON%" --version
  pause
  exit /b 1
)

echo ========================================
echo   RedCraft
echo ========================================
echo.
echo Starting sub-apps and portal on port 9090...
echo Keep this window open. Closing it will stop all services.
echo.

:: 子应用引擎开关：infinite-canvas / rag-assistant 只有 FastAPI 实现（app.py 是占位 stub），
:: 必须设 *_ENGINE=fastapi，否则 portal 会退回 stdlib app.py 并崩溃重启。
set "SEEDANCE_ENGINE=fastapi"
set "NANO_BANANA_ENGINE=fastapi"
set "DREAMINA_ENGINE=fastapi"
set "VOLCENGINE_PORTRAIT_ENGINE=fastapi"
set "INFINITE_CANVAS_ENGINE=fastapi"
set "RAG_ASSISTANT_ENGINE=fastapi"

start "AI Portal Server" /B "%PYTHON%" "app.py"

:: Wait for portal to be ready (HTTPS on 9090, HTTP redirect on 9089)
set "PORTAL_URL=https://127.0.0.1:9090"
set "PORTAL_FALLBACK=http://127.0.0.1:9089"
echo Waiting for portal to start...
for /l %%I in (1,1,60) do (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%PORTAL_URL%/api/platform/status'; if ($r.StatusCode -eq 200) { exit 0 } } catch { }" >nul 2>nul
  if not errorlevel 1 (
    echo Portal ready.
    goto :opened
  )
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%PORTAL_FALLBACK%/api/platform/status'; if ($r.StatusCode -eq 200 -or $r.StatusCode -eq 301) { exit 0 } } catch { }" >nul 2>nul
  if not errorlevel 1 (
    echo Portal ready ^(HTTP redirect port^).
    set "PORTAL_URL=%PORTAL_FALLBACK%"
    goto :opened
  )
  timeout /t 1 >nul
)
echo WARNING: Portal did not become ready within 60 seconds.

:opened
start "" "%PORTAL_URL%"
echo Opened: %PORTAL_URL%
echo.
echo Press Ctrl+C or close this window to stop all services.

:keep_alive
timeout /t 3600 >nul
goto keep_alive

:load_local_ai_env
if not defined AIPORT_BASE_URL (
  if exist "%ROOT%config\local_ai.env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ROOT%config\local_ai.env") do (
      if /i "%%A"=="AIPORT_BASE_URL" set "AIPORT_BASE_URL=%%B"
    )
  )
)
exit /b 0

:find_python
if defined PYTHON exit /b 0
for %%C in ("py -3" "python" "python3") do (
  for /f "usebackq delims=" %%P in (`%%~C -c "import sys; print(sys.executable)" 2^>nul`) do (
    set "PYTHON=%%P"
    exit /b 0
  )
)
exit /b 0

:fail
echo.
echo Startup failed.
echo.
echo Troubleshooting:
echo   1. Make sure Python 3.9-3.12 is installed
echo   2. Run: pip install pyOpenSSL  (optional, for HTTPS)
echo   3. Check that firewall allows port 9090
echo.
pause
exit /b 1