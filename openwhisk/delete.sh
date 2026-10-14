#!/usr/bin/env bash
# Remove the OpenWhisk cluster and the registry (with all function images in it).
# The tools in $DEPS_DIR stay; delete that directory yourself if you don't need them.
set -euo pipefail
source "$(dirname "$0")/env.sh"

read -r -p "Delete cluster $CLUSTER_NAME and registry $REGISTRY_NAME? [y/N] " answer
[ "$answer" = y ] || [ "$answer" = Y ] || exit 0

if cluster_exists; then
  kind delete cluster --name "$CLUSTER_NAME"
fi
docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
ok "deleted cluster $CLUSTER_NAME and registry $REGISTRY_NAME"
