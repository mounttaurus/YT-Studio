# ===================================================================
# YouTube Auto - Windows installer / setup script
#
# Usage (typical first run):
#   .\install.ps1                              # fresh setup (light agents only)
#   .\install.ps1 -Gpu                         # fresh setup + start GPU services
#
# This repo only ships the agents (Docker) + an MCP server. Bring your own MCP-capable
# AI client (Claude Code, Goose, ...) to drive it -- see Docs/MCP_CLIENT_SETUP.md.
#
# Advanced: -FromOldRoot <path> carries over .env keys / voices / SD models / irodori cache
# from a previous install of this same repo (re-installs, not needed for a first-time setup):
#   .\install.ps1 -FromOldRoot D:\Docker\YT-Studio-old
#   .\install.ps1 -FromOldRoot D:\Docker\YT-Studio-old -SkipBuild   # prepare only, no docker build/up
#
# Config is a SINGLE root .env (users edit only this one file).
# All large data/models live in visible host folders (no named Docker volumes).
# Idempotent: safe to re-run. Existing .env / cloned source / models are kept.
# ===================================================================
[CmdletBinding()]
param(
    [string]$FromOldRoot = "",   # Advanced/optional: old root to carry over .env keys, voices, SD models, irodori model cache, character/style libs
    [switch]$Gpu,                # Also start GPU services (irodori-tts-server, imagegen-agent)
    [switch]$SkipBuild,          # Prepare everything but skip `docker compose build/up`
    [switch]$Yes                 # Skip confirmation prompts
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

function Info($m)  { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "[!] $m" -ForegroundColor Yellow }
function Err($m)   { Write-Host "[X] $m" -ForegroundColor Red }
function Step($m)  { Write-Host ""; Write-Host "=== $m ===" -ForegroundColor Magenta }
function Have($exe) { return [bool](Get-Command $exe -ErrorAction SilentlyContinue) }

# Keys NOT to harvest from an old root (root-specific values that must be regenerated here).
# それ以外の旧 .env のキーは全て汎用継承する（固定の許可リストはキー追加のたびに陳腐化して
# 取りこぼす事故があったため廃止。2026-07-02: GROK/CLOUDFLARE/RUNWARE 等8キーが漏れた実例）。
$ENV_SKIP_KEYS = @('HOST_SHARED_DIR')
$OLD_ENV_FILES = @('scripting-agent\.env','director-agent\.env','scrapping-agent\.env','editing-agent\.env','tts-agent\.env','.env')

# Reusable shared/ libraries to carry over; generated/test data is NOT copied.
$SHARED_KEEP     = @('characters','styles','voices')
$SHARED_SKELETON = @('characters','styles','voices','projects','footage_pool','imagegen','tts_cache','direct_output')

# --- .env helpers ---
function Get-EnvPairs($path) {
    $h = @{}
    if (-not (Test-Path $path)) { return $h }
    # UTF-8 明示読み。既定読み(cp932)だと日本語コメントの多バイト末尾が改行を食い、直後の行が消える
    foreach ($line in [System.IO.File]::ReadAllLines($path, [System.Text.Encoding]::UTF8)) {
        if ($line -match '^\s*#') { continue }
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $v = $matches[2].Trim()
            # strip a single pair of surrounding quotes if present
            if ($v.Length -ge 2 -and (($v[0] -eq '"' -and $v[-1] -eq '"') -or ($v[0] -eq "'" -and $v[-1] -eq "'"))) {
                $v = $v.Substring(1, $v.Length - 2)
            }
            $h[$matches[1]] = $v
        }
    }
    return $h
}
function Set-EnvValue($path, $key, $value) {
    # UTF-8 明示読み書き（既定読みcp932→UTF8書きだと日本語コメントが文字化けして固定化する）
    $content = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    if ($content -match "(?m)^\s*$key=.*$") {
        $content = [Regex]::Replace($content, "(?m)^\s*$key=.*$", "$key=$value")
    } else {
        $content = $content.TrimEnd() + "`r`n$key=$value`r`n"
    }
    [System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))
}

Write-Host ""
Write-Host "  YouTube Auto - installer" -ForegroundColor White
Write-Host "  root: $Root" -ForegroundColor DarkGray
if ($FromOldRoot) { Write-Host "  migrate from: $FromOldRoot" -ForegroundColor DarkGray }

