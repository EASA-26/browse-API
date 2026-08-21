"""SearXNG provider: self-hosted metasearch, the on-prem default backend.

Serves search, images, news, videos and autocomplete. Blocks SearXNG cannot
fill (knowledgeGraph fidelity, peopleAlsoAsk, places, shopping, scholar,
patents) are omitted or routed to other providers — never faked.
"""
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import ProviderName, Settings, Vertical
from app.engine_health import get_engine_monitor
from app.pacing import OutboundPacer, OutboundSaturatedError
from app.providers.base import ProviderError, SearchProvider, UnsupportedVerticalError
from app.quality import build_search_meta
from app.query_rewrite import rewrite_query
from app.rerank import rerank_results
from app.schemas import SearchRequest

logger = logging.getLogger(__name__)

# Google tbs time filters -> SearXNG time_range values
TBS_TO_TIME_RANGE = {
    "qdr:d": "day",
    "qdr:w": "week",
    "qdr:m": "month",
    "qdr:y": "year",
}

CATEGORY: dict[Vertical, str] = {
    Vertical.SEARCH: "general",
    Vertical.IMAGES: "images",
    Vertical.NEWS: "news",
    Vertical.VIDEOS: "videos",
}

RESOLUTION_RE = re.compile(r"(\d+)\s*[x×]\s*(\d+)")


def _domain(url: str) -> str | None:
    netloc = urlparse(url).netloc
    return netloc.removeprefix("www.") or None


def normalize_search(raw: dict[str, Any], num: int) -> dict[str, Any]:
    """Map a raw SearXNG JSON payload to canonical /search blocks."""
    blocks: dict[str, Any] = {}

    organic: list[dict[str, Any]] = []
    for position, item in enumerate(raw.get("results", [])[:num], start=1):
        entry: dict[str, Any] = {
            "title": item.get("title") or "",
            "link": item.get("url") or "",
            "position": position,
        }
        if item.get("content"):
            entry["snippet"] = item["content"]
        if item.get("publishedDate"):
            entry["date"] = str(item["publishedDate"])
        organic.append(entry)
    blocks["organic"] = organic

    related = [
        {"query": suggestion}
        for suggestion in raw.get("suggestions", [])
        if isinstance(suggestion, str) and suggestion
    ]
    if related:
        blocks["relatedSearches"] = related

    infoboxes = raw.get("infoboxes") or []
    if infoboxes:
        box = infoboxes[0]
        kg: dict[str, Any] = {"title": box.get("infobox") or box.get("id") or ""}
        if box.get("content"):
            kg["description"] = box["content"]
        if box.get("img_src"):
            kg["imageUrl"] = box["img_src"]
        urls = box.get("urls") or []
        if urls and urls[0].get("url"):
            kg["website"] = urls[0]["url"]
        attributes = {
            attr["label"]: str(attr["value"])
            for attr in box.get("attributes", [])
            if attr.get("label") and attr.get("value") is not None
        }
        if attributes:
            kg["attributes"] = attributes
        if kg["title"]:
            blocks["knowledgeGraph"] = kg

    return blocks


def normalize_images(raw: dict[str, Any], num: int) -> dict[str, Any]:
    images: list[dict[str, Any]] = []
    position = 0
    for item in raw.get("results", []):
        if not item.get("img_src") or not item.get("url"):
            continue
        position += 1
        if position > num:
            break
        entry: dict[str, Any] = {
            "title": item.get("title") or "",
            "imageUrl": item["img_src"],
            "link": item["url"],
            "position": position,
        }
        if item.get("thumbnail_src"):
            entry["thumbnailUrl"] = item["thumbnail_src"]
        source = item.get("source") or _domain(item["url"])
        if source:
            entry["source"] = source
        domain = _domain(item["url"])
        if domain:
            entry["domain"] = domain
        match = RESOLUTION_RE.search(str(item.get("resolution") or ""))
        if match:
            entry["imageWidth"] = int(match.group(1))
            entry["imageHeight"] = int(match.group(2))
        images.append(entry)
    return {"images": images}


def normalize_news(raw: dict[str, Any], num: int) -> dict[str, Any]:
    news: list[dict[str, Any]] = []
    for position, item in enumerate(raw.get("results", [])[:num], start=1):
        entry: dict[str, Any] = {
            "title": item.get("title") or "",
            "link": item.get("url") or "",
            "position": position,
        }
        if item.get("content"):
            entry["snippet"] = item["content"]
        # metadata looks like "13 hours ago | Barchart on MSN"
        date_part, _, source_part = str(item.get("metadata") or "").partition("|")
        if item.get("publishedDate"):
            entry["date"] = str(item["publishedDate"])
        elif date_part.strip():
            entry["date"] = date_part.strip()
        source = source_part.strip() or _domain(item.get("url") or "")
        if source:
            entry["source"] = source
        if item.get("thumbnail"):
            entry["imageUrl"] = item["thumbnail"]
        news.append(entry)
    return {"news": news}


