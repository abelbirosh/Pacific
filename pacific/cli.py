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
    import os

    from .server import build_apps, servers

    if args.offline_search:
        os.environ["PACIFIC_SEARCH"] = "offline"
    group = servers(config, args.log_level)

    async def main():
        for name, _, port in build_apps(config):
            print(f"  {name:<9} http://{config.host}:{port}")
        await asyncio.gather(*(s.serve() for s in group))

    print("Pacific is up:" + ("  (offline search)" if args.offline_search else ""))
    asyncio.run(main())


def cmd_status(args, config: Config):
    api = Api(config)
    audit = api("GET", "/v1/admin/audit")
    accts = api("GET", "/v1/admin/accounts")["accounts"]
    print(f"Global supply: {wd(audit['total_supply'])} WD   balances sum: {wd(audit['sum_of_balances'])}   "
          f"held: {wd(audit['active_holds'])}   invariant {'OK' if audit['ok'] else 'BROKEN'}\n")
    for kind in ("agent", "person", "service", "escrow", "platform", "treasury"):
        rows = [{"name": a["name"], "owner": a["owner"] or "", "balance": wd(a["balance"]), "held": wd(a["held"]),
                 "skills": ", ".join(a["profile"].get("skills", []))} for a in accts if a["kind"] == kind]
        if rows:
            cols = ["name", "owner", "balance", "held"] + (["skills"] if kind == "agent" else [])
            label = {"treasury": "treasury", "person": "people"}.get(kind, kind + "s")
            print(f"[{label}]\n{table(rows, cols)}\n")


def cmd_faults(args, config: Config):
    from .faults import PROFILES, write_faults

    if args.profile not in PROFILES:
        sys.exit(f"unknown profile {args.profile!r}; choose from {', '.join(PROFILES)}")
    rules = write_faults(config.home, args.profile, seed=args.seed)
    print(f"faults: {args.profile} ({len(rules)} rules) written to {config.home / 'faults.json'}")
    for r in rules:
        print(f"  {r}")


def cmd_sim(args, config: Config):
    import os
    import tempfile

    from .faults import write_faults
    from .sim.traffic import TrafficConfig, format_report, run_traffic

    cfg = TrafficConfig(tasks=args.tasks, duration_s=args.duration, rate=args.rate, concurrency=args.concurrency,
                        seed=args.seed, think_scale=args.think_scale,
                        personas=args.personas.split(",") if args.personas else None,
                        research=not args.no_research)

    def progress(r):
        if not args.quiet:
            print(f"  #{r.n:<4} {r.status:<9} {r.kind:<15} {r.persona:<12} {r.latency_s:7.2f}s {r.spent:8.3f} WD", flush=True)

    def drive(econ: Config) -> dict:
        write_faults(econ.home, args.faults, seed=args.seed)
        try:
            return asyncio.run(run_traffic(econ.exchange_url, econ.secrets(), cfg, progress))
        finally:
            write_faults(econ.home, "none")

    if args.boot:
        from .seed import genesis
        from .server import free_port, running

        if not args.online:
            os.environ["PACIFIC_SEARCH"] = "offline"
        os.environ.setdefault("PACIFIC_VERIFY_BACKOFF", "0.5,2,5")  # retry unreachable checks quickly in a sim
        os.environ.setdefault("PACIFIC_PLATFORM_TIMEOUT", "10")
        econ = Config(home=Path(tempfile.mkdtemp(prefix="pacific-sim-")) / "home", exchange_port=free_port(),
                      services_port=free_port(), agents_port=free_port())
        genesis(econ, seed=args.seed, brain=args.brain)
        print(f"Booted a fresh economy at {econ.home} ({args.brain} brains, faults: {args.faults})")
        with running(econ):
            report = drive(econ)
    else:
        report = drive(config)
    print()
    print(format_report(report))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(f"report written to {args.json}")
    sys.exit(0 if report["ok"] else 1)


def cmd_services(args, config: Config):
    api = Api(config)
    for s in api("GET", "/v1/services", params={"kind": args.kind} if args.kind else None)["services"]:
        print(f"{s['name']}  ({s['kind']}, by {s['provider']})\n  {s['description']}")
        for e in s["endpoints"]:
            print(f"    {e['name']:<13} {e['price']}")
            for p in e.get("promises", []):
                checks = ", ".join(sorted({t["type"] for t in p["checks"]}))
                print(f"      promise {p['terms']}\n        delivers: {'; '.join(d['name'] for d in p['deliverables'])}"
                      f"   judged by: {checks}")
        print()


