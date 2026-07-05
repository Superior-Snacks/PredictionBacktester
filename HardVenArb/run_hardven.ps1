<#
run_hardven.ps1 - unattended supervisor for the HardVenArb telemetry stack (Windows).

Launches the Pinnacle sidecar + the C# bot, RESTARTS either one if it crashes, and writes
each (re)start's output to a timestamped log under .\HardVenArb\logs. Ctrl+C stops both
cleanly (kills the Chrome the sidecar spawned too). Health + logout alerts still come via
Discord from the bot itself - this script only keeps the two processes alive.

Usage (from anywhere):
  powershell -ExecutionPolicy Bypass -File HardVenArb\run_hardven.ps1
  powershell -File HardVenArb\run_hardven.ps1 -Sports "tennis,baseball" -Port 8787
Stop: Ctrl+C in this window.

Notes:
  - Builds the bot ONCE in Release so restarts are instant (no rebuild lock).
  - The sidecar opens a HEADED Chrome (PINNACLE_SESSION_SOURCE=browser must be set in .env).
    The very FIRST run needs you to be logged in once so the profile is saved; after that the
    auto-login watcher re-authenticates unattended.
  - Tail a live log:  Get-Content -Wait HardVenArb\logs\bot_<stamp>.log
#>
param(
    [string]$Mode   = "telemetry",     # telemetry | dry-run | live  (telemetry for the data-collection run)
    [int]   $Port   = 8787,            # sidecar port (must match HARDVEN_SIDECAR_URL)
    [string]$Sports = "",              # optional C# sport/league filter args, comma or space separated
    [string]$Python = "python",        # python launcher (set to your venv python if needed)
    [string]$LogDir = ""               # default: HardVenArb\logs
)

$ErrorActionPreference = "Stop"
$hvDir    = $PSScriptRoot                          # ...\HardVenArb
$repoRoot = Split-Path -Parent $hvDir             # ...\PredictionBacktester (holds .env; where `dotnet run` is normally invoked)
$sideDir  = Join-Path $hvDir "sidecar"
if (-not $LogDir) { $LogDir = Join-Path $hvDir "logs" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Deliberate-stop sentinel: the bot writes this on a Discord 'close'/'end' so we DON'T restart it. Clear any
# leftover from a prior run so a stale file can't kill a fresh start.
$stopSentinel = Join-Path $hvDir ".stop_requested"
Remove-Item $stopSentinel -Force -ErrorAction SilentlyContinue

# ── 1. Build the bot once (Release) so each restart is instant ────────────────────
Write-Host "[SUPERVISOR] building HardVenArb (Release)..." -ForegroundColor Cyan
& dotnet build -c Release (Join-Path $hvDir "HardVenArb.csproj") --nologo
if ($LASTEXITCODE -ne 0) { throw "dotnet build failed - fix errors before running unattended." }
$botExe = Join-Path $hvDir "bin\Release\net10.0\HardVenArb.exe"
if (-not (Test-Path $botExe)) { throw "bot exe not found at $botExe" }

# ── process definitions ───────────────────────────────────────────────────────────
$botArgs = @("--$Mode")
if ($Sports) { $botArgs += ($Sports -split '[,\s]+' | Where-Object { $_ }) }

$procs = [ordered]@{
    sidecar = @{ Exe = $Python; Argv = @('-m','uvicorn','app:app','--port',"$Port"); Cwd = $sideDir; Proc = $null }
    bot     = @{ Exe = $botExe; Argv = $botArgs;                                       Cwd = $repoRoot; Proc = $null }
}

function Start-Logged($name, $spec) {
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $out   = Join-Path $LogDir "${name}_${stamp}.log"
    $err   = Join-Path $LogDir "${name}_${stamp}.err.log"
    Write-Host "[SUPERVISOR] starting $name -> $out" -ForegroundColor Green
    return Start-Process -FilePath $spec.Exe -ArgumentList $spec.Argv -WorkingDirectory $spec.Cwd `
        -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
}

function Stop-Tree($proc) {
    if ($proc -and -not $proc.HasExited) {
        # taskkill /T kills the child tree too (the sidecar's headed Chrome) - .NET 4.x Kill() can't.
        & taskkill /PID $proc.Id /T /F 2>$null | Out-Null
    }
}

try {
    # ── 2. Sidecar first, wait for /health, then the bot ──────────────────────────
    $procs.sidecar.Proc = Start-Logged 'sidecar' $procs.sidecar
    Write-Host "[SUPERVISOR] waiting for sidecar /health on port $Port ..." -ForegroundColor Cyan
    $healthy = $false
    for ($i = 0; $i -lt 90 -and -not $healthy; $i++) {
        Start-Sleep -Seconds 2
        if ($procs.sidecar.Proc.HasExited) { throw "sidecar exited during startup (code $($procs.sidecar.Proc.ExitCode)) - check its log." }
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$Port/health" -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -eq 200) { $healthy = $true }
        } catch { }
    }
    if (-not $healthy) { throw "sidecar never became healthy (90s) - check its log in $LogDir." }
    Write-Host "[SUPERVISOR] sidecar healthy. NOTE: log in to the Pinnacle window if this is the first run." -ForegroundColor Green

    $procs.bot.Proc = Start-Logged 'bot' $procs.bot

    # ── 3. Supervise: restart whichever process dies ──────────────────────────────
    Write-Host "[SUPERVISOR] both up. Restarting either on crash. Ctrl+C to stop both." -ForegroundColor Cyan
    while ($true) {
        Start-Sleep -Seconds 5
        foreach ($name in @('sidecar','bot')) {
            $p = $procs[$name].Proc
            if ($p -and $p.HasExited) {
                if ($name -eq 'bot' -and (Test-Path $stopSentinel)) {
                    Write-Host "[SUPERVISOR] bot requested shutdown (Discord close/end) - NOT restarting; stopping all." -ForegroundColor Cyan
                    Remove-Item $stopSentinel -Force -ErrorAction SilentlyContinue
                    return   # unwinds to the finally block -> stops the sidecar + Chrome, exits the supervisor
                }
                Write-Host "[SUPERVISOR] $name exited (code $($p.ExitCode)) - restarting in 3s" -ForegroundColor Yellow
                Start-Sleep -Seconds 3
                $procs[$name].Proc = Start-Logged $name $procs[$name]
                if ($name -eq 'sidecar') {
                    # give the fresh sidecar time to bind before the bot's next /odds poll; the bot survives the blip.
                    Start-Sleep -Seconds 5
                }
            }
        }
    }
}
finally {
    Write-Host "`n[SUPERVISOR] shutting down - stopping bot + sidecar (and Chrome)..." -ForegroundColor Cyan
    Stop-Tree $procs.bot.Proc
    Stop-Tree $procs.sidecar.Proc
    Write-Host "[SUPERVISOR] stopped." -ForegroundColor Cyan
}
