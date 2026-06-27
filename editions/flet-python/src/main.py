"""Workbench v0 — UI shell, now reading from a real Obsidian vault.

Layout: sidebar (PROJECTS + AGENTS) + main area (tabs + pluggable view slot).
Views: ChatView (chat tab), ProjectOverviewView (overview tab), AgentView (agent tab).

Data source:
  ./vault is a symlink to the user's Obsidian vault root (gitignored).
  Projects: anything under ./vault/10_Projects/ with `type: project` frontmatter
            (top-level .md files, or folder/<folder>.md).
  Agents:   anything under ./vault/_System/Agents/*.md with `type: agent`.
  Threads:  persisted as JSON under ~/.workbench/threads/ (see store.py),
            rehydrated into their project on startup.

Architecture: ./vault/10_Projects/Project OS/Workbench.md (§ v0 Architecture).
"""

import atexit
import os
import re
import sys
import threading
import time
import uuid
from datetime import date
from collections import Counter
from pathlib import Path
from typing import Optional

import flet as ft
import frontmatter

import json

from brain import brain_for, clamp_max_tokens
from config import (CONFIG_PATH, load_config, save_config,
                    load_telegram_cfg, save_telegram_cfg, load_title_themes)
from store import save_thread, load_thread_dicts
from tools import ToolContext, execute_tool, schemas_for, MUTATING_TOOLS
import publish
from models import (Agent, Turn, Thread, Area, Project, Task, Note, FileItem,
                    InboxItem, VaultEntry, OpenTab)
from theme import (STATUS_COLORS, PLATINUM, _all_border, _bevel, _raised, _recessed, _sticky)
from vault import (_DEFAULT_VAULT_PATH, _resolve_vault_path, _load_project_from_md,
                   load_inbox_items, load_vault_entries, load_reflections,
                   _coerce_date, _iso_week, append_to_main_note,
                   create_project_note, promote_to_folder, write_status_review,
                   load_state_from_vault, _scan_tasks,
                   toggle_task, scan_project_content)
from views.people import PeopleView
from osactions import (_resolve_working_dir, _scan_working_dir, _git_short_status,
                       open_in_editor, open_in_terminal, reveal_in_files, _is_git_repo, open_in_git_ui, open_in_obsidian,
                       sync_context_symlinks, open_cli_session,
                       list_session_images, import_session_image)
from telegram_daemon import TelegramDaemon


# Live vault root. Initialised from the bundled default; repointed at runtime by
# _on_save_and_reload_vault (and __init__) from the config. Passed explicitly into
# the vault.py / osactions.py functions — they never read this global themselves.
VAULT_PATH = _DEFAULT_VAULT_PATH


# Fonts loaded into the Flet page (single source of truth — feeds page.fonts and
# the Settings title-font picker). Newsreader (titles/body serif) + JetBrains
# Mono (paths/code) are the workhorses; the rest are title-experiment display
# faces selectable per-install in Settings → TITLE STYLE.
FONT_SOURCES = {
    "Newsreader": "https://github.com/google/fonts/raw/main/ofl/newsreader/Newsreader%5Bopsz%2Cwght%5D.ttf",
    "JetBrains Mono": "https://github.com/JetBrains/JetBrainsMono/raw/master/fonts/variable/JetBrainsMono%5Bwght%5D.ttf",
    "Caveat": "https://github.com/google/fonts/raw/main/ofl/caveat/Caveat%5Bwght%5D.ttf",          # handwritten marker
    "Pacifico": "https://github.com/google/fonts/raw/main/ofl/pacifico/Pacifico-Regular.ttf",       # casual script
    "Lobster": "https://github.com/google/fonts/raw/main/ofl/lobster/Lobster-Regular.ttf",          # bold retro script
    "Bebas Neue": "https://github.com/google/fonts/raw/main/ofl/bebasneue/BebasNeue-Regular.ttf",   # tall condensed display
    "Fredoka": "https://github.com/google/fonts/raw/main/ofl/fredoka/Fredoka%5Bwght%5D.ttf",        # rounded playful
    "Archivo Black": "https://github.com/google/fonts/raw/main/ofl/archivoblack/ArchivoBlack-Regular.ttf",  # heavy poster
}
# Fonts offered in the title-style picker (Newsreader first = the default).
TITLE_FONTS = list(FONT_SOURCES.keys())


# ----------------------------------------------------------------------------
# Display helpers (formatters used across views)
# ----------------------------------------------------------------------------


def _age_string(ts: float, ago: bool = False) -> str:
    """Compact age label for an mtime: just now / 5m / 2h 10m / 3d 3h / 4mo 2d / 2y 3mo.

    Past the minute mark each label carries a second, finer unit (when non-zero) so
    "3d" reads "3d 3h" — coarse-unit-only labels hide up to a full unit of age.
    Pass ago=True to append " ago" (kept off "just now")."""
    age = time.time() - ts
    if age < 60:
        return "just now"
    suffix = " ago" if ago else ""
    if age < 3600:
        return f"{int(age // 60)}m{suffix}"
    if age < 86400:
        h, m = int(age // 3600), int((age % 3600) // 60)
        return (f"{h}h {m}m" if m else f"{h}h") + suffix
    if age < 86400 * 30:
        d, h = int(age // 86400), int((age % 86400) // 3600)
        return (f"{d}d {h}h" if h else f"{d}d") + suffix
    if age < 86400 * 365:
        mo, d = int(age // (86400 * 30)), int((age % (86400 * 30)) // 86400)
        return (f"{mo}mo {d}d" if d else f"{mo}mo") + suffix
    y, mo = int(age // (86400 * 365)), int((age % (86400 * 365)) // (86400 * 30))
    return (f"{y}y {mo}mo" if mo else f"{y}y") + suffix


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n // 1024} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n // (1024 * 1024)} MB"
    return f"{n // (1024 * 1024 * 1024)} GB"










def build_messages_for_agent(agent: Agent, history: list[Turn]) -> list[dict]:
    """OpenAI-format messages: agent role as system prompt + thread history.

    Used for the legacy per-agent message build (composition B). Retired in v0.2.
    """
    messages: list[dict] = [{"role": "system", "content": agent.role}]
    for turn in history:
        if not turn.text:
            continue
        role = "user" if turn.speaker == "user" else "assistant"
        messages.append({"role": role, "content": turn.text})
    return messages


# Vault context cap. ~4 chars/token is a rough but adequate estimate (matches
# how Project.context_tokens is computed at load time).
VAULT_CONTEXT_TOKEN_CAP = 30_000
_CHARS_PER_TOKEN = 4
# Per-persona role cap — send the full role (not just the first paragraph) so
# "ask the CFO" channels it faithfully, but bound it so a long role can't crowd
# out the rest of the prompt.
_PERSONA_ROLE_CHAR_CAP = 2_000


def _project_context_paths(project: Project) -> list[tuple[str, Path]]:
    """(vault-relative display name, absolute path) for each of the project's
    context files — main project note first, then sub-notes by most-recently-
    modified. The display name is the cite-by-filename header the prompt uses."""
    base = VAULT_PATH / project.vault_folder
    if project.vault_folder.endswith("/"):
        paths = [base / name for name in project.context_files]
    else:
        # File-scoped project: vault_folder IS the single note.
        paths = [base]

    def sort_key(p: Path):
        is_main = 0 if p.stem == project.name else 1
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (is_main, -mtime)  # main note first, then newest-modified

    paths.sort(key=sort_key)
    out: list[tuple[str, Path]] = []
    for p in paths:
        try:
            rel = str(p.relative_to(VAULT_PATH))
        except ValueError:
            rel = p.name
        out.append((rel, p))
    return out


def _build_vault_context_block(project: Project,
                               token_cap: int = VAULT_CONTEXT_TOKEN_CAP) -> str:
    """Inject the project's .md files into the system prompt, cite-by-filename,
    truncated at token_cap (main note first, then sub-notes by recency). Live
    read — picks up vault edits on the next turn."""
    char_budget = max(0, token_cap) * _CHARS_PER_TOKEN
    sections: list[str] = []
    for rel, path in _project_context_paths(project):
        if char_budget <= 0:
            break
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if len(text) > char_budget:
            text = text[:char_budget] + "\n…[truncated — context cap reached]"
            char_budget = 0
        else:
            char_budget -= len(text)
        sections.append(f"--- {rel} ---\n{text}")
    if not sections:
        return ""
    header = ("\n## Vault context\n"
              "The current project's notes follow. Cite them by filename when "
              "relevant. This is a live read — it reflects the vault as of now.\n")
    return header + "\n\n".join(sections)


def _history_messages(history: list["Turn"]) -> list[dict]:
    """Serialize thread history to chat messages. A team turn that ran tools is
    replayed as the genuine exchange — an assistant `tool_calls` message, then a
    `tool` result message per call, then the assistant's final text — so the
    model sees what actually happened (and the results) rather than the bare
    narrated prose it used to get. Empty turns are skipped."""
    msgs: list[dict] = []
    for turn in history:
        if turn.speaker == "user":
            if turn.text:
                msgs.append({"role": "user", "content": turn.text})
            continue
        # team (assistant) turn
        steps = getattr(turn, "tool_steps", None) or []
        if steps:
            msgs.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": s.get("id") or f"call_{i}", "type": "function",
                     "function": {"name": s.get("name", ""),
                                  "arguments": s.get("arguments") or "{}"}}
                    for i, s in enumerate(steps)
                ],
            })
            for i, s in enumerate(steps):
                msgs.append({"role": "tool",
                             "tool_call_id": s.get("id") or f"call_{i}",
                             "content": s.get("result", "")})
        if turn.text:
            msgs.append({"role": "assistant", "content": turn.text})
    return msgs


def build_messages_for_general_agent(
    general: Agent,
    persona_library: list[Agent],
    project: Optional[Project],
    working_dir_summary: Optional[str],
    history: list[Turn],
    system_preamble: str = "",
    context_token_cap: int = VAULT_CONTEXT_TOKEN_CAP,
    tools_available: bool = False,
    delegate_available: bool = False,
    publish_available: bool = False,
    system_override: Optional[str] = None,
) -> list[dict]:
    """Single-agent chat (v0.2): one system prompt assembled from an optional user
    preamble + general agent role + persona library reference + current project
    context + working_dir info, followed by full thread history. The general agent
    can role-switch into other personas when invoked in conversation.

    If system_override is set (per-thread frozen prompt), it is used verbatim as
    the system message and assembly is skipped."""
    if system_override is not None:
        return [{"role": "system", "content": system_override}, *_history_messages(history)]

    parts: list[str] = []
    if system_preamble.strip():
        parts.append(system_preamble.strip())
    parts.append(general.role)

    if tools_available:
        parts.append(
            "\n## Tools\n"
            "You can act on the vault directly via tools: read_vault_note, "
            "write_vault_note, list_dir, move_note, run_shell. Prefer reading a "
            "file with the tool over guessing its contents. Relative paths are "
            "vault-relative; the working_dir (above) is where run_shell executes. "
            "Trust mode — your writes apply immediately, so be deliberate.\n"
            "CRITICAL: to move, create, edit, or delete anything you MUST call the "
            "tool. Never claim or imply you have moved/created/edited/written a "
            "file unless a tool call has actually returned success this turn — "
            "describing the action in prose does NOT perform it. If you intend to "
            "act, emit the tool call; if you can't yet (need info), say so plainly "
            "instead of pretending it's done."
        )
        if delegate_available:
            parts.append(
                "\nFor heavy, multi-step coding work in the working_dir, you can "
                "delegate_to_claude_code(task) — it runs the Claude Code CLI in the "
                "background and returns a job_id; poll it with check_delegation(job_id). "
                "Use this for real code changes; run_shell is for quick one-offs."
            )
        if publish_available:
            parts.append(
                "\nTo publish a note to the web, call publish_note(path) — it posts "
                "to WordPress.com (draft-first; re-publishing updates the same post). "
                "Categories and tags are set automatically from the note's project & "
                "area, so do NOT pass them. Optional: status (draft/publish), "
                "visibility (public/private/password)."
            )

    others = [a for a in persona_library if a.name != general.name]
    if others:
        parts.append("\n## Available personas you can channel\n"
                     "(When the user invokes one — e.g. \"ask the CFO\" / "
                     "\"what would Architect say\" — switch into that voice "
                     "faithfully, then return to your own.)\n")
        for a in others:
            role = (a.role or "").strip()
            if len(role) > _PERSONA_ROLE_CHAR_CAP:
                role = role[:_PERSONA_ROLE_CHAR_CAP] + "\n…[role truncated]"
            parts.append(f"\n### {a.name}\n{role}")

    if project:
        parts.append(f"\n## Current project: {project.name}")
        parts.append(f"- status: {project.status}")
        if project.area:
            parts.append(f"- area: {project.area}")
        if project.hypothesis:
            parts.append(f"- hypothesis: {project.hypothesis}")
        if working_dir_summary:
            parts.append(f"- working_dir: {working_dir_summary}")

        ctx = _build_vault_context_block(project, context_token_cap)
        if ctx:
            parts.append(ctx)

    system = "\n".join(parts)
    return [{"role": "system", "content": system}, *_history_messages(history)]


# ----------------------------------------------------------------------------
# @-references — fuzzy file/folder picker from the chat input
# ----------------------------------------------------------------------------

# Dirs never worth indexing/referencing.
_ATREF_EXCLUDE = {".git", "node_modules", ".venv", "__pycache__", ".obsidian",
                  "dist", "build", ".pytest_cache", ".mypy_cache", ".idea", ".vscode"}
_ATREF_CAP = 3000  # max entries indexed per source (keeps fuzzy matching snappy)


def _quick_capture_note(text: str) -> Path:
    """Write a capture note into 00_Inbox/. Filename slugged from the first line;
    de-duplicated with a numeric suffix. Returns the path written."""
    inbox = VAULT_PATH / "00_Inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    first = text.strip().splitlines()[0] if text.strip() else "capture"
    slug = re.sub(r"[^\w\- ]", "", first).strip().replace(" ", "-")[:48] or "capture"
    path = inbox / f"{slug}.md"
    n = 2
    while path.exists():
        path = inbox / f"{slug}-{n}.md"
        n += 1
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _trailing_at_query(text: str) -> Optional[tuple[int, str]]:
    """If the text ends with an `@<query>` token (no whitespace after the @),
    return (index_of_@, query); else None. Drives the picker as you type."""
    i = text.rfind("@")
    if i == -1:
        return None
    q = text[i + 1:]
    if any(c.isspace() for c in q):
        return None
    return (i, q)


def _atref_match(entries: list[dict], query: str, limit: int = 8) -> list[dict]:
    """Substring fuzzy match over the index. Empty query → first `limit` entries.
    Ranks filename-prefix > filename-substring > path-substring, then by length."""
    if not query:
        return entries[:limit]
    ql = query.lower()
    scored = []
    for e in entries:
        name, rel = e["name"].lower(), e["insert"].lower()
        if ql in name:
            rank = 0 if name.startswith(ql) else 1
        elif ql in rel:
            rank = 2
        else:
            continue
        scored.append(((rank, len(e["insert"])), e))
    scored.sort(key=lambda x: x[0])
    return [e for _, e in scored[:limit]]


def _walk_atref(base: Path, source: str, to_insert) -> list[dict]:
    """Index files+folders under base. `to_insert(abs_path)` yields the string the
    picker inserts (vault-relative for vault, absolute for working_dir)."""
    out: list[dict] = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _ATREF_EXCLUDE]
        rootp = Path(root)
        for d in sorted(dirs):
            out.append({"insert": to_insert(rootp / d), "name": d,
                        "is_dir": True, "source": source})
        for f in sorted(files):
            if f.startswith("."):
                continue
            out.append({"insert": to_insert(rootp / f), "name": f,
                        "is_dir": False, "source": source})
        if len(out) >= _ATREF_CAP:
            break
    return out[:_ATREF_CAP]


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

