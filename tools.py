"""tools.py - STUDENT IMPLEMENTS.  Source tools for the research agents.   Guide: GUIDE.md, part 1.

Rules for every tool:
  * runs on the HOST (not in the sandbox): API keys must never enter the sandbox;
  * returns a STRING (JSON text of compact records) and NEVER raises:
        "NO RESULTS"  when the source answers with nothing,
        "ERROR: ..."  when the source keeps failing after the retries (the agent then tries another source);
  * the docstring is the tool description the LLM reads: keep it precise (what it does, what it returns, when to use it).
Try your tools without any agent:   python tools.py
"""
import json
import os
import random
import re
import time
import xml.etree.ElementTree as ET

import httpx
from langchain_core.tools import tool

# ---- constants (given) ----
ARXIV_URL = "https://export.arxiv.org/api/query"  # https only: http answers 301
HF_DAILY_URL = "https://huggingface.co/api/daily_papers"
HF_SEARCH_URL = "https://huggingface.co/api/papers/search"
EXA_URL = "https://mcp.exa.ai/mcp"

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RetryableError(Exception):
    """Given. Raise it inside a call to ask with_retry to wait and try again (retry_after in seconds, optional)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


# ---- TODO 1: retry helper ----
def with_retry(fn, *, attempts=5, base=1.0, cap=30.0):
    """Call fn(); when it raises RetryableError, wait and call it again."""
    for attempt in range(attempts):
        try:
            return fn()
        except RetryableError as exc:
            if attempt == attempts - 1:
                raise
            if exc.retry_after is not None:
                delay = min(float(exc.retry_after), cap)
            else:
                delay = min(base * 2**attempt, cap) + random.uniform(0, base)
            time.sleep(delay)


def _clean(text):
    """Collapse whitespace/newlines into single spaces."""
    return " ".join(str(text or "").split())


def _http_get(url, params, *, timeout=30.0):
    """GET wrapped so 429/5xx and transport errors become RetryableError; everything else is raised as-is."""

    def attempt():
        try:
            resp = httpx.get(url, params=params, timeout=timeout)
        except httpx.TransportError as exc:
            raise RetryableError(str(exc)) from exc
        if resp.status_code in _RETRYABLE_STATUS:
            retry_after = resp.headers.get("Retry-After")
            raise RetryableError(f"HTTP {resp.status_code}", retry_after=float(retry_after) if retry_after else None)
        resp.raise_for_status()
        return resp

    return attempt


# ---- TODO 2: arXiv ----
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
_last_arxiv_call = 0.0


@tool
def arxiv_search(query: str, max_results: int = 10) -> str:
    """Search arXiv papers by keywords, newest first. Returns a JSON list of {id, url, published, title, summary}."""
    global _last_arxiv_call
    try:
        terms = re.findall(r"[A-Za-z0-9-]+", query)
        if not terms:
            return "NO RESULTS"
        wait = 3.0 - (time.monotonic() - _last_arxiv_call)
        if wait > 0:
            time.sleep(wait)
        params = {
            "search_query": " AND ".join(f"all:{t}" for t in terms),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": max(1, min(max_results, 30)),
            "start": 0,
        }
        # arXiv also throttles by IP (a whole classroom can share one): retry with a bounded cap/attempt count so
        # one stuck call cannot stall the whole run (and the sandbox) for minutes.
        resp = with_retry(_http_get(ARXIV_URL, params), attempts=5, base=3.0, cap=30.0)
        _last_arxiv_call = time.monotonic()
        root = ET.fromstring(resp.text)
        records = []
        for entry in root.findall("atom:entry", _ATOM_NS):
            raw_id = (entry.findtext("atom:id", default="", namespaces=_ATOM_NS) or "").strip()
            arxiv_id = re.sub(r"v\d+$", "", raw_id.rsplit("/", 1)[-1])
            if not arxiv_id:
                continue
            published = (entry.findtext("atom:published", default="", namespaces=_ATOM_NS) or "")[:10]
            title = _clean(entry.findtext("atom:title", default="", namespaces=_ATOM_NS))
            summary = _clean(entry.findtext("atom:summary", default="", namespaces=_ATOM_NS))[:600]
            records.append({"id": arxiv_id, "url": f"https://arxiv.org/abs/{arxiv_id}", "published": published,
                            "title": title, "summary": summary})
        return json.dumps(records, ensure_ascii=False) if records else "NO RESULTS"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


# ---- TODO 3: Hugging Face ----
def _hf_record(item, *, prefer_ai_summary=False):
    paper = item.get("paper") or {}
    pid = paper.get("id")
    if not pid:
        return None
    summary = paper.get("summary") or item.get("summary") or ""
    if prefer_ai_summary and paper.get("ai_summary"):
        summary = paper["ai_summary"]
    return {
        "id": pid,
        "url": f"https://huggingface.co/papers/{pid}",
        "published": (paper.get("publishedAt") or item.get("publishedAt") or "")[:10],
        "title": _clean(paper.get("title") or item.get("title")),
        "summary": _clean(summary)[:600],
        "upvotes": paper.get("upvotes") or 0,
        "github": paper.get("githubRepo") or "",
        "stars": paper.get("githubStars") or 0,
    }


@tool
def hf_daily_papers(limit: int = 30, date: str = "", keyword: str = "") -> str:
    """Hugging Face Daily Papers = what is trending in AI research. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars} sorted by upvotes. `date` is YYYY-MM-DD (empty = latest).
    `keyword` filters title/summary; there is no topic search on this endpoint (use hf_search_papers for a topic)."""
    try:
        params = {"limit": max(1, min(limit, 100))}
        if date:
            params["date"] = date
        resp = with_retry(_http_get(HF_DAILY_URL, params))
        records = [r for r in (_hf_record(item) for item in resp.json()) if r]
        if keyword:
            kw = keyword.lower()
            records = [r for r in records if kw in f"{r['title']} {r['summary']}".lower()]
        records.sort(key=lambda r: r["upvotes"], reverse=True)
        return json.dumps(records, ensure_ascii=False) if records else "NO RESULTS"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


@tool
def hf_search_papers(query: str, limit: int = 10) -> str:
    """Search Hugging Face papers by topic. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars}."""
    try:
        params = {"q": query, "limit": max(1, min(limit, 50))}
        resp = with_retry(_http_get(HF_SEARCH_URL, params))
        records = [r for r in (_hf_record(item, prefer_ai_summary=True) for item in resp.json()) if r]
        return json.dumps(records, ensure_ascii=False) if records else "NO RESULTS"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


# ---- TODO 4: web search / fetch through the Exa MCP endpoint ----
def _exa_endpoint():
    key = os.environ.get("EXA_API_KEY", "").strip()
    return f"{EXA_URL}?exaApiKey={key}" if key else EXA_URL


def _redact(text):
    """The optional EXA_API_KEY can leak into httpx exception text (it rides on the URL): scrub it before it reaches
    the agent."""
    key = os.environ.get("EXA_API_KEY", "").strip()
    text = str(text)
    if key:
        text = text.replace(key, "***")
    return re.sub(r"exaApiKey=[^&\s'\"]+", "exaApiKey=***", text)


def _exa_payload(resp):
    for line in resp.text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    return resp.json()  # a hard-limited/plain-JSON error response has no "data:" framing


def _exa_call(name, arguments):
    def attempt():
        try:
            resp = httpx.post(_exa_endpoint(),
                              json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}},
                              headers={"Content-Type": "application/json",
                                       "Accept": "application/json, text/event-stream"},
                              timeout=60)
        except httpx.TransportError as exc:
            raise RetryableError(_redact(str(exc))) from exc
        if resp.status_code in _RETRYABLE_STATUS:
            retry_after = resp.headers.get("Retry-After")
            raise RetryableError(f"HTTP {resp.status_code}", retry_after=float(retry_after) if retry_after else None)
        resp.raise_for_status()
        payload = _exa_payload(resp)
        error = payload.get("error")
        if error:
            message = _redact(error.get("message", error) if isinstance(error, dict) else error)
            # the Exa free tier answers a hard rate limit as a JSON-RPC error, not always as HTTP 429
            if "rate limit" in str(message).lower():
                raise RetryableError(message)
            raise RuntimeError(message)
        result = payload.get("result") or {}
        meta = result.get("_meta") or {}
        if any("rate" in str(k).lower() and "limit" in str(k).lower() for k in meta):
            # the Exa free tier can also answer HTTP 200 with the limit message folded into the text and a flag here
            raise RetryableError(_redact(f"exa rate limited: {meta}"))
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        return "\n".join(texts).strip()

    # Exa's free tier can stay limited for a while when a whole classroom shares one IP, but a bounded cap/attempt
    # count keeps one stuck call from stalling the whole run (and risking the sandbox's own idle timeout).
    return with_retry(attempt, attempts=5, base=2.0, cap=30.0)


@tool
def web_search(query: str, objective: str = "", num_results: int = 5) -> str:
    """Search the web (Exa). Describe the ideal page in natural language. Returns clean text of the top results with URLs."""
    try:
        objective = objective or f"find pages relevant to: {query}"
        text = _exa_call("web_search_exa", {"query": query, "objective": objective,
                                            "numResults": max(1, min(num_results, 10))})
        return text or "NO RESULTS"
    except Exception as exc:
        return f"ERROR: {_redact(f'{type(exc).__name__}: {exc}')}"


@tool
def web_fetch(url: str) -> str:
    """Read the full content of one web page (e.g. an arXiv abstract page) as markdown. Long pages are truncated."""
    try:
        text = _exa_call("web_fetch_exa", {"urls": [url]})
        return text[:12000] if text else "NO RESULTS"
    except Exception as exc:
        return f"ERROR: {_redact(f'{type(exc).__name__}: {exc}')}"


# ---- TODO 5: registry (the researcher subagent gets exactly these) ----
SOURCE_TOOLS = [arxiv_search, hf_daily_papers, hf_search_papers, web_search, web_fetch]


if __name__ == "__main__":
    for name, fn, args in [
        ("arxiv_search", arxiv_search, {"query": "world model", "max_results": 3}),
        ("hf_daily_papers", hf_daily_papers, {"limit": 20}),
        ("hf_search_papers", hf_search_papers, {"query": "world model", "limit": 3}),
        ("web_search", web_search, {"query": "survey paper on world models", "num_results": 2}),
        ("web_fetch", web_fetch, {"url": "https://arxiv.org/abs/1803.10122"}),
    ]:
        try:
            print(f"== {name}\n{fn.invoke(args)[:400]}\n")
        except NotImplementedError as exc:
            print(f"== {name}: not implemented yet ({exc})\n")
