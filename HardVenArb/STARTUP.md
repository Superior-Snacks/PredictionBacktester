# HardVenArb — Startup Protocol

Kalshi ↔ bookmaker.eu cross-arb telemetry bot. Runs **locally on Windows** (the bookmaker side is tethered
to a real Chrome to get past Cloudflare — not the Linux server). Three pieces: **sidecar** (browser +
odds API), **pairHard/pair_auto** (build the pair list), **the C# bot** (Kalshi WS + telemetry).

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
# leagues to watch (baseball ids + tennis sport-auto-discovery):
$env:PINNACLE_CATALOG_LEAGUES="246"; $env:PINNACLE_CATALOG_SPORTS="33"
python -m uvicorn app:app --port 8787
```
- A Chrome window opens on pinnacle.bet. **Log in**, then **click into any sport once** so the page opens its odds WebSocket (that frame yields the WS login). The persistent profile (`.pinnacle_profile`) remembers you, so most restarts capture automatically — no re-login.
- Watch for `[PINNACLE SESSION] captured WS login …` then the `SESSION CAPTURED — the bot is GO` banner. The feed stays idle until then; the C# bot logs `[HARDVEN] sidecar session READY` once odds flow.
- **Leave the window + uvicorn running.** The open tab holds the session (gentle auto-activity + the adapter's authed-REST keepalive guard against the inactivity logout). The bot replays the captured session over its own clean httpx/paho feed — the window only mints + holds it; it does not serve odds.
- Readiness check: `Invoke-RestMethod "http://127.0.0.1:8787/health" | ConvertTo-Json -Depth 4` → `session_ready` + a masked `session` block.
- Knobs: `PINNACLE_HEADLESS=1` (no window — only after a profile exists & login persists), `PINNACLE_CHANNEL` (default `chrome`), `PINNACLE_USER_DATA_DIR`, `PINNACLE_BROWSER_ACTIVITY_SEC` (default 200).
- **Pairing needs NO login.** `catalog()`/`pair_pinnacle.py` use Pinnacle's GUEST API (public key, no session), so you can build the pair list anytime — even before logging into the window. The browser login is only for the live odds feed. (If you ever see `[PAIR] 0 Pinnacle games in /catalog`, the sidecar isn't running or `PINNACLE_CATALOG_SPORTS`/`PINNACLE_CATALOG_LEAGUES` aren't set — it is NOT a login problem.)
- **Derivative lines (spread/total):** `python sidecar/pair_derivatives.py --write` pairs Kalshi spread/total ↔ Pinnacle handicap/over-under (MLB/KBO/ATP) into `derivative_pairs.json` (guest API, no login). The bot loads it at startup alongside `cross_pairs.json` and the sidecar resolves the `{lid}:{mid}:{type}:{points}:{side}` tokens. Hot-reload watches `cross_pairs.json` only → re-run the pairer + restart to refresh lines.
- **Lifecycle (human session rhythm):** set **`PINNACLE_LIFECYCLE=1`** to have the sidecar OPEN the browser only during game windows (computed by `schedule.py` from the slate) and go DARK between them / overnight — instead of holding it open 24/7. `PINNACLE_LIFECYCLE_SPORTS` (default `3,33` = baseball,tennis). Lifecycle state shows on `/health` under `session.lifecycle`. Default (unset) = browser stays open (M0/manual). Caveat: a long dark gap may log the Pinnacle session out → the next window needs a manual re-login (fine while login is manual).
- **Currency / FX:** the Pinnacle account is **EUR**, Kalshi is **USD**. Arb *detection* is unaffected (it compares unitless probabilities — `1/odds` and the Kalshi 0–1 price), but *depth/capital/profit* mix EUR-payout units with USD. Set **`HARDVEN_FX_TO_USD`** (USD per book-unit, e.g. `1.08`) so HardVen size is converted to USD-equivalent (price is left unitless). Default `1.0` = a USD book. Update it occasionally; the rate moves <1%/day.
- **Availability:** the adapter tracks each moneyline's status — a market that goes OFFLINE/suspended (status not `open`/`null`, or the moneyline drops out of the pushed record) is marked `suspended` → the C# clears that book so no arb fires on it. Run with `PINNACLE_DEBUG_STATUS=1` to log offline/suspend transitions (and capture the exact offline status string). Note: tennis "(Games)" tabs show "moneyline unavailable" because that derivative simply has no moneyline market (only spread/total) — pairing already excludes it.

## 2. Build the pair list  (Terminal 2)

```powershell
cd HardVenArb
python pairHard.py                              # scaffold Kalshi side: all in-season moneylines (CLASSIC_SERIES)
python sidecar/pair_auto.py --fuzzy --write     # fill the bookmaker side (soccer 3-way + fuzzy team names)
```
- `pairHard.py` **overwrites** `cross_pairs.json` with fresh blanks → **always run `pair_auto` right after.**
- `--days 20` widens the window; `--series KX…,KX…` narrows to specific sports.
- `pair_auto` preview (no `--write`) first if you want to eyeball matches; check the `unmatched` summary.

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
- Windows are logged to `CrossArbTelemetry_*.csv` (repo root).
- The bot **hot-reloads `cross_pairs.json` every 30s**, so you can re-run pair_auto without restarting it.

---

## Daily refresh (new games appear, old ones settle)

Just re-run step 2 — league discovery is dynamic (`GetLeagues`), so no id-wrangling:
```powershell
python pairHard.py ; python sidecar/pair_auto.py --fuzzy --write
```

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

## Gotchas (all learned the hard way)

- **Keep the machine awake.** Sleep drops the Kalshi WS and the bot shuts down (a feed exiting cancels everything). Disable sleep: `powercfg /change standby-timeout-ac 0`.
- **uvicorn does NOT hot-reload Python.** Edited the adapter/parser? Restart the sidecar.
- **Bookmaker session expires** (Cloudflare `cf_clearance`/`__cf_bm`, hours). The sidecar now **auto-recovers**: a keep-alive ping every `BOOKMAKER_KEEPALIVE_SEC` keeps it warm, and any odds 401/403/429/5xx fires a cooldown-guarded page reload that re-clears the *managed* challenge and re-captures `rtqname` — no manual click needed for the common case. Only an **interactive** challenge (captcha) needs a human (VNC). The Kalshi side keeps running regardless.
- **Stale HardVen quotes can't make phantom arbs anymore.** If the bookmaker side freezes (session dead, page mid-recovery), the sidecar re-serves the last quote with a frozen `ts`; the bot treats any quote older than `HARDVEN_QUOTE_MAX_AGE_MS` (30s) as stale and **clears that book** (logs `[HARDVEN] WARNING: N/M quotes STALE…`, then `…fresh again` on recovery). So a long run goes *quiet* during an outage instead of logging fat fakes — telemetry stays trustworthy. (Pre-2026-06-19 CSVs predate this fix and are poisoned.)
- **`pairHard` overwrites `cross_pairs.json`** (fresh blanks) → re-run `pair_auto` after, every time.
- **Fuzzy pairs are tagged `"fuzzy": true`** (MLB/WNBA/KBO/cricket team-name variants). They're fine for telemetry; the settlement back-test must validate them before any real-money M1.
- **Telemetry only.** No orders are placed (`--telemetry`). "Open arbs" that sit open for tens of seconds on thin/obscure markets are usually stale-quote phantoms, not executable — observe, don't trust.
