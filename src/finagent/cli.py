"""`finagent` command line."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp.client.stdio import StdioServerParameters

from finagent.data.generate import TODAY, generate
from finagent.gateway.approvals import ApprovalQueue
from finagent.gateway.audit import AuditLog, verify
from finagent.gateway.gateway import ApprovalRequest, Gateway
from finagent.gateway.policy import Policy, Principal
from finagent.servers.servers import SERVER_FACTORIES
from finagent.store import Store

DEFAULT_POLICY = Path(__file__).resolve().parents[2] / "policies" / "default.toml"


def _servers(db: str, transport: str) -> dict:
    if transport == "stdio":  # one OS process per MCP server, as in production
        return {n: StdioServerParameters(command=sys.executable, args=["-m", "finagent.servers.servers", n, "--db", db])
                for n in SERVER_FACTORIES}
    return {n: f(db) for n, f in SERVER_FACTORIES.items()}


def _name(db: str, emp: str) -> str:
    r = Store(db).one("SELECT name FROM employees WHERE id=?", (emp,))
    return r["name"] if r else emp


def _gateway(a: argparse.Namespace, approver=None) -> Gateway:
    kw = {"approver": approver} if approver else {"approver": ApprovalQueue(a.approvals)}
    return Gateway(_servers(a.db, a.transport), Policy.load(a.policy), Principal(a.principal, a.role),
                   audit=AuditLog(a.audit), enforce=not a.no_enforce, **kw)


async def cmd_run(a: argparse.Namespace) -> None:
    from finagent.agent.llm import AnthropicChat, LlamaCppChat, LlamaCppModel
    from finagent.agent.loop import run_agent

    if a.model:
        chat = LlamaCppChat(LlamaCppModel(a.model, model_id=Path(a.model).stem))
    else:
        chat = AnthropicChat(model=a.anthropic_model)
    async with _gateway(a) as gw:
        ep = await run_agent(chat, gw, a.instruction, principal_name=_name(a.db, a.principal),
                             today=TODAY.isoformat(), spotlighting=a.spotlight)
    for s in ep.steps:
        print(f"  {s.status:17s} {s.tool}({json.dumps(s.args)})")
    print(f"\n{ep.final_text}\n\n[{ep.turns} turns, {ep.seconds}s, audit -> {a.audit}]")


async def cmd_serve(a: argparse.Namespace) -> None:
    from mcp.server.stdio import stdio_server

    from finagent.gateway.server import build_proxy_server

    async with _gateway(a) as gw:
        srv = build_proxy_server(gw)
        async with stdio_server() as (r, w):
            await srv.run(r, w, srv.create_initialization_options())


async def cmd_approve(a: argparse.Namespace) -> None:
    q = ApprovalQueue(a.approvals)
    req = q.get(a.id)
    if a.action == "reject":
        q.resolve(a.id, "rejected", a.reviewer)
        AuditLog(a.audit).append({"event": "approval", "approval_id": a.id, "decision": "rejected",
                                  "reviewer": a.reviewer})
        print(f"{a.id} rejected")
        return

    async def yes(r: ApprovalRequest) -> bool:
        return True

    a.principal, a.role = req["principal"]["id"], req["principal"]["role"]
    async with _gateway(a, approver=yes) as gw:
        tool = req["tool"].split(".", 1)[1]
        gw.set_instruction(f"human approval {a.id} by {a.reviewer}")
        gw.taint.trusted.update(str(v) for v in _flat(req["args"]))  # the reviewer vouches for these values
        res = await gw.call(tool, req["args"])
    q.resolve(a.id, "approved", a.reviewer)
    print(f"{a.id} approved and executed: {res.to_text()}")


def _flat(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _flat(v)
    elif isinstance(x, list):
        for v in x:
            yield from _flat(v)
    else:
        yield x


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="finagent", description="Finance-ops agent over MCP with a policy gateway")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", default="data/acme.db")
        p.add_argument("--policy", default=str(DEFAULT_POLICY))
        p.add_argument("--principal", default="E002", help="employee id the agent acts for")
        p.add_argument("--role", default="finance_analyst")
        p.add_argument("--audit", default="data/audit.jsonl")
        p.add_argument("--approvals", default="data/approvals.jsonl")
        p.add_argument("--transport", choices=["stdio", "inprocess"], default="stdio")
        p.add_argument("--no-enforce", action="store_true", help="disable the policy (baseline; still audited)")

    p = sub.add_parser("seed", help="generate the synthetic ledger")
    p.add_argument("--db", default="data/acme.db")
    p.add_argument("--seed", type=int, default=7)

    p = sub.add_parser("run", help="run the agent on one instruction")
    common(p)
    p.add_argument("instruction")
    p.add_argument("--model", help="local GGUF model (llama.cpp)")
    p.add_argument("--anthropic-model", default="claude-opus-5-5")
    p.add_argument("--spotlight", action="store_true")

    p = sub.add_parser("serve-gateway", help="serve the policy gateway as an MCP server over stdio")
    common(p)

    p = sub.add_parser("audit", help="verify an audit log's hash chain")
    p.add_argument("path")

    p = sub.add_parser("approvals", help="review queued approval requests")
    common(p)
    p.add_argument("action", choices=["list", "approve", "reject"])
    p.add_argument("id", nargs="?")
    p.add_argument("--reviewer", default="controller")

    a = ap.parse_args(argv)
    if a.cmd == "seed":
        Path(a.db).parent.mkdir(parents=True, exist_ok=True)
        generate(a.db, a.seed).close()
        print(f"wrote {a.db}")
    elif a.cmd == "run":
        asyncio.run(cmd_run(a))
    elif a.cmd == "serve-gateway":
        asyncio.run(cmd_serve(a))
    elif a.cmd == "audit":
        r = verify(a.path)
        print(f"{'OK' if r.ok else 'TAMPERED'}: {r.records} records" +
              ("" if r.ok else f", first bad seq {r.first_bad_seq}: {r.error}"))
        sys.exit(0 if r.ok else 1)
    elif a.cmd == "approvals":
        if a.action == "list":
            for r in ApprovalQueue(a.approvals).list():
                print(f"{r['id']}  {r['tool']}({json.dumps(r['args'])})  by {r['principal']['id']}: {r['reason']}")
        else:
            if not a.id:
                ap.error("approve/reject needs an id")
            asyncio.run(cmd_approve(a))


if __name__ == "__main__":
    main()
