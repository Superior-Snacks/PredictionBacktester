#!/usr/bin/env python3
"""Retroactively check whether PAST signals were traded on a flipped pair.

WHY THIS IS A DIFFERENT PROBLEM FROM verify_pairs.py
-----------------------------------------------------
`verify_pairs.py` checks the LIVE pair file, which governs what the bot does tonight. It says nothing about
the signals already logged: `cross_pairs.json` is rewritten every 90 minutes, so the pairs that produced the
08-22..08-24 signals are gone. Measured 2026-08-25: only 11 of 104 signal tickers were still in the live
file, leaving 82 settled tennis signals - the dataset the entire edge estimate rests on - unverified.

WHY THE ID JOIN CANNOT BE USED HERE
------------------------------------
The live check works because `/odds` carries `bookmakerOutcomeId` ("KX...-DEL:yes", "home"), which names each
canonical outcome on both books. `/v4/historical-odds` STRIPS those ids - it returns only price series keyed
by outcome - and a finished fixture has no current odds to ask instead. So the authoritative join is
unavailable retroactively and something else is needed.

THE PRICE FINGERPRINT
---------------------
Our own telemetry supplies the missing half. Each signal row records `PinOddsMine` and `KalshiRestAsk` AT A
KNOWN TIMESTAMP, so those prices identify which outcome we were on:

    pinnacle history at t -> outcome 121 @ 1.787, outcome 122 @ 2.05   PinOddsMine 1.7874 -> we held 121
    kalshi   history at t -> outcome 121 @ 2.083, outcome 122 @ 1.923  KalshiRestAsk 0.48 -> we held 121
                                                                       same id => correctly oriented

Outcome ids are canonical ACROSS bookmakers, so landing on the same id means both legs named the same
player. Landing on different ids is a flip. No name comparison anywhere, and no dependence on our own pair
file being intact - the check is our recorded PRICES against the venue's own history.

IT AUDITS THE ORACLE FOR FREE
------------------------------
If `PinOddsMine` matches no Pinnacle price at that moment, the pairing is not what is wrong - our recorded
fair value is. That is reported separately as ORACLE_MISMATCH, because it invalidates a signal just as
thoroughly and has never been checkable from outside before.

COVERAGE CEILING - READ THIS BEFORE TRUSTING A CLEAN RESULT
-----------------------------------------------------------
A signal is only checkable if (a) `pair_ledger.jsonl` recorded the token we used, and (b) oddspapi maps that
Pinnacle matchup id. On 2026-08-25 that was 29 of 83 settled tennis signals: 25 predate the ledger, and only
332 of 775 past fixtures carry a `pinnacleId`. A clean result therefore means "no flips among the third we
can see", never "the dataset is clean".

`/v4/historical-odds` is UNMETERED, so the per-signal work is free; only the `/fixtures` mapping calls bill.
Do NOT pass hasOdds=true when fetching past fixtures - it means "has odds NOW" and hides finished ones
(measured: 13 fixtures returned for 08-24 with the filter, 259 without).
"""
from __future__ import annotations
import argparse, csv, datetime as dt, glob, io, json, os, re, sys, time

MONEYLINE = "121"


def load_key(p: str) -> str:
    for line in io.open(p, encoding="utf-8", errors="replace"):
        m = re.match(r'\s*(?:export\s+)?ODDSPAPI_API_KEY\s*=\s*["\']?([^"\'\s#]+)', line)
        if m:
            return m.group(1)
    raise SystemExit("ODDSPAPI_API_KEY not found in " + p)


def is_tennis(t: str) -> bool:
    u = (t or "").upper()
    return ("ATP" in u) or ("WTA" in u) or ("ITF" in u)


