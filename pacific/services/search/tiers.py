"""Three search products built from the same hand-written scrapers, at three levels of quality.

General web engines (Bing, DuckDuckGo, Startpage...) block or poison bot traffic, so the tiers
lean on keyless public APIs that honour the full query, and differ in breadth and depth:

ocean_brave   cheap and fast: Wikipedia + StackOverflow, titles and snippets in source order.
              No reranking and no page reading.
ocean_tavily  mid-tier: four sources (adds GitHub, Hacker News), BM25 rerank on snippets, then
              reads each result and returns its most relevant passages plus an extractive answer.
ocean_exa     premium: eight sources (adds MDN, npm, arXiv, Bing), larger candidate pools,
              popularity/answer-quality priors, a relevance filter, then reads the winners with
              source-aware readers and reranks again on what the pages actually say.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..common import ok
from . import web


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _interleave(results: list[dict]) -> list[dict]:
    """Round-robin across sources so one source can't crowd out the others."""
    by_source: dict[str, list[dict]] = {}
    for r in results:
        by_source.setdefault(r["source"], []).append(r)
    out, queues = [], list(by_source.values())
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


def _diverse(ranked: list[dict], n: int, per_source: int) -> list[dict]:
    """Best-first selection, but no source may take more than `per_source` slots."""
    picked, counts = [], {}
    for r in ranked:
        if counts.get(r["source"], 0) < per_source:
            picked.append(r)
            counts[r["source"]] = counts.get(r["source"], 0) + 1
        if len(picked) == n:
            break
    return picked


# ---- ocean_brave ------------------------------------------------------------------------------

brave = APIRouter(prefix="/ocean_brave")


class BraveSearch(BaseModel):
    query: str = Field(min_length=1, max_length=400)
    num_results: int = 10


@brave.post("/search")
async def brave_search(req: BraveSearch):
    t0 = time.monotonic()
    async with web.client(timeout=8) as http:
        merged, errors = await web.gather_sources({
            "wikipedia": web.wikipedia(http, req.query, 5),
            "stackoverflow": web.stackoverflow(http, req.query, 5),
        })
    if not merged:
        raise HTTPException(502, f"all sources failed: {errors}")
    results = _interleave(web.dedupe(merged))[: _clamp(req.num_results, 1, 10)]
    return ok({"query": req.query, "results": results, "source_errors": errors, "took_ms": _ms(t0)}, units=0)


# ---- ocean_tavily -----------------------------------------------------------------------------

tavily = APIRouter(prefix="/ocean_tavily")


class TavilySearch(BaseModel):
    query: str = Field(min_length=1, max_length=400)
    max_results: int = 5
    include_answer: bool = True


@tavily.post("/search")
async def tavily_search(req: TavilySearch):
    t0 = time.monotonic()
    n = _clamp(req.max_results, 1, 5)
    async with web.client(timeout=10) as http:
        merged, errors = await web.gather_sources({
            "wikipedia": web.wikipedia(http, req.query, 5),
            "stackoverflow": web.stackoverflow(http, req.query, 5),
            "github": web.github(http, req.query, 5),
            "hackernews": web.hackernews(http, req.query, 5),
        })
        if not merged:
            raise HTTPException(502, f"all sources failed: {errors}")
        candidates = web.dedupe(merged)
        scores = web.bm25_rank(req.query, [c["title"] + " " + c["snippet"] for c in candidates])
        ranked = [c for _, c in sorted(zip(scores, candidates), key=lambda x: -x[0])][:n]
        pages = await asyncio.gather(*(web.read(http, r) for r in ranked))
    results, all_passages, fetched = [], [], 0
    for hit, page in zip(ranked, pages):
        passages = web.best_passages(page["text"], req.query, k=3) if "text" in page else []
        fetched += "text" in page
        all_passages.extend(passages)
        results.append({**hit, "content": "\n".join(passages) or hit["snippet"], "fetch_error": page.get("error")})
    answer = None
    if req.include_answer and all_passages:
        answer = " ".join(web.best_passages("\n".join(all_passages), req.query, k=3))
    return ok({"query": req.query, "answer": answer, "results": results, "source_errors": errors,
               "took_ms": _ms(t0)}, units=fetched)


