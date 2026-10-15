"""Account keys, DPoP-bound session tokens, scopes, job-limited tokens and owner mandates."""

import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pacific.agents.client import ExchangeClient, MarketError
from pacific.auth import JWT_BEARER, Credentials, DPoPAuth, dpop_proof, sign_jwt
from support import boot, load, run_task


def creds(config, account_id) -> Credentials:
    return Credentials.load(config.home, account_id)


def token_request(config, c: Credentials, assertion: str | None = None, **extra) -> httpx.Response:
    url = f"{config.exchange_url}/v1/auth/token"
    body = {"grant_type": JWT_BEARER, "assertion": assertion or c.assertion(url), **extra}
    return httpx.post(url, json=body, headers={"DPoP": dpop_proof(c.key, "POST", url)})


def signed(config, c: Credentials, token: str, method: str, path: str, key=None, for_path=None) -> dict:
    return {"Authorization": f"DPoP {token}",
            "DPoP": dpop_proof(key or c.key, method, f"{config.exchange_url}{for_path or path}", token)}


def test_sign_in_and_every_proof_check(economy):
    config, secrets, _ = economy
    aid = next(iter(secrets["agents"]))
    c = creds(config, aid)
    got = token_request(config, c)
    assert got.status_code == 200 and got.json()["token_type"] == "DPoP"
    token = got.json()["access_token"]
    me = lambda headers: httpx.get(f"{config.exchange_url}/v1/me", headers=headers)

    headers = signed(config, c, token, "GET", "/v1/me")
    assert me(headers).json()["id"] == aid
    refusals = {
        "replayed proof": me(headers),
        "no proof": me({"Authorization": f"DPoP {token}"}),
        "proof for another URL": me(signed(config, c, token, "GET", "/v1/me", for_path="/v1/services")),
        "someone else's key": me(signed(config, c, token, "GET", "/v1/me", key=Ed25519PrivateKey.generate())),
        "made-up token": me(signed(config, c, "not-a-token", "GET", "/v1/me")),
    }
    for why, r in refusals.items():
        assert r.status_code == 401 and r.json()["error"] in ("invalid_dpop_proof", "invalid_token"), why
        assert r.headers["WWW-Authenticate"].startswith("DPoP"), why

    url = f"{config.exchange_url}/v1/auth/token"
    now = int(time.time())
    expired = sign_jwt(c.key, {"iss": aid, "sub": aid, "aud": url, "iat": now - 600, "exp": now - 300, "jti": "e1"})
    wrong_aud = sign_jwt(c.key, {"iss": aid, "sub": aid, "aud": "https://elsewhere/token", "iat": now, "exp": now + 60, "jti": "w1"})
    other = list(secrets["agents"])[1]
    once = c.assertion(url)
    assert token_request(config, c, once).status_code == 200
    for assertion, code in [(expired, "invalid_grant"), (wrong_aud, "invalid_grant"), (once, "invalid_grant"),
                            (Credentials(other, c.key).assertion(url), "invalid_client")]:
        assert token_request(config, c, assertion).json()["error"] == code


def test_scopes_and_job_limited_tokens(economy):
    config, secrets, admin = economy
    (aid, a), (other, _) = list(secrets["agents"].items())[:2]
    task = admin.post("/v1/tasks", json={**load("code_run.json"), "agent": aid, "budget": 5}).json()
    job_token = DPoPAuth(creds(config, aid), scope="pacific:read pacific:spend", resource=f"job:{task['workspace']}")
    agent = ExchangeClient(config.exchange_url, auth=job_token)
    receipt = agent.call("ocean_storage", "list", {})  # no job header: the token's job is used
    assert receipt.job["id"] == task["workspace"]
    with pytest.raises(MarketError) as e:
        agent.transfer(other, 1)
    assert e.value.code == "insufficient_scope"
    with pytest.raises(MarketError) as e:
        agent.call(f"{secrets['agents'][other]['name'].lower()}.{secrets['agents'][other]['skills'][0]}", "delegate",
                   {"description": "x", "budget": 1})
    assert e.value.code == "insufficient_scope"  # no hire scope asked for
    elsewhere = admin.post("/v1/tasks", json={**load("code_run.json"), "agent": aid}).json()["workspace"]
    r = agent.raw("POST", "/v1/call/ocean_storage/list", json={}, headers={"X-Pacific-Job": elsewhere})
    assert r.json()["error"] == "forbidden"
    assert job_token.granted["scope"] == "pacific:read pacific:spend"

    owner = ExchangeClient(config.exchange_url, auth=DPoPAuth(creds(config, a["owner"])))
    with pytest.raises(MarketError) as e:
        owner.call("ocean_storage", "list", {})
    assert e.value.code == "insufficient_scope"  # people assign work; they don't buy services
    assert token_request(config, creds(config, aid), resource=f"job:{elsewhere}").status_code == 200
    assert token_request(config, creds(config, other), resource=f"job:{elsewhere}").json()["error"] == "forbidden"


