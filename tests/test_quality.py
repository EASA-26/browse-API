"""searchMeta quality scoring (improvement plan 2.1)."""
import json
from pathlib import Path
from typing import Any

from app.config import Settings
from app.providers.searxng import normalize_search
from app.quality import build_search_meta, duplicate_rate

SETTINGS = Settings(_env_file=None)
FIXTURE = Path(__file__).parent / "fixtures" / "searxng_search.json"


def load_fixture() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return loaded


def test_recorded_fixture_reports_engine_outage_truthfully() -> None:
    """The recorded fixture IS a degraded response: 4 engines unresponsive,
    only google cse answering. searchMeta must say so — the coverage floor
    catches it even though sufficiency keeps the score at 0.6."""
    raw = load_fixture()
    blocks = normalize_search(raw, num=10)
    meta = build_search_meta(raw, blocks, expected_count=10, settings=SETTINGS)

    assert meta["enginesQueried"] == 5  # google cse + 4 unresponsive
    assert meta["enginesResponded"] == 1
    assert meta["engineCoverage"] == 0.2
    assert meta["resultCount"] == 10
    assert meta["cached"] is False
    # coverage 0.2*0.5 + sufficiency 1.0*0.5 = 0.6 -> above score threshold,
    # but coverage 0.2 < floor 0.4 -> degraded regardless
    assert meta["qualityScore"] == 0.6
    assert meta["degraded"] is True


def test_thin_response_is_degraded() -> None:
    """Two of eight engines answering with few results -> degraded."""
    raw = {
        "results": [
            {"title": "a", "url": "https://a.example/1", "engines": ["google"]},
            {"title": "b", "url": "https://b.example/2", "engines": ["bing"]},
        ],
        "unresponsive_engines": [[e, "timeout"] for e in
                                 ["brave", "duckduckgo", "startpage", "wikipedia",
                                  "qwant", "mojeek"]],
    }
    blocks = normalize_search(raw, num=10)
    meta = build_search_meta(raw, blocks, expected_count=10, settings=SETTINGS)

    assert meta["enginesQueried"] == 8
    assert meta["enginesResponded"] == 2
    assert meta["engineCoverage"] == 0.25
    # 0.25*0.5 + 0.2*0.5 = 0.225 -> below 0.5 threshold
    assert meta["qualityScore"] == 0.225
    assert meta["degraded"] is True


def test_empty_response_scores_zero() -> None:
    raw: dict[str, Any] = {"results": [], "unresponsive_engines": []}
    meta = build_search_meta(raw, {"organic": []}, expected_count=10, settings=SETTINGS)
    assert meta["enginesQueried"] == 0
    assert meta["qualityScore"] == 0.0
    assert meta["degraded"] is True


def test_duplicate_rate_counts_normalized_urls() -> None:
    blocks = {
        "organic": [
            {"link": "https://example.com/page"},
            {"link": "https://www.example.com/page/"},  # same after normalization
            {"link": "https://other.com/x"},
            {"link": "https://other.com/y"},
        ]
    }
    assert duplicate_rate(blocks) == 0.25


def test_duplicates_lower_the_score() -> None:
    raw = {
        "results": [
            {"title": "a", "url": "https://a.example/1", "engines": ["google"]},
        ],
        "unresponsive_engines": [],
    }
    blocks = {
        "organic": [
            {"link": "https://a.example/1", "position": 1},
            {"link": "https://a.example/1", "position": 2},
        ]
    }
    meta = build_search_meta(raw, blocks, expected_count=2, settings=SETTINGS)
    assert meta["duplicateRate"] == 0.5
    # 1.0*0.5 + 1.0*0.5 - 0.5*0.3 = 0.85
    assert meta["qualityScore"] == 0.85


def test_coverage_floor_marks_degraded_despite_full_results() -> None:
    """Sufficiency must not mask engine collapse (the CEO-of-TNB case)."""
    raw = {
        "results": [
            {"title": f"t{i}", "url": f"https://x/{i}", "engines": ["google cse"]}
            for i in range(10)
        ],
        "unresponsive_engines": [[e, "err"] for e in ["a", "b", "c", "d"]],
    }
    blocks = normalize_search(raw, num=10)
    meta = build_search_meta(raw, blocks, expected_count=10, settings=SETTINGS)
    assert meta["qualityScore"] >= 0.5  # score alone would pass
    assert meta["degraded"] is True     # floor catches coverage 0.2


def test_coverage_floor_can_be_disabled() -> None:
    no_floor = Settings(_env_file=None, quality_coverage_floor=0)
    raw = load_fixture()
    blocks = normalize_search(raw, num=10)
    meta = build_search_meta(raw, blocks, expected_count=10, settings=no_floor)
    assert meta["degraded"] is False  # score 0.6 above threshold, no floor


def test_good_coverage_is_not_degraded() -> None:
    raw = {
        "results": [
            {"title": f"t{i}", "url": f"https://x/{i}", "engines": ["google", "bing"]}
            for i in range(10)
        ],
        "unresponsive_engines": [["brave", "err"]],
    }
    blocks = normalize_search(raw, num=10)
    meta = build_search_meta(raw, blocks, expected_count=10, settings=SETTINGS)
    assert meta["engineCoverage"] == round(2 / 3, 3)
    assert meta["degraded"] is False


def test_threshold_is_configurable() -> None:
    strict = Settings(_env_file=None, quality_degraded_threshold=0.7)
    raw = load_fixture()
    blocks = normalize_search(raw, num=10)
    meta = build_search_meta(raw, blocks, expected_count=10, settings=strict)
    assert meta["qualityScore"] == 0.6
    assert meta["degraded"] is True  # 0.6 < 0.7


def test_score_is_clamped() -> None:
    heavy = Settings(_env_file=None, quality_weight_duplicates=5.0)
    raw = {"results": [{"title": "a", "url": "https://a/1", "engines": ["g"]}],
           "unresponsive_engines": []}
    blocks = {"organic": [{"link": "https://a/1"}, {"link": "https://a/1"}]}
    meta = build_search_meta(raw, blocks, expected_count=2, settings=heavy)
    assert meta["qualityScore"] == 0.0
