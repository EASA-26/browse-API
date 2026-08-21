"""Commercial adapter for serpapi.com's wire format (COMMERCIAL_FORMAT=serpapi).

SerpApi speaks GET with query params and returns differently named blocks
(organic_results, related_questions, ...); this adapter maps them into the
canonical schema so an existing SerpApi subscription can serve as the
quality fall-through / high-fidelity provider with zero consumer changes.

Phase 1 scope: /search and /news (the fall-through verticals). Other
verticals raise UnsupportedVerticalError and keep their 501 behaviour.
"""
import logging
from typing import Any

import httpx

from app.config import ProviderName, Settings, Vertical
from app.providers.base import (
    ProviderAuthError,
    ProviderError,
    SearchProvider,
    UnsupportedVerticalError,
)
from app.rerank import rerank_results
from app.schemas import SearchRequest

logger = logging.getLogger(__name__)

# How many results to ask SerpApi for, whatever the consumer asked for.
#
# SerpApi bills per search, not per result, so a wider window is free. It has
# to be wider: the fall-through exists to answer the queries the metasearch
# could not, and those are the ones whose answer sits low. Asked "ceo tnb
# 2017", SerpApi returns the 2017 annual report and the Azman Mohd story at
# positions seven and eight -- a consumer asking for five never sees either,
# and would be told the current officeholder instead. Fetch wide, re-rank,
# then cut to what was asked for.
FALLTHROUGH_POOL = 20


def _normalize_organic(raw: dict[str, Any]) -> list[dict[str, Any]]:
    organic: list[dict[str, Any]] = []
    for index, item in enumerate(raw.get("organic_results", []), start=1):
        if not item.get("title") or not item.get("link"):
            continue
        entry: dict[str, Any] = {
            "title": item["title"],
            "link": item["link"],
            "position": item.get("position", index),
        }
        if item.get("snippet"):
            entry["snippet"] = item["snippet"]
        if item.get("date"):
            entry["date"] = str(item["date"])
        inline = (item.get("sitelinks") or {}).get("inline") or []
        sitelinks = [
            {"title": s["title"], "link": s["link"]}
            for s in inline
            if s.get("title") and s.get("link")
        ]
        if sitelinks:
            entry["sitelinks"] = sitelinks
        organic.append(entry)
    return organic


def normalize_search(raw: dict[str, Any]) -> dict[str, Any]:
    blocks: dict[str, Any] = {}
    organic = _normalize_organic(raw)
    if organic:
        blocks["organic"] = organic

    kg_raw = raw.get("knowledge_graph") or {}
    if kg_raw.get("title"):
        kg: dict[str, Any] = {"title": kg_raw["title"]}
        for source, target in (("type", "type"), ("description", "description"),
                               ("website", "website")):
            if kg_raw.get(source):
                kg[target] = kg_raw[source]
        blocks["knowledgeGraph"] = kg

    paa = [
        {k: v for k, v in {
            "question": item.get("question"),
            "snippet": item.get("snippet"),
            "title": item.get("title"),
            "link": item.get("link"),
        }.items() if v}
        for item in raw.get("related_questions", [])
        if item.get("question")
    ]
    if paa:
        blocks["peopleAlsoAsk"] = paa

    related = [
        {"query": item["query"]}
        for item in raw.get("related_searches", [])
        if item.get("query")
    ]
    if related:
        blocks["relatedSearches"] = related
    return blocks


def rerank_raw(
    raw: dict[str, Any], request: SearchRequest, settings: Settings, vertical: Vertical
) -> None:
    """Re-rank SerpApi's own results by query relevance, then cut to num.

    In place, and before normalisation, which is where the searxng provider
    does it too. Position is re-stamped because the normalisers prefer
    SerpApi's own position field over their enumeration, and a re-ranked list
    still carrying the old numbers would report an order it is not in.
    """
    key = "news_results" if vertical is Vertical.NEWS else "organic_results"
    items = raw.get(key)
    if not isinstance(items, list) or not items:
        return
    if settings.rerank:
        # SerpApi calls the summary a snippet; the metasearch calls it content.
        items = rerank_results(items, request.q, title_key="title", content_key="snippet")
    items = items[: request.num]
    for index, item in enumerate(items, start=1):
        if isinstance(item, dict):
            item["position"] = index
    raw[key] = items


def normalize_news(raw: dict[str, Any]) -> dict[str, Any]:
    news: list[dict[str, Any]] = []
    for index, item in enumerate(raw.get("news_results", []), start=1):
        if not item.get("title") or not item.get("link"):
            continue
        entry: dict[str, Any] = {
            "title": item["title"],
            "link": item["link"],
            "position": item.get("position", index),
        }
        if item.get("snippet"):
            entry["snippet"] = item["snippet"]
        if item.get("date"):
            entry["date"] = str(item["date"])
        source = item.get("source")
        if isinstance(source, dict):
            source = source.get("name")
        if source:
            entry["source"] = str(source)
        if item.get("thumbnail"):
            entry["imageUrl"] = item["thumbnail"]
        news.append(entry)
    return {"news": news} if news else {}


class SerpApiProvider(SearchProvider):
    name = ProviderName.COMMERCIAL

    def __init__(
        self,
        settings: Settings,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._base_url = (settings.commercial_base_url or "https://serpapi.com").rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=settings.http_timeout, transport=transport)

    def build_params(self, request: SearchRequest, vertical: Vertical) -> dict[str, Any]:
        params: dict[str, Any] = {
            "engine": "google",
            "q": request.q,
            "gl": request.gl,
            "hl": request.hl,
            # Wider than asked for, so re-ranking has something to choose from.
            # Only on the first page: a wider window on page two would overlap
            # page one, and paging is not what the fall-through does.
            "num": max(request.num, FALLTHROUGH_POOL) if request.page == 1 else request.num,
            "start": (request.page - 1) * request.num,
            "api_key": self._api_key,
        }
        if request.location:
            params["location"] = request.location
        if request.tbs:
            params["tbs"] = request.tbs
        if vertical is Vertical.NEWS:
            params["tbm"] = "nws"
        return params

    async def search(self, request: SearchRequest, vertical: Vertical) -> dict[str, Any]:
        if vertical not in (Vertical.SEARCH, Vertical.NEWS):
            raise UnsupportedVerticalError(
                f"serpapi adapter does not serve '{vertical.value}' yet"
            )
        try:
            response = await self._client.get(
                f"{self._base_url}/search.json", params=self.build_params(request, vertical)
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"serpapi request failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise ProviderAuthError("serpapi rejected the API key")
        if response.status_code != 200:
            raise ProviderError(f"serpapi returned HTTP {response.status_code}")
        try:
            raw = response.json()
        except ValueError as exc:
            raise ProviderError("serpapi returned non-JSON payload") from exc
        if raw.get("error"):
            raise ProviderError(f"serpapi error: {raw['error']}")

        rerank_raw(raw, request, self._settings, vertical)
        if vertical is Vertical.NEWS:
            return normalize_news(raw)
        return normalize_search(raw)

    async def aclose(self) -> None:
        await self._client.aclose()
