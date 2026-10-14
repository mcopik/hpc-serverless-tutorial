# Settings and helpers shared by the OpenWhisk scripts.
#
# Every setting can be overridden with an environment variable, e.g. to run a second
# cluster next to an existing one:
#   TUTORIAL_KIND_CLUSTER=test TUTORIAL_REGISTRY_PORT=5002 TUTORIAL_OPENWHISK_PORT=31002 ./openwhisk/setup.sh

TUTORIAL_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# Tools (kind, kubectl, helm, wsk, crane) and the OpenWhisk chart, inside this repository.
DEPS_DIR=${OPENWHISK_DEPS:-$TUTORIAL_DIR/openwhisk/deps}
CLUSTER_NAME=${TUTORIAL_KIND_CLUSTER:-phdopen}
REGISTRY_NAME=${TUTORIAL_REGISTRY_NAME:-${CLUSTER_NAME}-registry}
REGISTRY_PORT=${TUTORIAL_REGISTRY_PORT:-5001}
OPENWHISK_PORT=${TUTORIAL_OPENWHISK_PORT:-31001}
NAMESPACE=openwhisk
RELEASE=owdev

# Versions tested together. The kind node image is the default of kind 0.22.
KIND_VERSION=v0.22.0
KIND_NODE_IMAGE=kindest/node:v1.29.2@sha256:51a1434a5397193442f0be2a297b488b6c919ce8a3931be0ce822606ea5ca245
KUBECTL_VERSION=v1.29.2
HELM_VERSION=v3.14.4
WSK_VERSION=1.2.0
CRANE_VERSION=v0.20.2                                           # google/go-containerregistry: copies images between registries
OPENWHISK_CHART_COMMIT=5a5a2168c9c0342c026ca137f96fa9d2c8ae2748 # apache/openwhisk-deploy-kube

# The Docker Hub images of an OpenWhisk deployment with this chart (from `helm template`, plus
# the prewarmed Node.js runtime). prepare.sh copies them into the local registry, which
# deploy.sh makes the cluster's Docker Hub mirror: deploying downloads nothing.
OPENWHISK_IMAGES=(
  apache/couchdb:2.3
  busybox:latest
  nginx:1.21.1
  redis:4.0
  zookeeper:3.4
  wurstmeister/kafka:2.12-2.3.1
  openwhisk/controller:ef725a6
  openwhisk/invoker:ef725a6
  openwhisk/ow-utils:ef725a6
  openwhisk/apigateway:1.0.0
  openwhisk/alarmprovider:2.3.0
  openwhisk/kafkaprovider:2.1.0
  openwhisk/action-nodejs-v14:1.20.0
)
# The default credentials of the guest namespace in openwhisk-deploy-kube.
WSK_AUTH=23bc46b1-71f6-4ed5-8c54-816aa4f8c502:123zO3xZCLrMN6v2BKK1dXYFpXlPkccOFqm12CdAsMgRU4VrNZ9lyGVCGuMDGIwP

export PATH="$DEPS_DIR:$PATH"

info() { printf '\033[1m==> %s\033[0m\n' "$*"; }
ok() { printf '\033[32m✔\033[0m %s\n' "$*"; }
fail() {
  printf '\033[31m✘ %s\033[0m\n' "$*" >&2
  exit 1
}

kubectl_ow() { kubectl --context "kind-${CLUSTER_NAME}" -n "$NAMESPACE" "$@"; }

cluster_exists() { kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; }

registry_running() { [ "$(docker inspect -f '{{.State.Running}}' "$REGISTRY_NAME" 2>/dev/null)" = "true" ]; }

# Succeeds when the OpenWhisk API answers for the guest namespace.
openwhisk_api_ready() {
  curl -sk -o /dev/null -w '%{http_code}' -u "$WSK_AUTH" \
    "https://localhost:${OPENWHISK_PORT}/api/v1/namespaces/_/actions?limit=1" 2>/dev/null | grep -q '^200$'
}

# Succeeds when the controller has a healthy invoker: it sends user actions only to invokers
# that passed their self-test (a health test action). The controller reports the count itself.
invoker_healthy() {
  local count
  count=$(curl -sk -m 5 "https://localhost:${OPENWHISK_PORT}/invokers/healthy/count" 2>/dev/null)
  [[ "$count" =~ ^[0-9]+$ ]] && [ "$count" -ge 1 ]
}

