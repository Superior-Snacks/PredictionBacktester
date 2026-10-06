"""Was raising the price floor from 0.20 to 0.30 (EV_MIN_PRICE, 2026-10-06) the right call?

The bot still screens 20-30c markets after the change; it just labels them OUT_OF_BAND and sends no order.
This rebuilds the WOULD-BE signals in that band - SIGNAL rows from before the change, and OUT_OF_BAND rows after
it that passed every other guard (clears 1c at REST, in-play, Pinnacle rising >= 0.5c, both de-vigs agree, not
Kalshi-led, WS/REST within 3c, oracle WS-verified, disagreement <= 15c) - and grades them against settlement and
Kalshi's move afterwards, beside the 30-40c signals the bot does trade.

What it CAN answer: did those signals carry edge at the price on offer (won - cost at the REST ask; ~1c of walk
comes off a real order), and did Kalshi move toward them. What it CANNOT: whether the orders that would have
FILLED were the bad ones - the losses that motivated the floor sat in the fills, and with no orders there is no
fill data. Read the FRESH section (after the change) first.

    python ev_floor_check.py
    python ev_floor_check.py --since 2026-10-07

Read-only over EvTelemetry_*.csv, EvFollowUp_*.csv and ev_settlements.jsonl.
"""
import argparse, csv, glob, io, json, math, os, sys

CHANGED = "2026-10-06"     # the day EV_MIN_PRICE went 0.20 -> 0.30


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def passes_other_guards(r) -> bool:
    ask = f(r.get("KalshiRestAsk"))
    return (f(r.get("Ev")) >= 0.01 and r.get("InPlay") == "1" and r.get("DeVigAgree") == "1"
            and f(r.get("PinnacleRiseCents")) >= 0.5 and abs(f(r.get("WsRestGapCents"))) <= 3
            and r.get("MoveRegime") != "KALSHI_LED" and r.get("OracleWsVerified") == "1"
            and abs(f(r.get("PTrueUsed")) - ask) <= 0.15)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--since", default="2026-10-07", help="FRESH window start (default: the day after the change)")
    a = ap.parse_args()

    res = {}
    try:
        with io.open(os.path.join(a.root, "ev_settlements.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if j.get("result") in ("yes", "no"):
                    res[j["ticker"]] = j["result"]
    except OSError:
        pass

    band, ref = {}, {}        # (ticker, side) -> first would-be signal in 20-30c / first real signal in 30-40c
    band_f, ref_f = {}, {}    # the same, but the first FILLABLE one - section 6's definition ("graded at the first such")
    for fn in sorted(glob.glob(os.path.join(a.root, "EvTelemetry_*.csv"))):
        with io.open(fn, newline="", encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                d = r.get("Decision", "")
                if d not in ("SIGNAL", "OUT_OF_BAND") or "MATCH" not in r.get("Ticker", "").split("-")[0]:
                    continue
                ask = f(r.get("KalshiRestAsk"))
                k = (r["Ticker"], r["Side"])
                o = dict(tk=r["Ticker"], side=r["Side"], p=f(r.get("PTrueProp")), cost=f(r.get("CostPerContract")),
                         day=r["Timestamp"][:10], t=r["Timestamp"], fill=f(r.get("WsDepthToLimit")) > 0)
                if 0.20 <= ask < 0.30 and (d == "SIGNAL" or passes_other_guards(r)):
                    band.setdefault(k, o)
                    if o["fill"]:
                        band_f.setdefault(k, o)
                elif 0.30 <= ask < 0.40 and d == "SIGNAL":
                    ref.setdefault(k, o)
                    if o["fill"]:
                        ref_f.setdefault(k, o)

    # Kalshi's move 20s after (CLV, + = toward us), first follow-up per side
    clv = {}
    want = set(band) | set(ref) | set(band_f) | set(ref_f)
    for fn in sorted(glob.glob(os.path.join(a.root, "EvFollowUp_*.csv"))):
        with io.open(fn, newline="", encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                k = (r.get("Ticker", ""), r.get("Side", ""))
                if k not in want or k in clv or r.get("Decision") not in ("SIGNAL", "OUT_OF_BAND"):
                    continue
                if abs(f(r.get("AgeSec")) - 20) > 4:
                    continue
                kd = f(r.get("KalshiDriftCents"))
                if kd == kd:
                    clv[k] = kd * (1 if f(r.get("EntryPTrue")) > f(r.get("EntryAsk")) else -1)

    def grade(label, d, keep):
        xs = [(k, o) for k, o in d.items() if keep(o) and o["tk"] in res and o["p"] == o["p"] and o["cost"] == o["cost"]]
        if len(xs) < 2:
            print(f"   {label:<34} n={len(xs):>4}  (too few)")
            return
        n = len(xs)
        won = [1.0 if res[o["tk"]] == o["side"].lower() else 0.0 for _, o in xs]
        w = sum(won) / n
        p = sum(o["p"] for _, o in xs) / n
        edge = sum(x - o["cost"] for x, (_, o) in zip(won, xs)) / n
        se = math.sqrt(max(w * (1 - w), 1e-6) / n)
        cv = [clv[k] for k, _ in xs if k in clv]
        cvs = f"   Kalshi T+20 {sum(cv) / len(cv):+.2f}c" if cv else ""
        print(f"   {label:<34} n={n:>4}   won {100 * w:5.1f}% vs Pinnacle {100 * p:5.1f}% ({100 * (w - p):+5.1f}pt)"
              f"   won-cost {100 * edge:+5.1f}c +/- {100 * se:.1f}{cvs}")

    for title, keep in ((f"BEFORE the change (to {CHANGED})", lambda o: o["day"] <= CHANGED),
                        (f"FRESH (from {a.since})", lambda o: o["day"] >= a.since)):
        print(f"-- {title} --")
        grade("20-30c would-be signals (excluded)", band, keep)
        grade("  first FILLABLE one per side", band_f, keep)
        grade("30-40c signals (traded)", ref, keep)
        grade("  first FILLABLE one per side", ref_f, keep)
        print()
    print("won-cost is at the REST ask: a real order pays ~1c more. The 20-30c floor was a cheap precaution, not a")
    print("proven fix: the band's FILLABLE signals looked fine at the signal (+4.1c +/- 3.5 before the change); the")
    print("losses sat in the orders that filled. If the FRESH fillable 20-30c line stays clearly positive (above ~+2c")
    print("with the error bar not crossing zero), the floor is costing money - set EV_MIN_PRICE back to 0.20.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
