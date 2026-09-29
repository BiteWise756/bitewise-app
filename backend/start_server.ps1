# BiteWise Backend - Start Server

# Auto-create venv if missing
if (-not (Test-Path ".\venv\Scripts\python.exe")) {
    Write-Host "Virtual environment not found. Running setup..." -ForegroundColor Yellow
    & ".\setup.ps1"
}

$env:PYTHONUNBUFFERED = "1"
Write-Host "Starting BiteWise backend on http://127.0.0.1:8000 ..." -ForegroundColor Green
& ".\venv\Scripts\python.exe" -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
