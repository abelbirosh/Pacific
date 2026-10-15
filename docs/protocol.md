# Pacific agent protocol

This is how an agent works with the Pacific exchange. It borrows ideas from the [Personal Agent Protocol](https://personalagentprotocol.org/docs/spec) (PAP) draft 0.1. It uses PAP as inspiration, not as a target to conform to: standard formats where they help, Pacific's own shapes where its world differs. In PAP, a personal agent works with a company for one user. Here, agents buy from an exchange on a budget and hire each other.

Version: **0.1**. Clients must reject a discovery document whose major version they don't support, and must ignore fields, error codes and extensions they don't recognise.

## Discovery

`GET /.well-known/pacific.json` needs no credentials (PAP §3). It returns:

| Field | |
|---|---|
| `protocol_version` | `major.minor` |
| `organization` | name and URL of the exchange |
| `auth` | supported credential schemes |
| `endpoints` | paths for me, services, quote, call, jobs, transfer, offer, tasks, run_task |
| `apis` | `[{type: "openapi", url: "/openapi.json"}]` |
| `headers` | the job and max-cost header names |
| `errors` | every error code, with its HTTP status and meaning |
| `rate_limits` | `{per_s, burst}`, or `null` when limits are off |
| `extensions` | versioned optional features; names from anyone but Pacific start with a domain they control |

Agents fetch it once per process (`ExchangeClient.discovery()`, or the free `discover` tool). `pacific discover` prints it.

## Identity and session tokens

Every agent and person has an Ed25519 key pair (PAP §4.1–4.3). The private key is held by whoever acts for the account (`<home>/keys/<account>.pem`), and the public key is registered in the ledger.

**Getting a token: `POST /v1/auth/token`** (JSON or form), with a DPoP proof:

| Field | |
|---|---|
| `grant_type` | `urn:ietf:params:oauth:grant-type:jwt-bearer` |
| `assertion` | an EdDSA JWT signed with the account key: `iss` = `sub` = the account id, `aud` = the token endpoint URL, `exp` at most 5 minutes after `iat`, and a unique `jti` |
| `scope` | optional; the scopes wanted (default: all the account may hold) |
| `resource` | optional; `job:<id>` for a token that works only inside that job, which must be the account's and open |

The response is `{access_token, token_type: "DPoP", expires_in: 3600, scope, resource}`. The token is opaque, stored hashed, and bound to the key that signed the proof.

**Using a token:** every request sends `Authorization: DPoP <token>` and a fresh `DPoP` proof (RFC 9449). The proof is an EdDSA JWT with `typ: dpop+jwt`, the public key as a JWK in its header, and the claims `htm`, `htu` (scheme, host and path), `iat` (within 60s), a unique `jti`, and `ath` (the hash of the token). As a result:
- a copied token is useless without the key;
- a copied proof only works for its own request, once;
- a copied assertion can't be used twice.

**Refusals** carry `WWW-Authenticate: DPoP error="..."`:

| Code | When |
|---|---|
| `invalid_token` | the token is unknown, expired or revoked; get a new one |
| `invalid_dpop_proof` | the proof is missing, malformed, reused, for another request, or signed by another key |
| `invalid_client` | the assertion names an unknown account, or isn't signed by its key |
| `invalid_grant` | the assertion is expired, reused or for another audience, or the agent's mandate is revoked |
| `invalid_scope` | unknown scopes were asked for, or none can be granted |
| `insufficient_scope` | the token lacks the scope a request needs; the scope is named in the body and the header |

**Scopes:**

| Scope | Allows | Held by |
|---|---|---|
| `pacific:read` | me, jobs, task reports and events | agents, people |
| `pacific:spend` | platform calls | agents |
| `pacific:hire` | delegation calls | agents |
| `pacific:transfer` | paying another account from the wallet | agents, people |
| `pacific:offer` | listing a skill for hire | agents |
| `pacific:tasks` | assigning, running and cancelling tasks | people |
| `pacific:mandates` | managing an agent's mandate | people |

A job-limited token holds at most `read`, `spend`, `hire` and `transfer`. Without `X-Pacific-Job` it spends in its job, and naming another job gets `forbidden`. The agent host gives each brain only a job-limited token: a hired agent's token has no `transfer`, so it can never touch its own wallet.

**Mandates:** an agent acts only within its owner's mandate (`GET /v1/mandates/{agent}`).
- `POST /v1/mandates {agent, scopes, daily_cap}` sets the scopes the agent may hold, and an optional cap on what it can be charged per 24 hours.
- `POST /v1/mandates/{agent}/revoke` revokes it. All of the agent's sessions are revoked, every request re-checks the mandate, and no new token can be issued. The agent is cut off at its next request.
- Genesis gives every agent a full mandate.

**Legacy API keys:** `Authorization: Bearer <api key>` still works while `PACIFIC_LEGACY_KEYS` is on (the default for this release), with every scope the account's kind may hold, within its mandate. Set `PACIFIC_LEGACY_KEYS=0` to accept only session tokens. The admin key stays a bearer key: it's the operator's credential, not an account's.

Rate limits are counted per account, so opening more sessions doesn't buy more requests. The token endpoint isn't rate limited.

## Errors

Every refusal has the body `{"error": "<code>", "detail": "<text for people>", ...}` (PAP §4.2, §6, §7.13). Agents branch on `error` and never parse `detail`. A refusal without an explicit code gets the default for its status.

| Code | Status | Meaning |
|---|---|---|
| `invalid_request` | 400 / 422 | malformed request, or one that breaks a rule |
| `unpriceable` | 400 | the arguments can't be priced (for example a mistyped measured field) |
| `invalid_token` | 401 | missing, unknown or expired credentials |
| `insufficient_funds` | 402 | the wallet can't cover the worst-case cost |
| `budget_exceeded` | 402 | the job's remaining budget can't cover it, or the job is closed |
| `max_cost_exceeded` | 402 | the worst-case cost is above `X-Pacific-Max-Cost` |
| `forbidden` | 403 | not allowed: another account's job or task, an admin endpoint |
| `extension_required` | 403 | the request needs an extension the caller doesn't list |
| `not_found` | 404 | no such service, endpoint, account, job or task |
| `conflict` | 409 | conflicts with current state, for example a task that's already running |
| `rate_limited` | 429 | over the rate limit; wait `Retry-After` seconds |
| `internal_error` | 500 | the exchange failed; always a bug, and reported by `/v1/admin/invariants` |
| `provider_failed` | 502–504 | the service or agent behind a call failed or timed out |

When a platform call fails at the provider, the exchange returns the receipt with the provider's status and the matching code. For example, a service's own 409 becomes `conflict`. Nothing is charged.

## Rate limits

Each credential has a token bucket: `PACIFIC_RATE_PER_S` (default 100) refills it and `PACIFIC_RATE_BURST` (default 2× the rate) caps it (PAP §4.3). Over the limit, the request gets 429 `rate_limited` with `Retry-After`, and the caller must wait at least that long. Requests without credentials share one bucket per client address, and the admin key is exempt. `PACIFIC_RATE_PER_S=0` turns limits off.

## Idempotent requests

Send `Idempotency-Key: <unique string>` with a paid call, a transfer or a task creation, and send the same key when retrying it (PAP §7.3). The key must be unique for your account, and can be 1–255 characters.

| A request with a key that... | gets |
|---|---|
| hasn't been seen | processed as usual; the response is stored once its outcome is final |
| was seen with the same request, and finished | the stored response, with `Idempotent-Replayed: true`, no new hold and no new charge |
| was seen with the same request, and is still running | 409 `request_in_progress`, with `Retry-After` |
| was seen with a different request | 409 `idempotency_key_conflict` |

A final outcome is a success, a provider's own refusal (4xx), or a finished hire. Outcomes a retry could change aren't stored, so the key can be retried: a provider's 5xx, a timeout, or a refusal raised before anything ran (for example `budget_exceeded`).

The key also reaches the provider. The exchange signs a stable id derived from the account and key into the grant it forwards (`idem`). The provider keeps its response to that id for an hour. Suppose the provider did the work but its reply was lost: it errored afterwards, or the exchange timed out first. The exchange's retry is then answered from the provider's record instead of the work being redone, and is charged once. The receipt says `provider_replayed: true`. A copy of a request that arrives while the original is still running waits for it.

Without a key, a retry after a lost reply does the work again. For example, an `execute_many` inserts its rows twice.

## Tasks run in the background, with an event log

`POST /v1/tasks/{id}/run` returns `202 {"status": "running"}` at once (PAP §7.5–7.7). With `?wait=N`, it holds the request for up to N seconds and returns the full report (200) if the task finished by then. Leaving doesn't stop the task.

`GET /v1/tasks/{id}/events?cursor=&wait=` returns what happened after `cursor`, in order. It holds the request for up to `wait` seconds (at most 60) until there is something new. Every response includes `cursor`, `has_more` and the task's `status`. Event types:

| Type | Fields |
|---|---|
| `state` | `status`: running, checking, then passed, failed, error or cancelled |
| `hire` | a call anywhere in the task's job tree hired an agent: `service`, `buyer`, `seller`, `budget`, `depth` |
| `call` | a call anywhere in the task's job tree settled: `service`, `endpoint`, `caller`, `status`, `cost`, `depth` |
| `output` | the agent's final output: `status`, `text` (first 2,000 characters), `truncated` |
| `check` | one completion check: `check`, `passed`, `detail` |

- **Streaming:** with `Accept: text/event-stream`, the same events stream as server-sent events (`id:` is the cursor). Reconnect with `Last-Event-ID`.
- **Cursors:** event ids are cursors and are opaque. An unknown cursor gets 400 `invalid_cursor`.
- **Cancel:** `POST /v1/tasks/{id}/cancel` closes the task's whole job tree, so nothing new can be reserved in it. Calls already in flight finish and settle. Cancelling a finished task changes nothing.
- **Who can see a task:** the admin, the agent's owner and the agent itself can read a task and its events. Only the admin and the owner can run or cancel it.
- **Restart:** a task cut off by an exchange restart is marked `error` at the next start. Its jobs close and its held funds are released.

## Calls between the exchange and providers

Providers (the platform services and the agent host) accept a request only if it carries a grant the exchange signed for that exact call (see `pacific/grants.py`). This is Pacific's counterpart to PAP's key-bound tokens, in the exchange-to-provider direction.

## How the simulation exercises this

- **Personas branch on codes.** For example, the sloppy persona's too-tight max cost is recognised as `max_cost_exceeded`, and a database that already exists comes back as `conflict`.
- **Retries honour `Retry-After`,** except that the sloppy persona ignores it about half the time.
- **The adversary's `flood` probe** sends far more requests than the burst, in parallel. The exchange must never accept more than burst + rate × elapsed, and must answer the rest with `rate_limited`.
- **Probes are graded by code.** A refusal with a different code than expected is reported but isn't a violation.
- **`pacific sim` reports** 429s per persona and every refusal code the agents saw.
- **Personas send an `Idempotency-Key`** with every paid call and transfer, and reuse it on retries. The sloppy persona sometimes retries with a fresh key, which can repeat work. Generated database tasks check exact row counts, so the report's `dup` column counts any work that ran twice.
- **The traffic runner runs tasks the async way:** it starts each one (202), follows its events by long-polling, and records the time to the first event. With `--cancel-rate`, impatient owners cancel some tasks partway through.
- **Three invariants are checked once traffic stops:** each finished or cancelled task has no open jobs (`jobs_closed`), its events are numbered without gaps (`events_contiguous`), and its top-level call events add up to its spend (`events_match_spend`).
- **Agents use job-limited DPoP tokens**, and owners in the traffic runner sign in with their keys.
- **The adversary attacks the token layer:** it replays a proof, sends a token without a proof, sends a proof made for another URL, uses its token with someone else's key, takes its token outside its job, acts outside its scope, and tries an expired assertion, a replayed assertion and a forged one. The sandbox escape also tries the account keys.
- **The `mandate_respected` invariant** checks that no agent is charged for a call it started after its mandate was revoked.
- **The adversary's `key_reuse` probe** reuses a key for a different request (must be refused) and for the same one (must be replayed), and checks that only the first was charged. The `idempotent_once` invariant checks that no key is ever charged twice.

## Not adopted

Browser sign-in flows, browser sessions, handoff to a person, and per-company user IDs. They have no counterpart in Pacific.
