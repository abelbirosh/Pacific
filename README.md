# Pacific

A small economy where AI agents get a **budget** and a **task**, and spend **wave dollars (WD)** on real services, or hire each other, to get the task done. Completion is checked programmatically against real state.

```
            ┌──────────────────────────── Exchange :8700 ─────────────────────────────┐
 tasks ───▶ │ auth · catalog · price plans · holds/charges · job budgets · checks     │
            │                        ledger.sqlite (WD)                                │
            └───────┬───────────────────────────────────────────────┬─────────────────┘
                    │ metered calls                                  │ run task / delegate
                    ▼                                                ▼
     Platform services :8701                              Agent host :8702
     ocean_exa / ocean_tavily / ocean_brave  (search)     5 agents, each with a pluggable brain
     ocean_db       (real SQLite per workspace)           and 2-4 skills it sells to other agents
     ocean_shield   (bandit, secret rules, OSV CVEs)        └─ calls back into the exchange,
     ocean_sandbox  (run Python, isolated)                     paying from its own wallet
     ocean_storage  (files per workspace)
```

## Quick start

```bash
uv sync
uv run pacific init --seed 7      # genesis: mint 1,000,000 WD, people, agents, services
uv run pacific serve              # exchange :8700, services :8701, agents :8702
```

In another terminal:

```bash
uv run pacific status                                     # supply, invariant, every balance
uv run pacific services                                   # catalog with price plans
uv run pacific task run tasks/research.json --agent <name>
uv run pacific task run tasks/build_feature.json --agent <name>
uv run pacific task show <task_id> --trace
uv run pacific ledger
```

`pacific init` without `--seed` gives fresh random names, owners, skills and prices.

## Concepts

**Wave dollars.** One global supply (`meta.total_supply`), minted only by the treasury at genesis or by an admin mint. Amounts are integer micro-WD. Every movement is a row in an append-only `transactions` table. The invariant *supply = sum of balances = sum of mints* is checked by `pacific audit`.

**Accounts.** `treasury` → `person` (random generated owners, given a genesis grant) → `agent` (funded by its owner). Each platform service has its own `service` account under `Pacific Platform`, so revenue per service is visible.

**Price plans** (`pacific/pricing.py`). Every endpoint has one:
- *metered*: `per_call + per_unit × units`, where the provider reports the units (pages read, rows, KLOC, seconds, KB). A cap is set by `max_units`, by a request field (`units_param`), or by measuring the request (`units_from`, e.g. `kb:content`).
- *delegation*: `fee + (hired agent's actual spend) × (1 + markup%)`.

**Holds.** Before a call, the exchange reserves the worst-case cost. A call it can't afford gets a **402**. Afterwards it charges the actual cost, which is never more than the hold, and releases the rest.

**Jobs.** A job is a budget scope. Each task opens a root job. When an agent calls with `X-Pacific-Job`, spending counts against that budget. Hiring an agent opens a *child job* for the seller with the budget the buyer chose. The seller pays its own platform costs, and the buyer is then billed fee + cost-plus. Everything created anywhere in one task tree lands in the root job's **workspace**, so checks can find it.

**Checks** (`pacific/exchange/checks.py`). Once the agent finishes, the exchange inspects real state: `storage_contains`, `storage_min_bytes`, `db_table_exists`, `db_columns`, `db_min_rows`, `db_query_equals`, `security_scan_clean`, `output_contains`, `output_matches`, `max_spend`, `used_service`. A task **passes** only if the agent reports completion *and* every check passes.

## Platform services

| Service | What's real | Price |
|---|---|---|
| `ocean_brave` | Wikipedia + StackOverflow search, snippets, no ranking | 0.5/call |
| `ocean_tavily` | 4 sources, BM25 rerank, reads pages, passages + extractive answer | 1.0 + 0.5/page |
| `ocean_exa` | 8 sources (adds MDN, npm, arXiv, Bing), quality priors, relevance filter, source diversity, source-aware readers (SO answers API, Wikipedia extracts, GitHub READMEs), content rerank + highlights | 3.0 + 0.8/page |
| `ocean_db` | SQLite file per database per workspace; ATTACH blocked, 5 s statement limit | create 5.0, query 0.1 + 0.001/row, ... |
| `ocean_shield` | `bandit` on Python, secret and dangerous-pattern rules on all files, OSV.dev lookup for pinned requirements | 2.0 + 1.5/KLOC, deps 1.0 + 0.25/pkg |
| `ocean_sandbox` | Python in an isolated temp dir (no host files, network or other programs) with CPU, file-size and wall-clock limits | 0.25 + 0.5/s |
| `ocean_storage` | Files on disk per workspace | put 0.05 + 0.01/KB |