# -------------------------------------------------------------------
Step "1. Prerequisite check"
# -------------------------------------------------------------------
$fail = $false

if (Have 'docker') {
    Ok "docker: $(docker --version)"
    try { $cv = (docker compose version) | Select-Object -First 1; Ok "compose: $cv" }
    catch { Err "'docker compose' (v2) not found. Update Docker Desktop."; $fail = $true }
    try { docker info *> $null; Ok "docker daemon is running" }
    catch { Err "Docker daemon not reachable. Start Docker Desktop."; $fail = $true }
} else {
    Err "docker not found. Install Docker Desktop for Windows (with WSL2 backend)."
    $fail = $true
}

if (Have 'git') { Ok "git: $(git --version)" } else { Err "git not found. Install Git for Windows."; $fail = $true }

if (Have 'python') { Ok "python: $(python --version)" }
else { Warn "python not found. mcp-agent (MCP server for Claude Code/Goose etc.) setup will be skipped; Docker services still work standalone." }

if (Have 'nvidia-smi') {
    $gpuName = (nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1)
    if ($gpuName) { Ok "NVIDIA GPU: $gpuName" }
} else {
    if ($Gpu) { Err "-Gpu requested but nvidia-smi not found. Need NVIDIA GPU + drivers + WSL2 GPU support."; $fail = $true }
    else { Warn "No NVIDIA GPU detected. GPU services (irodori/imagegen) will be unavailable; light agents still run." }
}

if ($fail) { Err "Prerequisite check failed. Fix the above and re-run."; exit 1 }

# -------------------------------------------------------------------
Step "2. Clone Irodori-TTS-Server source"
# -------------------------------------------------------------------
$irodoriSrc = Join-Path $Root 'tts-agent\irodori-tts-server-src'
if (Test-Path (Join-Path $irodoriSrc 'Dockerfile')) {
    Ok "Irodori source already present (skip clone)"
} else {
    Info "git clone Irodori-TTS-Server ..."
    git clone --depth 1 https://github.com/Aratako/Irodori-TTS-Server.git $irodoriSrc
    Ok "cloned -> tts-agent\irodori-tts-server-src"
}

# -------------------------------------------------------------------
Step "2b. Clone omnivoice-server source (multilingual TTS)"
# -------------------------------------------------------------------
$omnivoiceSrc = Join-Path $Root 'tts-agent\omnivoice-tts-server-src'
if (Test-Path (Join-Path $omnivoiceSrc 'Dockerfile.cuda')) {
    Ok "omnivoice-server source already present (skip clone)"
} else {
    Info "git clone omnivoice-server ..."
    git clone --depth 1 https://github.com/maemreyo/omnivoice-server.git $omnivoiceSrc
    Ok "cloned -> tts-agent\omnivoice-tts-server-src"
}

# -------------------------------------------------------------------
Step "3. Configuration (single root .env)"
# -------------------------------------------------------------------
$envPath     = Join-Path $Root '.env'
$examplePath = Join-Path $Root '.env.example'
if (Test-Path $envPath) {
    Ok ".env exists (kept)"
} else {
    Copy-Item $examplePath $envPath
    Warn ".env created from .env.example"
}

# Harvest known keys from old root (old layout used per-agent .env files)
if ($FromOldRoot) {
    $harvest = @{}
    foreach ($rel in $OLD_ENV_FILES) {
        $p = Join-Path $FromOldRoot $rel
        foreach ($kv in (Get-EnvPairs $p).GetEnumerator()) {
            if ($ENV_SKIP_KEYS -notcontains $kv.Key -and $kv.Value -ne '' -and -not $harvest.ContainsKey($kv.Key)) {
                $harvest[$kv.Key] = $kv.Value
            }
        }
    }
    if ($harvest.Count -gt 0) {
        foreach ($k in $harvest.Keys) { Set-EnvValue $envPath $k $harvest[$k] }
        Ok "merged $($harvest.Count) keys from old root: $(( $harvest.Keys | Sort-Object ) -join ', ')"
    } else {
        Warn "no reusable keys found in old root .env files"
    }
}

# HOST_SHARED_DIR = this root's shared (used by DaVinci Resolve via editing-agent)
Set-EnvValue $envPath 'HOST_SHARED_DIR' (Join-Path $Root 'shared')
Ok "HOST_SHARED_DIR -> $(Join-Path $Root 'shared')"
if (-not $FromOldRoot) { Warn "Fill API keys in: $envPath" }

