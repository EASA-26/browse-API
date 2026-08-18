"""NL -> keyword query rewriting for the metasearch backend."""
import httpx
import pytest

from app.config import Settings, Vertical
from app.providers.searxng import SearXNGProvider
from app.query_rewrite import rewrite_query
from app.schemas import SearchRequest


def test_the_original_failing_query() -> None:
    """The exact query from the CEO-of-TNB incident."""
    assert rewrite_query("Who is the ceo of TNB in 2017") == "ceo TNB 2017"


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What is the capital of Malaysia?", "capital Malaysia"),
        ("when did TNB acquire GSPARX", "TNB acquire GSPARX"),
        ("How does a transformer work", "transformer work"),
        ("who was the prime minister in 1990?", "prime minister 1990"),
    ],
)
def test_questions_are_rewritten(question: str, expected: str) -> None:
    assert rewrite_query(question) == expected


@pytest.mark.parametrize(
    "query",
    [
        "attention is all you need",        # statement containing 'is' mid-query
        "TNB CEO 2017",                     # already keywords
        "malaysia electricity tariff",      # no interrogative signal
        "python asyncio.gather timeout",    # technical keywords
    ],
)
def test_keyword_queries_pass_verbatim(query: str) -> None:
    assert rewrite_query(query) is None


def test_too_short_after_rewrite_passes_verbatim() -> None:
    assert rewrite_query("who is he") is None
    assert rewrite_query("what?") is None


def test_case_and_numbers_survive() -> None:
    assert rewrite_query("Who is the CEO of TNB in 2017?") == "CEO TNB 2017"


# --- provider integration ---


def make_provider(settings: Settings, seen: list[httpx.Request]) -> SearXNGProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": [], "unresponsive_engines": []})

    return SearXNGProvider(settings, transport=httpx.MockTransport(handler))


async def test_provider_sends_rewritten_query_and_reports_it() -> None:
    seen: list[httpx.Request] = []
    provider = make_provider(Settings(_env_file=None), seen)
    blocks = await provider.search(
        SearchRequest(q="Who is the ceo of TNB in 2017"), Vertical.SEARCH
    )
    await provider.aclose()
    assert seen[0].url.params["q"] == "ceo TNB 2017"
    assert blocks["searchMeta"]["rewrittenQuery"] == "ceo TNB 2017"


async def test_provider_flag_disables_rewriting() -> None:
    seen: list[httpx.Request] = []
    provider = make_provider(Settings(_env_file=None, query_rewrite=False), seen)
    blocks = await provider.search(
        SearchRequest(q="Who is the ceo of TNB in 2017"), Vertical.SEARCH
    )
    await provider.aclose()
    assert seen[0].url.params["q"] == "Who is the ceo of TNB in 2017"
    assert "rewrittenQuery" not in blocks["searchMeta"]


async def test_keyword_query_sent_verbatim_with_no_meta_field() -> None:
    seen: list[httpx.Request] = []
    provider = make_provider(Settings(_env_file=None), seen)
    blocks = await provider.search(SearchRequest(q="TNB CEO 2017"), Vertical.SEARCH)
    await provider.aclose()
    assert seen[0].url.params["q"] == "TNB CEO 2017"
    assert "rewrittenQuery" not in blocks["searchMeta"]
