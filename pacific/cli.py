"""pacific: command line for the wave-dollar economy."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from .config import Config, load_config


# ---- helpers ------------------------------------------------------------------------------------


def table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "(none)"
    cells = [[str(r.get(c, "")) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    line = lambda vals: "  ".join(v.ljust(w) for v, w in zip(vals, widths)).rstrip()
    return "\n".join([line(cols), line(["-" * w for w in widths]), *(line(r) for r in cells)])


def wd(x: float) -> str:
    return f"{x:,.4f}"


class Api:
    def __init__(self, config: Config, key: str | None = None):
        self.config = config
        self.key = key or config.secrets()["admin_key"]
        self.http = httpx.Client(base_url=config.exchange_url, timeout=900,
                                 headers={"Authorization": f"Bearer {self.key}"})

    def __call__(self, method: str, path: str, **kw):
        try:
            r = self.http.request(method, path, **kw)
        except httpx.ConnectError:
            sys.exit(f"Can't reach the exchange at {self.config.exchange_url}. Start it with `pacific serve`.")
        data = r.json()
        if not r.is_success:
            sys.exit(f"error {r.status_code}: {data.get('detail', data)}")
        return data


def agent_key(config: Config, ref: str) -> tuple[str, str]:
    for aid, a in config.secrets()["agents"].items():
        if ref in (aid, a["name"]) or ref.lower() == a["name"].lower():
            return aid, a["key"]
    sys.exit(f"no agent {ref!r}")


# ---- commands -----------------------------------------------------------------------------------


def cmd_init(args, config: Config):
    from .seed import genesis

    try:
        out = genesis(config, seed=args.seed, supply=args.supply, force=args.force, brain=args.brain)
    except (RuntimeError, ValueError) as e:
        sys.exit(str(e))
    s = out["secrets"]
    print(f"Genesis complete at {config.home}")
    print(f"Supply minted: {wd(args.supply)} WD   audit ok: {out['audit']['ok']}\n")
    rows = [
        {"agent": a["name"], "owner": s["people"][a["owner"]]["name"], "skills for hire": ", ".join(a["skills"]),
         "brain": a["brain"]}
        for a in s["agents"].values()
    ]
    print(table(rows, ["agent", "owner", "skills for hire", "brain"]))
    print("\nNext: `pacific serve`, then `pacific status` or `pacific task run tasks/research.json`.")


def cmd_serve(args, config: Config):
    import uvicorn

    from .agents.host import create_app as agents_app
    from .exchange.app import create_app as exchange_app
    from .services.app import create_app as services_app

    secrets = config.secrets()
    apps = [
        ("exchange", exchange_app(config), config.exchange_port),
        ("services", services_app(config.data_dir, secrets["gateway_secret"]), config.services_port),
        ("agents", agents_app(config), config.agents_port),
    ]

    async def main():
        servers = [
            uvicorn.Server(uvicorn.Config(app, host=config.host, port=port, log_level=args.log_level))
            for _, app, port in apps
        ]
        for name, _, port in apps:
            print(f"  {name:<9} http://{config.host}:{port}")
        await asyncio.gather(*(s.serve() for s in servers))

    print("Pacific is up:")
    asyncio.run(main())


def cmd_status(args, config: Config):
    api = Api(config)
    audit = api("GET", "/v1/admin/audit")
    accts = api("GET", "/v1/admin/accounts")["accounts"]
    print(f"Global supply: {wd(audit['total_supply'])} WD   balances sum: {wd(audit['sum_of_balances'])}   "
          f"held: {wd(audit['active_holds'])}   invariant {'OK' if audit['ok'] else 'BROKEN'}\n")
    for kind in ("agent", "person", "service", "platform", "treasury"):
        rows = [{"name": a["name"], "owner": a["owner"] or "", "balance": wd(a["balance"]), "held": wd(a["held"]),
                 "skills": ", ".join(a["profile"].get("skills", []))} for a in accts if a["kind"] == kind]
        if rows:
            cols = ["name", "owner", "balance", "held"] + (["skills"] if kind == "agent" else [])
            label = {"treasury": "treasury", "person": "people"}.get(kind, kind + "s")
            print(f"[{label}]\n{table(rows, cols)}\n")


def cmd_services(args, config: Config):
    api = Api(config)
    for s in api("GET", "/v1/services", params={"kind": args.kind} if args.kind else None)["services"]:
        print(f"{s['name']}  ({s['kind']}, by {s['provider']})\n  {s['description']}")
        for e in s["endpoints"]:
            print(f"    {e['name']:<13} {e['price']}")
        print()


def cmd_ledger(args, config: Config):
    api = Api(config)
    params = {"limit": args.limit}
    if args.account:
        params["account"] = args.account
    txs = api("GET", "/v1/admin/transactions", params=params)["transactions"]
    rows = [{"#": t["id"], "kind": t["kind"], "from": t["from_name"] or "-", "to": t["to_name"],
             "amount": wd(t["amount"]), "memo": t["memo"][:40]} for t in reversed(txs)]
    print(table(rows, ["#", "kind", "from", "to", "amount", "memo"]))


def cmd_audit(args, config: Config):
    print(json.dumps(Api(config)("GET", "/v1/admin/audit"), indent=2))


def cmd_fund(args, config: Config):
    secrets = config.secrets()
    aid, _ = agent_key(config, args.agent)
    owner = secrets["agents"][aid]["owner"]
    api = Api(config, key=secrets["people"][owner]["key"])
    api("POST", "/v1/transfer", json={"to": aid, "amount": args.amount, "memo": "owner top-up"})
    print(f"{secrets['people'][owner]['name']} sent {wd(args.amount)} WD to {secrets['agents'][aid]['name']}")


def cmd_call(args, config: Config):
    """Make a raw purchase as an agent, outside any task (handy for poking at services)."""
    _, key = agent_key(config, args.agent)
    api = Api(config, key=key)
    body = json.loads(args.body) if args.body else {}
    print(json.dumps(api("POST", f"/v1/call/{args.service}/{args.endpoint}", json=body), indent=2)[: args.max_chars])


def print_report(r: dict):
    print(f"\nTask {r['id']}  [{r['status'].upper()}]  {r['title']}")
    print(f"Agent: {r['agent']}   budget {r['budget']['display']}   spent {r['spent']['display']}   "
          f"workspace {r['workspace']}")
    if r["calls"]:
        print("\nCalls:")
        rows = [{"by": ("  " * c["depth"]) + (c["caller"] or ""), "service": f"{c['service']}/{c['endpoint']}",
                 "status": c["status"], "units": c["units"], "cost": wd(c["cost_wd"]),
                 "ms": c["latency_ms"]} for c in r["calls"]]
        print(table(rows, ["by", "service", "status", "units", "cost", "ms"]))
    if r["checks"]:
        print("\nChecks:")
        for c in r["checks"]:
            print(f"  [{'PASS' if c['passed'] else 'FAIL'}] {c['check']['type']}: {c['detail']}")
    if r["output"]:
        out = r["output"]
        print(f"\nOutput:\n{out[:3000]}{'...' if len(out) > 3000 else ''}")


def cmd_task_run(args, config: Config):
    spec = json.loads(Path(args.file).read_text())
    if args.agent:
        spec["agent"] = args.agent
    if args.budget:
        spec["budget"] = args.budget
    if "agent" not in spec:
        sys.exit("say which agent: --agent NAME (see `pacific status`)")
    api = Api(config)
    task = api("POST", "/v1/tasks", json=spec)
    print(f"Created {task['id']} for {task['agent']} with budget {task['budget']['display']}; running...")
    report = api("POST", f"/v1/tasks/{task['id']}/run")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)


def cmd_task_show(args, config: Config):
    report = Api(config)("GET", f"/v1/tasks/{args.id}")
    print(json.dumps(report, indent=2) if args.json else "", end="")
    if not args.json:
        print_report(report)
        if args.trace:
            print("\nTrace:")
            for t in report["trace"]:
                print(f"  {'ok ' if t['ok'] else 'ERR'} {t['tool']:<16} {t['summary'][:110]}")


def cmd_tasks(args, config: Config):
    rows = Api(config)("GET", "/v1/tasks")["tasks"]
    print(table([{**r, "budget": wd(r["budget"]), "spent": wd(r["spent"])} for r in rows],
                ["id", "status", "agent", "budget", "spent", "title"]))


def main(argv: list[str] | None = None):
    p = argparse.ArgumentParser(prog="pacific", description="Wave-dollar economy for budgeted agents.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="genesis: mint supply, create people, agents and services")
    s.add_argument("--seed", type=int, help="RNG seed for reproducible names/skills/prices")
    s.add_argument("--supply", type=float, default=1_000_000, help="wave dollars to mint")
    s.add_argument("--force", action="store_true", help="wipe an existing economy")
    s.add_argument("--brain", default="scripted",
                   help='every agent\'s brain: "scripted", "sim" (a mix of personas) or "sim:<persona>"')
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("serve", help="run the exchange, platform services and agent host")
    s.add_argument("--log-level", default="warning")
    s.set_defaults(fn=cmd_serve)

    sub.add_parser("status", help="supply, invariant and every balance").set_defaults(fn=cmd_status)

    s = sub.add_parser("services", help="the marketplace catalog with price plans")
    s.add_argument("--kind", choices=["platform", "agent"])
    s.set_defaults(fn=cmd_services)

    s = sub.add_parser("ledger", help="recent transactions")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--account")
    s.set_defaults(fn=cmd_ledger)

    sub.add_parser("audit", help="check supply == sum of balances").set_defaults(fn=cmd_audit)

    s = sub.add_parser("fund", help="an agent's owner tops up the agent's wallet")
    s.add_argument("agent")
    s.add_argument("amount", type=float)
    s.set_defaults(fn=cmd_fund)

    s = sub.add_parser("call", help="make a raw service call as an agent")
    s.add_argument("agent")
    s.add_argument("service")
    s.add_argument("endpoint")
    s.add_argument("body", nargs="?", help="JSON request body")
    s.add_argument("--max-chars", type=int, default=6000)
    s.set_defaults(fn=cmd_call)

    t = sub.add_parser("task", help="create/run/show tasks").add_subparsers(dest="task_cmd", required=True)
    s = t.add_parser("run", help="give an agent a task (JSON file) and a budget, run it, check completion")
    s.add_argument("file")
    s.add_argument("--agent")
    s.add_argument("--budget", type=float)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_task_run)
    s = t.add_parser("show")
    s.add_argument("id")
    s.add_argument("--json", action="store_true")
    s.add_argument("--trace", action="store_true")
    s.set_defaults(fn=cmd_task_show)
    sub.add_parser("tasks", help="list tasks").set_defaults(fn=cmd_tasks)

    args = p.parse_args(argv)
    args.fn(args, load_config())


if __name__ == "__main__":
    main()
