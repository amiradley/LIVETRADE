$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$integrity = whoami /groups | Out-String
if ($integrity -match 'S-1-16-4096') {
    Write-Error 'LIVE TRADING was launched in a Low-integrity Windows context. Open a normal Windows PowerShell window under the same user/session as MT5 and run this launcher again.'
    exit 1
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "Project Python was not found at $python"
    exit 1
}

$terminal = Get-Process -Name terminal64 -ErrorAction SilentlyContinue
if (-not $terminal) {
    Write-Warning 'MT5 terminal64.exe is not currently running. The dashboard will start, but live financial values will remain unavailable.'
}

& $python -m live_trade dashboard --open-browser
