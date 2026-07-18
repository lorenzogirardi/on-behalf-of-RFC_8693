#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
SVC="${1:-}"
CMD="docker compose"
command -v podman-compose &>/dev/null && CMD="podman-compose"
$CMD logs -f --tail=100 $SVC