def normalize_videos(raw: dict[str, Any], num: int) -> dict[str, Any]:
    videos: list[dict[str, Any]] = []
    for position, item in enumerate(raw.get("results", [])[:num], start=1):
        entry: dict[str, Any] = {
            "title": item.get("title") or "",
            "link": item.get("url") or "",
            "position": position,
        }
        if item.get("content"):
            entry["snippet"] = item["content"]
        if item.get("thumbnail"):
            entry["imageUrl"] = item["thumbnail"]
        if item.get("length"):
            entry["duration"] = str(item["length"])
        if item.get("author"):
            entry["channel"] = str(item["author"])
        if item.get("publishedDate"):
            entry["date"] = str(item["publishedDate"])
        source = _domain(item.get("url") or "")
        if source:
            entry["source"] = source
        videos.append(entry)
    return {"videos": videos}


def normalize_autocomplete(raw: Any, num: int) -> dict[str, Any]:
    """The autocompleter returns opensearch JSON: [query, [suggestion, ...], ...]."""
    suggestions: list[dict[str, str]] = []
    if isinstance(raw, list) and len(raw) >= 2 and isinstance(raw[1], list):
        suggestions = [
            {"value": value} for value in raw[1][:num] if isinstance(value, str) and value
        ]
    return {"suggestions": suggestions}


NORMALIZERS = {
    Vertical.SEARCH: normalize_search,
    Vertical.IMAGES: normalize_images,
    Vertical.NEWS: normalize_news,
    Vertical.VIDEOS: normalize_videos,
}


class SearXNGProvider(SearchProvider):
    name = ProviderName.GENXNG

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._base_url = settings.searxng_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=settings.http_timeout, transport=transport)
        self._pacer = OutboundPacer.from_settings(settings)

    async def _get_json(self, path: str, params: dict[str, str | int]) -> Any:
        try:
            await self._pacer.acquire()
        except OutboundSaturatedError as exc:
            raise ProviderError(f"genxng outbound pacing saturated: {exc}") from exc
        try:
            response = await self._client.get(f"{self._base_url}{path}", params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(f"genxng request failed: {exc}") from exc
        if response.status_code != 200:
            raise ProviderError(f"genxng returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError("genxng returned non-JSON payload") from exc

    async def search(self, request: SearchRequest, vertical: Vertical) -> dict[str, Any]:
        if vertical is Vertical.AUTOCOMPLETE:
            raw = await self._get_json("/autocompleter", {"q": request.q})
            return normalize_autocomplete(raw, request.num)

        category = CATEGORY.get(vertical)
        if category is None:
            raise UnsupportedVerticalError(f"genxng provider does not serve '{vertical}'")

        # Metasearch ranks keyword queries far better than natural-language
        # questions; rewrite the latter (searchMeta reports what was sent).
        rewritten = rewrite_query(request.q) if self._settings.query_rewrite else None
        params: dict[str, str | int] = {
            "q": rewritten or request.q,
            "format": "json",
            "pageno": request.page,
            "language": request.hl,
            "categories": category,
        }
        if request.tbs in TBS_TO_TIME_RANGE:
            params["time_range"] = TBS_TO_TIME_RANGE[request.tbs]
        # Engine quarantine (plan 2.2): when the health monitor has flagged
        # engines in this category, explicitly select only the healthy ones.
        healthy = get_engine_monitor(self._settings).healthy_for(category)
        if healthy is not None:
            params["engines"] = ",".join(healthy)

        raw = await self._get_json("/search", params)
        # Re-rank BEFORE slicing to num: the metasearch merge buries specific
        # answers under generically popular pages (see app/rerank.py).
        if self._settings.rerank and vertical in (Vertical.SEARCH, Vertical.NEWS):
            raw["results"] = rerank_results(
                raw.get("results", []), str(params["q"])
            )
        blocks = NORMALIZERS[vertical](raw, request.num)
        meta = build_search_meta(raw, blocks, request.num, self._settings)
        if rewritten is not None:
            meta["rewrittenQuery"] = rewritten
        blocks["searchMeta"] = meta
        return blocks

    async def aclose(self) -> None:
        await self._client.aclose()
