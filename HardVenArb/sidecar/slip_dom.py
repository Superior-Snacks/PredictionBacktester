"""Print the sidecar browser's form controls, readably. For a hand-driven betslip recon.

Run it at each stage of a manual bet in the sidecar's own window — slip closed, slip open, after typing
the price, after typing the stake — and diff the output. Whatever changes is the selector.

Reads only: no clicks, and the reads run in Playwright's isolated world, so the page cannot see them.

    python slip_dom.py                 # all tabs
    python slip_dom.py --inputs-only   # skip panel text
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--inputs-only", action="store_true")
    a = ap.parse_args()
    try:
        d = json.load(urllib.request.urlopen(
            f"http://127.0.0.1:{a.port}/debug/slip_dom", timeout=30))
    except Exception as e:
        print(f"could not reach the sidecar on {a.port}: {type(e).__name__}: {e}")
        return 2
    if not d.get("ok"):
        print("ERROR:", d.get("error"))
        return 1
    pages = d.get("pages") or []
    print(f"{len(pages)} open tab(s)\n")
    for p in pages:
        if p.get("error"):
            print(f"--- tab {p['index']}: ERROR {p['error']}")
            continue
        # Tabs with no form controls are the board pages; they are noise for this question.
        inputs = p.get("inputs") or ""
        if "no input elements" in inputs and not p.get("place_candidates"):
            continue
        print(f"--- tab {p['index']}  {p.get('url', '')}")
        # Read this FIRST on a freshly-opened slip: a hand-opened slip focuses the stake field, and
        # whether a bot-opened one does is the open question about why bot slips are dismissed.
        print(f"  ACTIVE ELEMENT: {p.get('active_element')}")
        print("  INPUTS:")
        print(inputs)
        pc = p.get("place_candidates") or []
        print(f"  PLACE CANDIDATES ({len(pc)}) — substring 'place', which also matches 'UNPLACED':")
        for line in pc:
            print(f"      {line}")
        # What the bot ACTUALLY uses. Exact match cannot hit the book rows' UNPLACED labels, so this is
        # the list that has to contain the real button -- the substring list above is only diagnostic.
        px = p.get("place_exact") or []
        print(f"  PLACE EXACT ('Place') — THIS is what _place_via_ui clicks (.last of these):")
        for line in px:
            print(f"      {line}")
        bl = p.get("buttons") or []
        if bl:
            print(f"  BUTTONS:")
            for line in bl:
                print(f"      {line}")
        if not a.inputs_only and p.get("panel_text"):
            print(f"  PANEL TEXT: {p['panel_text'][:300]!r}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
