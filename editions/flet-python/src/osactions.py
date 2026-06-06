"""OS / shell integration — launch external tools and inspect the working dir.

The "Workbench is a hub, not an editor" layer: open files in the editor / a
terminal / Obsidian / lazygit, reveal in the file manager, and read a working
dir's git + file summary. All non-blocking spawns; every launcher returns an
error string (for a toast) or None on success.
"""

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Optional

from config import Config


def _resolve_working_dir(raw: str) -> Optional[Path]:
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except Exception:
        return None


_WD_SCAN_IGNORE = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", "dist", "build", ".next", "target",
}


def _scan_working_dir(path: Path) -> dict:
    """Walk path, summarising files. Skips common build/cache dirs."""
    if not path.exists() or not path.is_dir():
        return {"exists": False, "missing": not path.exists()}
    counts: Counter = Counter()
    total_size = 0
    last_mtime = 0.0
    file_count = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _WD_SCAN_IGNORE]
        for f in files:
            fp = Path(root) / f
            try:
                st = fp.stat()
                total_size += st.st_size
                last_mtime = max(last_mtime, st.st_mtime)
                file_count += 1
                counts[fp.suffix.lower() or "(no ext)"] += 1
            except Exception:
                pass
    return {
        "exists": True,
        "file_count": file_count,
        "size": total_size,
        "last_mtime": last_mtime,
        "by_suffix": dict(counts.most_common(8)),
    }


def _git_short_status(path: Path) -> Optional[str]:
    """Tiny `git status` summary if the dir is a git repo. Returns None otherwise."""
    if not (path / ".git").exists():
        return None
    try:
        r = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain=v1", "--branch"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return None
        lines = r.stdout.splitlines()
        if not lines:
            return "(no git output)"
        branch_line = lines[0]
        change_count = len(lines) - 1
        m = re.match(r"## ([^.\s]+)(\.\.\.)?(\S*)\s*(\[.*\])?", branch_line)
        branch = m.group(1) if m else "?"
        sync_info = (m.group(4) or "").strip("[]") if m else ""
        parts = [branch]
        if change_count:
            parts.append(f"{change_count} changes")
        else:
            parts.append("clean")
        if sync_info:
            parts.append(sync_info)
        return " · ".join(parts)
    except Exception:
        return None


def _format_subprocess_cmd(template: str, path: Path) -> list[str]:
    p = str(path)
    if "{path}" in template:
        rendered = template.replace("{path}", p)
        return shlex.split(rendered)
    # No placeholder — append the path
    return shlex.split(template) + [p]


