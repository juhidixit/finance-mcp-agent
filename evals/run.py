"""Run the eval matrix: scenarios x defense configurations.

Examples
--------
Record a run with a local model (writes a cassette so the run can be replayed exactly):

    python -m evals.run --model models/qwen3-4b-instruct-2507-q4_k_m.gguf --model-id qwen3-4b-instruct-2507-q4_k_m \
        --mode record --cassette evals/cassettes/qwen3-4b.jsonl --out results/qwen3-4b

Replay it without the model (what CI does) and check the results did not drift:

    python -m evals.run --model-id qwen3-4b-instruct-2507-q4_k_m --mode replay \
        --cassette evals/cassettes/qwen3-4b.jsonl --out /tmp/replay --check results/qwen3-4b/episodes.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from evals.scenarios import Ctx, Scenario, build_scenarios
from finagent.agent.llm import Cassette, LlamaCppChat, LlamaCppModel
from finagent.agent.loop import run_agent
from finagent.data.generate import TODAY, generate
from finagent.gateway.audit import AuditLog
from finagent.gateway.gateway import Gateway
from finagent.gateway.policy import Policy, Principal
from finagent.servers.servers import SERVER_FACTORIES
from finagent.store import Store

CONFIGS = {
    "none": dict(enforce=False, spotlight=False),
    "spotlight": dict(enforce=False, spotlight=True),
    "gateway": dict(enforce=True, spotlight=False),
    "gateway+spotlight": dict(enforce=True, spotlight=True),
}
ROOT = Path(__file__).resolve().parents[1]
DETERMINISTIC_FIELDS = ("config", "scenario", "utility", "attack_success", "injection_seen", "statuses", "turns",
                        "stop_reason")


async def run_episode(sc: Scenario, cfg_name: str, base_db: Path, work: Path, model: LlamaCppModel,
                      policy: Policy, audit_dir: Path) -> dict:
    cfg = CONFIGS[cfg_name]
    db = work / f"{cfg_name}_{sc.id.replace('/', '_').replace('+', '_')}.db"
    shutil.copy(base_db, db)
    st = Store(db)
    for sql, params in sc.setup_sql:
        st.exec(sql, params)
    emp, role, name = sc.task.principal
    servers = {n: f(str(db)) for n, f in SERVER_FACTORIES.items()}
    audit = AuditLog(audit_dir / f"{cfg_name}.jsonl")
    async with Gateway(servers, policy, Principal(emp, role), audit=audit, enforce=cfg["enforce"],
                       session_id=f"{cfg_name}:{sc.id}") as gw:
        ep = await run_agent(LlamaCppChat(model), gw, sc.task.instruction, principal_name=name,
                             today=TODAY.isoformat(), spotlighting=cfg["spotlight"])
        pending = list(gw.pending)
    ctx = Ctx(st, ep.final_text, pending)
    rec = {
        "config": cfg_name, "scenario": sc.id, "task": sc.task.id, "attack": sc.attack.id if sc.attack else None,
        "in_scope_attack": sc.attack.in_scope if sc.attack else None,
        "utility": bool(sc.task.check(ctx)),
        "attack_success": bool(sc.attack.check(st)) if sc.attack else None,
        "injection_seen": any("<INFORMATION>" in t for t in ep.observed) if sc.attack else None,
        "statuses": [s.status for s in ep.steps],
        "denied": sum(s.status == "denied" for s in ep.steps),
        "pending_approval": sum(s.status == "pending_approval" for s in ep.steps),
        "turns": ep.turns, "stop_reason": ep.stop_reason,
        "prompt_tokens": ep.prompt_tokens, "completion_tokens": ep.completion_tokens, "seconds": ep.seconds,
        "steps": [asdict(s) for s in ep.steps], "final_text": ep.final_text,
    }
    st.close()
    return rec


async def main_async(a: argparse.Namespace) -> int:
    cassette = Cassette(a.cassette, a.mode if a.mode != "live" else "off")
    model = LlamaCppModel(a.model, model_id=a.model_id, cassette=cassette, n_threads=a.threads)
    policy = Policy.load(a.policy)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="finagent-eval-"))
    base = work / "base.db"
    st = generate(base, seed=7)
    scenarios = build_scenarios(st)
    st.close()
    if a.only:
        scenarios = [s for s in scenarios if any(o in s.id for o in a.only.split(","))]
    configs = a.configs.split(",")
    ep_path = out / "episodes.jsonl"
    done = set()
    if a.resume and ep_path.exists():
        done = {(r["config"], r["scenario"]) for r in map(json.loads, ep_path.read_text().splitlines())}
    elif ep_path.exists():
        ep_path.unlink()
    audit_dir = out / "audit"
    if not a.resume and audit_dir.exists():
        shutil.rmtree(audit_dir)
    for cfg in configs:
        # Group by principal so consecutive episodes share the llama.cpp KV-cache prefix.
        for sc in sorted(scenarios, key=lambda s: (s.task.principal[1], s.task.principal[0])):
            if (cfg, sc.id) in done:
                continue
            rec = await run_episode(sc, cfg, base, work, model, policy, audit_dir)
            with ep_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            atk = "" if rec["attack_success"] is None else f" attack={'HIT' if rec['attack_success'] else 'blocked'}"
            print(f"[{cfg:18s}] {sc.id:55s} utility={int(rec['utility'])}{atk} turns={rec['turns']} "
                  f"{rec['seconds']}s", flush=True)
    shutil.rmtree(work, ignore_errors=True)
    if a.check:
        return check(ep_path, Path(a.check))
    return 0


def check(new: Path, ref: Path) -> int:
    def load(p: Path) -> dict:
        return {(r["config"], r["scenario"]): {k: r[k] for k in DETERMINISTIC_FIELDS}
                for r in map(json.loads, p.read_text().splitlines())}

    a, b = load(new), load(ref)
    bad = [k for k in b if a.get(k) != b[k]]
    if bad:
        print(f"DRIFT in {len(bad)} episodes, e.g. {bad[:3]}", file=sys.stderr)
        return 1
    print(f"replay matches reference: {len(b)} episodes")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", help="path to a GGUF model (not needed in replay mode)")
    ap.add_argument("--model-id", required=True, help="name recorded in the cassette key")
    ap.add_argument("--mode", choices=["record", "replay", "live"], default="record")
    ap.add_argument("--cassette")
    ap.add_argument("--policy", default=str(ROOT / "policies" / "default.toml"))
    ap.add_argument("--configs", default=",".join(CONFIGS))
    ap.add_argument("--only", help="comma-separated substrings of scenario ids")
    ap.add_argument("--out", required=True)
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--check", help="reference episodes.jsonl to compare against (exit 1 on drift)")
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
