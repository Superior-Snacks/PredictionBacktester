"""cricket_live_probe.py - READ-ONLY one-off (2026-11-23): does Pinnacle price CRICKET in play, and how?

The cricket shadow got 0 in-play rows in its first 4 days: every paired match was priced only until its start. Two
explanations fit: (a) Pinnacle does not price these matches in play at all, or (b) it prices them on the SAME listing
past its betting-close time (cutoffAt), which pinnacle_adapter drops by design - it was built for tennis, whose in-play
prices live on a separate child listing. A third would be (c) a separate in-play listing the sidecar failed to link.

LESSON 2026-11-24: AN OPEN MONEYLINE AFTER THE START IS NOT IN-PLAY EVIDENCE. China v Indonesia's pre-match listing
stayed open 2h past its start - status pending, never flagged live, Pinnacle's cricket live board empty - at exactly
its pre-match price in every read. The first version of verdict() called that "priced in play on the same listing" and
recommended a cutoffAt pass-through, which would have fed the shadow a frozen price. The verdict now needs Pinnacle's
own live flags (isLive / liveMode / the live board) or a price that moves; see live_flags().

This watches named matches around their start with a few Pinnacle guest-API reads - 4 requests per read at ~1/s, reads
at start -10, +10, +30, +60 and +120 min - and says which it is. For a paired match it also records what the local
sidecar holds (/debug/redirect, asks Pinnacle nothing) and, at the end, how many in-play rows the bot logged.

    pythonw -I helpers/cricket_live_probe.py          # the matches below, sleeping until each (run detached)
    python  -I helpers/cricket_live_probe.py --now    # one read now, to test the code path (4 requests)

Writes cricket_live_probe.txt (readable) and cricket_live_probe.jsonl (raw reads) at the repo root, and posts each
match's verdict to Discord (DISCORD_WEBHOOK_URL from .env) so the answer arrives without anyone watching.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import httpx

R = r"C:\Users\jonin\dev\projects\PredictionBacktester"
# UNDER pythonw (how the scheduled task runs it, so no window can steal the sidecar browser's focus) there is no
# stdout/stderr, and the sidecar's schedule.py calls sys.stdout.reconfigure() at import - which crashed the run with
# nothing logged (found by testing the task's exact command, 2026-11-23). Give both a sink first.
for _stream in ("stdout", "stderr"):
    if getattr(sys, _stream) is None:
        setattr(sys, _stream, open(os.devnull, "w", encoding="utf-8"))
sys.path.insert(0, os.path.join(R, "HardVenArb", "sidecar"))
import schedule as sched  # noqa: E402  (the sidecar's guest-API base and public key)

TXT = os.path.join(R, "cricket_live_probe.txt")
RAW = os.path.join(R, "cricket_live_probe.jsonl")
SIDECAR = "http://127.0.0.1:8787"
OFFSETS_MIN = (-10, 10, 30, 60, 120)
MATCHES = [
    {"name": "China v Indonesia (women's T20I - not on Kalshi; shows Pinnacle's behaviour)",
     "mid": 1640992318, "start": "2026-11-24T02:00:00Z", "token": None, "kalshi": None},
    {"name": "WBBL Melbourne Renegades v Brisbane Heat (paired: the bot is watching it)",
     "mid": 1640630256, "start": "2026-11-25T08:10:00Z", "token": "216411:1640630256:away",
     "kalshi": "KXWT20MATCH-26NOV250310BHWMRW"},
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def log(msg: str) -> None:
    line = f"{utcnow():%Y-%m-%d %H:%M:%S}Z  {msg}"
    with open(TXT, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    try:
        print(line, flush=True)
    except Exception:
        pass                                   # pythonw has no console


def webhook() -> str:
    url = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    if url:
        return url
    p = os.path.join(R, ".env")
    if os.path.exists(p):
        for line in io.open(p, encoding="utf-8", errors="replace"):
            m = re.match(r'\s*(?:export\s+)?DISCORD_WEBHOOK_URL\s*=\s*["\']?([^"\'\s#]+)', line)
            if m:
                return m.group(1)
    return ""


def discord(msg: str) -> None:
    url = webhook()
    if not url:
        log("[discord] DISCORD_WEBHOOK_URL not set - verdict only in this file")
        return
    try:
        with httpx.Client(timeout=20.0) as c:          # httpx, not urllib: Cloudflare blocks Python-urllib
            r = c.post(url, json={"content": msg[:1900]})
        log(f"[discord] posted (HTTP {r.status_code})")
    except Exception as e:
        log(f"[discord] {type(e).__name__}: {e}")


class Guest:
    def __init__(self) -> None:
        self.c = httpx.Client(headers={"accept": "application/json", "x-api-key": sched.GUEST_KEY,
                                       "origin": "https://www.pinnacle.bet", "user-agent": "Mozilla/5.0"},
                              timeout=30.0, follow_redirects=True)

    def get(self, path: str):
        try:
            r = self.c.get(sched.GUEST_BASE + path)
            out = r.json() if r.status_code == 200 else {"_http": r.status_code}
        except Exception as e:
            out = {"_error": f"{type(e).__name__}: {e}"}
        time.sleep(1.2)                                 # gentle: about one request a second
        return out


def names_of(x: dict) -> frozenset:
    return frozenset((p.get("name") or "").strip().lower() for p in (x.get("participants") or []) if p.get("name"))


def ml_of(markets: list, mid: int, src: str) -> list[dict]:
    out = []
    for k in markets if isinstance(markets, list) else []:
        if k.get("matchupId") == mid and k.get("type") == "moneyline" and k.get("period") == 0:
            out.append({"src": src, "status": k.get("status"), "cutoffAt": k.get("cutoffAt"),
                        "alt": k.get("isAlternate"),
                        "prices": sorted((p.get("designation"), p.get("price")) for p in (k.get("prices") or []))})
    return out


def read_all(g: Guest, matches: list[dict]) -> dict:
    """ONE read of the cricket board (4 requests), then each watched match's listings pulled out of it."""
    at = utcnow()
    ms = g.get("/sports/8/matchups?withSpecials=false&brandId=0")
    mk = g.get("/sports/8/markets/straight?primaryOnly=false&withSpecials=false")
    lms = g.get("/sports/8/matchups/live?brandId=0")
    lmk = g.get("/sports/8/markets/live/straight?primaryOnly=false&withSpecials=false")
    errs = {k: v for k, v in (("matchups", ms), ("markets", mk), ("live_matchups", lms), ("live_markets", lmk))
            if not isinstance(v, list)}
    board = (ms if isinstance(ms, list) else []) + (lms if isinstance(lms, list) else [])
    out = {"at": at.isoformat(), "errors": errs, "board_size": len(ms) if isinstance(ms, list) else None,
           "live_board_size": len(lms) if isinstance(lms, list) else None,
           # who IS in play - so a live listing the linking below misses (other id, no parentId, other names) still
           # shows up in the verdict instead of reading as "not priced in play"
           "live_board": [{"id": x.get("id"), "parentId": x.get("parentId"), "names": sorted(names_of(x)),
                           "units": x.get("units"), "liveMode": x.get("liveMode"), "start": x.get("startTime")}
                          for x in (lms if isinstance(lms, list) else [])],
           "matches": {}}
    for m in matches:
        target = next((x for x in board if x.get("id") == m["mid"]), None)
        if target is not None:
            m.setdefault("names", names_of(target))
            if target.get("startTime") and target.get("startTime") != m["start"]:
                m["moved_to"] = target.get("startTime")
        nm = m.get("names") or frozenset()
        st = iso(m.get("moved_to") or m["start"])
        seen, rel = set(), []
        for x in board:
            xid = x.get("id")
            twin = (nm and names_of(x) == nm and st and iso(x.get("startTime"))
                    and abs((iso(x.get("startTime")) - st).total_seconds()) < 12 * 3600)
            if xid in seen or not (xid == m["mid"] or x.get("parentId") == m["mid"] or twin):
                continue
            seen.add(xid)
            rel.append({"id": xid, "role": "target" if xid == m["mid"] else "child" if x.get("parentId") == m["mid"]
                        else "twin", "parentId": x.get("parentId"), "type": x.get("type"), "units": x.get("units"),
                        "liveMode": x.get("liveMode"), "isLive": x.get("isLive"), "status": x.get("status"),
                        "startTime": x.get("startTime"), "on_live_board": any(y.get("id") == xid for y in
                                                                           (lms if isinstance(lms, list) else [])),
                        "ml": ml_of(mk, xid, "straight") + ml_of(lmk, xid, "live")})
        side = None
        if m.get("token"):
            try:
                with httpx.Client(timeout=5.0) as c:
                    r = c.get(SIDECAR + "/debug/redirect", params={"sid": m["token"]})
                side = r.json() if r.status_code == 200 else {"_http": r.status_code}
            except Exception as e:
                side = {"_error": f"{type(e).__name__}: {e}"}
        out["matches"][str(m["mid"])] = {"listings": rel, "sidecar": side}
    with open(RAW, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(out, default=str) + "\n")
    return out


