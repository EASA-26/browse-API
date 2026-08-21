# Starts the SearXNG metasearch that browse-API queries on 127.0.0.1:8082.
#
# Registered as the SearXNG scheduled task. Without it, a reboot brings
# browse-API back to find nothing to search with -- and because the commercial
# fall-through works now, every question would quietly be answered by SerpApi
# instead of failing visibly. Silent and billable is worse than broken.

$ErrorActionPreference = 'Stop'

$searxRoot = 'D:\EASA\searxng'
$browseApi = Split-Path -Parent $PSScriptRoot

$src = Join-Path $searxRoot 'searxng-src'
$python = Join-Path $searxRoot '.venv\Scripts\python.exe'
$log = Join-Path $searxRoot 'searxng.log'

# The tuned settings live in the browse-API repository rather than beside the
# SearXNG source. The engine list there was measured against this network's
# egress -- bing answers, duckduckgo and brave are blocked -- and keeping one
# copy means a git pull updates the search backend too.
$settings = Join-Path $browseApi 'searxng\settings.yml'

$stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')

# Say which piece is missing, in the log, before throwing. A task that exits
# non-zero with no explanation is the failure this whole exercise was about.
foreach ($required in @($python, $src, $settings)) {
    if (-not (Test-Path $required)) {
        Add-Content -Path $log -Value "==== $stamp cannot start: missing $required ====" -Encoding UTF8
        throw "SearXNG cannot start: missing $required"
    }
}

# Trust what Windows trusts. searx is started with -m, so there is no entry
# point of ours to run first; sitecustomize is imported by the site module at
# interpreter startup, which is early enough. Without this the proxy's CA is
# unknown to certifi, engines fail to initialise, and the ones that fail never
# respond -- which is what drops engine coverage below the floor and sends
# questions to the paid provider.
$env:PYTHONPATH = Join-Path $PSScriptRoot 'searxng-site'

$env:SEARXNG_SETTINGS_PATH = $settings
$env:SEARXNG_BIND_ADDRESS = '127.0.0.1'
$env:SEARXNG_PORT = '8082'

# The repo's settings.yml takes its secret from the environment, the way
# docker-compose supplies it. Generated once and kept, so a restart does not
# invalidate anything that outlives it.
$secretFile = Join-Path $searxRoot 'secret.txt'
if (-not (Test-Path $secretFile)) {
    $generated = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
    Set-Content -Path $secretFile -Value $generated -Encoding ascii
}
$env:SEARXNG_SECRET = (Get-Content $secretFile -Raw).Trim()

Add-Content -Path $log -Value '' -Encoding UTF8
Add-Content -Path $log -Value "==== $stamp starting on 127.0.0.1:8082 ====" -Encoding UTF8
Add-Content -Path $log -Value "     settings: $settings" -Encoding UTF8

Set-Location $src

# Continue, not Stop: SearXNG logs to stderr, PowerShell turns a native
# command's stderr into error records, and under Stop the first line it logged
# would kill it. "$_" flattens each record back to the line it actually wrote.
$ErrorActionPreference = 'Continue'

& $python -m searx.webapp 2>&1 |
    ForEach-Object { "$_" } |
    Out-File -FilePath $log -Append -Encoding utf8
