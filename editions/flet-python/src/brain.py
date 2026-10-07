"""Brain layer — pluggable LLM providers behind a streaming interface.

Each agent's `model:` string routes to a specific brain:
  mock/anything           → MockBrain  (instant fake responses, no network/cost)
  ollama/<model>          → OllamaBrain (local Ollama at config.ollama_base_url)
  codex  |  codex/<model>  → CodexBrain (Codex CLI on your ChatGPT login; tools
                             reach the app through an MCP bridge)
  openrouter/<provider>/<model>  → OpenRouterBrain (real cloud call)
  <provider>/<model>      → OpenRouterBrain (default if no prefix)

Set Config.force_mock = True to override everything and use MockBrain — handy
during UI dev to avoid burning tokens.

Streaming: each brain's .stream() is a sync generator yielding text chunks.
We use threading (one thread per agent in composition B), not asyncio.
"""

import json
import secrets
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterator, Optional

import httpx

from config import Config


# OpenRouter recommends these for app attribution / leaderboard ranking.
APP_REFERER = "https://github.com/nofatetech/ProjectOSWorkbenchApp1"
APP_TITLE = "ProjectOS Workbench"


# Known per-model output-token ceilings, matched by substring against the bare
# model slug (most specific first). Sending max_tokens above a model's real cap
# makes some providers 400 the request; we clamp to keep calls valid. Unknown
# models pass through unclamped — let the provider decide rather than guess low.
_MODEL_OUTPUT_CAPS: list[tuple[str, int]] = [
    ("gemini-2.0-flash", 8192),
    ("gemini-1.5", 8192),
    ("gemini", 8192),
    ("claude-opus-4", 32000),
    ("claude-sonnet-4", 64000),
    ("claude-3-7", 64000),
    ("claude-3-5", 8192),
    ("claude-3", 4096),
    ("gpt-4o", 16384),
    ("gpt-4.1", 32768),
    ("o1", 32768),
    ("gpt-4", 8192),
    ("llama", 8192),
    ("qwen", 8192),
]


def clamp_max_tokens(model: str, max_tokens: Optional[int]) -> Optional[int]:
    """Clamp a requested max_tokens to the model's known output ceiling. Returns
    the value unchanged when it's already within cap, falsy, or the model is
    unknown (no entry → pass through, provider clamps server-side)."""
    if not max_tokens:
        return max_tokens
    m = model.lower()
    for key, cap in _MODEL_OUTPUT_CAPS:
        if key in m:
            return min(max_tokens, cap)
    return max_tokens


def _gen_params(temperature: Optional[float], max_tokens: Optional[int]) -> dict:
    """Optional sampling params, omitted when unset so provider defaults apply."""
    out: dict = {}
    if temperature is not None:
        out["temperature"] = temperature
    if max_tokens:  # 0/None → don't cap
        out["max_tokens"] = max_tokens
    return out


# Tool-calling stream protocol: stream_with_tools() yields tagged tuples
#   ("text", str)          — a content chunk (stream live to the UI)
#   ("tool_calls", list)   — emitted once at end of a turn if the model wants
#                            tools; each item is {"id", "name", "arguments"(str)}
# The caller loops: execute the tools, append the assistant tool-call message +
# tool result messages, then call stream_with_tools() again until a turn yields
# only text (no tool_calls).
ToolEvent = tuple  # ("text", str) | ("tool_calls", list[dict])


class Brain(ABC):
    @abstractmethod
    def stream(self, messages: list[dict], model: str,
               temperature: Optional[float] = None,
               max_tokens: Optional[int] = None) -> Iterator[str]:
        """Yield text chunks as the model generates them."""
        ...

    def stream_with_tools(self, messages: list[dict], model: str,
                          tools: Optional[list[dict]] = None,
                          temperature: Optional[float] = None,
                          max_tokens: Optional[int] = None) -> Iterator[ToolEvent]:
        """Default: no tool support — wrap plain stream() as text events. Brains
        that support function-calling (OpenRouter) override this."""
        for chunk in self.stream(messages, model, temperature, max_tokens):
            yield ("text", chunk)


class MockBrain(Brain):
    """Returns canned responses chunked over ~half a second. No network."""

    def stream(self, messages: list[dict], model: str,
               temperature: Optional[float] = None,
               max_tokens: Optional[int] = None) -> Iterator[str]:
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "(no input)",
        )
        # Compact, honest mock — no truncation of the actual user message.
        response = (
            f"[mock {model}] backend not wired for real responses yet. "
            f"You said: {last_user}"
        )
        for word in response.split(" "):
            yield word + " "
            time.sleep(0.04)


