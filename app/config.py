"""Application settings.

A single pydantic-settings module. Every vertical is mapped to a provider here;
Phase 1+ providers read this mapping and nothing else decides routing.
"""
from enum import StrEnum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Vertical(StrEnum):
    SEARCH = "search"
    IMAGES = "images"
    NEWS = "news"
    PLACES = "places"
    VIDEOS = "videos"
    SHOPPING = "shopping"
    SCHOLAR = "scholar"
    PATENTS = "patents"
    AUTOCOMPLETE = "autocomplete"


class ProviderName(StrEnum):
    # "genxng" is the user-facing name of the on-prem metasearch backend
    # (implemented by SearXNG under the hood; infra names keep SEARXNG_*).
    GENXNG = "genxng"
    COMMERCIAL = "commercial"
    DIRECT_SCRAPE = "direct_scrape"


DEFAULT_PROVIDER_MAP: dict[Vertical, ProviderName] = {
    Vertical.SEARCH: ProviderName.GENXNG,
    Vertical.IMAGES: ProviderName.GENXNG,
    Vertical.NEWS: ProviderName.GENXNG,
    Vertical.VIDEOS: ProviderName.GENXNG,
    Vertical.AUTOCOMPLETE: ProviderName.GENXNG,
    # High-fidelity verticals need a commercial search API (Phase 3)
    Vertical.PLACES: ProviderName.COMMERCIAL,
    Vertical.SHOPPING: ProviderName.COMMERCIAL,
    Vertical.SCHOLAR: ProviderName.COMMERCIAL,
    Vertical.PATENTS: ProviderName.COMMERCIAL,
}

# Cache TTL per vertical, in seconds. RAG tolerates long TTLs; news/prices do not.
DEFAULT_CACHE_TTL: dict[Vertical, int] = {
    Vertical.SEARCH: 6 * 3600,
    Vertical.IMAGES: 6 * 3600,
    Vertical.NEWS: 300,
    Vertical.PLACES: 24 * 3600,
    Vertical.VIDEOS: 6 * 3600,
    Vertical.SHOPPING: 300,
    Vertical.SCHOLAR: 24 * 3600,
    Vertical.PATENTS: 24 * 3600,
    Vertical.AUTOCOMPLETE: 3600,
}

# Credits deducted per successful query.
DEFAULT_CREDIT_COST: dict[Vertical, int] = {
    **{v: 1 for v in Vertical},
    Vertical.PLACES: 2,
    Vertical.SCHOLAR: 2,
    Vertical.PATENTS: 2,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    log_level: str = "INFO"

    http_timeout: float = 10.0

    # Per-key token bucket; qps <= 0 disables rate limiting.
    rate_limit_qps: float = 5.0
    rate_limit_burst: int = 10

    # Provider resilience: retries with exponential backoff + circuit breaker.
    provider_max_retries: int = 2
    provider_backoff_seconds: float = 0.5
    breaker_failure_threshold: int = 5
    breaker_recovery_seconds: float = 30.0

    # Result-quality scoring (searchMeta). score = wc*coverage + ws*sufficiency
    # - wd*duplicateRate, clamped to [0, 1]; degraded when below the threshold.
    quality_weight_coverage: float = 0.5
    quality_weight_sufficiency: float = 0.5
    quality_weight_duplicates: float = 0.3
    quality_degraded_threshold: float = 0.5
    # Engine coverage below this floor marks the response degraded even when
    # plenty of results came back (sufficiency must not mask engine collapse).
    # 0 disables the floor.
    quality_coverage_floor: float = 0.4

    # Quality fall-through: when a genxng response is degraded and the
    # commercial provider is configured, serve the commercial result instead.
    quality_fallthrough: bool = True

    # Rewrite natural-language questions into keyword queries before genxng
    # (metasearch ranks keywords far better than questions). Disable when
    # consumers reformulate queries themselves.
    query_rewrite: bool = True

    # Outbound pacing toward the genxng backend (the egress-facing traffic):
    # consumer spikes queue and smooth instead of bursting through the proxy.
    # Set from the measured ceiling (scripts/ceiling_probe.py) with headroom;
    # qps <= 0 disables pacing.
    outbound_qps: float = 2.0
    outbound_burst: int = 5
    outbound_jitter_seconds: float = 0.3
    outbound_max_wait_seconds: float = 20.0

    # Engine health probes: interval seconds (0 disables the background task)
    # and consecutive failures before an engine is quarantined. Each round
    # probes every enabled engine once — keep the interval generous on a
    # single shared egress (probes are outbound traffic too).
    engine_probe_interval: float = 900.0
    engine_fail_threshold: int = 3

    # Log hygiene: query text is never logged unless explicitly enabled;
    # request logs carry a short hash for correlation instead.
    log_queries: bool = False

    # Egress classification gate: deny-list of sensitive-query patterns.
    # Matching queries fail closed with 403 before any egress or caching.
    policy_path: str = "policy/sensitive.yml"

    redis_url: str = "redis://redis:6379/0"
    database_url: str = "postgresql://gen:gen@postgres:5432/gen"
    searxng_url: str = "http://searxng:8080"

    # Commercial Gen-compatible search API (Phase 3). The key comes from
    # secrets (COMMERCIAL_API_KEY via Vault or env), never from here.
    commercial_base_url: str = ""
    # When true, /search responses served by a non-commercial provider are
    # enriched with knowledgeGraph/peopleAlsoAsk from the commercial provider.
    search_enrichment: bool = False

    # Vault is optional; app/secrets.py falls back to env vars when unset.
    vault_addr: str | None = None
    vault_token: str | None = None
    vault_mount: str = "secret"
    vault_path: str = "gen-api"

    # Override per-vertical routing via env, e.g.
    # PROVIDER_MAP='{"search": "commercial"}' merges over the defaults.
    provider_map: dict[Vertical, ProviderName] = {}
    cache_ttl: dict[Vertical, int] = {}
    credit_cost: dict[Vertical, int] = {}

    direct_scrape_enabled: bool = False

    @property
    def redis_disabled(self) -> bool:
        """Whether this deployment runs without Redis at all.

        Only the explicit REDIS_URL=memory:// selects the in-process cache and
        rate limiter -- the zero-container mode. An empty URL stays an error,
        as it always was: silence must not switch modes. Single worker process
        only; the Redis backing is what makes those safe across several.
        """
        return self.redis_url.strip() == "memory://"

    def provider_for(self, vertical: Vertical) -> ProviderName:
        return self.provider_map.get(vertical, DEFAULT_PROVIDER_MAP[vertical])

    def ttl_for(self, vertical: Vertical) -> int:
        return self.cache_ttl.get(vertical, DEFAULT_CACHE_TTL[vertical])

    def credits_for(self, vertical: Vertical) -> int:
        return self.credit_cost.get(vertical, DEFAULT_CREDIT_COST[vertical])


@lru_cache
def get_settings() -> Settings:
    return Settings()
