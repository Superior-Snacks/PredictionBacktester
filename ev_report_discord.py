#!/usr/bin/env python3
"""Run the EV bot's --resolve and post a digest to Discord, for monitoring from away from the machine.

WHY A DIGEST AND NOT THE REPORT
--------------------------------
The calibration report is ~200 lines; a Discord message caps at 2000 characters. Truncating it would cut
section 6 - the only part that says whether anything has changed - so this extracts the handful of lines
that answer "is it healthy, is it still positive, how far along is it" and drops the rest. The full report
stays on the machine for when you actually want to read it.

WHAT IT REPORTS, AND WHY EACH LINE EARNS ITS PLACE
--------------------------------------------------
  PAIR ORIENTATION  first, because every other number is void if the pairs are flipped.
  429 / errors      a resolve shares Kalshi's REST budget with the running bot; this is the one way the two
                    interact, so a spike belongs in the digest rather than being discovered later.
  pooled bias       the oracle. Settled as of 2026-08-26, so it is a regression check, not news.
  signals-only      the strategy's calibration, the number that actually moves.
  section 6 A/B/C   sigma above break-even, ROI per contract, and the always-valid bound - C being the one
                    that licenses a decision under the daily re-checking this very script encourages.
  progress          n of target, so "nothing changed" reads as progress rather than silence.

SAFE TO RUN WHILE THE BOT IS LIVE. `--resolve` is REST-only and never opens a WebSocket, so it cannot
disturb the single Kalshi WS the bot holds. Both processes append to ev_settlements.jsonl; that was verified
clean across concurrent runs (2265 lines, 0 malformed). The only real interaction is the shared REST rate
limit, which is why the 429 count is surfaced above.
"""
from __future__ import annotations
import argparse, io, os, re, subprocess, sys, urllib.error, urllib.request, json, datetime as dt

DEFAULT_EXE = os.path.join("KalshiEvBot", "bin", "report", "KalshiEvBot.exe")


def load_webhook(env_path: str) -> str:
    url = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    if url:
        return url
    if os.path.exists(env_path):
        for line in io.open(env_path, encoding="utf-8", errors="replace"):
            m = re.match(r'\s*(?:export\s+)?DISCORD_WEBHOOK_URL\s*=\s*["\']?([^"\'\s#]+)', line)
            if m:
                return m.group(1)
    return ""


def clean(s: str) -> str:
    """ASCII-fold a line lifted from the report.

    The capture has MIXED encodings - the exe emits UTF-8 while a Windows console hands back cp1252 - so no
    single decode is right for the whole file and the plus-minus sign comes through mangled either way.
    These lines are machine-extracted numbers, so folding to ASCII loses nothing and beats guessing.
    """
    for a, b in (("±", "+/-"), ("ñ", "+/-"), ("Â", ""), ("→", "->"),
                 ("—", "-"), ("–", "-"), ("‘", "'"), ("’", "'")):
        s = s.replace(a, b)
    return "".join(ch if ord(ch) < 128 else "?" for ch in s)


def grab(text: str, pattern: str, group: int = 0) -> str:
    m = re.search(pattern, text, re.MULTILINE)
    return (clean(m.group(group)).strip() if m else "")


def digest(out: str) -> tuple[str, bool]:
    """(message, alarming). `alarming` drives the leading emoji so a bad run is visible without reading."""
    bad = False
    L = []

    orient = grab(out, r"^\s*PAIR ORIENTATION: .*$")
    if orient:
        n_flip = grab(out, r"PAIR ORIENTATION: \d+ signal ticker\(s\) name-verified, (\d+) MIS-ORIENTED", 1)
        if n_flip and n_flip != "0":
            bad = True
            L.append(f"🚨 **{orient.strip()}**")
        else:
            L.append(f"✅ {orient.strip()}")

    settled = grab(out, r"^\s*settled: .*$")
    if settled:
        L.append(f"`{settled.strip()}`")

    # A resolve shares Kalshi's REST budget with the live bot - surface failures rather than hiding them.
    fetched = grab(out, r"^\[RESOLVE\] \d+ fetched.*$")
    if fetched:
        fails = grab(out, r"(\d+) failed", 1)
        if fails and fails != "0":
            bad = True
            L.append(f"⚠️ `{fetched.strip()}`")

    pooled = grab(out, r"^\s*proportional\s+predicted .*$")
    if pooled:
        L.append("**oracle** `" + re.sub(r"\s+", " ", pooled.strip()) + "`")

    sig = grab(out, r"^\s*SIGNALS ONLY\s+n=.*$")
    if sig:
        L.append("**signals** `" + re.sub(r"\s+", " ", sig.strip()) + "`")

    a = grab(out, r"^\s*now\s+n=\d+\s+diff .*$")
    b = grab(out, r"^\s*now\s+[+-][\d.]+ per contract.*$")
    c = grab(out, r"^\s*now\s+95% lower bound on the edge = .*$")
    prog = grab(out, r"^\s*PROGRESS: .*$")
    if a or b or c or prog:
        L.append("**verdict**")
        for x in (a, b, c, prog):
            if x:
                L.append("`" + re.sub(r"\s+", " ", x.strip()) + "`")
    # PARSE THE SIGMA, DO NOT SUBSTRING-SEARCH FOR "-". The line ends "...sigma above break-even", and
    # "break-even" contains a hyphen - the first cut flagged every healthy run as an alarm because of it.
    sg = grab(a, r"->\s*([+-]?[\d.]+)\s*sigma", 1) if a else ""
    try:
        if sg and float(sg) < 0:
            bad = True                                 # below break-even
    except ValueError:
        pass

    head = ("🚨 **EV bot — resolve**" if bad else "📊 **EV bot — resolve**")
    stamp = dt.datetime.now().strftime("%a %d %b %H:%M")
    msg = head + f"  _{stamp}_\n" + "\n".join(L)
    return (msg[:1980], bad)


