"""User config — API keys + per-install preferences.

Stored as JSON at ~/.workbench/config.json (chmod 600). Keep it simple for v0;
move secrets to OS keychain (keyring) later if this leaves a personal machine.
"""

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path


CONFIG_PATH = Path.home() / ".workbench" / "config.json"


@dataclass
class Config:
    # Vault root. Empty = use the bundled `./vault` symlink (resolved through the
    # link to its real target). Set an absolute path to point at another vault and
    # reload from Settings. `~` is expanded.
    vault_path: str = ""
    openrouter_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434/v1"
    # When True, ALL agent calls route through MockBrain regardless of the agent's
    # `model:` prefix. Useful during dev to avoid burning OpenRouter tokens on
    # every UI change.
    force_mock: bool = True
    # When True, print the exact message payload sent to the brain (system prompt
    # + history) to stderr before each call. Env WORKBENCH_DEBUG_PROMPTS=1 also
    # turns it on for a single run without editing this file.
    debug_prompts: bool = False
    # Commands for opening a project's working_dir. `{path}` is substituted; if
    # the placeholder is absent the path is appended. Change in Settings to e.g.
    # `cursor {path}`, `code {path}`, `nvim {path}` (under a terminal), `lazyvim`, etc.
    editor_command: str = "zed {path}"
    terminal_command: str = "gnome-terminal --working-directory={path}"
    # Fallback model when an agent file doesn't declare its own (rare).
    default_model: str = "anthropic/claude-opus-4-7"
    # Model used for the single-agent chat — overrides the Workbench agent's own
    # `model:` so you can switch models from Settings without editing the agent
    # file. Must be a valid OpenRouter slug (see https://openrouter.ai/models).
    chat_model: str = "anthropic/claude-opus-4-7"
    # Default sampling temperature for chat. Seeds the per-message control in the
    # input bar; that control can override it for a single send.
    temperature: float = 1.0
    # Max tokens for a chat response. 0 = don't send a cap (let the model/provider
    # default decide).
    max_tokens: int = 0
    # Token budget for vault context injected into the system prompt. ~4 chars/token.
    context_token_cap: int = 30_000
    # Reviews due-board column day-boundaries (the "next [n1]d / next [n2]d" lenses).
    review_window_n1: int = 7
    review_window_n2: int = 30
    # Persistent instructions prepended to the chat agent's system prompt (your
    # voice, language, standing rules). Empty = none.
    system_preamble: str = ""
    # Let the chat agent call tools (read/write/list/move vault files, run_shell).
    # Trust mode — tools act directly on the vault / working_dir.
    tools_enabled: bool = True
    # Render assistant replies as Markdown (headings, lists, code, bold) instead of
    # plain text. Toggle live from the chat header; persisted here. User turns stay
    # plain so your own `#`/`*` don't get reinterpreted.
    render_markdown: bool = True
    # --- Delegation to CLI agents (Claude Code / Codex) ---
    # Two paths, different trust models:
    #  • Interactive (a button): opens the CLI in a real terminal in the project's
    #    working_dir — full normal prompts, you drive it. Always available.
    #  • Headless (an agent tool): `delegate_to_claude_code` runs the CLI with -p
    #    in the background and the chat agent polls for the result. Headless has no
    #    TTY, so it can't prompt — it runs under `delegate_permission_mode` (or an
    #    explicit allow-list). Off by default; opt-in here.
    delegate_enabled: bool = False  # gates the headless agent tool only
    delegate_command: str = "claude"  # CLI binary for headless delegation (base of `<cli> -p ...`)
    # Headless permission handling. Non-empty allow-list takes precedence;
    # otherwise fall back to permission_mode. `bypassPermissions` = full autonomy
    # in working_dir (matches the locked trust-mode decision; you manage git).
    delegate_permission_mode: str = "bypassPermissions"
    delegate_allowed_tools: str = ""  # space/comma list, e.g. "Read Grep Glob Edit Write"
    delegate_timeout: int = 600  # seconds before a headless run is killed
    # Template to open a CLI in a real terminal (interactive path). {dir} = the
    # working_dir, {cmd} = the CLI to start (e.g. `claude`, `codex`). `exec bash`
    # keeps the terminal open after the CLI exits so you can read the result.
    delegate_terminal_command: str = (
        "gnome-terminal --working-directory={dir} -- bash -c '{cmd}; exec bash'"
    )
    # Git TUI launched by the "Open in lazygit" buttons (vault on Home + each
    # project's working_dir card). It's a terminal app, so it rides the same
    # interactive terminal template above ({dir}/{cmd}). Swap for gitui/tig/etc.
    git_ui_command: str = "lazygit"
    # CLI sessions (Tier 1): "Open CLI session" launches an interactive coding-
    # agent CLI in a terminal, rooted in the project's working_dir. Vault context
    # is granted by symlinking the parts you pick into an in-cwd folder
    # (cli_session_context_dir) — CLI-agnostic, inspectable, gitignored, and
    # NEVER the whole vault unless you choose it. Verified Claude Code reads
    # through such symlinks (it doesn't canonicalize them out of cwd). Reuses the
    # delegate_terminal_command terminal wrapper.
    cli_session_command: str = "claude"
    cli_session_context_dir: str = ".workbench-context"
    # Where the "Open CLI session" image uploader copies attached images (a
    # gitignored folder in working_dir, kept separate from the vault-context
    # symlink dir). Images are referenced by their cwd-relative path.
    cli_session_image_dir: str = ".workbench-media"
    # Theme — Workbench metaphor: a flat work surface (the bench) sitting in a
    # darker room. Tabs label sections of the surface; the surface itself is one
    # unified color where things are laid out (cards, chats, notes).
    #   main_bg_color       = room / surroundings (sidebar + page bg)
    #   tab_inactive_bg_color = unselected section labels
    #   view_bg_color       = the workbench surface itself (active section + view body)
    # Platinum 9 light theme (see _System/Methods/Workbench UI.md):
    main_bg_color: str = "#CCCCCC"          # canvas — room / surroundings
    view_bg_color: str = "#EEEEEE"          # panel — the workbench surface (active tab merges in)
    tab_inactive_bg_color: str = "#DDDDDD"  # face — unselected section labels
    # (page_bg_color was removed in v0.2 — single-surface workbench feel, no inner page)
    # --- Title style (the big main heading on project / area / settings views) ---
    # Fonts/size/color only for now — NOT background (kept clean on purpose). Pick a
    # preset in Settings → TITLE STYLE (presets in title_themes.json) or hand-tune.
    # The renderer (`_title_text`) reads these six fields directly; `title_theme` is
    # just the label of the last-applied preset ("Custom" once you edit a field).
    title_theme: str = "Newsreader — big & light"
    title_font: str = "Newsreader"
    title_size: int = 54
    title_color: str = ""        # "" = inherit the view's default text color
    title_bold: bool = False
    title_italic: bool = False
    title_letter_spacing: float = 0.0


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        return Config()
    try:
        data = json.loads(CONFIG_PATH.read_text())
        known = {k: v for k, v in data.items()
                 if k in {f.name for f in dataclasses.fields(Config)}}
        return Config(**known)
    except Exception:
        return Config()


