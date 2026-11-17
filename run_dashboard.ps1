$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$logPath = Join-Path $PSScriptRoot 'logs\dashboard_launcher.log'
New-Item -ItemType Directory -Path (Split-Path -Parent $logPath) -Force | Out-Null

function Write-LauncherLog([string]$Message) {
    $line = "$(Get-Date -Format o) $Message"
    Add-Content -LiteralPath $logPath -Value $line
    Write-Output $line
}

$integrity = whoami /groups | Out-String
if ($integrity -match 'S-1-16-4096') {
    Write-LauncherLog 'Refused Low-integrity launch.'
    Write-Error 'LIVE TRADING was launched in a Low-integrity Windows context. Open a normal Windows PowerShell window under the same user/session as MT5 and run this launcher again.'
    exit 1
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Write-LauncherLog "Python executable missing: $python"
    Write-Error "Project Python was not found at $python"
    exit 1
}

$url = 'http://127.0.0.1:8765/'
try {
    $existing = Invoke-WebRequest -Uri ($url + 'api/status') -UseBasicParsing -TimeoutSec 2
    if ($existing.StatusCode -eq 200) {
        Write-LauncherLog 'Existing dashboard detected; opening it instead of starting a duplicate server.'
        Start-Process $url
        exit 0
    }
} catch {
    Write-LauncherLog 'No existing dashboard detected; continuing with startup.'
}

$terminal = Get-Process -Name terminal64 -ErrorAction SilentlyContinue
if (-not $terminal) {
    Write-LauncherLog 'MT5 terminal64.exe is not currently running.'
    Write-Warning 'MT5 terminal64.exe is not currently running. The dashboard will start, but live financial values will remain unavailable.'
} else {
    Write-LauncherLog "MT5 terminal detected: PID $($terminal.Id -join ',')"
}

Write-LauncherLog "Starting dashboard with $python"
try {
    & $python -m live_trade dashboard --open-browser
    if ($LASTEXITCODE -ne 0) {
        throw "Dashboard exited with code $LASTEXITCODE"
    }
} catch {
    Write-LauncherLog "Dashboard startup failed: $($_.Exception.Message)"
    Write-Error $_
    exit 1
} finally {
    Write-LauncherLog 'Dashboard process ended.'
}
