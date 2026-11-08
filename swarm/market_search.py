"""
swarm.market_search — keyless demand-signal search
===================================================

`market.search` used to depend on a paid news index. Two public endpoints answer
the same question without a credential, and both were verified live before this
module was written:

* **Hacker News (Algolia)** — tech and startup chatter, JSON, no key, no quota.
* **SEC EDGAR full-text search** — filings mentioning an exact phrase, JSON,
  needs a descriptive User-Agent and roughly ten requests a minute.

Neither replaces a commercial news index. Both mean a scout can search while the
operator is asleep, which is the difference between "waiting on a signup" and
"running".

Design rules, same as the rest of the control plane: standard library only,
nothing raises, a dead source is data rather than an exception, and one failing
source never prevents the other from answering.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "Hit",
    "search",
    "search_hackernews",
    "search_edgar",
    "probe",
    "SOURCES",
    "DEFAULT_SOURCES",
]

USER_AGENT = "passive_income_swarm/1.0 (market.search; operator contact in repo)"
HN_URL = "https://hn.algolia.com/api/v1/search"
EDGAR_URL = "https://efts.sec.gov/LATEST/search-index"

SOURCES = ("hackernews", "edgar")
DEFAULT_SOURCES = ("hackernews", "edgar")

_TAGS = re.compile(r"<[^>]+>")

#: opener(url, headers, timeout) -> (status, body_bytes)
Opener = Callable[[str, Dict[str, str], float], Tuple[int, bytes]]


@dataclass(frozen=True)
class Hit:
    source: str
    title: str
    url: str
    published: str
    snippet: str
    score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _clean(text: Any, limit: int = 280) -> str:
    value = _TAGS.sub(" ", str(text or ""))
    value = html.unescape(value)
    value = " ".join(value.split())
    return value[:limit]


def _default_opener(url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


def _get_json(url: str, timeout: float, opener: Optional[Opener] = None) -> Dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    status, body = (opener or _default_opener)(url, headers, timeout)
    if not 200 <= status < 300:
        raise RuntimeError(f"HTTP {status}")
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #
def search_hackernews(
    query: str, limit: int = 5, timeout: float = 8.0, opener: Optional[Opener] = None
) -> List[Hit]:
    """Tech/startup chatter from the public Algolia index."""
    params = urllib.parse.urlencode(
        {"query": query, "tags": "story", "hitsPerPage": str(max(1, min(limit, 50)))}
    )
    data = _get_json(f"{HN_URL}?{params}", timeout, opener)
    hits: List[Hit] = []
    for raw in (data.get("hits") or [])[:limit]:
        if not isinstance(raw, dict):
            continue
        object_id = str(raw.get("objectID") or "")
        url = str(raw.get("url") or "")
        if not url and object_id:
            url = f"https://news.ycombinator.com/item?id={object_id}"
        hits.append(
            Hit(
                source="hackernews",
                title=_clean(raw.get("title") or raw.get("story_title"), 200),
                url=url,
                published=str(raw.get("created_at") or ""),
                snippet=_clean(raw.get("story_text") or raw.get("comment_text"), 280),
                score=float(raw.get("points") or 0.0),
            )
        )
    return hits


def _edgar_url(raw: Dict[str, Any]) -> str:
    """Build the archive URL from the hit's own identifiers, or return ''.

    No guessed paths: if the response does not carry a CIK and a filename, the
    hit gets no link rather than a plausible-looking wrong one.
    """
    source = raw.get("_source") or {}
    identifier = str(raw.get("_id") or "")
    if ":" not in identifier:
        return ""
    accession, filename = identifier.split(":", 1)
    ciks = source.get("ciks") or []
    if not ciks or not filename:
        return ""
    cik = str(ciks[0]).strip().lstrip("0") or "0"
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{cik}/{accession.replace('-', '')}/{filename}"
    )


def search_edgar(
    query: str, limit: int = 5, timeout: float = 8.0, opener: Optional[Opener] = None
) -> List[Hit]:
    """Filings that mention an exact phrase, via SEC full-text search."""
    params = urllib.parse.urlencode({"q": f'"{query}"', "forms": "8-K"})
    data = _get_json(f"{EDGAR_URL}?{params}", timeout, opener)
    block = data.get("hits") or {}
    rows = block.get("hits") if isinstance(block, dict) else []
    hits: List[Hit] = []
    for raw in (rows or [])[:limit]:
        if not isinstance(raw, dict):
            continue
        source = raw.get("_source") or {}
        names = source.get("display_names") or []
        form = str(source.get("file_type") or source.get("form") or "").strip()
        if form and names:
            title = f"{form} — {names[0]}"
        else:
            title = form or (str(names[0]) if names else "")
        hits.append(
            Hit(
                source="edgar",
                title=_clean(title, 200),
                url=_edgar_url(raw),
                published=str(source.get("file_date") or ""),
                snippet=_clean(
                    source.get("file_description") or source.get("period_ending"), 280
                ),
                score=float(raw.get("_score") or 0.0),
            )
        )
    return hits


_SEARCHERS = {"hackernews": search_hackernews, "edgar": search_edgar}


# --------------------------------------------------------------------------- #
# unified entry point
# --------------------------------------------------------------------------- #
def search(
    query: str,
    *,
    sources: Sequence[str] = DEFAULT_SOURCES,
    limit: int = 5,
    timeout: float = 8.0,
    opener: Optional[Opener] = None,
) -> Dict[str, Any]:
    """Search every requested source. One failure never hides another's results."""
    cleaned = " ".join(str(query or "").split())
    report: Dict[str, Any] = {"query": cleaned, "hits": [], "sources": {}}
    if not cleaned:
        for name in sources:
            report["sources"][name] = {"ok": False, "error": "empty query", "count": 0}
        return report

    for name in sources:
        searcher = _SEARCHERS.get(name)
        if searcher is None:
            report["sources"][name] = {
                "ok": False,
                "error": f"unknown source (known: {', '.join(SOURCES)})",
                "count": 0,
            }
            continue
        try:
            found = searcher(cleaned, limit=limit, timeout=timeout, opener=opener)
            report["sources"][name] = {"ok": True, "error": None, "count": len(found)}
            report["hits"].extend(found)
        except Exception as exc:  # noqa: BLE001 - a dead source is data
            report["sources"][name] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "count": 0,
            }

    report["hits"].sort(key=lambda hit: hit.score, reverse=True)
    return report


def probe(timeout: float = 6.0, opener: Optional[Opener] = None):
    """Reachability check for the connection registry."""
    from .connections import Probe  # local import: connections also imports sources

    started = time.monotonic()
    try:
        hits = search_hackernews("invoice", limit=1, timeout=timeout, opener=opener)
    except Exception as exc:  # noqa: BLE001
        return Probe(ok=False, detail=f"hackernews: {type(exc).__name__}: {exc}")
    latency = round((time.monotonic() - started) * 1000.0, 1)
    return Probe(
        ok=True,
        detail=f"hackernews {len(hits)} hit(s), keyless",
        latency_ms=latency,
        meta={"hits": len(hits)},
    )
