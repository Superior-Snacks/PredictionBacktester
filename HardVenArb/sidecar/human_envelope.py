r"""What does a HUMAN session look like to BetInAsia's own telemetry — and does the bot fit inside it?

The venue posts `/web/metrics/` with `betslip.duration`, `betslip.closeTime`, `betslip.source` and
`context.tabId`. That is a first-party, server-side record of how betslips get used, and no browser-API
probe can see it (see detect_recon.py). It is also the one place the bot's behaviour is most obviously
NOT human: open a slip, hold it for a fixed interval, never place, close.

So rather than guessing what "looks normal", MEASURE IT — the same method that produced the WS subscribe
pacing envelope (real browser batches: min 1 / median 3 / max 32) instead of a made-up rate.

    # 1. record yourself using the site normally: open some slips, close them, browse.
    #    Runs until you Ctrl+C / close the window; writes betinasia_recon_<timestamp>.jsonl here.
    python betinasia_recon.py --url https://black.betinasia.com/sportsbook/tennis
    # 2. read the envelope back out
    python human_envelope.py betinasia_recon_*.jsonl

Then set the hold range so the bot's slip lifetime sits INSIDE the human distribution rather than outside
it. A bot whose every slip lasts exactly 120.0s is distinguishable no matter how human the mouse path was
on the way in.

── THE PAIRED TEST: does the site record the difference? ─────────────────────────────────────────────
The question "can they tell?" is answerable directly. Do the SAME action twice — once by hand, once with
the bot — capturing each, then diff what actually left the browser:

    # RUN 1 — you, by hand: open one betslip, wait ~20s, close it. Ctrl+C.
    python betinasia_recon.py --url https://black.betinasia.com/sportsbook/tennis
    # RUN 2 — the bot doing the SAME thing: leave it running and fire one probe from another shell
    python betinasia_recon.py --url https://black.betinasia.com/sportsbook/tennis
    #        (other shell)  .\slip_probe.ps1 -Sport tennis
    # Each run writes betinasia_recon_<timestamp>.jsonl; feed the two newest in, oldest first.
    python human_envelope.py --compare <human>.jsonl <bot>.jsonl

Identical traffic means the venue has nothing on the network side to separate them, and the whole question
reduces to the VALUES inside /web/metrics/ (duration, tabId, cadence) rather than to anything about mouse
paths or trusted events. Different traffic names precisely what to fix. Either way it beats reasoning
about it, which is how the last three wrong answers in this project got made.
"""
from __future__ import annotations

import glob
import json
import os
import statistics
import sys

sys.stdout.reconfigure(encoding="utf-8")   # some readouts below print em-dashes; cp1252 would raise


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(round(p / 100.0 * (len(xs) - 1)))))
    return xs[i]


def _traffic(path: str) -> tuple[set, dict, list]:
    """(endpoints, metrics fields -> values, raw metrics payloads) for one capture.

    RAISES on a missing or empty file rather than returning empties. A typo'd filename used to read as a
    capture in which the venue saw nothing at all — "A only:" against every single endpoint — which is a
    perfectly plausible-looking result and completely wrong. Same failure family as balance() returning
    0.0 for an unreadable wallet: broken and 'nothing happened' must not share a value.
    """
    endpoints, fields, payloads = set(), {}, []
    if not os.path.exists(path):
        raise SystemExit(f"ERROR: no such capture file: {path}\n"
                         f"       (check the extension — the recon writes ONE '.jsonl')")
    if os.path.getsize(path) == 0:
        raise SystemExit(f"ERROR: {path} is 0 bytes — that run recorded nothing. Re-run the capture.")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            url = rec.get("url") or ""
            if not url:
                continue
            from urllib.parse import urlparse
            u = urlparse(url)
            # Collapse ids out of the path so two captures compare on SHAPE, not on generated ids.
            import re as _re
            p = _re.sub(r"/[0-9a-f]{16,}", "/{id}", u.path)
            p = _re.sub(r"/\d{3,}", "/{n}", p)
            if rec.get("kind") == "http":
                endpoints.add(f"{rec.get('method','?')} {u.netloc}{p}")
            elif rec.get("kind") == "ws_frame":
                endpoints.add(f"WS/{rec.get('dir','?')} {rec.get('mtype','?')}")
            if "/web/metrics" in url:
                try:
                    d = json.loads(rec.get("post") or "")
                except Exception:
                    continue
                payloads.append(d)
                for k, v in d.items():
                    fields.setdefault(k, []).append(v)
    return endpoints, fields, payloads