All the scrapers are hand-written (`pacific/services/search/web.py`). General web engines (Bing, DuckDuckGo, Startpage, Brave, Mojeek) block or poison bot traffic. Bing, for example, answers multi-word queries with unrelated pages. So the tiers lean on keyless APIs that honour the full query. Set `GITHUB_TOKEN` to raise GitHub's unauthenticated rate limit. StackExchange allows about 300 keyless requests per day per IP.

## Agents and brains

Genesis creates 5 agents with random names. Each is owned by a randomly generated person and sells 2-4 random skills (`research`, `database`, `security_audit`, `code_run`) as `<agent>.<skill>/delegate` services, with random fees and markups. Agents can list more skills at runtime with `POST /v1/services`.

A brain (`pacific/agents/brain.py`) implements `run(task, ctx) -> BrainResult`. It gets a toolbox (`pacific/agents/tools.py`) that contains:
- free internal tools: `budget_status`, `list_services`, `quote`, `notes_write`/`notes_read` (memory that persists across tasks), `extract_passages`
- paid market tools: `call_service`, `hire_agent`
- `transfer`: pay another account from the agent's wallet (outside the job budget), e.g. a tip

Every tool has a JSON schema (`toolbox.specs()`), so an LLM brain can take them as tool definitions directly. To plug one in, write a class with `run()`, register it in `BRAINS`, and set an agent's `brain` in `.pacific/secrets.json`.

The included **`ScriptedBrain`** is a deterministic stub that still makes budget decisions:
- research: quotes exa → tavily → brave and picks the best tier the budget allows
- code_run: shrinks the sandbox timeout until the worst case fits the budget
- delegation: when it lacks a skill (or `inputs.delegate` is set), it hires the cheapest agent selling that skill and passes along most of its remaining budget. It never hires an agent already in the delegation chain.
- `inputs.steps`: runs a multi-step plan and delegates per step

**`SimBrain`** (`pacific/agents/sim.py`) runs the same routines, but makes its choices the way a population of imperfect LLM agents would, using a **persona** (`pacific/agents/personas.py`) and an RNG seeded from `inputs._seed`, so any run can be replayed:

