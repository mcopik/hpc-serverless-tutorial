#!/usr/bin/env bash
# Prepare OpenWhisk for the tutorial: download everything, deploy nothing.
#
#   ./openwhisk/prepare.sh
#
# Steps (each is skipped when already done, so the script can be rerun after a failure):
#   1. download kind, kubectl, helm, wsk, crane, and the OpenWhisk chart into openwhisk/deps/
#   2. start the local Docker registry (localhost:5001)
#   3. copy OpenWhisk's images from Docker Hub into the registry (about 3 GB, once)
#   4. configure wsk
#   5. build the 210.thumbnailer function image with SeBS and push it to the registry
#
# On the day, ./openwhisk/deploy.sh creates the cluster and deploys OpenWhisk from the
# registry, without downloading anything.
set -euo pipefail
source "$(dirname "$0")/env.sh"

# --------------------------------------------------------------------- 1. tools
info "1/5 Tools in $DEPS_DIR"
install_tools
ok "kind $(kind version | awk '{print $2}'), kubectl ${KUBECTL_VERSION}, helm ${HELM_VERSION}, wsk ${WSK_VERSION}, crane ${CRANE_VERSION}"

# --------------------------------------------------------------------- 2. registry
info "2/5 Local registry localhost:${REGISTRY_PORT}"
start_registry
ok "registry container $REGISTRY_NAME"

# --------------------------------------------------------------------- 3. images
info "3/5 OpenWhisk's images into the registry (about 3 GB the first time)"
copy_images
ok "${#OPENWHISK_IMAGES[@]} images in the registry"

# --------------------------------------------------------------------- 4. wsk
info "4/5 wsk configuration (${WSK_CONFIG_FILE:-$HOME/.wskprops})"
wsk property set --apihost "localhost:${OPENWHISK_PORT}" --auth "$WSK_AUTH" >/dev/null
ok "wsk talks to localhost:${OPENWHISK_PORT}"

# --------------------------------------------------------------------- 5. seed
info "5/5 Function image of 210.thumbnailer (built with SeBS, pushed to the registry)"
if ! command -v python >/dev/null || ! python -c "import sebs" 2>/dev/null; then
  fail "SeBS is not available; in the tutorial directory, run: uv run --project serverless-benchmarks ./openwhisk/prepare.sh"
fi
python "$TUTORIAL_DIR/openwhisk/seed.py"

echo
ok "Prepared. On the day: ./openwhisk/deploy.sh (Thursday's first notebook cell runs it)."
