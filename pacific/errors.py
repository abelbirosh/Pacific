"""Machine-readable errors. Every refusal from the exchange has the body

    {"error": "<code>", "detail": "<what happened, for people>", ...}

so an agent, LLM or scripted, branches on `error` and never has to parse `detail`. A refusal
that names no code gets the default for its HTTP status. Codes are stable; new ones may be added,
and clients treat an unknown code like the default for its status.
"""

from __future__ import annotations

from fastapi import HTTPException

CODES: dict[str, tuple[int, str]] = {
    "invalid_request": (400, "the request is malformed or breaks a rule"),
    "unpriceable": (400, "the exchange can't work out a worst-case price for these arguments"),
    "invalid_cursor": (400, "the event cursor is unknown or belongs to another task"),
    "invalid_token": (401, "missing, unknown, expired or revoked credentials"),
    "invalid_client": (401, "the assertion's account is unknown, or its signature doesn't match the account's key"),
    "invalid_dpop_proof": (401, "the DPoP proof is missing, malformed, reused, or made for another request or key"),
    "invalid_grant": (400, "the assertion is expired, reused or malformed, or the agent's mandate is revoked"),
    "invalid_scope": (400, "unknown scopes, or none of the requested scopes may be granted"),
    "insufficient_scope": (403, "the token doesn't carry the scope this needs"),
    "insufficient_funds": (402, "the wallet can't cover the worst-case cost"),
    "budget_exceeded": (402, "the job's remaining budget can't cover the worst-case cost, or the job is closed"),
    "max_cost_exceeded": (402, "the worst-case cost is above the caller's X-Pacific-Max-Cost"),
    "forbidden": (403, "authenticated, but not allowed to do this"),
    "extension_required": (403, "this needs an extension the caller doesn't support"),
    "not_found": (404, "no such service, endpoint, account, job or task"),
    "conflict": (409, "the request conflicts with current state"),
    "idempotency_key_conflict": (409, "this Idempotency-Key was already used for a different request"),
    "request_in_progress": (409, "a request with this Idempotency-Key is still running; retry shortly"),
    "rate_limited": (429, "too many requests; wait for Retry-After seconds"),
    "internal_error": (500, "the exchange failed; this is a bug"),
    "provider_failed": (502, "the service or agent behind the call failed or timed out"),
}

DEFAULT_FOR_STATUS = {400: "invalid_request", 401: "invalid_token", 402: "insufficient_funds", 403: "forbidden",
                      404: "not_found", 405: "invalid_request", 409: "conflict", 413: "invalid_request",
                      422: "invalid_request", 429: "rate_limited", 500: "internal_error"}


def code_for(status: int) -> str:
    return DEFAULT_FOR_STATUS.get(status, "provider_failed" if status >= 500 else "invalid_request")


class PacificError(HTTPException):
    """An HTTPException that carries an error code (and optionally extra body fields)."""

    def __init__(self, code: str, detail, status: int | None = None, headers: dict | None = None, **extra):
        super().__init__(status or CODES[code][0], detail, headers)
        self.code, self.extra = code, extra


def body(code: str, detail, **extra) -> dict:
    return {"error": code, "detail": detail, **extra}
