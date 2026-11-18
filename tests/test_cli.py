from finagent.cli import main
from finagent.gateway.approvals import ApprovalQueue
from finagent.store import Store


async def test_queue_then_human_approves_over_stdio(make_gateway, db, tmp_path):
    approvals = tmp_path / "approvals.jsonl"
    async with make_gateway(approver=ApprovalQueue(approvals)) as gw:
        r = await gw.call("pay_bill", {"bill_id": "B0006"})
    assert r.status == "pending_approval" and "AR-0001" in r.message
    audit = tmp_path / "audit.jsonl"
    args = ["--db", db, "--approvals", str(approvals), "--audit", str(audit)]
    # main() runs its own event loop, so call it from a worker thread
    import asyncio
    await asyncio.to_thread(main, ["approvals", "approve", "AR-0001", *args])
    assert Store(db).one("SELECT status FROM bills WHERE id='B0006'")["status"] == "paid"
    assert ApprovalQueue(approvals).get("AR-0001")["status"] == "approved"


def test_seed_and_audit_verify(tmp_path, capsys):
    import pytest

    db = tmp_path / "x.db"
    main(["seed", "--db", str(db)])
    assert Store(db).one("SELECT COUNT(*) n FROM transactions")["n"] == 569
    log = tmp_path / "audit.jsonl"
    from finagent.gateway.audit import AuditLog

    AuditLog(log, key=b"").append({"event": "x"})
    with pytest.raises(SystemExit) as e:
        main(["audit", str(log)])
    assert e.value.code == 0 and "OK: 1 records" in capsys.readouterr().out
