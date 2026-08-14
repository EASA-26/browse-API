"""All search verticals: one POST endpoint per vertical, one shared pipeline.

Pipeline: auth -> rate limit -> quota check -> cache -> provider -> deduct
credits (success only) -> usage log. Verticals whose configured provider isn't
implemented return 501 with a clear message instead of failing opaquely.
"""
import hashlib
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import ProviderFactory, get_db, get_policy, get_provider_factory, get_redis
from app.auth import require_api_key
from app.cache import CacheBackend, ResponseCache, build_cache_key
from app.config import ProviderName, Vertical, get_settings
from app.db import ApiKey, DatabaseBackend
from app.metrics import (
    CACHE_REQUESTS,
    DEGRADED_RESPONSES,
    FALLTHROUGH,
    POLICY_BLOCKED,
    PROVIDER_LATENCY,
    PROVIDER_REQUESTS,
    QUALITY_SCORE,
    REQUEST_LATENCY,
    REQUESTS,
)
from app.policy import PolicyBlockedError, PolicyGate
from app.providers.base import (
    ProviderError,
    ProviderNotConfiguredError,
    SearchProvider,
    UnsupportedVerticalError,
)
from app.ratelimit import LimiterBackend, RateLimiter
from app.schemas import (
    AutocompleteResponse,
    BaseSearchResponse,
    ImagesResponse,
    NewsResponse,
    PatentsResponse,
    PlacesResponse,
    ScholarResponse,
    SearchParameters,
    SearchRequest,
    SearchResponse,
    ShoppingResponse,
    VideosResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])

RESPONSE_MODELS: dict[Vertical, type[BaseSearchResponse]] = {
    Vertical.SEARCH: SearchResponse,
    Vertical.IMAGES: ImagesResponse,
    Vertical.NEWS: NewsResponse,
    Vertical.PLACES: PlacesResponse,
    Vertical.VIDEOS: VideosResponse,
    Vertical.SHOPPING: ShoppingResponse,
    Vertical.SCHOLAR: ScholarResponse,
    Vertical.PATENTS: PatentsResponse,
    Vertical.AUTOCOMPLETE: AutocompleteResponse,
}

# Blocks grafted onto /search from the commercial provider when enrichment is on.
ENRICHMENT_BLOCKS = ("knowledgeGraph", "peopleAlsoAsk")

# The block that must be non-empty for a fall-through result to replace the
# degraded primary one.
PRIMARY_BLOCK: dict[Vertical, str] = {
    Vertical.SEARCH: "organic",
    Vertical.IMAGES: "images",
    Vertical.NEWS: "news",
    Vertical.VIDEOS: "videos",
}


async def _try_fallthrough(
    blocks: dict[str, Any],
    body: SearchRequest,
    vertical: Vertical,
    provider_factory: ProviderFactory,
) -> tuple[dict[str, Any], bool]:
    """Plan 2.3: degraded genxng result -> try the commercial provider.

    Returns (blocks, served_by_commercial). Any failure keeps the original
    degraded result — fall-through must never make a response worse.
    """
    try:
        commercial = provider_factory(ProviderName.COMMERCIAL)
        alternative = await _timed_provider_search(commercial, body, vertical)
    except ProviderError as exc:
        FALLTHROUGH.labels(vertical.value, "error").inc()
        logger.warning("quality fall-through failed", extra={"vertical": vertical.value,
                                                            "error": str(exc)})
        return blocks, False
    if alternative.get(PRIMARY_BLOCK[vertical]):
        FALLTHROUGH.labels(vertical.value, "served").inc()
        return alternative, True
    FALLTHROUGH.labels(vertical.value, "kept_primary").inc()
    return blocks, False


async def _timed_provider_search(
    provider: SearchProvider, body: SearchRequest, vertical: Vertical
) -> dict[str, Any]:
    labels = (provider.name.value, vertical.value)
    start = time.perf_counter()
    try:
        blocks = await provider.search(body, vertical)
    except UnsupportedVerticalError:
        PROVIDER_REQUESTS.labels(*labels, "unsupported").inc()
        raise
    except ProviderError:
        PROVIDER_REQUESTS.labels(*labels, "error").inc()
        raise
    finally:
        PROVIDER_LATENCY.labels(*labels).observe(time.perf_counter() - start)
    PROVIDER_REQUESTS.labels(*labels, "success").inc()
    return blocks


