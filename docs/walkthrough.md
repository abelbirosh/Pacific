# End-to-end walkthrough: shipping a signup feature

This walks through one task from genesis to audit. An agent gets 60 WD and a three-step job. It does two steps itself and hires another agent for the third. The exchange then checks the real databases and files to decide whether the task passed.

All output below is copied from a real run (`--seed 7`, on 2026-10-14). The names, skills, prices and costs are the same on every seed-7 run. IDs are random, and the research text depends on what the live search sources return at the time.

## 1. Genesis

```bash
uv run pacific init --seed 7
```

```
Supply minted: 1,000,000.0000 WD   audit ok: True

agent    owner         skills for hire
-------  ------------  --------------------------------------------
Fetan    Uma Haddad    database, research
Tredo    Hiro Mbeki    code_run, database, research, security_audit
Trashol  Greta Silva   code_run, database, research, security_audit
Gela     Cyrus Sato    code_run, research, security_audit
Dutror   Farid Petrov  code_run, database, research, security_audit
```

The treasury mints 1,000,000 WD. Each person receives a genesis grant and funds their agent from it. For example, Uma Haddad receives 3,200 WD and gives 1,050 WD to Fetan. **Fetan can't do `security_audit`**, which is why it's the agent in this example.

Each skill an agent has is also listed for sale. For `security_audit`, four agents compete:

```
dutror.security_audit    0.9500 WD fee + subtask spend at cost +10%
trashol.security_audit   2.3400 WD fee + subtask spend at cost +15%
tredo.security_audit     2.7400 WD fee + subtask spend at cost +15%
gela.security_audit      3.2900 WD fee + subtask spend at cost +5%
```

## 2. The task

[`tasks/build_feature.json`](../tasks/build_feature.json) has three steps, a budget of 60 WD, and five checks:

| Step | Skill | Step budget | Fetan has the skill? |
|---|---|---|---|
| Research password hashing → `signup/research.md` | research | 12 | yes |
| Create and seed the `accounts` database | database | 12 | yes |
| Audit `signup.py` (uses MD5 and SQL string concatenation) → `signup/audit.md` | security_audit | 15 | **no, has to hire** |

The checks:

```json
{"type": "storage_contains", "key": "signup/research.md", "text": "## Sources"},
{"type": "db_columns",       "db": "accounts", "table": "accounts", "columns": ["email", "password_hash"]},
{"type": "db_min_rows",      "db": "accounts", "table": "accounts", "min": 1},
{"type": "storage_contains", "key": "signup/audit.md", "text": ["Verdict", "B324"]},
{"type": "max_spend",        "amount": 60}
```

The last storage check requires `B324`, the bandit rule for weak hashes. A real scan of this code finds it. A generic report the agent writes itself won't contain it.

## 3. Run it

```bash
uv run pacific serve                                            # terminal 1
uv run pacific task run tasks/build_feature.json --agent Fetan   # terminal 2
```

```
Task task_0f6746e15995  [PASSED]  Ship a signup feature
Agent: Fetan   budget 60.0000 WD   spent 10.9015 WD   workspace job_8fb930fd18da

Calls:
by        service                         status  units  cost    ms
--------  ------------------------------  ------  -----  ------  ---
Fetan     ocean_tavily/search             ok      2.0    2.0000  800
Fetan     ocean_storage/put               ok      2.0    0.0700  6
Fetan     ocean_db/create                 ok      0.0    5.0000  1
Fetan     ocean_db/execute                ok      0.0    0.2000  2
Fetan     ocean_db/execute_many           ok      1.0    0.2005  2
Fetan     ocean_db/schema                 ok      0.0    0.0500  1
Fetan     dutror.security_audit/delegate  ok      2.21   3.3810  545
  Dutror  ocean_shield/scan_code          ok      0.1    2.1500  519
  Dutror  ocean_storage/put               ok      1.0    0.0600  1

Checks:
  [PASS] storage_contains: 'signup/research.md' contains ['## Sources']
  [PASS] db_columns: has columns ['email', 'password_hash']
  [PASS] db_min_rows: accounts.accounts has 1 rows (need >= 1)
  [PASS] storage_contains: 'signup/audit.md' contains ['Verdict', 'B324']
  [PASS] max_spend: spent 10.9015 WD (limit 60.0000 WD)
```

### Step 1: Research (Fetan, 2.07 WD)

The step sets `depth: "standard"`, so Fetan doesn't consider Exa. It quotes Tavily for 4 pages: `1.0 + 0.5 × 4 = 3.0 WD`. That's within the 12 WD step budget, so it buys. The exchange **holds 3.0 WD** before forwarding the call. Tavily returns 2 usable pages, so the exchange **charges 2.0 WD and releases the other 1.0**. Fetan then writes the brief to storage for 0.07 WD.

### Step 2: Database (Fetan, 5.45 WD)

`create` costs 5.0. `execute` runs the schema for 0.2. `execute_many` inserts the seed row for 0.2 + 0.0005 per row. `schema` reads the result back for 0.05. All of it is created in the root job's workspace `job_8fb930fd18da`.

### Step 3: Security audit (hired from Dutror, 3.381 WD)

Fetan lists the `security_audit` offers, leaves out any agent already in the delegation chain, and picks the lowest fee, which is Dutror at 0.95 + 10%. It sets aside about 12.13 WD for Dutror. That's its remaining 15 WD step budget, minus the fee, divided by the markup, with 5% kept as slack. The exchange holds the worst case for Fetan:

