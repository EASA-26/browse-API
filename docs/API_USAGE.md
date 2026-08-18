# gen-api — Step-by-Step Usage Guide

_Last updated: 2026-08-14_

How to call the gen-api web-search service, from zero to production use.
Audience: developers integrating RAG pipelines, agents, or any internal
consumer. The [complete manual](MANUAL.md) covers everything (architecture,
configuration, providers, operations); the [RUNBOOK](RUNBOOK.md) covers
running it in production (dashboards, alert thresholds, incident response).

---

## Step 1 — Make sure the service is running

From the project folder on the host machine:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

Then confirm it's healthy (no auth needed for health endpoints):

```bash
curl http://localhost:8000/health/deps
```

```json
{"status": "ok", "redis": "ok", "postgres": "ok", "genxng": "ok"}
```

Interactive OpenAPI docs are at <http://localhost:8000/docs> — every endpoint
in this guide can be tried from the browser there.

## Step 2 — Get an API key

Every search request needs an API key. Ask the service operator for one, or
create one yourself on the host:

```bash
docker compose exec api python -m app.cli create-key --name my-service --credits 10000
```

The command prints the key once — store it in your service's secret store
(not in code, not in git). `--credits` sets the starting balance
(default 10000).

## Step 3 — Make your first request

All search endpoints are `POST` with a JSON body, authenticated with the
`X-API-KEY` header. `q` is the only required field.

**curl**

```bash
curl -X POST http://localhost:8000/search \
  -H "X-API-KEY: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"q": "attention is all you need"}'
```

**PowerShell**

```powershell
Invoke-RestMethod http://localhost:8000/search -Method Post `
  -ContentType "application/json" -Headers @{"X-API-KEY"="YOUR_KEY"} `
  -Body '{"q": "attention is all you need"}'
```

**Python**

```python
import httpx

response = httpx.post(
    "http://localhost:8000/search",
    headers={"X-API-KEY": "YOUR_KEY"},
    json={"q": "attention is all you need"},
    timeout=15,
)
response.raise_for_status()
results = response.json()
```

A successful response looks like:

```json
{
  "searchParameters": {
    "q": "attention is all you need",
    "gl": "us", "hl": "en", "num": 10, "page": 1,
    "type": "search"
  },
  "credits": 1,
  "organic": [
    {
      "title": "[1706.03762] Attention Is All You Need - arXiv",
      "link": "https://arxiv.org/abs/1706.03762",
      "snippet": "The dominant sequence transduction models are based on...",
      "position": 1
    }
  ],
  "relatedSearches": [{"query": "transformer architecture"}]
}
```

Every response echoes your request in `searchParameters` and reports the
`credits` deducted. Blocks a provider cannot fill are **omitted entirely**
(never `null`, never empty placeholders).

## Step 4 — Tune the request parameters

All endpoints accept the same body fields:

| Field      | Type   | Default | Meaning                                          |
|------------|--------|---------|--------------------------------------------------|
| `q`        | string | —       | Search query. **Required.**                      |
| `gl`       | string | `"us"`  | Country code, ISO-3166 alpha-2 (e.g. `"my"`)     |
| `hl`       | string | `"en"`  | UI language (e.g. `"ms"`)                        |
| `location` | string | `null`  | Free-text location bias (`"Kuala Lumpur, Malaysia"`) |
| `num`      | int    | `10`    | Results per page, 1–100                          |
| `page`     | int    | `1`     | Page number                                      |
| `tbs`      | string | `null`  | Time filter: `qdr:d` / `qdr:w` / `qdr:m` / `qdr:y` (day/week/month/year) |
| `debug`    | bool   | `false` | Adds `providersUsed` provenance to the response  |

Unknown fields are rejected with `422` — typos fail loudly instead of being
silently ignored.

Example — last week's news, Malaysian locale, 5 results:

```json
{"q": "artificial intelligence", "gl": "my", "hl": "en", "num": 5, "tbs": "qdr:w"}
```

## Step 5 — Pick the right endpoint

