"""Vault read/write layer — load the Obsidian vault into models, write changes back.

Pure I/O over `.md` files: no UI, no app state, no mutable globals. Every function
takes the vault root (or a concrete path) explicitly, so the same code serves any
vault the app is pointed at. main.py keeps the live `VAULT_PATH` and passes it in.
"""

import re
from datetime import date
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import flet as ft
import frontmatter

from models import (Agent, Area, Project, Task, Note, FileItem, ProjectContent,
                    InboxItem, VaultEntry, AppState, OpenTab)


# Default vault = the bundled `./vault` symlink, resolved THROUGH the link to its
# real target (e.g. ~/Vault1). Resolving matters: launching lazygit / a terminal
# on the unresolved symlink path lands inside the app repo, not the real vault.
_DEFAULT_VAULT_PATH = (Path(__file__).resolve().parent.parent / "vault").resolve()


def _resolve_vault_path(raw: str) -> Path:
    """Vault root from a config value. Empty → the bundled ./vault (resolved).
    Non-empty → that path, `~`-expanded and resolved."""
    if not raw or not raw.strip():
        return _DEFAULT_VAULT_PATH
    try:
        return Path(raw).expanduser().resolve()
    except Exception:
        return _DEFAULT_VAULT_PATH


def _project_id_from_name(name: str) -> str:
    safe = "".join(c.lower() if c.isalnum() else "_" for c in name)
    return f"p_{safe}"


def _estimate_tokens(text: str) -> int:
    # Rough heuristic: ~4 chars per token. Good enough for budgeting.
    return len(text) // 4


def _load_project_from_md(md_path: Path, vault_root: Path) -> Optional[Project]:
    try:
        post = frontmatter.load(str(md_path))
    except Exception:
        return None
    if post.metadata.get("type") != "project":
        return None

    folder = md_path.parent
    is_top_level = folder.name == "10_Projects"
    # A "folder-main" project owns the whole folder (its main .md matches the folder name).
    # Otherwise (top-level file, or nested sub-project file) its scope is just the file.
    is_folder_main = (not is_top_level and md_path.stem == folder.name)

    if is_folder_main:
        context_files = sorted(f.name for f in folder.glob("*.md"))
        try:
            vault_folder = str(folder.relative_to(vault_root)) + "/"
        except ValueError:
            vault_folder = str(folder) + "/"
        files_for_tokens = list(folder.glob("*.md"))
    else:
        context_files = [md_path.name]
        try:
            vault_folder = str(md_path.relative_to(vault_root))
        except ValueError:
            vault_folder = str(md_path)
        files_for_tokens = [md_path]

    total_chars = 0
    for f in files_for_tokens:
        try:
            total_chars += len(f.read_text(encoding="utf-8"))
        except Exception:
            pass

    name = md_path.stem
    hypothesis_raw = post.metadata.get("hypothesis", "") or ""
    hypothesis = hypothesis_raw if isinstance(hypothesis_raw, str) else str(hypothesis_raw)

    modes_raw = post.metadata.get("modes", None)
    if isinstance(modes_raw, list):
        modes = [str(m).strip().lower() for m in modes_raw if str(m).strip()]
    elif isinstance(modes_raw, str) and modes_raw.strip():
        modes = [modes_raw.strip().lower()]
    else:
        modes = []

    tags_raw = post.metadata.get("tags", None)
    if isinstance(tags_raw, list):
        tags = [str(t).strip().lower() for t in tags_raw if str(t).strip()]
    elif isinstance(tags_raw, str) and tags_raw.strip():
        tags = [tags_raw.strip().lower()]
    else:
        tags = []

    # Demand probe: detected liberally from the `demand-probe` token in either
    # tags or modes (the vault convention puts it in `tags:`). Optional `probe:`
    # block carries the tracking fields surfaced on Home.
    is_probe = "demand-probe" in tags or "demand-probe" in modes
    probe_raw = post.metadata.get("probe", None)
    probe = dict(probe_raw) if isinstance(probe_raw, dict) else {}

    review = _coerce_date(post.metadata.get("review"))
    started = _coerce_date(post.metadata.get("started"))
    scope = str(post.metadata.get("scope", "") or "")
    mc_raw = post.metadata.get("micro-commitment", "") or ""
    micro_commitment = mc_raw if isinstance(mc_raw, str) else str(mc_raw)

    return Project(
        id=_project_id_from_name(name),
        name=name,
        vault_folder=vault_folder,
        status=str(post.metadata.get("status", "idea")),
        hypothesis=hypothesis,
        area=str(post.metadata.get("area", "") or ""),
        modes=modes,
        tags=tags,
        working_dir=str(post.metadata.get("working_dir", "") or ""),
        context_files=context_files,
        context_tokens=total_chars // 4,
        threads=[],
        is_probe=is_probe,
        probe=probe,
        review=review,
        scope=scope,
        started=started,
        micro_commitment=micro_commitment,
    )