def describe(m: dict, snap: dict) -> str:
    info = snap["matches"].get(str(m["mid"])) or {}
    rel = info.get("listings") or []
    if not rel:
        return "no listing for it on Pinnacle's cricket board"
    at = iso(snap["at"])
    parts = []
    for x in rel:
        mls = "; ".join(f"{k['src']} ML {k['status']} cutoff "
                        + ("absent" if not k["cutoffAt"] else
                           f"{'past' if iso(k['cutoffAt']) <= at else 'in'} "
                           f"{abs((iso(k['cutoffAt']) - at).total_seconds()) / 60:.0f}m")
                        + f" {k['prices']}" for k in x["ml"]) or "no moneyline"
        parts.append(f"{x['role']} {x['id']} (units {x['units']}, liveMode {x['liveMode']}, isLive {x['isLive']}, "
                     f"live board {x['on_live_board']}): {mls}")
    s = " | ".join(parts)
    sc = info.get("sidecar")
    if isinstance(sc, dict) and "parent" in sc:
        p = sc.get("parent") or {}
        # league_last_live_push_s is the tell: non-null = the sidecar RECEIVED in-play pushes for this league
        s += (f"  || sidecar: parent {p.get('status')} live={p.get('live')} odds={p.get('odds')} "
              f"cutoff_in_s={p.get('cutoff_in_s')} age_s={p.get('age_s')}; league_last_live_push_s="
              f"{sc.get('league_last_live_push_s')}; parentid_link={sc.get('parentid_link')}; "
              f"players_link={sc.get('players_link')}; redirect={sc.get('redirect')}; "
              f"same names in league={len(sc.get('same_players_in_league') or [])}")
    elif sc is not None:
        s += f"  || sidecar: {str(sc)[:200]}"
    return s


