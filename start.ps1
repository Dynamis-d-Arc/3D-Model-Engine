# One-step start on Windows: sets up whatever is missing (Python environment, packages, three.js, .env), starts the
# app and opens it in the browser. Safe to run every time; finished steps are skipped.
#   powershell -ExecutionPolicy Bypass -File start.ps1        (or double-click start.bat)
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$uv = Get-Command uv -ErrorAction SilentlyContinue

if (-not (Test-Path $py)) {
    Write-Host "Setting up the Python 3.12 environment (.venv)..."
    if ($uv) { uv venv --python 3.12 .venv } else { py -3.12 -m venv .venv }
    if (-not (Test-Path $py)) { Write-Error "Could not create .venv: install Python 3.12 (or uv) first."; exit 1 }
}

& $py -c "import twin.api.app, rapidocr_onnxruntime" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing the Python packages..."
    if ($uv) { uv pip install --python $py -e ".[dev]" } else { & $py -m pip install -e ".[dev]" }
    if ($LASTEXITCODE -ne 0) { Write-Error "Installing the packages failed (see above)."; exit 1 }
}

if (-not (Test-Path "frontend\node_modules\three")) {
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Write-Host "Installing three.js for the 3D preview..."
        Push-Location frontend; npm install; Pop-Location
    } else {
        Write-Warning "npm was not found: the 3D preview needs three.js. Install Node.js, then run 'npm install' in frontend."
    }
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env. To use the AI, put your key in TWIN_AI_API_KEY and a model in TWIN_AI_MODEL, then restart."
}

$port = 8765
$line = Select-String -Path ".env" -Pattern '^\s*TWIN_PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($line) { $port = [int]$line.Matches[0].Groups[1].Value }
$url = "http://127.0.0.1:$port/"

# open the browser as soon as the server answers (in the background, so the server keeps this window)
Start-Job -ArgumentList $url -ScriptBlock {
    param($u)
    for ($i = 0; $i -lt 60; $i++) {
        try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "$($u)api/health" | Out-Null; Start-Process $u; return } catch { Start-Sleep 1 }
    }
} | Out-Null

Write-Host "Starting Floor Plan Twin on $url (close this window to stop it)"
& $py run_server.py
