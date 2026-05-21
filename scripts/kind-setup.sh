#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# kind-setup.sh — Create a Kind cluster and deploy all agent-sandbox
# components needed to run langchain-k8s integration tests.
#
# Usage:
#   ./scripts/kind-setup.sh                        # full setup
#   SKIP_CLUSTER=1 ./scripts/kind-setup.sh         # skip cluster creation entirely
#   REUSE_CLUSTER=1 ./scripts/kind-setup.sh        # reuse cluster if it exists
#
# Prerequisites:
#   - kind   (https://kind.sigs.k8s.io/)
#   - kubectl
#   - docker
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

CLUSTER_NAME="${CLUSTER_NAME:-langchain-k8s}"
NAMESPACE="agent-sandbox-system"
AGENT_SANDBOX_VERSION="${AGENT_SANDBOX_VERSION:-v0.4.6}"

# Colours for output
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m' # No colour

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Pre-flight checks ────────────────────────────────────────────────

for cmd in kind kubectl docker; do
    if ! command -v "$cmd" &>/dev/null; then
        error "$cmd is required but not installed."
        exit 1
    fi
done

# ── 1. Kind cluster ──────────────────────────────────────────────────

if [[ "${SKIP_CLUSTER:-}" == "1" ]]; then
    info "Skipping cluster creation (SKIP_CLUSTER=1)"
elif [[ "${REUSE_CLUSTER:-}" == "1" ]] && kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    info "Kind cluster '${CLUSTER_NAME}' already exists — reusing (REUSE_CLUSTER=1)"
else
    if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
        warn "Kind cluster '${CLUSTER_NAME}' already exists — deleting"
        kind delete cluster --name "${CLUSTER_NAME}"
    fi
    info "Creating Kind cluster '${CLUSTER_NAME}'"
    kind create cluster \
        --name "${CLUSTER_NAME}" \
        --wait 60s
fi

info "Setting kubectl context to kind-${CLUSTER_NAME}"
kind export kubeconfig --name "${CLUSTER_NAME}" &>/dev/null
kubectl cluster-info --context "kind-${CLUSTER_NAME}" &>/dev/null

# ── 2. Agent-sandbox controller + CRDs ───────────────────────────────

info "Removing legacy StatefulSet controller (if present, required for v0.2.x migration)"
kubectl delete statefulset agent-sandbox-controller -n "${NAMESPACE}" --ignore-not-found

info "Installing agent-sandbox controller ${AGENT_SANDBOX_VERSION} (core manifests)"
kubectl apply -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/manifest.yaml"

info "Installing agent-sandbox extensions ${AGENT_SANDBOX_VERSION} (SandboxTemplate, SandboxClaim, WarmPool CRDs)"
kubectl apply -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/extensions.yaml"

info "Enabling extensions in the controller"
kubectl patch deployment agent-sandbox-controller \
    -n "${NAMESPACE}" \
    -p '{"spec": {"template": {"spec": {"containers": [{"name": "agent-sandbox-controller", "args": ["--extensions=true"]}]}}}}'

info "Waiting for controller to be ready"
kubectl rollout status deployment/agent-sandbox-controller \
    -n "${NAMESPACE}" --timeout=120s

# ── 3. Sandbox router ────────────────────────────────────────────────

info "Deploying sandbox-router"
kubectl apply -f "${PROJECT_DIR}/k8s/sandbox-router.yaml"

info "Waiting for sandbox-router to be ready"
kubectl rollout status deployment/sandbox-router \
    -n "${NAMESPACE}" --timeout=120s

# ── 4. SandboxTemplate ──────────────────────────────────────────────

info "Creating SandboxTemplate 'python-sandbox-template'"
kubectl apply -f "${PROJECT_DIR}/k8s/sandbox-template.yaml"

# ── 5. Verification ─────────────────────────────────────────────────

info "Verifying CRDs"
kubectl get crd sandboxes.agents.x-k8s.io &>/dev/null \
    && info "  ✓ sandboxes.agents.x-k8s.io" \
    || error "  ✗ sandboxes CRD not found"

kubectl get crd sandboxclaims.extensions.agents.x-k8s.io &>/dev/null \
    && info "  ✓ sandboxclaims.extensions.agents.x-k8s.io" \
    || error "  ✗ sandboxclaims CRD not found"

kubectl get crd sandboxtemplates.extensions.agents.x-k8s.io &>/dev/null \
    && info "  ✓ sandboxtemplates.extensions.agents.x-k8s.io" \
    || error "  ✗ sandboxtemplates CRD not found"

info "Verifying pods in ${NAMESPACE}"
kubectl get pods -n "${NAMESPACE}"

info ""
info "╔══════════════════════════════════════════════════════════════╗"
info "║  Kind cluster '${CLUSTER_NAME}' is ready for integration tests.  ║"
info "║                                                              ║"
info "║  Run tests:                                                  ║"
info "║    uv run pytest tests/integration/ -v -m integration        ║"
info "║                                                              ║"
info "║  Tear down:                                                  ║"
info "║    ./scripts/kind-teardown.sh                                ║"
info "╚══════════════════════════════════════════════════════════════╝"
