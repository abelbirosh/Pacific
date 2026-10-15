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

## Calls between the exchange and providers

Providers (the platform services and the agent host) accept a request only if it carries a grant the exchange signed for that exact call (see `pacific/grants.py`). This is Pacific's counterpart to PAP's key-bound tokens, in the exchange-to-provider direction.

## How the simulation exercises this

- **Personas branch on codes.** For example, the sloppy persona's too-tight max cost is recognised as `max_cost_exceeded`, and a database that already exists comes back as `conflict`.
- **Retries honour `Retry-After`,** except that the sloppy persona ignores it about half the time.
- **The adversary's `flood` probe** sends far more requests than the burst, in parallel. The exchange must never accept more than burst + rate × elapsed, and must answer the rest with `rate_limited`.
- **Probes are graded by code.** A refusal with a different code than expected is reported but isn't a violation.
- **`pacific sim` reports** 429s per persona and every refusal code the agents saw.

## Planned (see the adoption plan)

| PAP idea | Pacific phase |
|---|---|
| Message IDs make retries safe (§7.3) | 2: `Idempotency-Key` on calls, transfers and tasks, carried through to providers |
| Async conversations: cursor, wait, status, server-sent events (§7.5–7.7) | 3: task runs return 202, with `GET /v1/tasks/{id}/events` |
| Agent identity, key-bound session tokens, scopes, revocation (§4.1–4.3, 4.8–4.9) | 4: agents sign in with their own keys, get DPoP-bound, scoped, job-limited tokens; owners can revoke |

**Not adopted:** browser sign-in flows, browser sessions, handoff to a person, and per-company user IDs. They have no counterpart in Pacific.
