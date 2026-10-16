# One-shot onboarding once the client is installed and logged in.
#
# Does the mechanical parts a human would otherwise fumble:
#   1. locate the 同花顺-kernel order program (xiadan.exe)
#   2. write broker.exe_path into config/config.json (editing only that field)
#   3. run doctor, status --connect and probe, so you see immediately whether
#      the client is compatible
#
# It never asks for or stores a password: the client is already logged in by
# hand, which is the whole design. Read the output of `probe` at the end.
#
# Usage:
#   powershell -File scripts\onboard.ps1
#   powershell -File scripts\onboard.ps1 -ExePath "C:\国元远航\xiadan.exe"
param(
    [string]$ExePath = "",
    [string]$ConfigPath = ""
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host "venv not found at $py - run scripts\setup.ps1 first" -ForegroundColor Red
    exit 2
}
if (-not $ConfigPath) { $ConfigPath = Join-Path $root 'config\config.json' }

Write-Host "== etf-auto-trader onboarding ==" -ForegroundColor Cyan

# --- 1. find the client ---------------------------------------------------
if (-not $ExePath) {
    Write-Host "-- searching for the trading client (this can take a minute)" -ForegroundColor Yellow
    $found = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'find-client.ps1') | ConvertFrom-Json
    if (-not $found.ok) {
        Write-Host ""
        Write-Host "No trading client found. Install 国元远航版PC客户端 first:" -ForegroundColor Red
        Write-Host "  https://www.gyzq.com.cn/main/wangting/software_download/index.html  -> 电脑客户端下载"
        Write-Host "Then log in by hand, keep the window open, and re-run this script."
        exit 3
    }
    Write-Host ("found " + $found.count + " candidate(s):")
    $found.hits | ForEach-Object { Write-Host ("  " + $_.exe + "   [" + $_.source + "]") }

    $xiadan = $found.hits | Where-Object { $_.exe -match 'xiadan\.exe$' } | Select-Object -First 1
    if ($xiadan) {
        $ExePath = $xiadan.exe
    } else {
        $anyHexin = $found.hits | Where-Object { $_.exe -match 'hexin\.exe$' } | Select-Object -First 1
        if ($anyHexin) { $ExePath = $anyHexin.exe }
    }
    if (-not $ExePath) {
        $tdx = $found.hits | Where-Object { $_.exe -match 'tdxw\.exe$' } | Select-Object -First 1
        if ($tdx) {
            Write-Host "Only a 通达信 client (tdxw.exe) was found; this project does not support it." -ForegroundColor Red
            exit 4
        }
        Write-Host "Found files but no xiadan.exe/hexin.exe. Pick one manually with -ExePath." -ForegroundColor Red
        exit 4
    }
}

if (-not (Test-Path -LiteralPath $ExePath)) {
    Write-Host "exe_path does not exist: $ExePath" -ForegroundColor Red
    exit 4
}
Write-Host ("-- using exe_path = " + $ExePath) -ForegroundColor Green

# --- 2. write it into config.json (only that field) -----------------------
if (-not (Test-Path $ConfigPath)) {
    $example = Join-Path $root 'config\config.example.json'
    Copy-Item $example $ConfigPath
    Write-Host "-- created config.json from the example"
}
$json = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $json.broker) { $json | Add-Member -NotePropertyName broker -NotePropertyValue ([pscustomobject]@{}) -Force }
$json.broker.type = 'easytrader_ths'
if (-not $json.broker.clienttype) { $json.broker.clienttype = 'ths' }
$json.broker.exe_path = $ExePath
$json | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ConfigPath -Encoding UTF8
Write-Host "-- wrote broker.exe_path into config.json" -ForegroundColor Green

# --- 3. verify ------------------------------------------------------------
Push-Location $root
Write-Host ""
Write-Host "-- doctor" -ForegroundColor Yellow
& $py -m etfa doctor
Write-Host ""
Write-Host "-- status --connect   (the client must be OPEN and LOGGED IN)" -ForegroundColor Yellow
& $py -m etfa status --connect
Write-Host ""
Write-Host "-- probe   (this is the compatibility verdict)" -ForegroundColor Yellow
& $py -m etfa probe
Pop-Location

Write-Host ""
Write-Host "Read probe above:" -ForegroundColor Cyan
Write-Host "  readable=true and fields_missing=[]  -> compatible, you can trade"
Write-Host "  fields_missing not empty             -> send the `raw.columns_returned` to the author"
Write-Host "  readable=false                       -> see docs/COMPATIBILITY.md"
