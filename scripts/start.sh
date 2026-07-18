#!/usr/bin/env bash
# Start the Agent Identity POC stack (self-contained, no VPN/cloud required).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
cd "$ROOT"

# Load .env if present
[ -f .env ] && set -a && source .env && set +a

# Detect compose command
if command -v podman-compose &>/dev/null; then
  COMPOSE="podman-compose"
elif command -v docker &>/dev/null && docker compose version &>/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
  COMPOSE="docker-compose"
else
  echo "ERROR: podman-compose or docker compose not found." >&2; exit 1
fi
echo "Compose: $COMPOSE"

# AEAD key (grant store encryption)
if [ -z "${AGENT_STATE_AEAD_KEY:-}" ]; then
  export AGENT_STATE_AEAD_KEY
  AGENT_STATE_AEAD_KEY=$(python3 -c 'import os,base64; print(base64.b64encode(os.urandom(32)).decode())')
  echo "Generated AGENT_STATE_AEAD_KEY (add to .env to keep it stable across restarts)"
fi

# LLM model selection
if [ -n "${OPENAI_API_KEY:-}" ]; then
  export AGENT_MODEL="${AGENT_MODEL:-gpt-4o-mini}"
  echo "LLM: OpenAI  model=$AGENT_MODEL"
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  export AGENT_MODEL="${AGENT_MODEL:-claude-haiku}"
  echo "LLM: Anthropic  model=$AGENT_MODEL"
else
  export AGENT_MODEL="${AGENT_MODEL:-qwen-local}"
  echo "LLM: local llama.cpp  model=$AGENT_MODEL"
  echo "     → needs LOCAL_LLM_BASE in .env pointing at an OpenAI-compatible server"
  echo "       (llama-server must run with --jinja for tool calling)"
fi

echo ""
echo "Building and starting services..."
$COMPOSE up --build -d

echo ""
echo "Waiting for services..."

wait_healthy() {
  local svc=$1 max=${2:-60}
  printf "  %-24s" "$svc..."
  for i in $(seq 1 $max); do
    health=$($COMPOSE ps "$svc" 2>/dev/null | tail -1 || true)
    if echo "$health" | grep -qE 'healthy|Up'; then
      echo "✓"; return 0
    fi
    sleep 2
  done
  echo "⚠ timeout — check: $COMPOSE logs $svc"
}

wait_healthy keycloak 90          # Keycloak is slow to start
wait_healthy keycloak-setup 120   # setup runs after Keycloak is up
wait_healthy redis 20
wait_healthy litellm 30
wait_healthy obo-exchange 20
wait_healthy mcp-mock 15
wait_healthy agent 20
wait_healthy webapp 15

echo ""
echo "════════════════════════════════════════════════════"
echo "  Stack is up!"
echo ""
echo "  Webapp (identity visualizer):  http://localhost:8080"
echo "  Keycloak admin console:        http://localhost:8180/admin"
echo "    realm: poc"
echo "    users: alice / alice123   bob / bob123"
echo ""
echo "  Agent API:     http://localhost:8082"
echo "  OBO Exchange:  http://localhost:8081"
echo "  MCP Mock:      http://localhost:8083"
echo "  LiteLLM:       http://localhost:4001"
echo "  Prometheus:    http://localhost:9090"
echo "  Grafana:       http://localhost:3000"
echo ""
echo "  Test:   ./scripts/test-flow.sh"
echo "  Logs:   ./scripts/logs.sh [service]"
echo "  Stop:   ./scripts/stop.sh"
echo "════════════════════════════════════════════════════"