def _load_area_from_md(md_path: Path) -> Optional[Area]:
    try:
        post = frontmatter.load(str(md_path))
    except Exception:
        return None
    if post.metadata.get("type") != "area":
        return None
    name = str(post.metadata.get("area") or md_path.stem)
    body = (post.content or "").strip()
    description = ""
    if body:
        for line in body.split("\n"):
            clean = line.strip().lstrip("#").strip()
            if clean and not clean.startswith("[["):
                description = clean[:160]
                break
    return Area(
        name=name,
        folder=str(md_path.parent),
        description=description,
        status=str(post.metadata.get("status", "active")),
        source_path=str(md_path),
    )


def _load_agent_from_md(md_path: Path) -> Optional[Agent]:
    try:
        post = frontmatter.load(str(md_path))
    except Exception:
        return None
    if post.metadata.get("type") != "agent":
        return None
    icon_name = str(post.metadata.get("icon", "smart_toy")).upper()
    icon = getattr(ft.Icons, icon_name, ft.Icons.SMART_TOY)
    return Agent(
        name=md_path.stem,
        role=post.content.strip(),
        model=str(post.metadata.get("model", "anthropic/claude-opus-4-7")),
        icon=icon,
        source_path=str(md_path),
    )


def load_inbox_items(vault_path: Path) -> list[InboxItem]:
    """Scan vault/00_Inbox/*.md. Returns items sorted newest-first by mtime."""
    items: list[InboxItem] = []
    inbox_dir = vault_path / "00_Inbox"
    if not inbox_dir.exists():
        return items
    for entry in sorted(inbox_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(entry))
            st = entry.stat()
        except Exception:
            continue
        items.append(InboxItem(
            name=entry.stem,
            path=str(entry),
            size=st.st_size,
            mtime=st.st_mtime,
            note_type=str(post.metadata.get("type", "")).lower(),
            summary=_first_body_line(post),
        ))
    items.sort(key=lambda x: x.mtime, reverse=True)
    return items


def load_vault_entries(vault_path: Path, subfolder: str) -> list[VaultEntry]:
    """Recursively scan vault/<subfolder>/**/*.md. Returns entries sorted alphabetically by name."""
    entries: list[VaultEntry] = []
    root = vault_path / subfolder
    if not root.exists():
        return entries
    for entry in sorted(root.rglob("*.md")):
        try:
            post = frontmatter.load(str(entry))
            st = entry.stat()
        except Exception:
            continue
        try:
            sub = str(entry.parent.relative_to(root))
        except ValueError:
            sub = ""
        if sub == ".":
            sub = ""
        entries.append(VaultEntry(
            name=entry.stem,
            path=str(entry),
            subfolder=sub,
            size=st.st_size,
            mtime=st.st_mtime,
            note_type=str(post.metadata.get("type", "")).lower(),
            summary=_first_body_line(post),
        ))
    entries.sort(key=lambda x: x.name.lower())
    return entries


