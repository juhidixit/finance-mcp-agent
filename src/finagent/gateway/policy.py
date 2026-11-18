"""Declarative authorization policy for agent tool calls.

A policy file (TOML) has four parts:

* ``[roles.<name>] allow = [...]`` – glob patterns over ``server.tool`` names (least privilege).
  Tools a role is not allowed are never shown to the model.
* ``[[rule]] bind = {arg = "principal.id"}`` – pin an argument to the caller's identity.
* ``[[rule]] arg_patterns = {arg = "regex"}`` – e.g. an email allow-list for egress.
* ``[[rule]] approval = "always"`` / ``provenance = [args]`` – human-in-the-loop. With
  ``provenance``, the call is escalated when any listed argument value did not come from the
  user's request or from trusted tool output (see ``gateway.Taint``).
* ``[[rule]] employee_named_in_request = "employee_id"`` – the employee an action targets must be
  named (or their id given) in the user's request, which catches the model guessing the wrong person.
* ``[limits]`` – per-session call and write budgets.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Principal:
    id: str
    role: str


@dataclass
class Rule:
    tools: list[str]
    roles: list[str] | None = None
    bind: dict[str, str] = field(default_factory=dict)
    arg_patterns: dict[str, str] = field(default_factory=dict)
    approval: str | None = None  # "always"
    provenance: list[str] = field(default_factory=list)
    employee_named_in_request: str | None = None  # arg holding an employee id whose name must be in the request

    def applies(self, qualified: str, role: str) -> bool:
        if self.roles is not None and role not in self.roles:
            return False
        return any(fnmatch.fnmatchcase(qualified, p) for p in self.tools)


@dataclass
class Policy:
    roles: dict[str, list[str]]
    rules: list[Rule]
    max_calls: int = 60
    max_writes: int = 40

    @classmethod
    def load(cls, path: str | Path) -> Policy:
        data = tomllib.loads(Path(path).read_text())
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Policy:
        roles = {name: list(v.get("allow", [])) for name, v in data.get("roles", {}).items()}
        rules = [Rule(**r) for r in data.get("rule", [])]
        lim = data.get("limits", {})
        return cls(roles, rules, lim.get("max_calls", 60), lim.get("max_writes", 40))

    def allowed(self, qualified: str, role: str) -> bool:
        return any(fnmatch.fnmatchcase(qualified, p) for p in self.roles.get(role, []))

    def rules_for(self, qualified: str, role: str) -> list[Rule]:
        return [r for r in self.rules if r.applies(qualified, role)]


@dataclass
class Decision:
    action: str  # allow | deny | approval
    reason: str = ""
    args: dict[str, Any] = field(default_factory=dict)  # possibly rewritten (bound) arguments


def resolve_binding(expr: str, principal: Principal) -> str:
    if expr == "principal.id":
        return principal.id
    if expr == "principal.role":
        return principal.role
    raise ValueError(f"unknown binding {expr}")


def check_patterns(rule: Rule, args: dict[str, Any]) -> str | None:
    for arg, pat in rule.arg_patterns.items():
        val = args.get(arg)
        vals = val if isinstance(val, list) else [val]
        for v in vals:
            if v is None or not re.fullmatch(pat, str(v)):
                return f"argument {arg}={v!r} does not match the allowed pattern"
    return None
