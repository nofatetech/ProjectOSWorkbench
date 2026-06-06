"""Thread persistence — JSON app-state under ~/.workbench/threads/.

One file per thread: <thread-id>.json. Threads survive restart (the v0→v0.2
limitation was in-memory only). We serialize the conversation tree (turns +
root/leaf pointers); the per-thread `team` field is intentionally dropped — chat
went single-agent in v0.2, so there's nothing to restore there.

Kept dict-based on purpose: main.py owns the Thread/Turn dataclasses and
rehydrates these dicts itself, which avoids a circular import (main imports
store, not the other way around). All functions are best-effort — persistence
should never crash the app, so failures are swallowed.
"""

import json
from pathlib import Path

THREADS_DIR = Path.home() / ".workbench" / "threads"


def _thread_path(thread_id: str) -> Path:
    return THREADS_DIR / f"{thread_id}.json"


def save_thread(thread) -> None:
    """Persist one thread (a main.Thread) to disk. Best-effort; never raises."""
    try:
        THREADS_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "id": thread.id,
            "name": thread.name,
            "project_id": thread.project_id,
            "root_id": thread.root_id,
            "current_leaf_id": thread.current_leaf_id,
            "system_prompt_override": thread.system_prompt_override,
            "turns": [
                {
                    "id": t.id,
                    "parent_id": t.parent_id,
                    "speaker": t.speaker,
                    "text": t.text,
                    "pinned": t.pinned,
                    "tool_steps": getattr(t, "tool_steps", []) or [],
                }
                for t in thread.turns.values()
            ],
        }
        _thread_path(thread.id).write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def delete_thread(thread_id: str) -> None:
    """Remove a thread's file. Best-effort."""
    try:
        _thread_path(thread_id).unlink(missing_ok=True)
    except Exception:
        pass


def load_thread_dicts() -> list[dict]:
    """Return raw thread dicts from disk, newest-mtime first. main.py turns these
    back into Thread/Turn objects (kept here as plain dicts to avoid importing
    main)."""
    if not THREADS_DIR.exists():
        return []
    out: list[dict] = []
    for f in sorted(
        THREADS_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            continue
    return out
