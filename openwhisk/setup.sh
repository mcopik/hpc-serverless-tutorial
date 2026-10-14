#!/usr/bin/env bash
# Install OpenWhisk on a local kind cluster, with a local image registry, for the SeBS tutorial.
#
#   ./openwhisk/setup.sh                   # Linux, WSL2, Intel macOS
#   ./openwhisk/setup.sh --apple-silicon   # Apple Silicon: x86 nodes under Rosetta
#
# Steps (each is skipped when already done, so the script can be rerun after a failure):
#   1. download kind, kubectl, helm, and wsk into $DEPS_DIR (no root needed)
#   2. start a local Docker registry (localhost:5001): function images never go to Docker Hub
#   3. create the kind cluster; its nodes pull localhost:5001 from the registry container
#   4. deploy OpenWhisk with Helm and wait until it's ready (10-20 min the first time)
#   5. configure wsk (~/.wskprops)
#   6. build the 210.thumbnailer function image with SeBS and push it to the registry
set -euo pipefail
source "$(dirname "$0")/env.sh"

APPLE_SILICON=false
for arg in "$@"; do
  case "$arg" in
    --apple-silicon) APPLE_SILICON=true ;;
    *) fail "unknown option $arg (usage: $0 [--apple-silicon])" ;;
  esac
done

command -v docker >/dev/null || fail "docker not found"
docker info >/dev/null 2>&1 || fail "cannot reach Docker: is it running, and can you use it without sudo?"
command -v git >/dev/null || fail "git not found"
command -v curl >/dev/null || fail "curl not found"

# --------------------------------------------------------------------- 1. tools
info "1/6 Tools in $DEPS_DIR"
mkdir -p "$DEPS_DIR"
case "$(uname -s)" in
  Linux) OS=linux ;;
  Darwin) OS=darwin ;;
  *) fail "unsupported system $(uname -s); on Windows, run this inside WSL2" ;;
esac
case "$(uname -m)" in
  x86_64 | amd64) ARCH=amd64 ;;
  arm64 | aarch64) ARCH=arm64 ;;
  *) fail "unsupported architecture $(uname -m)" ;;
esac

download() {  # url destination
  curl -fsSL --retry 3 -o "$2" "$1" || fail "download failed: $1"
}

if [ ! -x "$DEPS_DIR/kind" ]; then
  download "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-${OS}-${ARCH}" "$DEPS_DIR/kind"
  chmod +x "$DEPS_DIR/kind"
fi
if [ ! -x "$DEPS_DIR/kubectl" ]; then
  download "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/${OS}/${ARCH}/kubectl" "$DEPS_DIR/kubectl"
  chmod +x "$DEPS_DIR/kubectl"
fi
if [ ! -x "$DEPS_DIR/helm" ]; then
  tmp=$(mktemp -d)
  download "https://get.helm.sh/helm-${HELM_VERSION}-${OS}-${ARCH}.tar.gz" "$tmp/helm.tar.gz"
  tar -xzf "$tmp/helm.tar.gz" -C "$tmp"
  mv "$tmp/${OS}-${ARCH}/helm" "$DEPS_DIR/helm"
  rm -rf "$tmp"
fi
if [ ! -x "$DEPS_DIR/wsk" ]; then
  tmp=$(mktemp -d)
  if [ "$OS" = linux ]; then
    download "https://github.com/apache/openwhisk-cli/releases/download/${WSK_VERSION}/OpenWhisk_CLI-${WSK_VERSION}-linux-${ARCH}.tgz" "$tmp/wsk.tgz"
    tar -xzf "$tmp/wsk.tgz" -C "$tmp"
  else
    # No native Apple Silicon build: the Intel binary runs under Rosetta.
    download "https://github.com/apache/openwhisk-cli/releases/download/${WSK_VERSION}/OpenWhisk_CLI-${WSK_VERSION}-mac-amd64.zip" "$tmp/wsk.zip"
    (cd "$tmp" && unzip -q wsk.zip)
  fi
  mv "$tmp/wsk" "$DEPS_DIR/wsk"
  chmod +x "$DEPS_DIR/wsk"
  rm -rf "$tmp"
