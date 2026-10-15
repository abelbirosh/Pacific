"""Per-call grants: how a provider knows a request really comes from the exchange, for this call.

Every request the exchange forwards (a platform call, a delegation, a task run, a completion
check) carries a grant it signed with its Ed25519 key. Providers hold only the public key, so
nothing a provider stores can mint grants. A grant is good for one request:

    aud   the exact path it was issued for ("/ocean_db/create", "/agents/<id>/delegate")
    body  sha256 of the exact request body, so it can't carry different arguments
    ws    the workspace the provider must act in (providers take it from here, not a header)
    call  the exchange call it pays for; sub: the account paying
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
              sub: str | None = None) -> str:
        claims = {"aud": aud, "body": body_hash(body), "ws": ws, "call": call, "sub": sub,
                  "exp": time.time() + self.ttl_s, "jti": secrets.token_urlsafe(12)}
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


async def require_grant(request: Request) -> dict:
    """FastAPI dependency for providers: the request must carry a valid, unused grant for this
    path and body, checked against `app.state.grants` (a Verifier). Returns its claims."""
    verifier: Verifier | None = getattr(request.app.state, "grants", None)
    if verifier is None:
        raise HTTPException(401, "this provider accepts no requests")
    try:
        return verifier.verify(request.headers.get(HEADER, ""), request.url.path, await request.body())
    except GrantError as e:
        raise HTTPException(401, f"only the Pacific exchange may call this ({e})")