# ----------------------------------------------------------------------------
# Reviews / reflections — date helpers, reflection scan, frontmatter writeback
# ----------------------------------------------------------------------------

def _coerce_date(raw) -> Optional[date]:
    """Frontmatter dates parse as a `date` already (unquoted YAML); strings are
    tolerated too. Returns None for anything unparseable / missing."""
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return date.fromisoformat(raw.strip()[:10])
        except ValueError:
            return None
    return None


def _iso_week(d: date) -> str:
    """ISO week label, e.g. '2026-W22' (matches the weekly-review skill)."""
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


@dataclass
class Reflection:
    """One weekly reflection note in _System/Reflections/."""
    week: str        # 'YYYY-Www'
    path: str        # absolute path
    filled: bool     # body has real content past the template skeleton
    mtime: float


def _reflection_filled(body: str) -> bool:
    """Stub-vs-filled heuristic (no new frontmatter needed): a reflection counts
    as 'filled' once any template bullet has real text after its marker — a
    `key:` / `key →` value, or a checked / non-empty checkbox. An untouched copy
    of the template (all blank bullets) reads as 'started' (a stub)."""
    for raw in body.splitlines():
        s = raw.strip()
        if not s.startswith("- "):
            continue
        content = s[2:].strip()
        m = re.match(r"\[[ xX]\]\s*(.*)", content)
        if m:
            if m.group(1).strip():
                return True
            continue
        for sep in (":", "→"):
            if sep in content and content.split(sep, 1)[1].strip():
                return True
    return False


def load_reflections(vault_path: Path) -> list[Reflection]:
    """Scan _System/Reflections/*.md → Reflection per note (week from frontmatter
    `week:` or the filename stem)."""
    out: list[Reflection] = []
    root = vault_path / "_System" / "Reflections"
    if not root.exists():
        return out
    for entry in sorted(root.glob("*.md")):
        try:
            post = frontmatter.load(str(entry))
            st = entry.stat()
        except Exception:
            continue
        week = str(post.metadata.get("week") or entry.stem).strip()
        out.append(Reflection(
            week=week, path=str(entry),
            filled=_reflection_filled(post.content or ""),
            mtime=st.st_mtime,
        ))
    return out


def _set_fm_field(fm_block: str, key: str, value: Optional[str]) -> str:
    """Set/replace a top-level scalar key in a raw YAML frontmatter block,
    preserving key order + the rest of the block verbatim. value=None drops the
    key. Targeted line edit (not a yaml round-trip) so hand-formatting survives."""
    lines = fm_block.split("\n")
    out: list[str] = []
    found = False
    for ln in lines:
        if re.match(rf"^{re.escape(key)}\s*:", ln):
            found = True
            if value is None:
                continue
            out.append(f"{key}: {value}")
        else:
            out.append(ln)
    if not found and value is not None:
        out.append(f"{key}: {value}")
    return "\n".join(out)


def _append_under_heading(body: str, heading: str, line: str,
                          newest_first: bool = False) -> str:
    """Insert `line` under a `## Heading` section in a note body. newest_first →
    right after the heading (top of section); else at the end of the section
    (before the next `## ` or EOF). Creates the section (before `## Links` if
    present, else at the end) when the heading is absent. `heading` includes the
    `## ` prefix."""
    if heading in body:
        h_start = body.index(heading)
        h_end = body.find("\n", h_start)
        if h_end == -1:
            return body.rstrip("\n") + "\n" + line + "\n"
        if newest_first:
            return body[:h_end + 1] + line + "\n" + body[h_end + 1:]
        nxt = body.find("\n## ", h_end)
        if nxt == -1:
            return body.rstrip("\n") + "\n" + line + "\n"
        return body[:nxt].rstrip("\n") + "\n" + line + body[nxt:]
    section = f"{heading}\n{line}\n"
    marker = "## Links"
    if marker in body:
        i = body.index(marker)
        return body[:i] + section + "\n" + body[i:]
    return body.rstrip("\n") + "\n\n" + section


