"""An offline stand-in for the public web the search tiers scrape.

With PACIFIC_SEARCH=offline, `web.client()` gets this transport instead of the network. It
answers each upstream API (Wikipedia, StackExchange, GitHub, Hacker News, MDN, npm, arXiv, Bing)
and plain page fetches in that API's own response shape, so the scrapers, ranking, page readers
and billing all run for real. Documents are generated deterministically from the query, so the
same query always returns the same results. Upstreams are a little slow and occasionally fail,
like the real ones, which exercises the tiers' partial-failure paths.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
from html import escape
from urllib.parse import urlparse

import httpx

FAIL_RATE = 0.04
LATENCY_S = (0.005, 0.06)

_FILLER = [
    "The usual approach is to measure first and change one thing at a time.",
    "Most production setups pair this with monitoring and a rollback plan.",
    "Benchmarks vary a lot with workload, so test against realistic data.",
    "Older guides recommend a different default that is no longer considered safe.",
    "The official documentation covers the edge cases in more detail.",
    "Several libraries implement this, and they differ mainly in configuration.",
    "A common mistake is to apply it globally instead of per endpoint.",
    "Teams report that the simplest option is usually good enough at first.",
]
_ANGLES = ["Guide", "Overview", "Best practices", "Tutorial", "FAQ", "Comparison", "Pitfalls", "Reference"]
_WORD = re.compile(r"[a-z0-9]+")


def _rng(*parts) -> random.Random:
    return random.Random(int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big"))


def _terms(query: str) -> list[str]:
    return _WORD.findall(query.lower()) or ["topic"]


def _docs(source: str, query: str, n: int) -> list[dict]:
    terms, out = _terms(query), []
    for i in range(max(0, min(int(n), 10))):
        rng = _rng(source, query, i)
        picked = rng.sample(terms, k=max(1, min(len(terms), rng.randint(1, 3))))
        title = f"{' '.join(t.capitalize() for t in picked)}: {rng.choice(_ANGLES)}"
        slug = "-".join(picked) + f"-{i}"
        out.append({"title": title, "slug": slug, "snippet": f"{' '.join(picked)} {rng.choice(_FILLER)}",
                    "score": rng.randint(0, 900), "id": rng.randint(10_000, 9_999_999)})
    return out


def page_text(url: str) -> tuple[str, str]:
    """A readable article for any URL, about the terms in its path."""
    rng = _rng("page", url)
    terms = _terms(urlparse(url).path.replace("-", " ").replace("_", " ")) or ["topic"]
    title = " ".join(t.capitalize() for t in terms[:4])
    paras = []
    for _ in range(rng.randint(4, 8)):
        t = rng.sample(terms, k=min(len(terms), 2))
        paras.append(f"When working with {' and '.join(t)}, {rng.choice(_FILLER).lower()} "
                     f"{rng.choice(_FILLER)} This matters most for {rng.choice(terms)} at scale.")
    return title, "\n".join(paras)


def _json(data) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(data).encode(), headers={"content-type": "application/json"})


def _html(title: str, body: str) -> httpx.Response:
    paras = "".join(f"<p>{escape(p)}</p>" for p in body.split("\n"))
    html = f"<html><head><title>{escape(title)}</title></head><body><article><h1>{escape(title)}</h1>{paras}</article></body></html>"
    return httpx.Response(200, content=html.encode(), headers={"content-type": "text/html; charset=utf-8"})


class OfflineWeb(httpx.AsyncBaseTransport):
    def __init__(self, fail_rate: float = FAIL_RATE, latency_s: tuple[float, float] = LATENCY_S):
        self.fail_rate, self.latency_s = fail_rate, latency_s
        self.rng = random.Random()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self.rng.uniform(*self.latency_s))
        if self.rng.random() < self.fail_rate:
            return httpx.Response(503, content=b"upstream unavailable (simulated)", request=request)
        resp = self._route(request)
        resp.request = request
        return resp

    def _route(self, req: httpx.Request) -> httpx.Response:
        host, path, q = req.url.host, req.url.path, dict(req.url.params)
        if host == "en.wikipedia.org":
            if q.get("list") == "search":
                docs = _docs("wikipedia", q.get("srsearch", ""), int(q.get("srlimit", 5)))
                return _json({"query": {"search": [{"title": d["title"], "snippet": escape(d["snippet"])} for d in docs]}})
            title = q.get("titles", "Topic")
            return _json({"query": {"pages": {"1": {"title": title, "extract": page_text("/" + title)[1]}}}})
        if host == "api.stackexchange.com":
            m = re.match(r"/2\.3/questions/(\d+)(/answers)?", path)
            if m and m.group(2):
                rng = _rng("so-answers", m.group(1))
                return _json({"items": [{"body": f"<p>{rng.choice(_FILLER)} {rng.choice(_FILLER)}</p>",
                                         "score": rng.randint(0, 300), "is_accepted": i == 0} for i in range(3)]})
            if m:
                return _json({"items": [{"title": f"Question {m.group(1)}", "body": f"<p>{page_text(path)[1]}</p>"}]})
            docs = _docs("stackoverflow", q.get("q", ""), int(q.get("pagesize", 5)))
            return _json({"items": [{"title": d["title"], "link": f"https://stackoverflow.com/questions/{d['id']}/{d['slug']}",
                                     "score": d["score"] // 10, "answer_count": d["score"] % 7,
                                     "accepted_answer_id": d["id"] if d["score"] % 2 else None,
                                     "tags": _terms(q.get("q", ""))[:3]} for d in docs]})
        if host == "api.github.com":
            if path.startswith("/repos/") and path.endswith("/readme"):
                return httpx.Response(200, content=page_text(path)[1].encode(), headers={"content-type": "text/plain"})
            docs = _docs("github", q.get("q", ""), int(q.get("per_page", 5)))
            return _json({"items": [{"full_name": f"oss/{d['slug']}", "html_url": f"https://github.com/oss/{d['slug']}",
                                     "description": d["snippet"], "stargazers_count": d["score"] * 7} for d in docs]})
        if host == "hn.algolia.com":
            docs = _docs("hackernews", q.get("query", ""), int(q.get("hitsPerPage", 5)))
            return _json({"hits": [{"title": d["title"], "url": f"https://blog.example.com/{d['slug']}",
                                    "points": d["score"], "num_comments": d["score"] // 4, "objectID": str(d["id"])}
                                   for d in docs]})
        if host == "developer.mozilla.org" and path.startswith("/api/"):
            docs = _docs("mdn", q.get("q", ""), int(q.get("size", 5)))
            return _json({"documents": [{"title": d["title"], "mdn_url": f"/en-US/docs/Web/{d['slug']}",
                                         "summary": d["snippet"]} for d in docs]})
        if host == "registry.npmjs.org":
            docs = _docs("npm", q.get("text", ""), int(q.get("size", 5)))
            return _json({"objects": [{"package": {"name": d["slug"], "description": d["snippet"],
                                                   "links": {"npm": f"https://www.npmjs.com/package/{d['slug']}"}},
                                       "downloads": {"weekly": d["score"] * 100}} for d in docs]})
        if host == "export.arxiv.org":
            query = q.get("search_query", "").replace("all:", "").replace(" AND ", " ")
            docs = _docs("arxiv", query, int(q.get("max_results", 5)))
            entries = "".join(f"<entry><title>{escape(d['title'])}</title><id>http://arxiv.org/abs/{d['id']}</id>"
                              f"<summary>{escape(d['snippet'])}</summary></entry>" for d in docs)
            atom = f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'
            return httpx.Response(200, content=atom.encode(), headers={"content-type": "application/atom+xml"})
        if host == "www.bing.com":
            docs = _docs("bing", q.get("q", ""), 8)
            items = "".join(f'<li class="b_algo"><h2><a href="https://docs.example.org/{d["slug"]}">{escape(d["title"])}</a></h2>'
                            f'<div class="b_caption"><p>{escape(d["snippet"])}</p></div></li>' for d in docs)
            return httpx.Response(200, content=f"<html><body><ol>{items}</ol></body></html>".encode(),
                                  headers={"content-type": "text/html"})
        return _html(*page_text(str(req.url)))

