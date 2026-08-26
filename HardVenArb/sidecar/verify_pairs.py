#!/usr/bin/env python3
"""Verify cross_pairs.json orientation against oddspapi - are any pairs FLIPPED?

WHAT A FLIP IS, AND WHY IT IS THE WORST BUG HERE
------------------------------------------------
A pair maps a Kalshi market ticker to a Pinnacle token `{leagueId}:{matchupId}:{designation}`. If the
designation names the OPPONENT, the bot believes it is holding opposite sides of one match while both legs
actually sit on the SAME outcome. It does not look like an error: the de-vig still produces a number, the EV
still clears, and the "arb" books happily. Ten such tickers are already excluded from grading in
ev_misoriented.json, found only because their settled results were impossible.

THE CHECK IS AN ID JOIN, NOT A NAME MATCH
-----------------------------------------
Name matching is what created the flips in the first place ("Meligeni Alves" vs "Meligeni Rodrigues Alves").
oddspapi keys every outcome with a CANONICAL id that is shared across bookmakers, so the same outcome id
carries the Kalshi market ticker on one book and Pinnacle's home/away on the other:

    kalshi   market 121 outcome 121 -> bookmakerOutcomeId "KXITFMATCH-26AUG25DELKAR-DEL:yes"
    pinnacle market 121 outcome 121 -> bookmakerOutcomeId "home"

so outcome 121 IS "DEL" and IS "home", with no string comparison anywhere. That yields an authoritative
ticker -> designation map to check our pair file against.

A SECOND, INDEPENDENT SIGNAL COMES FREE
---------------------------------------
The same payload carries both books' prices. Correctly paired sides quote similar implied probabilities
(1.053 vs 1.045 in the example above); a flipped pair reads a favourite against a longshot (1.053 vs 19.23).
This cannot prove orientation on a near-coinflip - which is exactly where a flip is hardest to see - so it is
reported as corroboration, never as the verdict.

COST
----
1 /fixtures call, then 2 /odds-by-tournaments calls per 5 tournaments (the endpoint takes ONE bookmaker and
at most 5 tournamentIds). Today's slate: ~9 billable calls for the whole pair file.
"""
from __future__ import annotations
import argparse, datetime as dt, io, json, os, re, sys, time

MONEYLINE = "121"          # oddspapi market id for the two-way match winner


def load_key(env_path: str) -> str:
    for line in io.open(env_path, encoding="utf-8", errors="replace"):
        m = re.match(r'\s*(?:export\s+)?ODDSPAPI_API_KEY\s*=\s*["\']?([^"\'\s#]+)', line)
        if m:
            return m.group(1)
    raise SystemExit("ODDSPAPI_API_KEY not found in " + env_path)


def our_pairs(path: str) -> dict:
    """{kalshi_ticker: (pinnacle_mid, designation)} from the YES leg of each row."""
    out = {}
    for e in json.load(io.open(path, encoding="utf-8-sig")):
        tok = (e.get("hardven_yes_token") or "").split(":")
        tk = (e.get("kalshi_ticker") or "").strip()
        if tk and len(tok) >= 3 and tok[1]:
            out[tk] = (tok[1], tok[2])
    return out


def fetch(client, path: str, params: dict, key: str, cooldown: float):
    time.sleep(cooldown)
    r = client.get("https://api.oddspapi.io/v4" + path, params={**params, "apiKey": key})
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} on {path}: {r.text.replace(key, '***')[:200]}")
    return r.json()


