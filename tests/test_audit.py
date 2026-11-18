import json

from finagent.gateway.audit import AuditLog, verify


def _write(path, n=5, key=None):
    log = AuditLog(path, key=key)
    for i in range(n):
        log.append({"event": "tool_call", "tool": "ledger.flag_transaction", "args": {"txn_id": f"T{i}"}})


def test_chain_verifies(tmp_path):
    p = tmp_path / "a.jsonl"
    _write(p)
    r = verify(p, key=b"")
    assert r.ok and r.records == 5


def test_reopen_continues_chain(tmp_path):
    p = tmp_path / "a.jsonl"
    _write(p, 3)
    _write(p, 2)
    assert verify(p, key=b"").ok


def test_edit_is_detected(tmp_path):
    p = tmp_path / "a.jsonl"
    _write(p)
    lines = p.read_text().splitlines()
    rec = json.loads(lines[2])
    rec["args"]["txn_id"] = "T999"
    lines[2] = json.dumps(rec, sort_keys=True)
    p.write_text("\n".join(lines) + "\n")
    r = verify(p, key=b"")
    assert not r.ok and r.first_bad_seq == 3


def test_deletion_is_detected(tmp_path):
    p = tmp_path / "a.jsonl"
    _write(p)
    lines = p.read_text().splitlines()
    del lines[1]
    p.write_text("\n".join(lines) + "\n")
    assert not verify(p, key=b"").ok


def test_hmac_key_required_to_verify(tmp_path):
    p = tmp_path / "a.jsonl"
    _write(p, key=b"secret")
    assert verify(p, key=b"secret").ok
    assert not verify(p, key=b"wrong").ok