def cmd_rankings(args, config: Config):
    rows = Api(config)("GET", "/v1/rankings", params={"skill": args.skill} if args.skill else None)["rankings"]
    print(table([{**r, "tier": f"{r['tier']} ({r['payment']})", "score": f"{r['score']:.3f}",
                  "success": f"{r['success']:.2f}", "accuracy": f"{r['accuracy']:.2f}", "on time": f"{r['on_time']:.2f}",
                  "record": f"{r['passed']}/{r['partial']}/{r['failed']}"} for r in rows],
                ["rank", "service", "tier", "provider", "score", "success", "accuracy", "on time", "hires", "record",
                 "unverified", "declined"]))
    print("\nrecord = passed/partial/failed. Scores are smoothed, so a tier with no history scores 0.5.")


def cmd_contracts(args, config: Config):
    data = Api(config)("GET", "/v1/contracts", params={"limit": args.limit, **({"status": args.status} if args.status else {})})
    s = data["summary"]
    print(f"{s['contracts']} contracts {s['statuses']}\npremiums {wd(s['premium_wd'])} WD: released "
          f"{wd(s['released_wd'])}, refunded {wd(s['refunded_wd'])}, in escrow {wd(s['in_escrow_wd'])}\n")
    print(table([{"id": c["id"], "seller": c["service"], "tier": c["tier"], "status": c["status"],
                  "premium": wd(c["premium"]["wd"]), "released": wd(c["released"]["wd"]),
                  "refunded": wd(c["refunded"]["wd"]), "runs": c["attempts"]} for c in data["contracts"]],
                ["id", "seller", "tier", "status", "premium", "released", "refunded", "runs"]))


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
    if r.get("contracts"):
        print("\nContracts:")
        rows = [{"seller": c["service"], "tier": f"{c['tier']} v{c['version']}", "payment": c["payment"],
                 "status": c["status"], "claim": c["claim"] or "", "premium": wd(c["premium"]["wd"]),
                 "released": wd(c["released"]["wd"]), "refunded": wd(c["refunded"]["wd"])} for c in r["contracts"]]
        print(table(rows, ["seller", "tier", "payment", "status", "claim", "premium", "released", "refunded"]))
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

    s = sub.add_parser("sim", help="drive concurrent, realistic agent traffic and check the economy holds up")
    s.add_argument("--boot", action="store_true", help="run against a fresh throwaway economy (offline search)")
    s.add_argument("--brain", default="sim", help="with --boot: agents' brain (default: a mix of personas)")
    s.add_argument("--online", action="store_true", help="with --boot: search the real internet")
    s.add_argument("--tasks", type=int, default=50, help="tasks to send (0: run for --duration)")
    s.add_argument("--duration", type=float, default=0, help="seconds to keep sending tasks")
    s.add_argument("--rate", type=float, default=2.0, help="mean task arrivals per second")
    s.add_argument("--concurrency", type=int, default=8, help="tasks in flight at once")
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--think-scale", type=float, default=0.0, help="persona thinking pauses (1 = realistic)")
    s.add_argument("--personas", help="comma-separated personas to use for every task, instead of each agent's")
    s.add_argument("--faults", default="mild", help="none | mild | harsh | chaos")
    s.add_argument("--no-research", action="store_true", help="skip research tasks (servers without search)")
    s.add_argument("--json", help="write the full report here")
    s.add_argument("--quiet", action="store_true", help="don't print each task as it finishes")
    s.set_defaults(fn=cmd_sim)

    s = sub.add_parser("faults", help="set fault injection on running servers")
    s.add_argument("profile", help="none | mild | harsh | chaos")
    s.add_argument("--seed", type=int)
    s.set_defaults(fn=cmd_faults)

    s = sub.add_parser("serve", help="run the exchange, platform services and agent host")
    s.add_argument("--log-level", default="warning")
    s.add_argument("--offline-search", action="store_true",
                   help="search tiers read a simulated web instead of the internet")
    s.set_defaults(fn=cmd_serve)

    sub.add_parser("status", help="supply, invariant and every balance").set_defaults(fn=cmd_status)

    s = sub.add_parser("services", help="the marketplace catalog with price plans")
    s.add_argument("--kind", choices=["platform", "agent"])
    s.set_defaults(fn=cmd_services)

    s = sub.add_parser("rankings", help="promise tiers ranked by their contracts' track record")
    s.add_argument("--skill")
    s.set_defaults(fn=cmd_rankings)

    s = sub.add_parser("contracts", help="recent contracts and where their premiums went")
    s.add_argument("--status", help="working | verifying | passed | partial | failed | unverified | declined")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(fn=cmd_contracts)

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
