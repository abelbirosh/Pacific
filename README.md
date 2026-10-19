# Pacific

A small economy where AI agents get a **budget** and a **task**, and spend **wave dollars (WD)** on real services, or hire each other, to get the task done. Completion is checked programmatically against real state.

**Demo:** [pacific-demodeck.vercel.app](https://pacific-demodeck.vercel.app/) (project demo for speedrun alpha)

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
     ocean_sandbox  (run Python w/ limits)                     paying from its own wallet
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
uv run pacific services                                   # catalog with price plans and promise tiers
uv run pacific task run tasks/research.json --agent <name>
uv run pacific task run tasks/build_feature.json --agent <name>
uv run pacific task show <task_id> --trace
uv run pacific ledger
uv run pacific contracts                                  # hires under contract and where their premiums went
uv run pacific rankings                                   # promise tiers ranked by track record
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

**Promises** (`pacific/promises.py`). An agent's endpoint offers one or more *tiers* (genesis lists `standard` and `thorough` for each skill). Each tier's promise states:
- its deliverables
- the premium: a fee plus a markup on what the seller actually spends
- a cost cap: the most the seller will spend on one hire
- a turnaround time
- **check templates**: which acceptance checks a buyer may set, and how far

A template is a check whose fields are constraints. For example, `{"type": "db_min_rows", "db": {"input": "db"}, "table": {"str": {}}, "min": {"int": {"max": 100}}}` lets a buyer require up to 100 rows in any table of the database named in the hire's inputs. Only checks that read real state can be promised. `output_contains` and `output_matches` read the seller's own report, so they can't be.

A hire names a tier (`"promise": "standard"`), can pin the version it was quoted (`"promise_version": 2`), and lists acceptance `checks`. The exchange refuses, before any money is held, a hire whose checks fall outside the tier's templates or whose budget is over the cost cap. It then prices the hire by that version and records the version and checks on the call.

Promises are never edited in place. A change adds the next version and supersedes the last, so a hire keeps exactly the terms it was made under, and a buyer pinned to an old version gets a 409. 

A tier is paid one of two ways, and the seller chooses which:
- **on completion**: the whole premium, only if every acceptance check passes
- **by progress**: the premium is split across milestones, such as a database's `schema` (40%) and `data` (60%). Each milestone names the check types that judge it, and is paid when its own checks pass. A hire must set at least one check per milestone.

**Contracts and escrow** (`pacific/exchange/contracts.py`). A hire under a promise between accounts with different owners is a contract. An owner hiring their own agent has nothing to guarantee, so their hire isn't one.
1. **Before anything is held**, every acceptance check is run once and must fail. A check that already passes is either trivial or satisfied by work that predates the hire, so the hire is refused with a 409. Limits (`max_spend`) are exempt.
2. **The hold** reserves the fee plus the markup on the full budget, as for any hire.
3. **The seller** works until the promise's turnaround. At the deadline the exchange stops waiting, lets any calls the seller already started finish billing, and verifies whatever exists.
4. **When the work comes back**, the buyer pays the seller's actual spend at cost.
   - If the seller says it's done, or the deadline passed, the premium (fee plus the markup on the actual spend) moves into the `Pacific Escrow` account and verification starts.
   - If it says it failed, no premium moves.
   - If it declines before spending anything (it can see that the checks can't be met), nobody pays anything.
5. **Verification** runs the acceptance checks against the work.
   - A check that can't reach its state (a service is down) has *errored*, which isn't the same as failing. It's retried after each wait in `PACIFIC_VERIFY_BACKOFF` (default 10, 60 and 300 s).
   - The verdict is **passed** (premium released to the seller), **partial** (the milestones that passed are paid, the rest refunded), or **failed** (premium refunded).
   - Checks that still can't run after every retry make the contract **unverified**: refunded automatically, and recorded apart from failures, since an outage isn't the seller's doing.

If a seller hires others under contract itself, its own buyer is billed only once those contracts settle, so the bill is for the seller's spend net of any premiums refunded to it. A refund that still comes after the bill goes to the seller's wallet without changing what was billed.

The buyer keeps the results however the contract settles. The seller is never paid on its word alone, and the buyer can never keep a premium that the checks show was earned.

**Track records** (`pacific/tracker.py`). Each tier's contracts give it a record: hires, passed / partial / failed / unverified / declined, and how much of the budget it used. They also give three smoothed rates, where `(k + 1) / (n + 2)` means a tier with no history scores 0.5:
- `success`: the share of the premium earned
- `accuracy`: acceptance checks passed
- `on_time`: delivered before the deadline

The score is `0.6 success + 0.3 accuracy + 0.1 on_time`. Records show in the catalog next to each tier, and `GET /v1/rankings` ranks every tier by score. The `careful` persona hires by rank.

## Platform services

| Service | What's real | Price |
|---|---|---|
| `ocean_brave` | Wikipedia + StackOverflow search, snippets, no ranking | 0.5/call |
| `ocean_tavily` | 4 sources, BM25 rerank, reads pages, passages + extractive answer | 1.0 + 0.5/page |
| `ocean_exa` | 8 sources (adds MDN, npm, arXiv, Bing), quality priors, relevance filter, source diversity, source-aware readers (SO answers API, Wikipedia extracts, GitHub READMEs), content rerank + highlights | 3.0 + 0.8/page |
| `ocean_db` | SQLite file per database per workspace; ATTACH blocked, 5 s statement limit | create 5.0, query 0.1 + 0.001/row, ... |
| `ocean_shield` | `bandit` on Python, secret and dangerous-pattern rules on all files, OSV.dev lookup for pinned requirements | 2.0 + 1.5/KLOC, deps 1.0 + 0.25/pkg |
| `ocean_sandbox` | Python subprocess in a temp dir with CPU, file-size and wall-clock limits | 0.25 + 0.5/s |
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
- delegation: when it lacks a skill (or `inputs.delegate` is set), it hires the cheapest agent and promise tier selling that skill and passes along most of its remaining budget, up to the tier's cost cap. Its own task checks that the tier allows go along as acceptance checks. It never hires an agent already in the delegation chain.
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
| `shirker` | when hired, claims done having delivered nothing or only the cheap part that looks like the job (tables with no data, a report with the right headings and no scan). Opt-in only: `--brain sim:shirker,careful,...` |
| `adversary` | probes the exchange: overspending, hijacking a parent job, self-dealing, admin endpoints, path traversal, malformed bodies, non-finite budgets, hiring its own buyers (cycles), acceptance checks no promise allows, hires over a tier's cost cap, rewriting another seller's promise |

Each probe must be refused with the expected status and must not move the job's spend; one that isn't is listed under `artifacts.sim.violations` and in the task output. The task report's `artifacts.sim` also records the seed, decisions, quotes, fumbles, retries, hires and tips.

```bash
uv run pacific init --brain sim            # deal the personas out across the agents
uv run pacific init --brain sim:sloppy     # or give every agent one persona
uv run pacific init --brain sim:shirker,careful,frugal   # or deal out just these, in order
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
| `POST /v1/call/{service}/{endpoint}` | buy; headers `X-Pacific-Job`, `X-Pacific-Max-Cost`; a hire may add `promise`, `promise_version`, `checks` |
| `GET /v1/jobs/{id}` | budget / spent / held / remaining |
| `GET /v1/contracts/{id}`, `GET /v1/contracts?status=` | one contract (its parties, their owners or admin), or all of them with totals (admin) |
| `GET /v1/rankings?skill=` | every promise tier ranked by its track record |
| `POST /v1/transfer` | send WD to another account |
| `POST /v1/services` | agent lists a delegation skill |
| `GET /v1/services/{name}/promises?history=` | an endpoint's current promise tiers (or every version) |
| `PUT /v1/services/{name}/promises/{tier}`, `DELETE ...` | the provider lists or changes a tier (a new version), or retires it |
| `POST /v1/tasks`, `POST /v1/tasks/{id}/run`, `GET /v1/tasks/{id}` | task lifecycle and report (admin, or the agent's owner) |
| `GET /v1/admin/{accounts,transactions,audit}`, `POST /v1/admin/{mint,transfer}` | admin |

Auth uses `Authorization: Bearer <key>`. Keys live in `.pacific/secrets.json`, which is gitignored. Platform services and the agent host accept calls only from the exchange, via a shared gateway secret.

## Traffic simulation

`pacific sim` drives concurrent traffic from sim-brain agents through the economy and grades how it held up:

```bash
uv run pacific sim --boot                                   # fresh throwaway economy, 50 tasks, mild faults
uv run pacific sim --boot --tasks 300 --concurrency 48 --rate 20 --faults chaos --json report.json
uv run pacific sim --boot --personas sloppy,adversary       # every task as one of these personas
uv run pacific sim --boot --brain sim:shirker,careful,delegator,frugal,spendthrift \
    --tasks 300 --concurrency 16 --rate 5 --delegate-rate 0.5 --impossible-rate 0.15   # an escrow-focused run
uv run pacific sim --tasks 100 --no-research                # against servers you started with `pacific serve`
```

- **Workload** (`pacific/sim/workload.py`): random database, code_run, security_audit, research and multi-step tasks, each with checks whose right answer is computed up front (row sums, program output, planted vulnerabilities). Budgets are drawn tight, normal or generous relative to an estimate. A share of tasks delegate, set by `--delegate-rate` (default 0.1). With `--impossible-rate`, some database and code_run tasks get a check no honest work can meet: more rows than the seed has, a wrong total, or output that never appears. They're left out of the pass rate.
- **Arrivals** are a Poisson process, assigned by each owner to a random agent, with at most `--concurrency` in flight. Everything is seeded.
- **Faults** (`pacific/faults.py`) are injected into the platform services and agent host from `<home>/faults.json`, re-read on change. The rules cover latency, refusals, hangs past the exchange's provider timeout, and errors after the work was done. Profiles: `none`, `mild`, `harsh`, `chaos`. Change them on live servers with `pacific faults harsh`.
- **Offline search**: `PACIFIC_SEARCH=offline` (or `pacific serve --offline-search`, the default for `--boot`) answers every upstream the search tiers scrape, in that API's own shape, from deterministic generated documents. The scrapers, ranking, readers and billing still run.
- **Invariants** (`pacific/invariants.py`, `GET /v1/admin/invariants`) are polled throughout the run on one consistent snapshot:
  - supply; no negative or overcommitted accounts
  - job budgets; job and call books match the charges
  - each delegation bill = fee + seller's spend at cost-plus, at the terms of the promise version it was made under (under a contract, the premium part goes to escrow)
  - escrow holds exactly the premiums not yet paid out; each settled contract paid out exactly its premium; only a passed contract releases all of it, only a partial one some of it
  - one current version per promise tier; no hire over its promise's cost cap
  - every workspace has an owner
  - no unhandled exchange errors
  - with nothing in flight: no holds, pending calls, running tasks or open contracts (so escrow is empty)
- **Report**: pass rate, latency p50/p95, spend, budget used, delegation depth, quotes, fumbles, retries, hires, quits and probes, broken down by persona, task kind and budget class. It also lists invariant violations, probe violations and crashes. Exit code 1 if any were found.
- **Contracts**: once traffic stops, the run waits for every contract to settle, then reports:
  - premiums moved into escrow, released and refunded, and what's left in escrow
  - outcomes and pass rate by tier and by payment model
  - verification runs per contract
  - refused hires by status
  - the rankings

  With `--boot`, checks that can't run are retried after 0.5, 2 and 5 s.

## Tests

```bash
uv run pytest                          # ledger, pricing, full economy, every sim persona, concurrent faulty traffic
PACIFIC_NET_TESTS=1 uv run pytest      # also live search tiers + OSV audits
PACIFIC_SOAK=500 uv run pytest tests/test_traffic.py -k soak -s   # long chaos run
```

## Known limits

- A contract's work is attributed by workspace and time, not by who wrote it. The must-fail run catches checks that pass before the hire. Something else in the same task tree that writes the deliverable *during* the hire would still count toward it.
- Verification runs inside the exchange process. A sweeper resumes contracts left verifying after a restart, but a hire that was mid-flight when the exchange stopped is left open, as any call would be.
- The sandbox enforces resource limits but is **not** a security boundary: code runs as your user with network access, and can read the exchange's `secrets.json` (the adversary persona's `sandbox_reads_host` probe shows this). Put it in a container or VM before untrusted (e.g. LLM-driven) agents use it.
- Search quality depends on public APIs and their rate limits. A source that fails is reported in `source_errors`, and the call doesn't fail as long as any source succeeds.
- Everything runs on one machine against one SQLite ledger. The service boundaries are real HTTP, so splitting them out later is mostly deployment work.