def bot_inplay_rows(kalshi: str, day: datetime) -> int | None:
    fn = os.path.join(R, f"EvShadowTelemetry_{day:%Y%m%d}.csv")
    if not os.path.exists(fn):
        return None
    n = 0
    with open(fn, newline="", encoding="utf-8") as fh:
        r = csv.reader(fh)
        h = next(r, None) or []
        ix = {k: i for i, k in enumerate(h)}
        for x in r:
            if len(x) >= len(h) and kalshi in x[ix["Ticker"]] and x[ix["InPlay"]] == "1":
                n += 1
    return n


def live_flags(x: dict) -> list[str]:
    """Pinnacle's own signs that a listing is IN PLAY - with a price that moves, the only evidence verdict() takes.
    An open moneyline after the start is NOT one (see the module's LESSON 2026-11-24). The matchup's `status` is not
    used either: the sidecar never reads it, and China v Indonesia sat at "pending" 2h after its start."""
    return [f for f, on in (("isLive", x.get("isLive")), (f"liveMode {x.get('liveMode')}", x.get("liveMode")),
                            ("on the live board", x.get("on_live_board"))) if on]


def fmt_px(p) -> str:
    return " / ".join(f"{d} {v:+}" if isinstance(v, (int, float)) else f"{d} {v}" for d, v in p)


def sidecar_note(m: dict, post: list[dict]) -> str:
    """For a paired match: did the running sidecar follow the token to the in-play listing (/debug/redirect)?"""
    if not m.get("token"):
        return "Not paired, so the sidecar is not following it."
    reds = [((s["matches"].get(str(m["mid"])) or {}).get("sidecar") or {}) for s in post]
    got = [r.get("redirect") for r in reds if isinstance(r, dict) and r.get("redirect")]
    if got:
        return f"The sidecar followed it (redirect -> {got[-1]})."
    return "The sidecar did NOT follow it (no redirect in any read) - a linking fix."