def compare(a_path: str, b_path: str) -> int:
    """Diff two captures: what did the venue see differently?"""
    ae, af, ap = _traffic(a_path)
    be, bf, bp = _traffic(b_path)
    print(f"A = {a_path}   ({len(ae)} endpoint shapes, {len(ap)} metrics posts)")
    print(f"B = {b_path}   ({len(be)} endpoint shapes, {len(bp)} metrics posts)")

    print("\n=== endpoints hit by ONE side only ===")
    only_a, only_b = sorted(ae - be), sorted(be - ae)
    if not only_a and not only_b:
        print("  NONE — both sides produced the same traffic shapes. On the network, they are the same "
              "session; only the VALUES inside /web/metrics/ can separate them.")
    for e in only_a:
        print(f"  A only: {e}")
    for e in only_b:
        print(f"  B only: {e}")

    print("\n=== /web/metrics/ fields present on one side only ===")
    only = (set(af) ^ set(bf))
    if not only:
        print("  NONE — identical field sets.")
    for k in sorted(only):
        side = "A" if k in af else "B"
        print(f"  {side} only: {k}")

    print("\n=== fields present on BOTH — compare the VALUES ===")
    for k in sorted(set(af) & set(bf)):
        av, bv = af[k], bf[k]
        def summ(xs):
            nums = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
            if nums:
                return f"n={len(nums)} min={min(nums)} med={statistics.median(nums):.0f} max={max(nums)}"
            uniq = sorted({str(x) for x in xs})
            return f"n={len(xs)} {uniq[:3]}"
        print(f"  {k:32}\n      A: {summ(av)}\n      B: {summ(bv)}")
    return 0


def timeline(path: str) -> int:
    """Every betslip event in time order, so a close METHOD can be matched to what the venue recorded.

    The question this answers: does `context.action = betslip.close` fire on every close, or only on some?
    If a slip closed with Esc (or by re-clicking the odds) reports nothing, then the bot's missing
    betslip.duration is ordinary rather than a signature — and that changes what is worth fixing.
    """
    if not os.path.exists(path):
        raise SystemExit(f"ERROR: no such capture file: {path}")
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            url, t = rec.get("url") or "", rec.get("t")
            if not url or t is None:
                continue
            if "/web/metrics" in url:
                try:
                    d = json.loads(rec.get("post") or "")
                except Exception:
                    continue
                rows.append((t, "METRIC", d.get("context.action", "?"),
                             d.get("betslip.duration"), d.get("betslip.closeTime"),
                             d.get("betslip.source"), d.get("context.path", "")))
            elif "/v1/betslips/" in url:
                m = rec.get("method", "?")
                kind = "SLIP OPEN" if m == "POST" and url.rstrip("/").endswith("betslips") else \
                       "SLIP CANCEL" if m == "DELETE" else f"SLIP {m}"
                rows.append((t, kind, "", None, None, None, ""))
    rows.sort(key=lambda r: r[0])

    opens = sum(1 for r in rows if r[1] == "SLIP OPEN")
    cancels = sum(1 for r in rows if r[1] == "SLIP CANCEL")
    closes = sum(1 for r in rows if r[1] == "METRIC" and r[2] == "betslip.close")
    print(f"{path}\n  slips opened: {opens}   cancelled: {cancels}   "
          f"betslip.close metrics: {closes}\n")
    print(f"  {'t(s)':>8}  {'event':12}  detail")
    print("  " + "-" * 88)
    for t, kind, action, dur, close_ms, src, p in rows:
        if kind == "METRIC":
            if dur is not None:
                detail = f"action={action}  duration={dur/1000:.1f}s  closeTime={close_ms}ms  source={src}"
            else:
                detail = f"action={action}  {p}"
        else:
            detail = ""
        print(f"  {t:8.1f}  {kind:12}  {detail}")

    print("\n  READ IT LIKE THIS: an open with no matching betslip.close metric = a close the venue did "
          "NOT record a duration for.")
    if opens and closes < opens:
        print(f"  -> {opens - closes} of {opens} closes reported NOTHING. The metric is not emitted for "
              f"every close, so the bot emitting none is less anomalous than it first looked.")
    elif opens and closes == opens:
        print("  -> EVERY close reported a duration. The bot emitting none IS a distinguishing absence.")
    return 0


