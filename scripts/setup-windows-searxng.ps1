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

# git and pip write ordinary progress to stderr, and PowerShell turns any
# stderr from a native command into a NativeCommandError -- which, under
# ErrorActionPreference Stop, kills the script on a successful clone. Native
# commands run through here instead, judged on their exit code like everywhere
# else in computing.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [switch]$IgnoreExitCode)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object { "$_" } | Out-Null
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if (-not $IgnoreExitCode -and $code -ne 0) {
        throw "$Exe $($Arguments -join ' ') failed with exit code $code"
    }
}

Write-Host "== SearXNG native Windows setup ==" -ForegroundColor Cyan

# --- source -----------------------------------------------------------------
$src = Join-Path $Root "searxng-src"
$webapp = Join-Path $src "searx\webapp.py"

if (-not (Test-Path $src)) {
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    Write-Host "cloning SearXNG (the four colon-named templates will fail; that is expected)"
    Push-Location $Root
    # The checkout aborts on the invalid filenames -- a non-zero exit that is
    # not a failure here, because git has still recorded the whole tree.
    Invoke-Native "git" @("clone", "--depth", "1", "https://github.com/searxng/searxng.git", "searxng-src") -IgnoreExitCode
    Pop-Location
}

if (-not (Test-Path $webapp)) {
    # Writes every file Windows can represent, skipping the four it cannot.
    Write-Host "  completing the checkout"
    Push-Location $src
    Invoke-Native "git" @("restore", "--source=HEAD", ":/") -IgnoreExitCode
    Pop-Location
}

if (-not (Test-Path $webapp)) {
    throw "SearXNG source is incomplete: searx\webapp.py is missing. Delete $src and re-run."
}
Write-Host "  source ready: $src" -ForegroundColor Green

# --- the one POSIX patch ----------------------------------------------------
# Here-strings, not single-quoted strings: in single quotes PowerShell writes
# a literal backtick-n rather than a newline, which lands one unparseable line
# in the middle of somebody's Python.
$importOld = @"
import os
import pwd
import logging
"@
$importNew = @"
import os

try:  # pwd is POSIX-only; Windows has no account database module
    import pwd
except ImportError:  # pragma: no cover - Windows
    pwd = None  # type: ignore[assignment]
import logging
"@
$guardOld = '        _pw = pwd.getpwuid(os.getuid())'
$guardNew = @"
        if pwd is None:
            logger.exception("cannot connect valkey DB ...")
            return False
        _pw = pwd.getpwuid(os.getuid())
"@

$valkeydb = Join-Path $src "searx\valkeydb.py"
$text = (Get-Content $valkeydb -Raw) -replace "`r`n", "`n"

# A file this script wrote with the old single-quoted bug carries a literal
# backtick-n. Repair it from source rather than leaving it half-patched.
if ($text -match [regex]::Escape('`n')) {
    Write-Host "  repairing a previous bad patch" -ForegroundColor Yellow
    Push-Location $src
    Invoke-Native "git" @("checkout", "--", "searx/valkeydb.py") -IgnoreExitCode
    Pop-Location
    $text = (Get-Content $valkeydb -Raw) -replace "`r`n", "`n"
}

if ($text -notmatch "pwd is POSIX-only") {
    $text = $text.Replace($importOld.Replace("`r`n", "`n").TrimEnd("`n"), $importNew.Replace("`r`n", "`n").TrimEnd("`n"))
    $text = $text.Replace($guardOld, $guardNew.Replace("`r`n", "`n").TrimEnd("`n"))
    [System.IO.File]::WriteAllText($valkeydb, $text)
    Write-Host "  patched searx\valkeydb.py for Windows" -ForegroundColor Green
} else {
    Write-Host "  searx\valkeydb.py already patched" -ForegroundColor DarkGray
}

# Prove it parses before anything tries to import it.
Invoke-Native "python" @("-c", "import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())", $valkeydb) -IgnoreExitCode | Out-Null

# --- environment ------------------------------------------------------------
$venv = Join-Path $Root ".venv"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "creating virtual environment with $Python"
    # $Python may be a launcher invocation ("py -3.12") or a full path.
    $parts = $Python.Split(" ", 2)
    $exe = $parts[0]
    $rest = if ($parts.Count -gt 1) { $parts[1].Split(" ") } else { @() }
    Invoke-Native $exe (@($rest) + @("-m", "venv", $venv))
}
if (-not (Test-Path $py)) {
    throw "Could not create a virtual environment with '$Python'. Pass -Python with a working interpreter, e.g. -Python 'C:\Python314\python.exe'."
}
Write-Host "installing requirements (wheels only, no compiler needed)"
Invoke-Native $py @("-m", "pip", "install", "--quiet", "--upgrade", "pip")
Invoke-Native $py @("-m", "pip", "install", "--quiet", "-r", (Join-Path $src "requirements.txt"))
Write-Host "  dependencies installed" -ForegroundColor Green

# --- settings ---------------------------------------------------------------
# The repo's own searxng/settings.yml is the tuned one: it carries the
# engine-probe block, so the engines measured to work on this egress (bing,
# notably) are enabled there and regenerate with scripts/engine_probe.py.
# Native mode reads that file directly rather than keeping a second, staler
# copy -- a `git pull` then updates the search backend too.
if (-not $SettingsPath) {
    $repoSettings = Join-Path $PSScriptRoot "..\searxng\settings.yml"
    if (Test-Path $repoSettings) {
        $SettingsPath = (Resolve-Path $repoSettings).Path
        Write-Host "  using the repo's tuned settings: $SettingsPath" -ForegroundColor Green
    }
}
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
# The repo's settings.yml takes its secret from the environment, the way
# docker-compose supplies it. Generated once and kept, so restarts do not
# invalidate anything that outlives them.
$secretFile = Join-Path $Root "secret.txt"
if (-not (Test-Path $secretFile)) {
    Set-Content -Path $secretFile -Value ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')) -Encoding ascii
}
$env:SEARXNG_SECRET = (Get-Content $secretFile -Raw).Trim()
Start-Process -FilePath $py -ArgumentList "-m", "searx.webapp" `
    -WorkingDirectory $src -WindowStyle Hidden `
    -RedirectStandardError $log -RedirectStandardOutput "$log.out"

Start-Sleep -Seconds 20
try {
    $probe = Invoke-RestMethod "http://127.0.0.1:$Port/search?q=test&format=json" -TimeoutSec 30
    Write-Host "`nSearXNG is answering: $($probe.results.Count) results for 'test'" -ForegroundColor Green
    Write-Host "Point browse-API at it with:  SEARXNG_URL=http://127.0.0.1:$Port"
} catch {
    Write-Host "`nSearXNG did not answer yet. Check $log" -ForegroundColor Yellow
}