async def _enrich_search(
    blocks: dict[str, Any],
    provenance: dict[str, str],
    body: SearchRequest,
    provider_factory: ProviderFactory,
) -> None:
    """Fill missing high-fidelity blocks from the commercial provider.

    Enrichment is best-effort: any failure is logged and the primary
    provider's response is returned untouched.
    """
    try:
        commercial = provider_factory(ProviderName.COMMERCIAL)
        extra = await _timed_provider_search(commercial, body, Vertical.SEARCH)
    except ProviderError as exc:
        logger.warning("search enrichment skipped: %s", exc)
        return
    for block in ENRICHMENT_BLOCKS:
        if block in extra and block not in blocks:
            blocks[block] = extra[block]
            provenance[block] = ProviderName.COMMERCIAL.value


async def handle_vertical(
    request: Request,
    vertical: Vertical,
    body: SearchRequest,
    api_key: ApiKey,
    db: DatabaseBackend,
    redis_client: aioredis.Redis | None,
    provider_factory: ProviderFactory,
    policy: PolicyGate,
) -> BaseSearchResponse:
    settings = get_settings()
    start = time.perf_counter()
    status = "error"
    try:
        # Classification gate FIRST (plan 3.2): a sensitive query fails closed
        # before the rate limiter, quota, cache, and every provider path —
        # nothing about it egresses, persists, or costs credits.
        category = policy.check(body.q)
        if category is not None:
            status = "policy_blocked"
            POLICY_BLOCKED.labels(category).inc()
            logger.warning(
                "query blocked by policy",
                extra={
                    "api_key": api_key.name,
                    "vertical": vertical.value,
                    "category": category,
                    "query_hash": hashlib.sha256(body.q.encode()).hexdigest()[:16],
                },
            )
            raise PolicyBlockedError(category)

        # Redis-backed per request, or the lifespan's in-process twins when the
        # zero-container mode left no Redis to wrap.
        limiter: LimiterBackend
        if redis_client is None:
            limiter = request.app.state.limiter
        else:
            limiter = RateLimiter(
                redis_client, settings.rate_limit_qps, settings.rate_limit_burst
            )
        allowed, retry_after = await limiter.check(api_key.id)
        if not allowed:
            status = "rate_limited"
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

        cost = settings.credits_for(vertical)
        if api_key.credits < cost:
            status = "quota_exceeded"
            raise HTTPException(
                status_code=402,
                detail=f"Insufficient credits: balance {api_key.credits}, cost {cost}",
            )

        cache: CacheBackend
        if redis_client is None:
            cache = request.app.state.cache
        else:
            cache = ResponseCache(redis_client)
        cache_key = build_cache_key(vertical, body)
        cached = await cache.get(cache_key)
        served_from_cache = cached is not None
        CACHE_REQUESTS.labels(vertical.value, "hit" if served_from_cache else "miss").inc()

        blocks: dict[str, Any]
        if cached is not None:
            blocks = dict(cached["blocks"])
            if "searchMeta" in blocks:
                blocks["searchMeta"] = {**blocks["searchMeta"], "cached": True}
            source_provider: str = cached.get("provider", "unknown")
            provenance: dict[str, str] = {b: "cache" for b in blocks if b != "searchMeta"}
        else:
            provider_name = settings.provider_for(vertical)
            try:
                provider = provider_factory(provider_name)
            except ProviderNotConfiguredError as exc:
                status = "not_configured"
                raise HTTPException(
                    status_code=501,
                    detail=(
                        f"'{vertical.value}' is served by provider '{provider_name.value}', "
                        "which is not configured yet"
                    ),
                ) from exc
            try:
                blocks = await _timed_provider_search(provider, body, vertical)
            except UnsupportedVerticalError as exc:
                status = "unsupported"
                raise HTTPException(status_code=501, detail=str(exc)) from exc
            except ProviderError as exc:
                status = "provider_error"
                logger.error(
                    "provider failed",
                    extra={"provider": provider.name.value, "vertical": vertical.value,
                           "error": str(exc)},
                )
                raise HTTPException(
                    status_code=502, detail="Upstream search provider failed"
                ) from exc
            source_provider = provider.name.value
            meta = blocks.get("searchMeta")
            if meta is not None:
                QUALITY_SCORE.labels(vertical.value).observe(meta["qualityScore"])
                if meta["degraded"]:
                    DEGRADED_RESPONSES.labels(vertical.value).inc()
            if (
                meta is not None
                and meta["degraded"]
                and settings.quality_fallthrough
                and provider.name is not ProviderName.COMMERCIAL
                and vertical in PRIMARY_BLOCK
            ):
                blocks, served_by_commercial = await _try_fallthrough(
                    blocks, body, vertical, provider_factory
                )
                if served_by_commercial:
                    source_provider = ProviderName.COMMERCIAL.value
            provenance = {b: source_provider for b in blocks if b != "searchMeta"}
            if (
                vertical is Vertical.SEARCH
                and settings.search_enrichment
                and source_provider != ProviderName.COMMERCIAL.value
            ):
                await _enrich_search(blocks, provenance, body, provider_factory)
            await cache.set(
                cache_key,
                {"blocks": blocks, "provider": source_provider},
                ttl=settings.ttl_for(vertical),
            )

        # Success is certain from here: charge, then account.
        latency_ms = int((time.perf_counter() - start) * 1000)
        if not await db.deduct_credits(api_key.id, cost):
            logger.warning(
                "credit deduction failed",
                extra={"api_key": api_key.name, "vertical": vertical.value, "cost": cost},
            )
        try:
            await db.log_usage(
                api_key.id, vertical.value, cost, source_provider, served_from_cache, latency_ms
            )
        except Exception:
            logger.exception("usage log insert failed")

        response = RESPONSE_MODELS[vertical].model_validate(
            {
                "searchParameters": SearchParameters(
                    q=body.q,
                    gl=body.gl,
                    hl=body.hl,
                    location=body.location,
                    num=body.num,
                    page=body.page,
                    tbs=body.tbs,
                    type=vertical.value,
                ),
                "credits": cost,
                **blocks,
            }
        )
        if body.debug:
            response.providersUsed = provenance
        status = "success"
        log_fields: dict[str, Any] = {
            "api_key": api_key.name,
            "vertical": vertical.value,
            "provider": source_provider,
            "cached": served_from_cache,
            "latency_ms": latency_ms,
            "credits": cost,
            # query text is never logged by default (log hygiene, plan 3.3);
            # the short hash still lets identical queries be correlated.
            "query_hash": hashlib.sha256(body.q.encode()).hexdigest()[:16],
        }
        if settings.log_queries:
            log_fields["query"] = body.q
        meta_block = blocks.get("searchMeta")
        if meta_block is not None:
            log_fields["quality_score"] = meta_block["qualityScore"]
            log_fields["degraded"] = meta_block["degraded"]
        logger.info("request served", extra=log_fields)
        return response
    finally:
        REQUESTS.labels(vertical.value, status).inc()
        REQUEST_LATENCY.labels(vertical.value).observe(time.perf_counter() - start)


Endpoint = Callable[..., Coroutine[Any, Any, BaseSearchResponse]]


def _make_endpoint(vertical: Vertical) -> Endpoint:
    async def endpoint(
        request: Request,
        body: SearchRequest,
        api_key: Annotated[ApiKey, Depends(require_api_key)],
        db: Annotated[DatabaseBackend, Depends(get_db)],
        redis_client: Annotated[aioredis.Redis | None, Depends(get_redis)],
        provider_factory: Annotated[ProviderFactory, Depends(get_provider_factory)],
        policy: Annotated[PolicyGate, Depends(get_policy)],
    ) -> BaseSearchResponse:
        return await handle_vertical(
            request, vertical, body, api_key, db, redis_client, provider_factory, policy
        )

    endpoint.__name__ = vertical.value
    return endpoint


for _vertical, _model in RESPONSE_MODELS.items():
    router.add_api_route(
        f"/{_vertical.value}",
        _make_endpoint(_vertical),
        methods=["POST"],
        response_model=_model,
        response_model_exclude_none=True,
        summary=f"{_vertical.value} search",
    )
