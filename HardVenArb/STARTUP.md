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

## Gotchas (all learned the hard way)

- **Keep the machine awake.** Sleep drops the Kalshi WS and the bot shuts down (a feed exiting cancels everything). Disable sleep: `powercfg /change standby-timeout-ac 0`.
- **uvicorn does NOT hot-reload Python.** Edited the adapter/parser? Restart the sidecar.
- **Bookmaker session expires** (Cloudflare `cf_clearance`/`__cf_bm`, hours). When it does, the `H:` books go stale and `GetGameView`/odds 401/403 — **click into a match again** to refresh. The Kalshi side keeps running; the bot won't die from this.
- **`pairHard` overwrites `cross_pairs.json`** (fresh blanks) → re-run `pair_auto` after, every time.
- **Fuzzy pairs are tagged `"fuzzy": true`** (MLB/WNBA/KBO/cricket team-name variants). They're fine for telemetry; the settlement back-test must validate them before any real-money M1.
- **Telemetry only.** No orders are placed (`--telemetry`). "Open arbs" that sit open for tens of seconds on thin/obscure markets are usually stale-quote phantoms, not executable — observe, don't trust.
