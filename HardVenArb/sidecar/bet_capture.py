"""Record the Pinnacle bet-slip flow so `_place_via_ui()` can be written against REAL markup instead of
guessed selectors.

WHY THIS EXISTS. The bot must place bets by driving the site's UI. We cannot invent selectors for a site we
can't see, and blind-probing a live bet slip on a freshly-verified account is a bad idea. So: the user places
ONE small manual bet with the recorder armed, and this captures what the page actually looks like at every
stage of that flow.

WHAT IT CAPTURES, per stage:
  - the INTERACTION (what was clicked / typed into) with a full element descriptor + a ranked list of
    candidate selectors, most stable first
  - a STRUCTURAL SNAPSHOT of the subtrees that changed as a result (the bet slip is, by definition, whatever
    mutates right after you click an odds button)
  - a SCREENSHOT, so the layout can be read directly
  - any non-GET NETWORK call (the actual bet submission + its response shape)

THE ODDS-BOARD PROBLEM. A live Pinnacle page mutates constantly as prices tick, so an unfiltered
MutationObserver is pure noise. Mutations are therefore only recorded inside a short window after a real user
interaction -- which is exactly when the bet slip is reacting to you.

OUTPUT: `bet_capture_<ts>.jsonl` + `bet_capture_<ts>_shots/*.png`. BOTH ARE GITIGNORED AND MAY CONTAIN
ACCOUNT DATA (balance, bet ids, and whatever is on screen). Do not commit or paste them wholesale; the
`--redact` pass in `summarize()` strips obvious money/account strings for sharing.

CONTROL: POST /capture/start -> place the bet by hand -> POST /capture/stop. GET /capture/status to check.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

# Injected once per page. Installs the interaction listeners + a gated MutationObserver and exposes a queue
# the Python side drains. Written as one string so it can be replayed on navigation via add_init_script.
_CAPTURE_JS = r"""
(() => {
  if (window.__hvCap) return "already";
  const MAX_TEXT = 120, MAX_NODES = 400, MAX_DEPTH = 12, MUT_WINDOW_MS = 3000;
  const q = [];
  let lastInteraction = 0;
  let changed = new Set();

  const vis = (el) => {
    try {
      const r = el.getBoundingClientRect();
      if (!r || (r.width === 0 && r.height === 0)) return null;
      const s = getComputedStyle(el);
      if (s.visibility === "hidden" || s.display === "none" || s.opacity === "0") return null;
      return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
    } catch (e) { return null; }
  };

  const attrs = (el) => {
    const out = {};
    for (const a of el.attributes || []) {
      const n = a.name;
      // Keep what makes a selector stable; drop style/inline noise and long class blobs later.
      if (n === "style") continue;
      let v = a.value || "";
      if (v.length > MAX_TEXT) v = v.slice(0, MAX_TEXT) + "...";
      out[n] = v;
    }
    return out;
  };

  // Ranked selector candidates, most stable first. data-test-* beats id beats aria-label beats a path.
  const selectors = (el) => {
    const out = [];
    const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/["\\\]]/g, "\\$&");
    for (const a of el.attributes || []) {
      if (/^data-(test|testid|test-id|qa|cy|automation)/i.test(a.name) && a.value)
        out.push(`[${a.name}="${a.value}"]`);
    }
    if (el.id) out.push(`#${esc(el.id)}`);
    const al = el.getAttribute && el.getAttribute("aria-label");
    if (al) out.push(`${el.tagName.toLowerCase()}[aria-label="${al}"]`);
    const nm = el.getAttribute && el.getAttribute("name");
    if (nm) out.push(`${el.tagName.toLowerCase()}[name="${nm}"]`);
    const ph = el.getAttribute && el.getAttribute("placeholder");
    if (ph) out.push(`${el.tagName.toLowerCase()}[placeholder="${ph}"]`);
    const role = el.getAttribute && el.getAttribute("role");
    const txt = (el.textContent || "").trim().slice(0, 40);
    if (role && txt) out.push(`role=${role} >> text="${txt}"`);
    else if (txt && /^(button|a|label)$/i.test(el.tagName)) out.push(`text="${txt}"`);
    // structural fallback: nth-child path, capped
    try {
      const parts = [];
      let n = el, hops = 0;
      while (n && n.nodeType === 1 && n !== document.body && hops++ < MAX_DEPTH) {
        const p = n.parentElement;
        if (!p) break;
        const i = Array.prototype.indexOf.call(p.children, n) + 1;
        parts.unshift(`${n.tagName.toLowerCase()}:nth-child(${i})`);
        n = p;
      }
      if (parts.length) out.push("body " + parts.join(" > "));
    } catch (e) {}
    return out;
  };

  const describe = (el, depth, budget) => {
    if (!el || el.nodeType !== 1 || depth > MAX_DEPTH || budget.n > MAX_NODES) return null;
    budget.n++;
    let ownText = "";
    for (const c of el.childNodes) if (c.nodeType === 3) ownText += c.nodeValue;
    ownText = ownText.replace(/\s+/g, " ").trim().slice(0, MAX_TEXT);
    const node = {
      tag: el.tagName.toLowerCase(),
      attrs: attrs(el),
      rect: vis(el),
    };
    if (ownText) node.text = ownText;
    if (el.value !== undefined && el.value !== null && el.value !== "") node.value = String(el.value).slice(0, MAX_TEXT);
    if (/^(input|button|select|textarea|a)$/.test(node.tag)) node.sel = selectors(el);
    const kids = [];
    for (const c of el.children) {
      const d = describe(c, depth + 1, budget);
      if (d) kids.push(d);
      if (budget.n > MAX_NODES) break;
    }
    if (kids.length) node.children = kids;
    return node;
  };

  const push = (o) => { o.t = Date.now(); q.push(o); if (q.length > 900) q.shift(); };

  const onInteract = (ev) => {
    const el = ev.target;
    if (!el || el.nodeType !== 1) return;
    lastInteraction = Date.now();
    push({
      kind: "interaction",
      event: ev.type,
      target: {
        tag: el.tagName.toLowerCase(),
        attrs: attrs(el),
        text: (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, MAX_TEXT),
        value: el.value !== undefined ? String(el.value || "").slice(0, MAX_TEXT) : undefined,
        rect: vis(el),
        sel: selectors(el),
      },
      url: location.href,
    });
  };
  for (const t of ["pointerdown", "click", "input", "change", "submit"])
    document.addEventListener(t, onInteract, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === "Tab") onInteract(e);
  }, true);

  // Mutations only count in the window right after a real interaction -- otherwise the live odds board
  // floods this with price ticks.
  // A live odds board re-writes price TEXT constantly (`el.textContent = ...` shows up as a childList
  // mutation swapping a text node). Those are noise. A bet slip appearing/updating ADDS OR REMOVES ELEMENT
  // nodes, or toggles state attributes on form controls. Filtering on that distinction is what keeps the
  // snapshot budget spent on the slip instead of the ticker.
  const interesting = (r) => {
    if (r.type === "childList") {
      for (const n of r.addedNodes)   if (n.nodeType === 1) return true;
      for (const n of r.removedNodes) if (n.nodeType === 1) return true;
      return false;                                    // text-only churn -> ignore
    }
    if (r.type === "attributes") {
      const t = r.target, a = r.attributeName || "";
      if (/^(input|button|select|textarea|form)$/i.test(t.tagName)) return true;
      return /^(disabled|checked|value|aria-|data-test)/i.test(a);
    }
    return false;
  };
  new MutationObserver((recs) => {
    if (Date.now() - lastInteraction > MUT_WINDOW_MS) return;
    for (const r of recs) {
      if (!interesting(r)) continue;
      const n = r.target && r.target.nodeType === 1 ? r.target : null;
      if (n) changed.add(n);
    }
  }).observe(document.body, {childList: true, subtree: true, attributes: true, characterData: false});

  // Snapshot the changed region on demand (Python calls this after letting the UI settle).
  window.__hvCap = {
    drain: () => { const o = q.splice(0, q.length); return o; },
    snapshot: (label) => {
      // Collapse changed nodes to their outermost ancestors so we snapshot whole regions, not fragments.
      const nodes = Array.from(changed).filter(n => n.isConnected);
      changed = new Set();
      let roots = nodes.filter(n => !nodes.some(o => o !== n && o.contains(n)));
      // Spend the node budget on the regions that matter: a subtree holding form controls is far more likely
      // to be the bet slip than one that doesn't.
      const score = (n) => {
        try { return n.querySelectorAll("input,button,select,textarea").length; } catch (e) { return 0; }
      };
      roots = roots.map(n => [n, score(n)]).sort((a, b) => b[1] - a[1]).slice(0, 6).map(p => p[0]);
      const budget = {n: 0};
      return {
        kind: "snapshot", label, t: Date.now(), url: location.href,
        roots: roots.map(r => describe(r, 0, budget)).filter(Boolean),
        rootCount: roots.length,
      };
    },
    // One-off structural outline of the whole page, for orientation.
    outline: () => {
      const budget = {n: 0};
      return {kind: "outline", t: Date.now(), url: location.href, tree: describe(document.body, 0, budget)};
    },
  };
  return "installed";
})();
"""


class BetSlipRecorder:
    """Arms the managed page and records the bet-slip flow. One recorder per sidecar; start/stop via HTTP."""

    def __init__(self, out_dir: str | os.PathLike | None = None):
        self._dir = Path(out_dir or Path(__file__).parent)
        self._page = None
        self._task: Optional[asyncio.Task] = None
        self._fp = None
        self._shots: Optional[Path] = None
        self._active = False
        self._n_events = 0
        self._n_shots = 0
        self._started_at = 0.0
        self._path: Optional[Path] = None
        self._seen_net: set[str] = set()

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def start(self, page) -> dict:
        if self._active:
            return {"ok": False, "error": "capture already running", "file": str(self._path)}
        if page is None:
            return {"ok": False, "error": "no managed page (browser session not ready)"}

        ts = time.strftime("%Y%m%d_%H%M%S")
        self._path = self._dir / f"bet_capture_{ts}.jsonl"
        self._shots = self._dir / f"bet_capture_{ts}_shots"
        self._shots.mkdir(exist_ok=True)
        self._fp = open(self._path, "w", encoding="utf-8")
        self._page = page
        self._active = True
        self._n_events = self._n_shots = 0
        self._started_at = time.time()
        self._seen_net = set()

        try:
            res = await page.evaluate(_CAPTURE_JS)
        except Exception as e:
            await self._teardown()
            return {"ok": False, "error": f"inject failed: {e}"}
        # Re-inject after any navigation so a page reload doesn't silently stop the capture.
        try:
            await page.add_init_script(_CAPTURE_JS)
        except Exception:
            pass

        page.on("request", self._on_request)
        page.on("response", self._on_response)

        self._write({"kind": "meta", "t": int(time.time() * 1000), "note": "capture started", "inject": res,
                     "url": page.url})
        try:
            outline = await page.evaluate("() => window.__hvCap.outline()")
            self._write(outline)
        except Exception as e:
            self._write({"kind": "error", "t": int(time.time() * 1000), "where": "outline", "error": str(e)})
        await self._shoot("start")

        self._task = asyncio.create_task(self._loop())
        print(f"[BET-CAPTURE] ARMED -> {self._path.name} (place ONE small bet by hand now; "
              f"POST /capture/stop when done)")
        return {"ok": True, "file": str(self._path), "shots": str(self._shots)}

    async def stop(self) -> dict:
        if not self._active:
            return {"ok": False, "error": "capture not running"}
        await self._drain("final")
        path, n, s = self._path, self._n_events, self._n_shots
        await self._teardown()
        print(f"[BET-CAPTURE] STOPPED - {n} event(s), {s} screenshot(s) -> {path.name if path else '?'}")
        return {"ok": True, "file": str(path), "events": n, "screenshots": s}

    def status(self) -> dict:
        return {
            "active": self._active,
            "file": str(self._path) if self._path else None,
            "events": self._n_events,
            "screenshots": self._n_shots,
            "elapsed_sec": round(time.time() - self._started_at, 1) if self._active else 0,
        }

    async def _teardown(self) -> None:
        self._active = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._page:
            try:
                self._page.remove_listener("request", self._on_request)
                self._page.remove_listener("response", self._on_response)
            except Exception:
                pass
        if self._fp:
            try:
                self._fp.close()
            except Exception:
                pass
            self._fp = None
        self._page = None

    # ── capture loop ──────────────────────────────────────────────────────────
    async def _loop(self) -> None:
        """Drain the in-page queue. After an interaction, let the UI settle then snapshot + screenshot -- that
        settle delay is what makes each record a coherent 'stage' rather than a half-rendered frame."""
        while self._active:
            try:
                await asyncio.sleep(0.35)
                had = await self._drain(None)
                if had:
                    await asyncio.sleep(0.55)          # let the slip finish rendering
                    await self._snapshot("post-interaction")
                    await self._shoot("stage")
                    await asyncio.sleep(0.9)           # second look: catches async/confirmation renders
                    await self._snapshot("settled")
                    await self._shoot("settled")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._write({"kind": "error", "t": int(time.time() * 1000), "where": "loop", "error": str(e)})

    async def _drain(self, label: Optional[str]) -> bool:
        if not self._page:
            return False
        try:
            events = await self._page.evaluate("() => window.__hvCap ? window.__hvCap.drain() : []")
        except Exception as e:
            self._write({"kind": "error", "t": int(time.time() * 1000), "where": "drain", "error": str(e)})
            return False
        for e in events or []:
            self._write(e)
        if label and events:
            await self._snapshot(label)
        return bool(events)

    async def _snapshot(self, label: str) -> None:
        """NOTE: must be an ARROW FUNCTION, not a bare call. Playwright only passes the argument when the
        script IS a function expression; `snapshot(arguments[0])` evaluates as a plain expression where
        `arguments` is undefined -> ReferenceError. That failure used to be swallowed, silently dropping every
        structural snapshot while interactions and screenshots still recorded (so the capture looked fine)."""
        if not self._page:
            return
        try:
            snap = await self._page.evaluate(
                "(label) => window.__hvCap ? window.__hvCap.snapshot(label) : null", label)
        except Exception as e:
            self._write({"kind": "error", "t": int(time.time() * 1000), "where": "snapshot",
                         "label": label, "error": str(e)})
            return
        if snap is None:
            self._write({"kind": "error", "t": int(time.time() * 1000), "where": "snapshot",
                         "label": label, "error": "__hvCap missing (page navigated without re-inject?)"})
            return
        if snap.get("roots"):
            self._write(snap)

    async def _shoot(self, tag: str) -> None:
        if not self._page or not self._shots:
            return
        try:
            self._n_shots += 1
            p = self._shots / f"{self._n_shots:03d}_{tag}.png"
            await self._page.screenshot(path=str(p), full_page=False)
            self._write({"kind": "screenshot", "t": int(time.time() * 1000), "file": p.name, "tag": tag})
        except Exception:
            self._n_shots -= 1

    # ── network ───────────────────────────────────────────────────────────────
    def _on_request(self, req) -> None:
        """Non-GETs only -- the bet submission is a POST, and GETs are odds-poll noise."""
        try:
            if req.method == "GET":
                return
            body = None
            try:
                body = req.post_data
            except Exception:
                pass
            if body and len(body) > 4000:
                body = body[:4000] + "...(truncated)"
            self._write({"kind": "request", "t": int(time.time() * 1000), "method": req.method,
                         "url": req.url, "body": body})
        except Exception:
            pass

    def _on_response(self, resp) -> None:
        try:
            if resp.request.method == "GET":
                return
            key = f"{resp.request.method}:{resp.url}:{resp.status}"
            if key in self._seen_net:
                return
            self._seen_net.add(key)
            self._write({"kind": "response", "t": int(time.time() * 1000), "status": resp.status,
                         "url": resp.url})
        except Exception:
            pass

    # ── io ────────────────────────────────────────────────────────────────────
    def _write(self, obj: Any) -> None:
        if not self._fp:
            return
        try:
            self._fp.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._fp.flush()
            self._n_events += 1
        except Exception:
            pass


# ── offline reading helper ────────────────────────────────────────────────────
_REDACT = [
    (re.compile(r"\b\d{6,}\b"), "<NUM>"),                                  # account / bet ids
    (re.compile(r"(?i)\b(balance|saldo|wallet)\b[^,}\]]{0,40}"), r"\1 <REDACTED>"),
    (re.compile(r"[\w.\-+]+@[\w\-]+\.\w+"), "<EMAIL>"),
]


def summarize(path: str | os.PathLike, redact: bool = True) -> str:
    """Human/agent-readable digest of a capture: the interaction sequence, the selectors seen at each stage,
    and any non-GET network calls. This is the thing to actually read -- the raw jsonl is verbose."""
    lines: list[str] = []
    n_int = n_snap = 0
    with open(path, encoding="utf-8") as f:
        for raw in f:
            try:
                e = json.loads(raw)
            except Exception:
                continue
            k = e.get("kind")
            if k == "interaction":
                n_int += 1
                t = e.get("target", {})
                sels = t.get("sel") or []
                lines.append(f"[{n_int:02d}] {e.get('event','?'):<11} <{t.get('tag')}> "
                             f"text={t.get('text','')!r:.60} value={t.get('value')!r}")
                for s in sels[:3]:
                    lines.append(f"       sel: {s}")
            elif k == "snapshot":
                n_snap += 1
                lines.append(f"       -> snapshot '{e.get('label')}' : {e.get('rootCount', 0)} changed region(s)")
                for r in e.get("roots", [])[:2]:
                    lines.append(f"          region <{r.get('tag')}> "
                                 f"{json.dumps(r.get('attrs', {}), ensure_ascii=False)[:120]}")
            elif k == "request":
                lines.append(f"       NET {e.get('method')} {e.get('url','')[:110]}")
                if e.get("body"):
                    lines.append(f"           body: {str(e['body'])[:200]}")
            elif k == "screenshot":
                lines.append(f"       shot: {e.get('file')}")
    out = "\n".join(lines)
    if redact:
        for pat, rep in _REDACT:
            out = pat.sub(rep, out)
    return out or "(empty capture)"


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else None
    if not target:
        cands = sorted(Path(__file__).parent.glob("bet_capture_*.jsonl"))
        if not cands:
            print("no bet_capture_*.jsonl found")
            raise SystemExit(1)
        target = str(cands[-1])
    print(f"=== {target} ===")
    print(summarize(target, redact="--raw" not in sys.argv))
