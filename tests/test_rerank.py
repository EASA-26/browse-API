"""IDF-weighted relevance re-ranking (the position-17 problem)."""
from typing import Any

from app.rerank import rerank_results


def doc(title: str, content: str = "") -> dict[str, Any]:
    return {"title": title, "content": content}


def test_specific_results_float_above_generic_ones() -> None:
    """Mirror of the live failure: generic 'CEO' pages outranked the pages
    that actually contain the entity + year."""
    results = [
        doc("Chief executive officer - Wikipedia", "The CEO is the highest ranking officer"),
        doc("Hierarchy of Company: CEO, CFO, COO", "The CEO is the highest-ranking executive"),
        doc("CEO (Chief Executive Officer) - CFI", "The CEO is the highest-ranking employee"),
        doc("courtesy meeting by tenaga nasional berhad",
            "On 11th July 2017, TNB ... Azman Mohd, CEO of TNB accompanied"),
        doc("TNB commended for delivery success",
            "Jul 18, 2017 ... TNB chairman was also present"),
    ]
    ranked = rerank_results(results, "ceo tnb 2017")
    top_titles = [r["title"] for r in ranked[:2]]
    assert "courtesy meeting by tenaga nasional berhad" in top_titles
    assert "TNB commended for delivery success" in top_titles
    # generic pages sink below the specific ones
    last_title = str(ranked[-1]["title"])
    assert last_title.startswith(("Chief executive officer", "Hierarchy", "CEO ("))


def test_title_matches_outrank_content_matches() -> None:
    results = [
        doc("some page", "mentions tnb tariff in passing"),
        doc("TNB tariff schedule", "official page"),
    ]
    ranked = rerank_results(results, "tnb tariff")
    assert ranked[0]["title"] == "TNB tariff schedule"


def test_ties_keep_original_engine_order() -> None:
    results = [doc("alpha result", "x"), doc("beta result", "x"), doc("gamma result", "x")]
    ranked = rerank_results(results, "unrelated query terms")
    assert [r["title"] for r in ranked] == ["alpha result", "beta result", "gamma result"]


def test_small_or_empty_inputs_pass_through() -> None:
    assert rerank_results([], "q") == []
    single = [doc("only")]
    assert rerank_results(single, "q") == single
    results = [doc("a"), doc("b")]
    assert rerank_results(results, "") == results


def test_deterministic() -> None:
    results = [doc(f"page {i}", "tnb 2017" if i % 3 == 0 else "filler") for i in range(9)]
    first = rerank_results(list(results), "tnb 2017")
    second = rerank_results(list(results), "tnb 2017")
    assert first == second