def _append_status_log_line(body: str, line: str) -> str:
    """Status transitions: newest-first under `## Status log`."""
    return _append_under_heading(body, "## Status log", line, newest_first=True)


def append_to_main_note(path: Path, heading: str, line: str,
                        newest_first: bool = False) -> None:
    """Append `line` under `heading` in a note's body, preserving frontmatter."""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if m:
        fm, body = m.group(1), m.group(2)
        body = _append_under_heading(body, heading, line, newest_first)
        path.write_text(f"---\n{fm}\n---\n{body}", encoding="utf-8")
    else:
        path.write_text(_append_under_heading(text, heading, line, newest_first),
                        encoding="utf-8")


def _slugify(text: str, fallback: str = "note") -> str:
    first = text.strip().splitlines()[0] if text.strip() else fallback
    slug = re.sub(r"[^\w\- ]", "", first).strip().replace(" ", "-")[:48]
    return slug or fallback


def create_project_note(folder: Path, note_type: str, title: str) -> Path:
    """Create a typed note (`type: <note_type>`) in a project folder, slug from
    the title, de-duped. Returns the path written."""
    folder.mkdir(parents=True, exist_ok=True)
    slug = _slugify(title, fallback=note_type)
    path = folder / f"{slug}.md"
    n = 2
    while path.exists():
        path = folder / f"{slug}-{n}.md"
        n += 1
    path.write_text(f"---\ntype: {note_type}\n---\n\n# {title}\n", encoding="utf-8")
    return path


def promote_to_folder(project: Project, vault_root: Path) -> Optional[str]:
    """Move a file-scoped project (`10_Projects/Foo.md`) into its own folder
    (`10_Projects/Foo/Foo.md`) so it can hold posts/journals/notes/files. Mutates
    the live Project's vault_folder + context_files. Returns an error string, or
    None on success."""
    if project.vault_folder.endswith("/"):
        return "already a folder project"
    src = vault_root / project.vault_folder
    if not src.is_file():
        return f"main note not found: {src}"
    name = src.stem
    folder = src.parent / name
    if folder.exists():
        return f"a '{name}' folder already exists here"
    folder.mkdir()
    dst = folder / f"{name}.md"
    src.rename(dst)
    try:
        rel = str(folder.relative_to(vault_root)) + "/"
    except ValueError:
        rel = str(folder) + "/"
    project.vault_folder = rel
    project.context_files = [f"{name}.md"]
    return None


def write_status_review(path: Path, new_status: str,
                        new_review: Optional[date], old_status: str) -> None:
    """Write `status:` + `review:` to a project note's frontmatter and append a
    transition line to its `## Status log`. Frontmatter edited as text (order +
    comments preserved); review cleared when new_review is None."""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if m:
        fm, body = m.group(1), m.group(2)
    else:
        fm, body = "", text
    fm = _set_fm_field(fm, "status", new_status)
    fm = _set_fm_field(fm, "review",
                       new_review.isoformat() if new_review else None)
    log_line = f"- {date.today().isoformat()} — status: {old_status}→{new_status}"
    body = _append_status_log_line(body, log_line)
    path.write_text(f"---\n{fm}\n---\n{body}", encoding="utf-8")


