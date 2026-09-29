"""Women's vs men's tennis markets for KalshiEvBot: is the edge the same after the winner's curse?

Found 2026-09-29: after a signal, Pinnacle's OWN price falls back further in women's matches (WTA, WTA
Challenger, ITF women) than in men's - women minus men -1.71c +/- 0.71 on the 5-minute drift - leaving
women's edge ~0 (-0.24c/contract) against men's +1.47c. The edge BOUGHT was identical (+1.95c); only the
share of each gap that was Pinnacle's own noise differed.

That split was found by looking at six series after the fact, so the data it came from cannot confirm it.
The FRESH column uses only signals and fills after 2026-09-29: read that one. If women's still trail men's
there, skip or down-weight women's markets; if the gap is gone, it was noise.

    python ev_gender_check.py            # from the repo root
    python ev_gender_check.py --since 2026-10-01

Read-only over EvFollowUp_*.csv, EvLive_*.csv and ev_settlements.jsonl.
"""
import argparse, csv, glob, io, json, math, os, sys

FOUND_ON = "2026-09-29"          # everything up to this day is the data the split was found in


def gender(ticker: str):
    s = ticker.split("-")[0]
    if "WTA" in s or "ITFW" in s:
        return "women"
    if "ATP" in s or s == "KXITFMATCH":
        return "men"
    return None


def fee(p: float) -> float:
    return 0.07 * p * (1 - p)


def mean_se(v):
    n = len(v)
    if n < 2:
        return n, float("nan"), float("nan")
    m = sum(v) / n
    return n, m, math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1)) / math.sqrt(n)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--since", default="", help="FRESH window start (default: the day after " + FOUND_ON + ")")
    a = ap.parse_args()
    fresh_from = a.since or "2026-09-30"

    # the curse, fast: Pinnacle's own drift by T+300, first signal per ticker+side
    drift = {}
    for f in sorted(glob.glob(os.path.join(a.root, "EvFollowUp_*.csv"))):
        with io.open(f, newline="", encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                if r.get("Decision") != "SIGNAL":
                    continue
                try:
                    age, pd = float(r["AgeSec"]), float(r["PinnacleDriftCents"])
                except (ValueError, TypeError, KeyError):
                    continue
                if abs(age - 300) > 30:
                    continue
                k = r["Ticker"] + "|" + r["Side"]
                if k not in drift or r["EntryUtc"] < drift[k][0]:
                    drift[k] = (r["EntryUtc"], pd)

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

    fills = {}
    for f in sorted(glob.glob(os.path.join(a.root, "EvLive_*.csv"))):
        with io.open(f, newline="", encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                try:
                    n = float(r.get("FillCount") or 0)
                except ValueError:
                    continue
                if n > 0 and r.get("OrderId"):
                    fills[r["OrderId"]] = r

    def summarise(when):
        """when(date_str) -> bool picks the window. Returns per-gender numbers."""
        out = {}
        for g in ("men", "women"):
            d = [v[1] for k, v in drift.items() if gender(k.split("|")[0]) == g and when(v[0][:10])]
            nd, dm, dse = mean_se(d)
            ctr = bought = sctr = resid = 0.0
            sides = {}
            nfill = 0
            for r in fills.values():
                if gender(r["Ticker"]) != g or not when(r["At"][:10]):
                    continue
                try:
                    n, p = float(r["FillCount"]), float(r["PTrue"])
                    px = float(r.get("AvgFillPrice") or 0) or float(r["LimitPrice"])
                except (ValueError, TypeError, KeyError):
                    continue
                nfill += 1
                ctr += n
                bought += n * (p - px - fee(px))
                rr = res.get(r["Ticker"])
                if rr is not None:
                    w = 1.0 if rr == r["Side"].lower() else 0.0
                    sctr += n
                    resid += n * (w - p)
                    s = sides.setdefault(r["Ticker"] + "|" + r["Side"], [0.0, 0.0])
                    s[0] += n
                    s[1] += n * p
            rvar = sum(n * n * (np_ / n) * (1 - np_ / n) for n, np_ in sides.values())
            out[g] = dict(nd=nd, dm=dm, dse=dse, fills=nfill, ctr=ctr,
                          b=100 * bought / ctr if ctr else float("nan"),
                          rs=100 * resid / sctr if sctr else float("nan"),
                          rse=100 * math.sqrt(rvar) / sctr if sctr else float("nan"))
        return out

    windows = [(f"FOUND IN (to {FOUND_ON})", lambda d: d <= FOUND_ON),
               (f"FRESH (from {fresh_from})", lambda d: d >= fresh_from),
               ("ALL", lambda d: True)]
    print("WOMEN'S vs MEN'S TENNIS  (drift = Pinnacle's own move by 5 min after a signal, - = away from us;")
    print("                          edge left = bought + drift; settled = win minus P_true on fills)\n")
    for name, when in windows:
        s = summarise(when)
        m, w = s["men"], s["women"]
        print(f"-- {name} --")
        for g, x in (("men", m), ("women", w)):
            if x["nd"] == 0 and x["fills"] == 0:
                print(f"   {g:<6} no data yet")
                continue
            print(f"   {g:<6} signals {x['nd']:>4}  drift {x['dm']:+6.2f}c ± {x['dse']:4.2f}   fills {x['fills']:>4}"
                  f"  bought {x['b']:+5.2f}c  EDGE LEFT {x['b'] + x['dm']:+5.2f}c/ctr   settled {x['rs']:+6.2f}pt ± {x['rse']:4.2f}")
        if m["nd"] >= 2 and w["nd"] >= 2:
            gap = w["dm"] - m["dm"]
            se = math.sqrt(w["dse"] ** 2 + m["dse"] ** 2)
            share = w["ctr"] / (w["ctr"] + m["ctr"]) if (w["ctr"] + m["ctr"]) else float("nan")
            print(f"   women minus men: drift {gap:+.2f}c ± {se:.2f} (t={gap / se if se else 0:+.1f})"
                  f"   women are {100 * share:.0f}% of contracts")
        print()
    print("Decide on the FRESH line: women clearly behind men there (t around -2 or beyond, same sign as the")
    print("FOUND IN line) = skip or down-weight women's markets. Gap near zero on fresh data = it was noise.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