# Waits until OpenWhisk is ready to run actions: the API answers and a healthy invoker is up.
wait_for_openwhisk() {
  local timeout=${1:-900} waited=0
  until openwhisk_api_ready && invoker_healthy; do
    if [ "$waited" -ge "$timeout" ]; then
      return 1
    fi
    if [ $((waited % 60)) -eq 0 ]; then
      local api=no invoker=no
      openwhisk_api_ready && api=yes
      invoker_healthy && invoker=yes
      echo "   waiting for OpenWhisk ($((waited / 60)) min so far): API ready: $api, invoker healthy: $invoker," \
        "pods not running: $(kubectl_ow get pods --no-headers 2>/dev/null | awk '$3 != "Running" && $3 != "Completed" {n++} END {print n+0}')"
    fi
    sleep 10
    waited=$((waited + 10))
  done
}

# ---------------------------------------------------------------------------
# Used by prepare.sh and deploy.sh: OpenWhisk's images come from the local registry.
# ---------------------------------------------------------------------------

# The image's repository in the registry, named as on Docker Hub (busybox -> library/busybox).
mirror_path() {
  case "$1" in
  */*) echo "$1" ;;
  *) echo "library/$1" ;;
  esac
}

# Prints the OpenWhisk images that are not in the local registry yet.
missing_images() {
  local image
  for image in "${OPENWHISK_IMAGES[@]}"; do
    crane digest --insecure "localhost:${REGISTRY_PORT}/$(mirror_path "$image")" >/dev/null 2>&1 || echo "$image"
  done
}

# Downloads kind, kubectl, helm, wsk, crane, and the OpenWhisk chart into $DEPS_DIR (skips what's there).
install_tools() {
  command -v docker >/dev/null || fail "docker not found"
  docker info >/dev/null 2>&1 || fail "cannot reach Docker: is it running, and can you use it without sudo?"
  command -v git >/dev/null || fail "git not found"
  command -v curl >/dev/null || fail "curl not found"
  mkdir -p "$DEPS_DIR"
  local os arch tmp asset
  case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=darwin ;;
  *) fail "unsupported system $(uname -s); on Windows, run this inside WSL2" ;;
  esac
  case "$(uname -m)" in
  x86_64 | amd64) arch=amd64 ;;
  arm64 | aarch64) arch=arm64 ;;
  *) fail "unsupported architecture $(uname -m)" ;;
  esac
  download() { # url destination
    curl -fsSL --retry 3 -o "$2" "$1" || fail "download failed: $1"
  }

  if [ ! -x "$DEPS_DIR/kind" ]; then
    download "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-${os}-${arch}" "$DEPS_DIR/kind"
    chmod +x "$DEPS_DIR/kind"
  fi
  if [ ! -x "$DEPS_DIR/kubectl" ]; then
    download "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/${os}/${arch}/kubectl" "$DEPS_DIR/kubectl"
    chmod +x "$DEPS_DIR/kubectl"
  fi
  if [ ! -x "$DEPS_DIR/helm" ]; then
    tmp=$(mktemp -d)
    download "https://get.helm.sh/helm-${HELM_VERSION}-${os}-${arch}.tar.gz" "$tmp/helm.tar.gz"
    tar -xzf "$tmp/helm.tar.gz" -C "$tmp"
    mv "$tmp/${os}-${arch}/helm" "$DEPS_DIR/helm"
    rm -rf "$tmp"
  fi
  if [ ! -x "$DEPS_DIR/wsk" ]; then
    tmp=$(mktemp -d)
    if [ "$os" = linux ]; then
      download "https://github.com/apache/openwhisk-cli/releases/download/${WSK_VERSION}/OpenWhisk_CLI-${WSK_VERSION}-linux-${arch}.tgz" "$tmp/wsk.tgz"
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
  if [ ! -x "$DEPS_DIR/crane" ]; then
    tmp=$(mktemp -d)
    case "$os-$arch" in
    linux-amd64) asset=Linux_x86_64 ;;
    linux-arm64) asset=Linux_arm64 ;;
    darwin-amd64) asset=Darwin_x86_64 ;;
    darwin-arm64) asset=Darwin_arm64 ;;
    esac
    download "https://github.com/google/go-containerregistry/releases/download/${CRANE_VERSION}/go-containerregistry_${asset}.tar.gz" "$tmp/crane.tar.gz"
    tar -xzf "$tmp/crane.tar.gz" -C "$tmp" crane
    mv "$tmp/crane" "$DEPS_DIR/crane"
    rm -rf "$tmp"
  fi
  if [ ! -d "$DEPS_DIR/openwhisk-deploy-kube/.git" ]; then
    git clone -q https://github.com/apache/openwhisk-deploy-kube.git "$DEPS_DIR/openwhisk-deploy-kube"
  fi
  git -C "$DEPS_DIR/openwhisk-deploy-kube" checkout -q "$OPENWHISK_CHART_COMMIT"
}

# Compressed size of an image's amd64 variant on Docker Hub, in MB (0 if unknown).
image_size_mb() {
  crane manifest --platform linux/amd64 "docker.io/$1" 2>/dev/null |
    tr ',' '\n' | awk -F: '/"size"/ {gsub(/[^0-9]/, "", $2); s += $2} END {printf "%d\n", s / 1e6}'
}

# Data stored in the registry, in MB (grows while an image is copied into it).
registry_mb() {
  docker exec "$REGISTRY_NAME" du -sk /var/lib/registry 2>/dev/null | awk '{printf "%d\n", $1 / 1024}'
}

# Copies OpenWhisk's images that aren't in the registry yet from Docker Hub (3 attempts each),
# reporting each image's size and the download speed.
# Only the amd64 images are copied.
copy_images() {
  local image attempt log pid size start before done_mb now elapsed total=0 copied=0 count=0
  local -a images=() sizes=()
  for image in $(missing_images); do
    images+=("$image")
    sizes+=("$(image_size_mb "$image")")
    total=$((total + ${sizes[-1]:-0}))
  done
  [ "${#images[@]}" -eq 0 ] && return 0
  echo "   ${#images[@]} images, ${total} MB to download"
  log=$(mktemp)
  for image in "${images[@]}"; do
    size=${sizes[$count]:-0}
    count=$((count + 1))
    for attempt in 1 2 3; do
      echo "   [$count/${#images[@]}] $image (${size} MB)"
      start=$(date +%s)
      before=$(registry_mb)
      crane copy --insecure --platform linux/amd64 "docker.io/$image" \
        "localhost:${REGISTRY_PORT}/$(mirror_path "$image")" >"$log" 2>&1 &
      pid=$!
      # Progress every 10 s: data that arrived in the registry, and the average speed.
      while kill -0 "$pid" 2>/dev/null; do
        sleep 10
        kill -0 "$pid" 2>/dev/null || break
        now=$(date +%s)
        done_mb=$(($(registry_mb) - before))
        echo "         ${done_mb} of ${size} MB, $(awk -v m="$done_mb" -v t="$((now - start))" 'BEGIN {printf "%.1f", m / (t > 0 ? t : 1)}') MB/s"
      done
      if wait "$pid"; then
        elapsed=$(($(date +%s) - start))
        copied=$((copied + size))
        echo "         done in ${elapsed} s ($(awk -v m="$size" -v t="$elapsed" 'BEGIN {printf "%.1f", m / (t > 0 ? t : 1)}') MB/s); ${copied} of ${total} MB in total"
        break
      fi
      if [ "$attempt" -eq 3 ]; then
        tail -n 3 "$log" >&2
        rm -f "$log"
        fail "copying $image failed 3 times; rerun the script to continue"
      fi
      echo "   (attempt $attempt failed, retrying)"
      sleep 5
    done
  done
  rm -f "$log"
  [ -z "$(missing_images)" ] || fail "images are missing from the registry; rerun the script"
}

# Starts the local registry: function images, and the cluster's Docker Hub mirror.
start_registry() {
  registry_running && return 0
  if docker inspect "$REGISTRY_NAME" >/dev/null 2>&1; then
    docker start "$REGISTRY_NAME" >/dev/null
  else
    docker run -d --restart=always -p "127.0.0.1:${REGISTRY_PORT}:5000" --network bridge \
      --name "$REGISTRY_NAME" registry:2 >/dev/null
  fi
}

# Points every node's containerd at the registry container, reached by name on the kind network:
# localhost:<port>/... (function images) and docker.io (OpenWhisk's images; what the
# registry doesn't have still comes from Docker Hub).
configure_registry_mirror() {
  local node
  for node in $(kind get nodes --name "$CLUSTER_NAME"); do
    docker exec -i "$node" sh -c "mkdir -p '/etc/containerd/certs.d/localhost:${REGISTRY_PORT}' && \
      cat > '/etc/containerd/certs.d/localhost:${REGISTRY_PORT}/hosts.toml'" <<EOF
[host."http://${REGISTRY_NAME}:5000"]
EOF
    docker exec -i "$node" sh -c "mkdir -p /etc/containerd/certs.d/docker.io && \
      cat > /etc/containerd/certs.d/docker.io/hosts.toml" <<EOF
server = "https://registry-1.docker.io"

[host."http://${REGISTRY_NAME}:5000"]
  capabilities = ["pull", "resolve"]
EOF
  done
  if [ "$(docker inspect -f '{{json .NetworkSettings.Networks.kind}}' "$REGISTRY_NAME")" = "null" ]; then
    docker network connect kind "$REGISTRY_NAME"
  fi
}
