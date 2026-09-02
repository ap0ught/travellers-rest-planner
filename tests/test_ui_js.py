"""EV-1: the single-file UI's bridge_event label builder.

The vanilla UI's JS is embedded in static.py, so this extracts the pure
`bridgeEventLabel` function and runs cases through node — locking down the
verified before/after formatting ("+5 x #42 (2 -> 7)", buy/sell money+item,
unverifiable -1 suppression, value_read skip, error labels).

The React UI's twin label logic is compile-checked by its tsc/vite build; a
JS unit runner isn't set up for it.
"""
import json
import re
import shutil
import subprocess

import pytest

from planner.server.static import INDEX_HTML

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _extract_fn(src: str, name: str) -> str:
    """Brace-matched extraction of one function definition."""
    start = src.index(f"function {name}")
    depth = 0
    for j in range(start, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError(f"function {name} not found in script block")


def test_bridge_event_label_formats_verified_before_after(tmp_path):
    script = re.findall(r"<script>(.*?)</script>", INDEX_HTML, re.S)[0]
    fn = _extract_fn(script, "bridgeEventLabel")

    cases = [
        # (type, data, expected)
        ("addItem", {"itemId": 42, "count": 5, "before": 2, "after": 7},
         "+5 × #42  (2 → 7)"),
        # unverifiable (-1) -> no before/after parens
        ("addSeed", {"itemId": 9, "count": 3, "before": -1, "after": -1},
         "+3 × #9"),
        ("addMoney", {"copper": 50000, "before": 10000, "after": 60000},
         "5.00g  (1.00g → 6.00g)"),
        ("shop/buy", {"itemId": 1, "count": 2, "price": 10,
                      "before_item": 2, "after_item": 12,
                      "before_money": 1000, "after_money": 900},
         "bought 2 × #1  (item 2 → 12 · 0.10g → 0.09g)"),
        ("shop/sell", {"itemId": 7, "count": 3, "price": 150,
                       "before_item": 10, "after_item": 7,
                       "before_money": 10000, "after_money": 10150},
         "sold 3 × #7  (item 10 → 7 · 1.00g → 1.01g)"),
        # targeted query, not a change -> nothing to toast
        ("value_read", {"itemId": 42, "count": 5}, None),
        ("addItem_error", {"error": "no inventory"}, "add failed: no inventory"),
        ("mystery", {}, "mystery"),
    ]

    harness = (
        fn
        + "\nconst cases = " + json.dumps(cases) + ";\n"
        + "console.log(JSON.stringify(cases.map(([tp, d]) => bridgeEventLabel(tp, d))));\n"
    )
    js = tmp_path / "label_test.js"
    js.write_text(harness, encoding="utf8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got == [c[2] for c in cases], f"label mismatch:\n got: {got}\nwant: {[c[2] for c in cases]}"