def settled_tickers(path: str) -> set:
    out = set()
    for line in io.open(path, encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if (d.get("result") or "").strip() in ("yes", "no"):
            out.add(d["ticker"])
    return out


def first_signals(glob_pat: str) -> dict:
    """{ticker: row} for the EARLIEST signal per ticker - the one the calibration grades."""
    out = {}
    for f in sorted(glob.glob(glob_pat)):
        for r in csv.DictReader(io.open(f, encoding="utf-8", errors="replace")):
            if r.get("Decision", "").strip() != "SIGNAL":
                continue
            tk, ts = r.get("Ticker", "").strip(), r.get("Timestamp", "")
            if tk and (tk not in out or ts < out[tk].get("Timestamp", "")):
                out[tk] = r
    return out


def ledger_tokens(path: str) -> dict:
    out = {}
    if not os.path.exists(path):
        return out
    for line in io.open(path, encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        tok = d.get("yes_token") or ""
        if tok.count(":") >= 2 and d.get("ticker"):
            out.setdefault(d["ticker"], tok)
    return out


def price_at(series, when: dt.datetime):
    """Last quoted price at or before `when` - what the book actually showed when we acted."""
    best = None
    for p in series or []:
        try:
            ts = dt.datetime.fromisoformat(str(p.get("createdAt")).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue
        if ts <= when and (best is None or ts > best[0]):
            best = (ts, p.get("price"))
    return best[1] if best else None


def outcome_prices(hist: dict, book: str, when: dt.datetime) -> dict:
    out = {}
    mk = ((hist.get("bookmakers") or {}).get(book) or {}).get("markets") or {}
    for oid, o in ((mk.get(MONEYLINE) or {}).get("outcomes") or {}).items():
        for _pid, series in (o.get("players") or {}).items():
            p = price_at(series, when)
            if p:
                out[oid] = float(p)
    return out


def closest(prices: dict, target: float, tol: float):
    """Outcome id whose price is nearest `target` - or None when the answer is not UNAMBIGUOUS.

    RETURNING A GUESS HERE MANUFACTURES FLIPS. On a true coinflip both sides quote the same number, so the
    fingerprint cannot say which one we held; the first cut returned whichever it saw first and duly
    reported KXATPCHALLENGERMATCH-26AUG24KUZFAN-FAN as FLIPPED off Pinnacle prices of {121: 1.909,
    122: 1.909} against our logged 1.9091. That is the same near-coinflip blind spot that price-based
    inversion detection has always had, and it is the one place a flip is hardest to see - so the honest
    answer is "cannot judge", never a coin toss dressed as a verdict.

    Two candidates both inside `tol` therefore yields None, as does nothing inside it at all.
    """
    scored = sorted((abs(p - target) / max(target, 1e-9), oid) for oid, p in prices.items())
    if not scored or scored[0][0] > tol:
        return None
    if len(scored) > 1 and scored[1][0] <= tol:
        return None                                    # both plausible -> ambiguous, not a verdict
    return scored[0][1], scored[0][0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    ap.add_argument("--root", default=root)
    ap.add_argument("--env", default=os.path.join(root, ".env"))
    ap.add_argument("--ledger", default=os.path.join(root, "pair_ledger.jsonl"))
    ap.add_argument("--sport-id", type=int, default=12)
    ap.add_argument("--from", dest="frm", default="", help="fixtures window start (default: 5 days ago)")
    ap.add_argument("--to", dest="to", default="", help="fixtures window end (default: tomorrow)")
    # TWO TOLERANCES, because the two comparisons are not alike. `PinOddsMine` was copied from Pinnacle,
    # so it should match Pinnacle's own history almost exactly. The Kalshi side compares OUR recorded ask
    # against oddspapi's stored quote - different snapshot instants, and possibly mid vs ask - which was
    # measured 3.3% apart on a real row and failed a 2% gate.
    ap.add_argument("--tol", type=float, default=0.02, help="relative tolerance for the PINNACLE match")
    ap.add_argument("--tol-kalshi", type=float, default=0.08, help="relative tolerance for the KALSHI match")
    ap.add_argument("--limit", type=int, default=0, help="stop after N signals (0 = all)")
    ap.add_argument("--json-out", default="")
    a = ap.parse_args()

    import httpx
    key = load_key(a.env)
    sig = first_signals(os.path.join(a.root, "EvTelemetry_*.csv"))
    settled = settled_tickers(os.path.join(a.root, "ev_settlements.jsonl"))
    led = ledger_tokens(a.ledger)
    todo = {t: r for t, r in sig.items() if is_tennis(t) and t in settled and t in led}
    print(f"[SIGVERIFY] {len(sig)} signal ticker(s); {len(todo)} settled tennis with a recorded token")

    now = dt.datetime.now(dt.timezone.utc)
    frm = a.frm or (now - dt.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to = a.to or (now + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with httpx.Client(timeout=120.0) as c:
        # NO hasOdds FILTER - it hides finished fixtures, which are exactly the ones being verified.
        fx = c.get("https://api.oddspapi.io/v4/fixtures",
                   params={"sportId": a.sport_id, "from": frm, "to": to, "apiKey": key}).json()
        by_mid = {str((f.get("externalProviders") or {}).get("pinnacleId")): f
                  for f in fx if (f.get("externalProviders") or {}).get("pinnacleId")}
        print(f"[SIGVERIFY] {len(fx)} past fixture(s), {len(by_mid)} with a pinnacleId")

        checkable = {t: r for t, r in todo.items() if led[t].split(":")[1] in by_mid}
        print(f"[SIGVERIFY] retro-verifiable: {len(checkable)} of {len(todo)}"
              f"   ({len(todo) - len(checkable)} have no oddspapi mapping)\n")

        ok = flip = oracle_bad = nodata = 0
        findings = []
        items = sorted(checkable.items(), key=lambda kv: kv[1].get("Timestamp", ""))
        if a.limit:
            items = items[:a.limit]
        for tk, row in items:
            mid = led[tk].split(":")[1]
            fid = by_mid[mid]["fixtureId"]
            try:
                when = dt.datetime.fromisoformat(row["Timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                continue
            time.sleep(5.4)                        # documented cooldown; the endpoint itself is unmetered
            try:
                h = c.get("https://api.oddspapi.io/v4/historical-odds",
                          params={"fixtureId": fid, "bookmakers": "pinnacle,kalshi", "apiKey": key}).json()
            except Exception as ex:
                nodata += 1
                print(f"   ?  {tk[:44]:<44} fetch failed ({type(ex).__name__})")
                continue
            pin = outcome_prices(h, "pinnacle", when)
            kal = outcome_prices(h, "kalshi", when)
            try:
                mine = float(row.get("PinOddsMine") or 0)
                ask = float(row.get("KalshiRestAsk") or 0)
            except ValueError:
                mine = ask = 0
            if not pin or not kal or mine <= 1 or ask <= 0:
                nodata += 1
                print(f"   ?  {tk[:44]:<44} no comparable prices at {when:%m-%d %H:%M}")
                continue
            p_hit = closest(pin, mine, a.tol)
            k_hit = closest(kal, 1.0 / ask, a.tol_kalshi)   # Kalshi ask -> decimal odds
            if p_hit is None:
                # AMBIGUOUS IS NOT THE SAME AS WRONG. `closest` returns None both when nothing matches AND
                # when two outcomes match equally well - and on a coinflip the second case is the norm, not a
                # fault. Reporting it as ORACLE_MISMATCH accused a perfectly good row of a 0.005% error
                # (KUZFAN: logged 1.9091 against history of [1.909, 1.909]). Separate them by asking whether
                # ANY price was in range at all.
                near = [oid for oid, p in pin.items() if abs(p - mine) / max(mine, 1e-9) <= a.tol]
                if len(near) > 1:
                    nodata += 1
                    print(f"   ?  {tk[:44]:<44} Pinnacle sides tied at {sorted(set(pin.values()))} "
                          f"- cannot identify our side")
                    continue
                oracle_bad += 1
                findings.append({"ticker": tk, "kind": "ORACLE_MISMATCH", "pin_ours": mine,
                                 "pin_history": pin, "at": row["Timestamp"]})
                print(f"   !! {tk[:44]:<44} ORACLE_MISMATCH: we logged {mine}, history had {list(pin.values())}")
                continue
            if k_hit is None:
                nodata += 1
                print(f"   ?  {tk[:44]:<44} Kalshi side not identifiable (ask {ask})")
                continue
            # THE EXPECTED RELATION FLIPS WITH THE SIDE, and getting this backwards manufactures a flip on
            # every NO signal. `PinOddsMine` is NOT "the odds for the side we took" - EvEvaluator.cs:460 sets
            # `mine = quotes[pair.YesLegIndex]`, i.e. ALWAYS the YES leg, whatever side is being screened.
            # `KalshiRestAsk` does follow the side. So on a NO signal the two fingerprints land on OPPOSITE
            # outcome ids when everything is correct, and on the SAME id when the pair is flipped. Reading
            # "different ids" as a flip reported KXWTAMATCH-26AUG24AVALAZ-AVA and two others as flipped when
            # all three were sound.
            same = (p_hit[0] == k_hit[0])
            correct = same if (row.get("Side", "").strip().upper() == "YES") else (not same)
            if correct:
                ok += 1
            else:
                flip += 1
                findings.append({"ticker": tk, "kind": "FLIPPED", "side": row.get("Side"),
                                 "pinnacle_outcome": p_hit[0],
                                 "kalshi_outcome": k_hit[0], "at": row["Timestamp"],
                                 "pin_ours": mine, "kalshi_ask": ask})
                print(f"   *** FLIPPED *** {tk}  (side {row.get('Side','?')})")
                print(f"       at {row['Timestamp'][:19]}: Pinnacle YES-leg price sat on outcome {p_hit[0]}, "
                      f"our Kalshi {row.get('Side','?')} ask on outcome {k_hit[0]}")

        print(f"\n[SIGVERIFY] CORRECT {ok}   FLIPPED {flip}   ORACLE_MISMATCH {oracle_bad}   "
              f"not comparable {nodata}")
        if not flip and ok:
            print(f"   No flipped signals among the {ok} that could be checked.")
            print(f"   NOTE: {len(todo) - len(checkable)} settled tennis signals have no oddspapi mapping and")
            print(f"   {len(sig) - len(todo)} more lack a ledger token - this is not a clean bill for the whole set.")
    if a.json_out:
        io.open(a.json_out, "w", encoding="utf-8").write(json.dumps(
            {"at": dt.datetime.now().isoformat(), "correct": ok, "flipped": flip,
             "oracle_mismatch": oracle_bad, "not_comparable": nodata,
             "checkable": len(checkable), "candidates": len(todo), "findings": findings}, indent=1))
        print(f"[SIGVERIFY] wrote {a.json_out}")
    return 2 if flip else 0


if __name__ == "__main__":
    raise SystemExit(main())
