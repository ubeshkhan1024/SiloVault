# SiloVault M1 - Manual Browser Test Setup Script
# Run this from the repository root in PowerShell

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
Write-Host "=== SiloVault M1 - Browser Test Setup ===" -ForegroundColor Cyan
Write-Host ""

# Create isolated temp directories
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$testDir = Join-Path $env:TEMP ("silovault_browser_test_" + $timestamp)
$tempDb = Join-Path $testDir "test.db"
$tempUploads = Join-Path $testDir "uploads"
New-Item -ItemType Directory -Path $tempUploads -Force | Out-Null

Write-Host "Test directory : $testDir" -ForegroundColor Yellow
Write-Host "Temp database  : $tempDb" -ForegroundColor Yellow
Write-Host "Temp uploads   : $tempUploads" -ForegroundColor Yellow
Write-Host ""

# Run alembic upgrade head against the temp DB
Write-Host "--- Running Alembic migration ---" -ForegroundColor Cyan
$env:DATABASE_PATH = $tempDb
$env:UPLOADS_DIR = $tempUploads
& (Join-Path $repoRoot ".venv\Scripts\python.exe") -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Alembic migration failed!" -ForegroundColor Red
    exit 1
}
Write-Host "Migration complete." -ForegroundColor Green
Write-Host ""

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "MANUAL BROWSER TEST INSTRUCTIONS" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "STEP 1: Start Backend in Terminal 1:" -ForegroundColor White
Write-Host "  cd $repoRoot" -ForegroundColor Yellow
Write-Host "  `$env:DATABASE_PATH='$tempDb'" -ForegroundColor Yellow
Write-Host "  `$env:UPLOADS_DIR='$tempUploads'" -ForegroundColor Yellow
Write-Host "  .\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload" -ForegroundColor Yellow
Write-Host ""
Write-Host "STEP 2: Start Frontend in Terminal 2:" -ForegroundColor White
Write-Host "  cd $repoRoot" -ForegroundColor Yellow
Write-Host "  npx serve frontend -l 3000" -ForegroundColor Yellow
Write-Host ""
Write-Host "STEP 3: Open Browser:" -ForegroundColor White
Write-Host "  http://localhost:3000" -ForegroundColor Yellow
Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "Ready for testing. When finished, Ctrl+C both servers." -ForegroundColor Green
