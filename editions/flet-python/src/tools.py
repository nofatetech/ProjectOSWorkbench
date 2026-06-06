"""Tool registry + executor for the chat agent (Phase B).

Trust mode (locked decision): tools act directly on the vault / working_dir with
no permission dance — same model as Claude Code; the user manages git. Path
rules: absolute paths are used as-is; relative paths resolve against the vault
root. run_shell runs in the project's working_dir (or the vault root if none).

execute_tool() never raises — failures come back as strings so the model can
read the error and adjust.
"""

import shlex
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

READ_CAP = 24_000     # chars returned from a file read
LIST_CAP = 200        # entries from list_dir
SHELL_CAP = 8_000     # chars of shell output
SHELL_TIMEOUT = 60    # seconds
DELEGATE_CAP = 16_000  # chars of a delegated run's output returned to the agent


@dataclass
class ToolContext:
    vault_root: Path
    working_dir: Optional[Path] = None
    # Headless delegation config (mirrors Config fields). Delegation is only
    # advertised/usable when delegate_enabled is True.
    delegate_enabled: bool = False
    delegate_command: str = "claude"
    delegate_permission_mode: str = "bypassPermissions"
    delegate_allowed_tools: str = ""
    delegate_timeout: int = 600


# --- Headless delegation: background job registry ---------------------------
# A delegated CLI run takes minutes, so we never block the chat on it. The
# spawn tool returns a job id immediately; the agent calls check_delegation to
# poll. Jobs live in-process (lost on app restart) — fine for v1, since a run
# that outlives the session is an edge case.

@dataclass
class DelegationJob:
    id: str
    task: str
    status: str = "running"  # running | done | error
    output: str = ""
    returncode: Optional[int] = None


_JOBS: dict[str, DelegationJob] = {}
_JOBS_LOCK = threading.Lock()


def _build_delegate_cmd(ctx: ToolContext, task: str) -> list[str]:
    """Assemble the headless CLI invocation. Allow-list (if set) takes
    precedence over permission-mode."""
    cmd = shlex.split(ctx.delegate_command) + ["-p", task]
    allowed = ctx.delegate_allowed_tools.replace(",", " ").split()
    if allowed:
        cmd += ["--allowedTools", *allowed]
    elif ctx.delegate_permission_mode:
        cmd += ["--permission-mode", ctx.delegate_permission_mode]
    if ctx.working_dir:
        cmd += ["--add-dir", str(ctx.working_dir)]
    return cmd


def _run_delegation(job: DelegationJob, ctx: ToolContext) -> None:
    cwd = ctx.working_dir if (ctx.working_dir and ctx.working_dir.exists()) else ctx.vault_root
    try:
        r = subprocess.run(
            _build_delegate_cmd(ctx, job.task), cwd=str(cwd),
            capture_output=True, text=True, timeout=ctx.delegate_timeout,
        )
        out = (r.stdout or "") + (f"\n[stderr]\n{r.stderr}" if r.stderr else "")
        if len(out) > DELEGATE_CAP:
            out = out[:DELEGATE_CAP] + "\n…[truncated]"
        with _JOBS_LOCK:
            job.output = out.strip()
            job.returncode = r.returncode
            job.status = "done" if r.returncode == 0 else "error"
    except subprocess.TimeoutExpired:
        with _JOBS_LOCK:
            job.status = "error"
            job.output = f"[delegation timed out after {ctx.delegate_timeout}s]"
    except FileNotFoundError:
        with _JOBS_LOCK:
            job.status = "error"
            job.output = f"[CLI not found: {ctx.delegate_command!r} — is it installed?]"
    except Exception as ex:
        with _JOBS_LOCK:
            job.status = "error"
            job.output = f"[delegation error: {ex}]"


def _start_delegation(ctx: ToolContext, task: str) -> str:
    job = DelegationJob(id="d_" + uuid.uuid4().hex[:8], task=task)
    with _JOBS_LOCK:
        _JOBS[job.id] = job
    threading.Thread(target=_run_delegation, args=(job, ctx), daemon=True).start()
    return job.id


