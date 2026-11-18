"""Facts about the seed-7 ledger that the eval scenarios depend on."""

from evals.scenarios import build_scenarios, oct_txns
from finagent.rules import violations_for
from finagent.store import Store


def test_scenario_facts(db):
    st = Store(db)
    assert len(oct_txns(st, "E013")) == 5
    allt = st.q("SELECT * FROM transactions")
    e011 = [t for t in oct_txns(st, "E011") if violations_for(t, allt)]
    assert {t["id"] for t in e011} == {"T0490", "T0499", "T0563"}
    approved = lambda e: {c["id"] for c in st.q(  # noqa: E731
        "SELECT id FROM expense_claims WHERE employee_id=? AND status='approved'", (e,))}
    assert approved("E024") == {"C0024", "C0031", "C0063"}
    assert approved("E019") == {"C0006", "C0048", "C0056"}
    assert st.one("SELECT status FROM bills WHERE id='B0006'")["status"] == "open"
    assert st.one("SELECT status FROM bills WHERE id='B0002'")["status"] == "open"


def test_build_scenarios(db):
    scs = build_scenarios(Store(db))
    assert len(scs) == 18 and sum(s.is_attack for s in scs) == 10
    assert len({s.id for s in scs}) == 18
