"""The four MCP servers that make up the company's back office.

* ``ledger``    card transactions, receipts, chart of accounts, expense policy checks
* ``directory`` employee directory
* ``payments``  expense claims, reimbursements, vendors, bills
* ``mail``      outbound email

Each tool declares MCP annotations (read-only / destructive) and, where a field holds text
written by someone outside the finance team (an employee memo, a receipt footer, a vendor
invoice description), lists it in ``meta["untrusted_fields"]``. The gateway uses that
metadata for provenance tracking and the agent uses it for spotlighting.

Servers enforce *business invariants* (a claim must be approved before it is reimbursed).
*Authorization* (who may call what, with which arguments) lives in the gateway.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from finagent.rules import GL_ACCOUNTS, MCC_TO_GL, POLICY_TEXT, violations_for
from finagent.store import Store, cents, dollars

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
MONEY = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
EGRESS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)


def untrusted(*fields: str) -> dict[str, list[str]]:
    return {"untrusted_fields": list(fields)}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _txn_view(t: dict) -> dict:
    return {"id": t["id"], "employee_id": t["employee_id"], "date": t["txn_date"], "merchant": t["merchant"],
            "mcc": t["mcc"], "amount": dollars(t["amount_cents"]), "attendees": t["attendees"],
            "gl_code": t["gl_code"], "receipt_id": t["receipt_id"], "flagged": bool(t["flagged"]),
            "memo": t["memo"]}


# --------------------------------------------------------------------------------------- ledger
def ledger_server(db: str) -> MCPServer:
    s = MCPServer("ledger", instructions="Corporate card ledger for Acme Robotics (synthetic data).")
    st = Store(db)

    @s.tool(annotations=READ, meta=untrusted("memo"))
    def search_transactions(employee_id: str | None = None, start_date: str | None = None,
                            end_date: str | None = None, merchant: str | None = None,
                            uncategorized_only: bool = False, limit: int = 25) -> list[dict]:
        """Search corporate card transactions. Dates are YYYY-MM-DD (inclusive). Amounts are USD.
        `mcc` is the merchant category code; `gl_code` is the booked GL account (null = uncategorized)."""
        sql, p = "SELECT * FROM transactions WHERE 1=1", []
        if employee_id:
            sql += " AND employee_id=?"
            p.append(employee_id)
        if start_date:
            sql += " AND txn_date>=?"
            p.append(start_date)
        if end_date:
            sql += " AND txn_date<=?"
            p.append(end_date)
        if merchant:
            sql += " AND merchant LIKE ?"
            p.append(f"%{merchant}%")
        if uncategorized_only:
            sql += " AND gl_code IS NULL"
        sql += " ORDER BY txn_date, id LIMIT ?"
        p.append(max(1, min(int(limit), 100)))
        return [_txn_view(t) for t in st.q(sql, p)]

    @s.tool(annotations=READ, meta=untrusted("notes"))
    def get_receipt(receipt_id: str) -> dict:
        """Get an itemized receipt. `notes` is free text printed on the receipt."""
        r = st.one("SELECT * FROM receipts WHERE id=?", (receipt_id,))
        if not r:
            raise ToolError(f"receipt {receipt_id} not found")
        return {"id": r["id"], "txn_id": r["txn_id"], "merchant": r["merchant"], "total": dollars(r["total_cents"]),
                "line_items": json.loads(r["line_items"]), "notes": r["notes"]}

    @s.tool(annotations=READ)
    def list_gl_accounts() -> list[dict]:
        """List general-ledger expense accounts (code, name, description)."""
        return st.q("SELECT code, name, description FROM gl_accounts ORDER BY code")

    @s.tool(annotations=READ)
    def get_expense_policy() -> str:
        """Return the company expense policy text."""
        return POLICY_TEXT

    @s.tool(annotations=READ)
    def policy_check(txn_ids: list[str]) -> dict[str, list[str]]:
        """Run the expense-policy rules engine on transactions. Returns violated rule names per id."""
        allt = st.q("SELECT * FROM transactions")
        by_id = {t["id"]: t for t in allt}
        out = {}
        for tid in txn_ids[:100]:
            if tid not in by_id:
                raise ToolError(f"transaction {tid} not found")
            out[tid] = violations_for(by_id[tid], allt)
        return out

    @s.tool(annotations=WRITE)
    def categorize_transaction(txn_id: str, gl_code: str) -> dict:
        """Book one transaction to a general-ledger account. `gl_code` is a 4-digit account code from
        list_gl_accounts (e.g. 6050), not the merchant category code."""
        if gl_code not in GL_ACCOUNTS:
            raise ToolError(f"unknown gl_code {gl_code}")
        if not st.one("SELECT id FROM transactions WHERE id=?", (txn_id,)):
            raise ToolError(f"transaction {txn_id} not found")
        st.exec("UPDATE transactions SET gl_code=? WHERE id=?", (gl_code, txn_id))
        return {"txn_id": txn_id, "gl_code": gl_code, "status": "categorized"}

    @s.tool(annotations=WRITE)
    def flag_transaction(txn_id: str, reason: str) -> dict:
        """Flag a transaction for finance review. `reason` should name the violated policy rule."""
        if not st.one("SELECT id FROM transactions WHERE id=?", (txn_id,)):
            raise ToolError(f"transaction {txn_id} not found")
        st.exec("UPDATE transactions SET flagged=1, flag_reason=? WHERE id=?", (reason[:200], txn_id))
        return {"txn_id": txn_id, "flagged": True, "reason": reason[:200]}

    @s.tool(annotations=READ)
    def spend_summary(group_by: Literal["department", "gl_code", "employee"], start_date: str, end_date: str,
                      department: str | None = None) -> list[dict]:
        """Total card spend (USD) grouped by department, GL account or employee, for a date range.
        Uncategorized transactions are grouped by the account their merchant category maps to."""
        rows = st.q("SELECT t.*, e.department FROM transactions t JOIN employees e ON e.id=t.employee_id "
                    "WHERE txn_date>=? AND txn_date<=?", (start_date, end_date))
        tot: dict[str, int] = {}
        for r in rows:
            if department and r["department"] != department:
                continue
            key = {"department": r["department"], "gl_code": r["gl_code"] or MCC_TO_GL[r["mcc"]],
                   "employee": r["employee_id"]}[group_by]
            tot[key] = tot.get(key, 0) + r["amount_cents"]
        return [{group_by: k, "total": dollars(v)} for k, v in sorted(tot.items())]

    return s


# ------------------------------------------------------------------------------------ directory
def directory_server(db: str) -> MCPServer:
    s = MCPServer("directory", instructions="Employee directory (synthetic).")
    st = Store(db)

    @s.tool(annotations=READ)
    def list_employees(department: str | None = None) -> list[dict]:
        """List employees (id, name, email, department, title, manager_id)."""
        if department:
            return st.q("SELECT * FROM employees WHERE department=? ORDER BY id", (department,))
        return st.q("SELECT * FROM employees ORDER BY id")

    @s.tool(annotations=READ)
    def get_employee(employee_id: str) -> dict:
        """Get one employee record."""
        e = st.one("SELECT * FROM employees WHERE id=?", (employee_id,))
        if not e:
            raise ToolError(f"employee {employee_id} not found")
        return e

    return s


# ------------------------------------------------------------------------------------- payments
def payments_server(db: str) -> MCPServer:
    s = MCPServer("payments", instructions="Expense claims, reimbursements and accounts payable (synthetic).")
    st = Store(db)

    def claim_view(c: dict) -> dict:
        return {"id": c["id"], "employee_id": c["employee_id"], "description": c["description"],
                "amount": dollars(c["amount_cents"]), "expense_date": c["expense_date"], "gl_code": c["gl_code"],
                "status": c["status"]}

    @s.tool(annotations=READ, meta=untrusted("description"))
    def list_expense_claims(employee_id: str | None = None,
                            status: Literal["submitted", "approved", "rejected", "reimbursed"] | None = None
                            ) -> list[dict]:
        """List out-of-pocket expense claims, optionally filtered by employee and status."""
        sql, p = "SELECT * FROM expense_claims WHERE 1=1", []
        if employee_id:
            sql += " AND employee_id=?"
            p.append(employee_id)
        if status:
            sql += " AND status=?"
            p.append(status)
        return [claim_view(c) for c in st.q(sql + " ORDER BY id", p)]

    @s.tool(annotations=WRITE)
    def submit_expense_claim(employee_id: str, description: str, amount: float, expense_date: str,
                             gl_code: str) -> dict:
        """Submit a new out-of-pocket expense claim (status 'submitted')."""
        if gl_code not in GL_ACCOUNTS:
            raise ToolError(f"unknown gl_code {gl_code}")
        if not st.one("SELECT id FROM employees WHERE id=?", (employee_id,)):
            raise ToolError(f"employee {employee_id} not found")
        if not 0 < amount < 10_000:
            raise ToolError("amount must be between 0 and 10000")
        cid = st.next_id("expense_claims", "C")
        st.insert("expense_claims", {"id": cid, "employee_id": employee_id, "description": description[:200],
                                     "amount_cents": cents(amount), "expense_date": expense_date,
                                     "gl_code": gl_code, "status": "submitted", "receipt_id": None})
        return claim_view(st.one("SELECT * FROM expense_claims WHERE id=?", (cid,)))  # type: ignore[arg-type]

    @s.tool(annotations=MONEY)
    def create_reimbursement(employee_id: str, claim_ids: list[str], memo: str = "") -> dict:
        """Reimburse an employee for approved expense claims. The amount is computed from the claims."""
        if not claim_ids:
            raise ToolError("claim_ids is empty")
        total = 0
        for cid in claim_ids:
            c = st.one("SELECT * FROM expense_claims WHERE id=?", (cid,))
            if not c or c["employee_id"] != employee_id:
                raise ToolError(f"claim {cid} not found for employee {employee_id}")
            if c["status"] != "approved":
                raise ToolError(f"claim {cid} has status {c['status']}; only approved claims can be reimbursed")
            total += c["amount_cents"]
        rid = st.next_id("reimbursements", "RB")
        st.insert("reimbursements", {"id": rid, "employee_id": employee_id, "amount_cents": total,
                                     "claim_ids": json.dumps(sorted(claim_ids)), "memo": memo[:200],
                                     "status": "scheduled", "created_at": _now()})
        for cid in claim_ids:
            st.exec("UPDATE expense_claims SET status='reimbursed' WHERE id=?", (cid,))
        return {"reimbursement_id": rid, "employee_id": employee_id, "amount": dollars(total), "status": "scheduled"}

    @s.tool(annotations=READ)
    def list_vendors() -> list[dict]:
        """List vendors (id, name, email). Bank details are not included."""
        return st.q("SELECT id, name, email FROM vendors ORDER BY id")

    @s.tool(annotations=READ, meta=untrusted("description"))
    def list_bills(status: Literal["open", "paid", "on_hold"] | None = None,
                   vendor_id: str | None = None) -> list[dict]:
        """List vendor bills (accounts payable)."""
        sql, p = "SELECT * FROM bills WHERE 1=1", []
        if status:
            sql += " AND status=?"
            p.append(status)
        if vendor_id:
            sql += " AND vendor_id=?"
            p.append(vendor_id)
        return [{"id": b["id"], "vendor_id": b["vendor_id"], "amount": dollars(b["amount_cents"]),
                 "due_date": b["due_date"], "description": b["description"], "status": b["status"]}
                for b in st.q(sql + " ORDER BY due_date, id", p)]

    @s.tool(annotations=MONEY)
    def pay_bill(bill_id: str) -> dict:
        """Pay an open vendor bill in full to the vendor's bank account on file."""
        b = st.one("SELECT * FROM bills WHERE id=?", (bill_id,))
        if not b:
            raise ToolError(f"bill {bill_id} not found")
        if b["status"] != "open":
            raise ToolError(f"bill {bill_id} is {b['status']}")
        v = st.one("SELECT * FROM vendors WHERE id=?", (b["vendor_id"],))
        pid = st.next_id("payments", "P")
        st.insert("payments", {"id": pid, "bill_id": bill_id, "vendor_id": b["vendor_id"],
                               "amount_cents": b["amount_cents"], "bank_account": v["bank_account"],  # type: ignore[index]
                               "created_at": _now()})
        st.exec("UPDATE bills SET status='paid' WHERE id=?", (bill_id,))
        return {"payment_id": pid, "bill_id": bill_id, "amount": dollars(b["amount_cents"]), "status": "sent"}

    @s.tool(annotations=MONEY)
    def update_vendor_bank_account(vendor_id: str, bank_account: str) -> dict:
        """Change the bank account that future payments to a vendor are sent to."""
        if not st.one("SELECT id FROM vendors WHERE id=?", (vendor_id,)):
            raise ToolError(f"vendor {vendor_id} not found")
        st.exec("UPDATE vendors SET bank_account=? WHERE id=?", (bank_account, vendor_id))
        return {"vendor_id": vendor_id, "status": "updated"}

    return s


# ----------------------------------------------------------------------------------------- mail
def mail_server(db: str) -> MCPServer:
    s = MCPServer("mail", instructions="Outbound email.")
    st = Store(db)

    @s.tool(annotations=EGRESS)
    def send_email(to: str, subject: str, body: str) -> dict:
        """Send an email."""
        mid = st.next_id("outbox", "M")
        st.insert("outbox", {"id": mid, "to_addr": to, "subject": subject[:200], "body": body[:5000],
                             "sent_at": _now()})
        return {"message_id": mid, "status": "sent"}

    return s


SERVER_FACTORIES = {"ledger": ledger_server, "directory": directory_server, "payments": payments_server,
                    "mail": mail_server}


def main() -> None:  # pragma: no cover - exercised via stdio in tests
    import argparse

    ap = argparse.ArgumentParser(description="Run one back-office MCP server over stdio")
    ap.add_argument("name", choices=sorted(SERVER_FACTORIES))
    ap.add_argument("--db", required=True)
    a = ap.parse_args()
    SERVER_FACTORIES[a.name](a.db).run()


if __name__ == "__main__":  # pragma: no cover
    main()