# -------------------------------------------------------------------
Step "4. Host folders (data + model skeleton)"
# -------------------------------------------------------------------
foreach ($d in $SHARED_SKELETON) {
    $p = Join-Path $Root "shared\$d"
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p | Out-Null }
}
# model/data folders are bind-mounted (no Docker volumes)
foreach ($p in @('tts-agent\irodori-models','shared\voices\irodori','imagegen-agent\models\checkpoints','imagegen-agent\models\vae','imagegen-agent\models\loras','imagegen-agent\output')) {
    $full = Join-Path $Root $p
    if (-not (Test-Path $full)) { New-Item -ItemType Directory -Path $full | Out-Null }
}
Ok "host folders ready (shared/, tts-agent/irodori-models, imagegen-agent/models, ...)"

# -------------------------------------------------------------------
Step "5. Carry over assets from old root (optional)"
# -------------------------------------------------------------------
if ($FromOldRoot) {
    if (-not (Test-Path $FromOldRoot)) { Err "FromOldRoot path not found: $FromOldRoot"; exit 1 }

    function Mirror($src, $dst, $label) {
        if (-not (Test-Path $src)) { Warn "skip $label (source missing: $src)"; return }
        Info "copy $label ..."
        robocopy $src $dst /E /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { Err "robocopy failed for $label (code $LASTEXITCODE)" }
        else { Ok "$label copied"; $global:LASTEXITCODE = 0 }
    }

    # voices (reference audio = 声カタログ). 旧ルートは tts-agent\voices、新ルートは shared\voices\irodori
    Mirror (Join-Path $FromOldRoot 'tts-agent\voices') (Join-Path $Root 'shared\voices\irodori') 'voices -> shared\voices\irodori'
    # SD models (avoid multi-GB re-download)
    Mirror (Join-Path $FromOldRoot 'imagegen-agent\models') (Join-Path $Root 'imagegen-agent\models') 'imagegen-agent\models'
    # reusable shared libraries only (NOT projects/footage/cache test data)
    foreach ($d in $SHARED_KEEP) {
        Mirror (Join-Path $FromOldRoot "shared\$d") (Join-Path $Root "shared\$d") "shared\$d"
    }
    Warn "NOT copied (kept blank): shared\projects, shared\footage_pool, shared\tts_cache, shared\direct_output"

    # irodori HF model cache: old layout stored it in a NAMED VOLUME (not a folder).
    # Export it once into the new bind-mount folder so it is NOT re-downloaded.
    $irodoriDst = Join-Path $Root 'tts-agent\irodori-models'
    $hasData = (Test-Path $irodoriDst) -and (Get-ChildItem $irodoriDst -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($hasData) {
        Ok "irodori-models already populated (skip volume export)"
    } else {
        $vol = $null
        try { $vol = (docker volume ls -q) | Where-Object { $_ -match 'irodori_models' } | Select-Object -First 1 } catch {}
        if ($vol) {
            Info "exporting Docker volume '$vol' -> tts-agent\irodori-models (avoids ~4.7G re-download) ..."
            docker run --rm -v "${vol}:/from" -v "${irodoriDst}:/to" alpine sh -c "cp -a /from/. /to/ 2>/dev/null || true"
            if ($LASTEXITCODE -eq 0) { Ok "irodori model cache exported" }
            else { Warn "volume export returned $LASTEXITCODE; irodori will re-download on first GPU start" }
        } else {
            Warn "no old irodori_models volume found; irodori will download model on first GPU start"
        }
    }
} else {
    Info "no -FromOldRoot: starting blank (models downloaded on first run / via UI)"
}

# -------------------------------------------------------------------
Step "5b. User-friendly folder (_ユーザーファイル\)"
# -------------------------------------------------------------------
# shared\ への "名前付きショートカット集" を作る (Docker非依存・SkipBuildでも実行)。
try {
    & (Join-Path $Root 'refresh-user-folders.ps1') -Quiet
    Ok "_ユーザーファイル\ ready (named shortcuts into shared/; re-run フォルダ整理.cmd after adding chars/projects)."
} catch {
    Warn "Could not build _ユーザーファイル\ (non-fatal): $($_.Exception.Message)"
}

# -------------------------------------------------------------------
Step "5c. mcp-agent (MCP server for Claude Code / Goose etc.)"
# -------------------------------------------------------------------
# Bring-your-own-AI-agent model: this repo only exposes the MCP server. Users connect their own
# MCP-capable AI client (Claude Code, Goose, ...). See Docs/MCP_CLIENT_SETUP.md.
if (-not (Have 'python')) {
    Warn "python not found on host; skipping mcp-agent venv + .mcp.json generation."
} else {
    $mcpDir     = Join-Path $Root 'mcp-agent'
    $venvDir    = Join-Path $mcpDir '.venv'
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    if (Test-Path $venvPython) {
        Ok "mcp-agent venv already present (skip)"
    } else {
        Info "creating mcp-agent venv ..."
        python -m venv $venvDir
        & $venvPython -m pip install --quiet --upgrade pip
        & $venvPython -m pip install --quiet -r (Join-Path $mcpDir 'requirements.txt')
        Ok "mcp-agent venv ready"
    }

    $mcpJsonPath      = Join-Path $Root '.mcp.json'
    $mcpTemplatePath  = Join-Path $Root '.mcp.json.template'
    if (Test-Path $mcpTemplatePath) {
        $serverPy = Join-Path $mcpDir 'server.py'
        # JSON needs backslashes doubled; use literal .Replace (not -replace/regex) to avoid escaping pitfalls.
        $pyForJson     = $venvPython.Replace('\', '\\')
        $serverForJson = $serverPy.Replace('\', '\\')
        $content = Get-Content $mcpTemplatePath -Raw
        $content = $content.Replace('{{PYTHON_EXE}}', $pyForJson).Replace('{{SERVER_PY}}', $serverForJson)
        Set-Content -Path $mcpJsonPath -Value $content -Encoding UTF8
        Ok ".mcp.json generated -> $mcpJsonPath"
    } else {
        Warn ".mcp.json.template not found (skip MCP config generation)"
    }
}

# -------------------------------------------------------------------
Step "6. Build & start"
# -------------------------------------------------------------------
if ($SkipBuild) {
    Warn "-SkipBuild set. Validating compose config only."
    docker compose -f docker-compose.yml config --quiet
    Ok "base docker-compose.yml is valid."
    docker compose config --quiet
    Ok "dev override is valid."
    Write-Host ""
    Info "To build & start later:"
    Write-Host "    docker compose up -d --build                 # dev (hot reload, light agents)" -ForegroundColor Gray
    Write-Host "    docker compose --profile gpu up -d --build   # + GPU services" -ForegroundColor Gray
    Write-Host "    docker compose -f docker-compose.yml up -d   # clean/prod (no hot reload)" -ForegroundColor Gray
    exit 0
}

$profileArgs = @()
if ($Gpu) { $profileArgs = @('--profile','gpu') }

if (-not $Yes) {
    $what = if ($Gpu) { "ALL services (incl. GPU, downloads multi-GB models on first run)" } else { "light agents (scripting/director/scrapping/editing/tts)" }
    $ans = Read-Host "Build and start $what now? [y/N]"
    if ($ans -notmatch '^(y|Y)') { Warn "Aborted before build. Re-run without prompt using -Yes."; exit 0 }
}

Info "docker compose build ..."
docker compose @profileArgs build
Info "docker compose up -d ..."
docker compose @profileArgs up -d

Step "Done"
docker compose @profileArgs ps
Write-Host ""
Ok "Endpoints:"
Write-Host "  director  : http://localhost:8005" -ForegroundColor Gray
Write-Host "  scripting : http://localhost:8002" -ForegroundColor Gray
Write-Host "  scrapping : http://localhost:8003" -ForegroundColor Gray
Write-Host "  tts       : http://localhost:8004" -ForegroundColor Gray
Write-Host "  editing   : http://localhost:8006" -ForegroundColor Gray
if ($Gpu) {
    Write-Host "  irodori   : http://localhost:8088  (model load can take a few min)" -ForegroundColor Gray
    Write-Host "  imagegen  : http://localhost:8188" -ForegroundColor Gray
}
Write-Host ""
Info "Next: connect your MCP-capable AI client (Claude Code / Goose / ...) -- see Docs/MCP_CLIENT_SETUP.md"
