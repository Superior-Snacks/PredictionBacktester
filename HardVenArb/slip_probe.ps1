<#
.SYNOPSIS
  Fire /slip_quote by hand against a chosen game, in plain English, and report how the sidecar got there.

.DESCRIPTION
  Manual stress test for the betslip reader. The bot's own sampler is rate-limited and fires only on arb
  opens, which makes it useless for answering "what happens if the tab is closed / the row is off screen /
  the competition is collapsed". This drives the same endpoint directly, so tab state can be varied by hand
  between probes.

  Every probe names the game three ways so it can be checked against the screen:

      BIA      what BetInAsia's own catalog calls this fixture and the side being backed
      KALSHI   what the pair file thinks it is matched to  (a disagreement here IS a mispair)
      VENUE    the name the BETSLIP itself came back with, after the click

  The VENUE line is the one that proves the click landed on the right player. It is compared against the
  BIA catalog name automatically and flagged loudly on a mismatch -- that is the same class of error that
  produced the two-legs-on-one-side fill, so it is checked rather than eyeballed.

  READ THIS BEFORE INTERPRETING TIMINGS -- the SECOND probe of the same game does not measure the same
  thing as the first. Clicking subscribes the event on the betslip channel, and the venue then KEEPS
  PUSHING prices for it. So:

      1st probe   ->  via=sport-tab / rover   (a real click, the number you care about)
      2nd probe   ->  via=cache               (no click at all, answers in ~0ms)

  and if the subscription exists but the cache has gone stale, the quote is REFUSED outright, because
  re-clicking makes the venue reply `event_already_subscribed` instead of a price. A repeat run therefore
  measures the cache, not the path. Use a DIFFERENT game for each timing sample.

.EXAMPLE
  .\slip_probe.ps1 -List
  .\slip_probe.ps1
  .\slip_probe.ps1 -Sport tennis -Count 3
  .\slip_probe.ps1 -Selection "tennis:1534:2026-08-14~45149~10023046:tennis_match~all:p1"
