"""SerpApi-format commercial adapter — mocked transport, no live network."""
import json
from typing import Any

import httpx
import pytest

from app.config import ProviderName, Settings, Vertical
from app.providers.base import ProviderAuthError, ProviderError, UnsupportedVerticalError
from app.providers.serpapi import SerpApiProvider
from app.schemas import SearchRequest, SearchResponse

SETTINGS = Settings(_env_file=None)

SEARCH_PAYLOAD: dict[str, Any] = {
    "search_metadata": {"status": "Success"},
    "organic_results": [
        {
            "position": 1,
            "title": "Datuk Seri Ir. Azman Mohd - Tenaga Nasional",
            "link": "https://www.tnb.com.my/leadership/azman",
            "snippet": "President/CEO of TNB from 2012 until end of 2017.",
            "sitelinks": {"inline": [{"title": "Profile", "link": "https://t/profile"}]},
        },
        {
            "position": 2,
            "title": "TNB leadership history",
            "link": "https://example.com/history",
            "snippet": "Successive presidents and CEOs of Tenaga Nasional Berhad.",
        },
    ],
    "knowledge_graph": {
        "title": "Tenaga Nasional Berhad",
        "type": "Utilities company",
        "description": "Malaysian multinational electricity company.",
        "website": "https://www.tnb.com.my",
    },
    "related_questions": [
        {"question": "Who founded TNB?", "snippet": "s", "title": "t", "link": "https://x"}
    ],
    "related_searches": [{"query": "tnb ceo history"}],
}


def make_provider(handler: Any) -> tuple[SerpApiProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response: httpx.Response = handler(request)
        return response

    provider = SerpApiProvider(SETTINGS, "serpapi-test-key",
                               transport=httpx.MockTransport(record))
    return provider, seen


async def test_search_params_and_normalization() -> None:
    provider, seen = make_provider(lambda r: httpx.Response(200, json=SEARCH_PAYLOAD))
    blocks = await provider.search(
        SearchRequest(q="who is the ceo of tnb in 2017", gl="my", num=10, page=2),
        Vertical.SEARCH,
    )
    params = seen[0].url.params
    assert seen[0].url.path == "/search.json"
    assert params["engine"] == "google"
    assert params["q"] == "who is the ceo of tnb in 2017"
    assert params["gl"] == "my"
    assert params["num"] == "10"
    assert params["start"] == "10"  # page 2
    assert params["api_key"] == "serpapi-test-key"

    assert set(blocks) == {"organic", "knowledgeGraph", "peopleAlsoAsk", "relatedSearches"}
    assert blocks["organic"][0]["title"].startswith("Datuk Seri Ir. Azman Mohd")
    assert blocks["organic"][0]["sitelinks"] == [
        {"title": "Profile", "link": "https://t/profile"}
    ]
    assert blocks["knowledgeGraph"]["title"] == "Tenaga Nasional Berhad"
    assert blocks["peopleAlsoAsk"][0]["question"] == "Who founded TNB?"

    # canonical schema accepts the normalized blocks unchanged
    SearchResponse.model_validate(
        {"searchParameters": {"q": "x", "type": "search"}, "credits": 1, **blocks}
    )


async def test_news_uses_tbm_and_normalizes() -> None:
    payload = {
        "news_results": [
            {
                "position": 1,
                "title": "TNB names new CEO",
                "link": "https://news/1",
                "snippet": "s",
                "date": "07/18/2017",
                "source": {"name": "The Star"},
                "thumbnail": "https://img/1.png",
            }
        ]
    }
    provider, seen = make_provider(lambda r: httpx.Response(200, json=payload))
    blocks = await provider.search(SearchRequest(q="tnb"), Vertical.NEWS)
    assert seen[0].url.params["tbm"] == "nws"
    assert blocks["news"][0]["source"] == "The Star"
    assert blocks["news"][0]["imageUrl"] == "https://img/1.png"


async def test_auth_rejection_is_non_retryable() -> None:
    provider, _ = make_provider(lambda r: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(ProviderAuthError):
        await provider.search(SearchRequest(q="x"), Vertical.SEARCH)


async def test_error_field_in_200_body_raises() -> None:
    provider, _ = make_provider(
        lambda r: httpx.Response(200, json={"error": "Google hasn't returned results"})
    )
    with pytest.raises(ProviderError, match="serpapi error"):
        await provider.search(SearchRequest(q="x"), Vertical.SEARCH)


async def test_unsupported_verticals_stay_truthful() -> None:
    provider, _ = make_provider(lambda r: httpx.Response(200, json={}))
    with pytest.raises(UnsupportedVerticalError):
        await provider.search(SearchRequest(q="x"), Vertical.PLACES)


def test_registry_selects_serpapi_format(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.providers as registry

    settings = Settings(_env_file=None, commercial_format="serpapi")
    monkeypatch.setattr(registry, "get_settings", lambda: settings)
    monkeypatch.setattr(
        registry, "get_secret", lambda name: "k" if name == "COMMERCIAL_API_KEY" else None
    )
    registry.get_provider.cache_clear()
    provider = registry.get_provider(ProviderName.COMMERCIAL)
    # unwrap the resilience layer to check the adapter class
    assert isinstance(provider._inner, SerpApiProvider)  # type: ignore[attr-defined]
    registry.get_provider.cache_clear()


def test_payload_shape_is_json_serializable() -> None:
    assert json.loads(json.dumps(SEARCH_PAYLOAD))["organic_results"][0]["position"] == 1
