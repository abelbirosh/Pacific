"""Hand-written scrapers and the text tools the three search tiers are built from."""

from __future__ import annotations

import asyncio
import base64
import math
import os
import re
from collections import Counter
from xml.etree import ElementTree
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
# Wikipedia's API policy requires a descriptive UA with a contact URL.
API_HEADERS = {"User-Agent": "PacificSearch/0.1 (https://github.com/abelbirosh/Pacific)"}


def client(timeout: float = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=timeout, follow_redirects=True)


def _result(title: str, url: str, snippet: str, source: str, **extra) -> dict:
    return {"title": title.strip(), "url": url, "snippet": snippet.strip(), "source": source, **extra}


# ---- sources --------------------------------------------------------------------------------


def _decode_bing_url(href: str) -> str:
    """Bing wraps result links in /ck/a?...&u=a1<base64url>; unwrap to the real URL."""
    if "bing.com/ck/" not in href:
        return href
    u = parse_qs(urlparse(href).query).get("u", [""])[0]
    if u.startswith("a1"):
        raw = u[2:]
        try:
            return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
        except Exception:
            return href
    return href


async def bing(http: httpx.AsyncClient, query: str, page: int = 1) -> list[dict]:
    first = (page - 1) * 10 + 1
    url = f"https://www.bing.com/search?q={quote_plus(query)}&first={first}&setlang=en-US&cc=US"
    resp = await http.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a or not a.get("href"):
            continue
        snippet_el = li.select_one(".b_caption p") or li.select_one(".b_lineclamp2, .b_lineclamp3, .b_algoSlug")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        out.append(_result(a.get_text(" ", strip=True), _decode_bing_url(a["href"]), snippet, "bing", rank=len(out) + 1))
    return out


async def wikipedia(http: httpx.AsyncClient, query: str, n: int = 5) -> list[dict]:
    resp = await http.get(
        "https://en.wikipedia.org/w/api.php",
        params={"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": n},
        headers=API_HEADERS,
    )
    resp.raise_for_status()
    out = []
    for hit in resp.json().get("query", {}).get("search", []):
        title = hit["title"]
        snippet = BeautifulSoup(hit.get("snippet", ""), "html.parser").get_text()
        out.append(_result(title, f"https://en.wikipedia.org/wiki/{quote_plus(title.replace(' ', '_'))}", snippet, "wikipedia"))
    return out


async def hackernews(http: httpx.AsyncClient, query: str, n: int = 5) -> list[dict]:
    resp = await http.get(
        "https://hn.algolia.com/api/v1/search",
        params={"query": query, "hitsPerPage": n, "tags": "story"},
        headers=API_HEADERS,
    )
    resp.raise_for_status()
    out = []
    for hit in resp.json().get("hits", []):
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"
        snippet = f"{hit.get('points', 0)} points, {hit.get('num_comments', 0)} comments on Hacker News"
        out.append(_result(hit.get("title") or "", url, snippet, "hackernews", points=hit.get("points", 0)))
    return out


