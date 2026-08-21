"""Provider registry: config decides which backend serves each vertical.

Every provider is wrapped in ResilientProvider (retry with exponential
backoff + per-provider circuit breaker) before it reaches an endpoint.
"""
from functools import lru_cache

from app.config import ProviderName, get_settings
from app.providers.base import ProviderNotConfiguredError, SearchProvider
from app.providers.commercial import CommercialProvider
from app.providers.direct_scrape import DirectScrapeProvider
from app.providers.resilience import CircuitBreaker, ResilientProvider
from app.providers.searxng import SearXNGProvider
from app.providers.serpapi import SerpApiProvider
from app.secrets import get_secret

__all__ = [
    "CommercialProvider",
    "DirectScrapeProvider",
    "ProviderNotConfiguredError",
    "SearXNGProvider",
    "SearchProvider",
    "get_provider",
]


def _build(name: ProviderName) -> SearchProvider:
    settings = get_settings()
    if name is ProviderName.GENXNG:
        return SearXNGProvider(settings)
    if name is ProviderName.COMMERCIAL:
        api_key = get_secret("COMMERCIAL_API_KEY")
        if settings.commercial_format == "serpapi":
            if not api_key:
                raise ProviderNotConfiguredError(
                    "serpapi provider requires COMMERCIAL_API_KEY (env or Vault)"
                )
            return SerpApiProvider(settings, api_key)
        if not settings.commercial_base_url or not api_key:
            raise ProviderNotConfiguredError(
                "commercial provider requires COMMERCIAL_BASE_URL and "
                "COMMERCIAL_API_KEY (env or Vault)"
            )
        return CommercialProvider(settings, api_key)
    if name is ProviderName.DIRECT_SCRAPE:
        if not settings.direct_scrape_enabled:
            raise ProviderNotConfiguredError(
                "direct-scrape provider is disabled (DIRECT_SCRAPE_ENABLED=false); "
                "it is a documented stub — see app/providers/direct_scrape.py"
            )
        return DirectScrapeProvider()
    raise ProviderNotConfiguredError(f"provider '{name}' is not implemented yet")


@lru_cache
def get_provider(name: ProviderName) -> SearchProvider:
    settings = get_settings()
    return ResilientProvider(
        _build(name),
        CircuitBreaker(
            failure_threshold=settings.breaker_failure_threshold,
            recovery_seconds=settings.breaker_recovery_seconds,
        ),
        max_retries=settings.provider_max_retries,
        backoff_seconds=settings.provider_backoff_seconds,
    )
