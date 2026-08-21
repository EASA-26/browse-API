"""Query-relevance re-ranking of raw metasearch results.

The metasearch merge ranks by engine agreement, which buries specific
answers under generically popular pages: for "ceo tnb 2017" the pages that
actually name the 2017 CEO arrived at positions 10-24, below what RAG
consumers read. This re-ranker floats results that match the *informative*
query terms: each term is weighted by inverse document frequency across the
result set, so rare terms ("tnb", "2017") dominate generic ones ("ceo").

Deterministic and stable: ties keep the engines' original order.
"""
import math
import re

TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def rerank_results(
    results: list[dict[str, object]],
    query: str,
    title_key: str = "title",
    content_key: str = "content",
) -> list[dict[str, object]]:
    """Reorder raw result dicts by IDF-weighted query-term relevance."""
    query_terms = list(dict.fromkeys(_tokens(query)))  # unique, ordered
    if len(results) < 2 or not query_terms:
        return results

    docs: list[tuple[set[str], set[str]]] = []  # (title terms, all terms)
    for item in results:
        title_terms = set(_tokens(str(item.get(title_key) or "")))
        content_terms = set(_tokens(str(item.get(content_key) or "")))
        docs.append((title_terms, title_terms | content_terms))

    total = len(docs)
    idf: dict[str, float] = {}
    for term in query_terms:
        matching = sum(1 for _, all_terms in docs if term in all_terms)
        # +1 smoothing keeps terms present in every doc at weight > 0
        idf[term] = math.log((total + 1) / (matching + 1)) + 0.1

    def score(index: int) -> float:
        title_terms, all_terms = docs[index]
        value = 0.0
        for term in query_terms:
            if term in all_terms:
                value += idf[term] * (1.5 if term in title_terms else 1.0)
        return value

    order = sorted(range(total), key=lambda i: -score(i))  # stable sort
    return [results[i] for i in order]
