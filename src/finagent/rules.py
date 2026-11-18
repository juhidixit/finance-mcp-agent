"""The company's expense policy, as deterministic rules.

The ledger MCP server exposes these through `policy_check`, and the eval graders use
the same functions as ground truth.
"""

from __future__ import annotations

from datetime import date
from typing import Any

MEAL_LIMIT_PER_PERSON_CENTS = 75_00
LODGING_LIMIT_PER_NIGHT_CENTS = 300_00
RECEIPT_REQUIRED_CENTS = 75_00
PROHIBITED_MCCS = {"7995": "gambling", "5813": "bars and lounges"}
DUPLICATE_WINDOW_DAYS = 3

# Chart of accounts used for card spend, and the correct account for each merchant category code.
GL_ACCOUNTS = {
    "6000": ("Travel - Airfare", "Flights and airline fees"),
    "6010": ("Travel - Lodging", "Hotels and short-term lodging"),
    "6020": ("Meals & Entertainment", "Restaurants, cafes, team and client meals"),
    "6030": ("Software & SaaS", "Software subscriptions and licenses"),
    "6040": ("Office Supplies", "Stationery and small office purchases"),
    "6050": ("Ground Transportation", "Taxis, rideshare, parking"),
    "6060": ("Training & Conferences", "Courses, conference tickets"),
    "6070": ("Computer Hardware", "Laptops, monitors, peripherals"),
    "6900": ("Suspense", "Unknown or non-business spend pending review"),
}
MCC_TO_GL = {
    "4511": "6000", "7011": "6010", "5812": "6020", "5814": "6020", "5734": "6030",
    "5943": "6040", "4121": "6050", "7523": "6050", "8299": "6060", "5732": "6070",
    "7995": "6900", "5813": "6900",
}

POLICY_TEXT = f"""Acme Robotics expense policy (synthetic, v2026.2)
1. Meals (GL 6020): at most ${MEAL_LIMIT_PER_PERSON_CENTS // 100} per attendee per transaction.
2. Lodging (GL 6010): at most ${LODGING_LIMIT_PER_NIGHT_CENTS // 100} per night.
3. Receipts are required for any card transaction of ${RECEIPT_REQUIRED_CENTS // 100} or more.
4. Prohibited merchant categories: gambling (MCC 7995) and bars/lounges (MCC 5813).
5. Duplicate charges (same employee, merchant and amount within {DUPLICATE_WINDOW_DAYS} days) must be flagged.
Violating transactions are flagged for review with the rule name as the reason.
Reimbursements are paid only for claims in status 'approved'."""


def violations_for(txn: dict[str, Any], all_txns: list[dict[str, Any]]) -> list[str]:
    """Return rule names that `txn` violates. `all_txns` is needed for duplicate detection."""
    out: list[str] = []
    gl = MCC_TO_GL.get(txn["mcc"])
    amt = txn["amount_cents"]
    if gl == "6020" and amt / max(1, txn["attendees"]) > MEAL_LIMIT_PER_PERSON_CENTS:
        out.append("meal_limit")
    if gl == "6010" and amt > LODGING_LIMIT_PER_NIGHT_CENTS:
        out.append("lodging_limit")
    if amt >= RECEIPT_REQUIRED_CENTS and not txn.get("receipt_id"):
        out.append("missing_receipt")
    if txn["mcc"] in PROHIBITED_MCCS:
        out.append("prohibited_merchant")
    d = date.fromisoformat(txn["txn_date"])
    for o in all_txns:
        if o["id"] >= txn["id"] or o["employee_id"] != txn["employee_id"]:
            continue
        if o["merchant"] == txn["merchant"] and o["amount_cents"] == amt:
            if abs((d - date.fromisoformat(o["txn_date"])).days) <= DUPLICATE_WINDOW_DAYS:
                out.append("duplicate")
                break
    return out
