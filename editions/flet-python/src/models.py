"""Data model for Workbench — pure dataclasses + AppState.

These carry no UI (no flet) and no I/O: vault loading lives in vault.py, which
builds these out of `.md` files. AppState is the in-memory root the UI renders.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from config import Config


@dataclass
class Agent:
    name: str
    role: str  # full system-prompt body (markdown)
    model: str = "anthropic/claude-opus-4-7"
    icon: object = None  # ft.Icons.* value; resolved from frontmatter `icon:` or defaulted
    source_path: Optional[str] = None  # path to the .md file in the vault


@dataclass
class Turn:
    id: str
    parent_id: Optional[str]
    speaker: str  # "user" | "team"
    text: str
    pinned: bool = False
    # Structured record of tools the agent ran during this (team) turn — one
    # dict per call: {"id", "name", "arguments"(JSON str), "result"}. Persisted
    # and replayed into history as real tool_calls/tool messages so the model
    # sees the genuine exchange (not narrated prose) on later turns, and so the
    # UI can show what actually happened + each tool's result.
    tool_steps: list[dict] = field(default_factory=list)


@dataclass
class Thread:
    id: str
    name: str
    project_id: str
    team: list[Agent] = field(default_factory=list)
    turns: dict[str, Turn] = field(default_factory=dict)
    root_id: Optional[str] = None
    current_leaf_id: Optional[str] = None
    # When set, this frozen system prompt REPLACES the auto-assembled one for
    # this thread (user edited it). None = auto-assembly (live vault context).
    system_prompt_override: Optional[str] = None

    def active_path(self) -> list["Turn"]:
        if not self.current_leaf_id or self.current_leaf_id not in self.turns:
            return []
        path: list[Turn] = []
        cur: Optional[Turn] = self.turns[self.current_leaf_id]
        while cur is not None:
            path.append(cur)
            cur = self.turns.get(cur.parent_id) if cur.parent_id else None
        return list(reversed(path))


@dataclass
class Area:
    name: str  # the `area:` frontmatter value — projects reference this
    folder: str  # absolute path to the area folder
    description: str  # first non-empty body line
    status: str = "active"
    source_path: str = ""


@dataclass
class Project:
    id: str
    name: str
    vault_folder: str
    status: str = "idea"  # idea | active | persist | pause | pivot | done
    hypothesis: str = ""
    area: str = ""  # `area:` frontmatter value — matches Area.name; "" if uncategorized
    modes: list[str] = field(default_factory=list)  # execution lenses: publish/create/networking/... lowercased
    tags: list[str] = field(default_factory=list)  # frontmatter `tags:`, lowercased
    working_dir: str = ""  # raw frontmatter value (may contain ~); resolve before use
    context_files: list[str] = field(default_factory=list)
    context_tokens: int = 0
    threads: list[Thread] = field(default_factory=list)
    # Demand probe (see _System/Methods/Kaizen Loop.md → "Demand probes"): a
    # low-touch active project listening for a bite. Detected from the
    # `demand-probe` tag/mode. `probe` holds the optional tracking block
    # (last/bites/channel/cadence_days) used for the Home glance.
    is_probe: bool = False
    probe: dict = field(default_factory=dict)
    # `review:` frontmatter date — when to do the next Persist/Pause/Pivot pass.
    # Drives the Reviews due board. None = no date set (→ "Needs attention").
    review: Optional[date] = None
    # Kaizen frontmatter surfaced in the overview metadata strip.
    scope: str = ""               # tiny | small | big
    started: Optional[date] = None
    micro_commitment: str = ""    # the 2-minute starter (frontmatter `micro-commitment`)


# Content types beyond Thread: parsed from the project's vault folder on demand.

@dataclass
class Task:
    text: str
    state: str  # "todo" | "doing" | "paused" | "done" (see vault.TASK_STATES)
    source_path: str  # absolute path to the .md file the checkbox lives in
    line_number: int  # 1-indexed
    # Populated by the global scan so a task can name its home project on the
    # board; left blank for per-project scans (the project is already implied).
    project_id: str = ""
    project_name: str = ""

    @property
    def checked(self) -> bool:
        """Back-compat for the open/done callers that predate multi-state."""
        return self.state == "done"


@dataclass
class Note:
    name: str  # filename stem
    path: str
    note_type: str  # frontmatter `type:` lowercased; "" if none
    summary: str  # first non-empty body line, capped


@dataclass
class FileItem:
    name: str
    path: str
    suffix: str  # lowercased file extension including the dot
    size: int  # bytes


@dataclass
class ProjectContent:
    """Everything in a project's folder, classified by content type."""
    tasks: list[Task] = field(default_factory=list)
    inbox: list[Note] = field(default_factory=list)   # notes in <project>/Inbox/
    posts: list[Note] = field(default_factory=list)   # notes typed post/journal
    wiki: list[Note] = field(default_factory=list)    # other notes (reference)
    files: list[FileItem] = field(default_factory=list)  # non-markdown