```
fee + budget × (1 + markup) = 0.95 + 12.1341 × 1.10 ≈ 14.30 WD
```

The exchange opens a **child job** for Dutror with that 12.13 WD budget. Dutror pays for its own platform calls out of its own wallet:

```
ocean_shield/scan_code   2.15  (2.0 + 1.5/KLOC × 0.1 KLOC)
ocean_storage/put        0.06
                         ----
seller spend             2.21
```

Fetan is then charged the fee plus Dutror's spend with the markup added, and the rest of the hold is released:

```
0.95 + 2.21 × 1.10 = 3.381 WD
```

Dutror's report goes to `signup/audit.md` in the **same root workspace**, because child jobs share the root job's workspace. That's how the `B324` check finds a file that Fetan never wrote:

```
**Verdict: FAIL: high-severity issues found**
- **HIGH**   signup.py:5 B324:hashlib: Use of weak MD5 hash for security.
- **MEDIUM** signup.py:6 sql-string-concat: sql string concat
- **MEDIUM** signup.py:6 B608:hardcoded_sql_expressions: Possible SQL injection vector ...
```

The task **passed**, even though the audit's verdict is FAIL. The task asked for an audit, and the checks confirm a real audit was done. Whether the code is safe is a separate question.

## 4. Follow the money

```bash
uv run pacific ledger
```

```
12  charge  Fetan   ocean_tavily   2.0000  ocean_tavily/search
13  charge  Fetan   ocean_storage  0.0700  ocean_storage/put
14  charge  Fetan   ocean_db       5.0000  ocean_db/create
15  charge  Fetan   ocean_db       0.2000  ocean_db/execute
16  charge  Fetan   ocean_db       0.2005  ocean_db/execute_many
17  charge  Fetan   ocean_db       0.0500  ocean_db/schema
18  charge  Dutror  ocean_shield   2.1500  ocean_shield/scan_code
19  charge  Dutror  ocean_storage  0.0600  ocean_storage/put
20  charge  Fetan   Dutror         3.3810  dutror.security_audit/delegate
```

Holds don't show up here. The ledger only records money that actually moved.

| Account | Before | After | Δ |
|---|---:|---:|---:|
| Fetan | 1,050.0000 | 1,039.0985 | −10.9015 |
| Dutror | 1,100.0000 | 1,101.1710 | **+1.1710** (3.381 earned − 2.21 spent) |
| ocean_db | 0 | 5.4505 | +5.4505 |
| ocean_shield | 0 | 2.1500 | +2.1500 |
| ocean_tavily | 0 | 2.0000 | +2.0000 |
| ocean_storage | 0 | 0.1300 | +0.1300 |
| **Net** | | | **0** |

```bash
uv run pacific audit
```

```json
{ "total_supply": 1000000.0, "sum_of_balances": 1000000.0, "sum_of_mints": 1000000.0, "active_holds": 0.0, "ok": true }
```

No wave dollars were created or destroyed. They only moved between accounts.

## 5. The same task with 8 WD

```bash
uv run pacific task run tasks/build_feature.json --agent Fetan --budget 8
```

```
Task task_7c2f264903d8  [FAILED]  Ship a signup feature
Agent: Fetan   budget 8.0000 WD   spent 7.0700 WD

Fetan  ocean_tavily/search  ok  2.0  2.0000
Fetan  ocean_storage/put    ok  2.0  0.0700
Fetan  ocean_db/create      ok  0.0  5.0000

  [PASS] storage_contains: 'signup/research.md' contains ['## Sources']
  [FAIL] db_columns: table accounts.accounts is missing
  [FAIL] db_min_rows: table accounts.accounts is missing
  [FAIL] storage_contains: object 'signup/audit.md' is missing
  [PASS] max_spend: spent 7.0700 WD (limit 60.0000 WD)

Step 2: Stopped: ran out of budget: needs up to 1.2000 WD but job job_c1bd2684b997 has 0.9300 WD of budget left
```

The schema `execute` call has a worst case of 1.2 WD (0.2 + 0.0005 × 2,000 rows). Only 0.93 WD is left in the job, so the **hold is refused before any work happens**. Fetan stops, the checks report exactly which parts are missing, and the audit still passes. Spending ended at 7.07 WD, under the 8 WD budget.

The run also shows a weakness in the scripted brain. It paid 5 WD for `ocean_db/create` without first checking that it could afford to finish the step. A better brain would quote the whole step before starting. That's a good first test for an LLM brain.

## What this example covers

| Mechanism | Shown by |
|---|---|
| Choosing a search tier by quote | Step 1 picks Tavily, which fits the budget |
| Hold, then charge the actual cost | Tavily held 3.0, charged 2.0 |
| Picking the cheapest seller | Dutror's 0.95 fee is the lowest of four offers |
| Child jobs and a shared workspace | Dutror's `audit.md` is visible to the root task's checks |
| Fee plus cost with markup | 0.95 + 2.21 × 1.10 = 3.381 |
| Checks against real state | `B324` only appears if a real scan ran |
| Budget enforcement | The 8 WD run is stopped by a refused hold, not by overspending |
| Conservation of supply | `audit` passes after both runs |

Note that `max_spend` is a fixed amount in the task file. It still says 60 when you override the budget with `--budget 8`.
