# Starts browse-API for FazuraGPT in zero-container mode.
#
# Registered as the BrowseAPI scheduled task, and runnable by hand for a
# manual restart. Every setting the process needs lives here rather than in
# whichever shell someone last typed them into: the service came back from a
# restart with no commercial key once, and FazuraGPT carried on answering
# from a degraded search without anybody being told.

$ErrorActionPreference = 'Stop'

# The repository root, whatever it is called and wherever it sits.
$root = Split-Path -Parent $PSScriptRoot
$fazura = 'D:\EASA\Fazura\FazuraGPT'

# Zero-container mode: SQLite instead of Postgres, an in-process cache and
# rate limiter instead of Redis. This machine is a VMware guest with
# virtualization disabled, so no container can run on it.
$env:DATABASE_URL = 'sqlite:///' + ($root -replace '\', '/') + '/data/gen.db'
$env:REDIS_URL = 'memory://'
$env:SEARXNG_URL = 'http://127.0.0.1:8082'

# The commercial fall-through, which is what answers a question the metasearch
# could not: this network blocks four of SearXNG's six engines, so a coverage
# floor of two engines marks most answers degraded, and degraded is what makes
# this fire.
$env:COMMERCIAL_FORMAT = 'serpapi'

# The key lives once, in FazuraGPT's .env.local, and is read from there rather
# than copied into a second file that could drift out of step with it.
$envFile = Join-Path $fazura '.env.local'
if (Test-Path $envFile) {
    $line = Get-Content $envFile | Where-Object { $_ -match '^SERPAPI_KEY=' } | Select-Object -First 1
    if ($line) {
        $env:COMMERCIAL_API_KEY = ($line -replace '^SERPAPI_KEY=', '').Trim().Trim('"')
    }
}
if (-not $env:COMMERCIAL_API_KEY) {
    Write-Warning "No SERPAPI_KEY in $envFile -- the quality fall-through cannot fire."
}

Set-Location $root
& (Join-Path $root '.venv\Scripts\python.exe') -m uvicorn app.main:app --host 127.0.0.1 --port 8010