| Endpoint        | Result block     | Item fields (main)                                            | Credits |
|-----------------|------------------|---------------------------------------------------------------|---------|
| `POST /search`  | `organic[]` (+ `knowledgeGraph`, `peopleAlsoAsk[]`, `relatedSearches[]`) | title, link, snippet, date, sitelinks, position | 1 |
| `POST /images`  | `images[]`       | title, imageUrl, thumbnailUrl, link, domain, source, imageWidth, imageHeight, position | 1 |
| `POST /news`    | `news[]`         | title, link, snippet, date, source, imageUrl, position        | 1 |
| `POST /videos`  | `videos[]`       | title, link, snippet, imageUrl, duration, channel, source, date, position | 1 |
| `POST /autocomplete` | `suggestions[]` | value                                                     | 1 |
| `POST /places`  | `places[]`       | title, address, latitude, longitude, rating, ratingCount, types, website, phoneNumber, placeId, position | 2 |
| `POST /shopping`| `shopping[]`     | title, source, link, price, delivery, imageUrl, rating, productId, position | 1 |
| `POST /scholar` | `organic[]`      | title, link, publicationInfo, snippet, year, citedBy          | 2 |
| `POST /patents` | `organic[]`      | title, snippet, link, assignee, inventor, priorityDate, filingDate, grantDate, publicationNumber, figures | 2 |

Credit costs are deployment-configurable; the values above are the defaults.

**Availability note:** `/search`, `/images`, `/news`, `/videos`,
`/autocomplete` are always live (served by GenXNG, the on-prem metasearch
backend). `/places`,
`/shopping`, `/scholar`, `/patents` require the commercial provider to be
configured; until then they return `501` with an explanatory message — treat
`501` as "this vertical is not enabled in this deployment".

## Step 6 — Handle the error codes

| Status | Meaning                              | What your client should do                        |
|--------|--------------------------------------|---------------------------------------------------|
| `401`  | Missing or invalid `X-API-KEY`       | Fix credentials; do not retry                     |
| `402`  | Credit balance can't cover the query | Stop; ask the operator to top up the key          |
| `403`  | Query blocked by the sensitivity policy (`policyBlocked: true` + `category`) | Do not retry or rephrase-and-retry automatically; answer from internal knowledge only. The query never left the service. |
| `422`  | Invalid body (missing `q`, unknown field, `num` out of range) | Fix the request; do not retry |
| `429`  | Rate limit exceeded                  | Wait `Retry-After` seconds (header), then retry   |
| `501`  | Vertical not enabled / not supported by its provider | Don't retry; feature-flag this vertical off |
| `502`  | Upstream provider failed (after internal retries), circuit breaker open, or the outbound egress queue is saturated | Retry later with backoff; the service already retried and is protecting the shared egress |

Error bodies carry `{"detail": "<human-readable reason>"}`; the policy `403`
additionally carries machine-readable fields:

```json
{"detail": "Insufficient credits: balance 0, cost 1"}
```

```json
{"detail": "query blocked by policy", "policyBlocked": true, "category": "grid-operations"}
```

Note on retries: the service itself retries transient provider failures
(2 attempts with backoff) and fails fast when a provider's circuit breaker is
open — so keep your own retry policy modest (e.g. one retry after 30–60s on
`502`).

## Step 7 — Understand credits and caching

- Each **successful** query deducts credits from your key (see table above).
  Failed requests (`4xx`/`5xx`) are never charged.
- Responses are cached (per full parameter set: query, locale, page, etc.)
  with per-vertical TTLs — roughly 6h for search/images/videos, 5min for
  news/shopping, 24h for places/scholar/patents, 1h for autocomplete.
  Cached responses return in ~10–70 ms and still deduct credits.
- Identical repeated queries are therefore cheap on latency and provider
  load — design your pipeline to reuse stable query strings where possible.

## Step 7b — Read the quality signal (`searchMeta`)

Responses served by the GenXNG backend carry a machine-readable quality
signal. A degraded search still returns HTTP 200 — **check `degraded`, don't
trust the status code**:

```json
"searchMeta": {
  "enginesQueried": 8,      // engines asked
  "enginesResponded": 2,    // engines that actually answered
  "engineCoverage": 0.25,
  "resultCount": 6,
  "duplicateRate": 0.0,
  "qualityScore": 0.41,     // weighted 0..1 (coverage + sufficiency - dupes)
  "cached": false,          // true when served from the response cache
  "degraded": true,         // low score OR engine coverage below the floor
  "rewrittenQuery": "ceo TNB 2017"  // present when your NL question was
}                           //  rewritten into keywords for the backend
```

Tip: keyword queries retrieve better than full questions. The API rewrites
obvious questions automatically (see `rewrittenQuery`), but a RAG pipeline
gets the best results by having its LLM reformulate the user's question
into 1–2 keyword queries before calling `/search`.

RAG pipelines should treat `degraded: true` as "consider a fallback source or
tell the user context is thin". The block is absent on commercial-served
verticals and `/autocomplete`. When the deployment has a commercial provider
configured, degraded responses are usually upgraded automatically before you
see them (in-API quality fall-through) — a response with `searchMeta` absent
and `providersUsed.organic: "commercial"` under `debug` means exactly that.
Score weights and the degraded threshold are deployment-configurable
(`QUALITY_*` env vars); the score distribution and degraded-response rate
are visible in `/metrics`.

To see where each block came from, send `"debug": true`:

```json
"providersUsed": {"organic": "genxng", "knowledgeGraph": "commercial"}
```

(`"cache"` means the whole response was served from cache.)

## Step 8 — Respect the rate limit

Each key has a token bucket (default: 5 requests/second, burst 10 — deployment
configurable). Exceeding it returns `429` with a `Retry-After` header in
seconds. Honor it:

```python
import time

def search_with_backoff(client: httpx.Client, body: dict) -> dict:
    while True:
        response = client.post("/search", json=body)
        if response.status_code != 429:
            response.raise_for_status()
            return response.json()
        time.sleep(int(response.headers.get("Retry-After", "1")))
```

## Step 9 — Use the ready-made RAG client

[`examples/rag_client.py`](../examples/rag_client.py) wraps steps 3–8 for the
common RAG case. Copy the module into your codebase (only dependency: httpx):

```python
from rag_client import GenSearchClient

client = GenSearchClient("http://localhost:8000", api_key="YOUR_KEY")

# ready-to-embed context strings: "title — snippet (url)"
context = client.snippets("attention is all you need", num=5)

# or the raw response / other verticals
raw = client.search("transformer architecture", gl="my", num=10)
headlines = client.news("artificial intelligence", num=5)

client.close()
```

## Step 10 — Monitor your usage

- **Your balance**: on the host, check remaining credits:
  ```bash
  docker compose exec postgres psql -U gen -d gen -c "SELECT name, credits FROM api_keys;"
  ```
- **Your query history**: every served request is recorded in the `usage_log`
  table (vertical, provider, cached, credits, latency).
- **Service-wide metrics**: Prometheus at `http://localhost:8000/metrics` —
  request counts by outcome, cache hit rate, provider latency p50/p95,
  `qualityScore` distribution, degraded/fall-through/policy-block counters,
  healthy-engine gauges, and outbound-queue wait times. The
  [RUNBOOK](RUNBOOK.md) turns these into dashboard queries and alert
  thresholds.
- **Privacy**: your query text is never written to logs or the usage table —
  request logs carry only a short hash. Queries matching the sensitivity
  policy are refused before any egress (see the `403` row above).

---

## Quick reference card

```text
Base URL     http://localhost:8000          (or your deployment host)
Auth         X-API-KEY: <key>               (all POST endpoints)
Body         {"q": "...", ...}              (q required, JSON)
Endpoints    /search /images /news /videos /autocomplete   (live)
             /places /shopping /scholar /patents           (need commercial key)
Success      200 + {searchParameters, credits, <blocks>, searchMeta?}
             -> check searchMeta.degraded, not just the status code
Failures     401 auth · 402 credits · 403 policy block (never auto-retry)
             422 bad body · 429 slow down (Retry-After)
             501 vertical off · 502 upstream/egress down (retry later)
Docs/health  /docs · /healthz · /health/deps · /metrics    (no auth)
```