def test_revoking_a_mandate_cuts_the_agent_off_at_once(economy):
    config, secrets, admin = economy
    aid, a = list(secrets["agents"].items())[2]
    agent = ExchangeClient(config.exchange_url, auth=DPoPAuth(creds(config, aid)))
    owner = httpx.Client(base_url=config.exchange_url, auth=DPoPAuth(creds(config, a["owner"])))
    assert agent.me()["id"] == aid
    stranger = httpx.Client(base_url=config.exchange_url, auth=DPoPAuth(creds(config, list(secrets["people"])[0 if a["owner"] != list(secrets["people"])[0] else 1])))
    assert stranger.post(f"/v1/mandates/{aid}/revoke").json()["error"] == "forbidden"

    assert owner.post(f"/v1/mandates/{aid}/revoke").json()["active"] is False
    with pytest.raises(MarketError) as e:
        agent.me()  # the live session is refused, and a new one can't be had
    assert e.value.code == "invalid_grant"
    assert token_request(config, creds(config, aid)).json()["error"] == "invalid_grant"
    assert httpx.get(f"{config.exchange_url}/v1/me", headers={"Authorization": f"Bearer {a['key']}"}).status_code == 401

    regrant = owner.post("/v1/mandates", json={"agent": aid, "scopes": ["pacific:read", "pacific:spend"], "daily_cap": 0.02})
    assert regrant.status_code == 200, regrant.text
    assert agent.me()["id"] == aid  # back, with a fresh token
    agent.call("ocean_storage", "list", {})  # 0.01 WD: inside the cap
    with pytest.raises(MarketError) as e:
        agent.call("ocean_storage", "put", {"key": "a", "content": "x"})  # worst case 0.06 WD: over it
    assert e.value.code == "budget_exceeded"
    owner.post("/v1/mandates", json={"agent": aid})
    assert admin.get("/v1/admin/invariants").json()["ok"]


def test_rate_limits_follow_the_account_not_the_session(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("PACIFIC_RATE_PER_S", "5")
    mp.setenv("PACIFIC_RATE_BURST", "5")
    with boot(tmp_path_factory.mktemp("acct-limits") / "home", seed=6) as (config, secrets, _):
        aid = next(iter(secrets["agents"]))
        sessions = [httpx.Client(base_url=config.exchange_url, auth=DPoPAuth(creds(config, aid))) for _ in range(3)]
        statuses = [s.get("/v1/me").status_code for _ in range(4) for s in sessions]
        assert statuses.count(429) >= 5  # three sessions, one bucket
    mp.undo()


def test_an_economy_without_legacy_keys(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("PACIFIC_LEGACY_KEYS", "0")
    with boot(tmp_path_factory.mktemp("no-keys") / "home", seed=7) as (config, secrets, admin):
        aid, a = next(iter(secrets["agents"].items()))
        r = httpx.get(f"{config.exchange_url}/v1/me", headers={"Authorization": f"Bearer {a['key']}"})
        assert (r.status_code, r.json()["error"]) == (401, "invalid_token")
        assert ExchangeClient(config.exchange_url, auth=DPoPAuth(creds(config, aid))).me()["id"] == aid
        report = run_task(admin, {**load("database.json"), "agent": aid, "budget": 20})  # agents run on tokens
        assert report["status"] == "passed", report["output"]
        assert admin.get("/v1/admin/invariants", params={"quiescent": True}).json()["ok"]
    mp.undo()
