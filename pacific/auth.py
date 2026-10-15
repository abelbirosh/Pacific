"""Who is calling: account keys, session tokens bound to them, and scopes (inspired by PAP §4).

Every agent and person has an Ed25519 key pair. The private key stays with whoever acts for the
account (`<home>/keys/<account>.pem`); the public key is registered in the ledger as a JWK.

Getting a session token (`POST /v1/auth/token`):
    grant_type  urn:ietf:params:oauth:grant-type:jwt-bearer
    assertion   a JWT signed with the account key: iss = sub = the account id, aud = the token
                endpoint, exp at most 5 minutes out, jti unique (a reused one is refused)
    scope       optional: the scopes wanted (default: everything the account may have)
    resource    optional: "job:<id>" for a token that only works inside that job
plus a DPoP proof. The token that comes back is opaque, short-lived and bound to the proof's key.

Using it: every request sends `Authorization: DPoP <token>` and a fresh `DPoP` proof (RFC 9449):
a JWT signed with the bound key over the method, the URL, the time, a unique jti and the token's
hash. A copied token is useless without the key; a copied proof is useless for any other request
and can't be replayed.

Scopes: pacific:read, pacific:spend (platform calls), pacific:hire (delegation), pacific:transfer,
pacific:offer (list a skill), pacific:tasks (assign, run, cancel tasks), pacific:mandates.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
TOKEN_PATH = "/v1/auth/token"
PROOF_WINDOW_S = 60
ASSERTION_MAX_S = 300

AGENT_SCOPES = ("pacific:read", "pacific:spend", "pacific:hire", "pacific:transfer", "pacific:offer")
PERSON_SCOPES = ("pacific:read", "pacific:transfer", "pacific:tasks", "pacific:mandates")
ALL_SCOPES = tuple(dict.fromkeys(AGENT_SCOPES + PERSON_SCOPES))


class AuthError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---- keys -----------------------------------------------------------------------------------------


def public_jwk(key: Ed25519PublicKey) -> dict:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"kty": "OKP", "crv": "Ed25519", "x": b64(raw)}


def jwk_key(jwk: dict) -> Ed25519PublicKey:
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or not isinstance(jwk.get("x"), str):
        raise AuthError("invalid_dpop_proof", "keys must be Ed25519 JWKs")
    return Ed25519PublicKey.from_public_bytes(unb64(jwk["x"]))


def thumbprint(jwk: dict) -> str:
    """RFC 7638: the key's identity, used to bind a token to it."""
    canonical = json.dumps({"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]}, separators=(",", ":"), sort_keys=True)
    return b64(hashlib.sha256(canonical.encode()).digest())


def key_path(home: Path, account_id: str) -> Path:
    return home / "keys" / f"{account_id}.pem"


def new_account_key(home: Path, account_id: str) -> dict:
    """Create and store an account's key pair; returns the public JWK for the ledger."""
    key = Ed25519PrivateKey.generate()
    path = key_path(home, account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    path.chmod(0o600)
    return public_jwk(key.public_key())


def load_account_key(home: Path, account_id: str) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key(key_path(home, account_id).read_bytes(), password=None)


# ---- JWTs (EdDSA, compact) --------------------------------------------------------------------------


def sign_jwt(key: Ed25519PrivateKey, payload: dict, header: dict | None = None) -> str:
    head = {"alg": "EdDSA", "typ": "JWT", **(header or {})}
    signing_input = f"{b64(json.dumps(head, separators=(',', ':')).encode())}.{b64(json.dumps(payload, separators=(',', ':')).encode())}"
    return f"{signing_input}.{b64(key.sign(signing_input.encode()))}"


def read_jwt(token: str, key: Ed25519PublicKey | None = None, verify: bool = True) -> tuple[dict, dict]:
    """(header, payload). Verifies the signature with `key`, or with the JWK in the header;
    `verify=False` only reads it (to find out whose key to check it with)."""
    try:
        h, p, s = token.split(".")
        header, payload = json.loads(unb64(h)), json.loads(unb64(p))
        if header.get("alg") != "EdDSA":
            raise AuthError("invalid_request", "only EdDSA (Ed25519) JWTs are accepted")
        if verify:
            (key or jwk_key(header.get("jwk") or {})).verify(unb64(s), f"{h}.{p}".encode())
    except AuthError:
        raise
    except (ValueError, TypeError, InvalidSignature) as e:
        raise AuthError("invalid_request", f"malformed or badly signed JWT ({type(e).__name__})") from e
    return header, payload


def token_hash(token: str) -> str:
    return b64(hashlib.sha256(token.encode()).digest())


def htu(url: httpx.URL | str) -> str:
    """The URL a DPoP proof covers: scheme, host and path; no query or fragment."""
    url = httpx.URL(str(url))
    return str(url.copy_with(query=None, fragment=None)).rstrip("?")


def dpop_proof(key: Ed25519PrivateKey, method: str, url, access_token: str | None = None) -> str:
    payload = {"jti": secrets.token_urlsafe(16), "htm": method.upper(), "htu": htu(url), "iat": int(time.time())}
    if access_token:
        payload["ath"] = token_hash(access_token)
    return sign_jwt(key, payload, {"typ": "dpop+jwt", "jwk": public_jwk(key.public_key())})


class Seen:
    """jti values seen within a time window, so nothing signed can be used twice."""

    def __init__(self):
        self.until: dict[str, float] = {}
        self.lock = threading.Lock()

    def first_time(self, jti: str, expires: float) -> bool:
        now = time.time()
        with self.lock:
            if len(self.until) > 10_000:
                self.until = {j: t for j, t in self.until.items() if t > now}
            if self.until.get(jti, 0) > now:
                return False
            self.until[jti] = expires
            return True


def verify_proof(proof: str, method: str, url, seen: Seen, access_token: str | None = None) -> str:
    """Check a DPoP proof for this request; returns the thumbprint of the key that signed it."""
    try:
        header, payload = read_jwt(proof)
    except AuthError as e:
        raise AuthError("invalid_dpop_proof", e.detail) from e
    if header.get("typ") != "dpop+jwt":
        raise AuthError("invalid_dpop_proof", "proof typ must be dpop+jwt")
    if payload.get("htm") != method.upper() or payload.get("htu") != htu(url):
        raise AuthError("invalid_dpop_proof", "proof was made for a different request")
    iat = payload.get("iat")
    if not isinstance(iat, (int, float)) or abs(time.time() - iat) > PROOF_WINDOW_S:
        raise AuthError("invalid_dpop_proof", "proof is too old or from the future")
    if access_token is not None and payload.get("ath") != token_hash(access_token):
        raise AuthError("invalid_dpop_proof", "proof isn't for this token")
    if not isinstance(payload.get("jti"), str) or not seen.first_time(payload["jti"], iat + 2 * PROOF_WINDOW_S):
        raise AuthError("invalid_dpop_proof", "proof was already used")
    return thumbprint(header["jwk"])


# ---- the client side ------------------------------------------------------------------------------


class Credentials:
    """What an agent (or person) holds: its account id and private key."""

    def __init__(self, account_id: str, key: Ed25519PrivateKey):
        self.account_id, self.key = account_id, key

    @classmethod
    def load(cls, home: Path, account_id: str) -> "Credentials":
        return cls(account_id, load_account_key(home, account_id))

    def assertion(self, audience: str, ttl_s: int = 60) -> str:
        now = int(time.time())
        return sign_jwt(self.key, {"iss": self.account_id, "sub": self.account_id, "aud": audience,
                                   "iat": now, "exp": now + ttl_s, "jti": secrets.token_urlsafe(16)})


class DPoPAuth(httpx.Auth):
    """httpx auth for an account: gets a session token when it needs one (scoped, optionally
    limited to a job), signs a DPoP proof for every request, and gets a new token when the
    exchange says the old one is no longer good. Works with sync and async clients."""

    requires_response_body = True

    def __init__(self, credentials: Credentials, scope: str | None = None, resource: str | None = None):
        self.credentials, self.scope, self.resource = credentials, scope, resource
        self.token: str | None = None
        self.expires = 0.0
        self.granted: dict = {}
        self.renewals = 0
        self.lock = threading.Lock()

    def _token_request(self, base: httpx.URL) -> httpx.Request:
        url = base.copy_with(path=TOKEN_PATH, query=None)
        body = {"grant_type": JWT_BEARER, "assertion": self.credentials.assertion(htu(url))}
        if self.scope:
            body["scope"] = self.scope
        if self.resource:
            body["resource"] = self.resource
        return httpx.Request("POST", url, json=body, headers={"DPoP": dpop_proof(self.credentials.key, "POST", url)})

    def _take(self, response: httpx.Response) -> bool:
        if response.status_code != 200:
            return False
        data = response.json()
        with self.lock:
            self.token, self.granted = data["access_token"], data
            self.expires = time.time() + data["expires_in"] - 30
        return True

    def auth_flow(self, request: httpx.Request):
        # If the token endpoint refuses (say the owner revoked the mandate), the flow ends there
        # and the caller gets that refusal as the response to its request.
        if self.token is None or time.time() > self.expires:
            if not self._take((yield self._token_request(request.url))):
                return
        response = yield self._sign(request)
        if response.status_code == 401 and "invalid_token" in response.headers.get("www-authenticate", ""):
            self.renewals += 1  # expired or revoked: one fresh token, one retry
            if self._take((yield self._token_request(request.url))):
                yield self._sign(request)

    def _sign(self, request: httpx.Request) -> httpx.Request:
        request.headers["Authorization"] = f"DPoP {self.token}"
        request.headers["DPoP"] = dpop_proof(self.credentials.key, request.method, request.url, self.token)
        return request