#>
[CmdletBinding()]
param(
    # Quote this exact selection. Omit to be handed a pre-live one that has not been probed this session.
    [string] $Selection,
    # Just list pre-live candidates (with names) and exit.
    [switch] $List,
    # Restrict picking/listing to one sport, e.g. tennis. Tab-state tests belong on a sport that WORKS,
    # or a refusal is confounded between "the tab broke it" and "the venue will not quote it".
    [string] $Sport,
    # Probe this many DIFFERENT games in sequence (each one a genuine first-click measurement).
    [int]    $Count = 1,
    # Seconds between probes.
    [int]    $DelaySec = 5,
    [string] $Sidecar = "http://127.0.0.1:8788",
    # Resolved in the body: $PSScriptRoot is empty inside a param default under Windows PowerShell 5.1
    # invoked with -File, which silently pointed this at C:\cross_pairs_bia.json.
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

# ── name lookups ─────────────────────────────────────────────────────────────
# BIA's own catalog: selection_id -> {league, event, selection_name, start_time}
$script:Cat = @{}
function Load-Catalog {
    if ($script:Cat.Count) { return }
    try {
        $c = Invoke-RestMethod "$Sidecar/catalog" -TimeoutSec 90
        foreach ($e in $c.selections) { $script:Cat[$e.selection_id] = $e }
        Write-Host "catalog: $($script:Cat.Count) selections" -ForegroundColor DarkGray
    } catch {
        Write-Host "catalog unavailable ($($_.Exception.Message)) - names will be blank" -ForegroundColor Yellow
    }
}

# The pair file's Kalshi side: BIA token -> {label, kalshi_ticker, kalshi_outcome}
$script:Pair = @{}
function Load-Pairs {
    if ($script:Pair.Count) { return }
    foreach ($p in (Get-Content $PairsFile -Raw | ConvertFrom-Json)) {
        foreach ($t in @($p.hardven_yes_token, $p.hardven_no_token)) {
            if ($t) { $script:Pair[$t] = $p }
        }
    }
}

function Show-Names([string]$sel) {
    $c = $script:Cat[$sel]
    if ($c) {
        Write-Host "  BIA     $($c.event)" -ForegroundColor White
        Write-Host "          backing: $($c.selection_name)   |  $($c.league)   |  starts $($c.start_time)"
    } else {
        Write-Host "  BIA     <not in catalog>" -ForegroundColor Yellow
    }
    $p = $script:Pair[$sel]
    if ($p) {
        Write-Host "  KALSHI  $($p.label)"
        Write-Host "          ticker: $($p.kalshi_ticker)   outcome: $($p.kalshi_outcome)"
    } else {
        Write-Host "  KALSHI  <this token is not in the pair file>" -ForegroundColor Yellow
    }
}

# Does the betslip's own label refer to the same competitor the catalog says we backed?
function Test-LabelMatch([string]$venueLabel, [string]$catalogName) {
    if (-not $venueLabel -or -not $catalogName) { return $null }   # unknown, not a failure
    $norm = { param($s) (($s -replace '[^A-Za-z0-9 ]', ' ') -replace '\s+', ' ').Trim().ToLower() }
    $v = & $norm $venueLabel
    $c = & $norm $catalogName
    if ($v -eq $c) { return $true }
    if ($v.Contains($c) -or $c.Contains($v)) { return $true }
    # surname test: venue labels carry suffixes like "(Sets)" and often only a surname
    $last = ($c -split ' ')[-1]
    if ($last.Length -ge 3 -and $v.Contains($last)) { return $true }
    return $false
}

function Get-Candidates {
    Load-Pairs
    $toks = @()
    foreach ($t in $script:Pair.Keys) { $toks += $t }
    $toks = $toks | Select-Object -Unique
    $out = @()
    for ($i = 0; $i -lt $toks.Count; $i += 100) {
        $batch = $toks[$i..([Math]::Min($i + 99, $toks.Count - 1))] -join ','
        try {
            $r = Invoke-RestMethod "$Sidecar/odds?selections=$([uri]::EscapeDataString($batch))" -TimeoutSec 25
        } catch { continue }
        foreach ($prop in $r.selections.PSObject.Properties) {
            $s = $prop.Value
            if ($s.status -ne 'open') { continue }
            if ((-not $IncludeInPlay) -and $s.live) { continue }
            if ($Sport -and (($s.selection_id -split ':')[0] -ne $Sport)) { continue }
            $out += $s
        }
    }
    return $out
}

if ($List) {
    Load-Catalog
    $c = Get-Candidates
    Write-Host "$($c.Count) quotable candidate(s)$(if ($Sport) { " in $Sport" })" -ForegroundColor Cyan
    $c | ForEach-Object {
        $e = $script:Cat[$_.selection_id]
        [pscustomobject]@{
            sport   = ($_.selection_id -split ':')[0]
            odds    = $_.decimal_odds
            event   = if ($e) { $e.event } else { '?' }
            backing = if ($e) { $e.selection_name } else { '?' }
            id      = $_.selection_id
        }
    } | Sort-Object sport, event | Format-Table -AutoSize -Wrap
    return
}

Load-Catalog
Load-Pairs

if (-not $global:SlipProbeSeen) { $global:SlipProbeSeen = @{} }

$targets = @()
if ($Selection) {
    $targets = @($Selection)
} else {
    $pool = Get-Candidates | Where-Object { -not $global:SlipProbeSeen.ContainsKey($_.selection_id) }
    if (-not $pool) {
        Write-Host "No unprobed pre-live games left$(if ($Sport) { " in $Sport" }). Open a new shell, or pass -Selection." -ForegroundColor Yellow
        return
    }
    $targets = ($pool | Get-Random -Count ([Math]::Min($Count, @($pool).Count))) | ForEach-Object { $_.selection_id }
}

foreach ($sel in $targets) {
    $sport = ($sel -split ':')[0]
    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor DarkGray
    Show-Names $sel
    Write-Host "  board   $Sidecar -> tab '$sport'   ($sel)" -ForegroundColor DarkGray
    if ($global:SlipProbeSeen.ContainsKey($sel)) {
        Write-Host "  (already probed this session - expect via=cache, not a click)" -ForegroundColor Yellow
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

        $catName = if ($script:Cat[$sel]) { $script:Cat[$sel].selection_name } else { "" }
        if ($res.selection_label) {
            $ok = Test-LabelMatch $res.selection_label $catName
            if ($ok -eq $true) {
                Write-Host "  VENUE   '$($res.selection_label)'  == '$catName'  NAMES AGREE" -ForegroundColor Green
            } elseif ($ok -eq $false) {
                Write-Host "  VENUE   '$($res.selection_label)'  != '$catName'  *** NAME MISMATCH ***" -ForegroundColor Red
                Write-Host "          the betslip that opened is NOT the side we asked for - do not trust this quote" -ForegroundColor Red
            } else {
                Write-Host "  VENUE   '$($res.selection_label)'  (nothing to compare against)" -ForegroundColor Yellow
            }
        } else {
            Write-Host "  VENUE   <no selection_label - BetInAsia has never returned one, so the executor's" -ForegroundColor Yellow
            Write-Host "           same-side name guard is INERT on this venue. Side identification here" -ForegroundColor Yellow
            Write-Host "           rests on matching the board price instead.>" -ForegroundColor Yellow
        }
        if ($res.max_stake) {
            Write-Host ("  DEPTH   `$$([math]::Round($res.max_stake,2)) available AT {0} (real, off the slip ladder - not the assumed `$100)" -f $res.decimal_odds) -ForegroundColor Green
            if ($res.ladder) {
                $top = $res.ladder | Select-Object -First 6 | ForEach-Object { "{0} {1} `${2:N0}" -f $_.book, $_.odds, $_.stake }
                Write-Host "          ladder: $($top -join '  |  ')" -ForegroundColor DarkGray
            }
        } elseif ($res.ok) {
            Write-Host "  DEPTH   <no ladder parsed - sizing falls back to the assumed max stake>" -ForegroundColor Yellow
        }
        if ($res.slip_panel_text -and $VerbosePreference -ne 'SilentlyContinue') {
            Write-Host "  PANEL   $($res.slip_panel_text)" -ForegroundColor DarkGray
        }

        if ($res.from_cache) { Write-Host "  SERVED FROM CACHE (age $($res.age_sec)s) - no click, so this is NOT a path timing" -ForegroundColor Yellow }
        if ($res.PSObject.Properties.Name -contains 'acca' -and -not $res.acca) {
            Write-Host "  [NON-ACCA EVENT] quoted anyway - the case the old gate refused blind" -ForegroundColor Magenta
        }
    } else {
        Write-Host "  REFUSED  ($($sw.ElapsedMilliseconds)ms)  clicked=$($res.clicked)" -ForegroundColor Red
        Write-Host "      $($res.error)"
        if ($res.diag) {
            foreach ($p in $res.diag.PSObject.Properties) {
                Write-Host ("      {0,-18} {1}" -f $p.Name, ($p.Value | Out-String).Trim())
            }
        }
    }

    if ($sel -ne $targets[-1]) { Start-Sleep -Seconds $DelaySec }
}