def live_digest(root: str) -> str:
    """Build the LIVE-PATH block from EvLive_*.csv directly, not from the report's text.

    WHY THE CSV AND NOT SECTION 7. Section 7 prints a summary; the CSV carries the columns that make a
    miss INTERPRETABLE - DepthToLimit above all. "Fill rate 60%" is not actionable on its own, because
    depth-bound and latency-bound misses point at opposite fixes: the first says the size was never there
    and firing sooner changes nothing, the second says the book moved while we were in a round trip. That
    split is the single most useful thing M1 can report, and it exists nowhere in the printed report.

    Returns "" when there are no live rows, so an M0 run posts nothing rather than an empty heading.
    """
    import csv, glob
    rows = []
    for f in sorted(glob.glob(os.path.join(root, "EvLive_*.csv"))):
        try:
            with io.open(f, encoding="utf-8", errors="replace", newline="") as fh:
                rows.extend(list(csv.DictReader(fh)))
        except Exception:
            continue
    if not rows:
        return ""

    def num(r, k, d=0.0):
        try:
            return float((r.get(k) or "").strip())
        except (ValueError, AttributeError):
            return d

    # An attempt is a row we actually sent. budget-exhausted and the fee-rounding refusal are decisions
    # NOT to try, and counting them as misses would understate the fill rate by exactly the caps' effect.
    # An error counts as an attempt even where Requested reads 0: rows written before the size was
    # hoisted out of the try block logged it that way, and dropping them would hide venue failures.
    attempts = [r for r in rows
                if ((r.get("Status") or "").startswith("error") or num(r, "Requested") > 0)
                and (r.get("Status") or "") not in ("budget-exhausted", "fee-rounding-negative")]
    fills = [r for r in attempts if num(r, "FillCount") > 0]
    errs = [r for r in attempts if (r.get("Status") or "").startswith("error")]
    misses = [r for r in attempts if num(r, "FillCount") <= 0 and r not in errs]
    if not attempts:
        return ""

    L = ["🔵 **LIVE PATH** — can we buy what we find?"]
    fr = 100.0 * len(fills) / len(attempts)
    L.append(f"`attempts {len(attempts)}   FILLED {len(fills)} ({fr:.1f}%)   "
             f"no-fill {len(misses)}   err {len(errs)}`")
    if fills:
        ctr = sum(num(r, "FillCount") for r in fills)
        spend = sum(num(r, "FillCount") * (num(r, "AvgFillPrice") or num(r, "LimitPrice")) for r in fills)
        L.append(f"`bought {ctr:.0f} contract(s) for ${spend:.2f}`")

    # THE SPLIT THAT DECIDES WHAT TO FIX. DepthToLimit is size showing at or better than our limit when we
    # fired; -1 means the WS book did not reach the limit but REST said it was there, which is unknowable
    # rather than either category, so it is reported separately instead of being forced into one.
    if misses:
        depth_bound = sum(1 for r in misses
                          if 0 <= num(r, "DepthToLimit", -1) < num(r, "Requested"))
        unknown = sum(1 for r in misses if num(r, "DepthToLimit", -1) < 0)
        other = len(misses) - depth_bound - unknown
        L.append(f"**misses**: depth-bound {depth_bound}/{len(misses)}"
                 + (f", book-moved {other}" if other else "")
                 + (f", unknown {unknown}" if unknown else ""))
        # ONLY THE CLASSIFIED MISSES MAY DECIDE THIS. An earlier cut compared depth_bound against
        # (misses - depth_bound), which folds `unknown` into `book-moved` and announced "latency is
        # costing fills" off a single unclassifiable miss with zero book-moved rows behind it.
        # `unknown` means the WS book never reached our limit while REST claimed it was there - which is
        # itself a signal that the price was phantom, not evidence about speed.
        known = depth_bound + other
        if known < 5:
            L.append(f"_{known} classified miss(es) — too few to say whether depth or latency dominates._")
        elif depth_bound > other:
            L.append("_depth-bound dominates: the size was never there; firing sooner would not help._")
        elif other > depth_bound:
            L.append("_book-moved dominates: latency is costing fills._")
        else:
            L.append("_depth and latency are running even._")

    # THE DAY'S TWO MONEY FIGURES, which are different things and are labelled as such: the telemetry
    # basis is frozen fake money that only sizes the CSV's Kelly column; equity is real spendable cash on
    # the trading shard. Read from the newest row that carries them.
    money = next((r for r in reversed(rows) if (r.get("EquityUsd") or "").strip()), None)
    if money:
        L.append(f"`bankroll: telemetry basis ${num(money,'BankrollUsd'):,.2f} (frozen)   "
                 f"real cash on shard ${num(money,'EquityUsd'):,.2f}`")

    # REAL P/L, on contracts we actually own — not section 5's hypothetical. Joins fills to settlement.
    settled = {}
    try:
        for line in io.open(os.path.join(root, "ev_settlements.jsonl"), encoding="utf-8", errors="replace"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            t, res = d.get("ticker", ""), (d.get("result") or "").strip().lower()
            if t and res in ("yes", "no"):
                settled[t] = res
    except Exception:
        pass
    done = open_n = 0
    realised = at_risk = 0.0
    for r in fills:
        n = num(r, "FillCount")
        px = num(r, "AvgFillPrice") or num(r, "LimitPrice")
        fee = num(r, "FeeVenueUsd") or num(r, "FeeChargedUsd")
        res = settled.get((r.get("Ticker") or "").strip())
        if res is None:
            open_n += 1
            at_risk += n * px + fee
            continue
        won = (res == "yes") if (r.get("Side") or "").upper() == "YES" else (res == "no")
        done += 1
        realised += n * ((1.0 if won else 0.0) - px) - fee
    if done or open_n:
        bits = []
        if done:
            bits.append(f"**settled {done}: {realised:+.2f}**")
        if open_n:
            bits.append(f"open {open_n} (${at_risk:.2f} at risk)")
        L.append("`P/L on real fills — " + "   ".join(bits) + "`")
        if done and done < 10:
            L.append(f"_{done} settled fill(s) is not a P/L, it is a sample. Watch slippage and fill rate._")

    slips = sorted(num(r, "SlippageCents") for r in fills if (r.get("SlippageCents") or "").strip())
    if slips:
        better = sum(1 for x in slips if x <= 0)
        L.append(f"`slippage vs screened: median {slips[len(slips)//2]:+.1f}c  "
                 f"worst {slips[-1]:+.1f}c  ({better}/{len(slips)} at or better)`")
    lat = sorted(num(r, "LatencyMs") for r in attempts if num(r, "LatencyMs") > 0)
    if lat:
        L.append(f"`round-trip: median {lat[len(lat)//2]:.0f}ms  p90 {lat[int(0.9*(len(lat)-1))]:.0f}ms`")
    partial = sum(1 for r in fills if 0 < num(r, "FillCount") < num(r, "Requested"))
    if partial:
        L.append(f"`PARTIAL on {partial} of {len(fills)} fills — depth ran out mid-order`")

    # Fee rounding: the real, unmodelled drag. Section 8 projects it; this is what we actually paid.
    ctr_f = sum(num(r, "FillCount") for r in fills)
    drag = sum(num(r, "FeeChargedUsd") - num(r, "FeeAssumedUsd") for r in fills)
    if ctr_f > 0 and any((r.get("FeeChargedUsd") or "").strip() for r in fills):
        L.append(f"`fee rounding actually paid: {drag / ctr_f * 100:.3f}c per contract`")

    for label, want in (("in-play", "1"), ("pre-match", "0")):
        sub = [r for r in attempts if (r.get("InPlay") or "") == want]
        if len(sub) >= 3:
            g = sum(1 for r in sub if num(r, "FillCount") > 0)
            L.append(f"`{label:9} {g}/{len(sub)} filled ({100.0*g/len(sub):.0f}%)`")
    skipped = sum(1 for r in rows if (r.get("Status") or "") == "budget-exhausted")
    feeskip = sum(1 for r in rows if (r.get("Status") or "") == "fee-rounding-negative")
    if skipped or feeskip:
        L.append(f"_not attempted: {skipped} budget-capped, {feeskip} fee-rounding_")
    if errs:
        kinds = sorted({(r.get("Status") or "") for r in errs})
        L.append(f"⚠️ `{len(errs)} venue error(s): {', '.join(kinds)[:90]}`")
    # NOT clean()'d. That helper ASCII-folds, which is right for lines lifted out of the report's
    # mixed-encoding capture but wrong here: this block is assembled from CSV fields we control, and
    # folding turned its own status emoji into literal "?" - losing exactly the at-a-glance signal they
    # exist to give. Every interpolated value here is a ticker, a count or an exception name, all ASCII.
    return "\n".join(L)[:1980]


def post(url: str, message: str) -> bool:
    """POST via httpx, NOT urllib.

    Discord sits behind Cloudflare, which blocks `Python-urllib` on client fingerprint and answers
    `HTTP 403 error code: 1010` - the identical trap that made a perfectly good oddspapi key look dead
    earlier in this project. httpx's default fingerprint passes; `notify.py` already uses it for the same
    webhook, so this matches the client that is known to work here.
    """
    try:
        import httpx
    except ImportError:
        print("[DISCORD] httpx not installed (pip install httpx) - cannot post.")
        return False
    try:
        with httpx.Client(timeout=20.0) as c:
            r = c.post(url, json={"content": message})
        if 200 <= r.status_code < 300:
            return True
        print(f"[DISCORD] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[DISCORD] {type(e).__name__}: {e}")
    return False


def main() -> int:
    # The digest carries emoji so a bad run is visible at a glance in Discord, but a Windows console is
    # cp1252 and dies on them - the PREVIEW would crash while the POST (UTF-8 over HTTP) was perfectly fine.
    # Same trap that produced the analyzer's ÔÇö mojibake.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    root = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--root", default=root)
    ap.add_argument("--exe", default="", help="path to the report build (default KalshiEvBot/bin/report)")
    ap.add_argument("--from-log", default="", help="parse an existing --resolve capture instead of running it")
    ap.add_argument("--dry", action="store_true", help="print the digest, post nothing")
    ap.add_argument("--save", default="", help="also write the FULL report here")
    a = ap.parse_args()

    def decode(b: bytes) -> str:
        # The report prints '±' and box-drawing; a Windows console may hand them back as cp1252.
        # Try utf-8 first, fall back rather than replacing them with question marks.
        for enc in ("utf-8", "cp1252", "latin-1"):
            try: return b.decode(enc)
            except UnicodeDecodeError: continue
        return b.decode("utf-8", errors="replace")

    if a.from_log:
        out = decode(io.open(a.from_log, "rb").read())
    else:
        exe = a.exe or os.path.join(a.root, DEFAULT_EXE)
        if not os.path.exists(exe):
            print(f"[FATAL] report build not found at {exe}\n"
                  f"        build it with: dotnet build KalshiEvBot/KalshiEvBot.csproj -o KalshiEvBot/bin/report")
            return 1
        print(f"[EV] running {os.path.basename(exe)} --resolve (a few minutes)...")
        p = subprocess.run([exe, "--resolve"], cwd=a.root, capture_output=True)
        out = decode(p.stdout or b"") + decode(p.stderr or b"")
        if p.returncode != 0:
            print(f"[EV] --resolve exited {p.returncode}")
    if a.save:
        io.open(a.save, "w", encoding="utf-8").write(out)
        print(f"[EV] full report -> {a.save}")

    msg, bad = digest(out)
    # A SECOND MESSAGE, NOT A LONGER ONE. Discord caps at 2000 characters, and appending the live block
    # would push section 6 - the only part that says whether anything changed - off the bottom. They also
    # answer different questions ("is the edge real?" vs "can we buy it?"), so they read better apart.
    live = live_digest(a.root)
    print("\n" + msg + "\n")
    if live:
        print(live + "\n")
    if a.dry:
        return 2 if bad else 0
    url = load_webhook(os.path.join(a.root, ".env"))
    if not url:
        print("[DISCORD] DISCORD_WEBHOOK_URL not set - nothing posted.")
        return 1
    ok = post(url, msg)
    if live and ok:
        ok = post(url, live)
    print("[DISCORD] posted." if ok else "[DISCORD] post FAILED.")
    return 0 if ok and not bad else (2 if bad else 1)


if __name__ == "__main__":
    raise SystemExit(main())
