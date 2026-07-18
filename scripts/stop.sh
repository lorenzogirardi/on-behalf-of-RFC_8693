#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if command -v podman-compose &>/dev/null; then
  podman-compose down
elif docker compose version &>/dev/null 2>&1; then
  docker compose down
else
  docker-compose down
fi
echo "Stack stopped."
