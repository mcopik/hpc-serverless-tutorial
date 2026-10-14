#!/usr/bin/env bash
# Stop the OpenWhisk cluster and the registry to free memory. Nothing is deleted:
# ./openwhisk/start.sh brings everything back.
set -euo pipefail
source "$(dirname "$0")/env.sh"

cluster_exists || fail "no cluster $CLUSTER_NAME"
# shellcheck disable=SC2046
docker stop $(kind get nodes --name "$CLUSTER_NAME") >/dev/null
docker stop "$REGISTRY_NAME" >/dev/null 2>&1 || true
ok "stopped cluster $CLUSTER_NAME and registry $REGISTRY_NAME"
