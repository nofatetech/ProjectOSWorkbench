#!/usr/bin/env python3
"""Headless smoke check: construct WorkbenchApp and run build() against a mock
page, so Flet constructor-kwarg errors (e.g. helper_text→helper, Dropdown
on_change→on_select) are caught WITHOUT a display.

These bugs slip past `python -c "ast.parse(...)"` and check_build_refs.py because
the kwarg name is valid Python — it only blows up when Flet's control __init__
rejects it at runtime. build() instantiates every view (_build_*_view_body) +
refresh(), so a bad kwarg in any of them surfaces here.

Run:  .venv/bin/python scripts/smoke_build.py
Exit: 0 = build clean, 1 = a control rejected its args (prints the traceback).

NOT a functional test — it doesn't click anything or hit the network. It only
proves the UI tree *constructs*. Reads the real vault/config (via the ./vault
symlink), so run it from a checkout with that symlink present.
"""
import sys
import traceback
from pathlib import Path
from unittest.mock import MagicMock

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

# Neutralize the module-level `ft.run(main)` so importing main.py doesn't try to
# launch a real Flet window. Patch on the flet module *before* importing main,
# which calls `ft.run(...)` at import time (there's no __main__ guard).
import flet  # noqa: E402

flet.run = lambda *a, **k: None

import main  # noqa: E402


def fresh_page():
    """A permissive stand-in for ft.Page: swallows attribute assignment
    (page.title=…, page.bgcolor=…) and no-ops methods (add/update)."""
    return MagicMock(name="Page")


def run() -> int:
    try:
        app = main.WorkbenchApp(fresh_page())
        app.build()
    except Exception:
        print("SMOKE FAIL — build() raised:\n", file=sys.stderr)
        traceback.print_exc()
        return 1
    print("SMOKE OK — WorkbenchApp.build() constructed the full UI tree.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
