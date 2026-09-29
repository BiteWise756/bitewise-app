# BiteWise Backend Setup Script
# Run this ONCE on a new computer before starting the server.

Write-Host "=== BiteWise Backend Setup ===" -ForegroundColor Green

# 1. Create Python virtual environment
if (-not (Test-Path ".\venv")) {
    Write-Host "Creating Python virtual environment..." -ForegroundColor Yellow
    python -m venv venv
    if (-not $?) { Write-Host "Failed to create venv. Make sure Python 3.9+ is installed." -ForegroundColor Red; exit 1 }
    Write-Host "  ✓ venv created" -ForegroundColor Green
} else {
    Write-Host "  ✓ venv already exists" -ForegroundColor Green
}

# 2. Install Python dependencies
Write-Host "Installing Python dependencies..." -ForegroundColor Yellow
& ".\venv\Scripts\pip.exe" install -r requirements.txt
if (-not $?) { Write-Host "Failed to install dependencies." -ForegroundColor Red; exit 1 }
Write-Host "  ✓ Dependencies installed" -ForegroundColor Green

# 3. Check Ollama
try {
    $ollamaVer = ollama --version 2>$null
    Write-Host "  ✓ Ollama found: $ollamaVer" -ForegroundColor Green
    $models = ollama list 2>$null
    if ($models -match "qwen2.5-coder:3b") {
        Write-Host "  ✓ Model qwen2.5-coder:3b already installed" -ForegroundColor Green
    } else {
        Write-Host "Installing Ollama model qwen2.5-coder:3b..." -ForegroundColor Yellow
        ollama pull qwen2.5-coder:3b
        Write-Host "  ✓ Model installed" -ForegroundColor Green
    }
} catch {
    Write-Host "  ⚠ Ollama not found. Install from https://ollama.com and run: ollama pull qwen2.5-coder:3b" -ForegroundColor Red
}

# 4. Check .env
if (Test-Path ".env") {
    Write-Host "  ✓ .env file found" -ForegroundColor Green
    $apiKey = Select-String -Path ".env" -Pattern "FIREBASE_API_KEY=AIza" | Select-Object -First 1
    if (-not $apiKey) {
        Write-Host "  ⚠ FIREBASE_API_KEY may be missing or invalid." -ForegroundColor Yellow
        Write-Host "    Login will work but password verification is skipped (demo mode)." -ForegroundColor Yellow
        Write-Host "    To fix: Get your Web API Key from Firebase Console > Project Settings > General." -ForegroundColor Yellow
    }
} else {
    Write-Host "  ⚠ No .env file found. Copy .env.example or configure Firebase credentials." -ForegroundColor Red
}

# 5. Check SigLIP model
$pythonExe = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) { $pythonExe = "python" }
$siglipCheck = & $pythonExe -c "import transformers, torch; print('ok')" 2>$null
if ($siglipCheck -eq "ok") {
    Write-Host "  ✓ SigLIP dependencies (transformers + torch) found" -ForegroundColor Green
    Write-Host "    Model downloads automatically from Hugging Face on first scan: google/siglip-base-patch16-224" -ForegroundColor Cyan
} else {
    Write-Host "  ⚠ SigLIP dependencies not found. Run: pip install transformers torch" -ForegroundColor Yellow
}

# 6. Check USDA dataset
$datasetPath = Join-Path (Split-Path -Parent $PSScriptRoot) "dataset" "comprehensive_foods_usda.csv"
if (Test-Path $datasetPath) {
    Write-Host "  ✓ USDA dataset found" -ForegroundColor Green
} else {
    Write-Host "  ⚠ USDA dataset not found at $datasetPath" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Next Steps ===" -ForegroundColor Cyan
Write-Host "1. Start backend:   .\venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload" -ForegroundColor White
Write-Host "2. Open frontend:   Open bitewise-app.html in your browser" -ForegroundColor White
Write-Host "3. Firestore indexes: If dashboard shows errors, deploy indexes from firestore.indexes.json" -ForegroundColor White
Write-Host "   via Firebase CLI: firebase deploy --only firestore:indexes" -ForegroundColor White
Write-Host ""
Write-Host "Setup complete!" -ForegroundColor Green
