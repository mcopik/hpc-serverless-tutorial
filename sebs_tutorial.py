"""Helpers for the SeBS tutorial notebooks (PhDOpen MIMUW, October 2026).

The notebooks drive SeBS through its own Python API (``sebs.SeBS``, deployment
clients, benchmarks, triggers). This module contains only the plumbing around it:
environment checks, starting object storage, peeking into the registry, Kubernetes
and OpenWhisk, turning SeBS results into DataFrames, and plotting.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests

TUTORIAL_DIR = Path(__file__).resolve().parent
CONFIG_DIR = TUTORIAL_DIR / "configs"
STATE_DIR = TUTORIAL_DIR / ".state"
RESULTS_DIR = TUTORIAL_DIR / "results"

OPENWHISK_DEPS = Path(os.environ.get("OPENWHISK_DEPS", TUTORIAL_DIR / "openwhisk" / "deps"))
OPENWHISK_NAMESPACE = "openwhisk"
# Must match openwhisk/env.sh.
REGISTRY = f"localhost:{os.environ.get('TUTORIAL_REGISTRY_PORT', '5001')}"
KIND_CLUSTER = os.environ.get("TUTORIAL_KIND_CLUSTER", "phdopen")

# GCP Cloud Functions (1st gen), Tier 1 prices in USD.
# https://cloud.google.com/functions/pricing-1stgen
GCP_PRICE_PER_GB_S = 0.0000025
GCP_PRICE_PER_GHZ_S = 0.0000100
# 1st gen allocates CPU together with memory.
GCP_CPU_GHZ = {128: 0.2, 256: 0.4, 512: 0.8, 1024: 1.4, 2048: 2.4, 4096: 4.8, 8192: 4.8}

# AWS Lambda (x86, first pricing tier, most regions) in USD.
# https://aws.amazon.com/lambda/pricing/
AWS_PRICE_PER_GB_S = 0.0000166667
AWS_PRICE_PER_REQUEST = 0.20 / 1_000_000

# Clouds the notebooks support; any other SeBS platform works the same way with its own config.
CLOUDS = ("gcp", "aws")

# The tools installed by openwhisk/prepare.sh must be visible to SeBS, which calls wsk and kubectl.
# Always, even if the directory doesn't exist yet: openwhisk/deploy.sh may create it later.
if str(OPENWHISK_DEPS) not in os.environ["PATH"].split(os.pathsep):
    os.environ["PATH"] = str(OPENWHISK_DEPS) + os.pathsep + os.environ["PATH"]

OK, FAIL, WARN = "✔", "✘", "!"


def _line(status: str, what: str, detail: str = "") -> None:
    print(f"{status} {what}" + (f": {detail}" if detail else ""))


def _run(cmd: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _docker():
    import docker

    return docker.from_env()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config(platform_name: str) -> dict:
    """Return the course's SeBS configuration template for a platform as a dict."""
    with open(CONFIG_DIR / f"{platform_name}.json") as f:
        config = json.load(f)
    if platform_name == "openwhisk":
        config["deployment"]["openwhisk"]["docker_registry"]["registry"] = REGISTRY
    if platform_name == "local":
        # Local functions run natively: SeBS's local images exist for x64 and arm64.
        config["experiments"]["architecture"] = host_architecture()
    return config


def host_architecture() -> str:
    """This machine's architecture in SeBS's naming: "x64" or "arm64"."""
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"


