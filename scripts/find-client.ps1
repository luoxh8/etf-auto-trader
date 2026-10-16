# Locate the 同花顺-kernel trading client's order program (xiadan.exe).
#
# Why this exists: the client's path differs per install (and per broker-branded
# edition), and the single field a human must get right is exe_path. This walks
# the usual install roots and the registry's uninstall entries, and prints what
# it found as JSON so onboard.ps1 can consume it.
#
# It is read-only: it never runs anything from inside those directories.
$ErrorActionPreference = 'SilentlyContinue'

$results = New-Object System.Collections.Generic.List[object]

function Add-Hit([string]$path, [string]$source) {
    if ($path -and (Test-Path -LiteralPath $path)) {
        $script:results.Add([pscustomobject]@{
            exe    = (Resolve-Path -LiteralPath $path).Path
            source = $source
            size   = (Get-Item -LiteralPath $path).Length
        })
    }
}

# 1) Direct hits on the order programs, common roots and depths.
$roots = @('C:\', 'D:\', 'E:\')
$names = @('xiadan.exe', 'hexin.exe', 'tdxw.exe')
foreach ($r in $roots) {
    if (-not (Test-Path $r)) { continue }
    foreach ($n in $names) {
        Get-ChildItem -Path $r -Recurse -Include $n -Depth 5 -File |
            ForEach-Object { Add-Hit $_.FullName ("scan $r") }
    }
}

# 2) Registry uninstall entries whose name or install location looks like a client.
$keyGlobs = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
foreach ($g in $keyGlobs) {
    Get-ItemProperty $g | Where-Object {
        $_.DisplayName -match '同花顺|国元|证券|通达信|hexin'
    } | ForEach-Object {
        $loc = $_.InstallLocation
        if ($loc) {
            foreach ($n in $names) {
                Add-Hit (Join-Path $loc $n) ("registry: " + $_.DisplayName)
            }
        }
    }
}

# 3) Anything already running (the user may have the client open right now).
Get-Process | Where-Object { $_.ProcessName -match 'xiadan|hexin|tdxw' } | ForEach-Object {
    Add-Hit $_.Path 'running process'
}

$unique = $results | Sort-Object exe -Unique
$payload = [pscustomobject]@{
    ok      = ($unique.Count -gt 0)
    count   = $unique.Count
    order   = @('xiadan.exe', 'hexin.exe', 'tdxw.exe')
    note    = 'xiadan.exe/hexin.exe = 同花顺内核 (use with clienttype=ths). tdxw.exe = 通达信 (NOT supported).'
    hits    = @($unique)
}
$payload | ConvertTo-Json -Depth 4
