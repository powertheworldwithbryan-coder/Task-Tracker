# Start Task Tracker
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned

$venv = Join-Path $PSScriptRoot "..\\.venv\\Scripts\\Activate.ps1"
if (Test-Path $venv) { & $venv }

pip install -r (Join-Path $PSScriptRoot "requirements.txt") -q

Write-Host "Starting Task Tracker at http://localhost:5050" -ForegroundColor Cyan
Start-Process "http://localhost:5050"
python (Join-Path $PSScriptRoot "app.py")
