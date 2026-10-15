"""Per-call grants: how a provider knows a request really comes from the exchange, for this call.

Every request the exchange forwards (a platform call, a delegation, a task run, a completion
check) carries a grant it signed with its Ed25519 key. Providers hold only the public key, so
nothing a provider stores can mint grants. A grant is good for one request:

    aud   the exact path it was issued for ("/ocean_db/create", "/agents/<id>/delegate")
    body  sha256 of the exact request body, so it can't carry different arguments
    ws    the workspace the provider must act in (providers take it from here, not a header)
    call  the exchange call it pays for; sub: the account paying
    idem  (optional) the same for every retry of one idempotent call: the provider answers a
          repeat from its record instead of redoing the work (see idempotency.py)
    exp   expiry, seconds after issue
    jti   a unique id; a provider accepts each grant once

A leaked grant is therefore worth at most the one call that was already reserved and paid for,
and only until the exchange delivers it a moment later.

Header: `X-Pacific-Grant: <base64url(claims)>.<base64url(signature)>`
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import HTTPException, Request

HEADER = "X-Pacific-Grant"
TTL_S = 30
PRIVATE, PUBLIC = "exchange_key.pem", "exchange_key.pub"


class GrantError(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def ensure_keys(home: Path) -> None:
    """Create the exchange's signing key pair under `home` if it isn't there yet."""
    private, public = home / PRIVATE, home / PUBLIC
    if private.exists() and public.exists():
        return
    home.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    private.chmod(0o600)
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                     serialization.PublicFormat.SubjectPublicKeyInfo))


class Signer:
    def __init__(self, key: Ed25519PrivateKey, ttl_s: float = TTL_S):
        self.key, self.ttl_s = key, ttl_s

    @classmethod
    def load(cls, home: Path) -> "Signer":
        ensure_keys(home)
        return cls(serialization.load_pem_private_key((home / PRIVATE).read_bytes(), password=None))

    def grant(self, aud: str, body: bytes, ws: str | None = None, call: str | None = None,
              sub: str | None = None, idem: str | None = None) -> str:
        claims = {"aud": aud, "body": body_hash(body), "ws": ws, "call": call, "sub": sub,
                  "exp": time.time() + self.ttl_s, "jti": secrets.token_urlsafe(12)}
        if idem:
            claims["idem"] = idem  # retries of one idempotent call share this; see idempotency.py
        payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
        return f"{_b64(payload)}.{_b64(self.key.sign(payload))}"

    def headers(self, aud: str, body: bytes, **claims) -> dict:
        return {HEADER: self.grant(aud, body, **claims), "Content-Type": "application/json"}


class Verifier:
    def __init__(self, key: Ed25519PublicKey):
        self.key = key
        self.seen: dict[str, float] = {}  # jti -> expiry, pruned as grants expire

    @classmethod
    def load(cls, home: Path) -> "Verifier":
        ensure_keys(home)
        return cls(serialization.load_pem_public_key((home / PUBLIC).read_bytes()))

    def verify(self, token: str, path: str, body: bytes) -> dict:
        try:
            payload_b64, sig_b64 = token.split(".")
            payload = _unb64(payload_b64)
            self.key.verify(_unb64(sig_b64), payload)
            claims = json.loads(payload)
        except (ValueError, InvalidSignature) as e:
            raise GrantError("missing or invalid grant") from e
        now = time.time()
        if claims.get("exp", 0) < now:
            raise GrantError("grant expired")
        if claims.get("aud") != path:
            raise GrantError(f"grant is for {claims.get('aud')!r}, not {path!r}")
        if claims.get("body") != body_hash(body):
            raise GrantError("request body differs from the one the grant was issued for")
        self.seen = {j: exp for j, exp in self.seen.items() if exp >= now}
        if claims["jti"] in self.seen:
            raise GrantError("grant already used")
        self.seen[claims["jti"]] = claims["exp"]
        return claims


def install(app, verifier: Verifier | None, cache=None) -> None:
    """Gate a provider app: every request must carry a valid grant (else 401), and a retried
    idempotent call (grant claim `idem`) is answered from `cache` instead of being redone.
    Install it before the fault injector, so injected failures land outside the cache, like a
    reply lost on the way back."""
    app.state.grants = verifier
    app.add_middleware(GrantGate, verifier=verifier, cache=cache)


class GrantGate:
    """ASGI middleware (see `install`). Leaves the verified claims in `request.state.grant`."""

    def __init__(self, app, verifier: Verifier | None, cache=None):
        self.app, self.verifier, self.cache = app, verifier, cache

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, more = [], True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)
        token = next((v.decode() for k, v in scope["headers"] if k.decode().lower() == HEADER.lower()), "")
        try:
            if self.verifier is None:
                raise GrantError("this provider accepts no requests")
            claims = self.verifier.verify(token, scope["path"], body)
        except GrantError as e:
            return await _send_json(send, 401, {"error": "invalid_token",
                                                "detail": f"only the Pacific exchange may call this ({e})"})
        scope.setdefault("state", {})["grant"] = claims

        replayed = False

        async def replay_body():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        idem = claims.get("idem") if self.cache is not None else None
        if not idem:
            return await self.app(scope, replay_body, send)
        key = (claims.get("ws"), idem)
        cached = await self.cache.claim(key)
        if cached is not None:
            status, headers, content = cached
            await send({"type": "http.response.start", "status": status,
                        "headers": [*headers, (b"idempotent-replayed", b"true")]})
            return await send({"type": "http.response.body", "body": content})
        captured = {"status": 500, "headers": [], "body": []}

        async def capture(message):
            if message["type"] == "http.response.start":
                captured["status"], captured["headers"] = message["status"], list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                captured["body"].append(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, replay_body, capture)
        finally:
            ok = captured["status"] < 500
            self.cache.complete(key, (captured["status"], captured["headers"], b"".join(captured["body"])) if ok else None)


async def _send_json(send, status: int, payload: dict):
    content = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(content)).encode())]})
    await send({"type": "http.response.body", "body": content})


async def require_grant(request: Request) -> dict:
    """FastAPI dependency for providers: the claims of the grant `GrantGate` verified."""
    grant = getattr(request.state, "grant", None)
    if grant is None:
        raise HTTPException(401, "only the Pacific exchange may call this (no verified grant)")
    return grant