def save_config(config: Config) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(dataclasses.asdict(config), indent=2))
    try:
        CONFIG_PATH.chmod(0o600)
    except Exception:
        pass


# --- Telegram capture daemon state -----------------------------------------
# Kept in its own file (not in Config) for two reasons: the bot token is a
# secret we don't want duplicated alongside the OpenRouter key, and `owner_id`
# is written by the daemon itself (the single-owner claim flow) — so the app and
# the daemon share one small file as the source of truth. Format matches what
# telegram_capture.py reads: {"bot_token": str, "owner_id": int}.
TELEGRAM_CONFIG_PATH = Path.home() / ".workbench" / "telegram.json"


def load_telegram_cfg() -> dict:
    """Read the telegram daemon's config; {} if absent/unreadable."""
    try:
        data = json.loads(TELEGRAM_CONFIG_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_telegram_cfg(cfg: dict) -> None:
    """Persist the telegram daemon's config (chmod 600). Best-effort."""
    TELEGRAM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    TELEGRAM_CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        TELEGRAM_CONFIG_PATH.chmod(0o600)
    except Exception:
        pass


# --- Title-style presets ----------------------------------------------------
# Pre-made "big heading" looks for the title picker. Ship a bundled set next to
# this module; let the user add their own (or override) in a per-install file at
# ~/.workbench/title_themes.json without touching the repo. Each preset is a dict:
#   {name, font, size, color, bold, italic, letter_spacing}
# `color: ""` inherits the view's default text color. `font` must be one of the
# registered families (see FONT_SOURCES in main.py).
BUNDLED_TITLE_THEMES_PATH = Path(__file__).parent / "title_themes.json"
USER_TITLE_THEMES_PATH = Path.home() / ".workbench" / "title_themes.json"

_TITLE_THEME_DEFAULTS = {
    "color": "", "bold": False, "italic": False, "letter_spacing": 0.0,
}


def _normalize_theme(t: dict) -> dict:
    """Fill missing keys with defaults; coerce types. Returns None for junk."""
    if not isinstance(t, dict) or not t.get("name") or not t.get("font"):
        return None
    out = {"name": str(t["name"]), "font": str(t["font"]),
           "size": int(t.get("size", 40))}
    out["color"] = str(t.get("color", _TITLE_THEME_DEFAULTS["color"]))
    out["bold"] = bool(t.get("bold", _TITLE_THEME_DEFAULTS["bold"]))
    out["italic"] = bool(t.get("italic", _TITLE_THEME_DEFAULTS["italic"]))
    try:
        out["letter_spacing"] = float(t.get("letter_spacing", 0.0))
    except (TypeError, ValueError):
        out["letter_spacing"] = 0.0
    return out


def load_title_themes() -> list:
    """Bundled presets + any user presets (appended; user file may also override
    a bundled preset by reusing its exact name). Always returns a non-empty list."""
    presets: list = []
    seen: dict = {}
    for path in (BUNDLED_TITLE_THEMES_PATH, USER_TITLE_THEMES_PATH):
        try:
            raw = json.loads(path.read_text())
        except Exception:
            continue
        if not isinstance(raw, list):
            continue
        for item in raw:
            norm = _normalize_theme(item)
            if not norm:
                continue
            if norm["name"] in seen:           # user file overrides same-named bundled
                presets[seen[norm["name"]]] = norm
            else:
                seen[norm["name"]] = len(presets)
                presets.append(norm)
    if not presets:  # never hand back an empty picker
        presets = [{"name": "Newsreader — big & light", "font": "Newsreader",
                    "size": 54, **_TITLE_THEME_DEFAULTS}]
    return presets
