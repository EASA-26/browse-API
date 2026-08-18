"""Result-quality scoring (improvement plan 2.1).

The failure mode that hurts RAG is plausible-but-degraded: two engines
answering instead of eight still returns HTTP 200. Every genxng-served
response therefore carries a machine-readable searchMeta block; consumers
(and the in-API fall-through) act on `degraded` instead of trusting 200.
"""
from typing import Any
from urllib.parse import urlparse

from app.config import Settings

# Response blocks whose items constitute "the results" for quality purposes.
PRIMARY_BLOCKS = ("organic", "images", "news", "videos")


def _responded_engines(raw: dict[str, Any]) -> set[str]:
    responded: set[str] = set()
    for item in raw.get("results", []):
        engines = item.get("engines") or ([item["engine"]] if item.get("engine") else [])
        responded.update(e for e in engines if isinstance(e, str) and e)
    return responded


def _unresponsive_engines(raw: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for entry in raw.get("unresponsive_engines", []):
        if isinstance(entry, (list, tuple)) and entry and isinstance(entry[0], str):
            names.add(entry[0])
    return names


def _normalize_url(url: str) -> str:
    parsed = urlparse(url.lower())
    return f"{parsed.netloc.removeprefix('www.')}{parsed.path.rstrip('/')}"


def duplicate_rate(blocks: dict[str, Any]) -> float:
    """Share of primary-block results whose (normalized) URL repeats."""
    links = [
        item.get("link", "")
        for name in PRIMARY_BLOCKS
        for item in blocks.get(name, [])
        if item.get("link")
    ]
    if not links:
        return 0.0
    unique = len({_normalize_url(link) for link in links})
    return round(1 - unique / len(links), 3)


def build_search_meta(
    raw: dict[str, Any],
    blocks: dict[str, Any],
    expected_count: int,
    settings: Settings,
) -> dict[str, Any]:
    """Compute the searchMeta block from a raw genxng payload + normalized blocks."""
    responded = _responded_engines(raw)
    unresponsive = _unresponsive_engines(raw)
    queried = responded | unresponsive

    coverage = len(responded) / len(queried) if queried else 0.0
    result_count = sum(len(blocks.get(name, [])) for name in PRIMARY_BLOCKS)
    sufficiency = min(result_count / expected_count, 1.0) if expected_count > 0 else 0.0
    dup_rate = duplicate_rate(blocks)

    score = (
        settings.quality_weight_coverage * coverage
        + settings.quality_weight_sufficiency * sufficiency
        - settings.quality_weight_duplicates * dup_rate
    )
    score = round(max(0.0, min(1.0, score)), 3)

    # Sufficiency must not mask engine collapse: many results from one
    # engine is still a degraded search (the coverage floor catches it).
    degraded = score < settings.quality_degraded_threshold
    if settings.quality_coverage_floor > 0 and coverage < settings.quality_coverage_floor:
        degraded = True

    return {
        "enginesQueried": len(queried),
        "enginesResponded": len(responded),
        "engineCoverage": round(coverage, 3),
        "resultCount": result_count,
        "duplicateRate": dup_rate,
        "qualityScore": score,
        "cached": False,
        "degraded": degraded,
    }