def _github_headers(accept: str = "application/vnd.github+json") -> dict:
    h = {**API_HEADERS, "Accept": accept}
    if os.environ.get("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return h


async def github(http: httpx.AsyncClient, query: str, n: int = 5) -> list[dict]:
    resp = await http.get(
        "https://api.github.com/search/repositories",
        params={"q": query, "per_page": n},
        headers=_github_headers(),
    )
    resp.raise_for_status()
    return [
        _result(r["full_name"], r["html_url"], r.get("description") or "", "github", stars=r.get("stargazers_count", 0))
        for r in resp.json().get("items", [])
    ]


async def stackoverflow(http: httpx.AsyncClient, query: str, n: int = 5) -> list[dict]:
    resp = await http.get(
        "https://api.stackexchange.com/2.3/search/advanced",
        params={"order": "desc", "sort": "relevance", "q": query, "site": "stackoverflow", "pagesize": n},
        headers=API_HEADERS,
    )
    resp.raise_for_status()
    out = []
    for q in resp.json().get("items", []):
        title = BeautifulSoup(q.get("title", ""), "html.parser").get_text()
        snippet = f"score {q.get('score', 0)}, {q.get('answer_count', 0)} answers, tags: {', '.join(q.get('tags', []))}"
        out.append(_result(title, q["link"], snippet, "stackoverflow", score=q.get("score", 0),
                           answers=q.get("answer_count", 0), accepted=bool(q.get("accepted_answer_id"))))
    return out


async def mdn(http: httpx.AsyncClient, query: str, n: int = 5) -> list[dict]:
    resp = await http.get("https://developer.mozilla.org/api/v1/search", params={"q": query, "size": n, "locale": "en-US"})
    resp.raise_for_status()
    return [
        _result(d["title"], "https://developer.mozilla.org" + d["mdn_url"], d.get("summary", ""), "mdn")
        for d in resp.json().get("documents", [])
    ]


async def npm(http: httpx.AsyncClient, query: str, n: int = 5) -> list[dict]:
    resp = await http.get("https://registry.npmjs.org/-/v1/search", params={"text": query, "size": n}, headers=API_HEADERS)
    resp.raise_for_status()
    out = []
    for o in resp.json().get("objects", []):
        pkg = o["package"]
        url = (pkg.get("links") or {}).get("npm") or f"https://www.npmjs.com/package/{pkg['name']}"
        out.append(_result(pkg["name"], url, pkg.get("description") or "", "npm",
                           weekly_downloads=(o.get("downloads") or {}).get("weekly", 0)))
    return out


async def arxiv(http: httpx.AsyncClient, query: str, n: int = 5) -> list[dict]:
    terms = " AND ".join(f"all:{w}" for w in tokens(query)[:6])
    resp = await http.get("http://export.arxiv.org/api/query", params={"search_query": terms, "max_results": n},
                          headers=API_HEADERS)
    resp.raise_for_status()
    atom = "{http://www.w3.org/2005/Atom}"
    clean = lambda el: re.sub(r"\s+", " ", el.text or "").strip() if el is not None else ""
    return [
        _result(clean(e.find(atom + "title")), clean(e.find(atom + "id")), clean(e.find(atom + "summary"))[:500], "arxiv")
        for e in ElementTree.fromstring(resp.content).iter(atom + "entry")
    ]


# ---- source-aware readers: use each site's API where scraping is blocked or noisy ----------


async def wikipedia_content(http: httpx.AsyncClient, url: str) -> dict:
    title = url.rsplit("/wiki/", 1)[-1]
    resp = await http.get("https://en.wikipedia.org/w/api.php", headers=API_HEADERS, params={
        "action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1, "format": "json",
        "titles": unquote_plus(title).replace("_", " "),
    })
    resp.raise_for_status()
    page = next(iter(resp.json()["query"]["pages"].values()))
    if "extract" not in page:
        return {"url": url, "error": "page not found"}
    return {"url": url, "title": page["title"], "text": page["extract"][:20_000]}


async def github_readme(http: httpx.AsyncClient, url: str) -> dict:
    repo = "/".join(urlparse(url).path.strip("/").split("/")[:2])
    resp = await http.get(f"https://api.github.com/repos/{repo}/readme", headers=_github_headers("application/vnd.github.raw+json"))
    if resp.status_code != 200:
        return await fetch_page(http, url)
    return {"url": url, "title": repo, "text": resp.text[:20_000]}


async def read(http: httpx.AsyncClient, result: dict) -> dict:
    url = result["url"]
    try:
        if result["source"] == "stackoverflow" and _SO_QUESTION.search(url):
            return await stackoverflow_content(http, url)
        if result["source"] == "wikipedia":
            return await wikipedia_content(http, url)
        if result["source"] == "github":
            return await github_readme(http, url)
    except Exception as e:
        return {"url": url, "error": f"{type(e).__name__}: {e}"[:200]}
    return await fetch_page(http, url)


_SO_QUESTION = re.compile(r"stackoverflow\.com/questions/(\d+)")


async def stackoverflow_content(http: httpx.AsyncClient, url: str) -> dict:
    """StackOverflow blocks scrapers, so read the question and its best answers via the API."""
    qid = _SO_QUESTION.search(url).group(1)
    common = {"site": "stackoverflow", "filter": "withbody"}
    try:
        q, a = await asyncio.gather(
            http.get(f"https://api.stackexchange.com/2.3/questions/{qid}", params=common, headers=API_HEADERS),
            http.get(f"https://api.stackexchange.com/2.3/questions/{qid}/answers",
                     params={**common, "sort": "votes", "pagesize": 3}, headers=API_HEADERS),
        )
        q.raise_for_status()
        a.raise_for_status()
    except Exception as e:
        return {"url": url, "error": f"{type(e).__name__}: {e}"[:200]}
    items = q.json().get("items", [])
    if not items:
        return {"url": url, "error": "question not found"}
    text = lambda html: BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    parts = [f"Question: {text(items[0]['title'])}", text(items[0].get("body", ""))]
    for ans in a.json().get("items", []):
        tag = "Accepted answer" if ans.get("is_accepted") else "Answer"
        parts += [f"{tag} (score {ans.get('score', 0)}):", text(ans.get("body", ""))]
    return {"url": url, "title": text(items[0]["title"]), "text": "\n".join(parts)[:20_000]}


def quality(r: dict) -> float:
    """Popularity/answer signals from API sources, on a log scale."""
    if r["source"] == "stackoverflow":
        return 0.3 * math.log1p(max(r.get("score", 0), 0)) + (0.6 if r.get("accepted") else 0.3 if r.get("answers") else -0.8)
    if r["source"] == "github":
        return 0.15 * math.log1p(r.get("stars", 0))
    if r["source"] == "hackernews":
        return 0.1 * math.log1p(r.get("points", 0))
    if r["source"] == "npm":
        return 0.08 * math.log1p(r.get("weekly_downloads", 0))
    return 0.0


async def gather_sources(calls: dict[str, "asyncio.Future"]) -> tuple[list[dict], dict[str, str]]:
    """Run source coroutines concurrently; a failing source is reported, not fatal."""
    names = list(calls)
    results = await asyncio.gather(*calls.values(), return_exceptions=True)
    merged, errors = [], {}
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            errors[name] = f"{type(res).__name__}: {res}"[:200]
        else:
            merged.extend(res)
    return merged, errors


# ---- page contents ----------------------------------------------------------------------------

_DROP = ["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "iframe"]


def extract_text(html: str, max_chars: int = 20_000) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(_DROP):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    blocks = []
    for el in root.find_all(["h1", "h2", "h3", "p", "li", "pre", "td"]):
        text = el.get_text(" ", strip=True)
        if len(text) >= 30 or el.name in ("h1", "h2", "h3"):
            blocks.append(text)
    text = "\n".join(dict.fromkeys(blocks))  # drop repeated blocks, keep order
    if len(text) < 200:  # pages without semantic markup
        text = re.sub(r"\s+", " ", root.get_text(" ", strip=True))
    return title, text[:max_chars]


async def fetch_page(http: httpx.AsyncClient, url: str) -> dict:
    try:
        resp = await http.get(url)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return {"url": url, "error": f"unsupported content-type {ctype}"}
        title, text = extract_text(resp.text)
        return {"url": str(resp.url), "title": title, "text": text}
    except Exception as e:
        return {"url": url, "error": f"{type(e).__name__}: {e}"[:200]}


async def fetch_pages(http: httpx.AsyncClient, urls: list[str]) -> list[dict]:
    return list(await asyncio.gather(*(fetch_page(http, u) for u in urls)))


# ---- ranking ----------------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")
_STOP = set("the a an of to and or in on for with is are be how what why does do i you it this that by from at as".split())


def tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


def normalize_url(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme.lower(), p.netloc.lower().removeprefix("www."), p.path.rstrip("/"), "", p.query, ""))


def dedupe(results: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in results:
        key = normalize_url(r["url"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def bm25_rank(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    q = tokens(query)
    toks = [tokens(d) for d in docs]
    if not toks:
        return []
    avgdl = sum(len(t) for t in toks) / len(toks) or 1
    df = Counter(w for t in toks for w in set(t))
    n = len(toks)
    scores = []
    for t in toks:
        tf = Counter(t)
        s = 0.0
        for w in q:
            if w not in tf:
                continue
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(t) / avgdl))
        scores.append(s)
    return scores


def best_passages(text: str, query: str, k: int = 3, max_len: int = 400) -> list[str]:
    """Split into paragraphs/sentences and return the k most query-relevant ones."""
    parts = [p.strip() for p in re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z])", text) if 40 <= len(p.strip()) <= 2000]
    if not parts:
        return []
    scores = bm25_rank(query, parts)
    ranked = sorted(zip(scores, range(len(parts))), key=lambda x: -x[0])
    picked = sorted(i for s, i in ranked[:k] if s > 0)
    return [parts[i][:max_len] for i in picked]
