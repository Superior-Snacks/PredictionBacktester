<#
.SYNOPSIS
  Fire /slip_quote by hand against a chosen game, and report how the sidecar got there.

.DESCRIPTION
  Manual stress test for the betslip reader. The bot's own sampler is rate-limited and fires only on arb
  opens, which makes it useless for answering "what happens if the tab is closed / the row is off screen /
  the competition is collapsed". This drives the same endpoint directly, so tab state can be varied by hand
  between probes.

  READ THIS BEFORE INTERPRETING RESULTS -- the SECOND probe of the same game does not measure the same
  thing as the first. Clicking subscribes the event on the betslip channel, and the venue then KEEPS
  PUSHING prices for it. So:

      1st probe   ->  via=sport-tab / rover   (a real click, the number you care about)
      2nd probe   ->  via=cache               (no click at all, answers in ~0ms)

  and if the subscription exists but the cache has gone stale, the quote is REFUSED outright, because
  re-clicking makes the venue reply `event_already_subscribed` instead of a price. A repeat run therefore
  measures the cache, not the path. Use a DIFFERENT game for each timing sample: -List, or just re-run
  without -Selection to be handed a fresh one.

.EXAMPLE
  .\slip_probe.ps1 -List
  .\slip_probe.ps1
  .\slip_probe.ps1 -Selection "tennis:1534:2026-08-14~45149~10023046:tennis_match~all:p1"
  .\slip_probe.ps1 -Count 5 -DelaySec 10
#>
[CmdletBinding()]
param(
    # Quote this exact selection. Omit to be handed a pre-live one that has not been probed this session.
    [string] $Selection,
    # Just list pre-live candidates and exit.
    [switch] $List,
    # Probe this many DIFFERENT games in sequence (each one a genuine first-click measurement).
    [int]    $Count = 1,
    # Seconds between probes.
    [int]    $DelaySec = 5,
    [string] $Sidecar = "http://127.0.0.1:8788",
    # Resolved in the body, not here: $PSScriptRoot is empty inside a param default under Windows
    # PowerShell 5.1 invoked with -File, which silently pointed this at C:\cross_pairs_bia.json.
    [string] $PairsFile,
    # Include in-play games too (default is pre-live only, which is what the bot trades).
    [switch] $IncludeInPlay
)

$ErrorActionPreference = "Stop"

if (-not $PairsFile) {
    $root = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
    $PairsFile = Join-Path $root "cross_pairs_bia.json"
}
if (-not (Test-Path $PairsFile)) { throw "pairs file not found: $PairsFile (pass -PairsFile)" }

function Get-Candidates {
    $pairs = Get-Content $PairsFile -Raw | ConvertFrom-Json
    $toks = @()
    foreach ($p in $pairs) {
        if ($p.hardven_yes_token) { $toks += $p.hardven_yes_token }
        if ($p.hardven_no_token)  { $toks += $p.hardven_no_token }
    }
    $toks = $toks | Select-Object -Unique
    $out = @()
    # /odds in batches: the URL is the limit, not the sidecar.
    for ($i = 0; $i -lt $toks.Count; $i += 100) {
        $batch = $toks[$i..([Math]::Min($i + 99, $toks.Count - 1))] -join ','
        try {
            $r = Invoke-RestMethod "$Sidecar/odds?selections=$([uri]::EscapeDataString($batch))" -TimeoutSec 25
        } catch { continue }
        foreach ($prop in $r.selections.PSObject.Properties) {
            $s = $prop.Value
            if ($s.status -ne 'open') { continue }
            if ((-not $IncludeInPlay) -and $s.live) { continue }
            $out += $s
        }
    }
    return $out
}

if ($List) {
    $c = Get-Candidates
    Write-Host "$($c.Count) quotable candidate(s)" -ForegroundColor Cyan
    $c | Select-Object @{n='sport';e={($_.selection_id -split ':')[0]}},
                       @{n='odds';e={$_.decimal_odds}},
                       @{n='live';e={$_.live}},
                       selection_id | Sort-Object sport | Format-Table -AutoSize
    return
}

# Remember what we have already clicked THIS SESSION, so -Count hands out fresh games rather than
# re-probing one and reporting cache hits as if they were click timings.
if (-not $global:SlipProbeSeen) { $global:SlipProbeSeen = @{} }

$targets = @()
if ($Selection) {
    $targets = @($Selection)
} else {
    $pool = Get-Candidates | Where-Object { -not $global:SlipProbeSeen.ContainsKey($_.selection_id) }
    if (-not $pool) {
        Write-Host "No unprobed pre-live games left. Re-run in a new shell, or pass -Selection to force one." -ForegroundColor Yellow
        return
    }
    $targets = ($pool | Get-Random -Count ([Math]::Min($Count, @($pool).Count))) | ForEach-Object { $_.selection_id }
}

foreach ($sel in $targets) {
    $sport = ($sel -split ':')[0]
    $ekey  = ($sel -split ':')[2]
    Write-Host ""
    Write-Host "PROBE  $sel" -ForegroundColor Cyan
    Write-Host "       sport tab that SHOULD serve this: $sport    event key: $ekey"
    if ($global:SlipProbeSeen.ContainsKey($sel)) {
        Write-Host "       (already probed this session - expect via=cache, not a click)" -ForegroundColor Yellow
    }

    $sw = [Diagnostics.Stopwatch]::StartNew()
    $res = $null; $errText = ""
    try {
        $res = Invoke-RestMethod -Method Post -TimeoutSec 60 `
                  -Uri "$Sidecar/slip_quote?selection_id=$([uri]::EscapeDataString($sel))"
    } catch {
        $errText = $_.Exception.Message
    }
    $sw.Stop()
    $global:SlipProbeSeen[$sel] = $true

    if ($errText) {
        Write-Host "  HTTP FAIL  $errText  ($($sw.ElapsedMilliseconds)ms)" -ForegroundColor Red
    } elseif ($res.ok) {
        $via = if ($res.via) { $res.via } else { "?" }
        $col = if ($via -eq 'rover') { 'Yellow' } else { 'Green' }
        Write-Host ("  OK  odds={0}  implied={1}  via={2}  clicked={3}  sidecar={4}ms  wall={5}ms" -f `
                    $res.decimal_odds, $res.implied_price, $via, $res.clicked,
                    $res.elapsed_ms, $sw.ElapsedMilliseconds) -ForegroundColor $col
        if ($res.selection_label) { Write-Host "      venue calls it: '$($res.selection_label)'" }
        if ($res.from_cache)      { Write-Host "      SERVED FROM CACHE (age $($res.age_sec)s) - no click, so this is NOT a path timing" -ForegroundColor Yellow }
        if ($res.PSObject.Properties.Name -contains 'acca' -and -not $res.acca) {
            Write-Host "      [NON-ACCA EVENT] quoted anyway - this is the case the old gate refused blind" -ForegroundColor Magenta
        }
    } else {
        Write-Host "  REFUSED  ($($sw.ElapsedMilliseconds)ms)  clicked=$($res.clicked)" -ForegroundColor Red
        Write-Host "      $($res.error)"
        if ($res.diag) { Write-Host "      diag: $($res.diag | ConvertTo-Json -Compress -Depth 4)" }
    }

    if ($sel -ne $targets[-1]) { Start-Sleep -Seconds $DelaySec }
}