def orders(path: str) -> int:
    """Every state an order passed through, in order — the fill experiment's readout.

    Answers the questions the executor is blocked on: does a resting order report `closed:false` with a
    stake below want_stake while it waits; does `bets[]` grow one bookie at a time; what does a cancel
    look like against a fill. Requires the capture to have kept every /v1/orders/ body — see
    ALWAYS_BODY in betinasia_recon.py, without which only the first five responses exist.
    """
    if not os.path.exists(path):
        raise SystemExit(f"ERROR: no such capture file: {path}")
    states: dict = {}
    placements: list = []
    order = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            url, t = rec.get("url") or "", rec.get("t")
            if rec.get("kind") != "http" or "/v1/orders" not in url:
                continue
            if rec.get("method") == "POST" and rec.get("post"):
                placements.append((t, rec["post"]))
            try:
                d = json.loads(rec.get("body") or "")
            except Exception:
                continue
            rows = d.get("data")
            rows = rows if isinstance(rows, list) else [rows] if isinstance(rows, dict) else []
            for o in rows:
                if not isinstance(o, dict) or "order_id" not in o:
                    continue
                oid = o["order_id"]
                bets = o.get("bets") or []
                filled = sum((b.get("stake") or [0, 0])[1] for b in bets
                             if isinstance(b.get("stake"), list) and len(b["stake"]) > 1)
                snap = (o.get("closed"), o.get("close_reason"), o.get("status"),
                        json.dumps(o.get("want_stake")), json.dumps(o.get("stake")),
                        o.get("want_price"), o.get("price"), len(bets), round(filled, 4))
                if states.get(oid) != snap:                # only print CHANGES
                    states[oid] = snap
                    order.append((t, oid, snap, bets))

    print(f"{path}\n  placements captured: {len(placements)}   order state changes: {len(order)}\n")
    for t, post in placements:
        print(f"  t={t:7.1f}  PLACED  {post[:400]}")
        if "request_" in post:
            i = post.index("request_")
            print(f"            ^ request field present: {post[i:i+120]}")
    print()
    for t, oid, s, bets in order:
        closed, reason, status, want_stake, stake, want_price, price, nbets, filled = s
        print(f"  t={t:7.1f}  order {oid}  closed={closed} reason={reason} status={status}")
        print(f"            want {want_stake} @ {want_price}   got {stake} @ {price}   "
              f"bets={nbets} filled={filled}")
        for b in bets[:6]:
            st = (b.get("status") or {}).get("code") if isinstance(b.get("status"), dict) else b.get("status")
            print(f"              {b.get('bookie'):>12}  {json.dumps(b.get('stake'))} @ {b.get('price')}  {st}")
    if not order:
        print("  NO order bodies in this capture. Either nothing was placed, or the capture predates the\n"
              "  ALWAYS_BODY fix and threw away every response after the fifth.")
    return 0


