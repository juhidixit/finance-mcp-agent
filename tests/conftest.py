import pytest

from finagent.data.generate import generate
from finagent.gateway.audit import AuditLog
from finagent.gateway.gateway import Gateway
from finagent.gateway.policy import Policy, Principal
from finagent.servers.servers import SERVER_FACTORIES

POLICY = "policies/default.toml"


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "acme.db"
    generate(p, seed=7).close()
    return str(p)


@pytest.fixture
def make_gateway(db, tmp_path):
    def _make(principal="E002", role="finance_analyst", enforce=True, approver=None):
        servers = {n: f(db) for n, f in SERVER_FACTORIES.items()}
        kw = {"approver": approver} if approver else {}
        return Gateway(servers, Policy.load(POLICY), Principal(principal, role),
                       audit=AuditLog(tmp_path / "audit.jsonl"), enforce=enforce, **kw)
    return _make