class Extract(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=5)


@tavily.post("/extract")
async def tavily_extract(req: Extract):
    async with web.client(timeout=10) as http:
        pages = await web.fetch_pages(http, req.urls[:5])
    for p in pages:
        if "text" in p:
            p["text"] = p["text"][:8000]
    return ok({"results": pages}, units=sum("text" in p for p in pages))


# ---- ocean_exa --------------------------------------------------------------------------------

exa = APIRouter(prefix="/ocean_exa")

# Mild priors so authoritative sources win ties; relevance still dominates.
_SOURCE_PRIOR = {"wikipedia": 0.5, "mdn": 0.6, "stackoverflow": 0.5, "github": 0.4, "npm": 0.2,
                 "arxiv": 0.3, "hackernews": 0.2, "bing": 0.0}


class ExaSearch(BaseModel):
    query: str = Field(min_length=1, max_length=400)
    num_results: int = 8
    contents: bool = True


@exa.post("/search")
async def exa_search(req: ExaSearch):
    t0 = time.monotonic()
    n = _clamp(req.num_results, 1, 10)
    async with web.client(timeout=12) as http:
        merged, errors = await web.gather_sources({
            "wikipedia": web.wikipedia(http, req.query, 8),
            "stackoverflow": web.stackoverflow(http, req.query, 10),
            "github": web.github(http, req.query, 8),
            "hackernews": web.hackernews(http, req.query, 8),
            "mdn": web.mdn(http, req.query, 5),
            "npm": web.npm(http, req.query, 5),
            "arxiv": web.arxiv(http, req.query, 5),
            "bing": web.bing(http, req.query, 1),
        })
        if not merged:
            raise HTTPException(502, f"all sources failed: {errors}")
        candidates = web.dedupe(merged)
        scores = web.bm25_rank(req.query, [c["title"] + " " + c["snippet"] for c in candidates])
        relevant = []
        for c, s in zip(candidates, scores):
            if s <= 0:  # shares no query terms: drop it (this is what filters poisoned results)
                continue
            c["score"] = round(s + _SOURCE_PRIOR.get(c["source"], 0) + web.quality(c), 3)
            relevant.append(c)
        relevant.sort(key=lambda c: -c["score"])
        top = _diverse(relevant, n, per_source=max(2, (n + 1) // 2))
        fetched = 0
        if req.contents and top:
            pages = await asyncio.gather(*(web.read(http, c) for c in top))
            for c, page in zip(top, pages):
                if "text" in page:
                    fetched += 1
                    c["highlights"] = web.best_passages(page["text"], req.query, k=3)
                    c["text"] = page["text"][:4000]
                else:
                    c["highlights"], c["fetch_error"] = [], page["error"]
            # Rerank on what the pages actually say, not just their snippets.
            content_scores = web.bm25_rank(req.query, [c["title"] + " " + " ".join(c["highlights"]) for c in top])
            for c, s in zip(top, content_scores):
                c["score"] = round(0.5 * c["score"] + s, 3)
            top.sort(key=lambda c: -c["score"])
    return ok({
        "query": req.query, "results": top, "sources_searched": 8 - len(errors), "source_errors": errors,
        "candidates_considered": len(candidates), "candidates_relevant": len(relevant), "took_ms": _ms(t0),
    }, units=fetched)


class ExaContents(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10)
    query: str | None = None


@exa.post("/contents")
async def exa_contents(req: ExaContents):
    async with web.client(timeout=12) as http:
        pages = await web.fetch_pages(http, req.urls[:10])
    for p in pages:
        if "text" in p and req.query:
            p["highlights"] = web.best_passages(p["text"], req.query, k=5)
    return ok({"results": pages}, units=sum("text" in p for p in pages))
