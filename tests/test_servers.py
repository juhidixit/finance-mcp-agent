import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from finagent.rules import MCC_TO_GL, violations_for
from finagent.store import Store


async def test_stdio_transport(db):
    params = StdioServerParameters(command=sys.executable, args=["-m", "finagent.servers.servers", "ledger", "--db", db])
    async with Client(params) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert tools["search_transactions"].meta == {"untrusted_fields": ["memo"]}
        assert tools["categorize_transaction"].annotations.read_only_hint is False
        r = await c.call_tool("policy_check", {"txn_ids": ["T0490"]})
        assert r.structured_content["T0490"] == ["meal_limit"]


def test_generator_is_deterministic(db, tmp_path):
    from finagent.data.generate import generate

    other = generate(tmp_path / "b.db", seed=7)
    assert Store(db).snapshot() == other.snapshot()


def test_every_mcc_maps_to_an_account(db):
    st = Store(db)
    assert {t["mcc"] for t in st.q("SELECT mcc FROM transactions")} <= set(MCC_TO_GL)
    allt = st.q("SELECT * FROM transactions")
    assert sum(bool(violations_for(t, allt)) for t in allt) > 50
