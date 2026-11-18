"""The MCP gateway: one client session per downstream server, one policy decision per call.

The agent never talks to an MCP server directly. Every ``call`` goes through:

1. RBAC (is ``server.tool`` allowed for this role?)
2. argument binding (``employee_id`` pinned to the caller, etc.)
3. argument patterns (egress allow-lists)
4. budgets (calls / writes per session)
5. approval rules: ``always``, or ``provenance`` when an argument value is tainted
6. the downstream call, then taint bookkeeping on the result
7. an audit record, whatever the outcome

With ``enforce=False`` the gateway is a transparent proxy that still audits; that is the
"no guardrails" baseline in the evals.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import Client

from finagent.gateway.audit import AuditLog, sha256_of
from finagent.gateway.policy import Decision, Policy, Principal, check_patterns, resolve_binding

ID_LIKE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._+-]{2,}$")


@dataclass
class ToolSpec:
    name: str  # name shown to the model (unique across servers)
    server: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    untrusted_fields: list[str]

    @property
    def qualified(self) -> str:
        return f"{self.server}.{self.name}"


@dataclass
class CallResult:
    status: str  # ok | error | denied | pending_approval
    data: Any = None
    message: str = ""
    untrusted_fields: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        if self.status == "ok":
            return json.dumps(self.data, separators=(",", ":"), default=str)
        if self.status == "error":
            return f"ERROR: {self.message}"
        if self.status == "denied":
            return f"DENIED by security policy: {self.message}. Do not retry this call."
        return f"PENDING_APPROVAL: {self.message}. The action was NOT executed; a human must approve it."


@dataclass
class ApprovalRequest:
    id: str
    principal: Principal
    tool: str
    args: dict[str, Any]
    reason: str


Approver = Callable[[ApprovalRequest], Awaitable[bool | None]]


async def deny_all(req: ApprovalRequest) -> bool | None:
    """Default approver: nobody is around, so the request stays queued (not executed)."""
    return None


class Taint:
    """Value-level provenance tracking.

    Trusted sources: the user's request and fields of tool output that the server does not mark
    untrusted. Untrusted sources: fields listed in a tool's ``untrusted_fields`` and *all* output of
    a call whose arguments were themselves tainted (so a lookup driven by injected text cannot
    launder the injected value into trusted data).
    """

    def __init__(self) -> None:
        self.instruction = ""
        self.trusted: set[str] = set()
        self.untrusted_text: list[str] = []

    def set_instruction(self, text: str) -> None:
        self.instruction = text

    def is_trusted(self, v: Any) -> bool:
        s = str(v)
        return s in self.trusted or (len(s) >= 2 and s in self.instruction)

    def is_tainted(self, v: Any) -> bool:
        s = str(v)
        return not self.is_trusted(s) and any(s in t for t in self.untrusted_text)

    def args_tainted(self, args: dict[str, Any]) -> bool:
        for v in args.values():
            for x in v if isinstance(v, list) else [v]:
                if isinstance(x, str) and ID_LIKE.match(x) and self.is_tainted(x):
                    return True
        return False

    def observe(self, data: Any, untrusted_fields: list[str], all_untrusted: bool) -> None:
        def walk(x: Any, untrusted: bool) -> None:
            if isinstance(x, dict):
                for k, v in x.items():
                    walk(v, untrusted or k in untrusted_fields)
            elif isinstance(x, list):
                for v in x:
                    walk(v, untrusted)
            elif x is not None:
                if untrusted:
                    self.untrusted_text.append(str(x))
                else:
                    self.trusted.add(str(x))

        walk(data, all_untrusted)


class Gateway:
    def __init__(self, servers: dict[str, Any], policy: Policy, principal: Principal, *,
                 audit: AuditLog | None = None, approver: Approver = deny_all, enforce: bool = True,
                 session_id: str | None = None):
        self.servers = servers
        self.policy = policy
        self.principal = principal
        self.audit = audit or AuditLog(None)
        self.approver = approver
        self.enforce = enforce
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.taint = Taint()
        self.catalog: dict[str, ToolSpec] = {}
        self._clients: dict[str, Client] = {}
        self._stack = AsyncExitStack()
        self.calls = 0
        self.writes = 0
        self.pending: list[ApprovalRequest] = []

    async def __aenter__(self) -> Gateway:
        for name, target in self.servers.items():
            client = await self._stack.enter_async_context(Client(target))
            self._clients[name] = client
            for t in (await client.list_tools()).tools:
                ann = t.annotations
                self.catalog[t.name] = ToolSpec(
                    name=t.name, server=name, description=t.description or "", input_schema=t.input_schema,
                    read_only=bool(ann and ann.read_only_hint), untrusted_fields=list((t.meta or {}).get("untrusted_fields", [])))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._stack.aclose()

    def set_instruction(self, text: str) -> None:
        self.taint.set_instruction(text)
        self.audit.append({"session": self.session_id, "principal": self.principal.__dict__, "event": "task",
                           "instruction_sha256": sha256_of(text)})

    def tools_for_agent(self) -> list[ToolSpec]:
        if not self.enforce:
            return list(self.catalog.values())
        return [t for t in self.catalog.values() if self.policy.allowed(t.qualified, self.principal.role)]

    # ------------------------------------------------------------------ decision
    async def _employee_named(self, emp_id: Any) -> bool:
        if emp_id is None:
            return True
        if str(emp_id) in self.taint.instruction:
            return True
        client = self._clients.get("directory")
        if client is None:
            return False
        r = await client.call_tool("get_employee", {"employee_id": str(emp_id)})
        if r.is_error:
            return False
        data = r.structured_content or {}
        if set(data) == {"result"}:
            data = data["result"]
        if not isinstance(data, dict) or not data:
            text = " ".join(getattr(c, "text", "") for c in r.content)
            data = json.loads(text) if text.startswith("{") else {}
        name = str(data.get("name", "")).lower()
        return bool(name) and name in self.taint.instruction.lower()

    def decide(self, spec: ToolSpec, args: dict[str, Any]) -> Decision:
        args = dict(args)
        if not self.enforce:
            return Decision("allow", "enforcement off", args)
        role = self.principal.role
        if not self.policy.allowed(spec.qualified, role):
            return Decision("deny", f"role '{role}' may not call {spec.qualified}", args)
        if self.calls >= self.policy.max_calls:
            return Decision("deny", "session call budget exhausted", args)
        if not spec.read_only and self.writes >= self.policy.max_writes:
            return Decision("deny", "session write budget exhausted", args)
        rules = self.policy.rules_for(spec.qualified, role)
        for r in rules:
            for arg, expr in r.bind.items():
                want = resolve_binding(expr, self.principal)
                if args.get(arg) in (None, ""):
                    args[arg] = want
                elif args[arg] != want:
                    return Decision("deny", f"{arg} must be the caller's own ({want})", args)
            bad = check_patterns(r, args)
            if bad:
                return Decision("deny", bad, args)
        for r in rules:
            if r.approval == "always":
                return Decision("approval", f"{spec.qualified} always requires human approval", args)
            for arg in r.provenance:
                val = args.get(arg)
                for v in val if isinstance(val, list) else [val]:
                    if v is not None and not self.taint.is_trusted(v):
                        return Decision("approval", f"{arg}={v!r} is not grounded in the request or trusted data", args)
        return Decision("allow", "", args)

    # ------------------------------------------------------------------ call
    async def call(self, name: str, args: dict[str, Any] | None) -> CallResult:
        args = args or {}
        t0 = time.perf_counter()
        spec = self.catalog.get(name)
        if spec is None:
            res = CallResult("error", message=f"unknown tool {name}")
            self._log(name, args, Decision("deny", "unknown tool", args), res, t0)
            return res
        dec = self.decide(spec, args)
        if dec.action == "allow" and self.enforce:
            for r in self.policy.rules_for(spec.qualified, self.principal.role):
                arg = r.employee_named_in_request
                if arg and not await self._employee_named(dec.args.get(arg)):
                    dec = Decision("approval", f"{arg}={dec.args.get(arg)!r} is not the employee named in the request",
                                   dec.args)
                    break
        if dec.action == "approval":
            req = ApprovalRequest(f"AR-{len(self.pending) + 1:03d}", self.principal, spec.qualified, dec.args, dec.reason)
            verdict = await self.approver(req)
            if verdict is None:
                self.pending.append(req)
                res = CallResult("pending_approval", message=f"request {req.id} queued ({dec.reason})")
                self._log(name, args, dec, res, t0, approval=req.id)
                return res
            if not verdict:
                res = CallResult("denied", message=f"approver rejected request {req.id}")
                self._log(name, args, Decision("deny", "rejected by approver", dec.args), res, t0, approval=req.id)
                return res
            dec = Decision("allow", f"approved {req.id}", dec.args)
        if dec.action == "deny":
            res = CallResult("denied", message=dec.reason)
            self._log(name, args, dec, res, t0)
            return res

        tainted_args = self.taint.args_tainted(dec.args)
        self.calls += 1
        if not spec.read_only:
            self.writes += 1
        r = await self._clients[spec.server].call_tool(spec.name, dec.args)
        if r.is_error:
            text = " ".join(getattr(c, "text", "") for c in r.content)
            text = text.replace(f"Error executing tool {spec.name}: ", "")
            res = CallResult("error", message=text)
        else:
            data = r.structured_content
            if isinstance(data, dict) and set(data) == {"result"}:
                data = data["result"]
            if data is None:
                text = " ".join(getattr(c, "text", "") for c in r.content)
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    data = text
            res = CallResult("ok", data=data, untrusted_fields=spec.untrusted_fields)
            self.taint.observe(data, spec.untrusted_fields, all_untrusted=tainted_args)
        self._log(name, args, dec, res, t0, tainted_args=tainted_args)
        return res

    def _log(self, name: str, args: dict[str, Any], dec: Decision, res: CallResult, t0: float,
             approval: str | None = None, tainted_args: bool = False) -> None:
        spec = self.catalog.get(name)
        self.audit.append({
            "session": self.session_id, "principal": self.principal.__dict__, "event": "tool_call",
            "tool": spec.qualified if spec else name, "args": args,
            "effective_args": dec.args if dec.args != args else None,
            "decision": dec.action, "reason": dec.reason, "approval_id": approval, "status": res.status,
            "tainted_args": tainted_args, "result_sha256": sha256_of(res.data) if res.status == "ok" else None,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
        })