def load_state_from_vault(vault_path: Path) -> AppState:
    state = AppState()
    if not vault_path.exists():
        return state

    # Agents from _System/Agents/
    agents_dir = vault_path / "_System" / "Agents"
    if agents_dir.exists():
        for f in sorted(agents_dir.glob("*.md")):
            a = _load_agent_from_md(f)
            if a:
                state.agents.append(a)

    # Areas from 20_Areas/ — accept any .md file with `type: area` frontmatter,
    # not just the strict <folder>/<folder>.md pattern (some areas use README naming).
    areas_dir = vault_path / "20_Areas"
    if areas_dir.exists():
        for entry in sorted(areas_dir.iterdir()):
            if entry.is_dir():
                # Try strict pattern first; fall back to any type:area .md in the folder
                main_md = entry / f"{entry.name}.md"
                area = _load_area_from_md(main_md) if main_md.exists() else None
                if not area:
                    for f in sorted(entry.glob("*.md")):
                        a = _load_area_from_md(f)
                        if a:
                            area = a
                            break
                if area:
                    state.areas.append(area)
            elif entry.is_file() and entry.suffix == ".md":
                area = _load_area_from_md(entry)
                if area:
                    state.areas.append(area)

    # Projects from 10_Projects/ — recursive so nested sub-projects (e.g. Project OS/Workbench.md)
    # also surface. _load_project_from_md returns None for anything without `type: project`.
    projects_dir = vault_path / "10_Projects"
    if projects_dir.exists():
        for md_path in sorted(projects_dir.rglob("*.md")):
            # Skip _Archive subtrees if any happen to live inside 10_Projects
            if "_Archive" in md_path.parts:
                continue
            p = _load_project_from_md(md_path, vault_path)
            if p:
                state.projects.append(p)

    # Sort alphabetical (case-insensitive). Status filtering happens at render time.
    state.projects.sort(key=lambda p: p.name.lower())

    # Inbox: vault/00_Inbox/*.md — capture before triage
    state.inbox_items = load_inbox_items(vault_path)
    # Resources + People: top-level vault surfaces browsed via sidebar pinned rows
    state.resources = load_vault_entries(vault_path, "30_Resources")
    state.people = load_vault_entries(vault_path, "40_People")

    # Initial tab: Home (the launcher / today view)
    home = OpenTab(kind="home", ref_id="home")
    state.open_tabs = [home]
    state.active_tab = home

    return state


# Multi-state checkboxes. Group 2 is any single char inside the brackets so the
# parser sees `[/]` / `[-]` (Obsidian-Tasks convention) too, not just space/x.
_CHECKBOX_RE = re.compile(r"^(\s*)-\s+\[([^\]])\]\s+(.*)$")

# Canonical task states ↔ the checkbox char written to markdown. Order is the
# board's left→right column order. Unknown chars (e.g. `[>]`) read as "todo" so
# nothing is dropped; writeback only ever emits the chars below.
TASK_STATES = ("todo", "doing", "paused", "done")
TASK_CHAR_BY_STATE = {"todo": " ", "doing": "/", "paused": "-", "done": "x"}
_STATE_BY_CHAR = {" ": "todo", "/": "doing", "-": "paused", "x": "done", "X": "done"}


def task_state_for_char(ch: str) -> str:
    """Map a raw checkbox char to a canonical state; unknown → 'todo'."""
    return _STATE_BY_CHAR.get(ch, "todo")


def _project_base_path(project: Project, vault_root: Path) -> Path:
    """Resolve a project's base path. For folder projects it's the dir;
    for top-level file projects it's the .md file itself."""
    return vault_root / project.vault_folder.rstrip("/")


def _scan_tasks(project: Project, vault_root: Path) -> list[Task]:
    """Parse `- [ ]` / `- [/]` / `- [-]` / `- [x]` checkboxes from every .md
    file in the project, tagged with the project's id/name for the board."""
    base = _project_base_path(project, vault_root)
    md_files: list[Path] = []
    if base.is_file():
        md_files = [base]
    elif base.is_dir():
        md_files = sorted(p for p in base.glob("*.md"))
        inbox_dir = base / "Inbox"
        if inbox_dir.is_dir():
            md_files.extend(sorted(p for p in inbox_dir.glob("*.md")))

    tasks: list[Task] = []
    for f in md_files:
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        for i, line in enumerate(content.split("\n"), start=1):
            m = _CHECKBOX_RE.match(line)
            if not m:
                continue
            tasks.append(Task(
                text=m.group(3).strip(),
                state=task_state_for_char(m.group(2)),
                source_path=str(f),
                line_number=i,
                project_id=project.id,
                project_name=project.name,
            ))
    return tasks


