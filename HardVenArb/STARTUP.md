# HardVenArb — Startup Protocol

Kalshi ↔ sportsbook cross-arb telemetry bot (current venue **Pinnacle**; bookmaker.eu supported as an
alternate). Runs **locally on Windows** (the Pinnacle side runs a managed Chrome login window on a
residential IP). Three pieces: **sidecar** (browser + odds API), **pairHard → pair_pinnacle →
pair_derivatives** (build the pair list), **the C# bot** (Kalshi WS + telemetry).

---

## 0. One-time setup (or after a reinstall)

```powershell
cd HardVenArb/sidecar
python -m pip install fastapi "uvicorn[standard]" httpx websockets playwright rapidfuzz
playwright install chromium          # fallback browser; we normally use real Chrome (channel="chrome")
```
- **Real Chrome** installed (the sidecar launches it via `channel="chrome"` to dodge bot-detection).
- **`.env`** at the repo root (or `~/.env`) with Kalshi creds: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`.
  (Bookmaker session is handled by the browser login — no keys needed there.)

## After any C# code change: rebuild (the bot locks its DLLs while running)
```powershell
# stop the running bot first (Ctrl+C), then:
dotnet build HardVenArb/HardVenArb.csproj
```

---

## 1. Start the sidecar  (Terminal 1 — leave open)

```powershell
cd HardVenArb/sidecar
$env:HARDVEN_BOOK="bookmaker"; python -m uvicorn app:app --port 8787
```
- A real Chrome window opens. **Log in / clear any Cloudflare check**, then **click into any match once**.
- Wait for `[BOOKMAKER] captured rtqname (…)` — that arms the odds calls. Leave the window + uvicorn running.
- **Keep the machine awake** (sleep drops the WS/session and can kill the bot — see Gotchas).

Sanity check (Terminal 2, optional): `Invoke-RestMethod "http://127.0.0.1:8787/catalog" | ConvertTo-Json -Depth 4 | Select-Object -First 20` → should list today's games.

### 1-PIN. Pinnacle with a managed login window (`HARDVEN_BOOK=pinnacle`, `PINNACLE_SESSION_SOURCE=browser`)

Instead of hand-scraping `x-session` into `.env`, the sidecar can open a Pinnacle window, let you log in, capture the live session, and hold it open so it doesn't expire:

```powershell
cd HardVenArb/sidecar
$env:HARDVEN_BOOK="pinnacle"; $env:PINNACLE_SESSION_SOURCE="browser"
# Catalog sports now default from sports.py (baseball 3 / tennis 33 / soccer 29), so you normally set
# NOTHING here. Optional: PINNACLE_CATALOG_LEAGUES pins extra stable league ids; PINNACLE_CATALOG_SPORTS
# only to NARROW (e.g. ="33" = tennis-only — would exclude soccer/baseball).
python -m uvicorn app:app --port 8787
```
- A Chrome window opens on pinnacle.bet. **Log in**, then **click into any sport once** so the page opens its odds WebSocket (that frame yields the WS login). The persistent profile (`.pinnacle_profile`) remembers you, so most restarts capture automatically — no re-login.
- WS-login capture runs THREE paths automatically (page.on + CDP Network + a storage probe), since Pinnacle's odds WS may be in a Web Worker `page.on` can't see. If `WS login` never flips to `YES` after browsing a sport, add **`PINNACLE_DEBUG_STORAGE=1`** — it dumps localStorage keys so we can pin the account-id/suffix; the logs also report if a worker target attached (the tell that the WS is worker-hosted).
- Watch for `[PINNACLE SESSION] captured WS login …` then the `SESSION CAPTURED — the bot is GO` banner. The feed stays idle until then; the C# bot logs `[HARDVEN] sidecar session READY` once odds flow.
- **Leave the window + uvicorn running.** The open tab holds the session (gentle auto-activity + the adapter's authed-REST keepalive guard against the inactivity logout). The bot replays the captured session over its own clean httpx/paho feed — the window only mints + holds it; it does not serve odds.
- Readiness check: `Invoke-RestMethod "http://127.0.0.1:8787/health" | ConvertTo-Json -Depth 4` → `session_ready` + a masked `session` block.
- Knobs: `PINNACLE_HEADLESS=1` (no window — only after a profile exists & login persists), `PINNACLE_CHANNEL` (default `chrome`), `PINNACLE_USER_DATA_DIR`, `PINNACLE_BROWSER_ACTIVITY_SEC` (default 200).
- **Pairing needs NO login.** `catalog()`/`pair_pinnacle.py` use Pinnacle's GUEST API (public key, no session), so you can build the pair list anytime — even before logging into the window. The browser login is only for the live odds feed. (If you ever see `[PAIR] 0 Pinnacle games in /catalog`, the sidecar isn't running or `PINNACLE_CATALOG_SPORTS`/`PINNACLE_CATALOG_LEAGUES` aren't set — it is NOT a login problem.)
- **Derivative lines (spread/total):** `python sidecar/pair_derivatives.py --write` pairs Kalshi spread/total ↔ Pinnacle handicap/over-under (MLB/KBO/ATP) into `derivative_pairs.json` (guest API, no login). The bot loads it at startup alongside `cross_pairs.json` and the sidecar resolves the `{lid}:{mid}:{type}:{points}:{side}` tokens. **Hot-reloaded alongside `cross_pairs.json` (both files, every ~15 min)** → a re-pair refreshes the derivative lines live, no restart.
- **Sports config (one place):** **`HARDVEN_SPORTS`** (comma keys, e.g. `baseball,tennis,soccer` or just `tennis`; unset = all enabled) is the single source of which sports run — it drives the schedule/lifecycle sport ids AND the pairing (moneyline + derivatives) in lockstep. Add/edit a sport in `sidecar/sports.py` (Pinnacle id, duration, Kalshi series). Everything downstream follows. **The catalog's sport discovery now defaults from `sports.py`** (no separate `PINNACLE_CATALOG_SPORTS` edit needed — set it only to *narrow* catalog scope).
- **Soccer (3-way, added 2026-07-13):** enabled by default (`sports.py` id 29). Soccer is 1X2 (home/draw/away), so each match pairs as **up to 3 separate 2-leg NO-only arbs** — Pinnacle YES (back an outcome) + Kalshi NO — for Home, Away, **and Draw**. The draw leg is synthesised in `catalog()` from the moneyline's `draw` price (Pinnacle lists only 2 participants). Series: World Cup / MLS / Liga MX / UCL-quals / USL etc. **Watch the first soccer runs for mispairs** — 3-way pairs relax the HardVen mid-sum sanity check, so a fat `EntryNetCost` on a soccer window is a mispair suspect, not free money.
- **Lifecycle (human session rhythm):** set **`PINNACLE_LIFECYCLE=1`** to have the sidecar OPEN the browser only during game windows (computed by `schedule.py` from the slate) and go DARK between them / overnight — instead of holding it open 24/7. Sport ids default from `HARDVEN_SPORTS` (override `PINNACLE_LIFECYCLE_SPORTS`). **Window shaping:** `PINNACLE_LEAD_MIN` (open this many min before a block's first game, default 15), `PINNACLE_MAX_BLOCKS` (keep only the densest N blocks, default 4; 0 = all), `PINNACLE_MIN_GAMES` (skip blocks with fewer matches, default 1). Preview with `python sidecar/schedule.py` (shows the selected blocks + match counts). Lifecycle state shows on `/health` under `session.lifecycle`. Default (unset) = browser stays open (M0/manual). Caveat: a long dark gap may log the Pinnacle session out → the next window needs a manual re-login (fine while login is manual).
- **Auto-pairing (continuous runs):** set **`HARDVEN_AUTO_PAIR=1`** to have the sidecar re-run the whole pairing pipeline (scaffold → moneyline fill → derivatives) at startup **and every `HARDVEN_PAIR_INTERVAL_MIN` minutes (default 90)** — so LIVE and late-appearing games (esp. tennis ITF/challenger + soccer, which the board adds all day) get paired within the hour instead of only at a daily run. Set `HARDVEN_PAIR_INTERVAL_MIN=0` to fall back to daily-only at `HARDVEN_PAIR_HOUR` (local hour, default 5). Account-free (Kalshi public + Pinnacle guest); respects `HARDVEN_SPORTS`; results hot-reload into the bot. Off by default (manual pairing below is unchanged). **`pairHard` is now MERGE-additive** — a re-pair carries over already-filled Pinnacle tokens for still-open games, so a frequent re-run can't drop a working (esp. live) pairing whose odds are momentarily suspended at catalog time (`--fresh` forces the old blank rebuild).
- **Currency / FX:** the Pinnacle account is **EUR**, Kalshi is **USD**. Arb *detection* is unaffected (it compares unitless probabilities — `1/odds` and the Kalshi 0–1 price), but *depth/capital/profit* mix EUR-payout units with USD. Set **`HARDVEN_FX_TO_USD`** (USD per book-unit, e.g. `1.08`) so HardVen size is converted to USD-equivalent (price is left unitless). Default `1.0` = a USD book. Update it occasionally; the rate moves <1%/day.
- **Availability:** the adapter tracks each moneyline's status — a market that goes OFFLINE/suspended (status not `open`/`null`, or the moneyline drops out of the pushed record) is marked `suspended` → the C# clears that book so no arb fires on it. Run with `PINNACLE_DEBUG_STATUS=1` to log offline/suspend transitions (and capture the exact offline status string). Note: tennis "(Games)" tabs show "moneyline unavailable" because that derivative simply has no moneyline market (only spread/total) — pairing already excludes it.

## 2. Build the pair list  (Terminal 2)

```powershell
cd HardVenArb
python pairHard.py                              # scaffold Kalshi side: the ACTIVE sports' moneylines (HARDVEN_SPORTS)
python sidecar/pair_pinnacle.py --write         # fill the Pinnacle side (team/player match, guest /catalog)
python sidecar/pair_derivatives.py --write      # spread/total → derivative_pairs.json (independent, guest API)
```
- `pairHard.py` rewrites `cross_pairs.json` but **MERGES** — filled Pinnacle tokens for still-open games are carried over; only new/expired games change → **still run `pair_pinnacle.py` right after** to fill any new blanks (`--fresh` = old blank-everything rebuild).
- `pairHard.py` scopes to the active sports (`HARDVEN_SPORTS` / `sports.py`); `--classic` = the broad built-in allowlist (every sport), `--series KX…,KX…` = specific series, `--days 20` widens the settle window.
- `pair_pinnacle.py` preview (no `--write`) first if you want to eyeball matches; check the `unmatched` summary. Add `--fuzzy` for sub-100 name variants (tagged `"fuzzy": true`).
- **Or skip all three:** set `HARDVEN_AUTO_PAIR=1` on the sidecar (see §1-PIN) and it runs this pipeline at startup + daily.

## 3. Run the bot  (Terminal 3 — or reuse Terminal 2)

```powershell
dotnet run --project HardVenArb -- --telemetry
```
Healthy startup looks like:
```
[KALSHI AUTH OK] Balance: $...
[CONFIG] N manual pair(s) loaded
[BOOKS] ... order books created
[HARDVEN] Polling sidecar http://127.0.0.1:8787/odds every 9000 ms
[KALSHI WS] Subscribed to N tickers
[TELEMETRY] --- TOP 10 CLOSEST ... ---   (near-miss list; "Open arbs: 0" is normal)
```
- Keys: **`N`** = near-miss report, **`A`** = status dashboard.
- Output (repo root): `CrossArbTelemetry_*.csv` (every arb window) + `CrossArbHedgeMonitor_*.csv` (post-open Kalshi unwind trajectory for the failed-leg hedge model — feeds `analyze_cross_arb.py` §6).
- The bot **hot-reloads `cross_pairs.json` + `derivative_pairs.json` every ~15 min**, so a re-pair (manual `pair_pinnacle.py` / `pair_derivatives.py`, or `HARDVEN_AUTO_PAIR`) is picked up without a restart.
- **Robustness (unattended runs):** each feed is supervised — a WS drop / machine sleep-wake / sidecar blip **restarts** that feed instead of shutting the bot down; only a double-Ctrl+C quits. On Windows the bot also suppresses system sleep while running (`HARDVEN_KEEP_AWAKE=0` to disable).

---

## Staged testing — bring it up one piece at a time

Test **additively**: start from the baseline (everything optional OFF), verify one stage, then flip **exactly one** toggle and verify the next. Every feature has an independent switch, so a failure is unambiguous. Baseline = no lifecycle, no auto-pair (`PINNACLE_LIFECYCLE` and `HARDVEN_AUTO_PAIR` unset); default sports; hedge + keep-awake on (harmless).

| # | Stage | Toggle | Off / baseline |
|---|-------|--------|----------------|
| 1 | Sport config | `HARDVEN_SPORTS` | unset = all enabled |
| 2 | Schedule / blocks | `--lead/--max-blocks/--min-games` (preview) → `PINNACLE_LEAD_MIN/MAX_BLOCKS/MIN_GAMES` | defaults 15 / 4 / 1 |
| 3 | Pairing | manual cmds → `HARDVEN_AUTO_PAIR=1` | manual |
| 4 | Odds + telemetry | `--telemetry` (the bot) | — |
| 5 | Hedge tape | `HARDVEN_HEDGE_MONITOR_SECS` | `0` = off |
| 6 | Lifecycle | `PINNACLE_LIFECYCLE=1` | unset = held open |
| 7 | Organic gestures | `PINNACLE_ORGANIC` | `0` = off |
| 8 | Robustness | `HARDVEN_KEEP_AWAKE` (supervisor always on) | `0` = keep-awake off |

**Stage 1 — Sport config** (no bot, no login).
```powershell
python sidecar/sports.py                      # then:  $env:HARDVEN_SPORTS="tennis"; python sidecar/sports.py
```
- ✅ Default → `active: ['baseball', 'tennis', 'soccer']  pinnacle ids: [3, 33, 29]`; `=tennis` → `[33]`, derivatives only `KXATPG*`. (Soccer = 3-way, no derivatives yet.)
- ❌ `unknown key(s) [...]` → typo in `HARDVEN_SPORTS`; empty list → check the `CATALOG` in `sports.py`.

**Stage 2 — Schedule / block selection** (no bot, no login; guest API).
```powershell
python sidecar/schedule.py                     # vary: --max-blocks 2  --min-games 3  --lead 30
```
- ✅ Prints the blocks in **local** time with per-block match counts + `selected densest X of Y; dropped Z`, then a `NOW: OPEN/CLOSED` line. Changing the knobs visibly changes the selection.
- ❌ `0 games` → guest API/network down or wrong sports; blocks look merged/split wrong → tune `--min-gap` / `--lead`.

**Stage 3 — Pairing** (sidecar must be running for the moneyline fill's `/catalog`).
```powershell
python pairHard.py ; python sidecar/pair_pinnacle.py --write ; python sidecar/pair_derivatives.py --write
```
- ✅ `cross_pairs.json` entries have **non-blank** `hardven_yes_token`/`hardven_no_token`; `[OK] wrote N …`; `derivative_pairs.json` has lines. Then flip `HARDVEN_AUTO_PAIR=1` on the sidecar and watch `[PAIR SCHED] startup pairing run … complete`.
- ❌ Tokens still blank → `pair_pinnacle` couldn't reach `/catalog` (sidecar down) or no name match (read the `unmatched` summary; try `--fuzzy`); `scaffold 0 kept` → no in-window Kalshi markets (`--days` wider).

**Stage 4 — Odds + bot telemetry** (the core M0; needs the sidecar serving odds — logged-in post-KYC, or guest pre-match).
```powershell
dotnet run --project HardVenArb -- --telemetry
```
- ✅ `[KALSHI AUTH OK]`, `N manual pair(s) loaded`, `Kalshi books N/N | HardVen books N/N`, near-miss lines (`A` = dashboard, `N` = near-miss). `Open arbs: 0` while quiet is normal.
- ❌ `HardVen books 0/N` → sidecar not serving odds / session not ready (check `/health` `session_ready`); `Kalshi books 0/N` → Kalshi WS/auth.

**Stage 5 — Hedge tape.** Toggle `HARDVEN_HEDGE_MONITOR_SECS` (`0` = off, `30` = on).
- ✅ With it on: a `CrossArbHedgeMonitor_*.csv` appears; after a window opens it gains rows (`OffsetMs`, `KalshiUnwindBid`); `python analyze_cross_arb.py` prints a §6 NET-EV board. With `=0`: **no** hedge CSV is written.
- ❌ Hedge CSV present but empty → no arb window has opened yet (fine if quiet); analyzer §6 says "run the updated bot" → that CSV predates the within/hedge columns.

**Stage 6 — Lifecycle.** Toggle `PINNACLE_LIFECYCLE=1` on the sidecar.
- ✅ `[PINNACLE LIFECYCLE] N work window(s) planned`; the browser **OPENs** ~`PINNACLE_LEAD_MIN` before a block and `window CLOSED → dark` after; `/health` → `session.lifecycle` shows OPEN/dark + seconds. Cross-check against `python sidecar/schedule.py`.
- ❌ Never opens → no windows in the horizon (Stage 2) or wrong sports; open 24/7 → `PINNACLE_LIFECYCLE` isn't `1`.

**Stage 7 — Organic gestures.** Toggle `PINNACLE_ORGANIC` (default on; `0` = off). Watch the tab (headful / VNC).
- ✅ Irregular idle gaps, **curved** mouse moves, multi-notch scrolls; no error spam. With `=0`: no movement, log says `organic activity OFF`.
- ❌ `[error]` in the loop → a page/selector hiccup (best-effort — must never crash the loop; if it does, capture it).

**Stage 8 — Robustness.** Supervisor is **always on**; test it by disturbing a feed. Keep-awake toggle `HARDVEN_KEEP_AWAKE` (default on, Windows).
- ✅ Restart the sidecar (Ctrl+C + relaunch) while the bot runs → HardVen books go stale then **recover**, and the bot **stays up** (no `[SHUTDOWN]`). A real feed drop logs `[SUPERVISOR] <feed> feed … restarting (#n) in Xs`. Keep-awake logs `[KEEP-AWAKE] Suppressing system sleep`.
- ❌ Bot prints `[SHUTDOWN]` on a feed drop → the old self-shutdown; should not happen now — capture the lines above it.

---

## Daily refresh (new games appear, old ones settle)

Just re-run step 2 — league discovery is dynamic (`GetLeagues`), so no id-wrangling:
```powershell
python pairHard.py ; python sidecar/pair_pinnacle.py --write ; python sidecar/pair_derivatives.py --write
```
Or set `HARDVEN_AUTO_PAIR=1` on the sidecar and it does this at startup + every `HARDVEN_PAIR_INTERVAL_MIN` (default 90 min; §1-PIN) — no manual refresh, and live/late games get picked up intraday.

---

## Running unattended on a Google Cloud Ubuntu server (step by step)

The C# bot is portable (.NET 10, Kalshi side is just HTTP/WS); the **sidecar** needs real Chrome under a
virtual display (Xvfb). The bookmaker session is **IP + fingerprint bound**, so the server logs in on its
OWN IP (home cookies won't validate). **A GCP IP is a datacenter IP — Cloudflare/bookmaker.eu are harsher
on those**, so Step 1 is a go/no-go test; don't do the rest until it passes.

**VM:** Ubuntu 22.04/24.04 LTS, ≥2 vCPU, **≥4 GB RAM** (Chrome is hungry), ≥20 GB disk. Everything runs on
`localhost` — do **NOT** open ports 8787 or 5900 to the internet; reach them via SSH tunnels only.

### 1. Go/no-go: can the server even log in?  (do this FIRST)
```bash
sudo apt update && sudo apt install -y xvfb x11vnc python3-pip git tmux
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb
python3 -m pip install playwright && python3 -m playwright install-deps
Xvfb :99 -screen 0 1400x900x24 &      # virtual display
export DISPLAY=:99
x11vnc -display :99 -localhost -nopw -forever -bg     # VNC server (localhost only)
google-chrome --user-data-dir=$HOME/.bmtest https://be.bookmaker.eu &
```
From your **local machine**: `ssh -L 5900:localhost:5900 USER@SERVER_IP` then point a VNC viewer at
`localhost:5900`. Try to load the site and log in.
- **Loads + logs in** → proceed. **Perma-challenge / blocked / "verify your location"** → bookmaker.eu is
  rejecting the GCP IP; stop here and use the split setup (sidecar at home, bot on server) instead.

### 2. Install .NET 10 SDK
```bash
wget https://dot.net/v1/dotnet-install.sh -O dotnet-install.sh && chmod +x dotnet-install.sh
./dotnet-install.sh --channel 10.0
echo 'export PATH="$HOME/.dotnet:$PATH"' >> ~/.bashrc && export PATH="$HOME/.dotnet:$PATH"
dotnet --version
```

### 3. Get the code + credentials
```bash
git clone <your-repo-url> PredictionBacktester && cd PredictionBacktester
# Kalshi creds — scp your RSA key over securely (NOT committed), then point .env at it:
#   (from local)  scp kalshi_key.pem USER@SERVER_IP:~/PredictionBacktester/
chmod 600 kalshi_key.pem
cat > .env <<'EOF'
KALSHI_API_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=/home/USER/PredictionBacktester/kalshi_key.pem
BOOKMAKER_AUTOLOGIN=1
# A headless server has no desktop keyring, so Chrome can't autofill the saved password — set creds
# explicitly and the sidecar types them straight into the login form on re-login (this is the reliable
# auto-relogin path on a server; AUTOLOGIN autofill is desktop-only):
BOOKMAKER_USERNAME=...
BOOKMAKER_PASSWORD=...
EOF
```

### 4. Python sidecar deps
```bash
cd HardVenArb/sidecar
python3 -m pip install fastapi "uvicorn[standard]" httpx websockets playwright rapidfuzz requests
python3 -m playwright install chromium          # fallback browser only; we use channel="chrome"
```

### 5. Build the bot
```bash
cd ~/PredictionBacktester && dotnet build HardVenArb/HardVenArb.csproj   # expect 0 errors
```

### 6. One-time real login (seed `.bookmaker_profile` on the server)
```bash
export DISPLAY=:99                               # the Xvfb from step 1 (restart it if you rebooted)
cd ~/PredictionBacktester/HardVenArb/sidecar
HARDVEN_BOOK=bookmaker python3 -m uvicorn app:app --port 8787
```
VNC in (SSH tunnel from step 1). A real Chrome opens → **log in**, clear any Cloudflare check, click
into one match (seeds `rtqname`). Wait for `[BOOKMAKER] captured rtqname …`. Ctrl-C — the profile now
remembers the **session cookies**. (It can NOT remember the *password* for autofill — a headless server
has no keyring — so set `BOOKMAKER_USERNAME`/`BOOKMAKER_PASSWORD` (§3) for automatic re-login.)

### 7. Run unattended in tmux
```bash
tmux new -s hardven
# pane 1 — sidecar under Xvfb (auto-allocates a display; profile carries the login):
cd ~/PredictionBacktester/HardVenArb/sidecar
HARDVEN_BOOK=bookmaker BOOKMAKER_AUTOLOGIN=1 xvfb-run -a python3 -m uvicorn app:app --port 8787
# Ctrl-b % to split → pane 2 — pairs + bot:
cd ~/PredictionBacktester/HardVenArb
python3 pairHard.py && python3 sidecar/pair_auto.py --fuzzy --write
dotnet run --project HardVenArb -- --telemetry          # add --exclude tennis etc. to taste
# Ctrl-b d to detach. Survives logout/SSH close. `tmux attach -t hardven` to check on it.
```

GCP VMs don't sleep, so the Windows "keep awake" gotcha is gone. The sidecar keeps the session warm
(mouse-activity + "Stay connected") and auto-recovers Cloudflare; an *interactive* captcha still needs a
VNC nudge. CSV telemetry lands in `~/PredictionBacktester/CrossArbTelemetry_*.csv` — `scp` it home to analyze.

### Is the VM throttling the bot/Chrome?  (e2-medium is shared-core — worth checking)
```bash
python3 HardVenArb/sidecar/server_health.py            # one-shot: CPU steal, load, RAM/swap, sidecar latency
python3 HardVenArb/sidecar/server_health.py --watch 10 # live (run in a 3rd tmux pane)
```
Reports a verdict (✅ OK / ⚠️ WARN). The headline number is **CPU steal %** — sustained > ~10% means the
hypervisor is throttling this VM (burst credits exhausted). Also flags low free RAM, active swap, and a
slow sidecar `/health`. When the bot logs `STALE` quotes or a `poll timeout`, run this to see whether the
cause is throttling/swapping (server) vs a network blip (not the server).

## Tuning knobs (env)

| Var | Default | What |
|-----|---------|------|
| `HARDVEN_QUOTE_MAX_AGE_MS` | `30000` | **Bot:** a HardVen quote older than this (sidecar `ts`) is treated as STALE → its book is cleared so no phantom arb can fire after a session drop. |
| `HARDVEN_POLL_MS` | `3000` | **Bot:** how often the bot pulls the latest cached book from the sidecar. `/odds` is an instant cache read now, so this is cheap. (Quote *freshness* is set by `BOOKMAKER_REFRESH_SEC`, not this.) |
| `HARDVEN_SPORTS` | all enabled | **Sidecar:** the active sports (comma keys, e.g. `baseball,tennis`) — one source for schedule/lifecycle + pairing. Catalog in `sidecar/sports.py`. |
| `HARDVEN_FX_TO_USD` | `1.0` | **Bot + Sidecar:** USD per HardVen book-unit (EUR→USD). Applied to feed depth AND the wallet balance (`GetUsdcBalanceAsync` → sidecar `/balance` × FX). `~1.08` for the EUR account; price is left unitless. |
| `HARDVEN_BET_ENABLE` | unset | **Sidecar (real money):** `1` = allow real bet placement. **Default OFF → `place_bet()` PREVIEWS only** (logs the intended bet, places nothing). Placement itself goes through the browser UI and is DEFERRED — even with this on, `_place_via_ui()` raises until built. |
| `HARDVEN_MAX_STAKE` | `10` | **Sidecar (real money):** hard per-bet stake cap (account currency); a stake above it is rejected outright. Never overridden. |
| `HARDVEN_HEDGE_MONITOR_SECS` | `30` | **Bot:** seconds to sample the Kalshi unwind price into `CrossArbHedgeMonitor_*.csv` (the failed-leg hedge tape). **`0` = OFF** (no hedge CSV — clean baseline). |
| `HARDVEN_KEEP_AWAKE` | `1` | **Bot:** Windows-only — suppress system sleep while running (the unattended-laptop fix). `0` to disable. |
| `PINNACLE_LIFECYCLE` | unset | **Sidecar:** `1` = open the browser only during game windows, dark between (human rhythm). Unset = held open (M0/manual). |
| `PINNACLE_LEAD_MIN` | `15` | **Sidecar:** open this many minutes before a block's first game (lifecycle). |
| `PINNACLE_MAX_BLOCKS` | `4` | **Sidecar:** keep only the densest N game-blocks per day (0 = all). The "3–4 blocks where the most matches happen." |
| `PINNACLE_MIN_GAMES` | `1` | **Sidecar:** skip a block with fewer than this many matches (no login for one isolated game). Use `3` for session mode. |
| `PINNACLE_SESSION_HOURS` | `0` | **Sidecar:** `>0` (try `2`) = DISCRETE ~Nh sessions by game-START density instead of gap-merged windows — the right model for CONTINUOUS sports (tennis) where gap-merge collapses the whole day into one block. Gives the 3–4 fixed ~2h sessions. Preview: `python schedule.py --session-hours 2 --min-games 3`. |
| `PINNACLE_SESSION_TODAY_ONLY` | on | **Sidecar:** plan blocks for the CURRENT LOCAL DAY only — tomorrow's slate is still filling in, so it shouldn't compete for today's densest-N block budget. Recompute rolls to the new day hourly. `0` = use the full 36h horizon. Preview: `python schedule.py --session-hours 2 --min-games 3 --today-only`. |
| `PINNACLE_RELOGIN_MIN` | `20` | **Sidecar:** reload the page every N min to re-mint the session (< the ~30-min idle logout). The reliable keepalive. `0` = off. |
| `PINNACLE_MANUAL_PLAN` | unset | **Sidecar (TEST):** path to a hand-written JSON plan that OVERRIDES the game slate, so you can verify the lifecycle open→wait→close→wait→open cycle in minutes. Entries `{"open_in": <min from start>, "close_in": <min>}` (relative) or `{"open": ISO, "close": ISO}`. Example: `sidecar/manual_plan.json`. Needs `PINNACLE_LIFECYCLE=1`. |
| `PINNACLE_AUTO_LOGIN` | on | **Sidecar:** on (re)open, if the page is sitting on a login form whose password field is already AUTOFILLED by the saved Chrome profile, press Enter to re-authenticate unattended (button-click fallback). No credentials are stored — the profile fills them; we only submit. No-op on an empty form (first-time manual setup) and on normal pages. `0` = disable (manual login). |
| `PINNACLE_LOGIN_CHECK_SEC` | `8` | **Sidecar:** how often the auto-login watcher looks for a login form. |
| `PINNACLE_LOGIN_SUBMIT_COOLDOWN` | `30` | **Sidecar:** minimum seconds between auto-login submit attempts (avoids hammering the form). |
| `PINNACLE_LOGIN_HEALTHY_GRACE` | `180` | **Sidecar:** don't re-login while the session is LIVE — if authed API traffic was seen within this many seconds, a visible login form is a stray/autofilled widget (not a logout), so submitting it is skipped. Prevents the post-capture re-login churn (a redundant submit rotates the fresh session → guest-redirect cascade + WS auth-reject storm). Re-login still fires once authed traffic goes silent this long (a genuine logout / dark-gap reopen). |
| `DISCORD_HEARTBEAT_MIN` | `30` | **Bot:** cadence of the Discord `💓` heartbeat (uptime / books / WS / arbsLogged). Startup ping + session-lost / feed-down edge alerts fire regardless. Needs `DISCORD_WEBHOOK_URL`. Works in **every** mode incl. `--telemetry`. |
| `DISCORD_DOWN_GRACE_SEC` | `90` | **Bot:** a signal (session / feed) must be down this long before a 🔴 alert — absorbs startup warm-up + scheduled-reopen re-capture gaps, so it alerts only on a genuinely STUCK problem. |
| `DISCORD_BOT_TOKEN` | unset | **Bot:** a Discord BOT token (not the webhook) so the bot can READ the channel for commands. Enables remote `status` / `close` / `end`. Requires the bot in your server with View Channel + Read Message History and the **MESSAGE CONTENT INTENT** enabled. Paired with `DISCORD_CHANNEL_ID`. |
| `DISCORD_CHANNEL_ID` | unset | **Bot:** the #alerts channel id the command listener polls (right-click the channel → Copy Channel ID; needs Developer Mode). Both this and `DISCORD_BOT_TOKEN` must be set to enable commands. |
| `HARDVEN_PYTHON` | `python` | **Bot:** python executable used to run `analyze_cross_arb.py --summary` for the `status` reply. Set to a venv python if `python` isn't on PATH. |
| `HARDVEN_BOOK_FRESH_SEC` | `120` | **Bot:** a book counts as LIVE in the heartbeat/status if it updated within this many seconds (and isn't resolved). The count is now "currently live" (fresh + not dead), not the old "ever-received" latch that read a misleading constant. |
| `PINNACLE_DEBUG_WS` | off | **Sidecar:** `1` logs EVERY WS odds update — grows the log ~18 MB/day (90 MB over a week). **Keep it UNSET for production/long runs** (a startup warning fires if it's on). Only for proving live=WS during debugging. |
| `PINNACLE_ORGANIC` | on | **Sidecar:** `0` = disable the human-like mouse/scroll activity in the login window (the session still holds via the REST keepalive). |
| `PINNACLE_DEDICATED_WS` | `1` | **Sidecar:** `0` = do NOT open the sidecar's own dedicated paho odds WS (the second, non-browser MQTT-over-WSS connection). Session/catalog/pairing still run; odds must then come from another source (the browser-window WS reader). For the window-WS-reading bring-up, and the eventual drop of the extra connection. `1` (default) = normal. |
| `PINNACLE_WS_READ_PROBE` | unset | **Sidecar (diagnostic):** `1` = feasibility probe for reading odds off the PAGE's OWN WS — counts received MQTT PUBLISH (odds) frames + their leagues via CDP and logs a `[WS-READ-PROBE]` verdict every 15s (GREEN = odds flow / AMBER = frames but no odds / RED = no frames = worker-hidden). The go/no-go before the window-per-sport rebuild. Browse a sport with live odds so its WS opens. Off = no probe. |
| `PINNACLE_WINDOW_WS_READ` | unset | **Sidecar:** `1` = the browser-window WS READER — parse odds off the page's OWN WS (CDP, stream-reassembled MQTT) and feed the SAME cache path as the paho feed (`_apply`). Pair with `PINNACLE_DEDICATED_WS=0` so the browser WS is the only odds source (no second connection). **Coverage follows the open tabs** — CDP is now armed PER TAB (primary page + every tab you open), so all tabs' odds merge into the one reader. A league page is board-scoped and subscriptions don't accumulate, so full-slate coverage means one league per tab. Logs `[WS-READ]` counts every 15s (`active(<45s)` lists the leagues currently streaming across ALL tabs). |
| `PINNACLE_TAB_TEST` | unset | **Sidecar (diagnostic):** comma-separated Pinnacle league URLs — opens each in its OWN tab at startup to test background-tab WS survival. Only one tab is foregrounded, so the rest are background; if their league ids stay in the `[WS-READ] active(<45s)` list while a different tab is focused, background-tab odds sockets survive (the anti-throttle launch flags hold them) → one-league-per-tab coverage is viable. Off = no test tabs. |
| `HARDVEN_TAB_MANAGER` | unset | **Sidecar:** `1` = the LEAGUE TAB MANAGER — automatically opens one browser tab per PAIRED league the main board isn't already feeding, so the reader covers the whole paired slate (the board alone is ~25%). Needs `PINNACLE_WINDOW_WS_READ=1` + a browser session; IGNORED under `PINNACLE_LIFECYCLE` (the browser cycles per block). Reads `hardven_league_url` from cross_pairs.json (written by pair_pinnacle) and the reader's delivered matchups; opens/closes tabs as leagues appear/settle. Logs `[TAB-MGR]`. **3-tier coverage:** the main page (featured board) + `HARDVEN_TAB_MAX` dedicated gap tabs + one roving tail tab (below). |
| `HARDVEN_TAB_MAX` | `12` | **Sidecar:** max concurrent DEDICATED tab-manager tabs — the coverage-vs-machine-load ceiling (each = a Chrome renderer + its odds WS). Gap leagues beyond this are swept by the roving tail tab (or left uncovered if `HARDVEN_TAB_ROVE=0`). |
| `HARDVEN_TAB_ROVE` | `1` | **Sidecar:** the ROVING TAIL TAB — one extra tab (beyond the `HARDVEN_TAB_MAX` dedicated ones) that sweeps the overflow tail (paired leagues the board + dedicated tabs don't cover), re-pointing itself league→league every `HARDVEN_ROVE_DWELL_SEC`. Gives the tail opportunistic live-WS touches AND makes the browser genuinely visit those leagues (so the authed re-seed to them reads as organic browsing, not API-only). Its current league counts as WS-verified for verify-on-detection. `0` = off (tail relies on the httpx re-seed alone). |
| `HARDVEN_ROVE_DWELL_SEC` | `20` | **Sidecar:** how long the roving tail tab dwells on each league before moving to the next. |
| `HARDVEN_TAB_INTERVAL_SEC` | `20` | **Sidecar:** tab-manager tick period; also the pacing — at most one tab opens per tick (organic, not a burst). |
| `HARDVEN_TAB_COVER_TTL` | `240` | **Sidecar:** a league counts as "covered" (no tab needed) if the reader pushed one of its matchups within this many seconds. Pre-match lines tick slowly, so keep it generous; too low re-opens tabs for quiet-but-covered leagues. |
| `HARDVEN_TAB_START_DELAY_SEC` | `45` | **Sidecar:** delay before the tab manager's first tick, so the main board + the first pairing run settle before it computes gaps. |
| `PINNACLE_READER_RESEED_SEC` | `90` | **Sidecar:** in pure-reader mode (`PINNACLE_DEDICATED_WS=0 PINNACLE_WINDOW_WS_READ=1`), re-fetch EVERY active league's straight markets on this cadence. The browser WS is changes-only, so a stable pre-live line never re-pushes and an un-tabbed TAIL league would freeze at its one-time seed → this keeps them fresh. **Must stay comfortably below the C# `HARDVEN_BOOK_FRESH_SEC` gate (120)** so a re-seeded stable line never ages out between cycles (allow for the walk-time across all active leagues). Reader mode also serves the **real per-token timestamp** (not a global "fresh-while-connected" stamp), so a token that genuinely stops updating (league gone, market pulled) ages out and can't produce a phantom arb on a frozen price. |
| `PINNACLE_RESEED_SOURCE` | `authed` | **Sidecar:** where the reader re-seed (and one-time seed) gets prices. **`authed`** (default) = the logged-in `/markets/straight` = REAL, non-delayed prices (the guest feed can lag enough to swamp a ~1¢ pre-live edge). It's the most common request the real web app makes — a short authed GET, not a persistent socket, so the fingerprint footprint is low + mostly pre-existing; `_rest_death_check` treats a live reader WS as "up" so a re-seed blip can't false-kill the session. **`guest`** = the public API (no session, zero account link) if you prefer that trade over price freshness. |
| `PINNACLE_WINDOW_WS_TTL` | `30` | **Sidecar:** FALLBACK odds-recency staleness (seconds since the last odds PUBLISH). Now only used if the connection heartbeat below is unavailable — `_feed_live` prefers `odds_ws_alive()`. |
| `PINNACLE_WINDOW_WS_HEARTBEAT_TTL` | `150` | **Sidecar:** the reader's PRIMARY liveness gate — `_feed_live` reads live while the browser's Arcadia WS delivered ANY frame (odds OR MQTT keepalive) within this many seconds AND a socket is open. Keeps a **stable pre-match line LIVE through a quiet spell** (it stops re-pushing but the price is still valid) and flips dead only on a real drop/logout. Keep it comfortably above the MQTT keepalive interval (~60s) so pings hold it; the heartbeat age shows as `ws_hb=Ns` in the session-held line. |
| `PINNACLE_WS_AUTH_GIVEUP` | `2` | **Sidecar:** consecutive WS CONNACK auth-rejects (rc 4/5) before attempting recovery. If the browser is still logged in, this triggers a re-mint (below), not a give-up. |
| `PINNACLE_WS_REMINT_CAP` | `6` | **Sidecar:** on a WS auth-reject with a LOGGED-IN browser (a stale x-session after a rotation), force a browser re-mint (reload → fresh x-session → pushed to the paho WS) and retry — up to this many times per outage before finally giving up. Fixes HardVen books dying after the first block reopen. |
| `PINNACLE_WS_REMINT_THROTTLE_SEC` | `30` | **Sidecar:** minimum seconds between forced WS re-mints (so rapid reconnect-rejects during a reload don't stack reloads). |
| `PINNACLE_REST_AUTH_GIVEUP` | `3` | **Sidecar:** consecutive REST 401/403 on authed calls before declaring the session dead (the guest-redirect's backstop). |
| `PINNACLE_WS_WARN_SEC` | `120` | **Sidecar:** log a "still reconnecting" warning after this long down. Transient drops (network/server) auto-reconnect **forever** like a real tab — only genuine session death stops the feed. (Old `PINNACLE_WS_GIVEUP_SEC` still read for compat.) |
| `PINNACLE_SESSION_AGE_LOG_SEC` | `300` | **Sidecar:** cadence of the `session held Xm` heartbeat. On logout/give-up it prints `*** SESSION HELD Xm before this stop ***` — measures Pinnacle's real inactivity-logout window for the endurance test. |
| `HARDVEN_AUTO_PAIR` | unset | **Sidecar:** `1` = re-run the pairing pipeline at startup + on a repeating cadence (account-free). Off = pair manually (§2). |
| `HARDVEN_PAIR_INTERVAL_MIN` | `90` | **Sidecar:** re-pair every N minutes (subsumes the daily run) so LIVE/late-appearing games pair intraday. `0` = daily-only at `HARDVEN_PAIR_HOUR`. Gentle (guest `/catalog`); merge-safe (`pairHard` carries filled pairs). |
| `HARDVEN_PAIR_HOUR` | `5` | **Sidecar:** local hour for the daily auto-pair run (used only when `HARDVEN_PAIR_INTERVAL_MIN=0`). |
| `HARDVEN_PAIR_STARTUP_DELAY` | `8` | **Sidecar:** seconds to wait before the startup auto-pair (lets the sidecar's `/catalog` server come up). |
| `BOOKMAKER_REFRESH_SEC` | `2` | **Sidecar:** background loop cadence — how often it re-fetches the active leagues' schedules into the cache. This is the real quote-freshness floor. |
| `BOOKMAKER_SCHEDULE_CHUNK_LEAGUES` | `4` | **Sidecar:** leagues per concurrent GetSchedule request. The background fetch splits the active leagues into chunks fired in parallel (Promise.all) so wall-time ≈ slowest chunk, not sum. Lower = more parallelism (watch for rate-limits). |
| `BOOKMAKER_ACTIVE_TTL_SEC` | `120` | **Sidecar:** a league stops being refreshed if no `/odds` request has asked for it in this long (keeps the background fetch scoped to what the bot actually wants). |
| `BOOKMAKER_REFRESH_LOG` | unset | **Sidecar:** `1` = log EVERY refresh cycle's timing (for tuning the chunk size); default just a ~30s heartbeat + an immediate line on any overrun or rate-limit. |
| `BOOKMAKER_SCHEDULE_LINKDERIV` | `true` | **Sidecar:** `false` drops derivative markets (spreads/totals/props) from GetSchedule for a smaller/faster response while moneyline-only. Flip back to `true` when the props phase needs them. |
| `BOOKMAKER_KEEPALIVE_SEC` | `180` | **Sidecar:** keep-alive ping interval (renews `__cf_bm` / login). |
| `BOOKMAKER_RECOVER_COOLDOWN_SEC` | `45` | **Sidecar:** min seconds between session-recovery reloads. |
| `BOOKMAKER_RECOVER_WAIT_SEC` | `8` | **Sidecar:** wait after a recovery reload for the managed challenge to clear. |
| `BOOKMAKER_AUDIT` | unset | **Sidecar:** `1` = log ALL games' raw state to `quote_audit_*.jsonl` for post-mortem arb verification. Then `python sidecar/verify_arbs.py` cross-checks every CSV arb (MISPAIR / SUSPENDED / PRICE_DRIFT / THIN). |
| `BOOKMAKER_LIVE_DEBUG` | unset | **Sidecar:** `1` = log LIVE games only (`live_debug_*.jsonl`) for lock diagnosis (`find_lock_field.py`). |
| `BOOKMAKER_HEADLESS` | unset (headful) | Keep headful under Xvfb — `1` is more bot-detectable. |
| `BOOKMAKER_CATALOG_SPORTS` / `BOOKMAKER_CATALOG_LEAGUES` | — | Limit catalog discovery to specific sports / force explicit league ids. |
| `BOOKMAKER_AUTOLOGIN` | unset | **Sidecar:** `1` = re-login using Chrome's SAVED autofill — clicks the fields + presses Enter, no creds stored. **Desktop only:** needs an unlocked keyring, so it does NOT work on a headless server (Chrome can't persist the password there) — use `BOOKMAKER_USERNAME`/`PASSWORD` instead. The keep-alive already prevents the *inactivity* logout; this is the fallback for a hard logout. |
| `BOOKMAKER_USERNAME` / `BOOKMAKER_PASSWORD` | — | **Sidecar:** types these straight into the login form on re-login (no keyring needed) — **the reliable auto-relogin path on a headless server.** Caps at 3 tries. |
| `BOOKMAKER_LOGIN_USER_SEL` / `_PASS_SEL` / `_SUBMIT_SEL` | bookmaker.eu | **Sidecar:** override the login-form selectors (defaults: `input[name=account]` / `input[name=password]` / Enter). |

**Bot flag:** `--exclude tennis,cricket,…` skips pairs whose Kalshi ticker matches those sports (friendly names: tennis/baseball/basketball/cricket/soccer/football/afl/boxing/ufc — or any raw ticker substring like `KXATPMATCH`). For a cleaner telemetry run.

---

## Diagnostics / probes (reader mode)

Run these while the sidecar is up (pinnacle book, browser logged in). They need the live session, so they can't be run offline.

- **`coverage_check.py`** — reader coverage vs the guest board (covered / GAP / not-bettable). `python coverage_check.py [--ttl 120]`.
- **`GET /debug/reader?ttl=`** — the matchups the reader has actually pushed within `ttl`s (coverage truth), plus **`board_lids`** = the leagues the featured board (main page) is streaming (sport-level `sp/{id}/` topics). The tab manager excludes these from its dedicated-tab candidates so it never doubles up on a board-featured league (`PINNACLE_BOARD_LID_TTL`, default 300s).
- **`GET /debug/straight?lid=<lid>&source=cache|authed|guest`** — a league's current straight-market prices: `cache` (the live WS cache, no Pinnacle request), `authed` (one logged-in REST call), or `guest` (one public REST call).
- **`probe_reseed_delay.py`** — quantifies how far the **guest feed lags the authed feed** (the reason the re-seed defaults to `authed`). **Gentle by design:** defaults to 1 league, 5s interval, a hard `--max-rps` cap, and it **prints the projected load and waits for `y`** before sending anything. Default `--authed-source cache` reads the WS cache as the authed truth so the run adds **zero extra authed requests** (only public guest) — use a **live, tab-covered** league so the cache is real-time; `--authed-source rest` uses a logged-in REST call per sample for any league. `python probe_reseed_delay.py --lid 3649` (~0.2 req/s). Reports median / p90 / max guest catch-up lag + the typical disagreement in implied-prob cents. Small lag + tiny disagreement ⇒ guest is fine (and lower-footprint); multi-second lag or fat cents ⇒ keep `authed`.
- **`GET /debug/browser_fetch?lid=<lid>`** — feasibility probe for moving the re-seed *inside* the browser (`page.evaluate` fetch → genuine Chrome TLS, zero non-Chrome footprint). `ok:true, n_markets>0` ⇒ viable; an error (esp. CORS) ⇒ stick with the authed-httpx re-seed. `curl "127.0.0.1:8787/debug/browser_fetch?lid=3649"`. (Result 2026-07-16: **RED** — CORS-blocked; arcadia REST isn't browser-fetchable, so the re-seed stays on authed httpx.)

**Verify-on-detection** (tail arbs are confirmed on live WS before they're trusted):
- Each `/odds` price carries **`wv`** — `true` = under live WS coverage (a tab / recent push), `false` = **screening-only** (an httpx re-seed of an untabbed tail league). Absent in paho/REST mode → treated as verified.
- When a window opens on a `wv=false` HardVen leg, the bot POSTs **`/verify?lid=<lid>`** → the tab manager promotes that league to a live WS tab (jumping the gap queue, deduped 60s per league), so the arb re-evaluates on real-time prices. The window's **`HardVenWsVerified`** CSV column (0/1) records whether it was ever WS-confirmed — filter telemetry to `HardVenWsVerified=1` to trust only confirmed windows. Needs the tab manager (`HARDVEN_TAB_MANAGER=1`); `/verify` returns `at-cap` if `HARDVEN_TAB_MAX` is full (raise it), which the bot logs as `[VERIFY] league <id>: {…}`.

---

## Gotchas (all learned the hard way)

- **Machine sleep / feed drops are now survived.** The bot suppresses system sleep while running (Windows, `HARDVEN_KEEP_AWAKE`) and each feed is supervised — a WS drop / sleep-wake / sidecar blip **restarts** the feed instead of shutting the bot down (only a double-Ctrl+C quits). Belt-and-suspenders on a laptop: `powercfg /change standby-timeout-ac 0`.
- **uvicorn does NOT hot-reload Python.** Edited the adapter/parser? Restart the sidecar. **The 2026-07-13 changes (soccer in the catalog + the 3-way draw leg, intraday re-pairing, the `place_bet` safety gate) all live in sidecar code → they activate on the next sidecar restart.** Then the auto-pair (or a manual `pairHard.py → pair_pinnacle.py --write`) fills the soccer pairs.
- **Bookmaker session expires** (Cloudflare `cf_clearance`/`__cf_bm`, hours). The sidecar now **auto-recovers**: a keep-alive ping every `BOOKMAKER_KEEPALIVE_SEC` keeps it warm, and any odds 401/403/429/5xx fires a cooldown-guarded page reload that re-clears the *managed* challenge and re-captures `rtqname` — no manual click needed for the common case. Only an **interactive** challenge (captcha) needs a human (VNC). The Kalshi side keeps running regardless.
- **Stale HardVen quotes can't make phantom arbs anymore.** If the bookmaker side freezes (session dead, page mid-recovery), the sidecar re-serves the last quote with a frozen `ts`; the bot treats any quote older than `HARDVEN_QUOTE_MAX_AGE_MS` (30s) as stale and **clears that book** (logs `[HARDVEN] WARNING: N/M quotes STALE…`, then `…fresh again` on recovery). So a long run goes *quiet* during an outage instead of logging fat fakes — telemetry stays trustworthy. (Pre-2026-06-19 CSVs predate this fix and are poisoned.)
- **`pairHard` overwrites `cross_pairs.json`** (fresh blanks) → re-run `pair_pinnacle.py` after, every time (or use `HARDVEN_AUTO_PAIR`).
- **Fuzzy pairs are tagged `"fuzzy": true`** (MLB/WNBA/KBO/cricket team-name variants). They're fine for telemetry; the settlement back-test must validate them before any real-money M1.
- **Telemetry only (M0).** No orders are placed in `--telemetry`, and even in `--live`/`--dry-run` the HardVen side won't place a real bet: `place_bet()` previews unless `HARDVEN_BET_ENABLE=1`, and the actual UI bet-slip placement (`_place_via_ui`) is deferred/unbuilt. "Open arbs" that sit open for tens of seconds on thin/obscure markets are usually stale-quote phantoms, not executable — observe, don't trust.
