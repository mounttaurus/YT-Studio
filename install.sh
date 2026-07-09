#!/usr/bin/env bash
# ===================================================================
# YouTube Auto - Mac/Linux installer / setup script (install.ps1 の bash版)
#
# Usage (typical first run):
#   ./install.sh                 # fresh setup (light agents only)
#   ./install.sh --gpu            # fresh setup + start GPU services (NVIDIA + nvidia-container-toolkit が必要)
#   ./install.sh --skip-build     # prepare everything but skip docker compose build/up
#   ./install.sh --yes            # skip confirmation prompts
#
# This repo only ships the agents (Docker) + an MCP server. Bring your own MCP-capable
# AI client (Claude Code, Goose, ...) to drive it -- see Docs/MCP_CLIENT_SETUP.md.
#
# Config is a SINGLE root .env (users edit only this one file).
# All large data/models live in visible host folders (no named Docker volumes).
# Idempotent: safe to re-run. Existing .env / cloned source / models are kept.
# ===================================================================
set -uo pipefail

GPU=0
SKIP_BUILD=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --gpu) GPU=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

info() { printf '\033[36m[*] %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m[OK] %s\033[0m\n' "$1"; }
warn() { printf '\033[33m[!] %s\033[0m\n' "$1"; }
err()  { printf '\033[31m[X] %s\033[0m\n' "$1"; }
step() { printf '\n\033[35m=== %s ===\033[0m\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

echo ""
echo "  YouTube Auto - installer (Mac/Linux)"
echo "  root: $ROOT"

# -------------------------------------------------------------------
step "1. Prerequisite check"
# -------------------------------------------------------------------
FAIL=0

if have docker; then
  ok "docker: $(docker --version)"
  if docker compose version >/dev/null 2>&1; then
    ok "compose: $(docker compose version | head -n1)"
  else
    err "'docker compose' (v2) not found. Update Docker Desktop / docker-compose-plugin."
    FAIL=1
  fi
  if docker info >/dev/null 2>&1; then
    ok "docker daemon is running"
  else
    err "Docker daemon not reachable. Start Docker Desktop / dockerd."
    FAIL=1
  fi
else
  err "docker not found. Install Docker Desktop (Mac) or Docker Engine + compose plugin (Linux)."
  FAIL=1
fi

if have git; then ok "git: $(git --version)"; else err "git not found."; FAIL=1; fi

PYTHON_BIN=""
if have python3; then PYTHON_BIN=python3; ok "python: $(python3 --version)";
elif have python; then PYTHON_BIN=python; ok "python: $(python --version)";
else warn "python not found. mcp-agent (MCP server for Claude Code/Goose etc.) setup will be skipped; Docker services still work standalone."; fi

if have nvidia-smi; then
  GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n1)"
  [ -n "$GPU_NAME" ] && ok "NVIDIA GPU: $GPU_NAME"
else
  if [ "$GPU" = "1" ]; then
    err "--gpu requested but nvidia-smi not found. Need NVIDIA GPU + drivers + nvidia-container-toolkit (Linux). Mac has no NVIDIA GPU passthrough."
    FAIL=1
  else
    warn "No NVIDIA GPU detected. GPU services (irodori/imagegen) will be unavailable; light agents still run."
  fi
fi

if [ "$FAIL" = "1" ]; then err "Prerequisite check failed. Fix the above and re-run."; exit 1; fi

# -------------------------------------------------------------------
step "2. Clone Irodori-TTS-Server source"
# -------------------------------------------------------------------
IRODORI_SRC="$ROOT/tts-agent/irodori-tts-server-src"
if [ -f "$IRODORI_SRC/Dockerfile" ]; then
  ok "Irodori source already present (skip clone)"
else
  info "git clone Irodori-TTS-Server ..."
  git clone --depth 1 https://github.com/Aratako/Irodori-TTS-Server.git "$IRODORI_SRC"
  ok "cloned -> tts-agent/irodori-tts-server-src"
fi

# -------------------------------------------------------------------
step "2b. Clone omnivoice-server source (multilingual TTS)"
# -------------------------------------------------------------------
OMNIVOICE_SRC="$ROOT/tts-agent/omnivoice-tts-server-src"
if [ -f "$OMNIVOICE_SRC/Dockerfile.cuda" ]; then
  ok "omnivoice-server source already present (skip clone)"
else
  info "git clone omnivoice-server ..."
  git clone --depth 1 https://github.com/maemreyo/omnivoice-server.git "$OMNIVOICE_SRC"
  ok "cloned -> tts-agent/omnivoice-tts-server-src"
fi

# -------------------------------------------------------------------
step "3. Configuration (single root .env)"
# -------------------------------------------------------------------
ENV_PATH="$ROOT/.env"
EXAMPLE_PATH="$ROOT/.env.example"
if [ -f "$ENV_PATH" ]; then
  ok ".env exists (kept)"
else
  cp "$EXAMPLE_PATH" "$ENV_PATH"
  warn ".env created from .env.example"
fi

