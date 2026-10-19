"""How an agent talks to the exchange: discover services, get quotes, buy, check its budget."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class MarketError(Exception):
    def __init__(self, status: int, detail, receipt: dict | None = None):
        super().__init__(f"[{status}] {detail}")
        self.status, self.detail, self.receipt = status, detail, receipt or {}

    @property
    def cannot_afford(self) -> bool:
        return self.status == 402


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


class ExchangeClient:
    def __init__(self, base_url: str, api_key: str, job_id: str | None = None, timeout: float = 900):
        self.job_id = job_id
        self._http = httpx.Client(base_url=base_url, timeout=timeout,
                                  headers={"Authorization": f"Bearer {api_key}"})

    def _check(self, resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError:
            data = {"detail": resp.text}
        if not resp.is_success:
            raise MarketError(resp.status_code, data.get("detail", data), data)
        return data

    def me(self) -> dict:
        return self._check(self._http.get("/v1/me"))

    def services(self, kind: str | None = None, skill: str | None = None) -> list[dict]:
        params = {k: v for k, v in {"kind": kind, "skill": skill}.items() if v}
        return self._check(self._http.get("/v1/services", params=params))["services"]

    def quote(self, service: str, endpoint: str, body: dict) -> float:
        return self._check(self._http.post(f"/v1/quote/{service}/{endpoint}", json=body))["max_cost"]["wd"]

    def rankings(self, skill: str | None = None) -> list[dict]:
        return self._check(self._http.get("/v1/rankings", params={"skill": skill} if skill else None))["rankings"]

    def job(self) -> dict | None:
        if not self.job_id:
            return None
        return self._check(self._http.get(f"/v1/jobs/{self.job_id}"))

    def remaining(self) -> float:
        job = self.job()
        return job["remaining"]["wd"] if job else self.me()["available"]["wd"]

    def call(self, service: str, endpoint: str, body: dict, max_cost: float | None = None) -> Receipt:
        headers = {}
        if self.job_id:
            headers["X-Pacific-Job"] = self.job_id
        if max_cost is not None:
            headers["X-Pacific-Max-Cost"] = str(max_cost)
        data = self._check(self._http.post(f"/v1/call/{service}/{endpoint}", json=body, headers=headers))
        return Receipt(data["call_id"], service, endpoint, data["cost"]["wd"], data.get("result"),
                       data.get("error"), data.get("job"), data)

    def transfer(self, to: str, amount: float, memo: str = "") -> dict:
        """Pay another account from this wallet (not from the job budget)."""
        return self._check(self._http.post("/v1/transfer", json={"to": to, "amount": amount, "memo": memo}))

    def raw(self, method: str, path: str, **kw) -> httpx.Response:
        """Unchecked request, for probing how the exchange handles bad input."""
        return self._http.request(method, path, **kw)

    def close(self):
        self._http.close()
