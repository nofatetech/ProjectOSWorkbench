#!/usr/bin/env bash
# Preflight gate — run before AND after every refactor step (god-object
# extraction). These four checks together catch the failure modes of moving
# code between modules without a real functional test suite:
#   1. ruff      — undefined names, unused imports, redefinitions, bugbears
#   2. mypy      — bad attribute / wrong-arg type errors (lenient config)
#   3. build_refs — every self.<attr> in WorkbenchApp is defined (AST)
#   4. smoke      — the full UI tree constructs against a mock page
# Exit nonzero on the first failure. Run from the edition root.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
RUFF="uvx ruff"

echo "── 1/4 ruff ──────────────────────────────"
$RUFF check src/ scripts/

echo "── 2/4 mypy ──────────────────────────────"
# mypy is advisory during the refactor (lenient config); don't fail the gate on
# it yet — print results and continue. Flip to `||` removal once a module is clean.
.venv/bin/mypy 2>&1 | tail -5 || true

echo "── 3/4 build_refs ────────────────────────"
$PY scripts/check_build_refs.py

echo "── 4/4 smoke build ───────────────────────"
$PY scripts/smoke_build.py

echo "✓ preflight clean"
