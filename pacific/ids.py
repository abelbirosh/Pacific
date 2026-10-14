import hashlib
import secrets


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def new_api_key() -> str:
    return f"pk_{secrets.token_urlsafe(24)}"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()
