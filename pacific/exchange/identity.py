"""Authentication, scopes and mandates for the exchange (see pacific/auth.py for the protocol).

A request is made by a `Principal`: the admin (the operator's key), or an account acting through
a DPoP-bound session token, or (while PACIFIC_LEGACY_KEYS is on, the default for now) through a
long-lived API key with every scope its kind may have.

An agent acts only within its owner's mandate: the scopes it may hold and an optional daily cap
on what it can be charged. Revoking the mandate revokes the agent's sessions, and every request
re-checks it, so the agent is cut off at its next request.
"""

from __future__ import annotations

import json
import os
import secrets as pysecrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from ..auth import (AGENT_SCOPES, ALL_SCOPES, ASSERTION_MAX_S, JWT_BEARER, PERSON_SCOPES, TOKEN_PATH, AuthError,
                    Seen, htu, jwk_key, new_account_key, read_jwt, token_hash, verify_proof)
from ..db import Database
from ..errors import PacificError
from ..ledger import Ledger

SESSION_TTL_S = 3600
JOB_SCOPES = ("pacific:read", "pacific:spend", "pacific:hire", "pacific:transfer")


@dataclass(frozen=True)
class Principal:
    account: object | None  # sqlite Row, None for the admin
    scopes: frozenset = field(default_factory=frozenset)
    resource: str | None = None  # "job:<id>" when the token only works inside that job
    admin: bool = False

    @property
    def id(self) -> str:
        return "admin" if self.admin else self.account["id"]

    @property
    def job(self) -> str | None:
        return self.resource[4:] if self.resource and self.resource.startswith("job:") else None


class Revoke(BaseModel):
    token: str


class MandateIn(BaseModel):
    agent: str
    scopes: list[str] = list(AGENT_SCOPES)
    daily_cap: float | None = None  # WD per 24h the agent may be charged; None for no cap


def _refuse(code: str, detail: str, scheme: str = "DPoP", **extra) -> PacificError:
    return PacificError(code, detail, headers={"WWW-Authenticate": f'{scheme} error="{code}"'}, **extra)


