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
    print("\n" + msg + "\n")
    if a.dry:
        return 2 if bad else 0
    url = load_webhook(os.path.join(a.root, ".env"))
    if not url:
        print("[DISCORD] DISCORD_WEBHOOK_URL not set - nothing posted.")
        return 1
    ok = post(url, msg)
    print("[DISCORD] posted." if ok else "[DISCORD] post FAILED.")
    return 0 if ok and not bad else (2 if bad else 1)


if __name__ == "__main__":
    raise SystemExit(main())
