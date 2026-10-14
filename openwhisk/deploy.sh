#!/usr/bin/env bash
# Deploy OpenWhisk on a new kind cluster, from the images that prepare.sh put into the
# local registry; Thursday's first notebook cell runs it during the talk.
# If the cluster exists already, start it instead (like start.sh).
#
#   ./openwhisk/deploy.sh                   # Linux, WSL2, Intel macOS
#   ./openwhisk/deploy.sh --apple-silicon   # Apple Silicon: x86 nodes under Rosetta (detected anyway)
set -euo pipefail
source "$(dirname "$0")/env.sh"

APPLE_SILICON=false
if [ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ]; then
  APPLE_SILICON=true   # detected, so that the notebook's first cell needs no option
fi
for arg in "$@"; do
  case "$arg" in
    --apple-silicon) APPLE_SILICON=true ;;
    *) fail "unknown option $arg (usage: $0 [--apple-silicon])" ;;
  esac
done

# Without ./openwhisk/prepare.sh, do its downloads now (slower; the thumbnailer's function
# image is then built by the notebook instead).
if [ ! -x "$DEPS_DIR/kind" ] || [ ! -x "$DEPS_DIR/kubectl" ] || [ ! -x "$DEPS_DIR/helm" ] \
  || [ ! -x "$DEPS_DIR/wsk" ] || [ ! -x "$DEPS_DIR/crane" ] || [ ! -d "$DEPS_DIR/openwhisk-deploy-kube/.git" ]; then
  info "Downloading the tools into $DEPS_DIR (prepare.sh wasn't run)"
  install_tools
fi
docker info >/dev/null 2>&1 || fail "cannot reach Docker: is it running?"

info "Registry localhost:${REGISTRY_PORT}"
start_registry
missing=$(missing_images | wc -l | tr -d ' ')
if [ "$missing" -gt 0 ]; then
  info "Copying $missing of OpenWhisk's images from Docker Hub into the registry (prepare.sh wasn't finished; up to 3 GB)"
  copy_images
fi

if cluster_exists; then
  info "Starting the existing cluster $CLUSTER_NAME"
  # shellcheck disable=SC2046
  docker start $(kind get nodes --name "$CLUSTER_NAME") >/dev/null
else
  info "Creating the kind cluster $CLUSTER_NAME"
  if [ "$APPLE_SILICON" = true ]; then
    # OpenWhisk's images exist only for x86: run the whole node as x86 under Rosetta.
    docker pull --platform linux/amd64 "$KIND_NODE_IMAGE"
  fi
  if [ "$(uname -s)" = Linux ] && [ "$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || echo 512)" -lt 512 ]; then
    echo "   note: fs.inotify.max_user_instances is $(cat /proc/sys/fs/inotify/max_user_instances); if the nodes don't start,"
    echo "   run: sudo sysctl fs.inotify.max_user_instances=512"
  fi
  kind create cluster --name "$CLUSTER_NAME" --image "$KIND_NODE_IMAGE" --wait 5m --config - <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
containerdConfigPatches:
- |-
  [plugins."io.containerd.grpc.v1.cri".registry]
    config_path = "/etc/containerd/certs.d"
nodes:
- role: control-plane
- role: worker
  extraPortMappings:
    - hostPort: ${OPENWHISK_PORT}
      containerPort: ${OPENWHISK_PORT}
      listenAddress: "127.0.0.1"
- role: worker
EOF
fi
configure_registry_mirror
kubectl --context "kind-${CLUSTER_NAME}" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-public
data:
  localRegistryHosting.v1: |
    host: "localhost:${REGISTRY_PORT}"
EOF

if ! helm --kube-context "kind-${CLUSTER_NAME}" status "$RELEASE" -n "$NAMESPACE" >/dev/null 2>&1; then
  info "Installing OpenWhisk"
  helm --kube-context "kind-${CLUSTER_NAME}" install "$RELEASE" \
    "$DEPS_DIR/openwhisk-deploy-kube/helm/openwhisk" \
    -n "$NAMESPACE" --create-namespace \
    -f "$TUTORIAL_DIR/openwhisk/values.yaml" \
    --set "whisk.ingress.apiHostPort=${OPENWHISK_PORT}" \
    --set "nginx.httpsNodePort=${OPENWHISK_PORT}" >/dev/null
fi

info "Waiting for OpenWhisk (a few minutes)"
wait_for_openwhisk 1200 || fail "OpenWhisk is not ready after 20 minutes; see ./openwhisk/status.sh"
wsk property set --apihost "localhost:${OPENWHISK_PORT}" --auth "$WSK_AUTH" >/dev/null
ok "OpenWhisk is ready at https://localhost:${OPENWHISK_PORT}"
