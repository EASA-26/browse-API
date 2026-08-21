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

# Where this run can be read afterwards. Started by the scheduler, nobody is
# watching the console, and a task that fails on line one looks exactly like a
# task that ran perfectly -- State goes back to Ready either way.
$log = Join-Path $root 'browse-api.log'
$stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
$keyState = if ($env:COMMERCIAL_API_KEY) {
    'commercial key loaded (' + $env:COMMERCIAL_API_KEY.Length + ' characters)'
} else {
    'NO COMMERCIAL KEY -- the fall-through cannot fire'
}
Add-Content -Path $log -Value ''
Add-Content -Path $log -Value "==== $stamp starting: $keyState ===="

Set-Location $root

# Continue, not Stop, from here on. uvicorn writes its ordinary log lines to
# stderr, and PowerShell turns a native command's stderr into error records:
# under Stop the first log line the server writes would kill the server.
$ErrorActionPreference = 'Continue'

& (Join-Path $root '.venv\Scripts\python.exe') -m uvicorn app.main:app --host 127.0.0.1 --port 8010 2>&1 |
    Tee-Object -FilePath $log -Append