def _parse_sse_chunks(line_iter) -> Iterator[str]:
    """OpenAI-compatible SSE: each `data: {json}` line carries a delta.content."""
    for raw in line_iter:
        if not raw:
            continue
        line = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        if not line.startswith("data: "):
            continue
        data = line[6:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        try:
            delta = obj["choices"][0]["delta"].get("content")
        except (KeyError, IndexError, TypeError):
            continue
        if delta:
            yield delta


def _parse_sse_tool_stream(line_iter) -> Iterator[ToolEvent]:
    """OpenAI-compatible SSE with tool calls. Yields ('text', chunk) live, and
    accumulates streamed tool_call fragments (by index) into one final
    ('tool_calls', [...]) event if the model requested any."""
    tool_calls: dict = {}  # index -> {"id","name","arguments"}
    for raw in line_iter:
        if not raw:
            continue
        line = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        if not line.startswith("data: "):
            continue
        data = line[6:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        try:
            delta = obj["choices"][0].get("delta") or {}
        except (KeyError, IndexError, TypeError):
            continue
        content = delta.get("content")
        if content:
            yield ("text", content)
        for tc in (delta.get("tool_calls") or []):
            idx = tc.get("index", 0)
            slot = tool_calls.setdefault(idx, {"id": None, "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]
    if tool_calls:
        yield ("tool_calls", [tool_calls[i] for i in sorted(tool_calls)])


class OpenRouterBrain(Brain):
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": APP_REFERER,
            "X-Title": APP_TITLE,
        }

    def stream(self, messages: list[dict], model: str,
               temperature: Optional[float] = None,
               max_tokens: Optional[int] = None) -> Iterator[str]:
        if not self.api_key:
            yield "[OpenRouter] no API key set — open Settings to add one."
            return
        try:
            with httpx.Client(timeout=120.0) as client:
                with client.stream(
                    "POST", self.URL, headers=self._headers(),
                    json={"model": model, "messages": messages, "stream": True,
                          **_gen_params(temperature, max_tokens)},
                ) as resp:
                    if resp.status_code >= 400:
                        body = resp.read().decode("utf-8", errors="replace")
                        yield f"[OpenRouter HTTP {resp.status_code}] {body}"
                        return
                    yield from _parse_sse_chunks(resp.iter_lines())
        except httpx.HTTPError as e:
            yield f"[OpenRouter error] {e}"

    def stream_with_tools(self, messages: list[dict], model: str,
                          tools: Optional[list[dict]] = None,
                          temperature: Optional[float] = None,
                          max_tokens: Optional[int] = None) -> Iterator[ToolEvent]:
        if not self.api_key:
            yield ("text", "[OpenRouter] no API key set — open Settings to add one.")
            return
        body = {"model": model, "messages": messages, "stream": True,
                **_gen_params(temperature, max_tokens)}
        if tools:
            body["tools"] = tools
        try:
            with httpx.Client(timeout=120.0) as client:
                with client.stream("POST", self.URL, headers=self._headers(),
                                   json=body) as resp:
                    if resp.status_code >= 400:
                        body_txt = resp.read().decode("utf-8", errors="replace")
                        yield ("text", f"[OpenRouter HTTP {resp.status_code}] {body_txt}")
                        return
                    yield from _parse_sse_tool_stream(resp.iter_lines())
        except httpx.HTTPError as e:
            yield ("text", f"[OpenRouter error] {e}")


class OllamaBrain(Brain):
    def __init__(self, base_url: str = "http://localhost:11434/v1"):
        self.base_url = base_url.rstrip("/")

    def stream(self, messages: list[dict], model: str,
               temperature: Optional[float] = None,
               max_tokens: Optional[int] = None) -> Iterator[str]:
        try:
            with httpx.Client(timeout=300.0) as client:
                with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers={"Content-Type": "application/json"},
                    json={"model": model, "messages": messages, "stream": True,
                          **_gen_params(temperature, max_tokens)},
                ) as resp:
                    if resp.status_code >= 400:
                        body = resp.read().decode("utf-8", errors="replace")
                        yield f"[Ollama HTTP {resp.status_code}] {body}"
                        return
                    yield from _parse_sse_chunks(resp.iter_lines())
        except httpx.HTTPError as e:
            yield f"[Ollama error — is it running at {self.base_url}?] {e}"

    def stream_with_tools(self, messages: list[dict], model: str,
                          tools: Optional[list[dict]] = None,
                          temperature: Optional[float] = None,
                          max_tokens: Optional[int] = None) -> Iterator[ToolEvent]:
        """Ollama's OpenAI-compatible endpoint streams tool_calls in the same
        delta shape as OpenRouter (verified against qwen2.5), so we reuse the
        same tool-aware SSE parser. Tool support is per-model: a model without
        the `tools` capability (e.g. deepseek-r1, dolphin-phi) silently ignores
        `tools` and only ever yields text — same code path, just no tool_calls."""
        body = {"model": model, "messages": messages, "stream": True,
                **_gen_params(temperature, max_tokens)}
        if tools:
            body["tools"] = tools
        try:
            with httpx.Client(timeout=300.0) as client:
                with client.stream(
                    "POST", f"{self.base_url}/chat/completions",
                    headers={"Content-Type": "application/json"}, json=body,
                ) as resp:
                    if resp.status_code >= 400:
                        body_txt = resp.read().decode("utf-8", errors="replace")
                        yield ("text", f"[Ollama HTTP {resp.status_code}] {body_txt}")
                        return
                    yield from _parse_sse_tool_stream(resp.iter_lines())
        except httpx.HTTPError as e:
            yield ("text", f"[Ollama error — is it running at {self.base_url}?] {e}")


# --- Codex CLI ---------------------------------------------------------------

_BRIDGE = Path(__file__).resolve().parent / "codex_mcp_bridge.py"
_TOOL_RESULT_REPLAY_CAP = 2_000  # chars of each past tool result replayed to Codex


def _toml_str(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


def codex_transcript(messages: list[dict]) -> str:
    """Flatten OpenAI-format messages into one Codex prompt: the system prompt,
    the conversation so far (tool calls/results summarised), then the message to
    answer. Codex exec is single-shot, so the whole thread goes in each turn."""
    system = "\n\n".join(m["content"] for m in messages
                         if m.get("role") == "system" and m.get("content"))
    convo = [m for m in messages if m.get("role") != "system"]
    last = convo.pop() if convo and convo[-1].get("role") == "user" else None
    lines: list[str] = []
    for m in convo:
        role = m.get("role")
        if role == "user":
            lines.append(f"### User\n{m.get('content') or ''}")
        elif role == "assistant":
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                lines.append(f"[tool call] {fn.get('name')}({fn.get('arguments') or '{}'})")
            if m.get("content"):
                lines.append(f"### Assistant\n{m['content']}")
        elif role == "tool":
            text = str(m.get("content") or "")
            if len(text) > _TOOL_RESULT_REPLAY_CAP:
                text = text[:_TOOL_RESULT_REPLAY_CAP] + "…[truncated]"
            lines.append(f"[tool result] {text}")
    parts = ["<workbench_instructions>", system, "</workbench_instructions>",
             "You are running as the Workbench chat agent. Workbench's tools are the "
             "`workbench` MCP tools; use them for every vault write, publish and "
             "Shopify action. Your own shell is read-only. Reply in Markdown to the "
             "current message only."]
    if lines:
        parts += ["", "<conversation_so_far>", "\n\n".join(lines), "</conversation_so_far>"]
    parts += ["", "<current_message>", (last or {}).get("content") or "", "</current_message>"]
    return "\n".join(parts)


class _ToolEndpoint:
    """Localhost HTTP endpoint for one Codex turn: the MCP bridge POSTs
    {name, arguments} with this turn's bearer token; `executor` runs the tool."""

    def __init__(self, executor: Callable[[str, dict], str]):
        token = secrets.token_urlsafe(24)
        self.token = token

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.headers.get("Authorization") != f"Bearer {token}":
                    self.send_response(403)
                    self.end_headers()
                    return
                try:
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    result = executor(str(body.get("name", "")), body.get("arguments") or {})
                except Exception as ex:
                    result = f"[tool error: {ex}]"
                out = json.dumps({"result": str(result)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/tool"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CodexBrain(Brain):
    """Chat through the Codex CLI (`codex exec --json`), billed to the user's
    ChatGPT plan rather than an API key. Codex runs its own tool loop, so this
    brain never yields ("tool_calls", ...): Workbench tools are served to Codex
    over MCP (codex_mcp_bridge.py) and executed in-app by `tool_executor`, which
    the dispatcher sets per turn. Codex's own shell is sandboxed read-only in
    `workdir` (the vault), so writes go through Workbench's tools."""

    def __init__(self, command: str = "codex", workdir: Optional[Path] = None,
                 reasoning_effort: str = "", timeout: int = 900):
        self.command = command
        self.workdir = workdir
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.tool_executor: Optional[Callable[[str, dict], str]] = None

    def stream(self, messages: list[dict], model: str,
               temperature: Optional[float] = None,
               max_tokens: Optional[int] = None) -> Iterator[str]:
        for kind, text in self.stream_with_tools(messages, model):
            if kind == "text":
                yield text

    def _command(self, model: str, tools_file: Optional[str],
                 endpoint: Optional[_ToolEndpoint]) -> list[str]:
        cmd = [*shlex.split(self.command), "exec", "--json", "--ephemeral",
               "--skip-git-repo-check", "--ignore-user-config", "-s", "read-only"]
        if self.workdir:
            cmd += ["-C", str(self.workdir)]
        if model and model != "default":
            cmd += ["-m", model]
        if self.reasoning_effort:
            cmd += ["-c", f"model_reasoning_effort={_toml_str(self.reasoning_effort)}"]
        if endpoint and tools_file:
            env = (f"{{WB_TOOLS_FILE={_toml_str(tools_file)}, "
                   f"WB_TOOL_URL={_toml_str(endpoint.url)}, "
                   f"WB_TOOL_TOKEN={_toml_str(endpoint.token)}}}")
            cmd += ["-c", f"mcp_servers.workbench.command={_toml_str(sys.executable)}",
                    "-c", f"mcp_servers.workbench.args=[{_toml_str(str(_BRIDGE))}]",
                    "-c", f"mcp_servers.workbench.env={env}",
                    "-c", "mcp_servers.workbench.tool_timeout_sec=900",
                    "-c", 'mcp_servers.workbench.default_tools_approval_mode="approve"']
        return cmd + ["-"]

    def stream_with_tools(self, messages: list[dict], model: str,
                          tools: Optional[list[dict]] = None,
                          temperature: Optional[float] = None,
                          max_tokens: Optional[int] = None) -> Iterator[ToolEvent]:
        endpoint = _ToolEndpoint(self.tool_executor) if (tools and self.tool_executor) else None
        tmp = tempfile.TemporaryDirectory(prefix="wb-codex-")
        tools_file = None
        if endpoint:
            tools_file = str(Path(tmp.name) / "tools.json")
            Path(tools_file).write_text(json.dumps(tools), encoding="utf-8")
        proc = None
        try:
            try:
                proc = subprocess.Popen(
                    self._command(model, tools_file, endpoint), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            except FileNotFoundError:
                yield ("text", f"[Codex CLI not found: {self.command!r} — is it installed?]")
                return
            stderr_tail: list[str] = []
            threading.Thread(target=lambda: stderr_tail.extend(proc.stderr.readlines()[-20:]),
                             daemon=True).start()
            proc.stdin.write(codex_transcript(messages))
            proc.stdin.close()
            started = time.monotonic()
            said = False
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                item = event.get("item") or {}
                if kind == "item.completed" and item.get("type") == "agent_message":
                    text = item.get("text") or ""
                    if text:
                        yield ("text", ("\n\n" if said else "") + text)
                        said = True
                elif kind in ("error", "turn.failed"):
                    err = event.get("message") or (event.get("error") or {}).get("message")
                    yield ("text", f"[Codex error] {err or event}")
                if time.monotonic() - started > self.timeout:
                    proc.kill()
                    yield ("text", f"[Codex timed out after {self.timeout}s]")
                    return
            proc.wait(timeout=30)
            if proc.returncode and not said:
                tail = "".join(stderr_tail).strip()[-800:]
                yield ("text", f"[Codex exited {proc.returncode}] {tail}")
        finally:
            if proc and proc.poll() is None:
                proc.kill()
            if proc:
                proc.wait(timeout=30)
                proc.stdout.close()
                proc.stderr.close()
            if endpoint:
                endpoint.close()
            tmp.cleanup()


def brain_for(model: str, config: Config) -> tuple[Brain, str]:
    """Route the model string to a Brain. Returns (brain, model_name_to_send)."""
    if config.force_mock:
        return MockBrain(), model
    if model.startswith("mock/"):
        return MockBrain(), model[len("mock/"):]
    if model == "codex" or model.startswith("codex/"):
        brain = CodexBrain(config.codex_command, _codex_workdir(config),
                           config.codex_reasoning_effort)
        return brain, model[len("codex/"):] if "/" in model else "default"
    if model.startswith("ollama/"):
        return OllamaBrain(config.ollama_base_url), model[len("ollama/"):]
    if model.startswith("openrouter/"):
        return OpenRouterBrain(config.openrouter_api_key), model[len("openrouter/"):]
    # Default: assume bare provider/model string targets OpenRouter
    return OpenRouterBrain(config.openrouter_api_key), model


def _codex_workdir(config: Config) -> Optional[Path]:
    """Codex's (read-only) working directory: the configured vault, if any."""
    from vault import _resolve_vault_path  # local: vault imports are heavier
    try:
        return _resolve_vault_path(config.vault_path)
    except Exception:
        return None
