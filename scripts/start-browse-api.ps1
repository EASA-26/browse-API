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
# .Replace, not -replace: the second is a regular expression, and a lone
# backslash is not a valid pattern. There is no pattern to match here.
$env:DATABASE_URL = 'sqlite:///' + $root.Replace('\', '/') + '/data/gen.db'
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

# Where this run can be read afterwards. Started by the scheduler there is no
# console, and a task that dies on line one looks exactly like a task that ran
# perfectly: State returns to Ready either way, which is how this one reported
# success while never having started at all.
#
# Out-File with an explicit encoding rather than Tee-Object: on Windows
# PowerShell 5.1 Tee-Object takes no -Encoding and writes UTF-16, which
# interleaved with the header below produced a log with a null byte between
# every character. A log nobody can read is the thing this file exists to
# prevent.
$log = Join-Path $root 'browse-api.log'
$stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
$keyState = if ($env:COMMERCIAL_API_KEY) {
    'commercial key loaded (' + $env:COMMERCIAL_API_KEY.Length + ' characters)'
} else {
    'NO COMMERCIAL KEY -- the fall-through cannot fire'
}
Add-Content -Path $log -Value '' -Encoding UTF8
Add-Content -Path $log -Value "==== $stamp starting: $keyState ====" -Encoding UTF8

Set-Location $root

# Continue, not Stop, from here on. uvicorn writes its ordinary log lines to
# stderr, and PowerShell turns a native command's stderr into error records:
# under Stop the first log line the server writes would kill the server.
$ErrorActionPreference = 'Continue'

# "$_" flattens each record back to the line the server actually wrote.
# PowerShell wraps a native command's stderr in error records, and uvicorn logs
# to stderr, so without this every single log line arrives buried under four
# lines of "python.exe :" and "At ...start-browse-api.ps1:70 char:1".
& (Join-Path $root '.venv\Scripts\python.exe') -m uvicorn app.main:app --host 127.0.0.1 --port 8010 2>&1 |
    ForEach-Object { "$_" } |
    Out-File -FilePath $log -Append -Encoding utf8