class Identity:
    def __init__(self, db: Database, ledger: Ledger, home: Path, admin_key: str):
        self.db, self.ledger, self.home, self.admin_key = db, ledger, home, admin_key
        self.legacy_keys = os.environ.get("PACIFIC_LEGACY_KEYS", "1") != "0"
        self.proofs, self.assertions = Seen(), Seen()
        self._account_of_token: dict[str, str] = {}

    # ---- setup ------------------------------------------------------------------------------

    def ensure_keys_and_mandates(self):
        """Give every agent and person a key pair, and every agent a mandate, if a ledger made
        before identities existed lacks them."""
        with self.db.tx() as conn:
            for a in conn.execute("SELECT id, kind, owner_id FROM accounts WHERE kind IN ('agent', 'person') AND jwk IS NULL").fetchall():
                conn.execute("UPDATE accounts SET jwk = ? WHERE id = ?", (json.dumps(new_account_key(self.home, a["id"])), a["id"]))
            for a in conn.execute("SELECT id, owner_id FROM accounts WHERE kind = 'agent' AND id NOT IN (SELECT agent_id FROM mandates)").fetchall():
                conn.execute("INSERT INTO mandates(agent_id, owner_id, scopes) VALUES (?,?,?)",
                             (a["id"], a["owner_id"], " ".join(AGENT_SCOPES)))

    # ---- who is asking ------------------------------------------------------------------------

    def principal(self, request: Request, authorization: str = Header(default=""),
                  dpop: str | None = Header(default=None)) -> Principal:
        scheme, _, credential = authorization.partition(" ")
        credential = credential.strip()
        if scheme == "Bearer" and credential and pysecrets.compare_digest(credential, self.admin_key):
            return Principal(None, frozenset(ALL_SCOPES), admin=True)
        if scheme == "Bearer" and credential:
            if not self.legacy_keys:
                raise _refuse("invalid_token", "API keys are turned off; get a session token at " + TOKEN_PATH, "Bearer")
            row = self.ledger.account_by_key(credential)
            if row is None:
                raise _refuse("invalid_token", "unknown API key", "Bearer")
            return Principal(row, self._allowed(row))
        if scheme == "DPoP" and credential:
            session = self.db.one("SELECT * FROM sessions WHERE token_hash = ?", (token_hash(credential),))
            if session is None or session["revoked"] or session["expires_at"] < time.time():
                raise _refuse("invalid_token", "session token is unknown, expired or revoked")
            if not dpop:
                raise _refuse("invalid_dpop_proof", "a DPoP proof must accompany the session token")
            try:
                jkt = verify_proof(dpop, request.method, request.url, self.proofs, credential)
            except AuthError as e:
                raise _refuse(e.code, e.detail)
            if jkt != session["jkt"]:
                raise _refuse("invalid_dpop_proof", "the proof is signed by a different key than the token is bound to")
            row = self.ledger.account(session["account_id"])
            allowed = self._allowed(row)  # re-checked every request: a revoked mandate bites at once
            scopes = frozenset(session["scopes"].split()) & allowed
            return Principal(row, scopes, session["resource"])
        raise _refuse("invalid_token", "missing credentials: send a DPoP session token (or, for now, a Bearer API key)")

    def _allowed(self, account) -> frozenset:
        if account["kind"] == "person":
            return frozenset(PERSON_SCOPES)
        if account["kind"] != "agent":
            return frozenset()
        mandate = self.db.one("SELECT * FROM mandates WHERE agent_id = ?", (account["id"],))
        if mandate is None or mandate["revoked_at"]:
            raise _refuse("invalid_token", "this agent's mandate was revoked by its owner")
        return frozenset(mandate["scopes"].split())

    def account_for(self, authorization: str) -> str:
        """A stable rate-limit key: the account behind a credential (one bucket however many
        sessions it opens)."""
        scheme, _, credential = authorization.partition(" ")
        if scheme != "DPoP" or not credential:
            return authorization
        h = token_hash(credential.strip())
        if h not in self._account_of_token:
            row = self.db.one("SELECT account_id FROM sessions WHERE token_hash = ?", (h,))
            if len(self._account_of_token) > 50_000:
                self._account_of_token.clear()
            self._account_of_token[h] = row["account_id"] if row else h
        return self._account_of_token[h]

    @staticmethod
    def need(p: Principal, scope: str):
        if not p.admin and scope not in p.scopes:
            raise PacificError("insufficient_scope", f"this needs the {scope} scope",
                               headers={"WWW-Authenticate": f'DPoP error="insufficient_scope", scope="{scope}"'}, scope=scope)

    def check_daily_cap(self, conn, agent_id: str, amount: int):
        """Inside the hold's transaction: would this hold take the agent past its mandate's cap?"""
        cap = conn.execute("SELECT daily_cap FROM mandates WHERE agent_id = ?", (agent_id,)).fetchone()
        if not cap or cap[0] is None:
            return
        charged = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE from_id = ? AND kind = 'charge' "
                               "AND ts > strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 day')", (agent_id,)).fetchone()[0]
        held = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM holds WHERE account_id = ? AND status = 'active'",
                            (agent_id,)).fetchone()[0]
        if charged + held + amount > cap[0]:
            raise PacificError("budget_exceeded", "this would take the agent past its owner's daily cap")

    # ---- endpoints ----------------------------------------------------------------------------

    def router(self) -> APIRouter:
        r = APIRouter()
        principal = self.principal

        @r.post(TOKEN_PATH)
        async def token(request: Request, dpop: str | None = Header(default=None)):
            body = await _form_or_json(request)
            if body.get("grant_type") != JWT_BEARER:
                raise PacificError("invalid_request", f"grant_type must be {JWT_BEARER}")
            account, claims = self._check_assertion(str(body.get("assertion", "")), request)
            if not dpop:
                raise PacificError("invalid_dpop_proof", "a DPoP proof is required")
            try:
                jkt = verify_proof(dpop, "POST", request.url, self.proofs)
            except AuthError as e:
                raise PacificError(e.code, e.detail)
            try:
                allowed = self._allowed(account)
            except PacificError:
                raise PacificError("invalid_grant", "this agent's mandate was revoked by its owner")
            requested = set(str(body.get("scope") or " ".join(sorted(allowed))).split())
            if requested - set(ALL_SCOPES):
                raise PacificError("invalid_scope", f"unknown scopes: {sorted(requested - set(ALL_SCOPES))}")
            resource = body.get("resource")
            if resource:
                job = self._own_open_job(account, str(resource))
                allowed = allowed & frozenset(JOB_SCOPES)
                resource = f"job:{job}"
            granted = sorted(requested & allowed)
            if not granted:
                raise PacificError("invalid_scope", "none of the requested scopes may be granted")
            access_token = pysecrets.token_urlsafe(32)
            with self.db.tx() as conn:
                conn.execute("INSERT INTO sessions(token_hash, account_id, jkt, scopes, resource, expires_at) VALUES (?,?,?,?,?,?)",
                             (token_hash(access_token), account["id"], jkt, " ".join(granted), resource,
                              time.time() + SESSION_TTL_S))
            return {"access_token": access_token, "token_type": "DPoP", "expires_in": SESSION_TTL_S,
                    "scope": " ".join(granted), "resource": resource, "account": account["id"]}

        @r.post("/v1/auth/revoke")
        def revoke(req: Revoke, p: Principal = Depends(principal)):
            with self.db.tx() as conn:
                conn.execute("UPDATE sessions SET revoked = 1 WHERE token_hash = ? AND account_id = ?",
                             (token_hash(req.token), p.id))
            return {"revoked": True}

        def own_agent(p: Principal, agent_ref: str):
            agent = self.ledger.resolve(agent_ref)
            if agent is None or agent["kind"] != "agent":
                raise HTTPException(404, f"no agent {agent_ref!r}")
            if not p.admin:
                self.need(p, "pacific:mandates")
                if agent["owner_id"] != p.id:
                    raise HTTPException(403, "only the agent's owner manages its mandate")
            return agent

        def view(agent_id: str) -> dict:
            m = self.db.one("SELECT * FROM mandates WHERE agent_id = ?", (agent_id,))
            return {"agent": agent_id, "owner": m["owner_id"], "scopes": m["scopes"].split(),
                    "daily_cap": None if m["daily_cap"] is None else m["daily_cap"] / 1_000_000,
                    "revoked_at": m["revoked_at"], "active": m["revoked_at"] is None}

        @r.get("/v1/mandates/{agent_ref}")
        def get_mandate(agent_ref: str, p: Principal = Depends(principal)):
            return view(own_agent(p, agent_ref)["id"])

        @r.post("/v1/mandates")
        def grant(req: MandateIn, p: Principal = Depends(principal)):
            agent = own_agent(p, req.agent)
            if set(req.scopes) - set(AGENT_SCOPES):
                raise PacificError("invalid_scope", f"agents can't hold {sorted(set(req.scopes) - set(AGENT_SCOPES))}")
            cap = None if req.daily_cap is None else round(req.daily_cap * 1_000_000)
            with self.db.tx() as conn:
                conn.execute("INSERT OR REPLACE INTO mandates(agent_id, owner_id, scopes, daily_cap) VALUES (?,?,?,?)",
                             (agent["id"], agent["owner_id"], " ".join(req.scopes), cap))
            return view(agent["id"])

        @r.post("/v1/mandates/{agent_ref}/revoke")
        def revoke_mandate(agent_ref: str, p: Principal = Depends(principal)):
            agent = own_agent(p, agent_ref)
            with self.db.tx() as conn:
                conn.execute("UPDATE mandates SET revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                             "WHERE agent_id = ? AND revoked_at IS NULL", (agent["id"],))
                conn.execute("UPDATE sessions SET revoked = 1 WHERE account_id = ?", (agent["id"],))
            return view(agent["id"])

        return r

    def _check_assertion(self, assertion: str, request: Request):
        try:
            _, unverified = read_jwt(assertion, verify=False)
        except AuthError as e:
            raise PacificError("invalid_grant", e.detail)
        account = self.ledger.account(str(unverified.get("iss", "")))
        if account is None or not account["jwk"] or account["kind"] not in ("agent", "person"):
            raise PacificError("invalid_client", "unknown account in the assertion")
        try:
            _, claims = read_jwt(assertion, key=jwk_key(json.loads(account["jwk"])))
        except AuthError:
            raise PacificError("invalid_client", "the assertion isn't signed by the account's key")
        now = time.time()
        if claims.get("sub") != claims.get("iss"):
            raise PacificError("invalid_grant", "sub must equal iss")
        if claims.get("aud") != htu(request.url):
            raise PacificError("invalid_grant", "the assertion is for a different audience")
        exp, iat = claims.get("exp"), claims.get("iat")
        if not isinstance(exp, (int, float)) or not isinstance(iat, (int, float)) or exp < now or iat > now + 60 \
                or exp - iat > ASSERTION_MAX_S:
            raise PacificError("invalid_grant", "the assertion is expired, not yet valid, or lives too long")
        if not isinstance(claims.get("jti"), str) or not self.assertions.first_time(claims["jti"], exp + 60):
            raise PacificError("invalid_grant", "the assertion was already used")
        return account, claims

    def _own_open_job(self, account, resource: str) -> str:
        if not resource.startswith("job:"):
            raise PacificError("invalid_request", "resource must be job:<id>")
        job = self.db.one("SELECT * FROM jobs WHERE id = ?", (resource[4:],))
        if job is None or job["account_id"] != account["id"]:
            raise PacificError("forbidden", "that job isn't yours")
        if job["status"] != "open":
            raise PacificError("budget_exceeded", "that job is closed")
        return job["id"]


async def _form_or_json(request: Request) -> dict:
    raw = await request.body()
    if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        from urllib.parse import parse_qsl
        return dict(parse_qsl(raw.decode()))
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise PacificError("invalid_request", "body must be JSON or form-encoded")
    if not isinstance(body, dict):
        raise PacificError("invalid_request", "body must be an object")
    return body