def _spawn(cmd: list[str]) -> Optional[str]:
    """Spawn a non-blocking subprocess. Returns error message on failure, else None."""
    try:
        subprocess.Popen(cmd, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return None
    except FileNotFoundError:
        return f"command not found: {cmd[0] if cmd else '?'}"
    except Exception as ex:
        return str(ex)


def open_in_editor(config: Config, path: Path) -> Optional[str]:
    return _spawn(_format_subprocess_cmd(config.editor_command, path))


def open_in_terminal(config: Config, path: Path) -> Optional[str]:
    return _spawn(_format_subprocess_cmd(config.terminal_command, path))


def delegate_in_terminal(config: Config, working_dir: Path, cli: str = "") -> Optional[str]:
    """Open a CLI agent in a real terminal in working_dir — the interactive
    delegation path (full normal prompts, the user drives it). {dir}/{cmd} are
    substituted into delegate_terminal_command; {cmd} defaults to the headless
    delegate_command binary so both paths use the same CLI."""
    cmd = cli or config.delegate_command
    template = config.delegate_terminal_command
    rendered = template.replace("{dir}", shlex.quote(str(working_dir))).replace("{cmd}", cmd)
    return _spawn(shlex.split(rendered))


def reveal_in_files(path: Path) -> Optional[str]:
    return _spawn(["xdg-open", str(path)])


def _is_git_repo(path: Path) -> bool:
    """True if path is inside a git work tree. Cheap — used to gate the
    'Open in lazygit' buttons so they only show on real repos."""
    if not path or not path.exists():
        return False
    try:
        r = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


def open_in_git_ui(config: Config, repo_dir: Path) -> Optional[str]:
    """Launch the configured git TUI (lazygit) in a terminal at repo_dir.
    Reuses the interactive delegate_terminal_command template — lazygit is a
    terminal app, same as the CLI-in-a-terminal path. Returns an error string
    (so the caller can toast) or None on success. Checks the binary exists
    first to give an honest 'not found' instead of a silent terminal flash."""
    cli = (config.git_ui_command or "lazygit").strip()
    binary = shlex.split(cli)[0] if cli else "lazygit"
    if not shutil.which(binary):
        return (f"{binary} not found — install it "
                f"(e.g. `brew install {binary}` / your package manager)")
    template = config.delegate_terminal_command
    rendered = template.replace("{dir}", shlex.quote(str(repo_dir))).replace("{cmd}", cli)
    return _spawn(shlex.split(rendered))


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic"}


def list_session_images(working_dir: Path, image_dir: str) -> list[dict]:
    """List images already in working_dir/<image_dir>/, sorted by name. Each
    entry: {name, abs, rel} where rel is the cwd-relative path to reference."""
    d = working_dir / (image_dir or ".workbench-media")
    if not d.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(d.iterdir()):
        if p.is_file() and p.suffix.lower() in _IMAGE_EXTS:
            out.append({"name": p.name, "abs": p,
                        "rel": f"{(image_dir or '.workbench-media')}/{p.name}"})
    return out


def import_session_image(working_dir: Path, image_dir: str,
                         src: Path) -> tuple[Optional[dict], Optional[str]]:
    """Copy an image into working_dir/<image_dir>/ under a clean sequential name
    (img-01.png, img-02.png, …) for typo-proof referencing. Gitignores the dir
    when working_dir is a repo. Returns ({name, abs, rel}, None) or (None, err)."""
    try:
        src = Path(src)
        if not src.is_file():
            return None, f"not a file: {src}"
        ext = src.suffix.lower() or ".png"
        if ext not in _IMAGE_EXTS:
            return None, f"not an image: {src.name}"
        d = working_dir / (image_dir or ".workbench-media")
        d.mkdir(parents=True, exist_ok=True)
        n = sum(1 for p in d.iterdir() if p.is_file()) + 1
        while (d / f"img-{n:02d}{ext}").exists():
            n += 1
        dest = d / f"img-{n:02d}{ext}"
        shutil.copy2(src, dest)
        if _is_git_repo(working_dir):
            _ensure_gitignored(working_dir, image_dir or ".workbench-media")
        return ({"name": dest.name, "abs": dest,
                 "rel": f"{(image_dir or '.workbench-media')}/{dest.name}"}, None)
    except Exception as ex:
        return None, str(ex)


def open_cli_session(config: Config, working_dir: Path, cli: str = "",
                     first_message: str = "") -> Optional[str]:
    """Open the coding-agent CLI in a real terminal rooted in working_dir
    (interactive — the user drives it). If first_message is given it's sent as
    the CLI's opening prompt: staged to a temp file and read back via `$(cat …)`
    at launch, so the message text — quotes, apostrophes, newlines, $ — never
    touches the shell command line (no fragile quoting). The temp file deletes
    itself once read. Reuses delegate_terminal_command. Returns an error or None."""
    cli = (cli or config.cli_session_command or "claude").strip()
    binary = shlex.split(cli)[0] if cli else "claude"
    if not shutil.which(binary):
        return (f"{binary} not found — install the CLI or set a different "
                f"'CLI session command' in Settings")
    cmd = cli
    if first_message.strip():
        try:
            fd, tmp = tempfile.mkstemp(prefix="wb-cli-msg-", suffix=".txt")
            with os.fdopen(fd, "w") as fh:
                fh.write(first_message)
        except Exception as ex:
            return f"couldn't stage the opening message: {ex}"
        # cat the message (then delete the file) inside a command substitution,
        # double-quoted so the whole message is one arg. Inner double quotes are
        # independent within $(), and the outer bash -c wrapper uses single
        # quotes, so this nests cleanly regardless of the message's contents.
        cmd = f'{cli} "$(cat "{tmp}"; rm -f "{tmp}")"'
    template = config.delegate_terminal_command
    rendered = template.replace("{dir}", shlex.quote(str(working_dir))).replace("{cmd}", cmd)
    return _spawn(shlex.split(rendered))


def _ensure_gitignored(repo_dir: Path, name: str) -> None:
    """Add `name/` to repo_dir/.gitignore if absent — so the symlink context
    folder never gets committed into the code repo. Best-effort (silent)."""
    gi = repo_dir / ".gitignore"
    entry = name.rstrip("/") + "/"
    try:
        existing = gi.read_text() if gi.exists() else ""
        lines = {ln.strip() for ln in existing.splitlines()}
        if entry in lines or name in lines or name.rstrip("/") in lines:
            return
        sep = "" if (not existing or existing.endswith("\n")) else "\n"
        gi.write_text(existing + sep + entry + "\n")
    except Exception:
        pass


def sync_context_symlinks(working_dir: Path, context_dirname: str,
                          targets: list[Path]) -> tuple[list[str], Optional[str]]:
    """Mirror `targets` as symlinks inside working_dir/<context_dirname>/ so a
    CLI rooted in working_dir sees the chosen vault parts as a normal subfolder
    (CLI-agnostic, inspectable — no --add-dir flag, verified Claude Code reads
    through them). The folder is fully managed: symlinks no longer in `targets`
    are removed; real files/dirs are never clobbered. Gitignored when working_dir
    is a git repo. Returns (linked basenames, error-or-None)."""
    try:
        ctx = working_dir / (context_dirname or ".workbench-context")
        ctx.mkdir(parents=True, exist_ok=True)
        # Desired basename -> target. Dedupe colliding basenames with a suffix.
        desired: dict[str, Path] = {}
        for t in targets:
            base = t.name or "context"
            name, i = base, 2
            while name in desired and desired[name] != t:
                name, i = f"{base} ({i})", i + 1
            desired[name] = t
        # Drop stale symlinks we manage; leave real files alone.
        for child in ctx.iterdir():
            if child.is_symlink() and child.name not in desired:
                child.unlink()
        linked: list[str] = []
        for name, target in desired.items():
            link = ctx / name
            if link.is_symlink():
                if Path(os.readlink(link)) == target:
                    linked.append(name)
                    continue
                link.unlink()
            elif link.exists():
                continue  # real file/dir with this name — don't clobber
            link.symlink_to(target)
            linked.append(name)
        if _is_git_repo(working_dir):
            _ensure_gitignored(working_dir, context_dirname or ".workbench-context")
        return linked, None
    except Exception as ex:
        return [], str(ex)


def open_in_obsidian(path: Path, vault_root: Path) -> Optional[str]:
    """Open a vault file in Obsidian via the obsidian:// URI scheme.
    Vault name = the vault root folder's basename (resolved through symlinks)."""
    try:
        resolved = path.resolve()
        vault_resolved = vault_root.resolve()
        rel = resolved.relative_to(vault_resolved)
    except ValueError:
        return "file is not inside the vault"
    except Exception as ex:
        return str(ex)
    vault_name = vault_resolved.name
    encoded_file = urllib.parse.quote(str(rel))
    uri = f"obsidian://open?vault={vault_name}&file={encoded_file}"
    return _spawn(["xdg-open", uri])
