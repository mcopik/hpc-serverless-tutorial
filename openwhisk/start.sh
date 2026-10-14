#!/usr/bin/env bash
# Start the stopped OpenWhisk cluster and the registry, and wait until OpenWhisk is ready.
set -euo pipefail
source "$(dirname "$0")/env.sh"

cluster_exists || fail "no cluster $CLUSTER_NAME: run ./openwhisk/setup.sh first"

info "Starting registry and cluster nodes"
docker start "$REGISTRY_NAME" >/dev/null
docker start $(kind get nodes --name "$CLUSTER_NAME") >/dev/null

info "Waiting for OpenWhisk (usually 2-5 minutes)"
wait_for_openwhisk 900 || fail "OpenWhisk is not ready after 15 minutes; see ./openwhisk/status.sh"
ok "OpenWhisk is ready at https://localhost:${OPENWHISK_PORT}"
