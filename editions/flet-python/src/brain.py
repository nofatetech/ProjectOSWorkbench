"""Brain layer — pluggable LLM providers behind a streaming interface.

Each agent's `model:` string routes to a specific brain:
  mock/anything           → MockBrain  (instant fake responses, no network/cost)
  ollama/<model>          → OllamaBrain (local Ollama at config.ollama_base_url)
  openrouter/<provider>/<model>  → OpenRouterBrain (real cloud call)
  <provider>/<model>      → OpenRouterBrain (default if no prefix)

Set Config.force_mock = True to override everything and use MockBrain — handy
during UI dev to avoid burning tokens.

Streaming: each brain's .stream() is a sync generator yielding text chunks.
We use threading (one thread per agent in composition B), not asyncio.
"""

import json
import time
from abc import ABC, abstractmethod
from typing import Iterator, Optional

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


def brain_for(model: str, config: Config) -> tuple[Brain, str]:
    """Route the model string to a Brain. Returns (brain, model_name_to_send)."""
    if config.force_mock:
        return MockBrain(), model
    if model.startswith("mock/"):
        return MockBrain(), model[len("mock/"):]
    if model.startswith("ollama/"):
        return OllamaBrain(config.ollama_base_url), model[len("ollama/"):]
    if model.startswith("openrouter/"):
        return OpenRouterBrain(config.openrouter_api_key), model[len("openrouter/"):]
    # Default: assume bare provider/model string targets OpenRouter
    return OpenRouterBrain(config.openrouter_api_key), model