# OpenAI/OpenRouter-style function schemas advertised to the model.
TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "read_vault_note",
        "description": ("Read a text file. Relative paths resolve against the vault "
                        "root; absolute paths are read as-is (use for working_dir files)."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "file path (vault-relative or absolute)"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_vault_note",
        "description": ("Create or overwrite a text file with the given content. "
                        "Relative paths resolve against the vault root. Parent dirs are created."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List entries of a directory. Relative paths resolve against the vault root; '.' is the vault root.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "directory path; '.' for vault root"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "move_note",
        "description": "Move or rename a file (e.g. to triage an inbox note into a project).",
        "parameters": {"type": "object", "properties": {
            "src": {"type": "string"}, "dst": {"type": "string"}},
            "required": ["src", "dst"]}}},
    {"type": "function", "function": {
        "name": "run_shell",
        "description": "Run a shell command in the project working_dir (or vault root). Returns exit code + combined stdout/stderr.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}},
            "required": ["command"]}}},
]

# Advertised only when delegation is enabled (see schemas_for). Heavy coding work
# is handed to a CLI agent (Claude Code) running in the project working_dir; the
# run is asynchronous, so the agent spawns then polls.
DELEGATE_SCHEMAS = [
    {"type": "function", "function": {
        "name": "delegate_to_claude_code",
        "description": (
            "Hand a heavy, multi-step coding task to the Claude Code CLI running in "
            "the project's working_dir (full repo access, can edit files & run builds). "
            "Runs in the background and returns a job_id immediately — then call "
            "check_delegation(job_id) to poll for the result. Use for real code changes; "
            "use run_shell for quick one-off commands."),
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "self-contained instructions for the CLI agent"}},
            "required": ["task"]}}},
    {"type": "function", "function": {
        "name": "check_delegation",
        "description": "Poll a delegated job by id. Returns its status (running/done/error) and, when finished, the CLI agent's output.",
        "parameters": {"type": "object", "properties": {
            "job_id": {"type": "string"}},
            "required": ["job_id"]}}},
]


def schemas_for(ctx: ToolContext) -> list:
    """Tool schemas advertised to the model for this context — base tools plus
    delegation tools when enabled."""
    return TOOL_SCHEMAS + DELEGATE_SCHEMAS if ctx.delegate_enabled else list(TOOL_SCHEMAS)


def _resolve(ctx: ToolContext, path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else (ctx.vault_root / path)


def execute_tool(name: str, args: dict, ctx: ToolContext) -> str:
    try:
        if name == "read_vault_note":
            text = _resolve(ctx, args["path"]).read_text(encoding="utf-8")
            return text if len(text) <= READ_CAP else text[:READ_CAP] + "\n…[truncated]"

        if name == "write_vault_note":
            p = _resolve(ctx, args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            content = args.get("content", "")
            p.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} chars to {p}"

        if name == "list_dir":
            p = _resolve(ctx, args.get("path", ".") or ".")
            entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
            lines = [("📁 " if e.is_dir() else "📄 ") + e.name for e in entries[:LIST_CAP]]
            more = "" if len(entries) <= LIST_CAP else f"\n…(+{len(entries) - LIST_CAP} more)"
            return f"{p}:\n" + "\n".join(lines) + more

        if name == "move_note":
            src, dst = _resolve(ctx, args["src"]), _resolve(ctx, args["dst"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            return f"Moved {src} → {dst}"

        if name == "run_shell":
            cwd = ctx.working_dir if (ctx.working_dir and ctx.working_dir.exists()) else ctx.vault_root
            r = subprocess.run(args["command"], shell=True, cwd=str(cwd),
                               capture_output=True, text=True, timeout=SHELL_TIMEOUT)
            out = (r.stdout or "") + (f"\n[stderr]\n{r.stderr}" if r.stderr else "")
            if len(out) > SHELL_CAP:
                out = out[:SHELL_CAP] + "\n…[truncated]"
            return f"(exit {r.returncode}, cwd {cwd})\n{out}".strip()

        if name == "delegate_to_claude_code":
            if not ctx.delegate_enabled:
                return "[delegation is disabled — enable it in Settings]"
            job_id = _start_delegation(ctx, args.get("task", ""))
            return (f"Started delegation job {job_id} in the background. "
                    f"Call check_delegation(\"{job_id}\") to poll for the result "
                    f"(it may take a minute or more).")

        if name == "check_delegation":
            with _JOBS_LOCK:
                job = _JOBS.get(args.get("job_id", ""))
                if job is None:
                    return f"[no such job: {args.get('job_id')!r}]"
                if job.status == "running":
                    return f"Job {job.id}: still running…"
                return f"Job {job.id}: {job.status} (exit {job.returncode}).\n{job.output}"

        return f"[unknown tool: {name}]"
    except Exception as ex:
        return f"[tool error in {name}: {ex}]"