set_env_value() {
  local key="$1" value="$2"
  local escaped_value
  escaped_value=$(printf '%s' "$value" | sed -e 's/[\/&]/\\&/g')
  if grep -qE "^\s*${key}=" "$ENV_PATH"; then
    sed -i.bak -E "s|^\s*${key}=.*$|${key}=${escaped_value}|" "$ENV_PATH" && rm -f "$ENV_PATH.bak"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_PATH"
  fi
}

set_env_value 'HOST_SHARED_DIR' "$ROOT/shared"
ok "HOST_SHARED_DIR -> $ROOT/shared"

# -------------------------------------------------------------------
step "4. Host folders (data + model skeleton)"
# -------------------------------------------------------------------
for d in characters styles voices projects footage_pool imagegen tts_cache direct_output; do
  mkdir -p "$ROOT/shared/$d"
done
for p in "tts-agent/irodori-models" "shared/voices/irodori" "imagegen-agent/models/checkpoints" "imagegen-agent/models/vae" "imagegen-agent/models/loras" "imagegen-agent/output"; do
  mkdir -p "$ROOT/$p"
done
ok "host folders ready (shared/, tts-agent/irodori-models, imagegen-agent/models, ...)"

# -------------------------------------------------------------------
step "5. mcp-agent (MCP server for Claude Code / Goose etc.)"
# -------------------------------------------------------------------
# Bring-your-own-AI-agent model: this repo only exposes the MCP server. Users connect their own
# MCP-capable AI client (Claude Code, Goose, ...). See Docs/MCP_CLIENT_SETUP.md.
if [ -z "$PYTHON_BIN" ]; then
  warn "python not found on host; skipping mcp-agent venv + .mcp.json generation."
else
  MCP_DIR="$ROOT/mcp-agent"
  VENV_DIR="$MCP_DIR/.venv"
  VENV_PYTHON="$VENV_DIR/bin/python"
  if [ -x "$VENV_PYTHON" ]; then
    ok "mcp-agent venv already present (skip)"
  else
    info "creating mcp-agent venv ..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    "$VENV_PYTHON" -m pip install --quiet --upgrade pip
    "$VENV_PYTHON" -m pip install --quiet -r "$MCP_DIR/requirements.txt"
    ok "mcp-agent venv ready"
  fi

  MCP_JSON_PATH="$ROOT/.mcp.json"
  MCP_TEMPLATE_PATH="$ROOT/.mcp.json.template"
  if [ -f "$MCP_TEMPLATE_PATH" ]; then
    SERVER_PY="$MCP_DIR/server.py"
    sed -e "s|{{PYTHON_EXE}}|${VENV_PYTHON}|" -e "s|{{SERVER_PY}}|${SERVER_PY}|" "$MCP_TEMPLATE_PATH" > "$MCP_JSON_PATH"
    ok ".mcp.json generated -> $MCP_JSON_PATH"
  else
    warn ".mcp.json.template not found (skip MCP config generation)"
  fi
fi

# -------------------------------------------------------------------
step "6. Build & start"
# -------------------------------------------------------------------
PROFILE_ARGS=()
[ "$GPU" = "1" ] && PROFILE_ARGS=(--profile gpu)

if [ "$SKIP_BUILD" = "1" ]; then
  warn "--skip-build set. Validating compose config only."
  docker compose -f docker-compose.yml config --quiet && ok "base docker-compose.yml is valid."
  docker compose config --quiet && ok "dev override is valid."
  echo ""
  info "To build & start later:"
  echo "    docker compose up -d --build                 # dev (hot reload, light agents)"
  echo "    docker compose --profile gpu up -d --build   # + GPU services"
  echo "    docker compose -f docker-compose.yml up -d   # clean/prod (no hot reload)"
  exit 0
fi

if [ "$ASSUME_YES" != "1" ]; then
  if [ "$GPU" = "1" ]; then WHAT="ALL services (incl. GPU, downloads multi-GB models on first run)"; else WHAT="light agents (scripting/director/scrapping/editing/tts)"; fi
  read -r -p "Build and start $WHAT now? [y/N] " ANS
  case "$ANS" in
    y|Y) ;;
    *) warn "Aborted before build. Re-run without prompt using --yes."; exit 0 ;;
  esac
fi

info "docker compose build ..."
docker compose "${PROFILE_ARGS[@]}" build
info "docker compose up -d ..."
docker compose "${PROFILE_ARGS[@]}" up -d

step "Done"
docker compose "${PROFILE_ARGS[@]}" ps
echo ""
ok "Endpoints:"
echo "  director  : http://localhost:8005"
echo "  scripting : http://localhost:8002"
echo "  scrapping : http://localhost:8003"
echo "  tts       : http://localhost:8004"
echo "  editing   : http://localhost:8006"
if [ "$GPU" = "1" ]; then
  echo "  irodori   : http://localhost:8088  (model load can take a few min)"
  echo "  imagegen  : http://localhost:8188"
fi
echo ""
info "Next: connect your MCP-capable AI client (Claude Code / Goose / ...) -- see Docs/MCP_CLIENT_SETUP.md"
