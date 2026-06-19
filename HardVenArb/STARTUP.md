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

## Running unattended on a Linux server (tmux)

The C# bot is portable (.NET 10, Kalshi side is just HTTP/WS); the **sidecar** needs real Chrome under a
virtual display. The bookmaker session is **IP + fingerprint bound**, so the datacenter IP must do its own
one-time login (home cookies won't validate here) and will draw more Cloudflare challenges than home.

**One-time server setup**
```bash
sudo apt install -y xvfb x11vnc                       # virtual display + VNC for the one-time login
# install Google Chrome (the channel="chrome" the adapter prefers); bundled chromium is more bot-blocked
python -m pip install fastapi "uvicorn[standard]" httpx websockets playwright rapidfuzz
playwright install chromium                           # fallback browser only
```

**One-time login (seed `.bookmaker_profile` on the server's IP)**
```bash
Xvfb :99 -screen 0 1400x900x24 &
export DISPLAY=:99
x11vnc -display :99 -localhost -nopw -bg              # SSH-tunnel 5900, VNC in
cd HardVenArb/sidecar
HARDVEN_BOOK=bookmaker python -m uvicorn app:app --port 8787   # Chrome opens on :99 → VNC in,
#   log in / clear Cloudflare / click a match once. The profile remembers it across restarts.
```

**Normal unattended run (tmux, two panes)**
```bash
tmux new -s hardven
# pane 1 — sidecar under the virtual display:
cd HardVenArb/sidecar && HARDVEN_BOOK=bookmaker DISPLAY=:99 xvfb-run -a python -m uvicorn app:app --port 8787
# pane 2 — pairs + bot:
cd HardVenArb && python pairHard.py && python sidecar/pair_auto.py --fuzzy --write
dotnet run --project HardVenArb -- --telemetry
# Ctrl-b d to detach; the run survives logout/SSH close.
```
The sidecar now **keeps the session warm and auto-recovers** (see Gotchas) — but an *interactive* Cloudflare
challenge (captcha) still needs a human: VNC in and clear it once.

## Tuning knobs (env)

| Var | Default | What |
|-----|---------|------|
| `HARDVEN_QUOTE_MAX_AGE_MS` | `30000` | **Bot:** a HardVen quote older than this (sidecar `ts`) is treated as STALE → its book is cleared so no phantom arb can fire after a session drop. |
| `BOOKMAKER_KEEPALIVE_SEC` | `180` | **Sidecar:** keep-alive ping interval (renews `__cf_bm` / login). |
| `BOOKMAKER_RECOVER_COOLDOWN_SEC` | `45` | **Sidecar:** min seconds between session-recovery reloads. |
| `BOOKMAKER_RECOVER_WAIT_SEC` | `8` | **Sidecar:** wait after a recovery reload for the managed challenge to clear. |
| `BOOKMAKER_HEADLESS` | unset (headful) | Keep headful under Xvfb — `1` is more bot-detectable. |
| `BOOKMAKER_CATALOG_SPORTS` / `BOOKMAKER_CATALOG_LEAGUES` | — | Limit catalog discovery to specific sports / force explicit league ids. |
| `BOOKMAKER_USERNAME` / `BOOKMAKER_PASSWORD` | — | **Sidecar:** if set, recovery attempts auto re-login when the ACCOUNT session expires (caps at 3 tries; can't pass a captcha/2FA). Unset = manual login via the window/VNC. |
| `BOOKMAKER_LOGIN_USER_SEL` / `_PASS_SEL` / `_SUBMIT_SEL` | generic | **Sidecar:** CSS selectors for the login form, if the defaults don't match bookmaker.eu's form. |

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