def scan_all_tasks(projects: list[Project], vault_root: Path) -> list[Task]:
    """Global sweep: every project's checkboxes in one flat list."""
    tasks: list[Task] = []
    for p in projects:
        tasks.extend(_scan_tasks(p, vault_root))
    return tasks


def _rewrite_task_char(task: Task, new_char: str) -> bool:
    """Read source file, replace the checkbox char at task.line_number, write
    back. Returns True on success, False on any failure."""
    p = Path(task.source_path)
    try:
        content = p.read_text(encoding="utf-8")
    except Exception:
        return False
    lines = content.split("\n")
    if not (0 < task.line_number <= len(lines)):
        return False
    m = _CHECKBOX_RE.match(lines[task.line_number - 1])
    if not m:
        return False
    indent, rest = m.group(1), m.group(3)
    lines[task.line_number - 1] = f"{indent}- [{new_char}] {rest}"
    try:
        p.write_text("\n".join(lines), encoding="utf-8")
        return True
    except Exception:
        return False


def set_task_state(task: Task, new_state: str) -> bool:
    """Write `new_state` (a TASK_STATES key) back to the source checkbox and
    update the in-memory task. No-op-safe; returns False on bad state / IO."""
    new_char = TASK_CHAR_BY_STATE.get(new_state)
    if new_char is None:
        return False
    if _rewrite_task_char(task, new_char):
        task.state = new_state
        return True
    return False


def toggle_task(task: Task) -> bool:
    """Flip a task between done and todo (legacy open/done callers). Multi-state
    moves go through set_task_state."""
    return set_task_state(task, "todo" if task.state == "done" else "done")


def _first_body_line(post) -> str:
    body = (post.content or "").strip()
    if not body:
        return ""
    for line in body.split("\n"):
        clean = line.strip().lstrip("#").strip()
        if clean:
            return clean[:140]
    return ""


def scan_project_content(project: Project, vault_root: Path) -> ProjectContent:
    """One pass over the project folder; returns tasks + classified notes + files."""
    pc = ProjectContent(tasks=_scan_tasks(project, vault_root))
    base = _project_base_path(project, vault_root)
    if not base.exists():
        return pc

    # Top-level file projects have no folder content beyond the file itself
    if base.is_file():
        return pc

    main_name = f"{base.name}.md"  # the project's own main .md (skip from notes)

    # Non-markdown files at the project root
    for entry in sorted(base.iterdir()):
        if entry.is_file() and entry.suffix.lower() != ".md":
            try:
                size = entry.stat().st_size
            except Exception:
                size = 0
            pc.files.append(FileItem(
                name=entry.name, path=str(entry),
                suffix=entry.suffix.lower(), size=size,
            ))

    # Markdown notes at the project root (classified)
    for entry in sorted(base.glob("*.md")):
        if entry.name == main_name:
            continue
        try:
            post = frontmatter.load(str(entry))
        except Exception:
            continue
        ntype = str(post.metadata.get("type", "")).lower()
        if ntype in ("project", "thread"):
            continue  # nested project files / persisted threads handled elsewhere
        note = Note(
            name=entry.stem, path=str(entry),
            note_type=ntype, summary=_first_body_line(post),
        )
        if ntype in ("post", "journal"):
            pc.posts.append(note)
        else:
            pc.wiki.append(note)

    # Inbox subfolder
    inbox_dir = base / "Inbox"
    if inbox_dir.is_dir():
        for entry in sorted(inbox_dir.glob("*.md")):
            try:
                post = frontmatter.load(str(entry))
            except Exception:
                continue
            ntype = str(post.metadata.get("type", "")).lower()
            pc.inbox.append(Note(
                name=entry.stem, path=str(entry),
                note_type=ntype, summary=_first_body_line(post),
            ))

    return pc
