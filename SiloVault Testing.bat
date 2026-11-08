
@echo off
setlocal EnableExtensions
title SiloVault - Foundation Verification

cd /d "%~dp0"

set "LOG=%TEMP%\silovault_check_output.txt"

echo ============================================================
echo                 SILOVAULT FOUNDATION CHECK
echo ============================================================
echo.
echo Project: %CD%
echo Started: %DATE% %TIME%
echo.

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
    echo [OK] Project virtual environment found.
) else (
    set "PYTHON=python"
    echo [WARNING] Project virtual environment not found. Using system Python.
)

echo.
echo ============================================================
echo 1. PYTHON VERSION
echo ============================================================
%PYTHON% --version
echo.

echo ============================================================
echo 2. DEPENDENCY CHECK
echo ============================================================
%PYTHON% -m pip check
echo.

echo ============================================================
echo 3. REQUIRED PROJECT FILES
echo ============================================================

for %%F in (
    README.md
    .gitignore
    .env.example
    requirements.txt
    pyproject.toml
    backend\main.py
    backend\config.py
    backend\api\v1\router.py
    frontend\index.html
    frontend\js\api.js
    frontend\js\app.js
    docs\ARCHITECTURE.md
    docs\DATA_FLOW.md
    docs\API_CONTRACT.md
    docs\SECURITY.md
    docs\ROADMAP.md
) do (
    if exist "%%F" (
        echo [OK] %%F
    ) else (
        echo [MISSING] %%F
    )
)

echo.
echo ============================================================
echo 4. PYTHON SYNTAX CHECK
echo ============================================================

%PYTHON% -c "import ast,pathlib,sys; files=list(pathlib.Path('backend').rglob('*.py')); errors=[]; [(errors.append(str(f)+': '+str(e))) for f in files for e in []]; exec('for f in files:\n try: ast.parse(f.read_text(encoding=\"utf-8\"))\n except Exception as e: errors.append(str(f)+\": \"+str(e))'); print('Checked:',len(files),'Python files'); print('\n'.join(errors) if errors else '[OK] No syntax errors found'); sys.exit(bool(errors))"

echo.
echo ============================================================
echo 5. TEST SUITE (120 SECOND TIMEOUT)
echo ============================================================

%PYTHON% -c "import subprocess,sys; exec('try:\n r=subprocess.run([sys.executable,\"-m\",\"pytest\",\"-q\"],timeout=120)\n sys.exit(r.returncode)\nexcept subprocess.TimeoutExpired:\n print(\"[TIMEOUT] Tests exceeded 120 seconds.\")\n sys.exit(124)')"

if errorlevel 1 (
    echo [WARNING] Test suite did not complete successfully.
) else (
    echo [OK] Test suite completed successfully.
)

echo.
echo ============================================================
echo 6. BACKEND HEALTH CHECK
echo ============================================================

powershell -NoProfile -Command "try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/v1/health' -TimeoutSec 3; Write-Host '[OK] Backend responded:'; $r | Format-List } catch { Write-Host '[WARNING] Backend unavailable. Start the backend to test it.' }"

echo.
echo ============================================================
echo 7. FRONTEND SERVER CHECK
echo ============================================================

powershell -NoProfile -Command "if (Test-NetConnection -ComputerName 127.0.0.1 -Port 3000 -InformationLevel Quiet -WarningAction SilentlyContinue) { Write-Host '[OK] Port 3000 is listening.' } else { Write-Host '[WARNING] Frontend server is not running on port 3000.' }"

echo.
echo ============================================================
echo 8. GIT STATUS
echo ============================================================

git status --short
echo.

echo ============================================================
echo 9. ENVIRONMENT FILE SAFETY
echo ============================================================

git check-ignore .env
if errorlevel 1 (
    echo [WARNING] .env is not confirmed as ignored.
) else (
    echo [OK] .env is ignored by Git.
)

git ls-files .env
if not errorlevel 1 (
    echo [WARNING] Check whether .env is tracked by Git.
)

echo.
echo ============================================================
echo                 VERIFICATION FINISHED
echo ============================================================
echo.
echo Please copy the COMPLETE output and send it to ChatGPT.
echo No files were intentionally modified by this check.
echo.
pause
