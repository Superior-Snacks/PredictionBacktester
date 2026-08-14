"""Balance parsing, against the REAL /accounting_info/ response. Run: python test_balance_parse.py

The fixture is verbatim from a live capture on 2026-08-14. It exists because the previous parser assumed
`data` was a dict and the real one is a LIST OF RECORDS — so the function returned None every time, and
None is also its legitimate "unknown" answer, which is why nobody noticed.
"""
import json
import re
import sys

REAL = json.loads(
    '{"data":[{"key":"current_balance","label":"Current balance","unit":"USD","value":47.4675},'
    '{"key":"open_stakes","label":"Open stakes","unit":"USD","value":4.0},'
    '{"key":"commission_rate","label":"Current commission rate","unit":"%","value":0},'
    '{"key":"smart_credit","label":"Smart credit","unit":"USD","value":0.0},'
    '{"key":"credit_limit","label":"Agent credit limit","unit":"USD","value":0.0},'
    '{"key":"available_credit","label":"available Credit","unit":"USD","value":43.4675},'
    '{"key":"today_pl","label":"Today P/L","value":0,"unit":"USD"},'
    '{"key":"yesterday_pl","label":"Yesterday P/L","value":-4.36,"unit":"USD"}],"status":"ok"}')

bad = 0


def check(label, ok):
    global bad
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        bad += 1


def parse(res):
    """Mirrors betinasia_adapter.balance()'s extraction."""
    fields = {}
    data = res.get("data")
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict) and "key" in row:
                fields[str(row["key"])] = row.get("value")
    elif isinstance(data, dict):
        fields = data
    else:
        fields = res
    raw = fields.get("current_balance")
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        raw = raw[1]
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


print("[1] the REAL list-of-records shape")
check(f"current_balance = 47.4675 (got {parse(REAL)})", parse(REAL) == 47.4675)

print("\n[2] the OLD parser would have failed on it — proving the bug was real")
old = REAL.get("data") if isinstance(REAL.get("data"), dict) else REAL
check("old code found no current_balance", old.get("current_balance") is None)

print("\n[3] shapes that must still work")
check("dict under data", parse({"data": {"current_balance": 12.5}}) == 12.5)
check("flat dict", parse({"current_balance": 3.25}) == 3.25)
check('["USD", n] money tuple', parse({"data": {"current_balance": ["USD", 9.99]}}) == 9.99)
check("zero is a real balance, not a failure", parse({"current_balance": 0}) == 0.0)

print("\n[4] unreadable shapes must give None, never a number")
for bad_in in ({}, {"data": []}, {"data": None}, {"status": "err"},
               {"data": [{"label": "no key field", "value": 5}]},
               {"current_balance": "not a number"}):
    check(f"None for {json.dumps(bad_in)[:44]}", parse(bad_in) is None)

print("\n[5] the DOM read's label->number regex")
LABELS = ("Current balance", "Available credit", "Balance")


def dom_parse(txt):
    for label in LABELS:
        m = re.search(rf"{re.escape(label)}\D{{0,14}}(-?[\d,]+(?:\.\d+)?)", txt, re.I)
        if m:
            try:
                v = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if 0 <= v < 10_000_000:
                return v
    return None


check("'Current balance $47.47'", dom_parse("Current balance $47.47") == 47.47)
check("no currency symbol", dom_parse("Current balance 47.47") == 47.47)
check("thousands separator", dom_parse("Current balance $1,234.56") == 1234.56)
check("takes the number after the LABEL, not a stray one before it",
      dom_parse("Open stakes 4.00 Current balance $47.47") == 47.47)
check("ignores a far-away number (>14 non-digits between)",
      dom_parse("Current balance ................... 47.47") is None)
check("no label -> None", dom_parse("Open stakes $4.00") is None)
check("empty -> None", dom_parse("") is None)

print("\nALL PASS" if bad == 0 else f"\nFAILURES: {bad}")
sys.exit(1 if bad else 0)