def verdict(m: dict, snaps: list[dict]) -> str:
    """What Pinnacle did with this match after its start. Order matters: a separate live listing first (tennis and
    esports work that way, and a stale parent can sit next to it), then the same listing WITH live evidence, then
    the cases where nothing went live."""
    start = iso(m.get("moved_to") or m["start"])
    pre = [s for s in snaps if iso(s["at"]) < start]
    post = [s for s in snaps if iso(s["at"]) >= start + timedelta(minutes=5)]
    if not post:
        return "NO READ AFTER THE START (the match moved or the probe stopped early) - nothing learned"

    def rel(s):
        return (s["matches"].get(str(m["mid"])) or {}).get("listings") or []

    def opened(x):
        return [k for k in x["ml"] if k["status"] == "open" and k["prices"]]

    def px(k):
        return tuple(map(tuple, k["prices"]))

    seen: dict = {}                        # listing id -> what it showed after the start, in read order
    for s in post:
        for x in rel(s):
            e = seen.setdefault(x["id"], {"x": x, "prices": [], "flags": set(), "cut": set(), "status": set(),
                                          "reads": 0})
            e["x"] = x
            e["flags"].update(live_flags(x))
            e["status"].add(str(x.get("status")))
            ks = opened(x)
            e["reads"] += bool(ks)
            for k in ks:
                e["prices"].append(px(k))
                e["cut"].add("absent" if not k["cutoffAt"] else
                             "past" if iso(k["cutoffAt"]) <= iso(s["at"]) else "future")
    base = {px(k) for s in pre for x in rel(s) if x["role"] == "target" for k in opened(x)}

    def moved(e) -> bool:
        if not e["prices"]:
            return False
        ref = base if e["x"]["role"] == "target" and base else {e["prices"][0]}
        return any(p not in ref for p in e["prices"])

    def evidence(e) -> str:
        return ", ".join(sorted(e["flags"]) + (["price moved"] if moved(e) else []))

    # Who was in play at all - a live listing whose names share this match's name words ("brisbane heat w" next to
    # "brisbane heat") is one the linking above (parentId / exact names) would miss, so it must not read as "not
    # priced". Two shared words (or all of a one-word side's) keep a mere shared city from counting.
    def words(ns) -> set:
        return {w for n in ns for w in re.findall(r"[a-z]+", str(n).lower()) if len(w) >= 4 and w not in
                ("women", "womens")}
    mine = words(m.get("names") or ())
    near = {}
    for s in post:
        for y in s.get("live_board") or []:
            if mine and y.get("id") not in seen and len(words(y.get("names") or ()) & mine) >= min(2, len(mine)):
                near[y.get("id")] = y
    sizes = [b for b in (s.get("live_board_size") for s in post) if b is not None]
    board_note = ("Pinnacle's cricket live board was EMPTY at every read after the start" if sizes and not any(sizes)
                  else f"Pinnacle's cricket live board held up to {max(sizes)} matchup(s)" if sizes
                  else "Pinnacle's live board could not be read")
    if near:
        y = next(iter(near.values()))
        board_note += (f", and {y.get('id')} ({' / '.join(y.get('names') or [])}, liveMode {y.get('liveMode')}) on it "
                       "shares this match's names but is not linked to it")

    tgt = seen.get(m["mid"])
    others = [e for i, e in seen.items() if i != m["mid"] and e["prices"]]
    live_other = [e for e in others if e["flags"] or moved(e)]
    if live_other:
        e = live_other[0]
        x = e["x"]
        return (f"PRICED IN PLAY ON A SEPARATE LISTING - {x['role']} {x['id']} (units {x['units']}) had an open "
                f"moneyline after the start in {e['reads']} read(s) ({evidence(e)}). {sidecar_note(m, post)}")
    if tgt and tgt["prices"] and (tgt["flags"] or moved(tgt)):
        if tgt["flags"]:
            cut = sorted(tgt["cut"])
            return (f"PRICED IN PLAY ON THE SAME LISTING - its moneyline was open after the start in {tgt['reads']} "
                    f"read(s) ({evidence(tgt)}), cutoffAt {'/'.join(cut)}. "
                    + ("The sidecar's cutoffAt gate drops exactly this: a cricket-only pass-through would fix it."
                       if "past" in cut else "cutoffAt is not past, so the cutoff gate is NOT what blocks it - look "
                                             "at the sidecar's live subscription for cricket."))
        first = next(iter(base)) if base else tgt["prices"][0]
        return (f"PRICE MOVED AFTER THE START BUT NO LIVE FLAG - the pre-match listing stayed open and its price "
                f"went {fmt_px(first)} -> {fmt_px(tgt['prices'][-1])} while Pinnacle never flagged it live: in play on "
                "the same listing, or a delayed start being re-priced. Check when the match really started before "
                f"building anything; {board_note}.")
    shut = [e for e in seen.values() if e["flags"] and not e["prices"]]
    if shut:
        e = shut[0]
        return (f"LIVE BUT NO OPEN MONEYLINE - Pinnacle flagged {e['x']['role']} {e['x']['id']} live "
                f"({', '.join(sorted(e['flags']))}) but its moneyline was suspended or missing at every read: too few "
                "reads to tell, re-run with more.")
    if near:
        y = next(iter(near.values()))
        return (f"MAYBE PRICED IN PLAY ON AN UNLINKED LISTING - {y.get('id')} ({' / '.join(y.get('names') or [])}, "
                f"liveMode {y.get('liveMode')}) was on Pinnacle's live board after the start and shares this match's "
                "names, but it is not this match's child and its names differ, so nothing here links it ("
                + ("this match's own listing stayed up, frozen" if tgt and tgt["prices"] else "this match's own "
                   "listing closed") + "). Check it by hand before pulling cricket.")
    if tgt and tgt["prices"]:
        last = tgt["prices"][-1]
        return (f"NOT PRICED IN PLAY - the pre-match listing stayed up after the start but never went live: status "
                f"{'/'.join(sorted(tgt['status']))}, isLive / liveMode / live board all off in {tgt['reads']} read(s), "
                f"price never moved ({fmt_px(last)}{', the pre-match price' if last in base else ''}); {board_note}. "
                "A stale listing, not an in-play price: the sidecar's cutoffAt gate is right to drop it, and a "
                "pass-through would feed the shadow a frozen price.")
    if others:
        e = others[0]
        return (f"NOT PRICED IN PLAY - {e['x']['role']} {e['x']['id']} stayed open after the start but never went "
                f"live and its price never moved ({fmt_px(e['prices'][-1])}); {board_note}.")
    return ("NOT PRICED IN PLAY - after the start no open moneyline on this match anywhere on the board (the "
            f"original listing closed and nothing replaced it); {board_note}.")