def outcome_maps(row: dict) -> tuple[dict, dict, dict]:
    """(kalshi {outcome_id: ticker}, pinnacle {outcome_id: designation}, prices {book: {oid: price}})."""
    kal, pin, px = {}, {}, {"kalshi": {}, "pinnacle": {}}
    for bk, node in (row.get("bookmakerOdds") or {}).items():
        m = (node.get("markets") or {}).get(MONEYLINE)
        if not m:
            continue
        for oid, o in (m.get("outcomes") or {}).items():
            for _pid, pv in (o.get("players") or {}).items():
                boid = (pv or {}).get("bookmakerOutcomeId")
                if not boid:
                    continue
                if bk == "kalshi":
                    kal[oid] = boid.split(":")[0]          # strip the ":yes"/":no" suffix
                elif bk == "pinnacle":
                    pin[oid] = boid
                if (pv or {}).get("price"):
                    px.setdefault(bk, {})[oid] = pv["price"]
    return kal, pin, px


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--pairs", default=os.path.join(os.path.dirname(here), "cross_pairs.json"))
    ap.add_argument("--env", default=os.path.join(os.path.dirname(os.path.dirname(here)), ".env"))
    ap.add_argument("--sport-id", type=int, default=12, help="oddspapi sportId (12 = tennis)")
    ap.add_argument("--days", type=float, default=2.0, help="forward window for /fixtures")
    # LOOK BACKWARDS TOO. A `from=now` window silently drops every match already in progress, which on an
    # afternoon run is most of the day's slate - the first full run verified only 81 of 173 pairs for that
    # reason alone. In-progress pairs are precisely the ones currently exposed, so they matter most.
    ap.add_argument("--back-hours", type=float, default=14.0,
                    help="also look this far BACK, so in-progress matches are covered (default 14)")
    ap.add_argument("--max-tournaments", type=int, default=0,
                    help="cap tournaments checked (0 = all); each 5 costs 2 billable calls")
    ap.add_argument("--json-out", default="", help="write the findings to this path")
    a = ap.parse_args()

    import httpx
    key = load_key(a.env)
    ours = our_pairs(a.pairs)
    print(f"[VERIFY] {len(ours)} pair(s) in {os.path.basename(a.pairs)}")

    with httpx.Client(timeout=90.0) as c:
        now = dt.datetime.now(dt.timezone.utc)
        fx = fetch(c, "/fixtures", {
            "sportId": a.sport_id,
            "from": (now - dt.timedelta(hours=a.back_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": (now + dt.timedelta(days=a.days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bookmakers": "pinnacle,kalshi", "hasOdds": "true"}, key, 2.3)
        by_fid = {f["fixtureId"]: f for f in fx if (f.get("externalProviders") or {}).get("pinnacleId")}
        mids = {str(f["externalProviders"]["pinnacleId"]) for f in by_fid.values()}
        want_mids = {m for m, _ in ours.values()}
        # RANK TOURNAMENTS BY HOW MANY OF *OUR* PAIRS THEY HOLD. Checking the whole board would spend calls
        # on fixtures we do not trade; the pair file is the only thing that decides what is worth verifying.
        rank: dict = {}
        for f in by_fid.values():
            if str(f["externalProviders"]["pinnacleId"]) in want_mids:
                rank[str(f.get("tournamentId"))] = rank.get(str(f.get("tournamentId")), 0) + 1
        tids = [t for t, _ in sorted(rank.items(), key=lambda kv: -kv[1])]
        if a.max_tournaments:
            tids = tids[:a.max_tournaments]
        print(f"[VERIFY] {len(mids)} fixture(s) carry a pinnacleId; {len(want_mids & mids)} match our pairs, "
              f"across {len(tids)} tournament(s) -> {1 + 2 * ((len(tids) + 4) // 5)} billable call(s)")

        rows: dict = {}
        for bk in ("kalshi", "pinnacle"):
            for i in range(0, len(tids), 5):
                chunk = tids[i:i + 5]
                data = fetch(c, "/odds-by-tournaments",
                             {"tournamentIds": ",".join(chunk), "bookmaker": bk}, key, 1.3)
                for r in (data if isinstance(data, list) else (data.get("data") or [])):
                    fid = r.get("fixtureId")
                    if not fid:
                        continue
                    tgt = rows.setdefault(fid, {"fixtureId": fid, "bookmakerOdds": {}})
                    tgt["bookmakerOdds"].update(r.get("bookmakerOdds") or {})

    ok = flipped = unchecked = 0
    findings = []
    for fid, row in rows.items():
        f = by_fid.get(fid)
        if not f:
            continue
        mid = str((f.get("externalProviders") or {}).get("pinnacleId"))
        kal, pin, px = outcome_maps(row)
        if not kal or not pin:
            continue
        truth = {kal[oid]: pin[oid] for oid in kal.keys() & pin.keys()}      # ticker -> designation
        for tk, want in truth.items():
            if tk not in ours:
                continue
            got_mid, got_des = ours[tk]
            if got_mid != mid:
                unchecked += 1
                continue                                   # different matchup id: a pairing question, not orientation
            if got_des == want:
                ok += 1
            else:
                flipped += 1
                oid = next((o for o in kal if kal[o] == tk), None)
                findings.append({
                    "ticker": tk, "matchup": mid, "ours": got_des, "oddspapi": want,
                    "fixture": f"{f.get('participant1Name')} vs {f.get('participant2Name')}",
                    "kalshi_price": px.get("kalshi", {}).get(oid),
                    "pinnacle_price": px.get("pinnacle", {}).get(oid)})

    print(f"\n[VERIFY] AGREE {ok}   FLIPPED {flipped}   not comparable {unchecked}")
    for x in findings:
        print(f"   *** FLIPPED *** {x['ticker']}")
        print(f"       {x['fixture']}  (matchup {x['matchup']})")
        print(f"       we say '{x['ours']}', oddspapi says '{x['oddspapi']}'"
              f"   kalshi {x['kalshi_price']} vs pinnacle {x['pinnacle_price']}")
    if not findings and ok:
        print("   No orientation errors found in the checked set.")
    if a.json_out:
        io.open(a.json_out, "w", encoding="utf-8").write(json.dumps(
            {"at": dt.datetime.now().isoformat(), "agree": ok, "flipped": flipped,
             "not_comparable": unchecked, "findings": findings}, indent=1))
        print(f"[VERIFY] wrote {a.json_out}")
    return 2 if flipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