def fills(path: str) -> int:
    """Order + bet lifecycle as the WEBSOCKET reported it, with fill latency per bookie.

    The venue pushes `api` frames carrying `order` and `bet` records, so a fill is observable live with no
    polling and no request — the observer already receives these. This reads them back out of a capture.

    THE NUMBER THIS EXISTS FOR is placement -> bet done. Measured once (2026-08-14) at 13.6s on a
    marketable order that routed to `overtime`, a crypto book with a known artificial delay. Kalshi hedge
    windows on the same tape ran a median of ~4s. If that latency is typical rather than book-specific,
    HardVen-first cannot work; if a non-crypto book is fast, it can. One number, and it decides the model.
    """
    if not os.path.exists(path):
        raise SystemExit(f"ERROR: no such capture file: {path}")
    rows, placed, post_t = [], {}, {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"api"' not in line and "/v1/orders" not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue

            # t0 IS THE POST, not the venue's first mention of the order: an order the venue has already
            # acknowledged has already been exposed for some unmeasured time, so anchoring on the first WS
            # frame reads short. Its response carries the order_id, which is what joins click to lifecycle.
            # CAVEAT: the recon hooks RESPONSES, so this is the POST's *reply*, not its send. Measured
            # 2026-08-14 the WS push actually beat the reply by 0.1s -- both anchors land within noise of
            # each other, and the true send is earlier than both. Treat every number here as a FLOOR.
            if (rec.get("kind") == "http" and rec.get("method") == "POST"
                    and "/v1/orders" in (rec.get("url") or "")):
                try:
                    d = json.loads(rec.get("body") or "")
                except Exception:
                    continue
                got = d.get("data") if isinstance(d, dict) else None
                got = got if isinstance(got, list) else [got] if isinstance(got, dict) else []
                for o in got:
                    if isinstance(o, dict) and o.get("order_id") is not None:
                        post_t.setdefault(int(o["order_id"]), rec.get("t") or 0.0)
                continue

            if rec.get("kind") != "ws_frame":
                continue
            t = rec.get("t") or 0.0
            try:
                frames = json.loads(rec.get("body") or "[]")
            except Exception:
                continue
            for fr in frames:
                if not (isinstance(fr, list) and fr and fr[0] == "api"):
                    continue
                for entry in (fr[1].get("data") or []):
                    if not (isinstance(entry, list) and len(entry) == 2):
                        continue
                    kind, o = entry
                    if kind not in ("order", "bet") or not isinstance(o, dict):
                        continue
                    oid = o.get("order_id")
                    if oid is None:
                        continue
                    st = o.get("status")
                    st = st.get("code") if isinstance(st, dict) else st
                    rows.append((t, kind, oid, st, o.get("bookie"),
                                 o.get("want_price"), o.get("price"),
                                 o.get("want_stake"), o.get("stake"),
                                 o.get("closed"), o.get("close_reason")))
                    if kind == "order" and oid not in placed:
                        placed[oid] = t

    def t0_of(oid):
        """Commit time for one order: the POST if the capture kept it, else the venue's first mention."""
        return post_t.get(oid, placed.get(oid))

    seen = set()
    print(f"{path}\n")
    for r in rows:
        t, kind, oid, st, bookie, wp, pr, ws, s, closed, reason = r
        key = (kind, oid, st, str(pr), str(s), closed)
        if key in seen:
            continue                       # the venue repeats a state; show transitions only
        seen.add(key)
        lat = f"+{t - t0_of(oid):5.1f}s" if t0_of(oid) is not None else "      "
        who = f" via {bookie}" if bookie else ""
        got = f"{pr} / {json.dumps(s)}" if pr is not None else "— / —"
        print(f"  t={t:7.1f} {lat}  {kind:5} {oid}  {str(st):8}{who}")
        print(f"                    want {wp} / {json.dumps(ws)}   got {got}"
              + (f"   CLOSED {reason}" if closed else ""))

    print()
    for oid in sorted(set(placed) | set(post_t)):
        t0 = t0_of(oid)
        src = "POST" if oid in post_t else "first WS frame (POST not captured; READS SHORT)"
        done = sorted(r[0] for r in rows if r[2] == oid and r[1] == "bet" and r[3] == "done")
        shut = sorted(r[0] for r in rows if r[2] == oid and r[9])
        books = sorted({r[4] for r in rows if r[2] == oid and r[1] == "bet" and r[4]})
        if not done and not shut:
            print(f"  order {oid}: no 'bet done' in this capture (still open, or the run ended too soon)")
            continue
        print(f"  order {oid}  t0={src}   books: {', '.join(books) or '?'}")
        if done:
            # LAST, not first. A multi-book fill is not finished when one leg reports done -- the total
            # stake is still moving, so the Kalshi leg cannot be sized yet. The naked window is the whole
            # span, and quoting the first leg would understate it by however long the slowest book took.
            print(f"      first leg done   {done[0] - t0:6.1f}s")
            if len(done) > 1:
                print(f"      LAST leg done    {done[-1] - t0:6.1f}s   <-- sizeable here, {len(done)} legs")
        if shut:
            print(f"      order closed     {shut[0] - t0:6.1f}s   <-- naked window ends here")
    if not rows:
        print("  No order/bet frames. Either nothing was placed, or the capture ended before the venue\n"
              "  pushed them — the lifecycle arrives over the WS, so the run must outlast the fill.")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--fills":
        if len(argv) != 2:
            print("usage: python human_envelope.py --fills <recon.jsonl>")
            return 2
        return fills(argv[1])
    if argv and argv[0] == "--orders":
        if len(argv) != 2:
            print("usage: python human_envelope.py --orders <recon.jsonl>")
            return 2
        return orders(argv[1])
    if argv and argv[0] == "--timeline":
        if len(argv) != 2:
            print("usage: python human_envelope.py --timeline <recon.jsonl>")
            return 2
        return timeline(argv[1])
    if argv and argv[0] == "--compare":
        if len(argv) != 3:
            print("usage: python human_envelope.py --compare <human.jsonl> <bot.jsonl>")
            return 2
        return compare(argv[1], argv[2])

    paths: list[str] = []
    for a in argv:
        paths.extend(glob.glob(a))
    if not paths:
        print("usage: python human_envelope.py <recon.jsonl> [more.jsonl ...]")
        print("       python human_envelope.py --compare <human.jsonl> <bot.jsonl>")
        return 2

    durations: list[float] = []
    close_times: list[float] = []
    sources: dict[str, int] = {}
    tabs: set = set()
    pages: list[int] = []
    n_metrics = 0

    for p in paths:
        try:
            f = open(p, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if "/web/metrics" not in (rec.get("url") or ""):
                    continue
                try:
                    d = json.loads(rec.get("post") or "")
                except Exception:
                    continue
                n_metrics += 1
                if "betslip.duration" in d:
                    durations.append(float(d["betslip.duration"]) / 1000.0)
                if "betslip.closeTime" in d:
                    close_times.append(float(d["betslip.closeTime"]))
                if "betslip.source" in d:
                    s = str(d["betslip.source"])
                    sources[s] = sources.get(s, 0) + 1
                if "context.tabId" in d:
                    tabs.add(d["context.tabId"])
                if isinstance(d.get("context.pageNo"), (int, float)):
                    pages.append(int(d["context.pageNo"]))

    print(f"files: {len(paths)}   /web/metrics/ posts: {n_metrics}")
    if not durations:
        print("\nNO betslip.duration SAMPLES. Open and close some betslips by hand while a recon capture "
              "runs, then re-read this. Without human samples there is no envelope to compare against, "
              "and any hold time is a guess.")
        return 1

    print(f"\n=== betslip LIFETIME, seconds (n={len(durations)}) ===")
    print(f"  min {min(durations):8.1f}   p25 {pct(durations,25):8.1f}   median {statistics.median(durations):8.1f}")
    print(f"  p75 {pct(durations,75):8.1f}   p90 {pct(durations,90):8.1f}   max    {max(durations):8.1f}")
    if close_times:
        print(f"\n=== close time, ms (n={len(close_times)}) ===")
        print(f"  min {min(close_times):.0f}  median {statistics.median(close_times):.0f}  max {max(close_times):.0f}")
    if sources:
        print("\n=== where slips were opened from ===")
        for s, c in sorted(sources.items(), key=lambda kv: -kv[1]):
            print(f"  {c:5}x  {s}")
    print(f"\n=== tabs / pages ===")
    print(f"  distinct context.tabId: {len(tabs)}")
    if pages:
        print(f"  context.pageNo max:     {max(pages)}   (navigations in the session)")

    # ── does the bot fit? ────────────────────────────────────────────────────
    hold = float(os.environ.get("HARDVEN_SLIP_HOLD_SEC", "120"))
    lo, hi = pct(durations, 10), pct(durations, 90)
    print(f"\n=== does the bot fit inside this? ===")
    print(f"  HARDVEN_SLIP_HOLD_SEC = {hold:.0f}s   human p10-p90 = {lo:.1f}-{hi:.1f}s")
    if hold > max(durations):
        print(f"  *** OUTSIDE *** every bot slip would outlast the LONGEST human slip seen ({max(durations):.1f}s).")
    elif hold > hi:
        print(f"  *** HIGH *** above the human p90. Fits only the tail, and fits it EVERY time.")
    else:
        print(f"  within range — but a FIXED value is still a signature. Randomise it across the range.")
    print("  NOTE: a constant is the giveaway, not the value. Human slip lifetimes are scattered; a bot "
          "whose slips all last the same duration is separable at any setting.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