fi
if [ ! -d "$DEPS_DIR/openwhisk-deploy-kube/.git" ]; then
  git clone -q https://github.com/apache/openwhisk-deploy-kube.git "$DEPS_DIR/openwhisk-deploy-kube"
fi
git -C "$DEPS_DIR/openwhisk-deploy-kube" checkout -q "$OPENWHISK_CHART_COMMIT"
ok "kind $(kind version | awk '{print $2}'), kubectl ${KUBECTL_VERSION}, helm ${HELM_VERSION}, wsk ${WSK_VERSION}"

# --------------------------------------------------------------------- 2. registry
info "2/6 Local registry localhost:${REGISTRY_PORT}"
if ! registry_running; then
  docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
  docker run -d --restart=always -p "127.0.0.1:${REGISTRY_PORT}:5000" --network bridge \
    --name "$REGISTRY_NAME" registry:2 >/dev/null
fi
ok "registry container $REGISTRY_NAME"

# --------------------------------------------------------------------- 3. cluster
info "3/6 kind cluster $CLUSTER_NAME"
if ! cluster_exists; then
  if [ "$APPLE_SILICON" = true ]; then
    # OpenWhisk's images exist only for x86: run the whole node as x86 under Rosetta.
    docker pull --platform linux/amd64 "$KIND_NODE_IMAGE"
  fi
  if [ "$OS" = linux ] && [ "$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || echo 512)" -lt 512 ]; then
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
# The nodes reach the registry by its container name on the kind network,
# but under the same name as the host: localhost:<port>.
for node in $(kind get nodes --name "$CLUSTER_NAME"); do
  docker exec -i "$node" sh -c "mkdir -p '/etc/containerd/certs.d/localhost:${REGISTRY_PORT}' && \
    cat > '/etc/containerd/certs.d/localhost:${REGISTRY_PORT}/hosts.toml'" <<EOF
[host."http://${REGISTRY_NAME}:5000"]
EOF
done
if [ "$(docker inspect -f '{{json .NetworkSettings.Networks.kind}}' "$REGISTRY_NAME")" = "null" ]; then
  docker network connect kind "$REGISTRY_NAME"
fi
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
ok "cluster with $(kind get nodes --name "$CLUSTER_NAME" | wc -l | tr -d ' ') nodes"

# --------------------------------------------------------------------- 4. OpenWhisk
info "4/6 OpenWhisk (first time: 10-20 minutes, mostly image downloads)"
if ! helm --kube-context "kind-${CLUSTER_NAME}" status "$RELEASE" -n "$NAMESPACE" >/dev/null 2>&1; then
  helm --kube-context "kind-${CLUSTER_NAME}" install "$RELEASE" \
    "$DEPS_DIR/openwhisk-deploy-kube/helm/openwhisk" \
    -n "$NAMESPACE" --create-namespace \
    -f "$TUTORIAL_DIR/openwhisk/values.yaml" \
    --set "whisk.ingress.apiHostPort=${OPENWHISK_PORT}" \
    --set "nginx.httpsNodePort=${OPENWHISK_PORT}" >/dev/null
fi
wait_for_openwhisk 1800 || fail "OpenWhisk is not ready after 30 minutes; see ./openwhisk/status.sh"
ok "OpenWhisk API at https://localhost:${OPENWHISK_PORT}"

# --------------------------------------------------------------------- 5. wsk
info "5/6 wsk configuration (${WSK_CONFIG_FILE:-$HOME/.wskprops})"
wsk property set --apihost "localhost:${OPENWHISK_PORT}" --auth "$WSK_AUTH" >/dev/null
wsk -i action list >/dev/null || fail "wsk cannot reach OpenWhisk"
ok "wsk -i action list works"

# --------------------------------------------------------------------- 6. seed
info "6/6 Function image of 210.thumbnailer (built with SeBS, pushed to the registry)"
if ! command -v python >/dev/null || ! python -c "import sebs" 2>/dev/null; then
  fail "SeBS is not available: activate its environment (source ~/phdopen/serverless-benchmarks/.venv/bin/activate) and rerun"
fi
python "$TUTORIAL_DIR/openwhisk/seed.py"

echo
ok "OpenWhisk is ready. Daily use: ./openwhisk/start.sh, ./openwhisk/stop.sh, ./openwhisk/status.sh"