@dataclass
class InboxItem:
    """One file in vault/00_Inbox/ — pre-triage capture."""
    name: str            # filename stem
    path: str            # absolute path
    size: int            # bytes
    mtime: float         # last modified (unix ts)
    note_type: str       # frontmatter `type:` lowercased; "" if none
    summary: str         # first non-empty body line, capped


@dataclass
class VaultEntry:
    """A markdown note inside a top-level vault surface (Resources, People, ...).
    Parallel to InboxItem but tracks subfolder so nested layouts like 30_Resources/People/* render sensibly."""
    name: str            # filename stem
    path: str            # absolute path
    subfolder: str       # relative folder under the surface root; "" at root
    size: int            # bytes
    mtime: float         # last modified (unix ts)
    note_type: str       # frontmatter `type:` lowercased; "" if none
    summary: str         # first non-empty body line, capped


@dataclass(frozen=True)
class OpenTab:
    """One open view in the main area. kind is extensible: chat / overview / agent / ..."""
    kind: str  # "chat" | "overview" | "agent"
    ref_id: str  # thread id | project id | agent name


@dataclass
class AppState:
    projects: list[Project] = field(default_factory=list)
    # Top-level chats not tied to any project (started from Home). project_id="".
    scratch_threads: list["Thread"] = field(default_factory=list)
    agents: list[Agent] = field(default_factory=list)
    areas: list[Area] = field(default_factory=list)
    inbox_items: list[InboxItem] = field(default_factory=list)
    selected_inbox_path: Optional[str] = None  # which item is open in the preview pane
    resources: list[VaultEntry] = field(default_factory=list)
    selected_resource_path: Optional[str] = None
    people: list[VaultEntry] = field(default_factory=list)
    selected_person_path: Optional[str] = None
    people_filter: str = ""  # case-insensitive substring filter applied to the People list
    areas_filter: str = ""  # case-insensitive substring filter on project titles in the sidebar AREAS tree
    home_mode_filter: str = ""  # currently selected Home chip: "" (All) | "publish" | "create" | "networking"
    open_tabs: list[OpenTab] = field(default_factory=list)
    active_tab: Optional[OpenTab] = None
    minimap_open: bool = False
    config: Config = field(default_factory=Config)

    def get_project(self, pid: Optional[str]) -> Optional[Project]:
        return next((p for p in self.projects if p.id == pid), None) if pid else None

    def get_agent(self, name: Optional[str]) -> Optional[Agent]:
        return next((a for a in self.agents if a.name == name), None) if name else None

    def get_area(self, name: Optional[str]) -> Optional[Area]:
        return next((a for a in self.areas if a.name == name), None) if name else None

    def projects_by_area(self) -> dict[str, list[Project]]:
        """Group projects by their `area:` field. Empty area → '(uncategorized)'."""
        groups: dict[str, list[Project]] = {}
        for p in self.projects:
            key = p.area if p.area else "(uncategorized)"
            groups.setdefault(key, []).append(p)
        return groups

    def get_thread(self, tid: Optional[str]) -> Optional[Thread]:
        if not tid:
            return None
        for p in self.projects:
            for t in p.threads:
                if t.id == tid:
                    return t
        for t in self.scratch_threads:
            if t.id == tid:
                return t
        return None

    def project_of_thread(self, tid: str) -> Optional[Project]:
        for p in self.projects:
            for t in p.threads:
                if t.id == tid:
                    return p
        return None

    def is_thread_open(self, tid: str) -> bool:
        return any(t.kind == "chat" and t.ref_id == tid for t in self.open_tabs)

    @property
    def active_thread_id(self) -> Optional[str]:
        if self.active_tab and self.active_tab.kind == "chat":
            return self.active_tab.ref_id
        return None

    @property
    def active_project_id(self) -> Optional[str]:
        if not self.active_tab:
            return None
        if self.active_tab.kind == "overview":
            return self.active_tab.ref_id
        if self.active_tab.kind == "chat":
            p = self.project_of_thread(self.active_tab.ref_id)
            return p.id if p else None
        return None

    @property
    def active_project(self) -> Optional[Project]:
        return self.get_project(self.active_project_id)

    @property
    def active_thread(self) -> Optional[Thread]:
        return self.get_thread(self.active_thread_id)
