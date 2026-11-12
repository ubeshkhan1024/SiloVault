
@echo off
setlocal

:: SiloVault Local Startup Script

:: 1. Resolve project directory
set "PROJECT_ROOT=%~dp0"
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"
cd /d "%PROJECT_ROOT%"

echo.
echo [SiloVault] Initializing...

:: 2. Check Python virtual environment
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python virtual environment not found.
    echo Create it using: python -m venv .venv
    pause
    exit /b 1
)

:: 3. Check Node.js
where npx.cmd >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Node.js / npx.cmd not found in PATH.
    pause
    exit /b 1
)

:: 4. Create uploads directory
if not exist "uploads" mkdir "uploads"

:: 5. Configure local storage paths
set "DATABASE_PATH=%PROJECT_ROOT%\silo_vault.db"
set "UPLOADS_DIR=%PROJECT_ROOT%\uploads"

echo [SiloVault] Database: %DATABASE_PATH%
echo [SiloVault] Uploads: %UPLOADS_DIR%

:: 6. Run database migrations
echo.
echo [SiloVault] Running database migrations...

".venv\Scripts\python.exe" -m alembic upgrade head

if errorlevel 1 (
    echo.
    echo [ERROR] Database migration failed.
    echo Servers will not be started.
    pause
    exit /b 1
)

:: 7. Start backend
echo.
echo [SiloVault] Starting backend...

start "SiloVault Backend" /D "%PROJECT_ROOT%" cmd /k ".venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000"

:: 8. Start frontend
echo [SiloVault] Starting frontend...

start "SiloVault Frontend" /D "%PROJECT_ROOT%" cmd /k "npx.cmd serve frontend -l 3000"

echo.
echo ==========================================
echo SiloVault servers are starting!
echo ==========================================
echo Backend:  http://127.0.0.1:8000
echo Frontend: http://localhost:3000
echo ==========================================
echo.
echo Keep both server windows open.
echo Press any key to close this launcher.
pause >nul

endlocal
