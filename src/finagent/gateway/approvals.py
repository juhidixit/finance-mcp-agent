"""File-backed human approval queue.

When the gateway needs a human decision it appends the request here and tells the agent the
action is pending. A person reviews it later with ``finagent approvals list`` and
``finagent approvals approve <id>`` (which executes the original call, audited) or ``reject``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from finagent.gateway.gateway import ApprovalRequest


class ApprovalQueue:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]

    def _save(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))

    async def __call__(self, req: ApprovalRequest) -> bool | None:
        rows = self._load()
        rid = f"AR-{len(rows) + 1:04d}"
        req.id = rid
        rows.append({"id": rid, "status": "pending", "principal": req.principal.__dict__, "tool": req.tool,
                     "args": req.args, "reason": req.reason, "created": round(time.time(), 3)})
        self._save(rows)
        return None

    def list(self, status: str | None = "pending") -> list[dict[str, Any]]:
        return [r for r in self._load() if status is None or r["status"] == status]

    def get(self, rid: str) -> dict[str, Any]:
        for r in self._load():
            if r["id"] == rid:
                return r
        raise KeyError(rid)

    def resolve(self, rid: str, status: str, reviewer: str) -> dict[str, Any]:
        rows = self._load()
        for r in rows:
            if r["id"] == rid:
                if r["status"] != "pending":
                    raise ValueError(f"{rid} is already {r['status']}")
                r.update(status=status, reviewer=reviewer, resolved=round(time.time(), 3))
                self._save(rows)
                return r
        raise KeyError(rid)
