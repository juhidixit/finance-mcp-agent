"""Seeded generator for a synthetic company ledger ("Acme Robotics").

Nothing here is real: names, merchants, vendors and bank accounts are invented, and every
email domain ends in `.example` (reserved by RFC 2606).
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

from finagent.rules import GL_ACCOUNTS, MCC_TO_GL
from finagent.store import Store

COMPANY_DOMAIN = "acme-robotics.example"
TODAY = date(2026, 11, 3)
START = date(2026, 8, 1)
END = date(2026, 10, 31)

FIRST = ["Priya", "Marcus", "Elena", "Kenji", "Amara", "Diego", "Sofia", "Owen", "Leila", "Tomas",
         "Nadia", "Rahul", "Grace", "Ibrahim", "Mei", "Lucas", "Hana", "Victor", "Zoe", "Arjun",
         "Clara", "Mateo", "Ines", "Samuel", "Yara"]
LAST = ["Raman", "Bell", "Novak", "Sato", "Okafor", "Ruiz", "Moretti", "Hughes", "Haddad", "Silva",
        "Petrova", "Mehta", "Kim", "Farah", "Lin", "Weber", "Kato", "Duval", "Brooks", "Nair",
        "Fischer", "Rojas", "Costa", "Adeyemi", "Lund"]
DEPARTMENTS = ["Finance", "Engineering", "Sales", "Marketing", "Operations"]

# name, mcc, typical amount range in dollars
MERCHANTS = [
    ("SkyBridge Airlines", "4511", (180, 720)), ("Coastal Air", "4511", (150, 600)),
    ("Harborview Hotel", "7011", (160, 340)), ("Summit Inn & Suites", "7011", (120, 290)),
    ("Blue Fern Bistro", "5812", (25, 140)), ("Taqueria Sol", "5812", (12, 60)),
    ("Copper Kettle Cafe", "5814", (6, 28)), ("Nori House", "5812", (30, 190)),
    ("CloudDesk Pro", "5734", (20, 240)), ("Pixelboard", "5734", (15, 90)), ("Ledgerly", "5734", (40, 300)),
    ("PaperTrail Office Supply", "5943", (8, 120)), ("Desk Depot", "5943", (20, 260)),
    ("MetroCab", "4121", (9, 70)), ("RideNow", "4121", (8, 65)), ("Parkwise Garage", "7523", (10, 45)),
    ("DevSummit Conf", "8299", (250, 900)), ("LearnLoop", "8299", (30, 400)),
    ("VoltWorks Electronics", "5732", (40, 1600)),
]
PROHIBITED = [("Lucky Star Casino", "7995", (60, 400)), ("The Velvet Lounge", "5813", (40, 220))]

BENIGN_NOTES = ["Thank you for your business!", "Keep this receipt for your records.", "Paid by card ending 4421.",
                "Team lunch", "Client meeting", "Conference travel", "Monthly subscription", ""]

VENDORS = [("Northwind Logistics", "billing@northwind-logistics.example"),
           ("Brightline Facilities", "ar@brightline-facilities.example"),
           ("Quantum Parts Co", "invoices@quantumparts.example"),
           ("Evergreen Legal LLP", "accounts@evergreen-legal.example"),
           ("Atlas Cloud Hosting", "billing@atlascloud.example"),
           ("Cobalt Insurance", "premiums@cobalt-insurance.example"),
           ("Helix Staffing", "ap@helix-staffing.example"),
           ("Sunrise Catering", "orders@sunrise-catering.example")]


def _acct(rng: random.Random) -> str:
    return f"US{rng.randint(10, 99)}-{rng.randint(1000, 9999)}-{rng.randint(100000, 999999)}"


def generate(path: str | Path, seed: int = 7) -> Store:
    rng = random.Random(seed)
    s = Store.create(path)
    s.insert_many("meta", [{"key": "company", "value": "Acme Robotics (synthetic)"},
                           {"key": "today", "value": TODAY.isoformat()},
                           {"key": "seed", "value": str(seed)}])
    s.insert_many("gl_accounts", [{"code": c, "name": n, "description": d} for c, (n, d) in GL_ACCOUNTS.items()])

    # employees: 5 per department, first one is the manager
    emps = []
    for i in range(25):
        dept = DEPARTMENTS[i // 5]
        mgr = i % 5 == 0
        eid = f"E{i + 1:03d}"
        name = f"{FIRST[i]} {LAST[i]}"
        title = f"{dept} Manager" if mgr else ("Finance Analyst" if dept == "Finance" else f"{dept} Specialist")
        emps.append({"id": eid, "name": name, "email": f"{FIRST[i].lower()}.{LAST[i].lower()}@{COMPANY_DOMAIN}",
                     "department": dept, "title": title, "manager_id": None if mgr else f"E{(i // 5) * 5 + 1:03d}"})
    s.insert_many("employees", emps)

    # card transactions
    txns, receipts = [], []
    days = (END - START).days
    for e in emps:
        for _ in range(rng.randint(18, 28)):
            name, mcc, (lo, hi) = rng.choice(PROHIBITED) if rng.random() < 0.012 else rng.choice(MERCHANTS)
            d = START + timedelta(days=rng.randint(0, days))
            amt = round(rng.uniform(lo, hi), 2)
            attendees = rng.choice([1, 1, 2, 3, 4]) if MCC_TO_GL[mcc] == "6020" else 1
            if MCC_TO_GL[mcc] == "6020" and rng.random() < 0.08:  # over the meal limit
                amt = round(attendees * rng.uniform(80, 130), 2)
            txns.append({"employee_id": e["id"], "merchant": name, "mcc": mcc, "amount_cents": int(amt * 100),
                         "txn_date": d.isoformat(), "attendees": attendees, "memo": rng.choice(BENIGN_NOTES[3:])})
    # a few duplicate charges
    for t in rng.sample(txns, 9):
        dup = dict(t)
        dup["txn_date"] = (date.fromisoformat(t["txn_date"]) + timedelta(days=rng.randint(0, 2))).isoformat()
        txns.append(dup)
    txns.sort(key=lambda t: (t["txn_date"], t["employee_id"], t["merchant"]))
    for i, t in enumerate(txns):
        t["id"] = f"T{i + 1:04d}"
        has_receipt = t["amount_cents"] < 75_00 or rng.random() > 0.07
        if has_receipt and rng.random() < 0.9:
            rid = f"R{i + 1:04d}"
            t["receipt_id"] = rid
            receipts.append({"id": rid, "txn_id": t["id"], "merchant": t["merchant"], "total_cents": t["amount_cents"],
                             "line_items": json.dumps([{"desc": "Purchase", "amount": t["amount_cents"] / 100}]),
                             "notes": rng.choice(BENIGN_NOTES)})
        else:
            t["receipt_id"] = None
        # Aug/Sep are closed and categorized; October is open and uncategorized.
        t["gl_code"] = MCC_TO_GL[t["mcc"]] if t["txn_date"] < "2026-10-01" else None
        t["flagged"], t["flag_reason"] = 0, None
    s.insert_many("transactions", txns)
    s.insert_many("receipts", receipts)

    # out-of-pocket expense claims
    claim_kinds = [("Taxi to client site", "6050", (12, 60)), ("Conference parking", "6050", (10, 40)),
                   ("Team lunch (receipt attached)", "6020", (30, 140)), ("Online course", "6060", (40, 300)),
                   ("USB-C dock", "6070", (60, 220)), ("Printer paper and toner", "6040", (15, 90)),
                   ("Hotel incidentals", "6010", (20, 120))]
    claims = []
    for i in range(70):
        e = rng.choice(emps)
        desc, gl, (lo, hi) = rng.choice(claim_kinds)
        d = START + timedelta(days=rng.randint(0, days))
        claims.append({"id": f"C{i + 1:04d}", "employee_id": e["id"], "description": desc,
                       "amount_cents": int(round(rng.uniform(lo, hi), 2) * 100), "expense_date": d.isoformat(),
                       "gl_code": gl, "status": rng.choice(["submitted", "approved", "approved", "rejected", "reimbursed"]),
                       "receipt_id": None})
    s.insert_many("expense_claims", claims)

    vendors = [{"id": f"V{i + 1:03d}", "name": n, "email": em, "bank_account": _acct(rng)}
               for i, (n, em) in enumerate(VENDORS)]
    s.insert_many("vendors", vendors)
    bills = []
    for i in range(24):
        v = rng.choice(vendors)
        due = TODAY + timedelta(days=rng.randint(-20, 30))
        bills.append({"id": f"B{i + 1:04d}", "vendor_id": v["id"], "amount_cents": int(round(rng.uniform(300, 9000), 2) * 100),
                      "due_date": due.isoformat(), "description": f"{v['name']} invoice INV-{rng.randint(10000, 99999)}",
                      "status": "paid" if due < TODAY - timedelta(days=10) else "open"})
    s.insert_many("bills", bills)
    return s


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Generate the synthetic ledger")
    ap.add_argument("--out", default="data/acme.db")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    generate(a.out, a.seed).close()
    print(f"wrote {a.out}")
