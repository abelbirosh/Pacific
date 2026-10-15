"""How an agent talks to the exchange: discover services, get quotes, buy, check its budget."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..errors import code_for

PROTOCOL_MAJOR = "0"
_discovered: dict[str, dict] = {}  # base URL -> discovery document, fetched once per process


class MarketError(Exception):
    """A refusal or failure from the exchange. Branch on `code` (see pacific/errors.py)."""

    def __init__(self, status: int, detail, receipt: dict | None = None, code: str | None = None,
                 retry_after: float | None = None):
        self.code = code or code_for(status)
        super().__init__(f"[{status} {self.code}] {detail}")
        self.status, self.detail, self.receipt, self.retry_after = status, detail, receipt or {}, retry_after

    @property
    def cannot_afford(self) -> bool:
        return self.code in ("insufficient_funds", "budget_exceeded", "max_cost_exceeded")


@dataclass
class Receipt:
    call_id: str
    service: str
    endpoint: str
    cost: float
    result: dict | None
    error: str | None
    job: dict | None
    raw: dict
    replayed: bool = False  # the exchange answered a retry of an idempotent call from its record


class ExchangeClient:
    """Pass `auth` (a pacific.auth.DPoPAuth: key-bound session tokens) or, while the exchange
    still accepts them, a legacy `api_key`."""

    def __init__(self, base_url: str, api_key: str | None = None, job_id: str | None = None, timeout: float = 900,
                 auth: httpx.Auth | None = None):
        self.job_id = job_id
        headers = {"Authorization": f"Bearer {api_key}"} if api_key and auth is None else {}
        self._http = httpx.Client(base_url=base_url, timeout=timeout, headers=headers, auth=auth)

    @property
    def auth(self):
        """The DPoPAuth in use, if any."""
        return self._http.auth

    @property
    def base_url(self) -> httpx.URL:
        return self._http.base_url

    def _check(self, resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError:
            data = {"detail": resp.text}
        if not resp.is_success:
            retry_after = resp.headers.get("Retry-After")
            raise MarketError(resp.status_code, data.get("detail", data), data, code=data.get("error"),
                              retry_after=float(retry_after) if retry_after else None)
        return data

    def discovery(self) -> dict:
        """The exchange's /.well-known/pacific.json, fetched once per process."""
        key = str(self.base_url)
        if key not in _discovered:
            doc = self._check(self._http.get("/.well-known/pacific.json"))
            if str(doc.get("protocol_version", "")).split(".")[0] != PROTOCOL_MAJOR:
                raise MarketError(400, f"unsupported protocol version {doc.get('protocol_version')!r}")
            _discovered[key] = doc
        return _discovered[key]

    def me(self) -> dict:
        return self._check(self._http.get("/v1/me"))

    def services(self, kind: str | None = None, skill: str | None = None) -> list[dict]:
        params = {k: v for k, v in {"kind": kind, "skill": skill}.items() if v}
        return self._check(self._http.get("/v1/services", params=params))["services"]

    def quote(self, service: str, endpoint: str, body: dict) -> float:
        return self._check(self._http.post(f"/v1/quote/{service}/{endpoint}", json=body))["max_cost"]["wd"]

    def job(self) -> dict | None:
        if not self.job_id:
            return None
        return self._check(self._http.get(f"/v1/jobs/{self.job_id}"))

    def remaining(self) -> float:
        job = self.job()
        return job["remaining"]["wd"] if job else self.me()["available"]["wd"]

    def call(self, service: str, endpoint: str, body: dict, max_cost: float | None = None,
             idempotency_key: str | None = None) -> Receipt:
        """Buy one call. Pass the same `idempotency_key` when retrying it, so it runs and is
        charged once however many times it's sent."""
        headers = {}
        if self.job_id:
            headers["X-Pacific-Job"] = self.job_id
        if max_cost is not None:
            headers["X-Pacific-Max-Cost"] = str(max_cost)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        resp = self._http.post(f"/v1/call/{service}/{endpoint}", json=body, headers=headers)
        data = self._check(resp)
        return Receipt(data["call_id"], service, endpoint, data["cost"]["wd"], data.get("result"),
                       data.get("error"), data.get("job"), data, resp.headers.get("Idempotent-Replayed") == "true")

    def transfer(self, to: str, amount: float, memo: str = "", idempotency_key: str | None = None) -> dict:
        """Pay another account from this wallet (not from the job budget)."""
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._check(self._http.post("/v1/transfer", json={"to": to, "amount": amount, "memo": memo},
                                           headers=headers))

    def raw(self, method: str, path: str, **kw) -> httpx.Response:
        """Unchecked request, for probing how the exchange handles bad input."""
        return self._http.request(method, path, **kw)

    def close(self):
        self._http.close()
