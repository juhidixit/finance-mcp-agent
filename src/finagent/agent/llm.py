"""Model back-ends for the agent loop.

Every back-end implements the same small conversation interface, so the loop, the gateway and
the evals do not care which model is behind it:

* ``LlamaCppChat``  – any local GGUF model whose chat template supports tools (Qwen3, Qwen2.5 …),
  run with llama.cpp on CPU. Supports record/replay cassettes so the evals re-run in CI without
  the model.
* ``AnthropicChat`` – Claude through the Anthropic Messages API (needs ``pip install anthropic``
  and an API key).
* ``ScriptedChat``  – a deterministic policy for unit tests.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: str = ""


class Chat(Protocol):
    def start(self, system: str, user: str, tools: list[dict[str, Any]]) -> None: ...
    def step(self) -> Turn: ...
    def add_tool_results(self, results: list[tuple[ToolCall, str]]) -> None: ...


# --------------------------------------------------------------------------- cassette
class Cassette:
    """Prompt-hash -> completion store. ``mode``: off | record | replay."""

    def __init__(self, path: str | Path | None, mode: str = "off"):
        self.path = Path(path) if path else None
        self.mode = mode
        self.data: dict[str, str] = {}
        if self.path and self.path.exists() and mode in ("record", "replay"):
            for line in self.path.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self.data[rec["key"]] = rec["completion"]

    @staticmethod
    def key(model_id: str, prompt: str) -> str:
        return hashlib.sha256(f"{model_id}\n{prompt}".encode()).hexdigest()

    def get(self, k: str) -> str | None:
        return self.data.get(k)

    def put(self, k: str, completion: str) -> None:
        if self.mode != "record" or k in self.data:
            return
        self.data[k] = completion
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps({"key": k, "completion": completion}) + "\n")


# --------------------------------------------------------------------------- llama.cpp
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def parse_hermes_tool_calls(text: str) -> tuple[str, list[ToolCall]]:
    """Parse ``<tool_call>{"name":..., "arguments":...}</tool_call>`` blocks (Qwen/Hermes format)."""
    calls = []
    for i, m in enumerate(TOOL_CALL_RE.finditer(text)):
        try:
            obj = json.loads(m.group(1))
            args = obj.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
            calls.append(ToolCall(f"call_{i}", str(obj["name"]), args if isinstance(args, dict) else {}))
        except (json.JSONDecodeError, KeyError, TypeError):
            calls.append(ToolCall(f"call_{i}", "__malformed__", {"raw": m.group(1)[:500]}))
    content = TOOL_CALL_RE.sub("", text).strip()
    return content, calls


class LlamaCppModel:
    """Loads a GGUF once; many ``LlamaCppChat`` conversations share it (and its KV-cache prefix)."""

    def __init__(self, model_path: str | None, *, model_id: str, n_ctx: int = 16384, n_threads: int | None = None,
                 cassette: Cassette | None = None, max_tokens: int = 768, seed: int = 0):
        self.model_id = model_id
        self.model_path = model_path
        self.cassette = cassette or Cassette(None)
        self.max_tokens = max_tokens
        self._llm = None
        self._fmt = None
        self._n_ctx, self._n_threads, self._seed = n_ctx, n_threads, seed

    def _load(self) -> None:
        if self._llm is not None:
            return
        import llama_cpp
        from llama_cpp.llama_chat_format import Jinja2ChatFormatter

        if not self.model_path:
            raise RuntimeError("cassette miss in replay mode and no model_path given")
        self._llm = llama_cpp.Llama(self.model_path, n_ctx=self._n_ctx, n_threads=self._n_threads,
                                    seed=self._seed, verbose=False)
        tmpl = self._llm.metadata["tokenizer.chat_template"]
        eos = self._llm._model.token_get_text(self._llm.token_eos())
        bos = self._llm._model.token_get_text(self._llm.token_bos()) if self._llm.token_bos() >= 0 else ""
        self._fmt = Jinja2ChatFormatter(tmpl, eos_token=eos, bos_token=bos, add_generation_prompt=True)

    def render(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> str:
        if self._fmt is None:
            if self.cassette.mode == "replay" and not self.model_path:
                return json.dumps({"messages": messages, "tools": tools}, sort_keys=True)
            self._load()
        return self._fmt(messages=messages, tools=tools).prompt  # type: ignore[misc]

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        # The cassette key covers the full conversation + tool list, so a replay is exact.
        k = Cassette.key(self.model_id, json.dumps({"messages": messages, "tools": tools}, sort_keys=True))
        cached = self.cassette.get(k)
        if cached is not None:
            rec = json.loads(cached)
            text, pt, ct = rec["text"], rec["prompt_tokens"], rec["completion_tokens"]
        elif self.cassette.mode == "replay":
            raise KeyError("cassette miss: the prompt changed since the recording; re-record with a model")
        else:
            prompt = self.render(messages, tools)
            out = self._llm.create_completion(prompt, max_tokens=self.max_tokens, temperature=0.0,  # type: ignore[union-attr]
                                              stop=["<|im_end|>", "<|endoftext|>"])
            text = out["choices"][0]["text"]
            pt, ct = out["usage"]["prompt_tokens"], out["usage"]["completion_tokens"]
            self.cassette.put(k, json.dumps({"text": text, "prompt_tokens": pt, "completion_tokens": ct}))
        content, calls = parse_hermes_tool_calls(text)
        return Turn(content, calls, pt, ct, raw=text)


class LlamaCppChat:
    def __init__(self, model: LlamaCppModel):
        self.model = model
        self.messages: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []

    def start(self, system: str, user: str, tools: list[dict[str, Any]]) -> None:
        self.tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                        "parameters": t["input_schema"]}} for t in tools]
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def step(self) -> Turn:
        turn = self.model.complete(self.messages, self.tools)
        msg: dict[str, Any] = {"role": "assistant", "content": turn.text}
        if turn.tool_calls:
            msg["tool_calls"] = [{"id": c.id, "type": "function",
                                  "function": {"name": c.name, "arguments": c.arguments}} for c in turn.tool_calls]
        self.messages.append(msg)
        return turn

    def add_tool_results(self, results: list[tuple[ToolCall, str]]) -> None:
        for call, text in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": text})


# --------------------------------------------------------------------------- Anthropic
class AnthropicChat:
    """Claude via the Messages API (manual loop, so every call goes through the gateway)."""

    def __init__(self, model: str = "claude-opus-5-5", max_tokens: int = 16000, client: Any = None):
        import anthropic

        self.client = client or anthropic.Anthropic()
        self.model = model
        self.max_tokens = max_tokens
        self.system = ""
        self.tools: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    def start(self, system: str, user: str, tools: list[dict[str, Any]]) -> None:
        self.system = system
        self.tools = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
                      for t in tools]
        self.messages = [{"role": "user", "content": user}]

    def step(self) -> Turn:
        r = self.client.messages.create(model=self.model, max_tokens=self.max_tokens, system=self.system,
                                        tools=self.tools, messages=self.messages)
        self.messages.append({"role": "assistant", "content": r.content})
        text = "".join(b.text for b in r.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in r.content if b.type == "tool_use"]
        if r.stop_reason == "refusal":
            text = text or "[refused]"
        return Turn(text, calls, r.usage.input_tokens, r.usage.output_tokens)

    def add_tool_results(self, results: list[tuple[ToolCall, str]]) -> None:
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": text,
             "is_error": text.startswith(("ERROR", "DENIED", "PENDING_APPROVAL"))} for c, text in results]})


# --------------------------------------------------------------------------- scripted
@dataclass
class ScriptedChat:
    """Replays a fixed list of tool calls, then a final message. Used by unit tests."""

    script: list[list[tuple[str, dict[str, Any]]]]
    final: str = "done"
    seen: list[tuple[ToolCall, str]] = field(default_factory=list)
    _i: int = 0

    def start(self, system: str, user: str, tools: list[dict[str, Any]]) -> None:
        self._i = 0

    def step(self) -> Turn:
        if self._i >= len(self.script):
            return Turn(self.final, [])
        calls = [ToolCall(uuid.uuid4().hex[:8], n, a) for n, a in self.script[self._i]]
        self._i += 1
        return Turn("", calls)

    def add_tool_results(self, results: list[tuple[ToolCall, str]]) -> None:
        self.seen.extend(results)