def _cloud_credentials(cloud: str) -> Tuple[Optional[dict], str]:
    """SeBS credentials for a cloud, and where they come from (raises if there are none).

    Your own account, or one we provide on request; both go into the same files.
    GCP: configs/gcp-credentials.json (a service account key), else GOOGLE_APPLICATION_CREDENTIALS.
    AWS: configs/aws-credentials.json ({"access_key": ..., "secret_key": ...}),
    else AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.
    Files named *credentials*.json in configs/ are never committed (.gitignore).
    """
    if cloud == "gcp":
        if (CONFIG_DIR / "gcp-credentials.json").exists():
            path, source = str(CONFIG_DIR / "gcp-credentials.json"), "configs/gcp-credentials.json"
        elif os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            path, source = os.environ["GOOGLE_APPLICATION_CREDENTIALS"], "GOOGLE_APPLICATION_CREDENTIALS"
        else:
            raise FileNotFoundError("no GCP credentials: configs/gcp-credentials.json or GOOGLE_APPLICATION_CREDENTIALS")
        if not Path(path).exists():
            raise FileNotFoundError(f"GCP credentials file {path} is missing")
        project = json.load(open(path)).get("project_id")
        return {"credentials-json": path}, f"{source}, project {project}"
    if cloud == "aws":
        path = CONFIG_DIR / "aws-credentials.json"
        if path.exists():
            return json.load(open(path)), "configs/aws-credentials.json"
        if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
            return None, "AWS_ACCESS_KEY_ID"  # SeBS reads the environment itself
        raise FileNotFoundError("no AWS credentials: configs/aws-credentials.json, "
                                "or AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
    raise ValueError(f"unknown cloud {cloud!r}; the notebooks support {', '.join(CLOUDS)}")


def cloud_config(cloud: str) -> dict:
    """SeBS configuration for a cloud: the course's template plus your credentials."""
    config = load_config(cloud)
    credentials, source = _cloud_credentials(cloud)
    if credentials is not None:
        config["deployment"][cloud]["credentials"] = credentials
    print(f"{OK} {cloud}: {source}, region {config['deployment'][cloud]['region']}")
    return config


def laptop_id() -> str:
    """A short ID of this laptop and user, the same every day."""
    import hashlib
    import getpass

    return hashlib.sha1(f"{socket.gethostname()}/{getpass.getuser()}".encode()).hexdigest()[:6]


def cloud_prefix() -> str:
    """resource_prefix for SeBS: "phd" plus this laptop's ID (or TUTORIAL_RESOURCE_PREFIX).

    SeBS reuses a deployment whose name contains the prefix, or creates a new one. A
    prefix unique to the laptop keeps students apart when several share an account.
    """
    return os.environ.get("TUTORIAL_RESOURCE_PREFIX", f"phd{laptop_id()}")


def curl_command(url: str, payload: dict) -> str:
    """The curl command that invokes a function's HTTP trigger, as SeBS does, printing the
    response headers and body, and curl's timings."""
    import shlex

    timings = ("\\n\\ncurl: DNS %{time_namelookup}s, TCP connected %{time_connect}s, "
               "TLS done %{time_appconnect}s, first byte %{time_starttransfer}s, total %{time_total}s\\n")
    return (f"curl -sk -i -X POST -H 'Content-Type: application/json' "
            f"-d {shlex.quote(json.dumps(payload))} -w {shlex.quote(timings)} {shlex.quote(url)}")


def http_trigger(deployment, func):
    """The function's HTTP trigger; created if the platform didn't make one on deployment."""
    from sebs.faas.function import Trigger

    triggers = func.triggers(Trigger.TriggerType.HTTP)
    return triggers[0] if triggers else deployment.create_trigger(func, Trigger.TriggerType.HTTP)


def show_json(obj, depth: int = 1) -> None:
    """Collapsible JSON view (falls back to pretty-printing outside Jupyter)."""
    if hasattr(obj, "serialize"):
        obj = obj.serialize()
    try:
        from IPython.display import JSON, display

        display(JSON(obj, expanded=False, root="result"))
    except Exception:
        print(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------------------
# Object storage (RustFS, through SeBS)
# ---------------------------------------------------------------------------

STORAGE_TYPE = os.environ.get("TUTORIAL_STORAGE", "rustfs")
STORAGE_PORT = int(os.environ.get("TUTORIAL_STORAGE_PORT", "9011"))


def _is_docker_desktop() -> bool:
    try:
        return "Docker Desktop" in _docker().info().get("OperatingSystem", "")
    except Exception:
        return False


def start_storage() -> dict:
    """Start the object storage through SeBS (or reuse the running instance).

    Returns the configuration that SeBS expects under deployment.<platform>.storage.
    SeBS records two addresses: `address`, used by SeBS itself and by local function
    containers on the same Docker bridge, and `external_address`, the machine's IP and
    the published port, given to functions outside the bridge (OpenWhisk pods in kind).
    Both are detected again on every call, so rerun it after your IP address changes.
    """
    from sebs import SeBS
    from sebs.sebs_types import Storage

    storage_cls = SeBS.get_storage_implementation(Storage(STORAGE_TYPE))
    config_cls = SeBS.get_storage_config_implementation(Storage(STORAGE_TYPE))
    storage = storage_cls(_docker(), None, None, True)

    STATE_DIR.mkdir(exist_ok=True)
    state = STATE_DIR / "storage.json"
    container = None
    if state.exists():
        with open(state) as f:
            cfg = config_cls.deserialize(json.load(f))
        try:
            container = _docker().containers.get(cfg.instance_id)
            if container.status != "running":
                container.start()
        except Exception:
            container = None

    if container is not None:
        # Detect both addresses again: the container's and the machine's may have changed.
        cfg.address, cfg.external_address = "", ""
        storage.config = cfg
        storage._storage_container = container
        storage.configure_connection()
    else:
        # SeBS's default storage configuration pins the image version.
        import sebs

        defaults = Path(sebs.__file__).resolve().parent.parent / "configs" / "storage.json"
        with open(defaults) as f:
            base = json.load(f)["object"][STORAGE_TYPE]
        base.update({"mapped_port": STORAGE_PORT, "data_volume": f"sebs-tutorial-{STORAGE_TYPE}"})
        cfg = config_cls.deserialize(base)
        cfg.remove_containers = False
        storage.config = cfg
        storage.start()

    if not storage.config.external_address and not _is_docker_desktop():
        # Offline: no default route. The host's address on the kind network still works there.
        network = _docker().networks.get("kind")
        gateways = [c.get("Gateway", "") for c in network.attrs["IPAM"]["Config"]]
        gateway = next((g for g in gateways if g and ":" not in g), "")
        if gateway:
            storage.config.external_address = f"{gateway}:{storage.config.mapped_port}"
    storage.check_external_address()

    serialized = storage.serialize()
    with open(state, "w") as f:
        json.dump(serialized, f, indent=2)
    return {"object": {"type": STORAGE_TYPE, STORAGE_TYPE: serialized}}


def stop_storage() -> None:
    """Stop and remove the storage container started by start_storage (the data volume stays)."""
    state = STATE_DIR / "storage.json"
    if not state.exists():
        return
    with open(state) as f:
        instance_id = json.load(f)["instance_id"]
    try:
        _docker().containers.get(instance_id).remove(force=True)
    except Exception:
        pass
    state.unlink()


# ---------------------------------------------------------------------------
# OpenWhisk REST API, registry, Kubernetes
# ---------------------------------------------------------------------------


def _wskprops() -> Dict[str, str]:
    props = {}
    path = Path(os.environ.get("WSK_CONFIG_FILE", Path.home() / ".wskprops"))
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                props[key.strip()] = value.strip()
    return props


def openwhisk_api(path: str, **params) -> dict:
    """GET from the OpenWhisk REST API, e.g. openwhisk_api("namespaces/_/actions")."""
    props = _wskprops()
    host = props.get("APIHOST", "localhost:31001")
    if not host.startswith("http"):
        host = "https://" + host
    user, password = props["AUTH"].split(":", 1)
    with warnings.catch_warnings():
        # The kind deployment uses a self-signed certificate.
        warnings.simplefilter("ignore")
        ret = requests.get(
            f"{host}/api/v1/{path}", auth=(user, password), params=params, verify=False, timeout=10
        )
    ret.raise_for_status()
    return ret.json()


def action(func) -> dict:
    """What OpenWhisk stores about an action: image, memory, timeout, version."""
    data = openwhisk_api(f"namespaces/_/actions/{func.name}")
    return {
        "name": data["name"],
        "version": data["version"],
        "image": data["exec"].get("image"),
        "memory_mb": data["limits"]["memory"],
        "timeout_s": data["limits"]["timeout"] / 1000,
        "annotations": {a["key"]: a["value"] for a in data.get("annotations", [])},
    }


def _activation_row(act: dict) -> dict:
    ann = {a["key"]: a["value"] for a in act.get("annotations", [])}
    return {
        "activationId": act["activationId"],
        "start": datetime.fromtimestamp(act["start"] / 1000),
        "waitTime": ann.get("waitTime"),
        "initTime": ann.get("initTime"),
        "duration": act.get("duration"),
        "status": act.get("response", {}).get("status"),
    }


def activations(func=None, ids: Optional[Iterable[str]] = None, last: int = 5) -> pd.DataFrame:
    """OpenWhisk activation records (times in ms), newest last.

    Pass either a function (its ``last`` activations) or activation IDs.
    """
    if ids is not None:
        rows = [_activation_row(openwhisk_api(f"namespaces/_/activations/{i}")) for i in ids]
    else:
        params = {"limit": last, "docs": "true"}
        if func is not None:
            params["name"] = func.name
        rows = [_activation_row(a) for a in openwhisk_api("namespaces/_/activations", **params)]
    df = pd.DataFrame(rows)
    return df.sort_values("start").reset_index(drop=True) if len(df) else df


def registry_catalog() -> List[str]:
    """Repositories in the local registry (Docker Registry HTTP API v2)."""
    return requests.get(f"http://{REGISTRY}/v2/_catalog", timeout=5).json()["repositories"]


def registry_tags(repository: str = "spcleth/serverless-benchmarks") -> List[str]:
    """Image tags of one repository in the local registry."""
    tags = requests.get(f"http://{REGISTRY}/v2/{repository}/tags/list", timeout=5).json()["tags"]
    return sorted(tags or [])


def image_layers(image: str) -> pd.DataFrame:
    """Layers of a local image, newest first, with their size and the step that created them."""
    rows = []
    for layer in _docker().api.history(image):
        step = layer.get("CreatedBy", "").replace("/bin/sh -c #(nop) ", "").strip()
        rows.append({"size_MB": round(layer["Size"] / 1e6, 2), "created_by": step[:110]})
    return pd.DataFrame(rows)


def image_size(image: str) -> float:
    """Size of a local image in MB."""
    return round(_docker().images.get(image).attrs["Size"] / 1e6, 1)


def _kubectl_json(*args: str) -> dict:
    ret = _run(["kubectl", "--context", f"kind-{KIND_CLUSTER}", "-n", OPENWHISK_NAMESPACE, *args, "-o", "json"])
    if ret.returncode != 0:
        raise RuntimeError(ret.stderr.strip())
    return json.loads(ret.stdout)


def pods(func=None) -> pd.DataFrame:
    """OpenWhisk pods; with a function, only the pods running its action."""
    args = ["get", "pods"]
    if func is not None:
        args += ["-l", f"openwhisk/action={func.name}"]
    rows = []
    for pod in _kubectl_json(*args)["items"]:
        rows.append(
            {
                "name": pod["metadata"]["name"],
                "phase": pod["status"]["phase"],
                "node": pod["spec"].get("nodeName"),
                "created": pod["metadata"]["creationTimestamp"],
            }
        )
    return pd.DataFrame(rows)


def pod_events(pod_name: str) -> pd.DataFrame:
    """Kubernetes events of one pod: scheduling, image pull, container start."""
    events = _kubectl_json("get", "events", "--field-selector", f"involvedObject.name={pod_name}")
    rows = [
        {"time": e.get("lastTimestamp") or e.get("eventTime"), "reason": e["reason"], "message": e["message"]}
        for e in events["items"]
    ]
    return pd.DataFrame(rows)


def kind_nodes() -> List[str]:
    return [c.name for c in _docker().containers.list(filters={"label": f"io.x-k8s.kind.cluster={KIND_CLUSTER}"})]


def drop_image(func) -> List[str]:
    """Remove the function's image from every kind node, so the next cold start must pull it.

    Equivalent to: docker exec <node> crictl rmi <image>
    """
    image = action(func)["image"]
    removed = []
    for node in kind_nodes():
        ret = _docker().containers.get(node).exec_run(["crictl", "rmi", image])
        if ret.exit_code == 0:
            removed.append(node)
    return removed


def cleanup_openwhisk(sebs_client, deployment) -> List[str]:
    """Delete the OpenWhisk actions created by this notebook; keeps the cluster and the storage."""
    prefix = f"sebs-{deployment.config.resources.resources_id}-"
    cached = sebs_client.cache_client.get_all_functions(deployment.name())
    deleted = []
    for act in openwhisk_api("namespaces/_/actions", limit=200):
        if act["name"].startswith(prefix):
            deployment.delete_function(act["name"], cached.get(act["name"], {}))
            deleted.append(act["name"])
    for name, func in cached.items():
        if name in deleted:
            sebs_client.cache_client.remove_function(
                deployment.name(), func["benchmark"], func["config"]["runtime"]["language"], name
            )
    print(f"{OK} deleted {len(deleted)} actions: {', '.join(deleted)}")
    return deleted


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def benchmark_output(result) -> dict:
    """The benchmark's own output, independent of the platform's wrapper format."""
    out = result.output.get("result", {})
    if "output" in out:  # local backend
        out = out["output"]
    return out


def request_id(result) -> str:
    """The platform's ID of one invocation (OpenWhisk: the activation ID)."""
    return result.request_id or result.output.get("request_id", "")


def download_provider_metrics(deployment, func, results, begin: float, end: float) -> None:
    """Ask the platform for its own record of the invocations; fills result.provider_times."""
    requests_by_id = {request_id(r): r for r in results}
    deployment.download_metrics(func.name, int(begin) - 1, int(end) + 1, requests_by_id, {})


_MEASUREMENTS = {"download_time": "download", "compute_time": "compute", "upload_time": "upload"}


def to_frame(results, label: Optional[str] = None) -> pd.DataFrame:
    """One row per SeBS ExecutionResult; all times in milliseconds.

    Columns follow the four vantage points:
      client_time                     - client: request sent until response received
      provider_wait, provider_init, provider_duration
                                      - provider metrics: queueing (OpenWhisk only), container
                                        initialization, running the function (NaN when unavailable)
      internal_function_time          - inside the function: SeBS's wrapper, from entering it to
                                        returning (imports on a cold start + the handler)
      download, compute, upload       - benchmark: measured by the function code itself
    """
    rows = []
    for r in results:
        if r.stats.failure:
            rows.append({"request_id": request_id(r), "failed": True})
            continue
        measurement = benchmark_output(r).get("measurement", {}) or {}
        row = {
            "request_id": request_id(r),
            "is_cold": r.stats.cold_start,
            "client_time": r.times.client / 1000,
            # HTTP triggers only (curl's timings, in seconds): connection set up, first byte received
            "http_connect": (r.times.http_startup * 1000) or float("nan"),
            "http_first_byte": (r.times.http_first_byte_return * 1000) or float("nan"),
            "provider_wait": (r.provider_times.waiting / 1000) or float("nan"),
            "provider_init": r.provider_times.initialization / 1000,
            "provider_duration": (r.provider_times.execution / 1000) or float("nan"),
            "internal_function_time": r.times.benchmark / 1000,
        }
        for key, column in _MEASUREMENTS.items():
            if key in measurement:
                row[column] = measurement[key] / 1000
        rows.append(row)
    df = pd.DataFrame(rows)
    if label is not None:
        df.insert(0, "label", label)
    return df


COMPONENTS = ["client_time", "http_connect", "http_first_byte", "provider_wait", "provider_init", "provider_duration", "internal_function_time", "download", "compute", "upload"]


def compare(frames: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Median of every time component (ms), one column per data set."""
    out = {}
    for name, df in frames.items():
        cols = [c for c in COMPONENTS if c in df.columns]
        out[name] = df[cols].median(numeric_only=True)
    return pd.DataFrame(out).round(2)


def plot_breakdown(frames: Dict[str, pd.DataFrame]):
    """Where does the time go? Stacked medians along the invocation's path, inside out.

    Without provider metrics, everything outside the handler is "outside the function".
    """
    import matplotlib.pyplot as plt

    def med(m, col):
        value = m.get(col, float("nan"))
        return 0.0 if value != value else float(value)

    parts = {}
    for name, df in frames.items():
        m = df.median(numeric_only=True)
        code = med(m, "download") + med(m, "compute") + med(m, "upload")
        internal = med(m, "internal_function_time")
        row = {
            "download": med(m, "download"),
            "compute": med(m, "compute"),
            "upload": med(m, "upload"),
            "internal function time: imports, setup": max(internal - code, 0),
        }
        if med(m, "provider_duration") > 0:
            inside = med(m, "provider_wait") + med(m, "provider_init") + med(m, "provider_duration")
            row["runtime, outside the function"] = max(med(m, "provider_duration") - internal, 0)
            row["container init (initTime)"] = med(m, "provider_init")
            row["platform queueing (waitTime)"] = med(m, "provider_wait")
            row["client and network"] = max(med(m, "client_time") - inside, 0)
        else:
            row["outside the function (no provider metrics)"] = max(med(m, "client_time") - internal, 0)
        parts[name] = row
    data = pd.DataFrame(parts).T.fillna(0)
    data = data.loc[:, (data > 0).any()]
    ax = data.plot.bar(stacked=True, figsize=(8, 4.5), rot=0)
    ax.set_ylabel("median time [ms]")
    ax.set_title("Where does an invocation's time go?")
    ax.legend(loc="center left", bbox_to_anchor=(1, 0.5), frameon=False)
    plt.tight_layout()
    plt.show()
    return data.round(2)


def plot_memory_sweep(summary: pd.DataFrame, local_compute_ms: Optional[float] = None, cloud: str = "") -> None:
    """Compute time and cost per million invocations over the memory size (index of summary)."""
    import matplotlib.pyplot as plt

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 3.8))
    ax1.plot(summary.index, summary.compute_ms, "o-", label=f"cloud ({cloud})" if cloud else "cloud")
    if local_compute_ms is not None:
        ax1.axhline(local_compute_ms, ls="--", color="gray", label="laptop (OpenWhisk, warm)")
    ax1.set_xlabel("memory [MB]")
    ax1.set_ylabel("median compute time [ms]")
    ax1.set_ylim(bottom=0)
    ax1.legend()
    ax2.plot(summary.index, summary.usd_per_million, "o-", color="tab:red")
    ax2.set_xlabel("memory [MB]")
    ax2.set_ylabel("USD per million invocations")
    ax2.set_ylim(bottom=0)
    for ax in (ax1, ax2):
        ax.set_xticks(list(summary.index))
    fig.tight_layout()
    plt.show()


def save(df: pd.DataFrame, path: str) -> None:
    path = TUTORIAL_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    print(f"{OK} saved {len(df)} rows to {path.relative_to(TUTORIAL_DIR)}")


def gcp_cost(duration_ms: float, memory_mb: int) -> float:
    """Price of one 1st gen invocation: billed time rounded up to 100 ms, memory + CPU."""
    billed_s = -(-duration_ms // 100) / 10
    return billed_s * (memory_mb / 1024 * GCP_PRICE_PER_GB_S + GCP_CPU_GHZ[memory_mb] * GCP_PRICE_PER_GHZ_S)


def aws_cost(duration_ms: float, memory_mb: int) -> float:
    """Price of one warm Lambda invocation: billed time rounded up to 1 ms, memory, plus the request."""
    billed_s = -(-duration_ms // 1) / 1000
    return billed_s * memory_mb / 1024 * AWS_PRICE_PER_GB_S + AWS_PRICE_PER_REQUEST


def cloud_cost(cloud: str, duration_ms: float, memory_mb: int) -> float:
    """Price of one invocation in USD on `cloud`."""
    return {"gcp": gcp_cost, "aws": aws_cost}[cloud](duration_ms, memory_mb)


# ---------------------------------------------------------------------------
# Setup check
# ---------------------------------------------------------------------------


def check(day: str = "thursday") -> bool:
    """One line per component: Docker, SeBS, storage, registry, OpenWhisk, the cloud."""
    good = True

    def report(ok, what, detail="", hint=""):
        nonlocal good
        _line(OK if ok else FAIL, what, detail if ok else hint or detail)
        good = good and ok

    report(sys.version_info >= (3, 10), "Python", platform.python_version(), "Python 3.10+ required")

    # The most common problem: the notebook runs on another kernel than SeBS's environment,
    # e.g. a system Python (then Docker fails with "Not supported URL scheme http+docker").
    kernel_hint = (f"this notebook runs on {sys.executable}, not on SeBS's environment: "
                   "Kernel → Change Kernel → 'SeBS (PhDOpen)'; if it's not listed, see README step 3")
    try:
        import sebs  # noqa: F401
        import docker as docker_sdk
        import requests as requests_lib

        too_old = (tuple(int(x) for x in docker_sdk.__version__.split(".")[:2]) < (7, 1)
                   and tuple(int(x) for x in requests_lib.__version__.split(".")[:2]) >= (2, 32))
        report(not too_old, "Kernel", sys.executable,
               f"docker {docker_sdk.__version__} doesn't work with requests {requests_lib.__version__}; {kernel_hint}")
    except ImportError as e:
        report(False, "Kernel", hint=f"{e}; {kernel_hint}")
    if not good:
        return False

    try:
        version = _docker().version()["Version"]
        report(True, "Docker", version)
    except Exception as e:
        report(False, "Docker", hint=f"not reachable ({e}); start Jupyter from a terminal where `docker ps` works")
        return False

    try:
        import sebs
        from sebs.version import __version__

        report(True, "SeBS", f"{__version__} at {Path(sebs.__file__).parent.parent}")
    except Exception as e:
        report(False, "SeBS", hint=f"cannot import ({e}); select the kernel 'SeBS (PhDOpen)'")

    if day == "thursday":
        for tool in ["kubectl", "wsk"]:
            path = shutil.which(tool)
            report(path is not None, tool, path or "", "not found; run openwhisk/prepare.sh or set OPENWHISK_DEPS")
        try:
            items = _kubectl_json("get", "pods")["items"]
            # One-time setup jobs (certificates, database, catalog packages) don't affect actions.
            jobs = ("gen-certs", "init-couchdb", "install-packages")
            not_ready = [
                p["metadata"]["name"] for p in items
                if p["status"]["phase"] not in ("Running", "Succeeded")
                and not any(job in p["metadata"]["name"] for job in jobs)
            ]
            report(
                len(items) > 0 and not not_ready,
                "OpenWhisk pods",
                f"{len(items)} running or completed",
                f"not ready yet: {', '.join(not_ready) or 'none found'}; wait 2 minutes and rerun",
            )
        except Exception as e:
            report(False, "OpenWhisk pods", hint=f"{e}; is OpenWhisk deployed (openwhisk/deploy.sh)?")
        try:
            openwhisk_api("namespaces/_/actions", limit=1)
            report(True, "OpenWhisk API", _wskprops().get("APIHOST", ""))
        except Exception as e:
            report(False, "OpenWhisk API", hint=f"{e}; wait 2 minutes and rerun")
        try:
            # The controller sends actions only to invokers that passed their self-test.
            host = _wskprops().get("APIHOST", "localhost:31001")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                healthy = int(requests.get(f"https://{host}/invokers/healthy/count", verify=False, timeout=5).text)
            report(healthy > 0, "OpenWhisk invoker", f"{healthy} healthy", "no healthy invoker yet; wait 2 minutes and rerun")
        except Exception as e:
            report(False, "OpenWhisk invoker", hint=f"{e}; wait 2 minutes and rerun")
        try:
            registry_catalog()
            report(True, "Registry", REGISTRY)
        except Exception as e:
            report(False, "Registry", hint=f"{REGISTRY} not reachable ({e}); run openwhisk/deploy.sh")
        try:
            cfg = start_storage()["object"][STORAGE_TYPE]
            report(bool(cfg["external_address"]), "Storage", f"{STORAGE_TYPE}; OpenWhisk functions will use {cfg['external_address']}",
                   "no external address detected: are you connected to a network?")
        except Exception as e:
            report(False, "Storage", hint=str(e))

    found = False
    for cloud in CLOUDS:
        try:
            _, source = _cloud_credentials(cloud)
            _line(OK, f"Cloud: {cloud}", source)
            found = True
        except FileNotFoundError:
            pass
    if not found:
        _line(WARN, "Cloud", "no credentials yet (needed later today): your AWS or GCP account, see README "
                             "'Cloud account'; no account? ask us for credentials")

    print("All good, let's go!" if good else "Fix the lines marked " + FAIL + " (ask us if stuck).")
    return good
