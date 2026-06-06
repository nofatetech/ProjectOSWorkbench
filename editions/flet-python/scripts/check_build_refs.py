#!/usr/bin/env python3
"""Static guard: every `self.<attr>` referenced in WorkbenchApp must be defined
(as a method) or assigned (`self.<attr> = ...`) somewhere in the class.

Why this exists: the app's build()/UI code only runs when a Flet session
connects, so `python src/main.py` started headlessly does NOT catch a callback
wired to a non-existent method (e.g. on_submit=self._typo) — it crashes only
when a window opens. This parses the source with ast and flags such references
before you launch.

Usage:  python scripts/check_build_refs.py   (exit 1 if anything is missing)
"""

import ast
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parent.parent / "src" / "main.py"


def main() -> int:
    tree = ast.parse(MAIN.read_text())
    cls = next((n for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == "WorkbenchApp"), None)
    if cls is None:
        print("WorkbenchApp class not found")
        return 1

    defined: set[str] = set()
    for n in ast.walk(cls):
        if isinstance(n, ast.FunctionDef):
            defined.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) \
                        and t.value.id == "self":
                    defined.add(t.attr)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Attribute) \
                and getattr(n.target.value, "id", None) == "self":
            defined.add(n.target.attr)

    accessed: dict[str, int] = {}
    for n in ast.walk(cls):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                and n.value.id == "self":
            accessed.setdefault(n.attr, n.lineno)

    missing = {a: ln for a, ln in accessed.items() if a not in defined}
    if missing:
        print("MISSING self.* references in WorkbenchApp:")
        for a, ln in sorted(missing.items(), key=lambda x: x[1]):
            print(f"  line {ln}: self.{a}")
        return 1
    print(f"OK — {len(defined)} defined, {len(accessed)} distinct self.* references, none missing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