class WorkbenchApp:
    def __init__(self, page: ft.Page):
        self.page = page
        # Load config first so the vault path (which may point elsewhere) is known
        # before we read the vault. Sets the module-global VAULT_PATH all the
        # loaders/handlers read at call time.
        cfg = load_config()
        global VAULT_PATH
        VAULT_PATH = _resolve_vault_path(cfg.vault_path)
        self.state = load_state_from_vault(VAULT_PATH)
        self.state.config = cfg
        self._load_persisted_threads()
        self._last_prompt_text = "(no prompt sent yet — send a message first)"
        # Per-message sampling temperature, seeded from Settings; the input-bar
        # control overrides it for the next sends.
        self._chat_temperature = self.state.config.temperature
        # @-reference file index, built+cached per project on first use.
        self._atref_cache: dict[str, list[dict]] = {}
        # Home CHATS list pagination (ephemeral; resets each launch).
        self._scratch_page = 0
        self._scratch_page_size = 8
        # Sidebar AREAS filter: by default show only active projects under an
        # expanded area; toggle reveals inactive (idea/persist/pause/pivot/done).
        self._areas_show_inactive = False
        # Sidebar collapse: hide the whole sidebar body (keep only an expand
        # button) to give the main view full width. Session-only, not persisted.
        self._sidebar_collapsed = False
        # Area view "OTHER PROJECTS" box: collapsed by default (just a count +
        # nudge), click to expand the full sticky grid. Keyed by area name so
        # each area remembers its own toggle within a session.
        self._area_other_expanded: set[str] = set()
        # Reviews due-board column boundaries (editable in the view; persisted to
        # config). Columns: Needs attention · Past due · Today · Next [n1]d · Next [n2]d.
        self._review_n1 = self.state.config.review_window_n1
        self._review_n2 = self.state.config.review_window_n2
        # Telegram → inbox capture daemon (managed subprocess, started from
        # Settings). Stopped on app exit so we don't leave an orphan poller.
        self.telegram = TelegramDaemon()
        atexit.register(self.telegram.stop)

        # control refs — populated in build()
        self.sidebar_root: ft.Container
        self._sidebar_full_content: ft.Control
        self._sidebar_collapsed_content: ft.Control
        self.sidebar_areas_filter_field: ft.TextField
        self.sidebar_projects_col: ft.Column
        self.sidebar_agents_col: ft.Column
        self.tabs_row: ft.Row
        self.view_slot: ft.Container
        # ChatView controls (built once)
        self.team_chip: ft.Text
        self.minimap_toggle_btn: ft.IconButton
        self.thread_view: ft.ListView
        self.minimap_container: ft.Container
        self.minimap_content: ft.Column
        self.input_field: ft.TextField
        self.chat_view_body: ft.Control
        # OverviewView controls
        self.overview_content: ft.Column
        self.overview_view_body: ft.Control
        # AgentView controls
        self.agent_content: ft.Column
        self.agent_view_body: ft.Control
        # SettingsView controls
        self.settings_content: ft.Column
        self.settings_view_body: ft.Control
        self.settings_apikey_field: ft.TextField
        self.settings_mock_switch: ft.Switch
        self.settings_model_field: ft.TextField
        self.settings_model_dropdown: ft.Dropdown
        self.settings_debug_switch: ft.Switch
        self.settings_ollama_field: ft.TextField
        self.settings_save_status: ft.Text
        # TasksView controls
        self.tasks_content: ft.Column
        self.tasks_view_body: ft.Control
        # AreaView controls
        self.area_content: ft.Column
        self.area_view_body: ft.Control
        # InboxView controls
        self.inbox_list_col: ft.Column
        self.inbox_preview_col: ft.Column
        self.inbox_view_body: ft.Control
        # ResourcesView controls
        self.resources_list_col: ft.Column
        self.resources_preview_col: ft.Column
        self.resources_view_body: ft.Control
        # PeopleView — extracted to views/people.py (owns its own controls)
        self.people_view = PeopleView(self)
        # HomeView controls
        self.home_content: ft.Column
        self.home_view_body: ft.Control
        # ReviewsView controls
        self.reviews_content: ft.Column
        self.reviews_view_body: ft.Control
        self.sidebar_reviews_row: ft.Container

    # --- Build ---
    def build(self):
        self.page.title = "Workbench"
        self.page.theme_mode = ft.ThemeMode.LIGHT  # Platinum 9 is a light theme
        self.page.padding = 0
        self.page.bgcolor = self.state.config.main_bg_color
        # Fonts. Newsreader is the closest free font to Apple's New York (modern
        # serif designed for screen, optical sizing). Used for titles + content
        # text that's meant to be read carefully. JetBrains Mono for paths.
        self.page.fonts = dict(FONT_SOURCES)

        self._build_sidebar()
        main_area = self._build_main_area()
        self.chat_view_body = self._build_chat_view_body()
        self.overview_view_body = self._build_overview_view_body()
        self.agent_view_body = self._build_agent_view_body()
        self.settings_view_body = self._build_settings_view_body()
        self.tasks_view_body = self._build_tasks_view_body()
        self.area_view_body = self._build_area_view_body()
        self.inbox_view_body = self._build_inbox_view_body()
        self.resources_view_body = self._build_resources_view_body()
        self.people_view.build_body()
        self.home_view_body = self._build_home_view_body()
        self.reviews_view_body = self._build_reviews_view_body()

        # No divider — sidebar and main flow into each other; main bg color sets the
        # boundary visually if at all.
        self.page.add(
            ft.Row(
                controls=[self.sidebar_root, main_area],
                expand=True,
                spacing=0,
            )
        )
        self.refresh()

    def _build_sidebar(self) -> ft.Container:
        # sidebar_projects_col holds the AREAS section (area cards, each expanding to projects)
        # Pinned top rows (Home/Inbox/Resources/People) rebuilt each refresh.
        self.sidebar_projects_col = ft.Column(spacing=6)
        self.sidebar_areas_header = ft.Container(content=self._build_areas_header())
        self.sidebar_agents_col = ft.Column(spacing=2)
        self.sidebar_home_row = ft.Container()
        self.sidebar_inbox_row = ft.Container()
        self.sidebar_reviews_row = ft.Container()
        self.sidebar_resources_row = ft.Container()
        self.sidebar_people_row = ft.Container()
        # Quick capture: type → Enter (or +) drops a note into 00_Inbox/.
        self.sidebar_capture_field = ft.TextField(
            hint_text="Quick capture → Inbox",
            text_size=12, multiline=False,
            on_submit=self._on_quick_capture,
        )
        sidebar_capture_row = ft.Row(
            controls=[
                self.sidebar_capture_field,
                ft.IconButton(icon=ft.Icons.ADD, tooltip="Add to Inbox",
                              on_click=self._on_quick_capture),
            ],
            spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        # Simple title filter for the AREAS tree — narrows projects shown under
        # areas by substring; non-matching areas drop out while filtering.
        self.sidebar_areas_filter_field = ft.TextField(
            hint_text="Filter projects…",
            value=self.state.areas_filter,
            text_size=12, multiline=False, dense=True,
            prefix_icon=ft.Icons.SEARCH,
            on_change=self._on_areas_filter_change,
        )
        full_content = ft.Column(
                controls=[
                    ft.Column(
                        controls=[
                            ft.Row(
                                controls=[
                                    # Construction-yellow "under construction" badge —
                                    # tools on a rounded amber tile, top-left light source.
                                    ft.Container(
                                        width=38, height=38,
                                        border_radius=8,
                                        bgcolor=ft.Colors.AMBER_400,
                                        border=_all_border(ft.Colors.AMBER_700, 1),
                                        alignment=ft.Alignment.CENTER,
                                        content=ft.Icon(
                                            icon=ft.Icons.CONSTRUCTION_ROUNDED,
                                            size=24, color=ft.Colors.BROWN_900,
                                        ),
                                    ),
                                    ft.Text("Workbench", size=22,
                                            font_family="Newsreader",
                                            weight=ft.FontWeight.BOLD,
                                            expand=True),
                                    ft.IconButton(
                                        icon=ft.Icons.REFRESH,
                                        icon_size=20, tooltip="Reload all from vault",
                                        on_click=self._on_reload_all,
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.KEYBOARD_DOUBLE_ARROW_LEFT,
                                        icon_size=20, tooltip="Collapse sidebar",
                                        on_click=self._on_toggle_sidebar,
                                    ),
                                ],
                                spacing=6,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            ft.Container(height=4),
                            self.sidebar_home_row,
                            self.sidebar_inbox_row,
                            sidebar_capture_row,
                            self.sidebar_reviews_row,
                            self.sidebar_resources_row,
                            self.sidebar_people_row,
                            ft.Container(height=8),
                            self.sidebar_areas_header,
                            self.sidebar_areas_filter_field,
                            self.sidebar_projects_col,
                            ft.Container(height=12),
                            ft.Text("AGENTS", size=10, weight=ft.FontWeight.BOLD,
                                    color=ft.Colors.OUTLINE),
                            self.sidebar_agents_col,
                        ],
                        spacing=8,
                        scroll=ft.ScrollMode.AUTO,
                        expand=True,
                    ),
                    ft.Divider(height=1),
                    ft.TextButton(
                        "Settings", icon=ft.Icons.SETTINGS_OUTLINED,
                        on_click=self._open_settings,
                    ),
                ],
                spacing=4,
                expand=True,
            )
        self._sidebar_full_content = full_content
        # Collapsed rail: just an expand button (and the construction badge for
        # continuity). Clicking either re-opens the sidebar.
        self._sidebar_collapsed_content = ft.Column(
            controls=[
                ft.IconButton(
                    icon=ft.Icons.KEYBOARD_DOUBLE_ARROW_RIGHT,
                    icon_size=20, tooltip="Expand sidebar",
                    on_click=self._on_toggle_sidebar,
                ),
                ft.Container(
                    width=38, height=38, border_radius=8,
                    bgcolor=ft.Colors.AMBER_400,
                    border=_all_border(ft.Colors.AMBER_700, 1),
                    alignment=ft.Alignment.CENTER,
                    tooltip="Expand sidebar",
                    ink=True,
                    content=ft.Icon(icon=ft.Icons.CONSTRUCTION_ROUNDED,
                                    size=24, color=ft.Colors.BROWN_900),
                    on_click=self._on_toggle_sidebar,
                ),
            ],
            spacing=8,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.sidebar_root = ft.Container(
            animate=ft.Animation(180, ft.AnimationCurve.EASE_OUT),
        )
        self._apply_sidebar_collapsed()
        return self.sidebar_root

    def _build_areas_header(self) -> ft.Control:
        # "AREAS" label + a super-simple filter chip toggling whether inactive
        # projects show under expanded areas. Default = active only.
        showing_all = self._areas_show_inactive
        chip = ft.Container(
            padding=ft.Padding(left=8, top=2, right=8, bottom=2),
            border_radius=10,
            bgcolor=(ft.Colors.SURFACE_CONTAINER_HIGH if showing_all else None),
            border=_all_border(),
            ink=True,
            tooltip=("Showing all projects — click to show active only"
                     if showing_all else
                     "Showing active only — click to show all"),
            content=ft.Text("all" if showing_all else "active only",
                            size=9, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.OUTLINE),
            on_click=self._on_toggle_areas_filter,
        )
        return ft.Row(
            controls=[
                ft.Text("AREAS", size=10, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.OUTLINE),
                ft.Container(expand=True),
                chip,
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _on_toggle_areas_filter(self, e):
        self._areas_show_inactive = not self._areas_show_inactive
        self._refresh_sidebar()
        self.page.update()

    def _on_areas_filter_change(self, e):
        self.state.areas_filter = e.control.value or ""
        self._refresh_sidebar()
        self.page.update()

    def _apply_sidebar_collapsed(self):
        """Swap the sidebar between its full body and the collapsed rail."""
        if self._sidebar_collapsed:
            self.sidebar_root.width = 52
            self.sidebar_root.padding = ft.Padding(left=6, top=16, right=6, bottom=12)
            self.sidebar_root.content = self._sidebar_collapsed_content
        else:
            self.sidebar_root.width = 320
            self.sidebar_root.padding = ft.Padding(left=12, top=16, right=12, bottom=12)
            self.sidebar_root.content = self._sidebar_full_content

    def _on_toggle_sidebar(self, e=None):
        self._sidebar_collapsed = not self._sidebar_collapsed
        self._apply_sidebar_collapsed()
        self.page.update()

    def _tab_alive(self, tab: OpenTab) -> bool:
        """True if a tab still resolves after a vault reload. Singleton views
        (home/inbox/…) always do; object-backed tabs need their target to exist.
        Reuses the title resolver — None title means the target is gone."""
        try:
            return self._tab_title_and_icon(tab)[0] is not None
        except Exception:
            return False

    def _on_reload_all(self, e=None):
        """Reload everything vault-derived (projects, areas, inbox, resources,
        people, threads) from disk without restarting or switching vaults.
        Open tabs reference targets by id, so they survive the rebuild; tabs
        whose target vanished are pruned."""
        cfg = self.state.config
        old_tabs = list(self.state.open_tabs)
        old_active = self.state.active_tab
        old_areas_filter = self.state.areas_filter
        new_state = load_state_from_vault(VAULT_PATH)
        new_state.config = cfg
        new_state.areas_filter = old_areas_filter  # keep the sidebar filter field in sync
        self.state = new_state
        self._load_persisted_threads()
        survivors = [t for t in old_tabs if self._tab_alive(t)]
        if survivors:
            self.state.open_tabs = survivors
            self.state.active_tab = (
                old_active if old_active in survivors else survivors[-1]
            )
        # else: keep the fresh state's default Home tab.
        self.refresh()
        self._toast("reloaded from vault")

    def _build_home_sidebar_row(self) -> ft.Container:
        is_active = bool(self.state.active_tab and self.state.active_tab.kind == "home")
        return ft.Container(
            padding=ft.Padding(left=8, top=8, right=8, bottom=8),
            border_radius=6,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH if is_active else None,
            content=ft.Row(
                controls=[
                    ft.Icon(icon=ft.Icons.HOME_ROUNDED, size=23,
                            color=ft.Colors.BLUE_600),
                    ft.Text("Home", size=14, weight=ft.FontWeight.BOLD, expand=True),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            on_click=lambda e: self._open_home(),
        )

    def _build_inbox_sidebar_row(self) -> ft.Container:
        count = len(self.state.inbox_items)
        is_active = bool(self.state.active_tab and self.state.active_tab.kind == "inbox")
        return ft.Container(
            padding=ft.Padding(left=8, top=8, right=8, bottom=8),
            border_radius=6,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH if is_active else None,
            content=ft.Row(
                controls=[
                    ft.Icon(icon=ft.Icons.INBOX_ROUNDED, size=23,
                            color=ft.Colors.TEAL_600),
                    ft.Text("Inbox", size=14, weight=ft.FontWeight.BOLD, expand=True),
                    ft.Container(
                        padding=ft.Padding(left=6, top=1, right=6, bottom=1),
                        border_radius=10,
                        bgcolor=(ft.Colors.PRIMARY_CONTAINER if count
                                 else ft.Colors.SURFACE_CONTAINER_HIGHEST),
                        content=ft.Text(str(count), size=10,
                                        weight=ft.FontWeight.BOLD,
                                        color=(ft.Colors.ON_PRIMARY_CONTAINER if count
                                               else ft.Colors.OUTLINE)),
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            on_click=lambda e: self._open_inbox(),
        )

    def _build_reviews_sidebar_row(self) -> ft.Container:
        # Badge = "due now" (needs attention + past due + today) — the actionable count.
        buckets = self._compute_review_buckets()
        due_now = len(buckets["needs"]) + len(buckets["past"]) + len(buckets["today"])
        is_active = bool(self.state.active_tab and self.state.active_tab.kind == "reviews")
        return ft.Container(
            padding=ft.Padding(left=8, top=8, right=8, bottom=8),
            border_radius=6,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH if is_active else None,
            content=ft.Row(
                controls=[
                    ft.Icon(icon=ft.Icons.RATE_REVIEW_ROUNDED, size=23,
                            color=ft.Colors.DEEP_ORANGE_400),
                    ft.Text("Reviews", size=14, weight=ft.FontWeight.BOLD, expand=True),
                    ft.Container(
                        padding=ft.Padding(left=6, top=1, right=6, bottom=1),
                        border_radius=10,
                        bgcolor=(ft.Colors.ERROR_CONTAINER if due_now
                                 else ft.Colors.SURFACE_CONTAINER_HIGHEST),
                        content=ft.Text(str(due_now), size=10,
                                        weight=ft.FontWeight.BOLD,
                                        color=(ft.Colors.ON_ERROR_CONTAINER if due_now
                                               else ft.Colors.OUTLINE)),
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            on_click=lambda e: self._open_reviews(),
        )

    def _build_browser_sidebar_row(
        self, *, kind: str, label: str, icon, count: int, on_click,
        icon_color=None,
    ) -> ft.Container:
        """Shared pinned-row builder for vault browser surfaces (Resources, People).
        Mirrors the Inbox row visual but parameterized for any sidebar kind."""
        is_active = bool(self.state.active_tab and self.state.active_tab.kind == kind)
        return ft.Container(
            padding=ft.Padding(left=8, top=8, right=8, bottom=8),
            border_radius=6,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH if is_active else None,
            content=ft.Row(
                controls=[
                    ft.Icon(icon=icon, size=23,
                            color=icon_color or ft.Colors.OUTLINE),
                    ft.Text(label, size=14, weight=ft.FontWeight.BOLD, expand=True),
                    ft.Container(
                        padding=ft.Padding(left=6, top=1, right=6, bottom=1),
                        border_radius=10,
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                        content=ft.Text(str(count), size=10,
                                        weight=ft.FontWeight.BOLD,
                                        color=ft.Colors.OUTLINE),
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            on_click=on_click,
        )

    def _build_resources_sidebar_row(self) -> ft.Container:
        return self._build_browser_sidebar_row(
            kind="resources", label="Resources",
            icon=ft.Icons.LIBRARY_BOOKS_ROUNDED,
            icon_color=ft.Colors.GREEN_600,
            count=len(self.state.resources),
            on_click=lambda e: self._open_resources(),
        )

    def _build_main_area(self) -> ft.Container:
        # Folder-tab metaphor:
        #   Tab strip sits on the main_bg ("desk").
        #   Active tab matches the view body bg (view_bg_color) — pulled forward.
        #   Inactive tabs use tab_inactive_bg_color — sunken behind.
        #   No divider between tab strip and view: active tab merges into the body.
        self.tabs_row = ft.Row(spacing=2, scroll=ft.ScrollMode.AUTO)
        self.view_slot = ft.Container(
            expand=True, bgcolor=self.state.config.view_bg_color,
        )
        return ft.Container(
            expand=True,
            content=ft.Column(
                controls=[
                    ft.Container(
                        padding=ft.Padding(left=12, top=8, right=12, bottom=0),
                        content=self.tabs_row,
                    ),
                    self.view_slot,
                ],
                spacing=0,
                expand=True,
            ),
        )

    def _build_chat_view_body(self) -> ft.Control:
        self.team_chip = ft.Text("", size=13, color=ft.Colors.OUTLINE)
        # Clickable thread name → rename dialog (rename from inside the thread).
        self.thread_title = ft.Container(
            padding=ft.Padding(left=4, top=2, right=4, bottom=2),
            border_radius=6, ink=True,
            tooltip="Rename this thread",
            # Flex child of the header's left zone: a long title wraps downward
            # (see _refresh_thread_title) instead of shoving the busy ring and
            # action buttons off the right edge.
            expand=True,
        )
        # Clickable: shows the thread's project (→ its overview) or "scratch".
        self.project_chip = ft.Container(
            padding=ft.Padding(left=8, top=3, right=8, bottom=3),
            border_radius=12, ink=True,
        )
        self.minimap_toggle_btn = ft.IconButton(
            icon=ft.Icons.ACCOUNT_TREE_OUTLINED,
            tooltip="Toggle tree mini-map",
            on_click=self._on_toggle_minimap,
        )
        self.debug_prompt_btn = ft.IconButton(
            icon=ft.Icons.BUG_REPORT_OUTLINED,
            tooltip="Inspect the exact prompt last sent (system prompt + injected context + history)",
            on_click=self._show_prompt_dialog,
        )
        self.prompt_edit_btn = ft.IconButton(
            icon=ft.Icons.TUNE,
            tooltip="View / edit this thread's system prompt (override)",
            on_click=self._open_prompt_editor,
        )
        # Smart-follow: auto_scroll keeps the stream pinned to the bottom, but we
        # toggle it off the moment the user scrolls up to read (e.g. the top of a
        # long streaming reply) and back on when they return to the bottom. This
        # gates Flet's working ListView auto-scroll on user intent rather than
        # using scroll_to (which the docs flag as unreliable on dynamic ListViews).
        self.thread_view = ft.ListView(
            spacing=10,
            padding=ft.Padding(left=20, top=12, right=20, bottom=12),
            auto_scroll=True,
            on_scroll=self._on_thread_scroll,
            # Render all turns (don't lazy-build) so scroll_to(scroll_key=turn.id)
            # — used by the ▲/▼ prev/next-message nav buttons — lands reliably.
            # Chat threads are small enough that eager build is fine.
            build_controls_on_demand=False,
            expand=True,
        )
        # Vertical scroller for the rows; it sizes to its content's width so the
        # horizontal scroller below can scroll right when a branchy tree is wide.
        self.minimap_content = ft.Column(spacing=2, scroll=ft.ScrollMode.AUTO)
        self.minimap_container = ft.Container(
            width=260,
            padding=ft.Padding(left=12, top=12, right=12, bottom=12),
            border=ft.Border(left=ft.BorderSide(width=1, color=ft.Colors.OUTLINE_VARIANT)),
            # Horizontal scroll: deep / wide conversation trees extend past 260px.
            content=ft.Row(
                controls=[self.minimap_content],
                scroll=ft.ScrollMode.AUTO,
                expand=True,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            visible=False,
        )
        self.temp_btn = ft.TextButton(
            tooltip="Sampling temperature for the next message — click to adjust",
            on_click=self._open_temp_popover,
        )
        self._sync_temp_btn()
        self.atref_panel = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO)
        self.atref_panel_container = ft.Container(
            visible=False,
            height=190,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
            border=ft.Border(top=ft.BorderSide(width=1, color=ft.Colors.OUTLINE_VARIANT)),
            padding=ft.Padding(left=12, top=6, right=12, bottom=6),
            content=self.atref_panel,
        )
        self.input_field = ft.TextField(
            hint_text="Type to the team...   (@ to reference a file/folder)",
            expand=True,
            multiline=True,
            min_lines=1,
            max_lines=5,
            shift_enter=True,
            on_change=self._on_input_change,
            on_submit=self._on_send,
        )
        # Live "a reply is streaming" indicator (header spinner) + counter.
        self._chat_inflight = 0
        # Per-thread "always allow this tool" set for the tool-confirm gate (see
        # _ask_tool_permission). In-memory only — keyed by thread.id, reset on
        # restart so a fresh session re-asks. {thread_id: {tool_name, …}}.
        self._tool_allow: dict[str, set] = {}
        # Chat-view repaint coalescing. These flags are touched from BOTH the
        # streaming worker threads and the event loop, so every access is guarded
        # by this lock. `_chat_dirty` = there is unrendered chat state; while a
        # refresh task is running (`_chat_refresh_scheduled`) new requests just
        # mark dirty and the running task drains them in a loop — so the final
        # chunk is never dropped and the latch can't get stuck (the running task
        # always clears `_chat_refresh_scheduled` when it finds nothing dirty).
        # `_chat_refresh_task` retains the run_task future so asyncio can't GC the
        # coroutine before it runs (the footgun that used to freeze the view until
        # a tab switch forced a full refresh).
        self._chat_update_lock = threading.Lock()
        self._chat_dirty = False
        self._chat_refresh_scheduled = False
        self._chat_refresh_task = None
        self.chat_busy_ring = ft.ProgressRing(
            width=16, height=16, stroke_width=2, visible=False,
            tooltip="Working… waiting for the response")
        # Markdown-rendering toggle (assistant replies). Persisted in config.
        self.md_toggle_btn = ft.IconButton(
            icon=ft.Icons.SUBJECT,
            selected_icon=ft.Icons.ARTICLE_OUTLINED,
            selected=self.state.config.render_markdown,
            tooltip="Markdown rendering of replies (on/off)",
            on_click=self._on_toggle_markdown,
        )
        # Prev/next message navigation — jump-scroll through the thread.
        self._nav_index = 0
        self.nav_up_btn = ft.IconButton(
            icon=ft.Icons.KEYBOARD_ARROW_UP, icon_size=18,
            tooltip="Scroll to previous message",
            on_click=lambda e: self._on_nav_msg(-1))
        self.nav_down_btn = ft.IconButton(
            icon=ft.Icons.KEYBOARD_ARROW_DOWN, icon_size=18,
            tooltip="Scroll to next message",
            on_click=lambda e: self._on_nav_msg(1))

        return ft.Column(
            controls=[
                ft.Container(
                    padding=ft.Padding(left=20, top=8, right=12, bottom=8),
                    # Two zones: a flexible identity zone (left) that absorbs all
                    # the width pressure by wrapping a long title, and a fixed
                    # action zone (right) that is laid out at its natural size and
                    # so is never clipped — the busy ring + buttons always show.
                    content=ft.Row(
                        controls=[
                            ft.Row(
                                controls=[
                                    ft.Icon(icon=ft.Icons.AUTO_AWESOME, size=14,
                                            color=ft.Colors.TERTIARY),
                                    self.thread_title,
                                    self.project_chip,
                                    self.team_chip,
                                ],
                                spacing=6,
                                expand=True,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            ft.Row(
                                controls=[
                                    self.chat_busy_ring,
                                    self.nav_up_btn,
                                    self.nav_down_btn,
                                    self.md_toggle_btn,
                                    self.prompt_edit_btn,
                                    self.debug_prompt_btn,
                                    self.minimap_toggle_btn,
                                ],
                                spacing=6, tight=True,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=6,
                    ),
                ),
                ft.Divider(height=1),
                ft.Row(
                    controls=[self.thread_view, self.minimap_container],
                    expand=True,
                    spacing=0,
                ),
                self.atref_panel_container,
                ft.Divider(height=1),
                ft.Container(
                    padding=ft.Padding(left=12, top=8, right=12, bottom=12),
                    content=ft.Row(
                        controls=[
                            self.input_field,
                            self.temp_btn,
                            ft.IconButton(icon=ft.Icons.SEND, on_click=self._on_send),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.END,
                    ),
                ),
            ],
            spacing=0,
            expand=True,
        )

    def _build_overview_view_body(self) -> ft.Control:
        self.overview_content = ft.Column(spacing=16, scroll=ft.ScrollMode.AUTO, expand=True)
        return ft.Container(
            padding=ft.Padding(left=32, top=24, right=32, bottom=24),
            content=self.overview_content,
            expand=True,
        )

    def _build_agent_view_body(self) -> ft.Control:
        self.agent_content = ft.Column(spacing=16, scroll=ft.ScrollMode.AUTO, expand=True)
        return ft.Container(
            padding=ft.Padding(left=32, top=24, right=32, bottom=24),
            content=self.agent_content,
            expand=True,
        )

    def _build_tasks_view_body(self) -> ft.Control:
        self.tasks_content = ft.Column(spacing=16, scroll=ft.ScrollMode.AUTO, expand=True)
        return ft.Container(
            padding=ft.Padding(left=32, top=24, right=32, bottom=24),
            content=self.tasks_content,
            expand=True,
        )

    def _build_area_view_body(self) -> ft.Control:
        self.area_content = ft.Column(spacing=16, scroll=ft.ScrollMode.AUTO, expand=True)
        return ft.Container(
            padding=ft.Padding(left=32, top=24, right=32, bottom=24),
            content=self.area_content,
            expand=True,
        )

    def _build_home_view_body(self) -> ft.Control:
        self.home_content = ft.Column(spacing=20, scroll=ft.ScrollMode.AUTO, expand=True)
        return ft.Container(
            padding=ft.Padding(left=40, top=32, right=40, bottom=32),
            content=self.home_content,
            expand=True,
        )

    def _build_inbox_view_body(self) -> ft.Control:
        # Mail-style two-pane: scrollable item list on the left, preview pane on the right.
        # Selection lives in state.selected_inbox_path so refresh can re-render both panes.
        self.inbox_list_col = ft.Column(
            spacing=2, scroll=ft.ScrollMode.AUTO, expand=True,
        )
        self.inbox_preview_col = ft.Column(
            spacing=12, scroll=ft.ScrollMode.AUTO, expand=True,
        )
        return ft.Row(
            controls=[
                ft.Container(
                    width=340,
                    padding=ft.Padding(left=16, top=20, right=12, bottom=16),
                    content=self.inbox_list_col,
                ),
                ft.VerticalDivider(width=1),
                ft.Container(
                    padding=ft.Padding(left=24, top=20, right=32, bottom=24),
                    content=self.inbox_preview_col,
                    expand=True,
                ),
            ],
            spacing=0,
            expand=True,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    def _build_browser_view_body(
        self, list_col: ft.Column, preview_col: ft.Column,
        header_controls: list[ft.Control] | None = None,
    ) -> ft.Control:
        """Shared mail-style two-pane layout for vault browser surfaces.
        Selection lives in state so refresh can re-render both panes.
        header_controls (e.g. a filter field) pin above the list."""
        left_children: list[ft.Control] = []
        if header_controls:
            left_children.extend(header_controls)
        left_children.append(list_col)
        return ft.Row(
            controls=[
                ft.Container(
                    width=340,
                    padding=ft.Padding(left=16, top=20, right=12, bottom=16),
                    content=ft.Column(controls=left_children, spacing=8, expand=True),
                ),
                ft.VerticalDivider(width=1),
                ft.Container(
                    padding=ft.Padding(left=24, top=20, right=32, bottom=24),
                    content=preview_col,
                    expand=True,
                ),
            ],
            spacing=0,
            expand=True,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    def _build_resources_view_body(self) -> ft.Control:
        self.resources_list_col = ft.Column(
            spacing=2, scroll=ft.ScrollMode.AUTO, expand=True,
        )
        self.resources_preview_col = ft.Column(
            spacing=12, scroll=ft.ScrollMode.AUTO, expand=True,
        )
        return self._build_browser_view_body(
            self.resources_list_col, self.resources_preview_col,
        )

    def _build_settings_view_body(self) -> ft.Control:
        cfg = self.state.config
        self.settings_apikey_field = ft.TextField(
            label="OpenRouter API key",
            value=cfg.openrouter_api_key,
            password=True,
            can_reveal_password=True,
            hint_text="sk-or-...",
        )
        self.settings_mock_switch = ft.Switch(
            label="Force mock mode (all agent calls return canned responses; no network)",
            value=cfg.force_mock,
            # Apply + persist immediately — the switch was previously inert until
            # the Save button was clicked, which made it look like live mode was
            # on when config (and dispatch) still had force_mock=True.
            on_change=self._on_toggle_force_mock,
        )
        # Free-text field is the source of truth (any routing slug, incl.
        # ollama/<model>). The dropdown below is just a quick-pick that writes
        # into it — Flet's editable Dropdown only commits .value on an option
        # *selection*, so free-typed slugs were silently dropped (fell back to
        # the seeded default on save). A plain TextField captures them reliably.
        common_models = [
            "anthropic/claude-opus-4-8",
            "anthropic/claude-opus-4-7",
            "anthropic/claude-sonnet-4-6",
            "anthropic/claude-haiku-4-5",
            "openai/gpt-4o",
            "google/gemini-2.0-flash-001",
            "ollama/qwen2.5:3b",
        ]
        self.settings_model_field = ft.TextField(
            label="Chat model (routing slug)",
            value=cfg.chat_model or "anthropic/claude-opus-4-7",
            helper="Free text. The prefix picks the backend — see MODEL ROUTING below.",
        )

        def _pick_model(e):
            if e.control.value:
                self.settings_model_field.value = e.control.value
                self.settings_model_field.update()

        self.settings_model_dropdown = ft.Dropdown(
            label="Quick-pick a known model",
            options=[ft.dropdown.Option(m) for m in common_models],
            on_select=_pick_model,
        )
        self.settings_debug_switch = ft.Switch(
            label="Debug: print each prompt sent to the model to stderr",
            value=cfg.debug_prompts,
        )
        self.settings_tools_switch = ft.Switch(
            label="Enable tools (read/write/list/move vault files, run shell) — trust mode",
            value=cfg.tools_enabled,
        )
        self.settings_tool_confirm_switch = ft.Switch(
            label="Ask before changes (Allow/Deny dialog on write / move / shell / delegate)",
            value=getattr(cfg, "tool_confirm", False),
        )
        self.settings_delegate_switch = ft.Switch(
            label="Enable headless delegation tool (chat agent can hand coding tasks to the CLI)",
            value=cfg.delegate_enabled,
        )
        self.settings_delegate_cmd_field = ft.TextField(
            label="Delegate CLI command (base of `<cli> -p …`)",
            value=cfg.delegate_command,
        )
        self.settings_delegate_permmode_field = ft.TextField(
            label="Headless permission mode (bypassPermissions / acceptEdits / default)",
            value=cfg.delegate_permission_mode,
        )
        self.settings_delegate_allowed_field = ft.TextField(
            label="Headless allowed tools (space/comma list; overrides permission mode if set)",
            value=cfg.delegate_allowed_tools,
        )
        self.settings_delegate_timeout_field = ft.TextField(
            label="Delegate timeout (seconds)",
            value=str(cfg.delegate_timeout),
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        self.settings_delegate_term_field = ft.TextField(
            label="Interactive terminal template ({dir} = working_dir, {cmd} = CLI)",
            value=cfg.delegate_terminal_command,
        )
        self.settings_cli_session_field = ft.TextField(
            label="CLI session command ('Open CLI session' button)",
            value=cfg.cli_session_command,
        )
        # --- Publishing (WordPress.com) ---
        self.settings_wp_site_field = ft.TextField(
            label="WordPress.com site", value=getattr(cfg, "wpcom_site", ""),
            hint_text="myweb1712.wordpress.com")
        self.settings_wp_clientid_field = ft.TextField(
            label="Client ID", value=getattr(cfg, "wpcom_client_id", ""))
        self.settings_wp_secret_field = ft.TextField(
            label="Client Secret", value=getattr(cfg, "wpcom_client_secret", ""),
            password=True, can_reveal_password=True)
        self.settings_wp_user_field = ft.TextField(
            label="WordPress.com username", value=getattr(cfg, "wpcom_username", ""))
        self.settings_wp_pass_field = ft.TextField(
            label="Password / Application Password",
            value=getattr(cfg, "wpcom_password", ""),
            password=True, can_reveal_password=True)
        self.settings_wp_status_dd = ft.Dropdown(
            label="Default status",
            value=getattr(cfg, "publish_default_status", "draft") or "draft",
            options=[ft.dropdown.Option("draft"), ft.dropdown.Option("publish")])
        self.settings_wp_category_dd = ft.Dropdown(
            label="Auto category from",
            value=getattr(cfg, "publish_auto_category", "area") or "area",
            options=[ft.dropdown.Option("area"), ft.dropdown.Option("project"),
                     ft.dropdown.Option("none")])
        self.settings_wp_projtag_switch = ft.Switch(
            label="Add the project name as a tag",
            value=bool(getattr(cfg, "publish_add_project_tag", True)))
        self.settings_wp_notetags_switch = ft.Switch(
            label="Include the note's own tags",
            value=bool(getattr(cfg, "publish_include_note_tags", True)))
        self.settings_wp_tagexclude_field = ft.TextField(
            label="Never-public tags (comma list)",
            value=getattr(cfg, "publish_tag_exclude", ""))
        self.settings_wp_test_status = ft.Text("", size=11, selectable=True)
        self.settings_wp_agent_switch = ft.Switch(
            label="Let the chat agent publish (publish_note tool)",
            value=bool(getattr(cfg, "publish_enabled", False)))
        self.settings_temp_field = ft.TextField(
            label="Default temperature (0.0–2.0)",
            value=f"{cfg.temperature:.1f}",
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        self.settings_maxtokens_field = ft.TextField(
            label="Max response tokens (0 = no cap)",
            value=str(cfg.max_tokens),
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        self.settings_ctxcap_field = ft.TextField(
            label="Vault context token cap",
            value=str(cfg.context_token_cap),
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        self.settings_preamble_field = ft.TextField(
            label="Global system preamble (prepended to every chat)",
            value=cfg.system_preamble,
            multiline=True, min_lines=2, max_lines=6,
        )
        self.settings_ollama_field = ft.TextField(
            label="Ollama base URL",
            value=cfg.ollama_base_url,
        )
        self.settings_editor_field = ft.TextField(
            label="Editor command",
            value=cfg.editor_command,
            hint_text="zed {path}    (or cursor / code / nvim / etc.)",
        )
        self.settings_terminal_field = ft.TextField(
            label="Terminal command",
            value=cfg.terminal_command,
            hint_text="gnome-terminal --working-directory={path}",
        )
        self.settings_git_ui_field = ft.TextField(
            label="Git UI command",
            value=cfg.git_ui_command,
            hint_text="lazygit    (or gitui / tig)",
        )
        self.settings_vault_field = ft.TextField(
            label="Vault path",
            value=cfg.vault_path,
            hint_text=f"blank = bundled vault ({_DEFAULT_VAULT_PATH})",
        )
        self.settings_main_bg_field = ft.TextField(
            label="Main background",
            value=cfg.main_bg_color,
            hint_text="#1A1714",
        )
        self.settings_view_bg_field = ft.TextField(
            label="View / active tab background",
            value=cfg.view_bg_color,
            hint_text="#E8D5A0 (manila)",
        )
        self.settings_tab_inactive_bg_field = ft.TextField(
            label="Inactive tab background",
            value=cfg.tab_inactive_bg_color,
            hint_text="#1F1B16",
        )
        # Telegram capture: token lives in telegram.json (not Config), so seed
        # the field straight from there.
        _tgcfg = load_telegram_cfg()
        self.settings_tg_token_field = ft.TextField(
            label="Telegram bot token (from @BotFather)",
            value=_tgcfg.get("bot_token", ""),
            password=True,
            can_reveal_password=True,
            hint_text="123456789:AA…",
        )
        # --- Title style (big main heading) ---
        # Preset picker fills the fields below; editing any field flips the active
        # theme label to "Custom". A live preview renders the current values.
        self._title_themes = load_title_themes()
        self.settings_title_preset_dd = ft.Dropdown(
            label="Title preset",
            value=(cfg.title_theme if any(t["name"] == cfg.title_theme
                                          for t in self._title_themes) else None),
            options=[ft.dropdown.Option(t["name"]) for t in self._title_themes],
            on_select=self._on_pick_title_preset,
        )
        self.settings_title_font_dd = ft.Dropdown(
            label="Font",
            value=cfg.title_font if cfg.title_font in TITLE_FONTS else "Newsreader",
            options=[ft.dropdown.Option(f) for f in TITLE_FONTS],
            on_select=self._on_title_field_change,
        )
        self.settings_title_size_field = ft.TextField(
            label="Size", value=str(cfg.title_size), width=110,
            keyboard_type=ft.KeyboardType.NUMBER,
            on_change=self._on_title_field_change,
        )
        self.settings_title_color_field = ft.TextField(
            label="Color (blank = default)", value=cfg.title_color, width=220,
            hint_text="#3366CC", on_change=self._on_title_field_change,
        )
        self.settings_title_spacing_field = ft.TextField(
            label="Letter spacing", value=f"{cfg.title_letter_spacing:g}", width=130,
            keyboard_type=ft.KeyboardType.NUMBER,
            on_change=self._on_title_field_change,
        )
        self.settings_title_bold_switch = ft.Switch(
            label="Bold", value=cfg.title_bold, on_change=self._on_title_field_change,
        )
        self.settings_title_italic_switch = ft.Switch(
            label="Italic", value=cfg.title_italic, on_change=self._on_title_field_change,
        )
        self.settings_title_preview = ft.Text("")  # styled in _refresh_title_preview
        self._refresh_title_preview()

        self.settings_save_status = ft.Text("", size=12, color=ft.Colors.OUTLINE)

        self.settings_content = ft.Column(spacing=16, scroll=ft.ScrollMode.AUTO, expand=True)
        return ft.Container(
            padding=ft.Padding(left=32, top=24, right=32, bottom=24),
            content=self.settings_content,
            expand=True,
        )

    # --- Big title (project / area / settings headings) ---
    def _title_text(self, text: str, **kw) -> ft.Text:
        """Render a big main heading from the user's title style (Settings →
        TITLE STYLE). Reads the six title_* config fields so every view's title
        stays in sync. Extra kwargs (e.g. expand) pass through to ft.Text."""
        cfg = self.state.config
        ls = cfg.title_letter_spacing or 0.0
        return ft.Text(
            text,
            size=cfg.title_size or 30,
            font_family=cfg.title_font or "Newsreader",
            weight=ft.FontWeight.BOLD if cfg.title_bold else ft.FontWeight.NORMAL,
            italic=bool(cfg.title_italic),
            color=(cfg.title_color.strip() or None),
            style=ft.TextStyle(letter_spacing=ls) if ls else None,
            **kw,
        )

    # --- Project card ---
    def _build_project_card(self, p: Project) -> ft.Control:
        is_active = self.state.active_project_id == p.id

        status_color = STATUS_COLORS.get(p.status, ft.Colors.OUTLINE)
        header = ft.Row(
            controls=[
                ft.Container(width=8, height=8, border_radius=4, bgcolor=status_color),
                ft.Text(p.name, size=14, weight=ft.FontWeight.BOLD, expand=True),
                ft.Icon(
                    icon=ft.Icons.EXPAND_MORE if is_active else ft.Icons.CHEVRON_RIGHT,
                    size=18, color=ft.Colors.OUTLINE,
                ),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        stats = ft.Text(
            f"{p.status}  ·  {len(p.threads)} threads  ·  ~{p.context_tokens // 1000}k ctx",
            size=11, color=ft.Colors.OUTLINE,
        )

        header_clickable = ft.Container(
            padding=ft.Padding(left=2, top=2, right=2, bottom=2),
            content=ft.Column(controls=[header, stats], spacing=4),
            on_click=lambda e, pid=p.id: self._activate_project(pid),
        )

        card_children: list[ft.Control] = [header_clickable]

        if is_active:
            card_children.append(ft.Container(height=4))
            card_children.append(
                ft.Text("THREADS", size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE)
            )
            if not p.threads:
                card_children.append(
                    ft.Text("— none yet —", size=11, italic=True, color=ft.Colors.OUTLINE)
                )
            for t in p.threads:
                is_open = self.state.is_thread_open(t.id)
                is_thread_active = t.id == self.state.active_thread_id
                marker = "★" if is_thread_active else ("○" if is_open else "·")
                color = (ft.Colors.PRIMARY if is_thread_active
                         else ft.Colors.ON_SURFACE if is_open
                         else ft.Colors.OUTLINE)
                card_children.append(
                    ft.Container(
                        padding=ft.Padding(left=6, top=4, right=6, bottom=4),
                        border_radius=4,
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST if is_thread_active else None,
                        content=ft.Text(f"{marker}  {t.name}", size=13, color=color),
                        on_click=lambda e, tid=t.id: self._open_thread(tid),
                    )
                )
            card_children.append(
                ft.TextButton(
                    "+ new thread", icon=ft.Icons.ADD,
                    on_click=lambda e, pid=p.id: self._on_new_thread(pid),
                )
            )
            card_children.append(
                ft.Text("TASKS  (P2)", size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE)
            )
            card_children.append(
                ft.Text("— deferred —", size=11, italic=True, color=ft.Colors.OUTLINE)
            )
            card_children.append(
                ft.Text("CONTEXT", size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE)
            )
            card_children.append(
                ft.Text(
                    f"{len(p.context_files)} notes · ~{p.context_tokens // 1000}k tokens",
                    size=11, color=ft.Colors.OUTLINE,
                )
            )
            for f in p.context_files[:8]:  # cap visible in card
                card_children.append(
                    ft.Text(f"· {f}", size=11, color=ft.Colors.ON_SURFACE_VARIANT)
                )
            if len(p.context_files) > 8:
                card_children.append(
                    ft.Text(f"  + {len(p.context_files) - 8} more", size=11,
                            italic=True, color=ft.Colors.OUTLINE)
                )

        return ft.Container(
            padding=12,
            border_radius=8,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH if is_active else None,
            border=_all_border(),
            content=ft.Column(controls=card_children, spacing=4),
        )

    def _build_agent_card(self, a: Agent) -> ft.Control:
        return ft.Container(
            padding=12,
            border_radius=8,
            border=_all_border(),
            content=ft.Row(
                controls=[
                    ft.Container(
                        width=40, height=40, border_radius=20,
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Icon(
                            icon=a.icon or ft.Icons.SMART_TOY,
                            size=22, color=ft.Colors.TERTIARY,
                        ),
                    ),
                    ft.Column(
                        spacing=2,
                        controls=[
                            ft.Text(a.name, size=14, weight=ft.FontWeight.BOLD),
                            ft.Text(a.role, size=11, color=ft.Colors.OUTLINE),
                        ],
                        expand=True,
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
            on_click=lambda e, name=a.name: self._open_agent(name),
        )

    # --- Refresh ---
    def refresh(self):
        self._refresh_sidebar()
        self._refresh_tabs()
        self._refresh_view_slot()
        self.page.update()

    def _refresh_sidebar(self):
        # AREAS header (filter-chip label reflects the current toggle state)
        self.sidebar_areas_header.content = self._build_areas_header()
        # Pinned top rows: rebuild each refresh so active highlight + counts stay current
        for target, builder in (
            (self.sidebar_home_row, self._build_home_sidebar_row),
            (self.sidebar_inbox_row, self._build_inbox_sidebar_row),
            (self.sidebar_reviews_row, self._build_reviews_sidebar_row),
            (self.sidebar_resources_row, self._build_resources_sidebar_row),
            (self.sidebar_people_row, self.people_view.build_sidebar_row),
        ):
            rebuilt = builder()
            target.content = rebuilt.content
            target.bgcolor = rebuilt.bgcolor
            target.padding = rebuilt.padding
            target.border_radius = rebuilt.border_radius
            target.on_click = rebuilt.on_click
        self.sidebar_projects_col.controls.clear()
        if not self.state.projects and not self.state.areas:
            self.sidebar_projects_col.controls.append(
                ft.Text("No projects found.\nIs ./vault symlinked?",
                        size=11, italic=True, color=ft.Colors.OUTLINE)
            )
        else:
            groups = self.state.projects_by_area()
            # Title filter: when set, narrow each area's projects by substring,
            # force-expand areas to reveal matches, and drop empty areas.
            flt = self.state.areas_filter.strip().lower()

            def _match(projects):
                return [p for p in projects if flt in p.name.lower()] if flt else projects

            # One section per known area (in alphabetical order)
            for area in sorted(self.state.areas, key=lambda a: a.name.lower()):
                plist = _match(groups.get(area.name, []))
                if flt and not plist:
                    continue  # hide areas with no match while filtering
                self.sidebar_projects_col.controls.append(
                    self._build_area_section(area, plist, force_expand=bool(flt))
                )
            # Uncategorized — projects whose `area:` doesn't match any loaded area,
            # plus projects with no area at all
            known = {a.name for a in self.state.areas}
            uncategorized: list[Project] = []
            for key, projects in groups.items():
                if key == "(uncategorized)" or key not in known:
                    uncategorized.extend(projects)
            uncategorized = _match(uncategorized)
            uncategorized.sort(key=lambda p: p.name.lower())
            if uncategorized:
                self.sidebar_projects_col.controls.append(
                    self._build_uncategorized_section(uncategorized)
                )
            if flt and not self.sidebar_projects_col.controls:
                self.sidebar_projects_col.controls.append(
                    ft.Container(
                        padding=ft.Padding(left=8, top=4, right=8, bottom=4),
                        content=ft.Text(f"No projects match “{flt}”.",
                                        size=11, italic=True, color=ft.Colors.OUTLINE),
                    )
                )

        self.sidebar_agents_col.controls.clear()
        if not self.state.agents:
            self.sidebar_agents_col.controls.append(
                ft.Text("No agents yet.\nAdd .md files to _System/Agents/",
                        size=11, italic=True, color=ft.Colors.OUTLINE)
            )
        for a in self.state.agents:
            self.sidebar_agents_col.controls.append(self._build_agent_card(a))

    def _is_area_active(self, area_name: str) -> bool:
        """True if the area's own tab is active, OR a project from this area is active."""
        tab = self.state.active_tab
        if not tab:
            return False
        if tab.kind == "area" and tab.ref_id == area_name:
            return True
        p = self.state.active_project
        if p and p.area == area_name:
            return True
        return False

    def _build_area_section(self, area: Area, projects: list[Project],
                            force_expand: bool = False) -> ft.Control:
        is_active = self._is_area_active(area.name)
        # force_expand (title filter active) opens the section to show matches.
        expanded = is_active or force_expand
        active_count = sum(1 for p in projects if p.status == "active")
        status_color = STATUS_COLORS.get(area.status, ft.Colors.OUTLINE)

        header = ft.Container(
            padding=ft.Padding(left=8, top=8, right=8, bottom=8),
            border_radius=6,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH if is_active else None,
            content=ft.Row(
                controls=[
                    ft.Container(width=8, height=8, border_radius=4, bgcolor=status_color),
                    ft.Text(area.name, size=14, weight=ft.FontWeight.BOLD, expand=True),
                    ft.Text(f"{active_count}/{len(projects)}", size=11,
                            color=ft.Colors.OUTLINE),
                    ft.Icon(
                        icon=ft.Icons.EXPAND_MORE if expanded else ft.Icons.CHEVRON_RIGHT,
                        size=18, color=ft.Colors.OUTLINE,
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            on_click=lambda e, name=area.name: self._open_area(name),
        )

        children: list[ft.Control] = [header]
        if expanded:
            if not projects:
                children.append(
                    ft.Container(
                        padding=ft.Padding(left=20, top=4, right=8, bottom=4),
                        content=ft.Text("(no projects in this area yet)",
                                        size=11, italic=True, color=ft.Colors.OUTLINE),
                    )
                )
            # Filter: active only by default; the AREAS chip reveals inactive.
            # A title filter (force_expand) shows every match regardless of status.
            shown = projects if (self._areas_show_inactive or force_expand) else \
                [p for p in projects if p.status == "active"]
            sorted_projects = sorted(shown, key=lambda p: p.name.lower())
            if projects and not sorted_projects:
                hidden = len(projects)
                children.append(
                    ft.Container(
                        padding=ft.Padding(left=20, top=4, right=8, bottom=4),
                        content=ft.Text(
                            f"({hidden} inactive — toggle “all” to show)",
                            size=11, italic=True, color=ft.Colors.OUTLINE),
                    )
                )
            for p in sorted_projects:
                children.append(
                    ft.Container(
                        padding=ft.Padding(left=16, top=0, right=0, bottom=0),
                        content=self._build_project_card(p),
                    )
                )

        return ft.Column(controls=children, spacing=4)

    def _build_uncategorized_section(self, projects: list[Project]) -> ft.Control:
        active_count = sum(1 for p in projects if p.status == "active")
        header = ft.Container(
            padding=ft.Padding(left=8, top=8, right=8, bottom=8),
            content=ft.Row(
                controls=[
                    ft.Container(width=8, height=8, border_radius=4,
                                 bgcolor=ft.Colors.OUTLINE_VARIANT),
                    ft.Text("Uncategorized", size=14, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.OUTLINE, italic=True, expand=True),
                    ft.Text(f"{active_count}/{len(projects)}", size=11,
                            color=ft.Colors.OUTLINE),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        # Always show all uncategorized projects — they need triage / area assignment
        children: list[ft.Control] = [header]
        for p in projects:
            children.append(
                ft.Container(
                    padding=ft.Padding(left=16, top=0, right=0, bottom=0),
                    content=self._build_project_card(p),
                )
            )
        return ft.Column(controls=children, spacing=4)

    def _refresh_tabs(self):
        self.tabs_row.controls.clear()
        cfg = self.state.config
        for tab in self.state.open_tabs:
            title, icon = self._tab_title_and_icon(tab)
            if title is None:
                continue
            is_active = self.state.active_tab == tab
            # Platinum 9 folder tabs (see _System/Methods/Workbench UI.md):
            #   active = raised, fill == panel, NO bottom edge → merges into body.
            #   inactive = flush face, bottom bevel-line present → sits behind the lip.
            bg = cfg.view_bg_color if is_active else cfg.tab_inactive_bg_color
            hi = ft.BorderSide(1, PLATINUM["hi_bevel"])
            lo = ft.BorderSide(1, PLATINUM["lo_bevel"])
            if is_active:
                border = ft.Border(top=hi, left=hi, right=lo)  # open bottom
                text_color = PLATINUM["text"]
            else:
                border = ft.Border(top=hi, left=hi, right=lo, bottom=lo)
                text_color = PLATINUM["text2"]
            self.tabs_row.controls.append(
                ft.Container(
                    padding=ft.Padding(left=12, top=8, right=6, bottom=8),
                    # 2px top corners — flat bottom merges the active tab into view body
                    border_radius=ft.BorderRadius(
                        top_left=2, top_right=2, bottom_left=0, bottom_right=0,
                    ),
                    bgcolor=bg,
                    border=border,
                    content=ft.Row(
                        controls=[
                            ft.Icon(icon=icon, size=14, color=text_color),
                            # Chrome bold on the selected tab (Charcoal-flavored)
                            ft.Text(title, size=13, color=text_color,
                                    weight=(ft.FontWeight.BOLD if is_active
                                            else ft.FontWeight.NORMAL)),
                            ft.IconButton(
                                icon=ft.Icons.CLOSE, icon_size=14,
                                icon_color=text_color,
                                on_click=lambda e, t=tab: self._close_tab(t),
                            ),
                        ],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    on_click=lambda e, t=tab: self._set_active_tab(t),
                )
            )

    def _tab_title_and_icon(self, tab: OpenTab):
        if tab.kind == "chat":
            t = self.state.get_thread(tab.ref_id)
            return (t.name if t else None), ft.Icons.CHAT_BUBBLE_OUTLINE
        if tab.kind == "overview":
            p = self.state.get_project(tab.ref_id)
            return (p.name if p else None), ft.Icons.DASHBOARD_OUTLINED
        if tab.kind == "agent":
            a = self.state.get_agent(tab.ref_id)
            return (a.name if a else None), ft.Icons.PERSON_OUTLINE
        if tab.kind == "settings":
            return "Settings", ft.Icons.SETTINGS_OUTLINED
        if tab.kind == "tasks":
            p = self.state.get_project(tab.ref_id)
            return (f"Tasks · {p.name}" if p else None), ft.Icons.CHECKLIST
        if tab.kind == "area":
            a = self.state.get_area(tab.ref_id)
            return (a.name if a else None), ft.Icons.WORKSPACES_OUTLINED
        if tab.kind == "inbox":
            return "Inbox", ft.Icons.INBOX_OUTLINED
        if tab.kind == "resources":
            return "Resources", ft.Icons.LIBRARY_BOOKS_OUTLINED
        if tab.kind == "people":
            return "People", ft.Icons.PEOPLE_OUTLINE
        if tab.kind == "home":
            return "Home", ft.Icons.HOME_OUTLINED
        if tab.kind == "reviews":
            return "Reviews", ft.Icons.RATE_REVIEW_OUTLINED
        return tab.ref_id, ft.Icons.HELP_OUTLINE

    def _refresh_view_slot(self):
        tab = self.state.active_tab
        if not tab:
            self.view_slot.content = ft.Container(
                alignment=ft.Alignment.CENTER,
                padding=40,
                content=ft.Text(
                    "No tabs open. Click a project or an agent in the sidebar.",
                    color=ft.Colors.OUTLINE,
                ),
            )
            return
        if tab.kind == "chat":
            self.view_slot.content = self.chat_view_body
            self._refresh_chat_view()
        elif tab.kind == "overview":
            self.view_slot.content = self.overview_view_body
            self._refresh_overview_view()
        elif tab.kind == "agent":
            self.view_slot.content = self.agent_view_body
            self._refresh_agent_view()
        elif tab.kind == "settings":
            self.view_slot.content = self.settings_view_body
            self._refresh_settings_view()
        elif tab.kind == "tasks":
            self.view_slot.content = self.tasks_view_body
            self._refresh_tasks_view()
        elif tab.kind == "area":
            self.view_slot.content = self.area_view_body
            self._refresh_area_view()
        elif tab.kind == "inbox":
            self.view_slot.content = self.inbox_view_body
            self._refresh_inbox_view()
        elif tab.kind == "resources":
            self.view_slot.content = self.resources_view_body
            self._refresh_resources_view()
        elif tab.kind == "people":
            self.view_slot.content = self.people_view.body
            self.people_view.refresh()
        elif tab.kind == "home":
            self.view_slot.content = self.home_view_body
            self._refresh_home_view()
        elif tab.kind == "reviews":
            self.view_slot.content = self.reviews_view_body
            self._refresh_reviews_view()

    # --- ChatView refresh ---
    def _refresh_chat_view(self):
        self._refresh_thread_title()
        self._refresh_team_chip()
        self._refresh_thread_view()
        self._refresh_minimap()
        # Opening a thread re-enables follow so it lands on the latest message
        # (the children repopulate → auto_scroll snaps to the bottom).
        self.thread_view.auto_scroll = True
        # Park the prev/next-message cursor on the latest message.
        t = self.state.active_thread
        self._nav_index = max(0, len(self._active_path(t)) - 1) if t else 0

    def _refresh_thread_title(self):
        t = self.state.active_thread
        name = t.name if t else "—"
        # Title text is the flex child so a long name soft-wraps to extra lines
        # (no_wrap=False, no max_lines → no ellipsis); the pencil affordance stays
        # pinned beside it. The container's expand=True bounds this Row's width,
        # which is what lets the Text wrap instead of overflowing.
        self.thread_title.content = ft.Row(
            [ft.Text(name, size=13, weight=ft.FontWeight.BOLD,
                     no_wrap=False, expand=True),
             ft.Icon(ft.Icons.EDIT_OUTLINED, size=12, color=ft.Colors.OUTLINE)],
            spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.thread_title.on_click = (
            (lambda e, tid=t.id: self._rename_thread_dialog(tid)) if t else None)

    def _refresh_team_chip(self):
        # v0.2: single-agent chat. The general agent is always Workbench (with fallback).
        # Personas the user can invoke are listed in the prompt; nothing to configure here.
        general = self.state.get_agent("Workbench")
        if general:
            self.team_chip.value = "Workbench  ·  mention any agent to channel it"
        else:
            self.team_chip.value = "Workbench (fallback prompt; add _System/Agents/Workbench.md)"
        self.team_chip.color = ft.Colors.OUTLINE
        self._refresh_project_chip()

    def _refresh_project_chip(self):
        """Show the active thread's project as a clickable link to its overview,
        or a 'scratch' marker for project-less (Home) chats."""
        t = self.state.active_thread
        proj = self.state.project_of_thread(t.id) if t else None
        if proj:
            self.project_chip.content = ft.Row(
                [ft.Icon(ft.Icons.FOLDER_OUTLINED, size=13, color=ft.Colors.TERTIARY),
                 ft.Text(proj.name, size=12, color=ft.Colors.TERTIARY)],
                spacing=4, tight=True,
            )
            self.project_chip.tooltip = f"Open {proj.name} overview"
            self.project_chip.on_click = lambda e, pid=proj.id: self._activate_project(pid)
        else:
            self.project_chip.content = ft.Text(
                "scratch · no project", size=12, italic=True, color=ft.Colors.OUTLINE)
            self.project_chip.tooltip = "Top-level chat — not tied to a project"
            self.project_chip.on_click = None

    def _on_thread_scroll(self, e):
        """Follow the stream only while the user is parked at the bottom. Within a
        small threshold of max extent → keep auto_scroll on; scrolled up to read →
        switch it off so the next chunk doesn't yank them back down. The value is
        read at the next page.update() (every streamed chunk), so no explicit
        control update is needed here."""
        try:
            max_ext = getattr(e, "max_scroll_extent", None)
            px = getattr(e, "pixels", None)
            if max_ext is None or px is None:
                return
            self.thread_view.auto_scroll = (max_ext - px) <= 80
        except Exception:
            pass

    def _refresh_thread_view(self):
        self.thread_view.controls.clear()
        t = self.state.active_thread
        if not t or not t.root_id:
            self.thread_view.controls.append(
                ft.Container(
                    alignment=ft.Alignment.CENTER,
                    padding=40,
                    content=ft.Text("Empty thread. Type below to start.",
                                    color=ft.Colors.OUTLINE),
                )
            )
            return
        for turn in self._active_path(t):
            self.thread_view.controls.append(self._render_turn(turn))

    def _active_path(self, t: Thread) -> list[Turn]:
        if not t.current_leaf_id or t.current_leaf_id not in t.turns:
            return []
        path: list[Turn] = []
        cur: Optional[Turn] = t.turns[t.current_leaf_id]
        while cur:
            path.append(cur)
            cur = t.turns.get(cur.parent_id) if cur.parent_id else None
        return list(reversed(path))

    def _render_turn(self, turn: Turn) -> ft.Control:
        is_user = turn.speaker == "user"
        # Body = the message text with inline tool markers spliced in at the spot
        # the agent paused to use each tool; the full per-tool detail still lives
        # in the compact TOOLS block at the bottom.
        body: list[ft.Control] = self._render_message_flow(turn)
        if getattr(turn, "tool_steps", None):
            body.append(self._render_tool_steps(turn.tool_steps))
        return ft.Container(
            key=ft.ScrollKey(turn.id),  # target for ▲/▼ prev/next-message nav
            padding=12,
            border_radius=8,
            bgcolor=(ft.Colors.SURFACE_CONTAINER_HIGH if is_user
                     else ft.Colors.SURFACE_CONTAINER),
            content=ft.Column(
                spacing=6,
                controls=[
                    ft.Row(
                        controls=[
                            ft.Text(
                                "You" if is_user else "Team",
                                size=12, weight=ft.FontWeight.BOLD,
                                color=ft.Colors.PRIMARY if is_user else ft.Colors.TERTIARY,
                            ),
                            ft.Container(expand=True),
                            ft.IconButton(
                                icon=ft.Icons.PUSH_PIN if turn.pinned else ft.Icons.PUSH_PIN_OUTLINED,
                                icon_size=14, tooltip="Pin",
                                icon_color=ft.Colors.TERTIARY if turn.pinned else None,
                                on_click=lambda e, tid=turn.id: self._toggle_pin(tid),
                            ),
                            ft.IconButton(
                                icon=ft.Icons.ALT_ROUTE, icon_size=14,
                                tooltip="Continue from here",
                                on_click=lambda e, tid=turn.id: self._continue_from(tid),
                            ),
                            ft.IconButton(
                                icon=ft.Icons.REFRESH, icon_size=14,
                                tooltip="Regenerate (branch)",
                                on_click=lambda e, tid=turn.id: self._regenerate(tid),
                            ),
                        ],
                        spacing=0,
                    ),
                    *body,
                ],
            ),
        )

    @staticmethod
    def _format_tool_args(arguments: str) -> str:
        """Compact one-line render of a tool call's JSON arguments."""
        try:
            args = json.loads(arguments or "{}")
        except Exception:
            return (arguments or "").strip()[:80]
        if not isinstance(args, dict):
            return str(args)[:80]
        parts = []
        for k, v in args.items():
            sv = str(v).replace("\n", " ")
            if len(sv) > 50:
                sv = sv[:50] + "…"
            parts.append(f"{k}={sv}")
        return ", ".join(parts)

    def _render_message_flow(self, turn: Turn) -> list[ft.Control]:
        """Render the message text, splicing a small inline tool marker in at the
        point (text_offset) where the agent paused to call each tool — so the
        reader sees "…I need to check this folder [📖 read Project OS] I saw…"
        in the natural flow, not a silent jump. Legacy turns with no offsets
        (pre-this-feature) fall back to a single plain text block."""
        text = turn.text or ""
        steps = getattr(turn, "tool_steps", None) or []
        anchored = [s for s in steps if isinstance(s.get("text_offset"), int)]
        if not anchored:
            return [self._msg_text(text, turn)] if text else []
        controls: list[ft.Control] = []
        cursor = 0
        for raw_off in sorted({s["text_offset"] for s in anchored}):
            off = max(cursor, min(raw_off, len(text)))
            seg = text[cursor:off]
            if seg.strip():
                controls.append(self._msg_text(seg, turn))
            group = [s for s in anchored if s["text_offset"] == raw_off]
            controls.append(ft.Row(controls=[self._tool_chip(s) for s in group],
                                   wrap=True, spacing=6, run_spacing=4))
            cursor = off
        tail = text[cursor:]
        if tail.strip():
            controls.append(self._msg_text(tail, turn))
        return controls

    def _msg_text(self, seg: str, turn: Turn) -> ft.Control:
        """A message text segment — rendered as Markdown when the toggle is on
        (assistant turns only; user turns stay plain so their own `#`/`*` aren't
        reinterpreted)."""
        if turn.speaker != "user" and getattr(self.state.config, "render_markdown", False):
            return ft.Markdown(
                seg, selectable=True,
                extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                code_theme=ft.MarkdownCodeTheme.GITHUB,
                on_tap_link=lambda e: self.page.launch_url(e.data),
            )
        return ft.Text(seg, size=14, selectable=True)

    @staticmethod
    def _tool_glyph(name: str) -> tuple:
        """(icon, verb) for an inline tool marker — a friendly past-tense verb,
        not the raw tool name."""
        return {
            "read_vault_note": (ft.Icons.MENU_BOOK_OUTLINED, "read"),
            "write_vault_note": (ft.Icons.EDIT_NOTE_OUTLINED, "wrote"),
            "move_note": (ft.Icons.DRIVE_FILE_MOVE_OUTLINED, "moved"),
            "list_dir": (ft.Icons.FOLDER_OPEN_OUTLINED, "browsed"),
            "run_shell": (ft.Icons.TERMINAL, "ran"),
            "delegate_to_claude_code": (ft.Icons.SMART_TOY_OUTLINED, "delegated"),
            "check_delegation": (ft.Icons.HOURGLASS_BOTTOM, "checked"),
        }.get(name, (ft.Icons.BUILD_OUTLINED, name))

    @staticmethod
    def _tool_target(name: str, arguments: str) -> str:
        """A short, pretty target for the inline marker — a file's basename or a
        truncated command — never the full args (that's the bottom block's job)."""
        try:
            args = json.loads(arguments or "{}")
        except Exception:
            args = {}
        if not isinstance(args, dict):
            return ""
        if name == "run_shell":
            cmd = str(args.get("command") or args.get("cmd") or "").strip()
            return (cmd[:32] + "…") if len(cmd) > 32 else cmd
        if name == "delegate_to_claude_code":
            task = str(args.get("task") or "").strip()
            return (task[:32] + "…") if len(task) > 32 else task
        for key in ("dest", "dst", "path", "file", "note", "src", "dir", "directory"):
            v = args.get(key)
            if v:
                return str(v).rstrip("/").split("/")[-1] or str(v)
        for v in args.values():
            if isinstance(v, (str, int, float)):
                sv = str(v)
                return (sv[:32] + "…") if len(sv) > 32 else sv
        return ""

    def _tool_chip(self, s: dict) -> ft.Control:
        """One inline marker pill: spinner while running, icon + past-tense verb +
        target once done, error-tinted if the tool returned an error."""
        name = s.get("name", "?")
        icon, verb = self._tool_glyph(name)
        target = self._tool_target(name, s.get("arguments", ""))
        result = (s.get("result") or "").strip()
        pending = result in ("", "…")
        is_err = (not pending) and result.startswith("[") and (
            "error" in result.lower() or "HTTP" in result
            or "no such" in result.lower() or "not found" in result.lower())
        if pending:
            color = ft.Colors.OUTLINE
            leading: ft.Control = ft.ProgressRing(width=11, height=11,
                                                   stroke_width=2,
                                                   color=ft.Colors.TERTIARY)
        else:
            color = ft.Colors.ERROR if is_err else ft.Colors.TERTIARY
            leading = ft.Icon(ft.Icons.ERROR_OUTLINE if is_err else icon,
                              size=13, color=color)
        label = f"{verb} {target}".strip()
        return ft.Container(
            padding=ft.Padding(left=8, top=2, right=9, bottom=2),
            border_radius=11,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
            border=_all_border(ft.Colors.OUTLINE_VARIANT, 1),
            content=ft.Row(
                spacing=5, tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[leading,
                          ft.Text(label, size=11, italic=True, color=color)],
            ),
        )

    def _render_tool_steps(self, steps: list[dict]) -> ft.Control:
        """A compact log of the tools the agent ran this turn, each with its
        result — so a move/edit that happened (or failed) is visible, not hidden
        behind the model's summary."""
        rows: list[ft.Control] = []
        for s in steps:
            name = s.get("name", "?")
            result = (s.get("result") or "").strip()
            pending = result in ("", "…")
            is_err = result.startswith("[") and (
                "error" in result.lower() or "HTTP" in result
                or "no such" in result.lower() or "not found" in result.lower())
            snippet = result.replace("\n", " ")
            if len(snippet) > 160:
                snippet = snippet[:160] + "…"
            res_color = (ft.Colors.OUTLINE if pending
                         else ft.Colors.ERROR if is_err
                         else ft.Colors.ON_SURFACE_VARIANT)
            rows.append(ft.Column(spacing=1, controls=[
                ft.Text(f"› {name}({self._format_tool_args(s.get('arguments', ''))})",
                        size=11, font_family="JetBrains Mono",
                        color=ft.Colors.TERTIARY, selectable=True),
                ft.Text("running…" if pending else snippet, size=11,
                        font_family="JetBrains Mono", color=res_color,
                        selectable=True),
            ]))
        return ft.Container(
            padding=ft.Padding(left=10, top=8, right=10, bottom=8),
            border_radius=6, bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
            content=ft.Column(
                spacing=6,
                controls=[
                    ft.Text(f"TOOLS · {len(steps)}", size=9,
                            weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE),
                    *rows,
                ],
            ),
        )

    def _refresh_minimap(self):
        self.minimap_container.visible = self.state.minimap_open
        if not self.state.minimap_open:
            return
        self.minimap_content.controls.clear()
        t = self.state.active_thread
        if not t or not t.root_id:
            self.minimap_content.controls.append(
                ft.Text("(empty)", size=11, color=ft.Colors.OUTLINE, italic=True)
            )
            return
        pinned = [tn for tn in t.turns.values() if tn.pinned]
        if pinned:
            self.minimap_content.controls.append(
                ft.Text("PINNED", size=10, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.TERTIARY)
            )
            for pn in pinned:
                self.minimap_content.controls.append(self._minimap_pinned_row(pn))
            self.minimap_content.controls.append(ft.Container(height=10))

        self.minimap_content.controls.append(
            ft.Text("TREE", size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE)
        )
        self.minimap_content.controls.append(self._minimap_legend())
        self._render_tree_node(t, t.root_id, depth=0)

    def _minimap_legend(self) -> ft.Control:
        def chip(radius: float, color, text: str) -> ft.Control:
            return ft.Row(
                [
                    ft.Container(width=10, height=10, bgcolor=color, border_radius=radius),
                    ft.Text(text, size=9, color=ft.Colors.OUTLINE),
                ],
                spacing=4,
            )
        return ft.Container(
            padding=ft.Padding(left=2, top=2, right=2, bottom=8),
            content=ft.Row(
                [chip(2, ft.Colors.PRIMARY, "you"),
                 chip(5, ft.Colors.TERTIARY, "agent")],
                spacing=14,
            ),
        )

    def _minimap_pinned_row(self, turn: Turn) -> ft.Control:
        snippet = (turn.text or "…").strip().replace("\n", " ")
        if len(snippet) > 40:
            snippet = snippet[:40] + "…"
        return ft.Container(
            padding=ft.Padding(left=4, top=2, right=4, bottom=2),
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.STAR, size=11, color=ft.Colors.AMBER),
                    ft.Text(snippet, size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                ],
                spacing=4,
            ),
            on_click=lambda e, tid=turn.id: self._jump_to(tid),
            tooltip=turn.text,
            border_radius=6,
            ink=True,
        )

    def _render_tree_node(self, t: Thread, node_id: str, depth: int):
        if node_id not in t.turns:
            return
        turn = t.turns[node_id]
        self.minimap_content.controls.append(self._minimap_node(turn, depth))
        children = [tn for tn in t.turns.values() if tn.parent_id == node_id]
        # Full cascade: every turn steps one level right so the whole tree shape
        # is visible; the minimap's horizontal scroll handles long conversations.
        for c in children:
            self._render_tree_node(t, c.id, depth + 1)

    def _minimap_node(self, turn: Turn, depth: int) -> ft.Control:
        """One node in the tree: a shape, not a label. Circle = agent, rounded
        square = you. Filled + full opacity on the active path, hollow + dimmed
        off it; the current node gets a bright ring + glow; pinned gets a star.
        Hover for the text, click to jump there."""
        t = self.state.active_thread
        if not t:
            return ft.Text("")
        is_current = turn.id == t.current_leaf_id
        on_path = turn.id in {tn.id for tn in self._active_path(t)}
        is_user = turn.speaker == "user"
        base = ft.Colors.PRIMARY if is_user else ft.Colors.TERTIARY
        size = 16 if is_current else 12
        radius = 4 if is_user else size / 2  # square-ish for you, circle for agent

        shape: ft.Control = ft.Container(
            width=size, height=size,
            bgcolor=base if on_path else ft.Colors.TRANSPARENT,
            border=None if on_path else ft.Border.all(1.5, base),
            border_radius=radius,
            shadow=(ft.BoxShadow(blur_radius=10, color=base) if is_current else None),
        )
        if is_current:
            shape = ft.Container(  # bright ring around the active node
                content=shape, padding=2, border_radius=size,
                border=ft.Border.all(1.5, ft.Colors.ON_SURFACE),
            )
        if turn.pinned:
            shape = ft.Stack(
                controls=[
                    shape,
                    ft.Container(
                        content=ft.Icon(ft.Icons.STAR, size=8, color=ft.Colors.AMBER),
                        alignment=ft.Alignment.TOP_RIGHT,
                    ),
                ],
                width=size + 8, height=size + 8,
            )

        snippet = (turn.text or "…").strip().replace("\n", " ")
        if len(snippet) > 220:
            snippet = snippet[:220] + "…"
        who = "You" if is_user else "Workbench"

        row_controls: list[ft.Control] = [ft.Container(width=depth * 14)]
        if depth:
            row_controls.append(ft.Text("└", size=11, color=ft.Colors.OUTLINE_VARIANT))
        row_controls.append(shape)

        return ft.Container(
            padding=ft.Padding(left=4, top=3, right=4, bottom=3),
            content=ft.Row(row_controls, spacing=4,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
            tooltip=f"{who}: {snippet}",
            on_click=lambda e, tid=turn.id: self._jump_to(tid),
            opacity=1.0 if on_path else 0.5,
            border_radius=6,
            ink=True,
        )

    # --- OverviewView refresh ---
    def _refresh_overview_view(self):
        self.overview_content.controls.clear()
        p = self.state.active_project
        if not p:
            self.overview_content.controls.append(
                ft.Text("Select a project from the sidebar.",
                        size=14, color=ft.Colors.OUTLINE)
            )
            return

        # Header: name + status chip
        status_color = STATUS_COLORS.get(p.status, ft.Colors.OUTLINE)
        self.overview_content.controls.append(
            ft.Row(
                controls=[
                    ft.Container(width=12, height=12, border_radius=6, bgcolor=status_color),
                    self._title_text(p.name),
                    ft.Container(
                        padding=ft.Padding(left=8, top=2, right=8, bottom=2),
                        border_radius=10,
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                        content=ft.Text(p.status.upper(), size=10,
                                        weight=ft.FontWeight.BOLD, color=status_color),
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )

        # Header actions — open the project's main note in external tools. The
        # main note is the first context path (main-note-first ordering); folder-
        # main and file-scoped projects both resolve correctly via that helper.
        ctx_paths = _project_context_paths(p)
        if ctx_paths:
            main_note = ctx_paths[0][1]
            self.overview_content.controls.append(
                ft.Row(
                    controls=[
                        ft.FilledTonalButton(
                            "Open in Obsidian", icon=ft.Icons.HUB_OUTLINED,
                            on_click=lambda e, pth=main_note: self._on_open_in_obsidian(pth),
                        ),
                        ft.FilledTonalButton(
                            "Open in editor", icon=ft.Icons.EDIT_NOTE,
                            on_click=lambda e, pth=main_note: self._on_open_in_editor(pth),
                        ),
                        ft.FilledTonalButton(
                            "Reveal", icon=ft.Icons.FOLDER_OPEN,
                            on_click=lambda e, pth=main_note: self._on_reveal_in_files(pth),
                        ),
                        ft.IconButton(
                            icon=ft.Icons.REFRESH, icon_size=18,
                            tooltip="Re-read this project from disk (frontmatter + notes/files)",
                            on_click=lambda e: self._on_overview_refresh(),
                        ),
                    ],
                    spacing=8,
                    wrap=True,
                )
            )

        # Metadata strip — the Kaizen frontmatter at a glance (area · scope ·
        # started · review · tags/modes) + the 2-min micro-commitment.
        self.overview_content.controls.append(self._build_metadata_strip(p))

        # Single-note project → offer to promote it to its own folder so it can
        # hold posts / journals / notes / files (create actions need a folder).
        if not p.vault_folder.endswith("/"):
            self.overview_content.controls.append(
                ft.Container(
                    padding=ft.Padding(left=12, top=8, right=12, bottom=8),
                    border_radius=8, bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                    border=_all_border(),
                    content=ft.Row(
                        controls=[
                            ft.Icon(ft.Icons.DRIVE_FILE_MOVE_OUTLINED,
                                    size=16, color=ft.Colors.OUTLINE),
                            ft.Text("Single-note project — move it to its own folder "
                                    "to add posts / journals / notes / files.",
                                    size=11, color=ft.Colors.OUTLINE, expand=True),
                            ft.FilledTonalButton(
                                "Move to own folder",
                                icon=ft.Icons.CREATE_NEW_FOLDER_OUTLINED,
                                on_click=lambda e, pid=p.id: self._on_promote_to_folder(pid),
                            ),
                        ],
                        spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )

        # Main note body (rendered markdown — the project's actual prose, which
        # the dashboard sections otherwise never show). Read live from disk.
        if ctx_paths:
            self.overview_content.controls.append(
                self._build_note_body_card(ctx_paths[0][1])
            )

        # Hypothesis card
        if p.hypothesis:
            self.overview_content.controls.append(
                ft.Container(
                    padding=16,
                    border_radius=8,
                    bgcolor=ft.Colors.SURFACE_CONTAINER,
                    content=ft.Column(
                        spacing=4,
                        controls=[
                            ft.Text("HYPOTHESIS", size=10,
                                    weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE),
                            ft.Text(p.hypothesis, size=16,
                                    font_family="Newsreader", italic=True),
                        ],
                    ),
                )
            )

        # Scan once for all content types
        pc = scan_project_content(p, VAULT_PATH)

        # Section: Working directory (if declared)
        if p.working_dir:
            self.overview_content.controls.append(
                self._build_working_dir_section_card(p)
            )

        # Section: Threads (persisted as JSON under ~/.workbench/threads/)
        self.overview_content.controls.append(
            self._build_threads_section_card(p)
        )

        # Section: Tasks (from checkbox parsing)
        self.overview_content.controls.append(
            self._build_tasks_section_card(p, pc.tasks)
        )

        # Section: Inbox (notes in <project>/Inbox/)
        self.overview_content.controls.append(
            self._build_notes_section_card(
                label="INBOX", icon=ft.Icons.INBOX_OUTLINED,
                notes=pc.inbox,
                caption="Notes in `<project>/Inbox/` — project-local capture to triage later.",
            )
        )

        # Section: Posts / Blog / Journal (type: post or journal)
        self.overview_content.controls.append(
            self._build_notes_section_card(
                label="POSTS / JOURNAL",
                icon=ft.Icons.EDIT_NOTE,
                notes=pc.posts,
                caption="Notes with `type: post` or `type: journal` in the project folder.",
                project=p,
                create=[("+ new post", "post"), ("+ new journal", "journal")],
                publishable=True,
            )
        )

        # Section: Wiki / Reference (other type:note files)
        self.overview_content.controls.append(
            self._build_notes_section_card(
                label="WIKI / REFERENCE",
                icon=ft.Icons.MENU_BOOK,
                notes=pc.wiki,
                caption="Other `type: note` reference notes in the project folder.",
                project=p,
                create=[("+ new note", "note")],
            )
        )

        # Section: Files (non-markdown)
        self.overview_content.controls.append(
            self._build_files_section_card(pc.files)
        )

    # --- Section cards (overview dashboard) ---
    def _section_card_header(self, label: str, icon, count: int,
                             see_all: Optional[callable] = None) -> ft.Control:
        children: list[ft.Control] = [
            ft.Icon(icon=icon, size=16, color=ft.Colors.OUTLINE),
            ft.Text(label, size=11, weight=ft.FontWeight.BOLD,
                    color=ft.Colors.OUTLINE),
            ft.Container(
                padding=ft.Padding(left=6, top=1, right=6, bottom=1),
                border_radius=8,
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                content=ft.Text(str(count), size=10,
                                weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE),
            ),
            ft.Container(expand=True),
        ]
        if see_all is not None:
            children.append(
                ft.TextButton("see all", icon=ft.Icons.ARROW_FORWARD,
                              on_click=lambda e: see_all())
            )
        return ft.Row(controls=children, spacing=8,
                      vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _section_card(self, header: ft.Control, body_items: list[ft.Control]) -> ft.Control:
        return ft.Container(
            padding=14,
            border_radius=8,
            border=_all_border(),
            content=ft.Column(controls=[header, *body_items], spacing=6),
        )

    def _meta_pill(self, label: str, value: str, *, on_click=None,
                   urgent: bool = False, tooltip: Optional[str] = None) -> ft.Control:
        return ft.Container(
            padding=ft.Padding(left=10, top=5, right=10, bottom=5),
            border_radius=14,
            bgcolor=ft.Colors.ERROR_CONTAINER if urgent else ft.Colors.SURFACE_CONTAINER_LOW,
            border=(_all_border(ft.Colors.ERROR, 1) if urgent else _all_border()),
            ink=on_click is not None, on_click=on_click, tooltip=tooltip,
            content=ft.Row(
                controls=[
                    ft.Text(label.upper(), size=9, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.OUTLINE),
                    ft.Text(value, size=12,
                            color=ft.Colors.ON_ERROR_CONTAINER if urgent else None),
                ],
                spacing=6, tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _build_metadata_strip(self, p: Project) -> ft.Control:
        """Kaizen frontmatter at a glance. The review pill is clickable → Reviews
        (and tints red when overdue); the micro-commitment gets its own line."""
        pills: list[ft.Control] = []
        if p.area:
            pills.append(self._meta_pill("area", p.area))
        if p.scope:
            pills.append(self._meta_pill("scope", p.scope))
        if p.started:
            pills.append(self._meta_pill("started", p.started.isoformat()))
        # Review pill — always shown (even when unset), links to the Reviews board.
        if p.review:
            days = (p.review - date.today()).days
            tail = ("today" if days == 0 else
                    f"{-days}d overdue" if days < 0 else f"in {days}d")
            pills.append(self._meta_pill(
                "review", f"{p.review.isoformat()} · {tail}",
                on_click=lambda e: self._open_reviews(), urgent=days < 0,
                tooltip="Open the Reviews board"))
        else:
            pills.append(self._meta_pill(
                "review", "none set", on_click=lambda e: self._open_reviews(),
                tooltip="Open the Reviews board"))
        # Tags + modes as small # chips.
        for m in p.modes:
            pills.append(self._tag_chip(m))
        for t in p.tags:
            pills.append(self._tag_chip(t))

        children: list[ft.Control] = [
            ft.Row(pills, wrap=True, spacing=8, run_spacing=6),
        ]
        if p.micro_commitment:
            children.append(
                ft.Container(
                    padding=ft.Padding(left=12, top=8, right=12, bottom=8),
                    border_radius=8, bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                    border=_all_border(),
                    content=ft.Row(
                        controls=[
                            ft.Icon(ft.Icons.PLAY_CIRCLE_OUTLINE, size=16,
                                    color=ft.Colors.TERTIARY),
                            ft.Text("2-MIN START", size=9, weight=ft.FontWeight.BOLD,
                                    color=ft.Colors.OUTLINE),
                            ft.Text(p.micro_commitment, size=13, italic=True,
                                    expand=True),
                        ],
                        spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )
        return ft.Column(children, spacing=8)

    def _tag_chip(self, text: str) -> ft.Control:
        return ft.Container(
            padding=ft.Padding(left=8, top=3, right=8, bottom=3),
            border_radius=10, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
            content=ft.Text(f"#{text}", size=11, color=ft.Colors.OUTLINE),
        )

    def _build_note_body_card(self, main_note: Path) -> ft.Control:
        """Render the main note's body (frontmatter stripped) as markdown — the
        project's real prose, which the dashboard sections never surface. Read
        live from disk each render."""
        try:
            post = frontmatter.load(str(main_note))
            body = (post.content or "").strip()
        except Exception as ex:
            body = f"_(could not read note: {ex})_"
        header = ft.Row(
            controls=[
                ft.Icon(ft.Icons.ARTICLE_OUTLINED, size=16, color=ft.Colors.OUTLINE),
                ft.Text("NOTE", size=11, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.OUTLINE),
                ft.Container(expand=True),
                ft.Text(main_note.name, size=10, italic=True,
                        font_family="JetBrains Mono", color=ft.Colors.OUTLINE),
            ],
            spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        if not body:
            inner: ft.Control = ft.Text(
                "This note has no body yet — write in it via Open in Obsidian/editor.",
                size=11, italic=True, color=ft.Colors.OUTLINE)
        else:
            inner = ft.Markdown(
                body, selectable=True,
                extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                code_theme=ft.MarkdownCodeTheme.GITHUB,
                on_tap_link=lambda e: self.page.launch_url(e.data),
            )
        return self._section_card(header, [inner])

    def _build_working_dir_section_card(self, p: Project) -> ft.Control:
        resolved = _resolve_working_dir(p.working_dir)
        header = self._section_card_header(
            "WORKING DIR", ft.Icons.TERMINAL_OUTLINED, 0,
        )
        # Override header to omit count + show raw path
        header = ft.Row(
            controls=[
                ft.Icon(icon=ft.Icons.TERMINAL_OUTLINED, size=16, color=ft.Colors.OUTLINE),
                ft.Text("WORKING DIR", size=11, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.OUTLINE),
                ft.Container(expand=True),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        items: list[ft.Control] = []
        items.append(ft.Text(p.working_dir, size=12,
                             font_family="JetBrains Mono", selectable=True))
        if not resolved:
            items.append(ft.Text("(invalid path)", size=11, italic=True,
                                 color=ft.Colors.ERROR))
            return self._section_card(header, items)
        if not resolved.exists():
            items.append(ft.Text(f"(does not exist: {resolved})",
                                 size=11, italic=True, color=ft.Colors.ERROR))
            return self._section_card(header, items)

        scan = _scan_working_dir(resolved)
        git = _git_short_status(resolved)
        meta_parts = [f"{scan['file_count']} files",
                      _human_size(scan.get("size", 0))]
        if scan.get("last_mtime"):
            meta_parts.append(_age_string(scan["last_mtime"], ago=True))
        items.append(ft.Text(" · ".join(meta_parts), size=11,
                             color=ft.Colors.OUTLINE))
        if git:
            items.append(
                ft.Row(
                    controls=[
                        ft.Icon(icon=ft.Icons.SOURCE_OUTLINED, size=14,
                                color=ft.Colors.OUTLINE),
                        ft.Text(git, size=11, color=ft.Colors.OUTLINE),
                    ],
                    spacing=4,
                )
            )

        # Top file types
        by_suffix = scan.get("by_suffix", {})
        if by_suffix:
            suffix_str = " · ".join(f"{count} {suf}" for suf, count in by_suffix.items())
            items.append(ft.Text(suffix_str, size=11, color=ft.Colors.OUTLINE))

        # Actions
        actions = [
            ft.FilledTonalButton(
                "Open in editor", icon=ft.Icons.CODE,
                on_click=lambda e, pth=resolved: self._on_open_in_editor(pth),
            ),
            ft.FilledTonalButton(
                "Open in terminal", icon=ft.Icons.TERMINAL,
                on_click=lambda e, pth=resolved: self._on_open_in_terminal(pth),
            ),
            ft.FilledTonalButton(
                "Reveal", icon=ft.Icons.FOLDER_OPEN,
                on_click=lambda e, pth=resolved: self._on_reveal_in_files(pth),
            ),
            ft.FilledTonalButton(
                "Open CLI session", icon=ft.Icons.SMART_TOY_OUTLINED,
                tooltip=("Open a coding-agent CLI in a terminal here — pick which "
                         "vault parts it sees first (never the whole vault), then "
                         "drive it with full interactive prompts."),
                on_click=lambda e, pth=resolved, proj=p:
                    self._on_open_cli_session(pth, proj),
            ),
        ]
        # Only on real repos — lazygit needs one.
        if _is_git_repo(resolved):
            actions.append(
                ft.FilledTonalButton(
                    "Open in lazygit", icon=ft.Icons.SOURCE_OUTLINED,
                    tooltip="Open this repo in lazygit (a terminal git UI).",
                    on_click=lambda e, pth=resolved: self._on_open_in_git_ui(pth),
                )
            )
        items.append(ft.Row(controls=actions, spacing=8, wrap=True))
        return self._section_card(header, items)

    def _on_open_in_git_ui(self, path: Path):
        err = open_in_git_ui(self.state.config, path)
        if err:
            self._toast(err)

    def _default_cli_opening_message(self, working_dir: Path,
                                     project: Optional[Project]) -> str:
        """Pre-fill the CLI session's first prompt so a fresh chat is oriented:
        what cwd is, what the vault is, and real files to read first — the
        vault's own explainer (CLAUDE.md/README/Home, whichever exists) and the
        project's main note — addressed by their path *inside the symlink* so the
        agent can open them directly. Built dynamically, so it scales to any
        project/vault. The user edits this before launch."""
        ctx_dir = (self.state.config.cli_session_context_dir or
                   ".workbench-context").rstrip("/")
        vault_root = f"{ctx_dir}/{VAULT_PATH.name}"
        # The vault's global explainer (first that exists).
        explainer = next((c for c in ("CLAUDE.md", "README.md", "Home.md")
                          if (VAULT_PATH / c).exists()), "")
        # The project's main note (first context path), and whether it's a
        # folder-project (so we can hint that sibling notes live alongside it).
        main_rel = ""
        if project:
            paths = _project_context_paths(project)
            if paths:
                main_rel = paths[0][0]

        lines = [
            "You're a coding agent. cwd = this project's working_dir (the code you edit).",
            "",
            "The Obsidian vault (a projects / areas / resources knowledge base) is "
            f"symlinked into this repo at {vault_root}/ so you can read it. Get "
            "oriented first:",
        ]
        if explainer:
            lines.append(f"- {vault_root}/{explainer} — how the vault is organized "
                         "(structure + conventions).")
        if main_rel:
            sib = (" (related notes live in the same folder)"
                   if project and project.vault_folder.endswith("/") else "")
            lines.append(f"- {vault_root}/{main_rel} — THIS project: its goal, "
                         f"hypothesis, status and notes{sib}.")
        if project:
            lines.append(f"  Project: {project.name}.")
        lines += [
            "",
            "Images are files: to share an image with me, drop it in this folder "
            "and tell me the path; write any images you generate here too.",
            "",
            "Task: ",
        ]
        return "\n".join(lines)

    def _on_open_cli_session(self, path: Path, project: Optional[Project] = None):
        """Tier 1 CLI session: symlink the whole vault into a gitignored
        `.workbench-context/<vault>/` subfolder of working_dir (so the CLI sees
        it as a subfolder — reads are lazy + permission-gated, so "available" ≠
        "loaded"), then open the coding CLI in a terminal rooted in working_dir.
        The dialog shows an editable, pre-filled **opening message** that is sent
        to the CLI as its first prompt. Images are files: drop one in and
        reference it; generated images land in working_dir too. (Per-path context
        selection is deferred — keep it simple: whole vault + an editable prompt.)"""
        cfg = self.state.config
        default_msg = self._default_cli_opening_message(path, project)
        msg = ft.TextField(
            label="Opening message (sent to the CLI as your first prompt)",
            value=default_msg,
            multiline=True, min_lines=5, max_lines=14, text_size=12,
            hint_text="blank = open the CLI with no first message",
        )
        cli_field = ft.TextField(
            label="CLI", value=cfg.cli_session_command, dense=True, width=180,
        )

        # --- Image attach + gallery (Tier 1: images are files) ---
        def insert_path(rel: str):
            cur = msg.value or ""
            sep = "" if (not cur or cur.endswith("\n") or cur.endswith(" ")) else "\n"
            msg.value = cur + sep + rel
            try:  # .page raises if not yet mounted (Flet 0.85)
                msg.update()
            except Exception:
                pass

        def img_cell(info: dict) -> ft.Control:
            return ft.Container(
                width=96, padding=4, border_radius=4,
                bgcolor=ft.Colors.with_opacity(0.04, ft.Colors.ON_SURFACE),
                content=ft.Column(
                    [ft.Image(src=str(info["abs"]), width=88, height=72,
                              fit=ft.ImageFit.COVER, border_radius=3),
                     ft.Text(info["rel"], size=9, font_family="JetBrains Mono",
                             max_lines=2, selectable=True),
                     ft.TextButton(
                         "Insert", icon=ft.Icons.SUBDIRECTORY_ARROW_LEFT,
                         tooltip="Add this path to the opening message",
                         on_click=lambda e, r=info["rel"]: insert_path(r),
                         style=ft.ButtonStyle(padding=2)),
                     ],
                    spacing=2,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            )

        gallery = ft.Row(wrap=True, spacing=6, run_spacing=6)

        def rebuild_gallery():
            imgs = list_session_images(path, cfg.cli_session_image_dir)
            gallery.controls = ([img_cell(i) for i in imgs] if imgs else
                                [ft.Text("No images yet — Add images to attach one.",
                                         size=11, italic=True,
                                         color=ft.Colors.OUTLINE)])
            try:  # .page raises if not yet mounted (initial pre-dialog build)
                gallery.update()
            except Exception:
                pass

        # FilePicker is a Service in Flet 0.85 — register it on page.services
        # (once), and pick_files() is async and RETURNS the chosen files (no
        # on_result callback / FilePickerResultEvent in this version).
        if not getattr(self, "_cli_img_picker", None):
            self._cli_img_picker = ft.FilePicker()
            self.page.services.append(self._cli_img_picker)
            self.page.update()
        rebuild_gallery()

        async def _pick_images():
            try:
                files = await self._cli_img_picker.pick_files(
                    allow_multiple=True, file_type=ft.FilePickerFileType.IMAGE)
            except Exception as ex:
                self._toast(f"image picker failed: {ex}")
                return
            added: list[dict] = []
            for f in (files or []):
                fp = getattr(f, "path", None)
                if not fp:
                    continue
                info, err = import_session_image(
                    path, cfg.cli_session_image_dir, Path(fp))
                if err:
                    self._toast(f"image import failed: {err}")
                elif info:
                    added.append(info)
            rebuild_gallery()
            for info in added:  # auto-insert the freshly attached paths
                insert_path(info["rel"])

        add_imgs_btn = ft.FilledTonalButton(
            "Add images", icon=ft.Icons.ADD_PHOTO_ALTERNATE_OUTLINED,
            tooltip="Copy images into this session's folder and reference them",
            on_click=lambda e: self.page.run_task(_pick_images),
        )

        def launch(_e):
            self.page.pop_dialog()
            # Always symlink the whole vault — one link, simple.
            _linked, err = sync_context_symlinks(
                path, cfg.cli_session_context_dir, [VAULT_PATH])
            if err:
                self._toast(f"context setup failed: {err}")
                return
            launch_err = open_cli_session(
                cfg, path, cli=(cli_field.value or "").strip(),
                first_message=(msg.value or ""))
            if launch_err:
                self._toast(f"CLI launch failed: {launch_err}")
                return
            self._toast(f"CLI session opened in {path.name} · vault linked")

        dlg = ft.AlertDialog(
            title=ft.Text("Open CLI session"),
            content=ft.Container(
                width=540, height=560,
                content=ft.Column(
                    [ft.Text(f"Folder (cwd):  {path}", size=12,
                             font_family="JetBrains Mono", selectable=True),
                     ft.Text("Opens a coding-agent CLI in a terminal here. The "
                             f"whole vault is symlinked into {cfg.cli_session_context_dir}/ "
                             "(gitignored) so the agent can read your notes. Edit "
                             "the opening message below — it's sent as the first "
                             "prompt.",
                             size=11, color=ft.Colors.OUTLINE),
                     msg,
                     ft.Row([ft.Text("Images", size=11, weight=ft.FontWeight.BOLD,
                                     color=ft.Colors.OUTLINE),
                             ft.Container(expand=True), add_imgs_btn],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER),
                     gallery,
                     cli_field],
                    spacing=12, tight=True, scroll=ft.ScrollMode.AUTO,
                ),
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _e: self.page.pop_dialog()),
                ft.FilledButton("Launch", on_click=launch),
            ],
        )
        self.page.show_dialog(dlg)

    def _on_open_in_editor(self, path: Path):
        err = open_in_editor(self.state.config, path)
        if err:
            self._toast(f"open in editor failed: {err}")

    def _on_open_in_terminal(self, path: Path):
        err = open_in_terminal(self.state.config, path)
        if err:
            self._toast(f"open in terminal failed: {err}")

    def _on_reveal_in_files(self, path: Path):
        err = reveal_in_files(path)
        if err:
            self._toast(f"reveal failed: {err}")

    def _on_open_in_obsidian(self, path: Path):
        err = open_in_obsidian(path, VAULT_PATH)
        if err:
            self._toast(f"open in Obsidian failed: {err}")

    # --- Publish to WordPress.com (v0.10) -----------------------------------
    def _note_publish_state(self, path: Path) -> tuple[bool, str]:
        """Read a note's frontmatter for an existing WP publish. Returns
        (already_published, published_url) — drives the button's create↔update
        label and the toast's Open action."""
        try:
            post = frontmatter.load(str(path))
            pid = str(post.metadata.get("wp_post_id") or "").strip()
            url = str(post.metadata.get("published_url") or "").strip()
            return bool(pid), url
        except Exception:
            return False, ""

    def _project_for_path(self, path: Path) -> Optional[Project]:
        """Find the project whose vault folder contains this note (longest match).
        Used to derive the publish category/tags when the caller didn't pass one."""
        try:
            sp = path.resolve()
        except Exception:
            sp = path
        best, best_len = None, -1
        for p in self.state.projects:
            if not p.vault_folder.endswith("/"):
                continue  # file-scoped projects can't hold posts
            folder = (VAULT_PATH / p.vault_folder).resolve()
            try:
                sp.relative_to(folder)
            except ValueError:
                continue
            if len(str(folder)) > best_len:
                best, best_len = p, len(str(folder))
        return best

    def _publish_options_for(self, project: Optional[Project], *,
                             visibility: str = "", password: str = ""
                             ) -> "publish.PublishOptions":
        """Build PublishOptions from config + the note's project/area context."""
        cfg = self.state.config
        cat_src = getattr(cfg, "publish_auto_category", "area")
        category = ""
        if project:
            if cat_src == "area":
                category = project.area or ""
            elif cat_src == "project":
                category = project.name
        extra = [project.name] if (project and getattr(
            cfg, "publish_add_project_tag", True)) else []
        excl = [t.strip() for t in (getattr(cfg, "publish_tag_exclude", "") or "").split(",")
                if t.strip()]
        return publish.PublishOptions(
            default_status=getattr(cfg, "publish_default_status", "draft"),
            category=category, extra_tags=extra,
            include_note_tags=bool(getattr(cfg, "publish_include_note_tags", True)),
            tag_exclude=excl, visibility=visibility, password=password,
        )

    def _tool_publish_note(self, args: dict) -> str:
        """Executor for the agent's `publish_note` tool (wired into ToolContext).
        Runs on the dispatch worker thread, so the synchronous network publish is
        fine here. Reuses the same vault-aware policy as the button; returns a
        human-readable result string for the model."""
        raw = str(args.get("path", "")).strip()
        if not raw:
            return "[publish_note: missing 'path']"
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = VAULT_PATH / raw
        if not path.is_file():
            return f"[publish_note: note not found: {path}]"
        cfg = self.state.config
        creds = publish.creds_from_config(cfg)
        try:
            creds.validate()
        except publish.PublishError as ex:
            return f"[publish_note: {ex}]"
        project = self._project_for_path(path)
        opts = self._publish_options_for(
            project, visibility=str(args.get("visibility") or "").strip(),
            password=str(args.get("password") or "").strip())
        if args.get("status"):
            opts.status = str(args["status"]).strip()
        try:
            res = publish.publish_note(creds, path, options=opts)
        except publish.PublishError as ex:
            return f"[publish_note failed: {ex}]"
        except Exception as ex:
            return f"[publish_note error: {ex}]"
        parts = [f"{res.action} ({res.status}): {res.title}"]
        if res.url:
            parts.append(f"url={res.url}")
        if res.categories:
            parts.append(f"categories={', '.join(res.categories)}")
        if res.tags:
            parts.append(f"tags={', '.join(res.tags)}")
        return " · ".join(parts)

    def _on_publish_note(self, path: Path, project: Optional[Project] = None):
        """Confirm (with visibility/password + a category/tag preview), then publish
        or update on WordPress.com. Draft-first; the network call runs off the UI
        thread in _do_publish_note."""
        cfg = self.state.config
        try:
            publish.creds_from_config(cfg).validate()
        except publish.PublishError as ex:
            self._toast(str(ex))
            return
        project = project or self._project_for_path(path)
        published, _ = self._note_publish_state(path)
        default_status = getattr(cfg, "publish_default_status", "draft")
        site = getattr(cfg, "wpcom_site", "") or "WordPress.com"

        # Preview the computed categories/tags + seed visibility from frontmatter
        # (pass no visibility override so the note's own value shows through).
        try:
            _pl, prev, _pid = publish.build_payload(
                path, self._publish_options_for(project))
            cats, tags, seed_vis = prev.categories, prev.tags, prev.visibility
        except Exception:
            cats, tags, seed_vis = [], [], "public"

        vis_dd = ft.Dropdown(
            label="Visibility", value=seed_vis or "public",
            options=[ft.dropdown.Option("public"), ft.dropdown.Option("private"),
                     ft.dropdown.Option("password")])
        pwd_field = ft.TextField(label="Post password (for password-protected)",
                                 password=True, can_reveal_password=True, text_size=13)
        verb = "Update" if published else "Publish"
        body = (f"Push the latest version of “{path.stem}” to its existing post on {site}."
                if published else
                f"Create a new {default_status} post for “{path.stem}” on {site}.")
        meta_line = "  ·  ".join(filter(None, [
            f"Category: {', '.join(cats)}" if cats else "",
            f"Tags: {', '.join(tags)}" if tags else "",
        ])) or "No category/tags (set an area, or wp_categories/wp_tags in the note)."

        def go(_e):
            self.page.pop_dialog()
            opts = self._publish_options_for(
                project, visibility=(vis_dd.value or "").strip(),
                password=(pwd_field.value or "").strip())
            self._do_publish_note(path, opts)

        dlg = ft.AlertDialog(
            title=ft.Text("Update published post" if published else "Publish to web"),
            content=ft.Container(width=480, content=ft.Column([
                ft.Text(body, size=13),
                ft.Text(meta_line, size=11, italic=True, color=ft.Colors.OUTLINE),
                vis_dd,
                pwd_field,
                ft.Text(f"Status: {default_status}  ·  stays editable on WP.",
                        size=11, italic=True, color=ft.Colors.OUTLINE),
            ], tight=True, spacing=8)),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _e: self.page.pop_dialog()),
                ft.FilledButton(verb, icon=ft.Icons.PUBLIC, on_click=go),
            ],
        )
        self.page.show_dialog(dlg)

    def _do_publish_note(self, path: Path, options: "publish.PublishOptions"):
        """Run the (network) publish off the UI thread; marshal the result toast +
        a refresh back onto the event loop (Flet 0.85 can't touch UI from a worker
        thread — mirrors the dispatch / tool-confirm marshaling)."""
        self._toast("Publishing…")
        creds = publish.creds_from_config(self.state.config)

        def run():
            try:
                res = publish.publish_note(creds, path, options=options)
                extra = "".join([
                    f" · cat: {', '.join(res.categories)}" if res.categories else "",
                    f" · tags: {', '.join(res.tags)}" if res.tags else "",
                ])
                msg = f"{res.action} ({res.status}): {res.title}{extra}"
                url = res.url
            except publish.PublishError as ex:
                msg, url = f"Publish failed: {ex}", ""
            except Exception as ex:
                msg, url = f"Publish error: {ex}", ""

            async def _finish():
                self._toast(msg, url=url or None)
                self.refresh()  # re-scan disk → button flips create→update

            try:
                self.page.run_task(_finish)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _toast(self, msg: str, url: Optional[str] = None):
        # SnackBar is a DialogControl in this Flet (0.85) — show it via
        # show_dialog, not the old page.open(... open=True) API (which silently
        # failed, so toasts never appeared). `url` adds an "Open" action button
        # (used by publish to jump to the live post).
        try:
            if url:
                bar = ft.SnackBar(content=ft.Text(msg), action="Open",
                                  on_action=lambda _e: self.page.launch_url(url))
            else:
                bar = ft.SnackBar(content=ft.Text(msg))
            self.page.show_dialog(bar)
        except Exception:
            pass

    def _build_threads_section_card(self, p: Project) -> ft.Control:
        header = self._section_card_header(
            "THREADS", ft.Icons.CHAT_BUBBLE_OUTLINE, len(p.threads)
        )
        items: list[ft.Control] = []
        if not p.threads:
            items.append(ft.Text(
                "No threads yet. Start one from the chat tab.",
                size=11, italic=True, color=ft.Colors.OUTLINE,
            ))
        else:
            for t in p.threads[:5]:
                is_open = self.state.is_thread_open(t.id)
                items.append(
                    ft.Container(
                        padding=ft.Padding(left=10, top=8, right=10, bottom=8),
                        border_radius=6,
                        bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                        content=ft.Row(
                            controls=[
                                ft.Icon(icon=ft.Icons.CHAT_BUBBLE_OUTLINE, size=14,
                                        color=ft.Colors.OUTLINE),
                                ft.Text(t.name, size=13),
                                ft.Container(expand=True),
                                ft.Text("open" if is_open else "", size=10,
                                        color=ft.Colors.OUTLINE),
                                ft.IconButton(
                                    icon=ft.Icons.EDIT_OUTLINED, icon_size=14,
                                    tooltip="Rename thread",
                                    on_click=lambda e, tid=t.id: self._rename_thread_dialog(tid),
                                ),
                            ],
                            spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        on_click=lambda e, tid=t.id: self._open_thread(tid),
                    )
                )
        items.append(
            ft.TextButton(
                "+ new thread", icon=ft.Icons.ADD,
                on_click=lambda e, pid=p.id: self._on_new_thread(pid),
            )
        )
        return self._section_card(header, items)

    def _build_tasks_section_card(self, p: Project, tasks: list[Task]) -> ft.Control:
        open_count = sum(1 for t in tasks if not t.checked)
        done_count = sum(1 for t in tasks if t.checked)
        header = self._section_card_header(
            "TASKS", ft.Icons.CHECKLIST, len(tasks),
            see_all=(lambda: self._open_tasks_view(p.id)) if tasks else None,
        )
        items: list[ft.Control] = [
            ft.Text("`- [ ] task` / `- [x] done` checkboxes parsed from this "
                    "project's notes. New tasks are added to the main note.",
                    size=11, italic=True, color=ft.Colors.OUTLINE),
            ft.Text(f"{open_count} open · {done_count} done",
                    size=11, color=ft.Colors.OUTLINE),
        ]
        if tasks:
            preview = [t for t in tasks if not t.checked][:3]
            for t in preview:
                items.append(self._render_task_item(t))
            if open_count > 3:
                items.append(
                    ft.Text(f"+ {open_count - 3} more open · click 'see all' for full kanban",
                            size=11, italic=True, color=ft.Colors.OUTLINE)
                )
        # Inline add-task → appends to the main note under `## Tasks`.
        add_field = ft.TextField(
            hint_text="add a task…", text_size=12, dense=True, expand=True,
            on_submit=lambda e, pid=p.id: self._on_add_task(pid, e),
        )
        items.append(
            ft.Row(
                controls=[
                    add_field,
                    ft.IconButton(icon=ft.Icons.ADD, tooltip="Add task to the main note",
                                  on_click=lambda e, pid=p.id, f=add_field:
                                      self._on_add_task(pid, f)),
                ],
                spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )
        return self._section_card(header, items)

    def _build_notes_section_card(self, *, label: str, icon, notes: list[Note],
                                   caption: str, project: Optional[Project] = None,
                                   create: Optional[list] = None,
                                   publishable: bool = False) -> ft.Control:
        """A notes dashboard section. `caption` is always shown (the convention,
        e.g. "Notes with type: post"). `create` = list of (button_label,
        note_type) → buttons that create a typed note in the project folder.
        `publishable` → each row gets a Publish-to-web / Update-published button."""
        header = self._section_card_header(label, icon, len(notes))
        items: list[ft.Control] = [
            ft.Text(caption, size=11, italic=True, color=ft.Colors.OUTLINE),
        ]
        for n in notes[:5]:
            hdr_controls: list[ft.Control] = [
                ft.Icon(icon=ft.Icons.DESCRIPTION_OUTLINED,
                        size=14, color=ft.Colors.OUTLINE),
                ft.Text(n.name, size=13, weight=ft.FontWeight.BOLD),
                ft.Container(expand=True),
                ft.Text(n.note_type, size=10,
                        color=ft.Colors.OUTLINE, italic=True),
            ]
            if publishable:
                pub, _purl = self._note_publish_state(Path(n.path))
                hdr_controls.append(ft.IconButton(
                    icon=ft.Icons.CLOUD_DONE_OUTLINED if pub else ft.Icons.PUBLIC,
                    icon_size=16,
                    icon_color=ft.Colors.PRIMARY if pub else ft.Colors.OUTLINE,
                    tooltip=("Update published post on WordPress"
                             if pub else "Publish to web (draft) on WordPress"),
                    on_click=lambda e, pth=Path(n.path), pr=project:
                        self._on_publish_note(pth, pr),
                ))
            items.append(
                ft.Container(
                    padding=ft.Padding(left=10, top=6, right=10, bottom=6),
                    border_radius=6, ink=True,
                    bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                    tooltip="Open in Obsidian",
                    on_click=lambda e, pth=Path(n.path): self._on_open_in_obsidian(pth),
                    content=ft.Column(
                        spacing=2,
                        controls=[
                            ft.Row(
                                controls=hdr_controls,
                                spacing=6,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            ft.Text(n.summary, size=11,
                                    color=ft.Colors.ON_SURFACE_VARIANT),
                        ],
                    ),
                )
            )
        if len(notes) > 5:
            items.append(
                ft.Text(f"+ {len(notes) - 5} more",
                        size=11, italic=True, color=ft.Colors.OUTLINE)
            )
        if create and project is not None:
            items.append(
                ft.Row(
                    controls=[
                        ft.TextButton(
                            blabel, icon=ft.Icons.ADD,
                            on_click=lambda e, pid=project.id, nt=ntype:
                                self._on_create_typed_note(pid, nt),
                        )
                        for blabel, ntype in create
                    ],
                    spacing=4, wrap=True,
                )
            )
        return self._section_card(header, items)

    def _build_files_section_card(self, files: list[FileItem]) -> ft.Control:
        header = self._section_card_header(
            "FILES", ft.Icons.FOLDER_OUTLINED, len(files)
        )
        items: list[ft.Control] = []
        if not files:
            items.append(
                ft.Text("No non-markdown files in this project folder.",
                        size=11, italic=True, color=ft.Colors.OUTLINE)
            )
        else:
            counts = Counter(f.suffix or "(no ext)" for f in files)
            summary = " · ".join(f"{count} {suf}" for suf, count in counts.most_common())
            items.append(ft.Text(summary, size=11, color=ft.Colors.OUTLINE))
            for f in files[:6]:
                items.append(
                    ft.Container(
                        padding=ft.Padding(left=10, top=6, right=10, bottom=6),
                        border_radius=6,
                        bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                        content=ft.Row(
                            controls=[
                                ft.Icon(icon=self._icon_for_file(f.suffix),
                                        size=14, color=ft.Colors.OUTLINE),
                                ft.Text(f.name, size=13, expand=True),
                                ft.Text(_human_size(f.size), size=10,
                                        color=ft.Colors.OUTLINE),
                            ],
                            spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    )
                )
            if len(files) > 6:
                items.append(
                    ft.Text(f"+ {len(files) - 6} more",
                            size=11, italic=True, color=ft.Colors.OUTLINE)
                )
        return self._section_card(header, items)

    def _icon_for_file(self, suffix: str):
        s = suffix.lower()
        if s == ".pdf":
            return ft.Icons.PICTURE_AS_PDF
        if s in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
            return ft.Icons.IMAGE_OUTLINED
        if s in (".mp3", ".wav", ".flac", ".ogg", ".m4a"):
            return ft.Icons.AUDIO_FILE_OUTLINED
        if s in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
            return ft.Icons.MOVIE_OUTLINED
        if s in (".py", ".js", ".ts", ".rs", ".go", ".sh", ".html", ".css", ".json"):
            return ft.Icons.CODE
        if s == ".zip" or s == ".tar" or s == ".gz":
            return ft.Icons.FOLDER_ZIP_OUTLINED
        return ft.Icons.INSERT_DRIVE_FILE_OUTLINED

    # --- AgentView refresh ---
    def _refresh_agent_view(self):
        self.agent_content.controls.clear()
        if not self.state.active_tab or self.state.active_tab.kind != "agent":
            return
        a = self.state.get_agent(self.state.active_tab.ref_id)
        if not a:
            self.agent_content.controls.append(
                ft.Text("Agent not found.", size=14, color=ft.Colors.OUTLINE)
            )
            return

        self.agent_content.controls.append(
            ft.Row(
                controls=[
                    ft.Container(
                        width=56, height=56, border_radius=28,
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Icon(
                            icon=a.icon or ft.Icons.SMART_TOY,
                            size=32, color=ft.Colors.TERTIARY,
                        ),
                    ),
                    self._title_text(a.name),
                    ft.Container(
                        padding=ft.Padding(left=8, top=2, right=8, bottom=2),
                        border_radius=10,
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                        content=ft.Text("AGENT", size=10,
                                        weight=ft.FontWeight.BOLD, color=ft.Colors.TERTIARY),
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )

        self.agent_content.controls.append(
            ft.Text(f"Model: {a.model}", size=12,
                    font_family="JetBrains Mono", color=ft.Colors.OUTLINE)
        )
        if a.source_path:
            self.agent_content.controls.append(
                ft.Text(a.source_path, size=11,
                        font_family="JetBrains Mono",
                        color=ft.Colors.OUTLINE, italic=True)
            )

        self.agent_content.controls.append(
            ft.Container(
                padding=16,
                border_radius=8,
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                content=ft.Column(
                    spacing=4,
                    controls=[
                        ft.Text("ROLE / SYSTEM PROMPT", size=10,
                                weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE),
                        ft.Text(a.role, size=15,
                                font_family="Newsreader", selectable=True),
                    ],
                ),
            )
        )

        self.agent_content.controls.append(
            ft.Text("Edit this agent by opening its .md file in Obsidian.",
                    size=11, italic=True, color=ft.Colors.OUTLINE)
        )

    # --- AreaView refresh ---
    def _refresh_area_view(self):
        self.area_content.controls.clear()
        if not self.state.active_tab or self.state.active_tab.kind != "area":
            return
        area = self.state.get_area(self.state.active_tab.ref_id)
        if not area:
            self.area_content.controls.append(
                ft.Text("Area not found.", color=ft.Colors.OUTLINE))
            return

        projects = self.state.projects_by_area().get(area.name, [])
        active_projects = [p for p in projects if p.status == "active"]
        other_projects = [p for p in projects if p.status != "active"]
        status_color = STATUS_COLORS.get(area.status, ft.Colors.OUTLINE)

        # Header
        self.area_content.controls.append(
            ft.Row(
                controls=[
                    ft.Icon(icon=ft.Icons.WORKSPACES_OUTLINED, size=28,
                            color=ft.Colors.OUTLINE),
                    self._title_text(area.name),
                    ft.Container(
                        padding=ft.Padding(left=8, top=2, right=8, bottom=2),
                        border_radius=10,
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                        content=ft.Text("AREA", size=10,
                                        weight=ft.FontWeight.BOLD, color=status_color),
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )

        # Description (from the area note's body)
        if area.description:
            self.area_content.controls.append(
                ft.Container(
                    padding=16,
                    border_radius=8,
                    bgcolor=ft.Colors.SURFACE_CONTAINER,
                    content=ft.Text(area.description, size=16,
                                    font_family="Newsreader", italic=True),
                )
            )

        # Active projects
        header = self._section_card_header(
            "ACTIVE PROJECTS", ft.Icons.FOLDER_SPECIAL_OUTLINED, len(active_projects)
        )
        items: list[ft.Control] = []
        if not active_projects:
            items.append(
                ft.Text("No active projects in this area.",
                        size=11, italic=True, color=ft.Colors.OUTLINE)
            )
        else:
            items.append(self._sticky_grid(active_projects))  # board: sticky grid
        self.area_content.controls.append(self._section_card(header, items))

        # Other projects (idea / persist / pause / pivot / done) — collapsed by
        # default to keep the focus on active work; click the header to expand
        # the full sticky grid. Collapsed state shows a status breakdown line.
        if other_projects:
            expanded = area.name in self._area_other_expanded
            counts: dict[str, int] = {}
            for p in other_projects:
                counts[p.status] = counts.get(p.status, 0) + 1
            breakdown = " · ".join(
                f"{n} {st}" for st, n in sorted(counts.items(), key=lambda kv: -kv[1])
            )
            header2 = ft.Container(
                ink=True,
                border_radius=6,
                padding=ft.Padding(left=2, top=2, right=2, bottom=2),
                on_click=lambda e, name=area.name: self._on_toggle_area_other(name),
                content=ft.Row(
                    controls=[
                        ft.Icon(icon=ft.Icons.FOLDER_OUTLINED, size=16,
                                color=ft.Colors.OUTLINE),
                        ft.Text("OTHER PROJECTS", size=11, weight=ft.FontWeight.BOLD,
                                color=ft.Colors.OUTLINE),
                        ft.Container(
                            padding=ft.Padding(left=6, top=1, right=6, bottom=1),
                            border_radius=8,
                            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                            content=ft.Text(str(len(other_projects)), size=10,
                                            weight=ft.FontWeight.BOLD,
                                            color=ft.Colors.OUTLINE),
                        ),
                        ft.Container(expand=True),
                        ft.Icon(
                            icon=(ft.Icons.EXPAND_MORE if expanded
                                  else ft.Icons.CHEVRON_RIGHT),
                            size=18, color=ft.Colors.OUTLINE,
                        ),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            )
            if expanded:
                items2: list[ft.Control] = [self._sticky_grid(other_projects)]
            else:
                items2 = [
                    ft.Text(breakdown, size=12, italic=True,
                            color=ft.Colors.OUTLINE),
                ]
            self.area_content.controls.append(self._section_card(header2, items2))

        # Source path footer
        self.area_content.controls.append(
            ft.Text(f"Source: {area.source_path}", size=11,
                    italic=True, color=ft.Colors.OUTLINE)
        )

    def _on_toggle_area_other(self, area_name: str):
        if area_name in self._area_other_expanded:
            self._area_other_expanded.discard(area_name)
        else:
            self._area_other_expanded.add(area_name)
        self._refresh_area_view()
        self.page.update()

    # --- HomeView refresh ---
    def _refresh_home_view(self):
        self.home_content.controls.clear()
        today = date.today()
        date_str = today.strftime("%A · %B %-d, %Y")

        # Big date strip — anchor
        self.home_content.controls.append(
            ft.Text(date_str, size=32, font_family="Newsreader",
                    weight=ft.FontWeight.BOLD),
        )

        # Quick-stats row: counts as clickable chips, each jumping to its tab.
        # Demand probes are active projects too, but split into their own bucket
        # (Home shows them in a dedicated section) so the "Active" count reflects
        # focused work, matching the portfolio mental model (N active + M probes).
        inbox_n = len(self.state.inbox_items)
        active_projects = [p for p in self.state.projects if p.status == "active"]
        probe_projects = [p for p in active_projects if p.is_probe]
        active_focus = [p for p in active_projects if not p.is_probe]
        areas_n = len(self.state.areas)
        agents_n = len(self.state.agents)
        rbuckets = self._compute_review_buckets()
        reviews_due = len(rbuckets["needs"]) + len(rbuckets["past"]) + len(rbuckets["today"])

        self.home_content.controls.append(
            ft.Row(
                controls=[
                    self._home_stat_chip(
                        "Inbox", inbox_n, ft.Icons.INBOX_OUTLINED,
                        on_click=lambda e: self._open_inbox(),
                    ),
                    self._home_stat_chip(
                        "Reviews due", reviews_due, ft.Icons.RATE_REVIEW_OUTLINED,
                        on_click=lambda e: self._open_reviews(),
                        urgent=reviews_due > 0,
                    ),
                    self._home_stat_chip(
                        "Active", len(active_focus), ft.Icons.FOLDER_SPECIAL_OUTLINED,
                    ),
                    self._home_stat_chip(
                        "Probes", len(probe_projects), ft.Icons.SENSORS,
                    ),
                    self._home_stat_chip(
                        "Areas", areas_n, ft.Icons.WORKSPACES_OUTLINED,
                    ),
                    self._home_stat_chip(
                        "Agents", agents_n, ft.Icons.SMART_TOY_OUTLINED,
                    ),
                ],
                spacing=12,
                wrap=True,
            )
        )

        # Vault git launcher — manage the vault's own save points in lazygit.
        # Vault-level git sits with the other vault-level surfaces (Home). Only
        # shown when the vault is a git repo.
        if _is_git_repo(VAULT_PATH):
            self.home_content.controls.append(
                ft.Row(
                    controls=[
                        ft.FilledTonalButton(
                            "Open vault in lazygit", icon=ft.Icons.SOURCE_OUTLINED,
                            tooltip="Open the vault repo in lazygit (a terminal git UI).",
                            on_click=lambda e: self._on_open_in_git_ui(VAULT_PATH),
                        ),
                    ],
                )
            )

        # Top-level chat launcher — start asking without picking a project.
        self.home_content.controls.append(self._build_home_chat_launcher())

        # Mode filter chips — execution lenses across all projects.
        # "" = All (untagged projects only show here). Predefined set matches the
        # current vault convention; tags found on projects but not listed here
        # are still filterable via "All" — add new chips when one earns its own pane.
        selected_mode = self.state.home_mode_filter
        mode_chip_specs = [
            ("", "All"),
            ("publish", "Publish"),
            ("create", "Create"),
            ("networking", "Networking"),
        ]
        self.home_content.controls.append(
            ft.Text("EXECUTION MODES", size=10, weight=ft.FontWeight.BOLD,
                    color=ft.Colors.OUTLINE),
        )
        self.home_content.controls.append(
            ft.Row(
                controls=[
                    self._home_mode_chip(label, key, key == selected_mode)
                    for key, label in mode_chip_specs
                ],
                spacing=8,
                wrap=True,
            )
        )

        # Active projects — the launcher block. The mode filter is an execution
        # LENS across *all* active projects (probes included), orthogonal to the
        # probe/focus portfolio split. So a project that is both a probe and
        # carries `modes:` (e.g. the LinkedIn campaign) shows here under its mode
        # AND again in the DEMAND PROBES section below — intentional duplication:
        # the two sections answer different questions ("what's my publish work?"
        # vs "what's listening for a bite?").
        if selected_mode:
            filtered = [p for p in active_projects if selected_mode in p.modes]
        else:
            filtered = active_focus  # unfiltered view keeps probes in their own section only

        header_label = "ACTIVE PROJECTS"
        if selected_mode:
            header_label = f"ACTIVE · {selected_mode.upper()} ({len(filtered)}/{len(active_projects)})"

        if filtered:
            self.home_content.controls.append(
                ft.Text(header_label, size=10,
                        weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE)
            )
            # Board view: wrapping grid of Stickies notes (sidebar stays rows).
            self.home_content.controls.append(self._sticky_grid(filtered))
        elif active_projects and selected_mode:
            self.home_content.controls.append(
                ft.Text(header_label, size=10,
                        weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE)
            )
            self.home_content.controls.append(
                ft.Text(
                    f"No active projects tagged `{selected_mode}` yet. "
                    f"Add `modes: [{selected_mode}]` to a project's frontmatter.",
                    size=12, italic=True, color=ft.Colors.OUTLINE,
                ),
            )
        else:
            self.home_content.controls.append(
                ft.Text("No active projects. Open the sidebar to start one.",
                        size=12, italic=True, color=ft.Colors.OUTLINE),
            )

        # Demand probes — a distinct portfolio bucket (see Kaizen Loop →
        # "Demand probes"): low-touch active projects listening for a bite.
        # Always shown (unfiltered by mode), sorted stalest-first so the one
        # most overdue for an outbound action surfaces at the top.
        if probe_projects:
            ordered = sorted(
                probe_projects,
                key=lambda p: (not self._probe_glance(p)[1], p.name.lower()),
            )
            self.home_content.controls.append(
                ft.Container(height=6),
            )
            self.home_content.controls.append(
                ft.Text(f"DEMAND PROBES ({len(probe_projects)})", size=10,
                        weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE)
            )
            for p in ordered:
                self.home_content.controls.append(self._render_probe_row(p))

    def _build_home_chat_launcher(self) -> ft.Control:
        controls: list[ft.Control] = [
            ft.Row([ft.FilledButton("New chat", icon=ft.Icons.CHAT_BUBBLE_OUTLINE,
                                    on_click=self._on_new_scratch_thread)], spacing=8),
        ]
        all_chats = list(reversed(self.state.scratch_threads))  # newest first
        total = len(all_chats)
        size = self._scratch_page_size
        pages = max(1, (total + size - 1) // size)
        # Clamp the page in case threads were added/removed since last render.
        self._scratch_page = max(0, min(self._scratch_page, pages - 1))
        start = self._scratch_page * size
        page_chats = all_chats[start:start + size]

        if all_chats:
            controls.append(ft.Text("CHATS", size=10, weight=ft.FontWeight.BOLD,
                                    color=ft.Colors.OUTLINE))
            for th in page_chats:
                controls.append(self._render_scratch_thread_row(th))
            # Pager — only when there's more than one page.
            if pages > 1:
                controls.append(self._build_scratch_pager(pages, total))
        return ft.Column(controls, spacing=8)

    def _build_scratch_pager(self, pages: int, total: int) -> ft.Control:
        page = self._scratch_page
        return ft.Row(
            [
                ft.IconButton(icon=ft.Icons.CHEVRON_LEFT, icon_size=18,
                              tooltip="Newer chats", disabled=page <= 0,
                              on_click=lambda e: self._on_scratch_page(-1)),
                ft.Text(f"{page + 1} / {pages}", size=11, color=ft.Colors.OUTLINE),
                ft.IconButton(icon=ft.Icons.CHEVRON_RIGHT, icon_size=18,
                              tooltip="Older chats", disabled=page >= pages - 1,
                              on_click=lambda e: self._on_scratch_page(1)),
                ft.Container(expand=True),
                ft.Text(f"{total} chats", size=11, color=ft.Colors.OUTLINE),
            ],
            spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _on_scratch_page(self, delta: int):
        self._scratch_page += delta  # clamped in _build_home_chat_launcher
        self.refresh()

    def _render_scratch_thread_row(self, th: Thread) -> ft.Control:
        return ft.Container(
            padding=ft.Padding(left=10, top=8, right=10, bottom=8),
            border_radius=6, bgcolor=ft.Colors.SURFACE_CONTAINER_LOW, ink=True,
            on_click=lambda e, tid=th.id: self._open_thread(tid),
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.CHAT_BUBBLE_OUTLINE, size=14, color=ft.Colors.OUTLINE),
                    ft.Text(th.name, size=13),
                    ft.Container(expand=True),
                    ft.IconButton(icon=ft.Icons.EDIT_OUTLINED, icon_size=14,
                                  tooltip="Rename chat",
                                  on_click=lambda e, tid=th.id: self._rename_thread_dialog(tid)),
                ],
                spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _home_mode_chip(self, label: str, key: str, selected: bool) -> ft.Control:
        return ft.Container(
            padding=ft.Padding(left=12, top=6, right=12, bottom=6),
            border_radius=16,
            border=_all_border(
                color=ft.Colors.PRIMARY if selected else ft.Colors.OUTLINE_VARIANT,
            ),
            bgcolor=ft.Colors.PRIMARY_CONTAINER if selected else ft.Colors.SURFACE_CONTAINER_LOW,
            content=ft.Text(
                label,
                size=12,
                weight=ft.FontWeight.BOLD if selected else ft.FontWeight.NORMAL,
                color=ft.Colors.ON_PRIMARY_CONTAINER if selected else ft.Colors.ON_SURFACE,
            ),
            on_click=lambda e, k=key: self._on_home_mode_chip_click(k),
        )

    def _on_home_mode_chip_click(self, key: str):
        # Toggle off if the user re-clicks the active chip → back to All.
        if self.state.home_mode_filter == key:
            self.state.home_mode_filter = ""
        else:
            self.state.home_mode_filter = key
        self._refresh_home_view()
        if self.page:
            self.page.update()

    def _home_stat_chip(self, label: str, count: int, icon,
                        on_click=None, urgent: bool = False) -> ft.Control:
        # urgent = something needs you (e.g. reviews due) → error-tinted nudge.
        accent = ft.Colors.ERROR if urgent else ft.Colors.OUTLINE
        return ft.Container(
            padding=ft.Padding(left=14, top=10, right=14, bottom=10),
            border_radius=8,
            border=(_all_border(ft.Colors.ERROR, 1) if urgent else _all_border()),
            bgcolor=ft.Colors.ERROR_CONTAINER if urgent else ft.Colors.SURFACE_CONTAINER_LOW,
            content=ft.Row(
                controls=[
                    ft.Icon(icon=icon, size=18, color=accent),
                    ft.Text(str(count), size=20, weight=ft.FontWeight.BOLD,
                            font_family="Newsreader",
                            color=ft.Colors.ON_ERROR_CONTAINER if urgent else None),
                    ft.Text(label, size=12,
                            color=ft.Colors.ON_ERROR_CONTAINER if urgent else ft.Colors.OUTLINE),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            on_click=on_click,
        )

    # --- InboxView refresh ---
    def _refresh_inbox_view(self):
        self._refresh_inbox_list()
        self._refresh_inbox_preview()

    def _refresh_inbox_list(self):
        self.inbox_list_col.controls.clear()
        items = self.state.inbox_items

        # Header: title + count + refresh
        self.inbox_list_col.controls.append(
            ft.Row(
                controls=[
                    ft.Icon(icon=ft.Icons.INBOX_OUTLINED, size=18,
                            color=ft.Colors.OUTLINE),
                    ft.Text("Inbox", size=18, font_family="Newsreader",
                            weight=ft.FontWeight.BOLD),
                    ft.Container(
                        padding=ft.Padding(left=6, top=1, right=6, bottom=1),
                        border_radius=8,
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                        content=ft.Text(str(len(items)), size=10,
                                        weight=ft.FontWeight.BOLD,
                                        color=ft.Colors.OUTLINE),
                    ),
                    ft.Container(expand=True),
                    ft.IconButton(
                        icon=ft.Icons.REFRESH, icon_size=16,
                        tooltip="Re-scan vault/00_Inbox/",
                        on_click=lambda e: self._on_inbox_refresh(),
                    ),
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )
        self.inbox_list_col.controls.append(ft.Container(height=4))

        if not items:
            self.inbox_list_col.controls.append(
                ft.Text("Inbox is empty.\nDrop .md files into vault/00_Inbox/.",
                        size=12, italic=True, color=ft.Colors.OUTLINE)
            )
            return

        for item in items:
            self.inbox_list_col.controls.append(self._build_inbox_list_row(item))

    def _build_inbox_list_row(self, item: InboxItem) -> ft.Control:
        is_selected = self.state.selected_inbox_path == item.path
        type_chip: list[ft.Control] = []
        if item.note_type:
            type_chip.append(
                ft.Container(
                    padding=ft.Padding(left=5, top=0, right=5, bottom=0),
                    border_radius=6,
                    bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                    content=ft.Text(item.note_type, size=9,
                                    weight=ft.FontWeight.BOLD,
                                    color=ft.Colors.OUTLINE),
                )
            )

        meta_row = ft.Row(
            controls=[
                ft.Text(_age_string(item.mtime), size=10, color=ft.Colors.OUTLINE),
                ft.Text("·", size=10, color=ft.Colors.OUTLINE),
                ft.Text(_human_size(item.size), size=10, color=ft.Colors.OUTLINE),
                *type_chip,
            ],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        title_color = (ft.Colors.PRIMARY if is_selected else ft.Colors.ON_SURFACE)
        return ft.Container(
            padding=ft.Padding(left=10, top=8, right=10, bottom=8),
            border_radius=6,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH if is_selected else None,
            content=ft.Column(
                spacing=2,
                controls=[
                    ft.Text(item.name, size=13, weight=ft.FontWeight.BOLD,
                            color=title_color, max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS),
                    meta_row,
                    ft.Text(item.summary or "(no body)", size=11,
                            color=ft.Colors.OUTLINE,
                            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                            italic=not item.summary),
                ],
            ),
            on_click=lambda e, path=item.path: self._on_inbox_select(path),
        )

    def _refresh_inbox_preview(self):
        self.inbox_preview_col.controls.clear()
        items = self.state.inbox_items
        if not items:
            self.inbox_preview_col.controls.append(
                ft.Container(
                    alignment=ft.Alignment.CENTER,
                    padding=40,
                    content=ft.Text(
                        "Nothing in the inbox.\n\n"
                        "Anything dropped into vault/00_Inbox/ shows here for triage.",
                        size=13, color=ft.Colors.OUTLINE,
                        text_align=ft.TextAlign.CENTER,
                    ),
                )
            )
            return

        # Drop stale selection if the file was deleted/renamed
        if self.state.selected_inbox_path and not any(
                i.path == self.state.selected_inbox_path for i in items):
            self.state.selected_inbox_path = None

        # No selection → empty preview hint (user explicitly closed it / never opened one)
        if not self.state.selected_inbox_path:
            self.inbox_preview_col.controls.append(
                ft.Container(
                    alignment=ft.Alignment.CENTER,
                    padding=40,
                    content=ft.Text(
                        "Select an item from the list to preview it here.",
                        size=13, color=ft.Colors.OUTLINE,
                        text_align=ft.TextAlign.CENTER,
                    ),
                )
            )
            return

        item = next(i for i in items if i.path == self.state.selected_inbox_path)

        # Read the file fresh — items list only stored summary
        try:
            post = frontmatter.load(item.path)
            body = (post.content or "").strip()
            metadata = dict(post.metadata)
        except Exception as ex:
            body = f"(could not read file: {ex})"
            metadata = {}

        # Header: name + meta + close
        self.inbox_preview_col.controls.append(
            ft.Row(
                controls=[
                    ft.Text(item.name, size=26, font_family="Newsreader",
                            weight=ft.FontWeight.BOLD, selectable=True,
                            expand=True),
                    ft.IconButton(
                        icon=ft.Icons.CLOSE, icon_size=18,
                        tooltip="Close preview",
                        on_click=lambda e: self._on_inbox_close_preview(),
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.START,
                spacing=8,
            )
        )
        self.inbox_preview_col.controls.append(
            ft.Row(
                controls=[
                    ft.Text(_age_string(item.mtime), size=11,
                            color=ft.Colors.OUTLINE),
                    ft.Text("·", size=11, color=ft.Colors.OUTLINE),
                    ft.Text(_human_size(item.size), size=11,
                            color=ft.Colors.OUTLINE),
                    ft.Text("·", size=11, color=ft.Colors.OUTLINE),
                    ft.Text(item.path, size=11,
                            font_family="JetBrains Mono",
                            color=ft.Colors.OUTLINE,
                            selectable=True,
                            max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            expand=True),
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )

        # Action row
        path = Path(item.path)
        self.inbox_preview_col.controls.append(
            ft.Row(
                controls=[
                    ft.FilledTonalButton(
                        "Open in editor", icon=ft.Icons.CODE,
                        on_click=lambda e, p=path: self._on_open_in_editor(p),
                    ),
                    ft.FilledTonalButton(
                        "Open in Obsidian", icon=ft.Icons.BOOK_OUTLINED,
                        on_click=lambda e, p=path: self._on_open_in_obsidian(p),
                    ),
                    ft.FilledTonalButton(
                        "Reveal", icon=ft.Icons.FOLDER_OPEN,
                        on_click=lambda e, p=path: self._on_reveal_in_files(p),
                    ),
                    ft.OutlinedButton(
                        "Move to →", icon=ft.Icons.DRIVE_FILE_MOVE_OUTLINED,
                        on_click=lambda e: self._toast(
                            "Move-to picker — coming next iteration"),
                    ),
                    ft.OutlinedButton(
                        "Archive", icon=ft.Icons.ARCHIVE_OUTLINED,
                        on_click=lambda e: self._toast(
                            "Archive — coming next iteration"),
                    ),
                    ft.OutlinedButton(
                        "Delete", icon=ft.Icons.DELETE_OUTLINE,
                        on_click=lambda e: self._toast(
                            "Delete (with confirm) — coming next iteration"),
                    ),
                    ft.FilledButton(
                        "Ask Workbench", icon=ft.Icons.AUTO_AWESOME,
                        tooltip="Hand this note to the agent to triage — "
                                "add your own context first",
                        on_click=lambda e, it=item: self._on_inbox_ask_workbench(it),
                    ),
                ],
                spacing=8,
                wrap=True,
            )
        )

        # Metadata (if any)
        if metadata:
            meta_lines = [
                ft.Text("FRONTMATTER", size=10, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.OUTLINE),
            ]
            for k, v in metadata.items():
                meta_lines.append(
                    ft.Text(f"{k}: {v}", size=11,
                            font_family="JetBrains Mono",
                            color=ft.Colors.ON_SURFACE_VARIANT,
                            selectable=True),
                )
            self.inbox_preview_col.controls.append(
                ft.Container(
                    padding=12,
                    border_radius=6,
                    bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                    content=ft.Column(controls=meta_lines, spacing=2),
                )
            )

        # Body
        self.inbox_preview_col.controls.append(
            ft.Container(
                padding=16,
                border_radius=8,
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                content=ft.Text(
                    body if body else "(empty body)",
                    size=14, font_family="Newsreader",
                    selectable=True,
                    italic=not body,
                ),
            )
        )

    # --- ResourcesView refresh (PeopleView lives in views/people.py) ---
    def _refresh_resources_view(self):
        self._refresh_browser_list(
            list_col=self.resources_list_col,
            entries=self.state.resources,
            label="Resources",
            icon=ft.Icons.LIBRARY_BOOKS_OUTLINED,
            selected_path=self.state.selected_resource_path,
            on_select=self._on_resources_select,
            on_refresh=self._on_resources_refresh,
            empty_hint="No notes in vault/30_Resources/.",
        )
        self._refresh_browser_preview(
            preview_col=self.resources_preview_col,
            entries=self.state.resources,
            selected_path_attr="selected_resource_path",
            on_close=self._on_resources_close_preview,
            empty_msg=("Nothing in Resources.\n\n"
                       "Anything in vault/30_Resources/ shows here."),
            no_selection_msg="Select a resource to preview it here.",
        )

    def _refresh_browser_list(
        self, *, list_col: ft.Column, entries: list[VaultEntry],
        label: str, icon, selected_path: Optional[str],
        on_select, on_refresh, empty_hint: str,
        total_count: Optional[int] = None,
    ):
        list_col.controls.clear()
        shown_count = len(entries)
        badge_count = total_count if total_count is not None else shown_count

        list_col.controls.append(
            ft.Row(
                controls=[
                    ft.Icon(icon=icon, size=18, color=ft.Colors.OUTLINE),
                    ft.Text(label, size=18, font_family="Newsreader",
                            weight=ft.FontWeight.BOLD),
                    ft.Container(
                        padding=ft.Padding(left=6, top=1, right=6, bottom=1),
                        border_radius=8,
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                        content=ft.Text(
                            (f"{shown_count}/{badge_count}"
                             if total_count is not None and shown_count != badge_count
                             else str(badge_count)),
                            size=10, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.OUTLINE),
                    ),
                    ft.Container(expand=True),
                    ft.IconButton(
                        icon=ft.Icons.REFRESH, icon_size=16,
                        tooltip="Re-scan vault",
                        on_click=lambda e: on_refresh(),
                    ),
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )
        list_col.controls.append(ft.Container(height=4))

        if not entries:
            list_col.controls.append(
                ft.Text(empty_hint, size=12, italic=True, color=ft.Colors.OUTLINE)
            )
            return

        for entry in entries:
            is_selected = selected_path == entry.path
            list_col.controls.append(
                self._build_browser_list_row(entry, is_selected, on_select)
            )

    def _build_browser_list_row(
        self, entry: VaultEntry, is_selected: bool, on_select,
    ) -> ft.Control:
        chips: list[ft.Control] = []
        if entry.subfolder:
            chips.append(
                ft.Container(
                    padding=ft.Padding(left=5, top=0, right=5, bottom=0),
                    border_radius=6,
                    bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                    content=ft.Text(entry.subfolder, size=9,
                                    weight=ft.FontWeight.BOLD,
                                    color=ft.Colors.OUTLINE),
                )
            )
        if entry.note_type:
            chips.append(
                ft.Container(
                    padding=ft.Padding(left=5, top=0, right=5, bottom=0),
                    border_radius=6,
                    bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                    content=ft.Text(entry.note_type, size=9,
                                    weight=ft.FontWeight.BOLD,
                                    color=ft.Colors.OUTLINE),
                )
            )

        meta_row = ft.Row(
            controls=[
                ft.Text(_human_size(entry.size), size=10, color=ft.Colors.OUTLINE),
                *chips,
            ],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        title_color = ft.Colors.PRIMARY if is_selected else ft.Colors.ON_SURFACE
        return ft.Container(
            padding=ft.Padding(left=10, top=8, right=10, bottom=8),
            border_radius=6,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH if is_selected else None,
            content=ft.Column(
                spacing=2,
                controls=[
                    ft.Text(entry.name, size=13, weight=ft.FontWeight.BOLD,
                            color=title_color, max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS),
                    meta_row,
                    ft.Text(entry.summary or "(no body)", size=11,
                            color=ft.Colors.OUTLINE,
                            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                            italic=not entry.summary),
                ],
            ),
            on_click=lambda e, path=entry.path: on_select(path),
        )

    def _refresh_browser_preview(
        self, *, preview_col: ft.Column, entries: list[VaultEntry],
        selected_path_attr: str, on_close, empty_msg: str,
        no_selection_msg: str,
    ):
        preview_col.controls.clear()
        if not entries:
            preview_col.controls.append(
                ft.Container(
                    alignment=ft.Alignment.CENTER,
                    padding=40,
                    content=ft.Text(empty_msg, size=13, color=ft.Colors.OUTLINE,
                                    text_align=ft.TextAlign.CENTER),
                )
            )
            return

        selected_path = getattr(self.state, selected_path_attr)
        # Drop stale selection if the file was deleted/renamed
        if selected_path and not any(e.path == selected_path for e in entries):
            setattr(self.state, selected_path_attr, None)
            selected_path = None

        if not selected_path:
            preview_col.controls.append(
                ft.Container(
                    alignment=ft.Alignment.CENTER,
                    padding=40,
                    content=ft.Text(no_selection_msg, size=13, color=ft.Colors.OUTLINE,
                                    text_align=ft.TextAlign.CENTER),
                )
            )
            return

        entry = next(e for e in entries if e.path == selected_path)

        try:
            post = frontmatter.load(entry.path)
            body = (post.content or "").strip()
            metadata = dict(post.metadata)
        except Exception as ex:
            body = f"(could not read file: {ex})"
            metadata = {}

        preview_col.controls.append(
            ft.Row(
                controls=[
                    ft.Text(entry.name, size=26, font_family="Newsreader",
                            weight=ft.FontWeight.BOLD, selectable=True,
                            expand=True),
                    ft.IconButton(
                        icon=ft.Icons.CLOSE, icon_size=18,
                        tooltip="Close preview",
                        on_click=lambda e: on_close(),
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.START,
                spacing=8,
            )
        )
        preview_col.controls.append(
            ft.Row(
                controls=[
                    ft.Text(_human_size(entry.size), size=11,
                            color=ft.Colors.OUTLINE),
                    ft.Text("·", size=11, color=ft.Colors.OUTLINE),
                    ft.Text(entry.path, size=11,
                            font_family="JetBrains Mono",
                            color=ft.Colors.OUTLINE,
                            selectable=True,
                            max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            expand=True),
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )

        path = Path(entry.path)
        preview_col.controls.append(
            ft.Row(
                controls=[
                    ft.FilledTonalButton(
                        "Open in editor", icon=ft.Icons.CODE,
                        on_click=lambda e, p=path: self._on_open_in_editor(p),
                    ),
                    ft.FilledTonalButton(
                        "Open in Obsidian", icon=ft.Icons.BOOK_OUTLINED,
                        on_click=lambda e, p=path: self._on_open_in_obsidian(p),
                    ),
                    ft.FilledTonalButton(
                        "Reveal", icon=ft.Icons.FOLDER_OPEN,
                        on_click=lambda e, p=path: self._on_reveal_in_files(p),
                    ),
                ],
                spacing=8,
                wrap=True,
            )
        )

        if metadata:
            meta_lines = [
                ft.Text("FRONTMATTER", size=10, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.OUTLINE),
            ]
            for k, v in metadata.items():
                meta_lines.append(
                    ft.Text(f"{k}: {v}", size=11,
                            font_family="JetBrains Mono",
                            color=ft.Colors.ON_SURFACE_VARIANT,
                            selectable=True),
                )
            preview_col.controls.append(
                ft.Container(
                    padding=12,
                    border_radius=6,
                    bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                    content=ft.Column(controls=meta_lines, spacing=2),
                )
            )

        preview_col.controls.append(
            ft.Container(
                padding=16,
                border_radius=8,
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                content=ft.Text(
                    body if body else "(empty body)",
                    size=14, font_family="Newsreader",
                    selectable=True,
                    italic=not body,
                ),
            )
        )

    @staticmethod
    def _probe_glance(p: Project) -> tuple[str, bool]:
        """Build the probe subtitle ('last probe Nd ago · 0 bites') and a stale
        flag. Stale = days-since-last exceeds the cadence (default 7). Missing
        probe log → flagged stale with a 'no log yet' nudge."""
        probe = p.probe or {}
        cadence = probe.get("cadence_days")
        cadence = cadence if isinstance(cadence, int) and cadence > 0 else 7
        bites_raw = probe.get("bites", 0)
        bites = bites_raw if isinstance(bites_raw, int) else 0

        last_raw = probe.get("last")
        last_d: Optional[date] = None
        if isinstance(last_raw, date):
            last_d = last_raw
        elif isinstance(last_raw, str) and last_raw.strip():
            try:
                last_d = date.fromisoformat(last_raw.strip()[:10])
            except ValueError:
                last_d = None

        bite_bit = f"{bites} bite{'s' if bites != 1 else ''}"
        if last_d is None:
            return (f"no probe log yet  ·  {bite_bit}", True)

        days = (date.today() - last_d).days
        stale = days > cadence
        if days <= 0:
            age = "probed today"
        elif days == 1:
            age = "1d since last probe"
        else:
            age = f"{days}d since last probe"
        prefix = "⚠ " if stale else ""
        return (f"{prefix}{age}  ·  {bite_bit}", stale)

    def _render_probe_row(self, p: Project) -> ft.Control:
        glance, stale = self._probe_glance(p)
        accent = ft.Colors.TERTIARY if not stale else ft.Colors.ERROR
        threads_n = len(p.threads)
        meta = glance
        if threads_n:
            meta += f"  ·  {threads_n} thread{'s' if threads_n != 1 else ''}"

        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.SENSORS, size=16, color=accent),
                    ft.Column(
                        controls=[
                            ft.Text(p.name, size=14, weight=ft.FontWeight.W_500),
                            ft.Text(meta, size=11, color=accent if stale else ft.Colors.OUTLINE),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    ft.IconButton(
                        icon=ft.Icons.ARROW_FORWARD_IOS,
                        icon_size=14,
                        tooltip="Open project",
                        on_click=lambda e, pid=p.id: self._activate_project(pid),
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding(left=14, top=10, right=14, bottom=10),
            margin=ft.Margin(left=0, top=0, right=0, bottom=6),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
            on_click=lambda e, pid=p.id: self._activate_project(pid),
            border_radius=10,
        )

    def _render_project_sticky(self, p: Project) -> ft.Control:
        """Project rendered as a classic-Mac Stickies note for board views.
        Sidebar stays compact rows; this is the spacious 'board' renderer.
        Name wraps (never truncates). See _System/Methods/Workbench UI.md."""
        status_color = STATUS_COLORS.get(p.status, ft.Colors.OUTLINE)
        inner = ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        ft.Container(width=8, height=8, border_radius=4,
                                     bgcolor=status_color),
                        ft.Text(p.status, size=10, color=PLATINUM["text2"]),
                    ],
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Text(p.name, size=13, weight=ft.FontWeight.BOLD,
                        color=PLATINUM["text"]),  # wraps; no max_lines/ellipsis
                ft.Text(f"{len(p.threads)} threads · ~{p.context_tokens // 1000}k ctx",
                        size=10, color=PLATINUM["text2"]),
            ],
            spacing=6,
            tight=True,
        )
        note = _sticky(inner, width=165)
        note.on_click = lambda e, pid=p.id: self._activate_project(pid)
        return note

    def _sticky_grid(self, projects: list[Project]) -> ft.Control:
        """A wrapping board of project Stickies — the board-view list renderer."""
        return ft.Row(
            controls=[self._render_project_sticky(p) for p in projects],
            wrap=True, spacing=12, run_spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )

    # --- ReviewsView: the Reflect limb of the Kaizen loop --------------------
    # Two stacked surfaces: a due-board (Persist/Pause/Pivot queue, bucketed by
    # `review:` date) + a GitHub-style reflections grid. See Workbench.md v0.7.

    def _build_reviews_view_body(self) -> ft.Control:
        self.reviews_content = ft.Column(spacing=24, scroll=ft.ScrollMode.AUTO, expand=True)
        return ft.Container(
            padding=ft.Padding(left=32, top=28, right=32, bottom=32),
            content=self.reviews_content,
            expand=True,
        )

    def _review_eligible(self) -> list[Project]:
        """Projects that warrant a reflection nag: running experiments only.
        Excludes done (finished), pause (intentionally frozen), idea (not started)."""
        return [p for p in self.state.projects
                if p.status in ("active", "persist", "pivot")]

    def _project_last_edit(self, p: Project) -> Optional[date]:
        """Most-recent mtime across the project's notes (drives stale detection)."""
        latest: Optional[float] = None
        for _, path in _project_context_paths(p):
            try:
                m = path.stat().st_mtime
            except OSError:
                continue
            if latest is None or m > latest:
                latest = m
        return date.fromtimestamp(latest) if latest is not None else None

    @staticmethod
    def _days_glance(days: int) -> str:
        if days < 0:
            return f"{-days}d overdue"
        if days == 0:
            return "due today"
        if days == 1:
            return "due tomorrow"
        return f"in {days}d"

    def _compute_review_buckets(self) -> dict:
        """Bucket eligible projects into mutually-exclusive due columns. Priority:
        no review date → Needs attention; else past-due wins; else stale (no edit
        30d+, despite a future date) → Needs attention; else by days-until."""
        today = date.today()
        n1, n2 = self._review_n1, self._review_n2
        cols: dict[str, list] = {"needs": [], "past": [], "today": [], "n1": [], "n2": []}
        for p in self._review_eligible():
            if p.review is None:
                cols["needs"].append((p, "no review date"))
                continue
            days = (p.review - today).days
            if days < 0:
                cols["past"].append((p, self._days_glance(days)))
                continue
            le = self._project_last_edit(p)
            stale = (today - le).days if le else None
            if stale is not None and stale >= 30:
                cols["needs"].append((p, f"stale · {stale}d untouched"))
                continue
            if days == 0:
                cols["today"].append((p, self._days_glance(days)))
            elif days <= n1:
                cols["n1"].append((p, self._days_glance(days)))
            elif days <= n2:
                cols["n2"].append((p, self._days_glance(days)))
            # beyond n2 → not shown (not due soon enough to nag)
        return cols

    def _refresh_reviews_view(self):
        self.reviews_content.controls.clear()
        # Header
        self.reviews_content.controls.append(
            ft.Row(
                controls=[
                    ft.Icon(ft.Icons.RATE_REVIEW_OUTLINED, size=28,
                            color=PLATINUM["text2"]),
                    ft.Text("Reviews", size=28, font_family="Newsreader",
                            weight=ft.FontWeight.BOLD),
                    ft.Container(expand=True),
                    ft.Text("Reflect → Persist · Pause · Pivot", size=12,
                            italic=True, color=ft.Colors.OUTLINE),
                ],
                spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )
        self.reviews_content.controls.append(self._build_due_board())
        self.reviews_content.controls.append(ft.Divider(height=1))
        self.reviews_content.controls.append(self._build_reflections_section())

    # --- Due board -----------------------------------------------------------
    def _build_due_board(self) -> ft.Control:
        buckets = self._compute_review_buckets()
        n1, n2 = self._review_n1, self._review_n2
        specs = [
            ("needs", "Needs attention", ft.Colors.ERROR),
            ("past", "Past due", ft.Colors.ERROR),
            ("today", "Today", ft.Colors.PRIMARY),
            ("n1", f"Next {n1}d", ft.Colors.TERTIARY),
            ("n2", f"Next {n2}d", ft.Colors.OUTLINE),
        ]
        total = sum(len(v) for v in buckets.values())
        header_controls = [
            ft.Text("DUE BOARD", size=11, weight=ft.FontWeight.BOLD,
                    color=ft.Colors.OUTLINE),
            ft.Container(expand=True),
        ]
        if total:
            header_controls.append(self._review_window_control())
        body: ft.Control
        if total == 0:
            body = self._all_caught_up()
        else:
            columns = [self._build_due_column(key, label, accent, buckets[key])
                       for key, label, accent in specs]
            body = ft.Row(controls=columns, spacing=12,
                          vertical_alignment=ft.CrossAxisAlignment.START,
                          scroll=ft.ScrollMode.AUTO)
        return ft.Column(
            controls=[
                ft.Row(header_controls,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                body,
            ],
            spacing=10,
        )

    def _all_caught_up(self) -> ft.Control:
        """Shown when no project is due / needs attention — the reward for an
        empty board. Names the next upcoming review (if any) so the calm is
        informative, not just blank."""
        # Soonest future review across eligible projects, for the subtitle.
        today = date.today()
        upcoming = sorted(
            ((p.review, p) for p in self._review_eligible()
             if p.review is not None and (p.review - today).days >= 0),
            key=lambda t: t[0],
        )
        if upcoming:
            d, p = upcoming[0]
            days = (d - today).days
            when = "today" if days == 0 else (f"in {days} day{'s' if days != 1 else ''}")
            sub = f"Next up: {p.name} · {when} ({d.isoformat()})"
        else:
            sub = "Nothing scheduled. Set a review date with Persist/Pause/Pivot on any project."
        card = _raised(
            ft.Column(
                controls=[
                    ft.Icon(ft.Icons.TASK_ALT, size=44, color=ft.Colors.TERTIARY),
                    ft.Text("All caught up", size=22, font_family="Newsreader",
                            weight=ft.FontWeight.BOLD, color=PLATINUM["text"]),
                    ft.Text("No reviews due — nice.", size=13, color=PLATINUM["text2"]),
                    ft.Text(sub, size=11, color=ft.Colors.OUTLINE,
                            text_align=ft.TextAlign.CENTER),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=6, tight=True,
            ),
            fill=PLATINUM["panel"],
        )
        card.padding = ft.Padding(left=24, top=32, right=24, bottom=32)
        card.alignment = ft.Alignment.CENTER
        return ft.Row([card], alignment=ft.MainAxisAlignment.CENTER)

    def _review_window_control(self) -> ft.Control:
        """The two editable day-boundaries, lifted out of the column headers into
        a compact toolbar so the headers stay uniform (label + count, aligned)."""
        def field(which: str) -> ft.Control:
            return ft.TextField(
                value=str(getattr(self, f"_review_{which}")),
                width=46, height=36, text_size=12, dense=True,
                text_align=ft.TextAlign.CENTER,
                content_padding=ft.Padding(left=4, top=2, right=4, bottom=2),
                keyboard_type=ft.KeyboardType.NUMBER,
                tooltip="Days window for this column — Enter to apply",
                on_submit=lambda e, w=which: self._on_review_boundary(w, e),
            )
        def lbl(t: str) -> ft.Control:
            return ft.Text(t, size=11, color=PLATINUM["text2"])
        return ft.Row(
            controls=[lbl("window:  next"), field("n1"), lbl("d  ·  next"),
                      field("n2"), lbl("d")],
            spacing=5, vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
        )

    def _build_due_column(self, key: str, label: str, accent,
                          entries: list) -> ft.Control:
        header = ft.Row(
            controls=[
                ft.Text(label, size=11, weight=ft.FontWeight.BOLD, color=accent),
                ft.Container(expand=True),
                ft.Container(
                    padding=ft.Padding(left=6, top=1, right=6, bottom=1),
                    border_radius=8, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                    content=ft.Text(str(len(entries)), size=10,
                                    weight=ft.FontWeight.BOLD, color=accent),
                ),
            ],
            spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        items: list[ft.Control] = [header]
        if not entries:
            items.append(ft.Text("(none)", size=11, italic=True,
                                 color=ft.Colors.OUTLINE))
        urgent = key in ("needs", "past")
        for p, glance in entries:
            items.append(self._render_due_card(p, glance, urgent))
        # STRETCH so the stickies fill the column width (uniform board).
        col = _recessed(
            ft.Column(controls=items, spacing=8,
                      horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            fill=PLATINUM["panel"],
        )
        col.width = 232
        col.padding = ft.Padding(left=10, top=10, right=10, bottom=10)
        return col

    def _render_due_card(self, p: Project, glance: str, urgent: bool) -> ft.Control:
        """A project on the due board, rendered as a Stickies note (visual
        continuity with the Home/area boards). The note is loose paper (drop
        shadow); the column it sits in is the beveled tray. Name → open project;
        the P/P/P buttons act in place."""
        status_color = STATUS_COLORS.get(p.status, ft.Colors.OUTLINE)
        glance_color = ft.Colors.ERROR if urgent else PLATINUM["text2"]
        name = ft.Container(
            content=ft.Text(p.name, size=13, weight=ft.FontWeight.BOLD,
                            color=PLATINUM["text"]),  # wraps; no truncation
            ink=True, tooltip="Open project", expand=True,
            on_click=lambda e, pid=p.id: self._activate_project(pid),
        )
        ppp = ft.Row(
            controls=[
                self._ppp_button("Persist", "persist", p),
                self._ppp_button("Pause", "pause", p),
                self._ppp_button("Pivot", "pivot", p),
            ],
            spacing=4,
        )
        inner = ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        ft.Container(width=8, height=8, border_radius=4,
                                     bgcolor=status_color),
                        name,
                    ],
                    spacing=6, vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                ft.Text(f"{('⚠ ' if urgent else '')}{glance}"
                        f"{('  ·  ' + p.area) if p.area else ''}",
                        size=11, color=glance_color),
                ppp,
            ],
            spacing=8, tight=True,
        )
        return _sticky(inner)  # width=None → fills the (STRETCH) column

    def _ppp_button(self, label: str, action: str, p: Project) -> ft.Control:
        return ft.Container(
            padding=ft.Padding(left=8, top=4, right=8, bottom=4),
            content=ft.Text(label, size=11, weight=ft.FontWeight.W_500,
                            color=PLATINUM["text"]),
            border=_bevel(raised=True), border_radius=2, bgcolor=PLATINUM["face"],
            ink=True, tooltip=f"{label} — set status + next review date",
            on_click=lambda e, a=action, pr=p: self._on_ppp(pr, a),
        )

    def _on_review_boundary(self, which: str, e):
        try:
            v = int((e.control.value or "").strip())
        except ValueError:
            self._toast("Enter a whole number of days")
            return
        v = max(1, min(365, v))
        setattr(self, f"_review_{which}", v)
        # Keep n1 < n2 so the buckets don't overlap/invert.
        if self._review_n1 >= self._review_n2:
            if which == "n1":
                self._review_n2 = self._review_n1 + 1
            else:
                self._review_n1 = max(1, self._review_n2 - 1)
        # Persist so the chosen lenses survive a restart.
        self.state.config.review_window_n1 = self._review_n1
        self.state.config.review_window_n2 = self._review_n2
        try:
            save_config(self.state.config)
        except Exception:
            pass
        self._refresh_reviews_view()
        self.page.update()

    # --- Persist / Pause / Pivot writeback -----------------------------------
    def _on_ppp(self, p: Project, action: str):
        """Open a confirm dialog with the default next-review date pre-filled
        (editable). Persist → +14d · Pivot → +7d · Pause → cleared (frozen)."""
        today = date.today()
        if action == "persist":
            nxt = date.fromordinal(today.toordinal() + 14)
        elif action == "pivot":
            nxt = date.fromordinal(today.toordinal() + 7)
        else:
            nxt = None

        date_field = ft.TextField(
            label="Next review date (YYYY-MM-DD)",
            value=nxt.isoformat() if nxt else "",
            disabled=(action == "pause"), dense=True, width=240,
            hint_text="blank = no date",
        )
        verb = {"persist": "Persist", "pause": "Pause", "pivot": "Pivot"}[action]
        blurb = {
            "persist": "Extend the experiment — keep running it.",
            "pause": "Freeze it (no capacity this season). Review date cleared; stops nagging.",
            "pivot": "Adjust scope/time/friction and try a variation — shorter loop.",
        }[action]
        def commit(_e):
            self.page.pop_dialog()
            new_review: Optional[date] = None
            if action != "pause":
                raw = (date_field.value or "").strip()
                if raw:
                    new_review = _coerce_date(raw)
                    if new_review is None:
                        self._toast("Couldn't parse that date — use YYYY-MM-DD")
                        return
            self._apply_ppp(p, action, new_review)

        dlg = ft.AlertDialog(
            title=ft.Text(f"{verb} — {p.name}"),
            content=ft.Container(
                width=420,
                content=ft.Column(
                    [ft.Text(blurb, size=12, color=ft.Colors.OUTLINE),
                     ft.Text(f"status:  {p.status}  →  {action}", size=12,
                             weight=ft.FontWeight.W_500),
                     date_field],
                    spacing=12, tight=True,
                ),
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _e: self.page.pop_dialog()),
                ft.FilledButton(verb, on_click=commit),
            ],
        )
        self.page.show_dialog(dlg)

    def _apply_ppp(self, p: Project, action: str, new_review: Optional[date]):
        ctx = _project_context_paths(p)
        if not ctx:
            self._toast("Couldn't resolve the project's main note")
            return
        main_path = ctx[0][1]
        old_status = p.status
        try:
            write_status_review(main_path, action, new_review, old_status)
        except Exception as ex:
            self._toast(f"Write failed: {ex}")
            return
        # Reflect on the live Project so the board updates without a full reload.
        p.status = action
        p.review = new_review
        self.refresh()
        when = new_review.isoformat() if new_review else "cleared"
        self._toast(f"{p.name}: {old_status}→{action} · review {when}")

    # --- Reflections grid ----------------------------------------------------
    def _build_reflections_section(self) -> ft.Control:
        reflections = load_reflections(VAULT_PATH)
        by_week = {r.week: r for r in reflections}
        this_week = _iso_week(date.today())
        have_this_week = this_week in by_week

        cta = ft.Container(
            padding=ft.Padding(left=12, top=8, right=12, bottom=8),
            border=_bevel(raised=True), border_radius=2, bgcolor=PLATINUM["face"],
            ink=True,
            content=ft.Row(
                [ft.Icon(ft.Icons.EDIT_NOTE, size=16, color=PLATINUM["text"]),
                 ft.Text(("Open" if have_this_week else "Start")
                         + f" this week's review · {this_week}",
                         size=12, weight=ft.FontWeight.W_500, color=PLATINUM["text"])],
                spacing=6, tight=True,
            ),
            on_click=lambda e, w=this_week: self._on_reflection_cell(w),
        )

        header = ft.Row(
            controls=[
                ft.Text("REFLECTIONS", size=11, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.OUTLINE),
                ft.Container(expand=True),
                cta,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # GitHub-style grid: one row per ISO year, a cell per week 1..53, grouped
        # into quarters (Q1 1-13 · Q2 14-26 · Q3 27-39 · Q4 40-53) with a gap +
        # axis labels so a glance maps to roughly when in the year.
        QUARTERS = [("Q1", 1, 13), ("Q2", 14, 26), ("Q3", 27, 39), ("Q4", 40, 53)]
        CELL, GAP, QGAP = 18, 4, 16  # cell px · between-cell gap · between-quarter gap
        QWIDTH = 13 * CELL + 12 * GAP  # 13 cells + 12 gaps
        YLABEL_W = 46

        axis_cells: list[ft.Control] = [ft.Container(width=YLABEL_W)]
        for i, (qlabel, _a, _b) in enumerate(QUARTERS):
            axis_cells.append(ft.Container(
                width=QWIDTH, content=ft.Text(qlabel, size=11,
                                              weight=ft.FontWeight.W_500,
                                              color=PLATINUM["text2"])))
            if i < len(QUARTERS) - 1:
                axis_cells.append(ft.Container(width=QGAP))
        axis_row = ft.Row(axis_cells, spacing=GAP,
                          vertical_alignment=ft.CrossAxisAlignment.CENTER)

        years = self._reflection_years(by_week)
        grid_rows: list[ft.Control] = [axis_row]
        for y in years:
            row_cells: list[ft.Control] = [
                ft.Container(width=YLABEL_W, content=ft.Text(str(y), size=12,
                             color=PLATINUM["text2"])),
            ]
            for i, (_q, a, b) in enumerate(QUARTERS):
                qcells = [
                    self._reflection_cell(f"{y}-W{w:02d}", by_week.get(f"{y}-W{w:02d}"),
                                          is_current=(f"{y}-W{w:02d}" == this_week))
                    for w in range(a, b + 1)
                ]
                row_cells.append(ft.Row(qcells, spacing=GAP, wrap=False,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER))
                if i < len(QUARTERS) - 1:
                    row_cells.append(ft.Container(width=QGAP))
            grid_rows.append(ft.Row(row_cells, spacing=GAP, wrap=False,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER))

        # Give the horizontal scroll its own band with headroom so the scrollbar
        # sits *below* the cells instead of overlapping them on hover.
        row_h = CELL + 10  # cell + breathing room per grid row
        grid_height = len(grid_rows) * row_h + 22  # + scrollbar band
        grid_scroll = ft.Container(
            height=grid_height,
            padding=ft.Padding(left=0, top=0, right=0, bottom=10),
            content=ft.Row([ft.Column(grid_rows, spacing=6)],
                           scroll=ft.ScrollMode.AUTO),
        )

        legend = ft.Row(
            controls=[
                self._legend_swatch("none", "none"),
                self._legend_swatch("started", "started"),
                self._legend_swatch("done", "done"),
                ft.Container(width=8),
                ft.Text("click a week to view / start its reflection",
                        size=10, italic=True, color=ft.Colors.OUTLINE),
            ],
            spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        return ft.Column(
            controls=[
                header,
                grid_scroll,
                legend,
            ],
            spacing=12,
        )

    def _reflection_years(self, by_week: dict) -> list[int]:
        years = {date.today().isocalendar()[0]}
        for wk in by_week:
            m = re.match(r"^(\d{4})-W\d{1,2}$", wk)
            if m:
                years.add(int(m.group(1)))
        return sorted(years)

    def _reflection_cell(self, week: str, refl, is_current: bool) -> ft.Control:
        if refl is None:
            fill, tip = PLATINUM["face"], f"{week} · no reflection"
        elif refl.filled:
            fill, tip = ft.Colors.PRIMARY, f"{week} · done"
        else:
            fill, tip = ft.Colors.PRIMARY_CONTAINER, f"{week} · started"
        border = (ft.Border(top=ft.BorderSide(2, PLATINUM["accent"]),
                            left=ft.BorderSide(2, PLATINUM["accent"]),
                            right=ft.BorderSide(2, PLATINUM["accent"]),
                            bottom=ft.BorderSide(2, PLATINUM["accent"]))
                  if is_current else _bevel(raised=False))
        return ft.Container(
            width=18, height=18, border_radius=3, bgcolor=fill, border=border,
            tooltip=tip, ink=True,
            on_click=lambda e, w=week: self._on_reflection_cell(w),
        )

    def _legend_swatch(self, kind: str, label: str) -> ft.Control:
        fill = {"none": PLATINUM["face"], "started": ft.Colors.PRIMARY_CONTAINER,
                "done": ft.Colors.PRIMARY}[kind]
        return ft.Row(
            [ft.Container(width=11, height=11, border_radius=2, bgcolor=fill,
                          border=_bevel(raised=False)),
             ft.Text(label, size=10, color=PLATINUM["text2"])],
            spacing=4, tight=True,
        )

    def _on_reflection_cell(self, week: str):
        """Open a week: existing note → preview + open/continue actions;
        missing → a pre-text box that seeds a 'Review <week>' thread."""
        reflections = load_reflections(VAULT_PATH)
        refl = next((r for r in reflections if r.week == week), None)

        if refl is not None:
            try:
                body = Path(refl.path).read_text(encoding="utf-8")
            except Exception as ex:
                body = f"(could not read: {ex})"
            if len(body) > 6000:
                body = body[:6000] + "\n…(truncated)"
            path = Path(refl.path)
            dlg = ft.AlertDialog(
                title=ft.Text(f"Reflection · {week}"
                              + ("  (started)" if not refl.filled else "")),
                content=ft.Container(
                    width=640, height=440,
                    content=ft.Column(
                        [ft.Text(body, size=12, selectable=True,
                                 font_family="JetBrains Mono")],
                        scroll=ft.ScrollMode.AUTO, expand=True,
                    ),
                ),
                actions=[
                    ft.TextButton("Open in Obsidian",
                                  on_click=lambda _e, pa=path: (self.page.pop_dialog(),
                                                                self._on_open_in_obsidian(pa))),
                    ft.TextButton("Open in editor",
                                  on_click=lambda _e, pa=path: (self.page.pop_dialog(),
                                                                self._on_open_in_editor(pa))),
                    ft.FilledButton("Continue in a thread",
                                    on_click=lambda _e, w=week: (self.page.pop_dialog(),
                                                                 self._on_start_review(w))),
                ],
            )
            self.page.show_dialog(dlg)
        else:
            self._on_start_review(week)

    def _on_start_review(self, week: str):
        tips_field = ft.TextField(
            multiline=True, min_lines=3, max_lines=8, autofocus=True, text_size=13,
            hint_text=("What's on your mind for this week's reflection? "
                       "(energized / drained / what worked) — or leave blank "
                       "and let the agent walk you through it."),
        )
        if self.state.config.force_mock:
            hint = ft.Text(
                "Heads up: force-mock is ON (Settings) — the agent will reply with "
                "mock text and won't really write the note.",
                size=11, color=ft.Colors.TERTIARY)
        else:
            hint = ft.Text(
                "The agent works in trust mode: it drafts the reflection note + "
                "surfaces your due reviews, then you decide Persist/Pause/Pivot.",
                size=11, color=ft.Colors.OUTLINE)

        def go(_e):
            self.page.pop_dialog()
            self._start_weekly_review(week, tips_field.value or "")

        dlg = ft.AlertDialog(
            title=ft.Text(f"Start review · {week}"),
            content=ft.Container(
                width=600,
                content=ft.Column([tips_field, hint], spacing=10, tight=True),
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _e: self.page.pop_dialog()),
                ft.FilledButton("Start", icon=ft.Icons.AUTO_AWESOME, on_click=go),
            ],
        )
        self.page.show_dialog(dlg)

    def _start_weekly_review(self, week: str, tips: str):
        """Seed a scratch 'Review <week>' thread framed with the weekly-review
        steps + the currently-due projects, open it, and dispatch. Mirrors
        `_start_inbox_triage`; the agent uses file tools (trust mode)."""
        buckets = self._compute_review_buckets()
        due_lines: list[str] = []
        for key in ("needs", "past", "today"):
            for p, glance in buckets[key]:
                rv = p.review.isoformat() if p.review else "—"
                due_lines.append(f"  • {p.name} (status: {p.status}, review: {rv}) — {glance}")
        due_block = "\n".join(due_lines) if due_lines else "  (nothing due right now)"

        tips = tips.strip()
        tips_block = tips if tips else "(no notes yet — walk me through it.)"

        seed = (
            f"Help me run my weekly Kaizen reflection for {week}. Work in trust "
            "mode using your file tools.\n\n"
            "Steps (from the weekly-review method):\n"
            f"1. The reflection note lives at `_System/Reflections/{week}.md`. If it "
            "doesn't exist, create it by copying `_System/Templates/Weekly "
            "Reflection.md` and filling `week:`/`date:` + the heading. NEVER "
            "overwrite an existing reflection — append/extend it instead.\n"
            "2. Help me fill Observe (energized / drained / worked) from what I tell "
            "you below.\n"
            "3. For each project whose review is due, help me decide "
            "Persist / Pause / Pivot — then update that project's `status:` and push "
            "its `review:` date forward in its frontmatter.\n"
            "4. Capture next week's 2-minute micro-commitment(s).\n"
            "Don't decide Persist/Pause/Pivot for me — surface the data, I choose.\n\n"
            f"Projects due / needing review right now:\n{due_block}\n\n"
            f"What's on my mind:\n{tips_block}"
        )

        new_id = f"t_{uuid.uuid4().hex[:8]}"
        thread = Thread(id=new_id, name=f"Review {week}", project_id="")
        user_id = f"n_{uuid.uuid4().hex[:6]}"
        thread.turns[user_id] = Turn(
            id=user_id, parent_id=None, speaker="user", text=seed)
        thread.root_id = user_id
        team_id = f"n_{uuid.uuid4().hex[:6]}"
        team_turn = Turn(id=team_id, parent_id=user_id, speaker="team", text="")
        thread.turns[team_id] = team_turn
        thread.current_leaf_id = team_id

        self.state.scratch_threads.append(thread)
        self._open_or_focus(OpenTab(kind="chat", ref_id=new_id))
        self._save_thread(thread)
        self._dispatch_single(thread, team_turn)

    # --- TasksView refresh ---
    def _refresh_tasks_view(self):
        self.tasks_content.controls.clear()
        if not self.state.active_tab or self.state.active_tab.kind != "tasks":
            return
        p = self.state.get_project(self.state.active_tab.ref_id)
        if not p:
            self.tasks_content.controls.append(
                ft.Text("Project not found.", color=ft.Colors.OUTLINE))
            return

        tasks = _scan_tasks(p, VAULT_PATH)
        open_tasks = [t for t in tasks if not t.checked]
        done_tasks = [t for t in tasks if t.checked]

        # Header
        self.tasks_content.controls.append(
            ft.Row(
                controls=[
                    ft.Icon(icon=ft.Icons.CHECKLIST, size=28, color=ft.Colors.OUTLINE),
                    ft.Text(f"Tasks · {p.name}", size=28,
                            font_family="Newsreader", weight=ft.FontWeight.BOLD),
                    ft.Container(expand=True),
                    ft.Text(f"{len(open_tasks)} open · {len(done_tasks)} done",
                            size=12, color=ft.Colors.OUTLINE),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )
        self.tasks_content.controls.append(
            ft.Text("Parsed from `- [ ]` / `- [x]` checkboxes in the project's .md files. "
                    "Click any task to toggle — writes back to the source file.",
                    size=11, italic=True, color=ft.Colors.OUTLINE)
        )

        # Two-column kanban: Open | Done
        open_col = self._build_task_column("OPEN", open_tasks, ft.Colors.PRIMARY)
        done_col = self._build_task_column("DONE", done_tasks, ft.Colors.TERTIARY)
        self.tasks_content.controls.append(
            ft.Row(
                controls=[open_col, done_col],
                spacing=16,
                vertical_alignment=ft.CrossAxisAlignment.START,
                expand=True,
            )
        )

    def _build_task_column(self, label: str, tasks: list[Task],
                           accent: object) -> ft.Control:
        items: list[ft.Control] = [
            ft.Row(
                controls=[
                    ft.Text(label, size=11, weight=ft.FontWeight.BOLD, color=accent),
                    ft.Container(
                        padding=ft.Padding(left=6, top=1, right=6, bottom=1),
                        border_radius=8,
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                        content=ft.Text(str(len(tasks)), size=10,
                                        weight=ft.FontWeight.BOLD, color=accent),
                    ),
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        ]
        if not tasks:
            items.append(ft.Text("(none)", size=11, italic=True,
                                 color=ft.Colors.OUTLINE))
        for t in tasks:
            items.append(self._render_task_item(t))
        return ft.Container(
            expand=True,
            padding=12,
            border_radius=8,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=_all_border(),
            content=ft.Column(controls=items, spacing=8),
        )

    def _render_task_item(self, t: Task) -> ft.Control:
        src_label = Path(t.source_path).name
        return ft.Container(
            padding=ft.Padding(left=10, top=8, right=10, bottom=8),
            border_radius=6,
            bgcolor=ft.Colors.SURFACE_CONTAINER,
            content=ft.Row(
                controls=[
                    ft.Icon(
                        icon=ft.Icons.CHECK_BOX if t.checked else ft.Icons.CHECK_BOX_OUTLINE_BLANK,
                        size=18,
                        color=ft.Colors.TERTIARY if t.checked else ft.Colors.OUTLINE,
                    ),
                    ft.Column(
                        controls=[
                            ft.Text(t.text, size=13,
                                    color=(ft.Colors.OUTLINE if t.checked
                                           else ft.Colors.ON_SURFACE)),
                            ft.Text(f"{src_label}:{t.line_number}", size=10,
                                    color=ft.Colors.OUTLINE, italic=True),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
            on_click=lambda e, task=t: self._on_toggle_task(task),
        )

    def _on_toggle_task(self, task: Task):
        if toggle_task(task):
            self._refresh_tasks_view()
            # Also refresh the overview if it's the active or visible tab
            self._refresh_overview_view()
            self.page.update()

    # --- SettingsView refresh ---
    def _refresh_settings_view(self):
        cfg = self.state.config  # used by help-text f-strings below
        self.settings_content.controls.clear()
        self.settings_content.controls.extend([
            ft.Row(
                controls=[
                    ft.Icon(icon=ft.Icons.SETTINGS_OUTLINED, size=32,
                            color=ft.Colors.OUTLINE),
                    self._title_text("Settings"),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            ft.Text(f"Config file: {CONFIG_PATH}", size=11,
                    font_family="JetBrains Mono",
                    color=ft.Colors.OUTLINE, italic=True),
            ft.Container(
                padding=16,
                border_radius=8,
                bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                content=ft.Column(
                    spacing=12,
                    controls=[
                        ft.Text("VAULT", size=10, weight=ft.FontWeight.BOLD,
                                color=ft.Colors.OUTLINE),
                        ft.Text(f"Currently loaded: {VAULT_PATH}", size=11,
                                font_family="JetBrains Mono", color=ft.Colors.OUTLINE),
                        self.settings_vault_field,
                        ft.Row(
                            controls=[
                                ft.FilledButton(
                                    "Save & reload vault", icon=ft.Icons.FOLDER_OPEN,
                                    tooltip="Point Workbench at this vault and reload "
                                            "projects, agents, areas, inbox, threads.",
                                    on_click=self._on_save_and_reload_vault,
                                ),
                            ],
                        ),
                        ft.Text(
                            "Absolute path to an Obsidian vault root (with 10_Projects/, "
                            "_System/Agents/, …). Blank = the bundled vault. `~` expands. "
                            "Saving here reloads everything from the new vault.",
                            size=11, color=ft.Colors.OUTLINE,
                        ),
                    ],
                ),
            ),
            ft.Container(
                padding=16,
                border_radius=8,
                bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                content=ft.Column(
                    spacing=12,
                    controls=[
                        ft.Text("BACKEND", size=10, weight=ft.FontWeight.BOLD,
                                color=ft.Colors.OUTLINE),
                        self.settings_mock_switch,
                        ft.Text(
                            "When ON, every agent call returns a fast canned response — "
                            "useful during UI iteration to avoid burning tokens. Turn OFF to "
                            "route real calls based on each agent's `model:` prefix.",
                            size=11, color=ft.Colors.OUTLINE,
                        ),
                        self.settings_apikey_field,
                        ft.Text(
                            "Used when an agent's model is `openrouter/...` or has no prefix. "
                            "Get one at https://openrouter.ai/keys",
                            size=11, color=ft.Colors.OUTLINE,
                        ),
                        self.settings_model_field,
                        self.settings_model_dropdown,
                        ft.Text(
                            "Model for the chat agent. Editable — type any valid OpenRouter "
                            "slug; the list is just common picks. Browse https://openrouter.ai/models",
                            size=11, color=ft.Colors.OUTLINE,
                        ),
                        self.settings_debug_switch,
                        self.settings_tools_switch,
                        ft.Text("Tools let the agent act on your vault directly. It "
                                "reads files, writes notes, and runs shell commands "
                                "in the working_dir.",
                                size=11, color=ft.Colors.OUTLINE),
                        self.settings_tool_confirm_switch,
                        ft.Text("On: the agent pauses for an Allow/Deny dialog before "
                                "each change (write / move / shell / delegate); reads "
                                "never prompt. Approving offers “always allow in this "
                                "thread”. Off = trust mode: every call runs, git is "
                                "your undo.",
                                size=11, color=ft.Colors.OUTLINE),
                        ft.Container(height=8),
                        ft.Text("DELEGATION (CLI agents)", size=10,
                                weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE),
                        ft.Text("Two paths. Interactive: the “Delegate in terminal” "
                                "button on a project opens the CLI in a real terminal "
                                "(full prompts, you drive it) — always available. "
                                "Headless: the switch below lets the chat agent hand a "
                                "task to the CLI in the background. Headless has no TTY, "
                                "so it can’t prompt — it runs under the permission mode "
                                "(or allow-list) below.",
                                size=11, color=ft.Colors.OUTLINE),
                        self.settings_delegate_switch,
                        self.settings_delegate_cmd_field,
                        self.settings_delegate_permmode_field,
                        self.settings_delegate_allowed_field,
                        self.settings_delegate_timeout_field,
                        self.settings_delegate_term_field,
                        self.settings_cli_session_field,
                        ft.Text("'Open CLI session' (working-dir card) launches this "
                                "CLI in a terminal, rooted in the folder, symlinks "
                                "the whole vault into a gitignored "
                                f"{cfg.cli_session_context_dir}/ subfolder so the agent "
                                "can read your notes, and sends the editable opening "
                                "message as the first prompt. Reuses the terminal "
                                "template above.",
                                size=11, color=ft.Colors.OUTLINE),
                        ft.Container(height=8),
                        ft.Text("PUBLISHING (WordPress.com)", size=10,
                                weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE),
                        ft.Text("Publish a note as a WordPress.com post (draft-first). "
                                "Auth uses a registered app's OAuth2 password grant — "
                                "register one at developer.wordpress.com/apps (Type = "
                                "Web); with 2FA, use an Application Password.",
                                size=11, color=ft.Colors.OUTLINE),
                        self.settings_wp_site_field,
                        self.settings_wp_clientid_field,
                        self.settings_wp_secret_field,
                        self.settings_wp_user_field,
                        self.settings_wp_pass_field,
                        ft.Row([
                            ft.OutlinedButton("Test connection",
                                              icon=ft.Icons.WIFI_TETHERING,
                                              on_click=self._on_test_wp_connection),
                            self.settings_wp_test_status,
                        ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        self.settings_wp_status_dd,
                        ft.Text("Categories & tags are derived from the note's place in "
                                "the vault — the part WP can't know. A note's frontmatter "
                                "(visibility / wp_password / wp_categories / wp_tags) "
                                "overrides the auto policy.",
                                size=11, color=ft.Colors.OUTLINE),
                        self.settings_wp_category_dd,
                        self.settings_wp_projtag_switch,
                        self.settings_wp_notetags_switch,
                        self.settings_wp_tagexclude_field,
                        self.settings_wp_agent_switch,
                        ft.Text("Off by default (publishing is outward-facing). When "
                                "on, the chat agent can call publish_note; it's still "
                                "draft-first and obeys “Ask before changes”.",
                                size=11, color=ft.Colors.OUTLINE),
                        ft.Container(height=8),
                        ft.Text("GENERATION", size=10, weight=ft.FontWeight.BOLD,
                                color=ft.Colors.OUTLINE),
                        self.settings_temp_field,
                        ft.Text("Default sampling temperature. Seeds the 🌡 control "
                                "in the chat input bar, which can override it per message.",
                                size=11, color=ft.Colors.OUTLINE),
                        self.settings_maxtokens_field,
                        self.settings_ctxcap_field,
                        ft.Text("How much project-note text (in tokens, ~4 chars each) "
                                "to inject into the system prompt. Higher = more context, "
                                "higher cost.", size=11, color=ft.Colors.OUTLINE),
                        self.settings_preamble_field,
                        ft.Container(height=8),
                        ft.Text("BACKEND (cont.)", size=10, weight=ft.FontWeight.BOLD,
                                color=ft.Colors.OUTLINE),
                        self.settings_ollama_field,
                        ft.Text(
                            "Used when an agent's model is `ollama/<name>`. Default works "
                            "if Ollama is running on this machine.",
                            size=11, color=ft.Colors.OUTLINE,
                        ),
                        ft.Container(height=8),
                        ft.Text("WORKING DIR ACTIONS", size=10,
                                weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE),
                        self.settings_editor_field,
                        self.settings_terminal_field,
                        self.settings_git_ui_field,
                        ft.Text(
                            "Used by 'Open in editor' / 'Open in terminal' buttons in "
                            "project overview's WORKING DIR section. `{path}` is "
                            "substituted at runtime. 'Open in lazygit' (Home + working "
                            "dir card) runs the Git UI command in a terminal — shown "
                            "only on git repos.",
                            size=11, color=ft.Colors.OUTLINE,
                        ),
                        ft.Container(height=8),
                        *self._telegram_settings_controls(),
                        ft.Container(height=8),
                        ft.Text("APPEARANCE", size=10,
                                weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE),
                        self.settings_main_bg_field,
                        self.settings_view_bg_field,
                        self.settings_tab_inactive_bg_field,
                        ft.Text(
                            "Hex colors (#RRGGBB). Restart Workbench after saving to "
                            "see the new theme. Three layers: room (sidebar+bg) · "
                            "unselected sections · workbench surface (active section "
                            "+ view body).",
                            size=11, color=ft.Colors.OUTLINE,
                        ),
                        ft.Container(height=8),
                        ft.Text("TITLE STYLE", size=10,
                                weight=ft.FontWeight.BOLD, color=ft.Colors.OUTLINE),
                        ft.Text("The big heading on project / area / settings views. "
                                "Pick a preset, then fine-tune the font, size and color "
                                "(backgrounds stay as-is — only the title). Edits flip "
                                "the preset to “Custom”. Applies on the next time you "
                                "open a view; no restart needed.",
                                size=11, color=ft.Colors.OUTLINE),
                        self.settings_title_preset_dd,
                        ft.Row(spacing=12, wrap=True, controls=[
                            self.settings_title_font_dd,
                            self.settings_title_size_field,
                            self.settings_title_spacing_field,
                        ]),
                        ft.Row(spacing=16, controls=[
                            self.settings_title_color_field,
                            self.settings_title_bold_switch,
                            self.settings_title_italic_switch,
                        ]),
                        ft.Container(
                            padding=ft.Padding(left=14, top=14, right=14, bottom=14),
                            bgcolor=PLATINUM["panel"], border_radius=2,
                            border=_bevel(raised=False),
                            content=ft.Column(spacing=4, controls=[
                                ft.Text("PREVIEW", size=9, weight=ft.FontWeight.BOLD,
                                        font_family="JetBrains Mono",
                                        color=ft.Colors.OUTLINE),
                                self.settings_title_preview,
                            ]),
                        ),
                        ft.Text(f"Presets live in title_themes.json — add your own at "
                                f"~/.workbench/title_themes.json. Fonts available: "
                                f"{', '.join(TITLE_FONTS)}.",
                                size=11, color=ft.Colors.OUTLINE),
                    ],
                ),
            ),
            ft.Row(
                controls=[
                    ft.FilledButton("Save", icon=ft.Icons.SAVE,
                                    on_click=self._on_save_settings),
                    self.settings_save_status,
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            ft.Container(height=8),
            ft.Container(
                padding=16,
                border_radius=8,
                bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                content=ft.Column(
                    spacing=4,
                    controls=[
                        ft.Text("MODEL ROUTING", size=10, weight=ft.FontWeight.BOLD,
                                color=ft.Colors.OUTLINE),
                        ft.Text(
                            "Each agent's `model:` frontmatter routes by prefix:",
                            size=12,
                        ),
                        ft.Text("  mock/<anything>           → instant fake responses",
                                size=12, color=ft.Colors.OUTLINE),
                        ft.Text("  ollama/<model-name>       → local Ollama (offline; e.g. ollama/qwen2.5:3b)",
                                size=12, color=ft.Colors.OUTLINE),
                        ft.Text("  openrouter/<provider>/<model>  → OpenRouter",
                                size=12, color=ft.Colors.OUTLINE),
                        ft.Text("  <provider>/<model>        → OpenRouter (default)",
                                size=12, color=ft.Colors.OUTLINE),
                    ],
                ),
            ),
        ])

    # --- Events ---
    def _open_or_focus(self, tab: OpenTab):
        if tab not in self.state.open_tabs:
            self.state.open_tabs.append(tab)
        self.state.active_tab = tab
        self.refresh()

    def _set_active_tab(self, tab: OpenTab):
        self.state.active_tab = tab
        self.refresh()

    def _activate_project(self, pid: str):
        self._open_or_focus(OpenTab(kind="overview", ref_id=pid))

    def _open_thread(self, tid: str):
        self._open_or_focus(OpenTab(kind="chat", ref_id=tid))

    def _open_agent(self, name: str):
        self._open_or_focus(OpenTab(kind="agent", ref_id=name))

    def _open_tasks_view(self, project_id: str):
        self._open_or_focus(OpenTab(kind="tasks", ref_id=project_id))

    def _open_area(self, area_name: str):
        self._open_or_focus(OpenTab(kind="area", ref_id=area_name))

    def _open_inbox(self):
        self._open_or_focus(OpenTab(kind="inbox", ref_id="inbox"))

    def _open_home(self):
        self._open_or_focus(OpenTab(kind="home", ref_id="home"))

    def _open_reviews(self):
        self._open_or_focus(OpenTab(kind="reviews", ref_id="reviews"))

    def _open_resources(self):
        self._open_or_focus(OpenTab(kind="resources", ref_id="resources"))

    def _on_resources_select(self, path: str):
        self.state.selected_resource_path = path
        self._refresh_resources_view()
        self.page.update()

    def _on_resources_refresh(self):
        self.state.resources = load_vault_entries(VAULT_PATH, "30_Resources")
        if self.state.selected_resource_path and not any(
                r.path == self.state.selected_resource_path for r in self.state.resources):
            self.state.selected_resource_path = None
        self.refresh()

    def _on_resources_close_preview(self):
        self.state.selected_resource_path = None
        self._refresh_resources_view()
        self.page.update()

    def reload_people(self):
        """Reload 40_People/ from disk + drop a stale selection, then refresh.
        Stays on the app (owns VAULT_PATH + the global refresh); called by
        PeopleView.on_refresh."""
        self.state.people = load_vault_entries(VAULT_PATH, "40_People")
        if self.state.selected_person_path and not any(
                p.path == self.state.selected_person_path for p in self.state.people):
            self.state.selected_person_path = None
        self.refresh()

    def _on_inbox_select(self, path: str):
        self.state.selected_inbox_path = path
        self._refresh_inbox_list()
        self._refresh_inbox_preview()
        self.page.update()

    def _on_overview_refresh(self):
        """Re-read the active project from disk so edits made outside Workbench
        (status/hypothesis/tags in Obsidian, new notes or files in the folder)
        show without restarting. Reloads frontmatter in place onto the live
        Project — preserving its threads + identity — then re-renders; the
        overview's content sections (tasks/inbox/posts/files) re-scan disk on
        every render, so they pick up new files for free."""
        p = self.state.active_project
        if not p:
            return
        ctx = _project_context_paths(p)
        if ctx:
            fresh = _load_project_from_md(ctx[0][1], VAULT_PATH)
            if fresh:
                for attr in ("name", "vault_folder", "status", "hypothesis",
                             "area", "modes", "tags", "working_dir",
                             "context_files", "context_tokens", "is_probe",
                             "probe", "review", "scope", "started",
                             "micro_commitment"):
                    setattr(p, attr, getattr(fresh, attr))
        self.refresh()  # re-renders the overview + sidebar status dot/badges
        self._toast("Project refreshed from disk")

    def _on_promote_to_folder(self, pid: str):
        """Move a single-note project into its own folder so it can hold
        posts/journals/notes/files. Updates the live Project + re-renders."""
        p = self.state.get_project(pid)
        if not p:
            return
        err = promote_to_folder(p, VAULT_PATH)
        if err:
            self._toast(f"Move failed: {err}")
            return
        self.refresh()
        self._toast(f"{p.name} → its own folder. You can now add posts/journals/notes.")

    def _on_add_task(self, pid: str, src):
        """Append `- [ ] <text>` to the project's main note under `## Tasks`
        (heading created if absent). `src` is a TextField or its on_submit event."""
        p = self.state.get_project(pid)
        if not p:
            return
        field = src.control if hasattr(src, "control") else src
        text = (getattr(field, "value", "") or "").strip()
        if not text:
            return
        ctx = _project_context_paths(p)
        if not ctx:
            self._toast("Couldn't resolve the project's main note")
            return
        try:
            append_to_main_note(ctx[0][1], "## Tasks", f"- [ ] {text}")
        except Exception as ex:
            self._toast(f"Add task failed: {ex}")
            return
        try:
            field.value = ""
        except Exception:
            pass
        self.refresh()
        self._toast("Task added to the main note")

    def _on_create_typed_note(self, pid: str, note_type: str):
        """Dialog → create a `type: <note_type>` note in the project folder, then
        open it in Obsidian. File-scoped projects must be promoted first."""
        p = self.state.get_project(pid)
        if not p:
            return
        if not p.vault_folder.endswith("/"):
            self._toast("Move this project to its own folder first "
                        "(button near the top), then add notes.")
            return
        title_field = ft.TextField(label="Title", autofocus=True, text_size=13)

        def go(_e):
            title = (title_field.value or "").strip()
            if not title:
                return
            self.page.pop_dialog()
            folder = VAULT_PATH / p.vault_folder
            try:
                path = create_project_note(folder, note_type, title)
            except Exception as ex:
                self._toast(f"Create failed: {ex}")
                return
            self.refresh()
            self._toast(f"Created {note_type}: {path.name}")
            self._on_open_in_obsidian(path)

        dlg = ft.AlertDialog(
            title=ft.Text(f"New {note_type}"),
            content=ft.Container(width=420, content=title_field),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _e: self.page.pop_dialog()),
                ft.FilledButton("Create", icon=ft.Icons.ADD, on_click=go),
            ],
        )
        self.page.show_dialog(dlg)

    def _on_inbox_refresh(self):
        self.state.inbox_items = load_inbox_items(VAULT_PATH)
        # Keep selection if it still exists
        if self.state.selected_inbox_path and not any(
                i.path == self.state.selected_inbox_path for i in self.state.inbox_items):
            self.state.selected_inbox_path = None
        self.refresh()  # also updates the sidebar count badge

    def _on_inbox_close_preview(self):
        self.state.selected_inbox_path = None
        self._refresh_inbox_list()
        self._refresh_inbox_preview()
        self.page.update()

    # --- Inbox: Ask Workbench (agent-driven triage) --------------------------
    def _on_inbox_ask_workbench(self, item: InboxItem):
        """Open a dialog to capture what the user already knows about this inbox
        note, then hand the note + that context to the agent in a fresh chat so
        it can triage (move / create project / set status / add tasks) for real.
        Tool use is on by default, so the agent acts rather than just describing."""
        tips_field = ft.TextField(
            multiline=True, min_lines=4, max_lines=10, autofocus=True,
            text_size=13,
            hint_text=(
                "What do you already know, and what should happen?  e.g.\n"
                "  • new project for the <area> area → active + demand-probe\n"
                "  • notes for projects A and B — add task X and a Y mode to each\n"
                "  • reference material → file under 30_Resources\n"
                "(Leave blank to let the agent decide the best destination.)"
            ),
        )
        if self.state.config.force_mock:
            hint = ft.Text(
                "Heads up: force-mock is ON (Settings) — the agent will reply with "
                "mock text and won't really move files. Turn it off to triage for real.",
                size=11, color=ft.Colors.TERTIARY,
            )
        else:
            hint = ft.Text(
                "The agent works in trust mode: it will move/create/edit files "
                "directly, then tell you what it did.",
                size=11, color=ft.Colors.OUTLINE,
            )

        def go(_e):
            self.page.pop_dialog()
            self._start_inbox_triage(item, tips_field.value or "")

        dlg = ft.AlertDialog(
            title=ft.Text(f"Ask Workbench — {item.name}"),
            content=ft.Container(
                width=620,
                content=ft.Column([tips_field, hint], spacing=10, tight=True),
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _e: self.page.pop_dialog()),
                ft.FilledButton("Ask", icon=ft.Icons.AUTO_AWESOME, on_click=go),
            ],
        )
        self.page.show_dialog(dlg)

    def _start_inbox_triage(self, item: InboxItem, tips: str):
        """Seed a scratch chat thread with the note + the user's context + a
        triage framing, open it, and dispatch. Mirrors `_on_send`'s turn-seeding;
        the agent uses file tools (rooted at the vault) to do the triage."""
        try:
            raw = Path(item.path).read_text(encoding="utf-8")
        except Exception as ex:
            raw = f"(could not read file: {ex})"
        if len(raw) > 8000:  # inbox captures are small; cap defensively
            raw = raw[:8000] + "\n…(truncated)"
        vault_rel = item.path
        try:
            vault_rel = str(Path(item.path).relative_to(VAULT_PATH))
        except ValueError:
            pass

        tips = tips.strip()
        tips_block = tips if tips else "(no extra context — you decide the best destination.)"

        seed = (
            "I'm triaging a note from my vault inbox (`00_Inbox/`). Sort it out for "
            "me using your file tools — you're in trust mode, so make the changes "
            "directly and then tell me what you did. Ask only if something is "
            "genuinely ambiguous (e.g. a merge/rename you can't infer).\n\n"
            "Vault structure (root = single source of truth):\n"
            "- `10_Projects/` — one folder or note per project=experiment "
            "(`type: project`, needs `area:` + `status:`)\n"
            "- `20_Areas/` — ongoing areas (one note each, e.g. Work / Civic / "
            "Family); a project's `area:` must match an area note's `area:` key\n"
            "- `30_Resources/` — reference · `40_People/` — contacts · "
            "`00_Inbox/` — capture\n"
            "Conventions: every note has `type:` frontmatter. `status:` ∈ "
            "idea/active/persist/pause/pivot/done. A demand probe carries "
            "`demand-probe` in `tags:`. Status lives in frontmatter, never folder "
            "location. When creating a new project, read "
            "`_System/Templates/Project.md` and follow it. Read `CLAUDE.md` at the "
            "vault root if you need more on the conventions.\n\n"
            f"--- NOTE: {vault_rel} ---\n{raw}\n--- END NOTE ---\n\n"
            f"What I know / what to do:\n{tips_block}"
        )

        new_id = f"t_{uuid.uuid4().hex[:8]}"
        thread = Thread(id=new_id, name=f"Triage: {item.name}", project_id="")
        user_id = f"n_{uuid.uuid4().hex[:6]}"
        thread.turns[user_id] = Turn(
            id=user_id, parent_id=None, speaker="user", text=seed)
        thread.root_id = user_id
        team_id = f"n_{uuid.uuid4().hex[:6]}"
        team_turn = Turn(id=team_id, parent_id=user_id, speaker="team", text="")
        thread.turns[team_id] = team_turn
        thread.current_leaf_id = team_id

        self.state.scratch_threads.append(thread)
        self._open_or_focus(OpenTab(kind="chat", ref_id=new_id))  # refresh()es
        self._save_thread(thread)
        self._dispatch_single(thread, team_turn)

    def _open_settings(self, e=None):
        self._open_or_focus(OpenTab(kind="settings", ref_id="settings"))

    def _on_toggle_force_mock(self, e):
        """Force-mock takes effect the moment you flip it — no Save needed. brain_for
        reads config.force_mock live, so this immediately switches mock/real calls."""
        self.state.config.force_mock = bool(self.settings_mock_switch.value)
        try:
            save_config(self.state.config)
            self.settings_save_status.value = (
                "Force-mock ON — canned responses"
                if self.state.config.force_mock
                else "Force-mock OFF — real calls enabled"
            )
            self.settings_save_status.color = ft.Colors.TERTIARY
        except Exception as ex:
            self.settings_save_status.value = f"Save failed: {ex}"
            self.settings_save_status.color = ft.Colors.ERROR
        self.page.update()

    # --- Telegram capture (managed daemon) ---
    def _tg_status_text(self) -> tuple[str, str]:
        """(message, color) describing the daemon + claim state. Read fresh from
        telegram.json each call so an owner claimed by the running daemon shows."""
        tgcfg = load_telegram_cfg()
        if not tgcfg.get("bot_token"):
            return ("No bot token saved yet — paste one above and Save (or Start).",
                    ft.Colors.OUTLINE)
        if self.telegram.is_running():
            owner = tgcfg.get("owner_id")
            claim = (f"owner claimed (id {owner})" if owner
                     else "UNCLAIMED — message the bot to bind it to you")
            return (f"● Running — {claim}", ft.Colors.TERTIARY)
        return ("○ Stopped", ft.Colors.OUTLINE)

    def _telegram_settings_controls(self) -> list:
        """Build the TELEGRAM CAPTURE settings block (status is dynamic, so this
        is rebuilt on every settings refresh)."""
        running = self.telegram.is_running()
        msg, color = self._tg_status_text()
        controls = [
            ft.Text("TELEGRAM CAPTURE", size=10, weight=ft.FontWeight.BOLD,
                    color=ft.Colors.OUTLINE),
            ft.Text(
                "Send notes to your vault inbox from your phone. Create a bot with "
                "@BotFather, paste its token, then Start. The first person to "
                "message the bot is claimed as the owner; everyone else is ignored. "
                "Captures while Workbench is open (stops on exit).",
                size=11, color=ft.Colors.OUTLINE,
            ),
            self.settings_tg_token_field,
            ft.Row(
                controls=[
                    ft.FilledButton("Start", icon=ft.Icons.PLAY_ARROW,
                                    on_click=self._on_tg_start, disabled=running),
                    ft.OutlinedButton("Stop", icon=ft.Icons.STOP,
                                      on_click=self._on_tg_stop, disabled=not running),
                    ft.TextButton("Refresh", icon=ft.Icons.REFRESH,
                                  on_click=self._on_tg_refresh_status),
                ],
                spacing=8,
            ),
            ft.Text(msg, size=12, color=color),
        ]
        log = self.telegram.tail_log()
        if log:
            controls.append(
                ft.Container(
                    padding=8, border_radius=6,
                    bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                    content=ft.Text(log, size=10, font_family="JetBrains Mono",
                                    color=ft.Colors.ON_SURFACE_VARIANT),
                )
            )
        return controls

    def _save_tg_token(self) -> None:
        """Persist the token field into telegram.json, preserving owner_id."""
        tgcfg = load_telegram_cfg()
        tgcfg["bot_token"] = (self.settings_tg_token_field.value or "").strip()
        tgcfg.setdefault("owner_id", 0)
        save_telegram_cfg(tgcfg)

    def _on_tg_start(self, e):
        self._save_tg_token()
        if not load_telegram_cfg().get("bot_token"):
            self._toast("Add a bot token first.")
            return
        err = self.telegram.start()
        self._toast("Telegram capture started." if not err else f"Start failed: {err}")
        self._refresh_settings_view()
        self.page.update()

    def _on_tg_stop(self, e):
        self.telegram.stop()
        self._toast("Telegram capture stopped.")
        self._refresh_settings_view()
        self.page.update()

    def _on_tg_refresh_status(self, e):
        self._refresh_settings_view()
        self.page.update()

    # --- Title-style live controls ---
    def _read_title_size(self) -> int:
        try:
            return max(8, min(200, int(float((self.settings_title_size_field.value or "30").strip()))))
        except (TypeError, ValueError):
            return self.state.config.title_size or 30

    def _read_title_spacing(self) -> float:
        try:
            return float((self.settings_title_spacing_field.value or "0").strip())
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _safe_update(control):
        """control.update() but a no-op if it isn't mounted yet (Flet 0.85 raises
        on .page / .update() before a control is added to the page)."""
        try:
            control.update()
        except (RuntimeError, AssertionError, AttributeError):
            pass

    def _refresh_title_preview(self):
        """Repaint the preview Text from the current title-style field values."""
        p = self.settings_title_preview
        p.value = "The quick brown fox"
        p.size = self._read_title_size()
        p.font_family = self.settings_title_font_dd.value or "Newsreader"
        p.weight = (ft.FontWeight.BOLD if self.settings_title_bold_switch.value
                    else ft.FontWeight.NORMAL)
        p.italic = bool(self.settings_title_italic_switch.value)
        p.color = (self.settings_title_color_field.value or "").strip() or None
        ls = self._read_title_spacing()
        p.style = ft.TextStyle(letter_spacing=ls) if ls else None
        self._safe_update(p)

    def _on_title_field_change(self, e):
        # Hand-editing any field means we're no longer on a named preset.
        if self.settings_title_preset_dd.value is not None:
            self.settings_title_preset_dd.value = None
            self._safe_update(self.settings_title_preset_dd)
        self._refresh_title_preview()

    def _on_pick_title_preset(self, e):
        name = self.settings_title_preset_dd.value
        preset = next((t for t in self._title_themes if t["name"] == name), None)
        if not preset:
            return
        self.settings_title_font_dd.value = (preset["font"] if preset["font"] in TITLE_FONTS
                                             else "Newsreader")
        self.settings_title_size_field.value = str(preset["size"])
        self.settings_title_color_field.value = preset.get("color", "")
        self.settings_title_spacing_field.value = f"{preset.get('letter_spacing', 0):g}"
        self.settings_title_bold_switch.value = bool(preset.get("bold"))
        self.settings_title_italic_switch.value = bool(preset.get("italic"))
        for c in (self.settings_title_font_dd, self.settings_title_size_field,
                  self.settings_title_color_field, self.settings_title_spacing_field,
                  self.settings_title_bold_switch, self.settings_title_italic_switch):
            self._safe_update(c)
        self._refresh_title_preview()

    def _on_save_settings(self, e):
        cfg = self.state.config
        cfg.openrouter_api_key = (self.settings_apikey_field.value or "").strip()
        cfg.force_mock = bool(self.settings_mock_switch.value)
        cfg.chat_model = (self.settings_model_field.value or "").strip() or "anthropic/claude-opus-4-7"
        cfg.debug_prompts = bool(self.settings_debug_switch.value)
        cfg.tools_enabled = bool(self.settings_tools_switch.value)
        cfg.tool_confirm = bool(self.settings_tool_confirm_switch.value)

        def _num(field, cast, default, lo=None, hi=None):
            try:
                v = cast((field.value or "").strip())
            except (TypeError, ValueError):
                return default
            if lo is not None:
                v = max(lo, v)
            if hi is not None:
                v = min(hi, v)
            return v
        cfg.temperature = _num(self.settings_temp_field, float, cfg.temperature, 0.0, 2.0)
        cfg.max_tokens = _num(self.settings_maxtokens_field, int, cfg.max_tokens, 0)
        cfg.context_token_cap = _num(self.settings_ctxcap_field, int, cfg.context_token_cap, 0)
        cfg.system_preamble = self.settings_preamble_field.value or ""
        # Delegation
        cfg.delegate_enabled = bool(self.settings_delegate_switch.value)
        cfg.delegate_command = (self.settings_delegate_cmd_field.value or "claude").strip()
        cfg.delegate_permission_mode = (self.settings_delegate_permmode_field.value or "").strip()
        cfg.delegate_allowed_tools = (self.settings_delegate_allowed_field.value or "").strip()
        cfg.delegate_timeout = _num(self.settings_delegate_timeout_field, int, cfg.delegate_timeout, 1)
        cfg.delegate_terminal_command = (self.settings_delegate_term_field.value or "").strip()
        cfg.cli_session_command = (self.settings_cli_session_field.value or "claude").strip()
        # Publishing (WordPress.com)
        cfg.wpcom_site = (self.settings_wp_site_field.value or "").strip()
        cfg.wpcom_client_id = (self.settings_wp_clientid_field.value or "").strip()
        cfg.wpcom_client_secret = (self.settings_wp_secret_field.value or "").strip()
        cfg.wpcom_username = (self.settings_wp_user_field.value or "").strip()
        cfg.wpcom_password = (self.settings_wp_pass_field.value or "").strip()
        cfg.publish_default_status = (self.settings_wp_status_dd.value or "draft").strip()
        cfg.publish_auto_category = (self.settings_wp_category_dd.value or "area").strip()
        cfg.publish_add_project_tag = bool(self.settings_wp_projtag_switch.value)
        cfg.publish_include_note_tags = bool(self.settings_wp_notetags_switch.value)
        cfg.publish_tag_exclude = (self.settings_wp_tagexclude_field.value or "").strip()
        cfg.publish_enabled = bool(self.settings_wp_agent_switch.value)
        # Reseed the per-message temperature control to the new default.
        self._chat_temperature = cfg.temperature
        self._sync_temp_btn()
        cfg.ollama_base_url = (self.settings_ollama_field.value or
                               "http://localhost:11434/v1").strip()
        cfg.editor_command = (self.settings_editor_field.value or "zed {path}").strip()
        cfg.terminal_command = (self.settings_terminal_field.value or
                                "gnome-terminal --working-directory={path}").strip()
        cfg.git_ui_command = (self.settings_git_ui_field.value or "lazygit").strip()
        cfg.vault_path = (self.settings_vault_field.value or "").strip()
        cfg.main_bg_color = (self.settings_main_bg_field.value or PLATINUM["canvas"]).strip()
        cfg.view_bg_color = (self.settings_view_bg_field.value or PLATINUM["panel"]).strip()
        cfg.tab_inactive_bg_color = (self.settings_tab_inactive_bg_field.value or
                                     PLATINUM["face"]).strip()
        # Title style (applies on next view render — no restart needed).
        cfg.title_font = self.settings_title_font_dd.value or "Newsreader"
        cfg.title_size = self._read_title_size()
        cfg.title_color = (self.settings_title_color_field.value or "").strip()
        cfg.title_letter_spacing = self._read_title_spacing()
        cfg.title_bold = bool(self.settings_title_bold_switch.value)
        cfg.title_italic = bool(self.settings_title_italic_switch.value)
        cfg.title_theme = self.settings_title_preset_dd.value or "Custom"
        # Telegram bot token is stored in telegram.json (not config.json).
        self._save_tg_token()
        try:
            save_config(cfg)
            self.settings_save_status.value = f"Saved → {CONFIG_PATH}"
            self.settings_save_status.color = ft.Colors.TERTIARY
        except Exception as ex:
            self.settings_save_status.value = f"Save failed: {ex}"
            self.settings_save_status.color = ft.Colors.ERROR
        self.page.update()

    def _on_test_wp_connection(self, e):
        """Mint a token from the fields as currently typed (no save needed) so you
        can verify WordPress.com creds before publishing. Runs off the UI thread."""
        creds = publish.WPCreds(
            client_id=(self.settings_wp_clientid_field.value or "").strip(),
            client_secret=(self.settings_wp_secret_field.value or "").strip(),
            username=(self.settings_wp_user_field.value or "").strip(),
            password=(self.settings_wp_pass_field.value or "").strip(),
            site=(self.settings_wp_site_field.value or "").strip(),
        )
        self.settings_wp_test_status.value = "Testing…"
        self.settings_wp_test_status.color = ft.Colors.OUTLINE
        self.page.update()

        def run():
            try:
                tok = publish.mint_token(creds)
                msg, color = f"✓ Connected (token {tok[:6]}…)", ft.Colors.TERTIARY
            except publish.PublishError as ex:
                msg, color = str(ex), ft.Colors.ERROR
            except Exception as ex:
                msg, color = f"Error: {ex}", ft.Colors.ERROR

            async def _fin():
                self.settings_wp_test_status.value = msg
                self.settings_wp_test_status.color = color
                self.page.update()

            try:
                self.page.run_task(_fin)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _on_save_and_reload_vault(self, e):
        """Persist the vault path, then reload all vault-derived state from it.
        Lets you switch vaults without restarting the app."""
        global VAULT_PATH
        raw = (self.settings_vault_field.value or "").strip()
        new_path = _resolve_vault_path(raw)
        if not new_path.exists():
            self._toast(f"vault path does not exist: {new_path}")
            return
        cfg = self.state.config
        cfg.vault_path = raw
        try:
            save_config(cfg)
        except Exception as ex:
            self._toast(f"save failed: {ex}")
            return
        # Repoint the global every loader/handler reads, then rebuild state.
        VAULT_PATH = new_path
        new_state = load_state_from_vault(VAULT_PATH)
        new_state.config = cfg
        self.state = new_state
        self._load_persisted_threads()
        # Land on Home so we're not pointing at a tab from the old vault.
        self._open_home()
        self.refresh()
        self._toast(f"loaded vault: {VAULT_PATH}")

    def _close_tab(self, tab: OpenTab):
        if tab not in self.state.open_tabs:
            return
        idx = self.state.open_tabs.index(tab)
        self.state.open_tabs.remove(tab)
        if self.state.active_tab == tab:
            if self.state.open_tabs:
                self.state.active_tab = self.state.open_tabs[
                    min(idx, len(self.state.open_tabs) - 1)
                ]
            else:
                self.state.active_tab = None
        self.refresh()

    def _load_persisted_threads(self):
        """Rehydrate saved threads (store.py) into their project. Join key is
        project.id (stable from the project name). Threads whose project no
        longer exists are skipped, but their files are left untouched so a
        rename-back restores them."""
        by_project: dict[str, list[Thread]] = {}
        for d in load_thread_dicts():
            try:
                turns = {
                    td["id"]: Turn(
                        id=td["id"],
                        parent_id=td.get("parent_id"),
                        speaker=td.get("speaker", "team"),
                        text=td.get("text", ""),
                        pinned=td.get("pinned", False),
                        tool_steps=td.get("tool_steps", []) or [],
                    )
                    for td in d.get("turns", [])
                }
                thread = Thread(
                    id=d["id"],
                    name=d.get("name", "thread"),
                    project_id=d.get("project_id", ""),
                    turns=turns,
                    root_id=d.get("root_id"),
                    current_leaf_id=d.get("current_leaf_id"),
                    system_prompt_override=d.get("system_prompt_override"),
                )
                by_project.setdefault(thread.project_id, []).append(thread)
            except Exception:
                continue
        for p in self.state.projects:
            saved = by_project.get(p.id)
            if saved:
                # loader returns newest-first; present oldest-first to match the
                # append order new threads use during a session.
                p.threads = list(reversed(saved))
        # Top-level scratch chats (project_id == "") have no project to attach to.
        scratch = by_project.get("")
        if scratch:
            self.state.scratch_threads = list(reversed(scratch))

    def _save_thread(self, thread: Optional[Thread]):
        if thread is not None:
            save_thread(thread)

    def _rename_thread_dialog(self, tid: str):
        """User-initiated rename (no autorename). Persists + refreshes."""
        t = self.state.get_thread(tid)
        if not t:
            return
        field = ft.TextField(label="Thread name", value=t.name, autofocus=True)

        def save(_e):
            new = (field.value or "").strip()
            if new:
                t.name = new
                self._save_thread(t)
            self.page.pop_dialog()
            self.refresh()

        dlg = ft.AlertDialog(
            title=ft.Text("Rename thread"),
            content=ft.Container(width=360, content=field),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _e: self.page.pop_dialog()),
                ft.TextButton("Save", on_click=save),
            ],
        )
        self.page.show_dialog(dlg)

    def _on_new_thread(self, pid: str):
        proj = self.state.get_project(pid)
        if not proj:
            return
        new_id = f"t_{uuid.uuid4().hex[:8]}"
        new_thread = Thread(
            id=new_id,
            name=f"New thread {len(proj.threads) + 1}",
            project_id=proj.id,
        )
        proj.threads.append(new_thread)
        self._open_or_focus(OpenTab(kind="chat", ref_id=new_id))

    def _on_quick_capture(self, e=None):
        """Sidebar quick capture → drop a note into 00_Inbox/ and refresh."""
        text = (self.sidebar_capture_field.value or "").strip()
        if not text:
            return
        try:
            path = _quick_capture_note(text)
        except Exception as ex:
            self._toast(f"Capture failed: {ex}")
            return
        self.sidebar_capture_field.value = ""
        self.state.inbox_items = load_inbox_items(VAULT_PATH)
        self.refresh()
        self._toast(f"Captured → {path.name}")

    def _on_new_scratch_thread(self, e=None):
        """Start a top-level chat with no project (from Home). No vault context —
        just Workbench + tools. project_id='' so it lands in scratch_threads."""
        new_id = f"t_{uuid.uuid4().hex[:8]}"
        thread = Thread(id=new_id, name=f"Chat {len(self.state.scratch_threads) + 1}",
                        project_id="")
        self.state.scratch_threads.append(thread)
        self._open_or_focus(OpenTab(kind="chat", ref_id=new_id))

    def _on_toggle_minimap(self, e):
        self.state.minimap_open = not self.state.minimap_open
        self.refresh()

    def _on_toggle_markdown(self, e):
        cfg = self.state.config
        cfg.render_markdown = not cfg.render_markdown
        save_config(cfg)
        self.md_toggle_btn.selected = cfg.render_markdown
        self._refresh_thread_view()
        self.page.update()

    def _on_nav_msg(self, delta: int):
        """▲/▼ jump-scroll to the previous/next message in the current thread.
        View-only — doesn't change the conversation."""
        t = self.state.active_thread
        ids = [turn.id for turn in self._active_path(t)] if t else []
        if not ids:
            return
        cur = max(0, min(getattr(self, "_nav_index", len(ids) - 1), len(ids) - 1))
        cur = max(0, min(cur + delta, len(ids) - 1))
        self._nav_index = cur
        # Keep smart-follow consistent: only stick to bottom when on the last msg.
        self.thread_view.auto_scroll = (cur == len(ids) - 1)
        self._scroll_to_turn(ids[cur])

    def _scroll_to_turn(self, turn_id: str):
        async def _do():
            try:
                await self.thread_view.scroll_to(scroll_key=turn_id, duration=200)
            except Exception:
                pass
        try:
            self.page.run_task(_do)
        except Exception:
            pass

    def _sync_temp_btn(self):
        if getattr(self, "temp_btn", None) is not None:
            self.temp_btn.content = ft.Text(f"🌡 {self._chat_temperature:.1f}", size=13)

    # --- @-references ---------------------------------------------------------
    def _atref_entries(self, project: Optional[Project]) -> list[dict]:
        """Indexed files+folders for the active project: its vault folder + its
        working_dir. Built once per project per session and cached. Inserts use
        vault-relative paths for vault entries and absolute paths for working_dir
        entries, so the read tools resolve them correctly."""
        if project is None:
            return []
        if project.id in self._atref_cache:
            return self._atref_cache[project.id]
        entries: list[dict] = []
        # Vault folder (insert vault-relative paths).
        vbase = VAULT_PATH / project.vault_folder
        if vbase.is_file():
            vbase = vbase.parent
        if vbase.exists():
            def vault_insert(p: Path) -> str:
                try:
                    return str(p.relative_to(VAULT_PATH))
                except ValueError:
                    return str(p)
            entries += _walk_atref(vbase, "vault", vault_insert)
        # working_dir (insert absolute paths).
        if project.working_dir:
            wd = _resolve_working_dir(project.working_dir)
            if wd and wd.exists():
                entries += _walk_atref(wd, "code", lambda p: str(p))
        self._atref_cache[project.id] = entries
        return entries

    def _on_input_change(self, e):
        text = self.input_field.value or ""
        tok = _trailing_at_query(text)
        if tok is None:
            if self.atref_panel_container.visible:
                self.atref_panel_container.visible = False
                self.page.update()
            return
        _, query = tok
        matches = _atref_match(self._atref_entries(self.state.active_project), query)
        self.atref_panel.controls.clear()
        if not matches:
            self.atref_panel.controls.append(
                ft.Text("no matching files", size=11, italic=True, color=ft.Colors.OUTLINE))
        else:
            for m in matches:
                self.atref_panel.controls.append(self._atref_row(m))
        self.atref_panel_container.visible = True
        self.page.update()

    def _atref_row(self, entry: dict) -> ft.Control:
        icon = ft.Icons.FOLDER_OUTLINED if entry["is_dir"] else ft.Icons.DESCRIPTION_OUTLINED
        tag = "dir" if entry["source"] == "code" else "vault"
        return ft.Container(
            padding=ft.Padding(left=6, top=5, right=6, bottom=5),
            border_radius=6,
            ink=True,
            on_click=lambda e, en=entry: self._atref_pick(en),
            content=ft.Row(
                [
                    ft.Icon(icon, size=14, color=ft.Colors.OUTLINE),
                    # Filename primary, full path as a dim subtitle. The Column
                    # expands so the tag on the right always has room; both lines
                    # ellipsize instead of overflowing.
                    ft.Column(
                        [
                            ft.Text(entry["name"], size=12, max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                    color=ft.Colors.ON_SURFACE),
                            ft.Text(entry["insert"], size=10, max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                    color=ft.Colors.OUTLINE),
                        ],
                        spacing=0, tight=True, expand=True,
                    ),
                    ft.Text(tag, size=9, color=ft.Colors.OUTLINE),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _atref_pick(self, entry: dict):
        text = self.input_field.value or ""
        tok = _trailing_at_query(text)
        prefix = text[:tok[0]] if tok else text
        if prefix and not prefix.endswith((" ", "\n")):
            prefix += " "
        self.input_field.value = f"{prefix}{entry['insert']} "
        self.atref_panel_container.visible = False
        self.page.update()
        try:
            self.input_field.focus()
        except Exception:
            pass

    def _open_temp_popover(self, e):
        """Mini slider (0.0–2.0) for the next message's temperature. Seeded from
        Settings → temperature; changes here apply to subsequent sends this session."""
        val = ft.Text(f"{self._chat_temperature:.1f}", size=16, weight=ft.FontWeight.BOLD)

        def on_change(ev):
            self._chat_temperature = round(ev.control.value, 1)
            val.value = f"{self._chat_temperature:.1f}"
            self._sync_temp_btn()
            self.page.update()

        slider = ft.Slider(min=0.0, max=2.0, divisions=20,
                           value=self._chat_temperature, label="{value}",
                           on_change=on_change)
        dlg = ft.AlertDialog(
            title=ft.Text("Temperature"),
            content=ft.Container(
                width=340,
                content=ft.Column(
                    [
                        ft.Row(
                            [ft.Text("0 · focused", size=11, color=ft.Colors.OUTLINE),
                             ft.Container(expand=True),
                             val,
                             ft.Container(expand=True),
                             ft.Text("2 · wild", size=11, color=ft.Colors.OUTLINE)],
                        ),
                        slider,
                        ft.Text("Seeded from Settings → temperature; applies to the "
                                "next messages this session.",
                                size=11, color=ft.Colors.OUTLINE),
                    ],
                    tight=True,
                ),
            ),
            actions=[ft.TextButton("Done", on_click=lambda _e: self.page.pop_dialog())],
        )
        self.page.show_dialog(dlg)

    def _on_send(self, e):
        text = (self.input_field.value or "").strip()
        tab = self.state.active_tab
        t = self.state.active_thread
        self._trace(f"_on_send: len={len(text)} "
                    f"tab={(tab.kind, tab.ref_id) if tab else None} "
                    f"thread={t.id if t else None} project={t.project_id if t else None!r}")
        if not text:
            return
        if not t:
            self._trace("_on_send ABORT: active_thread None")
            return

        # 1. Append user turn
        user_id = f"n_{uuid.uuid4().hex[:6]}"
        t.turns[user_id] = Turn(
            id=user_id, parent_id=t.current_leaf_id, speaker="user", text=text
        )
        if not t.root_id:
            t.root_id = user_id

        # 2. Create empty assistant turn — streaming thread will fill it
        team_id = f"n_{uuid.uuid4().hex[:6]}"
        team_turn = Turn(
            id=team_id, parent_id=user_id, speaker="team", text="",
        )
        t.turns[team_id] = team_turn
        t.current_leaf_id = team_id

        self.input_field.value = ""
        self.atref_panel_container.visible = False  # dismiss the @-picker if open
        # Show the header "working…" spinner immediately; cleared in dispatch finally.
        # Lock-guarded: workers decrement it from their own threads, so a plain
        # read-modify-write could lose a count and leave the spinner stuck.
        with self._chat_update_lock:
            self._chat_inflight = getattr(self, "_chat_inflight", 0) + 1
        self.refresh()
        # Persist the user turn now so it survives a crash mid-stream; the
        # assistant turn is saved again when the response completes.
        self._save_thread(t)

        # 3. Single general-agent call (composition A as of v0.2 — no more parallel team)
        self._dispatch_single(t, team_turn)

    def _format_prompt(self, messages: list[dict], model: str) -> str:
        """Render the exact payload sent to the brain as readable text. Note a
        message's content may be None (assistant turns that only carry
        tool_calls), so coerce before measuring/printing."""
        sep = "=" * 72
        total_chars = sum(len(m.get("content") or "") for m in messages)
        lines = [
            sep,
            f"PROMPT → {model}  ·  {len(messages)} messages  ·  "
            f"~{total_chars // 4} tokens ({total_chars} chars)",
            sep,
        ]
        for m in messages:
            role = m.get("role", "?")
            tcid = m.get("tool_call_id")
            lines.append(f"[{role}]" + (f" (tool_call_id={tcid})" if tcid else ""))
            content = m.get("content")
            if content:
                lines.append(content)
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {})
                lines.append(f"  → tool_call {fn.get('name')}({fn.get('arguments')})")
            lines.append("-" * 72)
        lines.append("END PROMPT")
        return "\n".join(lines)

    def _dump_prompt(self, messages: list[dict], model: str):
        """Capture the exact payload sent to the brain. Always stored for the
        in-app inspector (bug icon in the chat header); also printed to stderr
        when `WORKBENCH_DEBUG_PROMPTS=1` or `debug_prompts: true` in config."""
        text = self._format_prompt(messages, model)
        self._last_prompt_text = text  # always available to the in-app inspector
        env_on = os.environ.get("WORKBENCH_DEBUG_PROMPTS", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        if env_on or self.state.config.debug_prompts:
            print("\n" + text + "\n", file=sys.stderr, flush=True)

    def _show_prompt_dialog(self, e):
        """Open a dialog showing the last prompt sent — full system prompt
        (persona library + vault context + project meta) plus message history."""
        dlg = ft.AlertDialog(
            title=ft.Text("Last prompt sent to the model"),
            content=ft.Container(
                width=760,
                height=560,
                content=ft.Column(
                    controls=[
                        ft.Text(
                            self._last_prompt_text,
                            size=12,
                            selectable=True,
                            font_family="monospace",
                        )
                    ],
                    scroll=ft.ScrollMode.AUTO,
                    tight=True,
                ),
            ),
            actions=[ft.TextButton("Close", on_click=lambda _e: self.page.pop_dialog())],
            scrollable=True,
        )
        self.page.show_dialog(dlg)

    def _open_prompt_editor(self, e):
        """View / edit this thread's system prompt. Editing + Save freezes a
        per-thread override that replaces auto-assembly; Reset to auto clears it
        (next send rebuilds live from the vault)."""
        t = self.state.active_thread
        if not t:
            self._toast("Open a chat thread first.")
            return
        has_override = t.system_prompt_override is not None
        seed = t.system_prompt_override if has_override else self._assemble_system_prompt(t)
        field = ft.TextField(value=seed, multiline=True, min_lines=14, max_lines=24,
                             text_size=12, expand=True)
        status = ft.Text(
            "OVERRIDE active — auto-assembly (live vault context) is off for this thread."
            if has_override else
            "AUTO (live) — edit + Save to freeze a custom prompt for this thread.",
            size=11,
            color=ft.Colors.TERTIARY if has_override else ft.Colors.OUTLINE,
        )

        def save_override(_e):
            t.system_prompt_override = field.value or ""
            self._save_thread(t)
            self.page.pop_dialog()
            self._toast("Saved — this thread now uses your custom prompt.")

        def reset_auto(_e):
            t.system_prompt_override = None
            self._save_thread(t)
            self.page.pop_dialog()
            self._toast("Reset — this thread uses the live auto-assembled prompt.")

        def reseed(_e):
            field.value = self._assemble_system_prompt(t)
            self.page.update()

        dlg = ft.AlertDialog(
            title=ft.Text("Thread system prompt"),
            content=ft.Container(
                width=780, height=560,
                content=ft.Column([status, field], spacing=10, tight=True, expand=True),
            ),
            actions=[
                ft.TextButton("Re-seed from live", on_click=reseed),
                ft.TextButton("Reset to auto", on_click=reset_auto),
                ft.TextButton("Save override", on_click=save_override),
                ft.TextButton("Close", on_click=lambda _e: self.page.pop_dialog()),
            ],
            scrollable=True,
        )
        self.page.show_dialog(dlg)

    def _general_agent(self) -> Agent:
        """The 'Workbench' agent, or a fallback if its seed file is missing."""
        general = self.state.get_agent("Workbench")
        if general is not None:
            return general
        return Agent(
            name="Workbench",
            role=("You are Workbench, the user's personal AI workspace assistant. "
                  "Pragmatic, opinionated, simple-first. Push back when something "
                  "is over-engineered or premature. Surface tradeoffs. Keep "
                  "responses tight."),
            model=self.state.config.default_model,
        )

    def _working_dir_info(self, project: Optional[Project]):
        """(summary string for the prompt, resolved Path or None) for a project."""
        if not (project and project.working_dir):
            return None, None
        resolved = _resolve_working_dir(project.working_dir)
        if resolved and resolved.exists():
            git = _git_short_status(resolved)
            summary = f"{resolved}" + (f" ({git})" if git else "")
            return summary, resolved
        return f"{project.working_dir} (not present on this machine)", None

    def _assemble_system_prompt(self, thread: Thread) -> str:
        """The auto-assembled system prompt for a thread (no override, no history) —
        used to seed/preview the per-thread prompt editor."""
        general = self._general_agent()
        project = self.state.project_of_thread(thread.id)
        wd_summary, _ = self._working_dir_info(project)
        cfg = self.state.config
        msgs = build_messages_for_general_agent(
            general, self.state.agents, project, wd_summary, [],
            system_preamble=cfg.system_preamble,
            context_token_cap=cfg.context_token_cap,
            tools_available=cfg.tools_enabled,
            delegate_available=cfg.tools_enabled and cfg.delegate_enabled,
            publish_available=cfg.tools_enabled and cfg.publish_enabled,
        )
        return msgs[0]["content"]

    def _tool_action_summary(self, name: str, args: dict) -> str:
        """One-line human description of what a tool call will do, for the confirm
        dialog. Mirrors the friendly verbs used by the inline tool markers."""
        if name == "write_vault_note":
            return f"Write file:  {args.get('path', '?')}"
        if name == "move_note":
            return f"Move:  {args.get('src', '?')}  →  {args.get('dst', '?')}"
        if name == "run_shell":
            return f"Run shell:  {args.get('command', '?')}"
        if name == "delegate_to_claude_code":
            return f"Delegate to CLI agent:\n{args.get('task', '?')}"
        return f"{name}({args})"

    def _ask_tool_permission(self, thread: Thread, name: str, args: dict) -> bool:
        """Gate a tool call when Config.tool_confirm is on. Returns True to run it,
        False to decline. Read-only tools and trust mode return True immediately.

        Threading: the dispatch loop runs on a daemon thread that can't show UI in
        Flet 0.85, so the dialog is marshalled onto the event loop (page.run_task)
        and this worker thread BLOCKS on an Event until the user answers. The
        button callbacks (which run on the event loop) record the choice and set
        the Event, releasing the worker. Fails safe to DENY if the dialog can't be
        shown."""
        cfg = self.state.config
        if not getattr(cfg, "tool_confirm", False) or name not in MUTATING_TOOLS:
            return True
        allow = self._tool_allow.setdefault(thread.id, set())
        if name in allow:
            return True

        decision = {"allow": False}
        ev = threading.Event()

        async def _show():
            try:
                remember = ft.Checkbox(
                    label=f"Always allow {name} in this thread", value=False)

                def _decide(ok: bool):
                    decision["allow"] = ok
                    if ok and remember.value:
                        allow.add(name)
                    self.page.pop_dialog()
                    ev.set()

                dlg = ft.AlertDialog(
                    modal=True,
                    title=ft.Text("Allow this action?"),
                    content=ft.Column([
                        ft.Text("The Workbench agent wants to:", size=12,
                                color=ft.Colors.OUTLINE),
                        ft.Text(self._tool_action_summary(name, args),
                                selectable=True),
                        remember,
                    ], tight=True, spacing=10),
                    actions=[
                        ft.TextButton("Deny", on_click=lambda _e: _decide(False)),
                        ft.FilledButton("Allow", on_click=lambda _e: _decide(True)),
                    ],
                )
                self.page.show_dialog(dlg)
            except Exception as ex:
                self._trace(f"tool-confirm dialog failed: {ex!r}")
                ev.set()  # fail safe → deny (decision stays False)

        try:
            self.page.run_task(_show)
        except Exception as ex:
            self._trace(f"tool-confirm schedule failed: {ex!r}")
            return False
        ev.wait()  # block the worker until the user (or the fail-safe) answers
        return decision["allow"]

    def _dispatch_single(self, thread: Thread, team_turn: Turn):
        """One LLM call to the general agent ('Workbench'); other personas referenced
        in its system prompt and channeled in-conversation."""
        general = self._general_agent()
        project = self.state.project_of_thread(thread.id)
        working_dir_summary, working_dir_path = self._working_dir_info(project)

        history = thread.active_path()[:-1]
        cfg = self.state.config
        messages = build_messages_for_general_agent(
            general, self.state.agents, project, working_dir_summary, history,
            system_preamble=cfg.system_preamble,
            context_token_cap=cfg.context_token_cap,
            tools_available=cfg.tools_enabled,
            delegate_available=cfg.tools_enabled and cfg.delegate_enabled,
            publish_available=cfg.tools_enabled and cfg.publish_enabled,
            system_override=thread.system_prompt_override,
        )
        self._dump_prompt(messages, general.model)
        temperature = self._chat_temperature  # per-message, set via the input-bar control
        max_tokens = cfg.max_tokens
        # Defensive: clear any stale coalescing latch so a new send always updates
        # even if a prior task somehow left it set (belt-and-suspenders; the drain
        # loop in _schedule_chat_update already self-heals).
        with self._chat_update_lock:
            self._chat_refresh_scheduled = False
            self._chat_dirty = False

        lock = threading.Lock()
        accumulated = [""]
        done = [False]

        def render():
            with lock:
                has_activity = accumulated[0] or done[0] or team_turn.tool_steps
                team_turn.text = accumulated[0] if has_activity else "thinking…"
            # The streaming worker thread can't flush UI directly in Flet 0.85 —
            # marshal the refresh onto the event loop (coalesced).
            self._schedule_chat_update()

        tool_ctx = ToolContext(
            vault_root=VAULT_PATH, working_dir=working_dir_path,
            delegate_enabled=cfg.delegate_enabled,
            delegate_command=cfg.delegate_command,
            delegate_permission_mode=cfg.delegate_permission_mode,
            delegate_allowed_tools=cfg.delegate_allowed_tools,
            delegate_timeout=cfg.delegate_timeout,
            publish_enabled=cfg.publish_enabled,
            publish_fn=self._tool_publish_note,
        )
        tools = schemas_for(tool_ctx) if cfg.tools_enabled else None

        def run():
            try:
                model_str = (self.state.config.chat_model or "").strip() or general.model
                brain, model = brain_for(model_str, self.state.config)
                eff_max_tokens = clamp_max_tokens(model, max_tokens)
                if eff_max_tokens != max_tokens:
                    self._trace(f"dispatch: clamped max_tokens {max_tokens} → "
                                f"{eff_max_tokens} for {model}")
                self._trace(f"dispatch: brain={type(brain).__name__} model={model} "
                            f"tools={'on' if tools else 'off'} msgs={len(messages)}")
                convo = list(messages)  # working copy extended with tool turns
                MAX_TOOL_ITERS = 8
                for _ in range(MAX_TOOL_ITERS):
                    calls = None
                    turn_text = ""
                    for kind, payload in brain.stream_with_tools(
                            convo, model, tools=tools,
                            temperature=temperature, max_tokens=eff_max_tokens):
                        if kind == "text":
                            turn_text += payload
                            with lock:
                                accumulated[0] += payload
                            render()
                        elif kind == "tool_calls":
                            calls = payload
                    if not calls:
                        break
                    # Record the assistant's tool-call message, then run each tool.
                    convo.append({
                        "role": "assistant",
                        "content": turn_text or None,
                        "tool_calls": [
                            {"id": c["id"], "type": "function",
                             "function": {"name": c["name"], "arguments": c["arguments"]}}
                            for c in calls
                        ],
                    })
                    for c in calls:
                        try:
                            args = json.loads(c["arguments"] or "{}")
                        except Exception:
                            args = {}
                        # Record the call now (result filled in after) so the live
                        # turn shows it; persisted on team_turn for faithful replay.
                        # text_offset anchors the call to its spot in the streamed
                        # text so the turn can show an inline marker right where the
                        # agent paused to use the tool (mid-sentence flow).
                        with lock:
                            step = {"id": c["id"], "name": c["name"],
                                    "arguments": c["arguments"] or "{}", "result": "…",
                                    "text_offset": len(accumulated[0])}
                            team_turn.tool_steps.append(step)
                        render()
                        if self._ask_tool_permission(thread, c["name"], args):
                            result = execute_tool(c["name"], args, tool_ctx)
                        else:
                            result = "[user declined this action]"
                        with lock:
                            step["result"] = result
                        render()
                        convo.append({"role": "tool", "tool_call_id": c["id"],
                                      "content": result})
            except Exception as ex:
                self._trace(f"dispatch error: {ex!r}")
                with lock:
                    if not accumulated[0]:
                        accumulated[0] = f"(error: {ex})"
            finally:
                with lock:
                    done[0] = True
                # Clear this call's slot on the header busy spinner (lock-guarded:
                # other workers may be touching the same counter concurrently).
                with self._chat_update_lock:
                    self._chat_inflight = max(0, getattr(self, "_chat_inflight", 1) - 1)
                self._trace(f"dispatch done: chars={len(accumulated[0])}")
                render()
                # Response complete (or errored) — persist the full tree.
                self._save_thread(thread)

        render()  # show "thinking…" immediately
        threading.Thread(target=run, daemon=True).start()

    def _trace(self, msg: str):
        # Temporary lifecycle trace (chasing the Home/scratch send bug). Remove
        # once resolved.
        print(f"[wb] {msg}", file=sys.stderr, flush=True)

    def _schedule_chat_update(self):
        """Refresh the chat view on Flet's event loop. Safe to call from the
        streaming worker threads (a direct page.update() there doesn't flush in
        Flet 0.85). Coalesces bursts so rapid token chunks don't flood the loop.

        Robustness (was the "reply doesn't show until I switch tabs" bug):
        - All coalescing flags are guarded by `_chat_update_lock` — they were
          previously read/written from the worker thread AND the event loop with
          no lock, a classic check-then-set race.
        - A single running refresh DRAINS the dirty flag in a loop, so the final
          chunk always paints AND the scheduled-latch is only held while a task is
          genuinely running. If the latch were ever left stuck True (the old bug),
          every subsequent render() returned early → frozen view until a full
          refresh (tab switch) bypassed it.
        - The run_task future is retained on self, so asyncio can't garbage-
          collect the coroutine before it runs (the documented footgun that left
          the latch stuck in the first place)."""
        with self._chat_update_lock:
            self._chat_dirty = True
            if self._chat_refresh_scheduled:
                return  # a running task will pick up the dirty flag and drain it
            self._chat_refresh_scheduled = True

        def _do_refresh():
            # Drain: keep painting while new chunks keep arriving, then release
            # the latch. Doing the release here (not in the scheduler) is what
            # makes the latch self-healing — it can never outlive the task.
            while True:
                with self._chat_update_lock:
                    if not self._chat_dirty:
                        self._chat_refresh_scheduled = False
                        return
                    self._chat_dirty = False
                try:
                    self._refresh_thread_view()
                    self._refresh_minimap()
                    ring = getattr(self, "chat_busy_ring", None)
                    if ring is not None:
                        ring.visible = getattr(self, "_chat_inflight", 0) > 0
                    # auto_scroll (toggled by _on_thread_scroll) follows the stream
                    # only when the user is at the bottom; page.update() applies it.
                    self.page.update()
                except Exception:
                    pass

        async def _runner():
            _do_refresh()

        try:
            self._chat_refresh_task = self.page.run_task(_runner)
        except Exception:
            # No reachable loop — run inline; _do_refresh clears the latch itself.
            _do_refresh()

    def _toggle_pin(self, turn_id: str):
        t = self.state.active_thread
        if t and turn_id in t.turns:
            t.turns[turn_id].pinned = not t.turns[turn_id].pinned
            self._save_thread(t)
            self.refresh()

    def _continue_from(self, turn_id: str):
        t = self.state.active_thread
        if t and turn_id in t.turns:
            t.current_leaf_id = turn_id
            self._save_thread(t)
            self.refresh()

    def _regenerate(self, turn_id: str):
        """Re-run the agent for a team turn. Creates an empty sibling under the
        same parent (a persistent branch — the original response stays in the
        tree) and dispatches for real. _dispatch_single rebuilds history from
        active_path()[:-1], i.e. up to the parent user turn."""
        t = self.state.active_thread
        if not t or turn_id not in t.turns:
            return
        original = t.turns[turn_id]
        if original.speaker != "team":
            return
        new_id = f"n_{uuid.uuid4().hex[:6]}"
        new_turn = Turn(
            id=new_id, parent_id=original.parent_id, speaker="team", text="",
        )
        t.turns[new_id] = new_turn
        t.current_leaf_id = new_id
        self.refresh()
        # Persist the branch now so it survives a crash mid-stream; the response
        # is saved again on completion inside _dispatch_single.
        self._save_thread(t)
        self._dispatch_single(t, new_turn)

    def _jump_to(self, turn_id: str):
        t = self.state.active_thread
        if t and turn_id in t.turns:
            t.current_leaf_id = turn_id
            self.refresh()


def main(page: ft.Page):
    WorkbenchApp(page).build()


ft.run(main)
