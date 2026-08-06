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
# Requires v0.5.2 or newer: the all-in-one release asset used below did not
# exist before then, and the SDK talks v1beta1 only from v0.5.2 onward.
AGENT_SANDBOX_VERSION="${AGENT_SANDBOX_VERSION:-v0.5.4}"
# Number of pre-warmed pods in the SandboxWarmPool.  0 (default) means pure
# on-demand cold start; raise it to exercise or measure the warm path.
WARMPOOL_REPLICAS="${WARMPOOL_REPLICAS:-0}"
WARMPOOL_NAME="python-sandbox-pool"

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

# Single all-in-one asset: core controller + the SandboxTemplate,
# SandboxClaim and SandboxWarmPool CRDs, with `--extensions` already set on
# the controller Deployment.  (v0.5.2 renamed the old core `manifest.yaml`
# to `sandbox.yaml` and added this bundle.)  Note there is deliberately no
# `kubectl patch` to enable extensions any more: the bundle's Deployment
# already carries `--extensions`, and patching the args array would replace
# it wholesale, silently dropping `--leader-elect=true`.
RELEASE_URL="https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}"

info "Installing agent-sandbox ${AGENT_SANDBOX_VERSION} (controller + extensions)"
kubectl apply -f "${RELEASE_URL}/sandbox-with-extensions.yaml"

# Must complete before any claim or warm pool is created: v1alpha1 is served
# through a conversion webhook hosted by this controller, and resources
# applied before it is serving fail with an opaque conversion error.
info "Waiting for controller to be ready"
kubectl rollout status deployment/agent-sandbox-controller \
    -n "${NAMESPACE}" --timeout=120s

# ── 3. Sandbox router ────────────────────────────────────────────────

# Applied through the k8s/dev-kind overlay, which adds
# ALLOW_UNAUTHENTICATED_ROUTER=true.  The base k8s/sandbox-router.yaml sets no
# auth env vars, so on its own the router refuses to start (agent-sandbox
# v0.5.0 hardening, kubernetes-sigs#755) — that keeps the reusable manifest
# secure-by-default and confines the insecure setting to a directory named
# for the only place it is safe.  See k8s/dev-kind/kustomization.yaml for why
# we opt out instead of setting a token, and for the --load-restrictor flag.
info "Deploying sandbox-router (dev-kind overlay: unauthenticated)"
kubectl kustomize --load-restrictor LoadRestrictionsNone "${PROJECT_DIR}/k8s/dev-kind" \
    | kubectl apply -f -

info "Waiting for sandbox-router to be ready"
kubectl rollout status deployment/sandbox-router \
    -n "${NAMESPACE}" --timeout=120s

# ── 4. SandboxTemplate ──────────────────────────────────────────────

info "Creating SandboxTemplate 'python-sandbox-template'"
kubectl apply -f "${PROJECT_DIR}/k8s/sandbox-template.yaml"

# ── 5. SandboxWarmPool ──────────────────────────────────────────────
#
# Mandatory, not an optimisation: a v1beta1 SandboxClaim requires
# spec.warmPoolRef and has no template reference at all.

info "Creating SandboxWarmPool '${WARMPOOL_NAME}' (replicas=${WARMPOOL_REPLICAS})"
kubectl apply -f "${PROJECT_DIR}/k8s/sandbox-warmpool.yaml"

if [[ "${WARMPOOL_REPLICAS}" != "0" ]]; then
    info "Scaling warm pool to ${WARMPOOL_REPLICAS} pre-warmed pod(s)"
    kubectl patch sandboxwarmpool "${WARMPOOL_NAME}" \
        -n "${NAMESPACE}" \
        --type=merge \
        -p "{\"spec\": {\"replicas\": ${WARMPOOL_REPLICAS}}}"
    kubectl wait --for=condition=Ready "sandboxwarmpool/${WARMPOOL_NAME}" \
        -n "${NAMESPACE}" --timeout=180s \
        || warn "warm pool did not report Ready — continuing, tests will cold-start"
fi

# ── 6. Verification ─────────────────────────────────────────────────

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

kubectl get crd sandboxwarmpools.extensions.agents.x-k8s.io &>/dev/null \
    && info "  ✓ sandboxwarmpools.extensions.agents.x-k8s.io" \
    || error "  ✗ sandboxwarmpools CRD not found"

# The SDK addresses claims as v1beta1 only.  Against a pre-v0.5.0 CRD every
# create returns a bare 404 with nothing pointing at the cause, so check the
# served versions explicitly rather than letting the tests guess.
kubectl get crd sandboxclaims.extensions.agents.x-k8s.io \
    -o jsonpath='{.spec.versions[*].name}' 2>/dev/null | grep -q v1beta1 \
    && info "  ✓ sandboxclaims serves v1beta1" \
    || error "  ✗ sandboxclaims does not serve v1beta1 — k8s-agent-sandbox >=0.5.2 will fail"

info "Verifying warm pool"
kubectl get sandboxwarmpool -n "${NAMESPACE}"

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
