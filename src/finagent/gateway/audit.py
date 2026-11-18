"""Append-only, hash-chained audit log.

Each record stores the SHA-256 of the previous record, so editing, deleting or reordering any
line breaks the chain from that point on. With a key (``FINAGENT_AUDIT_KEY``) the hash becomes an
HMAC, so someone with write access to the file but not the key cannot rebuild a valid chain.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def _digest(payload: bytes, key: bytes | None) -> str:
    if key:
        return hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def sha256_of(obj: Any) -> str:
    return hashlib.sha256(_canon(obj)).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path | None, key: bytes | None = None):
        self.path = Path(path) if path else None
        self.key = key if key is not None else (os.environ.get("FINAGENT_AUDIT_KEY", "").encode() or None)
        self.records: list[dict[str, Any]] = []
        self._prev = GENESIS
        self._seq = 0
        if self.path and self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._prev, self._seq = rec["hash"], rec["seq"]

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        rec = {"seq": self._seq, "ts": round(time.time(), 3), **event, "prev_hash": self._prev}
        rec["hash"] = _digest(_canon(rec), self.key)
        self._prev = rec["hash"]
        self.records.append(rec)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
        return rec


@dataclass
class VerifyResult:
    ok: bool
    records: int
    first_bad_seq: int | None = None
    error: str = ""


def verify(path: str | Path, key: bytes | None = None) -> VerifyResult:
    """Re-compute the chain. Returns the first record that does not verify, if any."""
    key = key if key is not None else (os.environ.get("FINAGENT_AUDIT_KEY", "").encode() or None)
    prev, n, expected_seq = GENESIS, 0, 1
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        n += 1
        claimed = rec.pop("hash", None)
        if rec.get("seq") != expected_seq:
            return VerifyResult(False, n, rec.get("seq"), f"expected seq {expected_seq}, found {rec.get('seq')}")
        if rec.get("prev_hash") != prev:
            return VerifyResult(False, n, rec["seq"], "prev_hash does not match previous record")
        if _digest(_canon(rec), key) != claimed:
            return VerifyResult(False, n, rec["seq"], "record hash mismatch (contents altered)")
        prev, expected_seq = claimed, expected_seq + 1
    return VerifyResult(True, n)