def probe(matches: list[dict], now_only: bool) -> None:
    g = Guest()
    if now_only:
        snap = read_all(g, matches)
        log(f"[test] one read: board {snap['board_size']} matchups, live board {snap['live_board_size']}, "
            f"errors {snap['errors'] or 'none'}")
        for m in matches:
            log(f"[test]   {m['name']}: {describe(m, snap)}")
        return
    log(f"[probe] started pid {os.getpid()} - reads at start {', '.join(f'{o:+d}m' for o in OFFSETS_MIN)} for: "
        + "; ".join(f"{m['name']} @ {m['start']}" for m in matches))
    for m in sorted(matches, key=lambda m: m["start"]):
        snaps: list[dict] = []
        i = 0
        while i < len(OFFSETS_MIN):
            start = iso(m.get("moved_to") or m["start"])
            when = start + timedelta(minutes=OFFSETS_MIN[i])
            wait = (when - utcnow()).total_seconds()
            if wait < -300:
                log(f"[{m['mid']}] read at start {OFFSETS_MIN[i]:+d}m skipped - its time passed")
                i += 1
                continue
            while wait > 0:
                time.sleep(min(wait, 600))
                wait = (when - utcnow()).total_seconds()
            snap = read_all(g, [m])
            snaps.append(snap)
            log(f"[{m['mid']}] start {OFFSETS_MIN[i]:+d}m: {describe(m, snap)}")
            if m.get("moved_to") and iso(m["moved_to"]) != start:
                log(f"[{m['mid']}] the listing moved its start to {m['moved_to']} - the remaining reads follow it")
            i += 1
        v = verdict(m, snaps)
        extra = ""
        if m.get("kalshi"):
            n = bot_inplay_rows(m["kalshi"], iso(m.get("moved_to") or m["start"]))
            extra = f" The bot logged {n if n is not None else '?'} in-play row(s) for {m['kalshi']}."
            pushes = [((s["matches"].get(str(m["mid"])) or {}).get("sidecar") or {}).get("league_last_live_push_s")
                      for s in snaps if iso(s["at"]) >= iso(m.get("moved_to") or m["start"])]
            got = [x for x in pushes if x is not None]
            extra += (f" The sidecar received in-play pushes for the league (latest {min(got):.0f}s before a read)."
                      if got else " The sidecar received NO in-play pushes for the league.")
        log(f"[{m['mid']}] VERDICT: {v}{extra}")
        discord(f"🏏 **Cricket in-play check** — {m['name']}: **{v.split(' - ')[0]}**. "
                f"{v.split(' - ', 1)[1] if ' - ' in v else ''}{extra} (log: cricket_live_probe.txt)")
    log("[probe] done")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--now", action="store_true", help="one read now (a test), no waiting, no Discord")
    ap.add_argument("--only", type=int, default=0, help="watch just this matchup id (one scheduled task per match)")
    a = ap.parse_args()
    try:
        chosen = [dict(m) for m in MATCHES if not a.only or m["mid"] == a.only]
        if not chosen:
            log(f"[probe] --only {a.only}: no such match in MATCHES")
            return
        probe(chosen, a.now)
    except Exception:
        log("[probe] CRASHED:\n" + traceback.format_exc())
        if not a.now:
            discord("🏏 Cricket in-play check crashed - see cricket_live_probe.txt")


if __name__ == "__main__":
    main()
