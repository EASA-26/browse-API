<#
.SYNOPSIS
    Install and run SearXNG natively on Windows, no containers.

.DESCRIPTION
    Zero-container mode leaves one gap: the search backend. SearXNG is a pure
    Python Flask application and runs on Windows once two POSIX assumptions are
    worked around, both handled here:

      1. Four deployment templates carry a colon in their filename
         (searxng.conf:socket). Windows cannot create those, so `git clone`
         aborts mid-checkout. `git restore` afterwards lands everything else --
         the four are nginx/uwsgi samples nothing at runtime reads.

      2. searx/valkeydb.py imports `pwd`, the POSIX account database, at module
         scope. It is used on exactly one line, to name the local user in an
         error message when Valkey is unreachable. This script guards the
         import so Windows gets the same message without the username.

    Everything else -- 252 engine modules, lxml, the Flask app -- installs from
    ordinary wheels with no compiler.

.PARAMETER Root
    Where to put the SearXNG source and its virtual environment.

.PARAMETER Port
    Port for SearXNG to listen on (loopback only).

.PARAMETER Python
    Python to build the venv with. SearXNG's dependency set is happiest on
    3.12/3.13; 3.14 is newer than its pins.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows-searxng.ps1 -Root D:\EASA\searxng -Port 8082
#>
param(
    [string]$Root = "D:\EASA\searxng",
    [int]$Port = 8082,
    [string]$Python = "py -3.12",
    [string]$SettingsPath = ""
)

$ErrorActionPreference = "Stop"

Write-Host "== SearXNG native Windows setup ==" -ForegroundColor Cyan

# --- source -----------------------------------------------------------------
$src = Join-Path $Root "searxng-src"
if (-not (Test-Path $src)) {
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    Write-Host "cloning SearXNG (the four colon-named templates will fail; that is expected)"
    Push-Location $Root
    # The checkout aborts on the invalid filenames; git still records the tree,
    # and restore then writes every file Windows can represent.
    git clone --depth 1 https://github.com/searxng/searxng.git searxng-src 2>&1 | Out-Null
    Pop-Location
    Push-Location $src
    git restore --source=HEAD :/ 2>&1 | Where-Object { $_ -notmatch "invalid path" } | Out-Null
    Pop-Location
}
if (-not (Test-Path (Join-Path $src "searx\webapp.py"))) {
    throw "SearXNG source is incomplete: searx\webapp.py is missing."
}
Write-Host "  source ready: $src" -ForegroundColor Green

# --- the one POSIX patch ----------------------------------------------------
$valkeydb = Join-Path $src "searx\valkeydb.py"
$text = Get-Content $valkeydb -Raw
if ($text -notmatch "pwd is POSIX-only") {
    $text = $text.Replace(
        "import os`nimport pwd`nimport logging",
        "import os`n`ntry:  # pwd is POSIX-only; Windows has no account database module`n    import pwd`nexcept ImportError:  # pragma: no cover - Windows`n    pwd = None  # type: ignore[assignment]`nimport logging")
    $text = $text.Replace(
        '        _pw = pwd.getpwuid(os.getuid())',
        '        if pwd is None:`n            logger.exception("can''t connect valkey DB ...")`n            return False`n        _pw = pwd.getpwuid(os.getuid())')
    Set-Content -Path $valkeydb -Value $text -Encoding utf8
    Write-Host "  patched searx\valkeydb.py for Windows" -ForegroundColor Green
} else {
    Write-Host "  searx\valkeydb.py already patched" -ForegroundColor DarkGray
}

# --- environment ------------------------------------------------------------
$venv = Join-Path $Root ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "creating virtual environment with $Python"
    Invoke-Expression "$Python -m venv `"$venv`""
}
$py = Join-Path $venv "Scripts\python.exe"
Write-Host "installing requirements (wheels only, no compiler needed)"
& $py -m pip install --quiet --upgrade pip | Out-Null
& $py -m pip install --quiet -r (Join-Path $src "requirements.txt")
Write-Host "  dependencies installed" -ForegroundColor Green

# --- settings ---------------------------------------------------------------
if (-not $SettingsPath) {
    $SettingsPath = Join-Path $Root "settings.yml"
    if (-not (Test-Path $SettingsPath)) {
@"
# Minimal settings for an internal, API-only SearXNG.
# JSON must be enabled or format=json answers 403.
use_default_settings: true
server:
  secret_key: "$([guid]::NewGuid().ToString('N'))$([guid]::NewGuid().ToString('N'))"
  limiter: false
  image_proxy: false
search:
  formats:
    - html
    - json
"@ | Set-Content -Path $SettingsPath -Encoding utf8
        Write-Host "  wrote $SettingsPath" -ForegroundColor Green
    }
}

# --- run --------------------------------------------------------------------
$log = Join-Path $Root "searxng.log"
Write-Host "starting SearXNG on 127.0.0.1:$Port"
$env:SEARXNG_SETTINGS_PATH = $SettingsPath
$env:SEARXNG_BIND_ADDRESS = "127.0.0.1"
$env:SEARXNG_PORT = "$Port"
Start-Process -FilePath $py -ArgumentList "-m", "searx.webapp" `
    -WorkingDirectory $src -WindowStyle Hidden `
    -RedirectStandardError $log -RedirectStandardOutput "$log.out"

Start-Sleep -Seconds 12
try {
    $probe = Invoke-RestMethod "http://127.0.0.1:$Port/search?q=test&format=json" -TimeoutSec 30
    Write-Host "`nSearXNG is answering: $($probe.results.Count) results for 'test'" -ForegroundColor Green
    Write-Host "Point browse-API at it with:  SEARXNG_URL=http://127.0.0.1:$Port"
} catch {
    Write-Host "`nSearXNG did not answer yet. Check $log" -ForegroundColor Yellow
}
