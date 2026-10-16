# One-shot setup for this project: create the venv, install the package and
# the live extras, then report readiness. Idempotent — safe to re-run.
#
# IMPORTANT: easytrader is NOT on the Tsinghua mirror that this machine's pip
# defaults to, so every install passes -i https://pypi.org/simple explicitly.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'

Write-Host "== etf-auto-trader setup ==" -ForegroundColor Cyan
Write-Host "root: $root"

# 1) venv
if (-not (Test-Path $py)) {
    Write-Host "-- creating venv" -ForegroundColor Yellow
    $basePy = 'D:\codex_proj\dev\toolchains\python\runtime\python.exe'
    if (-not (Test-Path $basePy)) { $basePy = 'python' }
    & $basePy -m venv (Join-Path $root '.venv')
}

# 2) package (editable) + test deps
Write-Host "-- installing package" -ForegroundColor Yellow
& $py -m pip install --quiet --upgrade pip -i https://pypi.org/simple
& $py -m pip install --quiet -e $root -i https://pypi.org/simple
& $py -m pip install --quiet "pytest>=8" -i https://pypi.org/simple

# 3) live extras
Write-Host "-- installing live extras (easytrader, pywin32)" -ForegroundColor Yellow
& $py -m pip install --quiet easytrader pywin32 -i https://pypi.org/simple

# 4) verify
Write-Host "-- verifying imports" -ForegroundColor Yellow
& $py -c "import etfa; print('  etfa', etfa.__version__)"
& $py -c "import easytrader; print('  easytrader ok')"
& $py -c "import pywinauto; print('  pywinauto', pywinauto.__version__)"

# 5) tests
Write-Host "-- running tests" -ForegroundColor Yellow
& $py -m pytest $root -q

Write-Host ""
Write-Host "-- doctor" -ForegroundColor Yellow
Push-Location $root
& $py -m etfa doctor
Pop-Location

Write-Host ""
Write-Host "Setup complete. Try:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\python.exe -m etfa --broker mock status"
