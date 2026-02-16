#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# kind-teardown.sh — Delete the Kind cluster used for integration tests.
#
# Usage:
#   ./scripts/kind-teardown.sh
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-langchain-k8s}"

GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

info() { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }

if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    info "Deleting Kind cluster '${CLUSTER_NAME}'"
    kind delete cluster --name "${CLUSTER_NAME}"
    info "Done."
else
    warn "Kind cluster '${CLUSTER_NAME}' does not exist — nothing to do."
fi
