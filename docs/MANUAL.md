# gen-api — Complete Manual

_Last updated: 2026-08-14_

The reference manual for **gen-api**: a self-hosted, Gen-wire-format web-search
API that backs internal RAG and AI-agent systems (FazuraGPT) from behind a
single corporate egress. This document covers everything; two companion
documents go deeper on their audiences:

- [API_USAGE.md](API_USAGE.md) — step-by-step guide for API consumers
- [RUNBOOK.md](RUNBOOK.md) — operations: dashboards, alerts, incident response

---

## Table of contents

1. [Introduction](#1-introduction)
2. [Architecture](#2-architecture)
3. [Installation and deployment](#3-installation-and-deployment)
4. [Configuration reference](#4-configuration-reference)
5. [API reference](#5-api-reference)
6. [Providers and resilience](#6-providers-and-resilience)
7. [Result quality system](#7-result-quality-system)
8. [Governance and security](#8-governance-and-security)
9. [Operations](#9-operations)
10. [Development guide](#10-development-guide)
11. [Troubleshooting](#11-troubleshooting)
12. [Appendix: schemas and defaults](#12-appendix-schemas-and-defaults)

---

## 1. Introduction

gen-api provides Google-SERP-style search results over a clean internal REST
API, without a per-query commercial vendor dependency. Its design principles:

- **Pluggable acquisition.** The API, normalizer, cache, quota, and quality
  layers are what we own and maintain; where results come *from* is swappable
  per vertical via config. Three backends implement one provider interface:
  GenXNG (self-hosted metasearch, the default), a commercial search API
  adapter (dormant until keyed), and a direct-scrape stub (disabled).
- **One canonical schema.** Every provider normalizes into the same
  Serper-compatible response shape, so consumers never care which backend
  served them and swapping providers is configuration, not a rewrite.
- **Tell the truth about quality.** A degraded search still returns HTTP 200;
  every GenXNG-served response therefore carries a machine-readable
  `searchMeta` quality signal, and the API can fall through to the
  commercial provider automatically when quality drops.
- **Fail closed on sensitive queries.** A classification gate refuses
  policy-matched queries before any egress, on every provider path.
- **Protect the shared egress.** Response caching, outbound pacing with
  jitter, engine quarantine, and measured (not guessed) rate ceilings keep
  the service under the block threshold of the corporate proxy IP.

## 2. Architecture

```
                      ┌─────────────────────────────┐
 RAG / agents / bots  │    Internal consumers        │
                      └──────────────┬──────────────┘
                                     │ HTTP + X-API-KEY
                      ┌──────────────▼──────────────┐
                      │  API layer (FastAPI)         │  /search /images /news ...
                      │  auth · policy gate · rate   │  /healthz /metrics /docs
                      │  limit · quota · usage log   │
                      └──────────────┬──────────────┘
                      ┌──────────────▼──────────────┐
                      │  Quality layer               │  searchMeta scoring,
                      │  degraded detection,         │  in-API fall-through
                      └──────────────┬──────────────┘
                      ┌──────────────▼──────────────┐
                      │  Cache (Redis, per-vertical  │
                      │  TTL) + outbound pacing      │
                      └──────────────┬──────────────┘
                      ┌──────────────▼──────────────┐
                      │  Provider interface (ABC)    │  retries · circuit breaker
                      ├─────────┬─────────┬─────────┤
                      │ GenXNG  │ Commer- │ Direct  │
                      │ (SearXNG│ cial    │ scrape  │
                      │ on-prem)│ adapter │ (stub)  │
                      └─────────┴─────────┴─────────┘
```

**Request pipeline** (every vertical, in order): authenticate key against
Postgres → classification gate (fail closed) → per-key rate limit (token
bucket in Redis) → quota check → cache lookup → provider call (paced,
retried, breaker-protected) → quality scoring → optional fall-through /
enrichment → cache store → credit deduction (success only) → usage log →
response.

**Services** (docker compose): `api` (FastAPI/uvicorn), `redis` (cache, rate
limits, pacing state), `postgres` (API keys, credit balances, usage log),
`searxng` (the GenXNG metasearch backend, rebranded UI on host port 8081).

## 3. Installation and deployment

### Prerequisites

- Docker with Compose v2 (Docker Desktop on Windows; WSL2 backend)
- ~2 GB RAM headroom for the four containers
- Outbound internet from the Docker host (via the corporate proxy in
  production)

### Quickstart (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

The launcher creates `.env` from `.env.example` on first run (generating a
random `SEARXNG_SECRET`), builds and starts the stack, waits for
`/health/deps` to go green, and creates a first API key if none exists.
`.\run.ps1 -Down` stops (keeps data); `.\run.ps1 -Reset` also wipes volumes.

### Manual start (any platform)

```bash
cp .env.example .env          # edit SEARXNG_SECRET at minimum
docker compose up --build -d
curl http://localhost:8000/health/deps
docker compose exec api python -m app.cli create-key --name my-service
```

### Zero-container mode (Windows-native)

For hosts that cannot run containers at all (for example a VM without nested
virtualization). The API runs as a plain Python process; SQLite replaces
Postgres and an in-process cache and rate limiter replace Redis:

```powershell
py -3.12 -m venv .venv; .\.venv\Scripts\pip install -e .
$env:DATABASE_URL = "sqlite:///C:/gen-api/data/gen.db"
$env:REDIS_URL = "memory://"
$env:SEARXNG_URL = "http://localhost:8080"   # SearXNG runs natively or on another host
.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
.\.venv\Scripts\python -m app.cli create-key --name my-service
```

Constraints, honestly stated: one worker process only (the in-process cache
and limiter are not shared across workers -- the Redis backing is what makes
them safe across several); SearXNG is not bundled by this mode and must be
provided natively or from another host; `/health/deps` reports
`redis: "disabled"` here, which is healthy. The Docker path is unchanged and
remains the recommended deployment wherever containers are possible.

### Verify

| URL | Expect |
|---|---|
| `http://localhost:8000/healthz` | `{"status": "ok"}` |
| `http://localhost:8000/health/deps` | all of redis/postgres/genxng `ok` |
| `http://localhost:8000/docs` | interactive OpenAPI UI |
| `http://localhost:8081` | GenXNG web UI (debug only) |

### Production notes

- **Throughput**: the image runs a single uvicorn worker; add
  `--workers N` to the Dockerfile CMD when one core saturates. The per-key
  rate limiter and cache are Redis-backed and safe across workers; outbound
  pacing and engine quarantine are per-process (divide `OUTBOUND_QPS`
  accordingly, or keep one worker).
- **Secrets**: set `VAULT_ADDR`/`VAULT_TOKEN` to resolve `COMMERCIAL_API_KEY`
  and friends from Vault KV-v2; env vars remain the dev fallback. Never
  commit `.env`.
- **Proxy**: containers inherit proxy settings from Docker daemon config;
  the probe scripts honor `HTTPS_PROXY` directly.
- **Before scaling past pilot**: run `scripts/ceiling_probe.py` on the real
  egress and set `OUTBOUND_QPS` from its measurement; run
  `scripts/engine_probe.py --apply` so the engine set matches proxy reality.

## 4. Configuration reference

All settings come from environment variables / `.env`, parsed by
`app/config.py` (pydantic-settings). Restart the api container to apply.

### Core

| Variable | Default | Meaning |
|---|---|---|
| `APP_ENV` | `dev` | Environment label (informational) |
| `LOG_LEVEL` | `INFO` | Root log level |
| `HTTP_TIMEOUT` | `10.0` | Seconds, all upstream HTTP calls |
| `REDIS_URL` | `redis://redis:6379/0` | Cache + rate limit (`memory://` selects the in-process zero-container mode) |
| `DATABASE_URL` | `postgresql://gen:gen@postgres:5432/gen` | Keys, credits, usage |
| `SEARXNG_URL` | `http://searxng:8080` | GenXNG backend (in-network) |

### Provider routing

| Variable | Default | Meaning |
|---|---|---|
| `PROVIDER_MAP` | see below | JSON map vertical→provider, merged over defaults, e.g. `{"search": "commercial"}` |
| `COMMERCIAL_BASE_URL` | *(empty)* | Gen-wire-compatible commercial endpoint; empty keeps commercial dormant |
| `COMMERCIAL_API_KEY` | — | Via env or Vault (secret, not a setting) |
| `SEARCH_ENRICHMENT` | `false` | Graft commercial knowledgeGraph/peopleAlsoAsk onto GenXNG `/search` |
| `DIRECT_SCRAPE_ENABLED` | `false` | Direct-scrape stub gate (leave off) |

Default routing: GenXNG serves search/images/news/videos/autocomplete;
commercial serves places/shopping/scholar/patents (501 until configured).

### Quotas, limits, cache

| Variable | Default | Meaning |
|---|---|---|
| `RATE_LIMIT_QPS` / `RATE_LIMIT_BURST` | `5` / `10` | Per-key token bucket; `429` + `Retry-After`; `0` disables |
| `CREDIT_COST` | `{}` | JSON override per vertical (defaults: 1; places/scholar/patents 2) |
| `CACHE_TTL` | `{}` | JSON override per vertical, seconds (defaults: search/images/videos 6h, news/shopping 5min, places/scholar/patents 24h, autocomplete 1h) |

### Resilience and egress protection

| Variable | Default | Meaning |
|---|---|---|
| `PROVIDER_MAX_RETRIES` | `2` | Retries per provider call (transient errors only) |
| `PROVIDER_BACKOFF_SECONDS` | `0.5` | Exponential backoff base (0.5s, 1s) |
| `BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive failures to open the circuit |
| `BREAKER_RECOVERY_SECONDS` | `30` | Half-open trial delay |
| `OUTBOUND_QPS` | `2` | Egress pacing toward GenXNG — set from the measured ceiling; `0` disables |
| `OUTBOUND_BURST` | `5` | Burst allowance before smoothing |
| `OUTBOUND_JITTER_SECONDS` | `0.3` | Random spread added to each slot |
| `OUTBOUND_MAX_WAIT_SECONDS` | `20` | Queue depth cap; deeper fails fast (502) |
| `ENGINE_PROBE_INTERVAL` | `900` | Seconds between engine-health rounds; `0` disables |
| `ENGINE_FAIL_THRESHOLD` | `3` | Consecutive probe failures before quarantine |

### Quality

| Variable | Default | Meaning |
|---|---|---|
| `QUALITY_WEIGHT_COVERAGE` | `0.5` | Weight of engineCoverage in the score |
| `QUALITY_WEIGHT_SUFFICIENCY` | `0.5` | Weight of resultCount/expected |
| `QUALITY_WEIGHT_DUPLICATES` | `0.3` | Penalty weight of duplicateRate |
| `QUALITY_DEGRADED_THRESHOLD` | `0.5` | Score below this sets `degraded: true` |
| `QUALITY_COVERAGE_FLOOR` | `0.4` | Coverage below this is degraded regardless of score (sufficiency must not mask engine collapse); `0` disables |
| `QUALITY_FALLTHROUGH` | `true` | Degraded GenXNG responses retry via commercial |
| `QUERY_REWRITE` | `true` | Rewrite NL questions into keyword queries for GenXNG (`searchMeta.rewrittenQuery` reports what was sent) |
| `RERANK` | `true` | Re-rank GenXNG search/news results by IDF-weighted query relevance before slicing to `num` — floats entity-specific answers above generically popular pages |
| `COMMERCIAL_FORMAT` | `serper` | Commercial wire format: `serper` (Gen-compatible POST) or `serpapi` (serpapi.com GET; serves /search + /news, key only, no BASE_URL needed) |

### Governance and logging

| Variable | Default | Meaning |
|---|---|---|
| `POLICY_PATH` | `policy/sensitive.yml` | Deny-list file for the classification gate |
| `LOG_QUERIES` | `false` | Raw query text in logs (default: hash only) |
| `VAULT_ADDR` / `VAULT_TOKEN` | *(unset)* | Enable Vault KV-v2 secret resolution |
| `VAULT_MOUNT` / `VAULT_PATH` | `secret` / `gen-api` | KV location |

### Compose-level

`POSTGRES_USER/PASSWORD/DB` (container bootstrap, default `gen`),
`SEARXNG_SECRET` (required, random string).

## 5. API reference

### Conventions

- All search endpoints: `POST`, JSON body, `X-API-KEY` header.
- Wire format is camelCase and Serper-compatible; code written against that
  format works unmodified.
- Blocks a provider cannot fill are **omitted**, never `null` or faked.
- Unknown body fields are rejected (`422`).

### Common request body

| Field | Type | Default | Notes |
|---|---|---|---|
| `q` | string | — | required, min length 1 |
| `gl` | string | `us` | ISO-3166 country |
| `hl` | string | `en` | language |
| `location` | string | — | free-text bias |
| `num` | int | `10` | 1–100 |
| `page` | int | `1` | ≥1 |
| `tbs` | string | — | `qdr:d`/`qdr:w`/`qdr:m`/`qdr:y` |
| `debug` | bool | `false` | adds `providersUsed` |

### Response envelope

Every response carries `searchParameters` (echo + `type`), `credits`
(deducted for this query), the vertical's result blocks, optionally
`searchMeta` (section 7) and, with `debug: true`, `providersUsed`
(block → `genxng` / `commercial` / `cache`).

### Endpoints and blocks

| Endpoint | Blocks | Item fields | Credits | Served by (default) |
|---|---|---|---|---|
| `/search` | `organic[]`, `knowledgeGraph`, `peopleAlsoAsk[]`, `relatedSearches[]` | title, link, snippet, date, sitelinks[], attributes, position | 1 | GenXNG |
| `/images` | `images[]` | title, imageUrl, thumbnailUrl, source, domain, link, imageWidth, imageHeight, position | 1 | GenXNG |
| `/news` | `news[]` | title, link, snippet, date, source, imageUrl, position | 1 | GenXNG |
| `/videos` | `videos[]` | title, link, snippet, imageUrl, duration, channel, source, date, position | 1 | GenXNG |
| `/autocomplete` | `suggestions[]` | value | 1 | GenXNG |
| `/places` | `places[]` | title, address, latitude, longitude, rating, ratingCount, type, types[], website, phoneNumber, cid, placeId, position | 2 | commercial |
| `/shopping` | `shopping[]` | title, source, link, price, delivery, imageUrl, rating, ratingCount, productId, position | 1 | commercial |
| `/scholar` | `organic[]` | title, link, publicationInfo, snippet, year, citedBy | 2 | commercial |
| `/patents` | `organic[]` | title, snippet, link, assignee, inventor, priorityDate, filingDate, grantDate, publicationNumber, figures[] | 2 | commercial |

Unauthenticated endpoints: `GET /healthz` (liveness), `GET /health/deps`
(readiness: redis/postgres/genxng), `GET /metrics` (Prometheus),
`GET /docs` + `GET /openapi.json`.

### Status codes

| Code | Meaning | Client action |
|---|---|---|
| 200 | Success (check `searchMeta.degraded`) | — |
| 401 | Bad/missing API key | fix credentials |
| 402 | Insufficient credits | top up; don't retry |
| 403 | Policy block: `{policyBlocked: true, category}` | **never auto-retry/rephrase**; answer internally |
| 422 | Invalid body | fix request |
| 429 | Per-key rate limit; `Retry-After` header | wait, retry |
| 501 | Vertical's provider not configured/supported | feature-flag off |
| 502 | Provider failed after retries / breaker open / egress queue saturated | modest retry later |

## 6. Providers and resilience

### GenXNG (default, on-prem)

A SearXNG instance (rebranded GenXNG) queried over its JSON API. Verticals
map to categories (general/images/news/videos); `/autocomplete` uses the
autocompleter endpoint (Google-backed suggestions). Raw engine results are
normalized into canonical blocks; engine attribution feeds the quality
signal. Requests pass the outbound pacer and, when the health monitor has
quarantined engines, explicitly select only healthy ones.

### Commercial adapter

Speaks the same wire format as the canonical schema, so "normalization" is
strict block extraction (per-vertical allow-lists; upstream bookkeeping
stripped; empty blocks dropped). Activated by `COMMERCIAL_BASE_URL` +
`COMMERCIAL_API_KEY`; until then its verticals answer 501 and fall-through /
enrichment quietly skip. 401/403 from upstream raise a non-retryable auth
error. Any Serper-wire-compatible provider is config-only; a different wire
format (e.g. DataForSEO) needs a small subclass.

### Direct scrape (stub)

Documented design (Playwright + rotating proxies, isolated failure domain)
behind `DIRECT_SCRAPE_ENABLED=false`. Requires proxy budget and
ToS/compliance sign-off before implementation; if enabled today it reports
itself unavailable rather than pretending to work.

### Resilience wrapper (all providers)

Every provider instance is wrapped with: timeout (`HTTP_TIMEOUT`), up to
`PROVIDER_MAX_RETRIES` retries with exponential backoff **on transient
errors only** (unsupported-vertical, not-configured, and auth-rejection
never retry), and a per-provider circuit breaker (opens after
`BREAKER_FAILURE_THRESHOLD` consecutive failures; half-open trial after
`BREAKER_RECOVERY_SECONDS`; open circuit fails in milliseconds instead of
hanging consumers).

### Outbound pacing (GenXNG only)

GCRA-style: outbound calls reserve evenly spaced slots at `OUTBOUND_QPS`
with an `OUTBOUND_BURST` allowance and jitter; consumer spikes queue and
smooth rather than bursting through the corporate egress. A queue deeper
than `OUTBOUND_MAX_WAIT_SECONDS` fails fast (502). Set `OUTBOUND_QPS` from
`scripts/ceiling_probe.py` measurements, not guesses.

## 7. Result quality system

### searchMeta

Computed for every GenXNG-served search/images/news/videos response:

```
engineCoverage = enginesResponded / enginesQueried
sufficiency    = min(resultCount / num, 1)
duplicateRate  = 1 - unique(normalized URLs) / total
qualityScore   = Wc*coverage + Ws*sufficiency - Wd*duplicateRate   (clamped 0..1)
degraded       = qualityScore < QUALITY_DEGRADED_THRESHOLD
                 or engineCoverage < QUALITY_COVERAGE_FLOOR
```

The coverage floor exists because sufficiency masks engine collapse: ten
results from a single surviving engine is still a degraded search.

**Query rewriting**: metasearch ranks keyword queries far better than
natural-language questions, so questions are heuristically rewritten before
GenXNG ("who is the ceo of TNB in 2017" → "ceo TNB 2017"; interrogative and
function words dropped, nothing invented). Keyword queries pass verbatim,
the commercial provider always receives the original query, and
`searchMeta.rewrittenQuery` reports what was actually sent. RAG consumers
doing their own LLM-based reformulation (recommended) can set
`QUERY_REWRITE=false`.

**Relevance re-ranking**: the metasearch merge ranks by engine agreement,
which buries specific answers under generically popular pages (in the
CEO-of-TNB incident the answer sat at positions 10–24, below what RAG
consumers read). Before slicing to `num`, search/news results are re-ranked
by IDF-weighted query-term relevance: rare, informative terms ("tnb",
"2017") outweigh generic ones ("ceo"), and title matches outweigh snippet
matches. Deterministic; ties keep engine order; `RERANK=false` disables.

`enginesQueried/Responded` come from the raw engine attribution and
`unresponsive_engines` data. `cached: true` marks cache-served responses.
The block is absent on commercial-served verticals and `/autocomplete`.

### Quality fall-through

With `QUALITY_FALLTHROUGH=true` and commercial configured: a degraded
GenXNG response triggers one commercial attempt for the same query; if the
commercial primary block is non-empty, that result is served (and cached)
instead. Failures and empty results keep the original degraded response —
fall-through can never make a response worse. Outcomes are counted in
`gen_api_fallthrough_total{outcome=served|kept_primary|error}`; the served
rate is both the health signal and the fallback-bill forecast.

### Engine health

A background task (every `ENGINE_PROBE_INTERVAL`s) discovers enabled engines
from GenXNG's `/config` and probes each individually. An engine failing
`ENGINE_FAIL_THRESHOLD` consecutive probes is quarantined — live requests
select only healthy engines for its category — and recovers on the next
good probe. If every engine in a category is down, filtering is skipped
(GenXNG tries its defaults). State is in-process; it re-learns within a few
rounds after a restart.

## 8. Governance and security

### The correct governance model (be precise in briefings)

For allowed queries, the query text **does** leave the network — GenXNG
forwards it to the engines it scrapes. What gen-api avoids versus a
commercial SERP vendor is a third-party SaaS account that ties queries to a
corporate identity and retains them: *no vendor retention*, *not* "no
egress". Genuinely sensitive queries must not reach any external search —
which is what the classification gate enforces.

### Classification gate (fail closed)

`policy/sensitive.yml` — a versioned, regex-based deny-list grouped by
category (confidential markers incl. `sulit`/`terhad`, grid operations,
security, procurement, named-asset incidents). Matching queries are refused
with `403 {policyBlocked: true, category}` **before** the rate limiter,
quota, cache, and any provider construction: nothing egresses on any path,
nothing is cached or charged, and only the category + query hash are logged.
The seed patterns need governance review; note the limitation honestly —
regex catches the obvious and the accidental, not paraphrases (semantic
classification would be a separate on-prem component).

### Log hygiene

Query text never reaches logs or the database by default: HTTP client
loggers are pinned to WARNING (their INFO lines contain full URLs), request
logs carry a 16-char SHA-256 prefix for correlation, `usage_log` stores no
query text, and `LOG_QUERIES=true` is an explicit opt-in. Logs are safe to
hand to ICT.

### Authentication and secrets

`X-API-KEY` validated against Postgres (`api_keys.active`); keys are
32-byte urlsafe tokens printed once at creation. Secrets resolve
Vault-first (KV-v2) with env fallback; no secrets in code or git (`.env` is
gitignored; `.env.example` carries placeholders only).

## 9. Operations

### Health and observability

- `GET /health/deps` — readiness of redis/postgres/genxng.
- Structured JSON logs, one `request served` line per request (api_key,
  vertical, provider, cached, latency_ms, credits, query_hash,
  quality_score, degraded).
- `GET /metrics` — full catalog:

| Metric | Labels | Meaning |
|---|---|---|
| `gen_api_requests_total` | vertical, status | requests by outcome (success, rate_limited, quota_exceeded, policy_blocked, provider_error, unsupported, not_configured) |
| `gen_api_request_latency_seconds` | vertical | end-to-end latency histogram |
| `gen_api_provider_requests_total` | provider, vertical, outcome | upstream calls |
| `gen_api_provider_latency_seconds` | provider, vertical | upstream latency |
| `gen_api_provider_retries_total` | provider | transient-failure retries |
| `gen_api_cache_requests_total` | vertical, result | hit/miss |
| `gen_api_quality_score` | vertical | qualityScore histogram |
| `gen_api_degraded_responses_total` | vertical | degraded 200s |
| `gen_api_fallthrough_total` | vertical, outcome | quality fall-through |
| `gen_api_policy_blocked_total` | category | gate refusals |
| `gen_api_healthy_engines` | category | gauge, current healthy count |
| `gen_api_engine_probes_total` | engine, outcome | probe results |
| `gen_api_outbound_wait_seconds` | — | egress pacing queue waits |

Dashboards, PromQL, and alert thresholds: [RUNBOOK.md](RUNBOOK.md).

### Routine tasks

| Task | Command |
|---|---|
| Create API key | `docker compose exec api python -m app.cli create-key --name X [--credits N]` |
| Check balances | `docker compose exec postgres psql -U gen -d gen -c "SELECT name, credits FROM api_keys;"` |
| Usage audit | see RUNBOOK §usage audit |
| Weekly egress probes | `scripts/ceiling_probe.py` (off-peak!) then `scripts/engine_probe.py --apply` |
| Load test | `python scripts/load_test.py --api-key KEY --requests 200 --concurrency 20` |
| Policy update | edit `policy/sensitive.yml`, commit, rebuild api |

## 10. Development guide

### Repository layout

```
app/
  main.py            app + lifespan (DB pool, redis, engine-probe task)
  config.py          every setting (pydantic-settings); routing/TTL/credit maps
  auth.py            X-API-KEY dependency
  policy.py          classification gate
  quality.py         searchMeta scoring
  cache.py           Redis response cache
  ratelimit.py       per-key token bucket (Redis Lua)
  pacing.py          outbound GCRA pacer
  engine_health.py   probe loop + quarantine
  db.py              api_keys + usage_log, atomic deductions
  metrics.py         Prometheus registry
  logging_config.py  JSON formatter, log hygiene
  secrets.py         Vault-first secret resolution
  cli.py             admin commands
  schemas/           canonical response models (one module per vertical)
  providers/         base ABC · searxng · commercial · direct_scrape ·
                     resilience wrapper · registry
  api/               health · metrics · deps · verticals (shared pipeline)
tests/               136 tests; recorded fixtures in tests/fixtures/
scripts/             load_test · ceiling_probe · engine_probe
examples/            rag_client.py
policy/              sensitive.yml (versioned deny-list)
searxng/             settings.yml + GenXNG branding overrides
docs/                MANUAL (this file) · API_USAGE · RUNBOOK
```

### Local development

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows path shown
pip install -e .[dev]
pytest            # never touches the network
ruff check .
mypy              # strict mode, whole tree incl. scripts/tests
```

The quality bar for every change: tests + ruff + mypy strict clean.
Unit tests use recorded fixtures (`tests/fixtures/`), `fakeredis[lua]`, and
`httpx.MockTransport` — re-record fixtures against a live stack when
upstream shapes change.

### Adding a provider

Implement `SearchProvider.search(request, vertical) -> blocks dict` in
`app/providers/`, normalize into the canonical schema (omit what you can't
fill), register it in `app/providers/__init__.py` (the registry wraps it
with retries + breaker automatically), add a `ProviderName` value, and route
verticals to it via `PROVIDER_MAP`. Test with `httpx.MockTransport`.

### Adding a vertical

Add the enum value, a schema module with its response model, entries in the
routing/TTL/credit default maps, the response-model registration in
`app/api/verticals.py`, and provider support (or let it 501).

## 11. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `docker compose up` fails: port 8080 allocated | Another app owns 8080; GenXNG's host port is 8081 in compose — update stale configs |
| `/health/deps` shows `genxng: error` | searxng container down or still starting; `docker compose logs searxng` |
| GenXNG JSON API returns 403 | `formats: [html, json]` missing from `searxng/settings.yml` search section |
| Every `/search` 502s instantly | Circuit breaker open (check logs for "circuit breaker is open"); fix the backend, breaker recovers ≤30s after |
| 502 "outbound pacing saturated" | Sustained load above `OUTBOUND_QPS`; raise it only if the measured ceiling allows, otherwise add cache TTL |
| All responses degraded | Engines blocked at the egress — run `engine_probe`, check `gen_api_healthy_engines`, see RUNBOOK block-spike procedure |
| `/places` etc. return 501 | Expected until `COMMERCIAL_BASE_URL` + `COMMERCIAL_API_KEY` are set |
| Legitimate query gets 403 | Overbroad pattern in `policy/sensitive.yml` — refine the regex, commit, rebuild |
| Load test shows mostly 429 | Per-key `RATE_LIMIT_QPS` doing its job; raise for benchmarks only |
| `git push` hangs on this repo | Stale credential prompt; run `gh auth setup-git` once |
| Old logo/branding in browser at 8081 | Cached image — hard refresh (Ctrl+F5) |

## 12. Appendix: schemas and defaults

### Database

```sql
api_keys (id, key UNIQUE, name, active BOOL, credits INT, created_at)
usage_log (id, api_key_id→api_keys, vertical, credits, provider,
           cached BOOL, latency_ms, created_at)   -- indexed (api_key_id, created_at)
```

Credit deduction is atomic (`UPDATE ... WHERE credits >= cost`) and happens
only after a successful serve; cache hits are charged (a served query is a
served query — the cache saves egress, not billing).

### Cache

Key: `cache:{vertical}:{sha256({vertical,q,gl,hl,location,num,page,tbs})}`.
Value: normalized blocks + serving provider. `debug` does not affect the key.

### Defaults at a glance

| Concern | Default |
|---|---|
| Credits | 1/query; places, scholar, patents 2 |
| Cache TTL | search/images/videos 6h · news/shopping 5min · places/scholar/patents 24h · autocomplete 1h |
| Per-key rate limit | 5 qps, burst 10 |
| Retries / breaker | 2 retries (0.5s, 1s) · open at 5 fails · 30s recovery |
| Outbound pacing | 2 qps, burst 5, 0.3s jitter, 20s max queue |
| Quality | weights 0.5/0.5/0.3 · degraded < 0.5 · fall-through on |
| Engine probes | every 15 min · quarantine at 3 consecutive fails |