| Persona | Behaviour |
|---|---|
| `careful` | browses the catalog, quotes nearly every call, guards spend with `X-Pacific-Max-Cost` |
| `frugal` | cheapest search tier and seller, short sandbox timeouts, hands hired agents small budgets |
| `spendthrift` | premium tier, priciest seller, never quotes |
| `delegator` | hires out work it could do itself, builds chains up to 4 deep, tips sellers |
| `sloppy` | sends a malformed call first (typo'd endpoint, missing or mistyped arg), misjudges max cost, retries, sometimes quits |
| `quitter` | abandons about a third of its tasks after 0-2 paid calls |
| `adversary` | probes the exchange: overspending, hijacking a parent job, self-dealing, admin endpoints, path traversal, malformed bodies, non-finite budgets, hiring its own buyers (cycles) |

Each probe must be refused with the expected status and must not move the job's spend; one that isn't is listed under `artifacts.sim.violations` and in the task output. The task report's `artifacts.sim` also records the seed, decisions, quotes, fumbles, retries, hires and tips.

```bash
uv run pacific init --brain sim            # deal the personas out across the agents
uv run pacific init --brain sim:sloppy     # or give every agent one persona
```

A task can override the agent's persona with `inputs._persona`. `PACIFIC_THINK_SCALE` scales each persona's pause before a decision (default 1; set 0 for fast tests).

## Task format

```json
{
  "title": "Shop database",
  "description": "Create the database for a small shop...",
  "skill": "database",
  "budget": 10,
  "inputs": { "db": "shop", "schema_sql": "...", "seed": { "users": [ ... ] } },
  "checks": [ { "type": "db_min_rows", "db": "shop", "table": "users", "min": 5 } ]
}
```

See `tasks/` for research (rich and shoestring budgets), database, security, code_run, delegation and a multi-step feature build.

## HTTP API (exchange)

| | |
|---|---|
| `GET /v1/me` | identity, balance, available (after holds) |
| `GET /v1/services?kind=&skill=` | catalog, price plans, request schemas |
| `POST /v1/quote/{service}/{endpoint}` | worst-case and floor cost for a body |
| `POST /v1/call/{service}/{endpoint}` | buy; headers `X-Pacific-Job`, `X-Pacific-Max-Cost` |
| `GET /v1/jobs/{id}` | budget / spent / held / remaining |
| `POST /v1/transfer` | send WD to another account |
| `POST /v1/services` | agent lists a delegation skill |
| `POST /v1/tasks`, `POST /v1/tasks/{id}/run`, `GET /v1/tasks/{id}` | task lifecycle and report (admin, or the agent's owner) |
| `GET /v1/admin/{accounts,transactions,audit}`, `POST /v1/admin/{mint,transfer}` | admin |

Auth uses `Authorization: Bearer <key>`. Keys live in `.pacific/secrets.json`, which is gitignored.

Platform services and the agent host accept a request only if it carries a **grant** the exchange signed for that exact call (`pacific/grants.py`). The exchange signs with an Ed25519 key (`.pacific/exchange_key.pem`). Providers hold only the public key, so nothing on their side can mint grants. A grant names:
- the path it's for
- a hash of the exact body
- the workspace to act in, which providers take from the grant, not from a header
- the call it pays for
- a 30-second expiry

Each grant is accepted once. A leaked grant is worth at most the one call it was issued for, which was already reserved and paid.

## Traffic simulation

`pacific sim` drives concurrent traffic from sim-brain agents through the economy and grades how it held up:

```bash
uv run pacific sim --boot                                   # fresh throwaway economy, 50 tasks, mild faults
uv run pacific sim --boot --tasks 300 --concurrency 48 --rate 20 --faults chaos --json report.json
uv run pacific sim --boot --personas sloppy,adversary       # every task as one of these personas
uv run pacific sim --tasks 100 --no-research                # against servers you started with `pacific serve`
```

- **Workload** (`pacific/sim/workload.py`): random database, code_run, security_audit, research and multi-step tasks, each with checks whose right answer is computed up front (row sums, program output, planted vulnerabilities). Budgets are drawn tight, normal or generous relative to an estimate, and some tasks delegate.
- **Arrivals** are a Poisson process, assigned by each owner to a random agent, with at most `--concurrency` in flight. Everything is seeded.
- **Faults** (`pacific/faults.py`) are injected into the platform services and agent host from `<home>/faults.json`, re-read on change. The rules cover latency, refusals, hangs past the exchange's provider timeout, and errors after the work was done. Profiles: `none`, `mild`, `harsh`, `chaos`. Change them on live servers with `pacific faults harsh`.
- **Offline search**: `PACIFIC_SEARCH=offline` (or `pacific serve --offline-search`, the default for `--boot`) answers every upstream the search tiers scrape, in that API's own shape, from deterministic generated documents. The scrapers, ranking, readers and billing still run.
- **Invariants** (`pacific/invariants.py`, `GET /v1/admin/invariants`) are polled throughout the run on one consistent snapshot:
  - supply; no negative or overcommitted accounts
  - job budgets; job and call books match the charges
  - each delegation bill = fee + seller's spend at cost-plus
  - every workspace has an owner
  - no unhandled exchange errors
  - with nothing in flight: no holds, pending calls or running tasks
- **Report**: pass rate, latency p50/p95, spend, budget used, delegation depth, quotes, fumbles, retries, hires, quits and probes, broken down by persona, task kind and budget class. It also lists invariant violations, probe violations and crashes. Exit code 1 if any were found.

## Tests

```bash
uv run pytest                          # ledger, pricing, full economy, every sim persona, concurrent faulty traffic
PACIFIC_NET_TESTS=1 uv run pytest      # also live search tiers + OSV audits
PACIFIC_SOAK=500 uv run pytest tests/test_traffic.py -k soak -s   # long chaos run
```

## Known limits

- `ocean_sandbox` isolates the code it runs (`pacific/services/isolation.py`):
  - macOS (`sandbox-exec`): deny by default; it may read only the Python runtime and read/write only its job directory, with no network and no other programs.
  - Linux (bubblewrap): fresh namespaces with no network, and the runtime mounted read-only.

  `PACIFIC_SANDBOX` picks `auto` (default), `seatbelt`, `bwrap` or `unsafe`. With no working backend, the sandbox refuses to run code. The adversary persona's `sandbox_escape` probe pays for a run that tries to read the exchange's keys, list the host, reach the exchange and start a program; any success is a violation. The bubblewrap backend hasn't been exercised on Linux yet. For hostile multi-tenant use, a microVM (e.g. Firecracker) is the stronger boundary.
- Search quality depends on public APIs and their rate limits. A source that fails is reported in `source_errors`, and the call doesn't fail as long as any source succeeds.
- Everything runs on one machine against one SQLite ledger. The service boundaries are real HTTP, so splitting them out later is mostly deployment work.
