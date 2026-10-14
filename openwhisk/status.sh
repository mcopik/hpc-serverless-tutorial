#!/usr/bin/env bash
# Is everything up? One line per component, and the pods that aren't ready yet.
set -uo pipefail
source "$(dirname "$0")/env.sh"

status=0
check() {  # description, then a command that succeeds when the component is fine
  local what=$1
  shift
  if "$@" >/dev/null 2>&1; then
    ok "$what"
  else
    printf '\033[31m✘\033[0m %s\n' "$what"
    status=1
  fi
}

for tool in kind kubectl helm wsk; do
  check "$tool in $DEPS_DIR" test -x "$DEPS_DIR/$tool"
done
check "registry $REGISTRY_NAME running" registry_running
check "registry answers at localhost:${REGISTRY_PORT}" curl -sf "http://localhost:${REGISTRY_PORT}/v2/_catalog"
check "cluster $CLUSTER_NAME exists" cluster_exists
check "Kubernetes API reachable" kubectl --context "kind-${CLUSTER_NAME}" get nodes
check "OpenWhisk API at localhost:${OPENWHISK_PORT}" openwhisk_api_ready
check "invoker healthy (controller: /invokers/healthy/count)" invoker_healthy
check "wsk connected (wsk -i action list)" wsk -i action list

# One-time setup jobs (certificates, database, catalog packages) don't affect actions.
JOBS='gen-certs|init-couchdb|install-packages'
pods=$(kubectl_ow get pods --no-headers 2>/dev/null | awk '$3 != "Running" && $3 != "Completed"')
not_ready=$(echo "$pods" | grep -v -E "$JOBS" || true)
jobs=$(echo "$pods" | grep -E "$JOBS" || true)
if [ -n "$not_ready" ]; then
  echo
  echo "Pods not ready yet:"
  echo "$not_ready"
  status=1
fi
if [ -n "$jobs" ]; then
  echo
  echo "Unfinished setup jobs (harmless for the tutorial):"
  echo "$jobs"
fi
exit $status
