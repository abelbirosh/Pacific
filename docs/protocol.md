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

## Calls between the exchange and providers

Providers (the platform services and the agent host) accept a request only if it carries a grant the exchange signed for that exact call (see `pacific/grants.py`). This is Pacific's counterpart to PAP's key-bound tokens, in the exchange-to-provider direction.

## How the simulation exercises this

- **Personas branch on codes.** For example, the sloppy persona's too-tight max cost is recognised as `max_cost_exceeded`, and a database that already exists comes back as `conflict`.
- **Retries honour `Retry-After`,** except that the sloppy persona ignores it about half the time.
- **The adversary's `flood` probe** sends far more requests than the burst, in parallel. The exchange must never accept more than burst + rate × elapsed, and must answer the rest with `rate_limited`.
- **Probes are graded by code.** A refusal with a different code than expected is reported but isn't a violation.
- **`pacific sim` reports** 429s per persona and every refusal code the agents saw.
- **Personas send an `Idempotency-Key`** with every paid call and transfer, and reuse it on retries. The sloppy persona sometimes retries with a fresh key, which can repeat work. Generated database tasks check exact row counts, so the report's `dup` column counts any work that ran twice.
- **The adversary's `key_reuse` probe** reuses a key for a different request (must be refused) and for the same one (must be replayed), and checks that only the first was charged. The `idempotent_once` invariant checks that no key is ever charged twice.

## Planned (see the adoption plan)

| PAP idea | Pacific phase |
|---|---|
| Async conversations: cursor, wait, status, server-sent events (§7.5–7.7) | 3: task runs return 202, with `GET /v1/tasks/{id}/events` |
| Agent identity, key-bound session tokens, scopes, revocation (§4.1–4.3, 4.8–4.9) | 4: agents sign in with their own keys, get DPoP-bound, scoped, job-limited tokens; owners can revoke |

**Not adopted:** browser sign-in flows, browser sessions, handoff to a person, and per-company user IDs. They have no counterpart in Pacific.
