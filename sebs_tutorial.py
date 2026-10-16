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
import time
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


def _show_link(text: str, path: Path) -> None:
    """A clickable link in Jupyter; the plain path elsewhere."""
    try:
        from IPython import get_ipython

        if get_ipython() is not None and get_ipython().__class__.__name__ == "ZMQInteractiveShell":
            from IPython.display import Markdown, display

            rel = path.relative_to(TUTORIAL_DIR)
            display(Markdown(f"{text}: [`{rel}`]({rel})"))
            return
    except Exception:
        pass
    print(f"{text}: {path}")


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

    The configuration is also saved in .state/storage.json, in the format of SeBS's CLI:
    `sebs storage stop object .state/storage.json` stops the storage without the notebook.
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
        cfg = config_cls.deserialize(_storage_state(state))
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

    storage_config = {"object": {"type": STORAGE_TYPE, STORAGE_TYPE: storage.serialize()}}
    with open(state, "w") as f:
        json.dump(storage_config, f, indent=2)
    return storage_config


def _storage_state(state: Path) -> dict:
    """The storage instance's settings from .state/storage.json.

    Written in the CLI's format ({"object": {"type": ..., <type>: {...}}}); earlier versions of
    this module wrote only the inner dictionary.
    """
    with open(state) as f:
        saved = json.load(f)
    if "object" in saved:
        return saved["object"][saved["object"]["type"]]
    return saved


def stop_storage() -> None:
    """Stop and remove the storage container started by start_storage (the data volume stays)."""
    state = STATE_DIR / "storage.json"
    if not state.exists():
        return
    instance_id = _storage_state(state)["instance_id"]
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
# Friday: CPU pinning and interference
# ---------------------------------------------------------------------------

STRESS_IMAGE = "sebs-tutorial/stress-ng:latest"


def cpu_topology() -> pd.DataFrame:
    """CPUs as Docker sees them (on macOS/Windows: the CPUs of Docker Desktop's VM).

    core and socket: the physical core and package of each CPU; smt_siblings: the CPUs that
    share its core; max_mhz: its highest clock; type: P (performance) or E (efficiency) core,
    on Intel's hybrid CPUs only.
    """
    script = (
        "for c in /sys/devices/system/cpu/cpu[0-9]*; do "
        "echo $(basename $c) $(cat $c/topology/core_id $c/topology/physical_package_id "
        "$c/topology/thread_siblings_list 2>/dev/null) "
        "$(cat $c/cpufreq/cpuinfo_max_freq 2>/dev/null || echo -); done; "
        "echo P $(cat /sys/devices/cpu_core/cpus 2>/dev/null); echo E $(cat /sys/devices/cpu_atom/cpus 2>/dev/null)"
    )
    out = _docker().containers.run(STRESS_IMAGE, entrypoint=["sh", "-c", script], remove=True).decode()
    rows, types = [], {}
    for line in out.split("\n"):
        parts = line.split()
        if len(parts) == 5:
            rows.append({"cpu": int(parts[0][3:]), "core": int(parts[1]), "socket": int(parts[2]),
                         "smt_siblings": parts[3], "max_mhz": int(parts[4]) // 1000 if parts[4].isdigit() else None})
        elif len(parts) == 2 and parts[0] in ("P", "E"):
            types.update({cpu: parts[0] for cpu in _cpu_list(parts[1])})
    df = pd.DataFrame(rows).sort_values("cpu").reset_index(drop=True)
    if df.max_mhz.isna().all():
        df = df.drop(columns="max_mhz")
    if types:
        df["type"] = df.cpu.map(types)
    return df


def _cpu_list(text: str) -> List[int]:
    """'0-3,8' -> [0, 1, 2, 3, 8] (the format of Linux's CPU lists and of Docker's cpuset)."""
    cpus = []
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-")
            cpus += range(int(a), int(b) + 1)
        elif part.strip():
            cpus.append(int(part))
    return cpus


def pick_cpus(function_cpu: int = 2) -> Dict[str, Optional[str]]:
    """CPUs for the interference experiment: the function's CPU, its SMT sibling, another core."""
    topo = cpu_topology()
    if function_cpu not in set(topo.cpu):
        function_cpu = int(topo.cpu.iloc[-1])
    me = topo[topo.cpu == function_cpu].iloc[0]
    same_core = topo[(topo.core == me.core) & (topo.socket == me.socket) & (topo.cpu != function_cpu)]
    other = topo[(topo.core != me.core) | (topo.socket != me.socket)]
    return {
        "function": str(function_cpu),
        "smt_sibling": str(int(same_core.cpu.iloc[0])) if len(same_core) else None,
        "other_core": str(int(other.cpu.iloc[-1])) if len(other) else None,
    }


def check_cpus(cpus: Dict[str, Optional[str]]) -> bool:
    """✔ when the CPUs chosen for the interference experiment fit the topology."""
    topo = cpu_topology().set_index("cpu")
    good = True

    def cpu(key: str) -> Optional[int]:
        value = cpus.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.isdigit():
            raise ValueError(f'CPUS["{key}"] must be one CPU number as a string, e.g. "2", not {value!r}')
        if int(value) not in topo.index:
            raise ValueError(f'CPUS["{key}"] = "{value}": Docker sees no such CPU')
        return int(value)

    fn, sib, other = cpu("function"), cpu("smt_sibling"), cpu("other_core")
    if fn is None or other is None:
        _line(FAIL, "CPUS", 'choose "function" and "other_core"')
        return False
    me = topo.loc[fn]
    siblings = [c for c in _cpu_list(me.smt_siblings) if c != fn]
    if sib is None:
        ok = not siblings
        _line(OK if ok else FAIL, "smt_sibling", "none: no SMT" if ok else f"CPU {fn} shares its core with {siblings}")
    else:
        ok = sib in siblings
        _line(OK if ok else FAIL, "smt_sibling", f"CPU {sib} {'shares' if ok else 'does not share'} the core of CPU {fn}")
    good &= ok
    other_row = topo.loc[other]
    ok = (other_row.core, other_row.socket) != (me.core, me.socket)
    _line(OK if ok else FAIL, "other_core", f"CPU {other}: core {other_row.core}" + ("" if ok else f", the same core as CPU {fn}"))
    good &= ok
    if fn == 0:
        _line(WARN, "function", "CPU 0 handles many interrupts and system tasks: another CPU gives quieter measurements")
    for column, what in (("type", "type"), ("max_mhz", "maximum clock")):
        if column in topo.columns and topo.loc[fn, column] != topo.loc[other, column]:
            _line(WARN, "other_core", f"another {what} than the function's CPU ({topo.loc[other, column]} vs {topo.loc[fn, column]})")
    return good


class stressor:
    """Context manager: a stress-ng container pinned to `cpus`, running while the block runs.

        with st.stressor(cpus="2"):            # one CPU-burning worker on CPU 2
            ...
        with st.stressor(cpus="3", kind="stream"):   # memory-bandwidth stressor on CPU 3
            ...
    """

    ARGS = {"cpu": ["--cpu", "1", "--cpu-method", "matrixprod"], "stream": ["--stream", "1"]}

    def __init__(self, cpus: str, kind: str = "cpu", warmup_s: float = 1.0):
        if kind not in self.ARGS:
            raise ValueError(f"kind must be one of {list(self.ARGS)}")
        self.cpus, self.kind, self.warmup_s = cpus, kind, warmup_s
        self.container = None

    def __enter__(self):
        self.container = _docker().containers.run(
            STRESS_IMAGE, command=self.ARGS[self.kind], cpuset_cpus=self.cpus,
            detach=True, remove=True, labels={"sebs-tutorial": "stressor"},
        )
        time.sleep(self.warmup_s)
        print(f"stressor ({self.kind}) running on CPU {self.cpus}")
        return self

    def __exit__(self, *exc):
        try:
            self.container.kill()
        except Exception:
            pass
        print("stressor stopped")
        return False


def container_cpuset(func) -> Dict[str, str]:
    """The CPUs a local function's container may use: Docker's setting and the cgroup's view."""
    container = _docker().containers.get(func.container.id)
    container.reload()
    _, cgroup = container.exec_run(["cat", "/sys/fs/cgroup/cpuset.cpus.effective"])
    return {
        "docker": container.attrs["HostConfig"].get("CpusetCpus", ""),
        "cgroup": cgroup.decode().strip(),
    }


def check_pinning(func, expected: str) -> bool:
    """✔ when the function's container is pinned to `expected` (e.g. "2")."""
    seen = container_cpuset(func)
    if seen["docker"] == expected and seen["cgroup"] == expected:
        print(f"✔ pinned to CPU {expected} (docker: {seen['docker']}, cgroup: {seen['cgroup']})")
        return True
    hint = "did you restart the container (enforce_cold_start) after setting SEBS_LOCAL_CPUSET?"
    if not seen["docker"]:
        hint = "Docker didn't receive a cpuset: is your change in _start_container, and did you restart the kernel after editing?"
    print(f"✘ docker: '{seen['docker']}', cgroup: '{seen['cgroup']}' - {hint}")
    return False


def bootstrap_ci(values, stat=None, n: int = 2000, alpha: float = 0.05, seed: int = 0):
    """Bootstrap confidence interval of a statistic (default: median)."""
    import numpy as np

    rng = np.random.default_rng(seed)
    values = np.asarray(pd.Series(values).dropna())
    stat = stat or np.median
    samples = [stat(rng.choice(values, size=len(values), replace=True)) for _ in range(n)]
    return float(np.quantile(samples, alpha / 2)), float(np.quantile(samples, 1 - alpha / 2))


def slowdown(base: pd.DataFrame, others: Dict[str, pd.DataFrame], column: str = "client_time") -> pd.DataFrame:
    """Median slowdown against `base`, with a bootstrap 95% CI of the ratio of medians."""
    import numpy as np

    rng = np.random.default_rng(0)
    b = base[column].dropna().to_numpy()
    rows = []
    for name, df in others.items():
        o = df[column].dropna().to_numpy()
        ratios = [
            np.median(rng.choice(o, len(o))) / np.median(rng.choice(b, len(b))) for _ in range(2000)
        ]
        rows.append({
            "condition": name,
            "median_ms": round(float(np.median(o)), 2),
            "slowdown": round(float(np.median(o) / np.median(b)), 3),
            "ci_low": round(float(np.quantile(ratios, 0.025)), 3),
            "ci_high": round(float(np.quantile(ratios, 0.975)), 3),
        })
    return pd.DataFrame(rows).set_index("condition")


def compare_ci(frames: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Median and bootstrap 95% CI per component (ms), one column group per data set."""
    out = {}
    for name, df in frames.items():
        cols = [c for c in COMPONENTS if c in df.columns and df[c].notna().any()]
        out[name] = {
            c: f"{df[c].median():.1f} [{bootstrap_ci(df[c])[0]:.1f}, {bootstrap_ci(df[c])[1]:.1f}]" for c in cols
        }
    return pd.DataFrame(out)


def sebs_source(path: str = "sebs/local/local.py") -> Path:
    """Make SeBS's source editable from JupyterLab: link it into the tutorial directory."""
    import sebs

    sebs_dir = Path(sebs.__file__).resolve().parent.parent
    link = TUTORIAL_DIR / "serverless-benchmarks"
    if not link.exists():
        link.symlink_to(sebs_dir, target_is_directory=True)
    target = link / path
    _show_link("Open in JupyterLab", target)
    return target


BENCHMARKS_DIR = TUTORIAL_DIR / "benchmarks"


def register_benchmark(name: str) -> Path:
    """Make a benchmark of this repository visible to SeBS.

    SeBS looks for benchmarks only in its own benchmarks/ directory, so we link ours
    there (benchmarks/900.tutorial/<name>). The code stays here, where you edit it.
    """
    import sebs

    source = BENCHMARKS_DIR / name
    if not (source / "config.json").exists():
        raise FileNotFoundError(f"{source} is not a benchmark (no config.json)")
    target_dir = Path(sebs.__file__).resolve().parent.parent / "benchmarks" / "900.tutorial"
    target_dir.mkdir(exist_ok=True)
    link = target_dir / name
    if link.is_symlink() and link.resolve() != source.resolve():
        link.unlink()
    if not link.exists():
        link.symlink_to(source, target_is_directory=True)
    _show_link(f"Benchmark {name} registered in SeBS; its code", source / "python" / "function.py")
    return link


def cleanup_local(sebs_client, deployment) -> List[str]:
    """Stop and remove the local function containers created by this notebook."""
    removed = []
    for name, func in sebs_client.cache_client.get_all_functions(deployment.name()).items():
        try:
            _docker().containers.get(func["instance_id"]).remove(force=True)
        except Exception:
            pass
        sebs_client.cache_client.remove_function(
            deployment.name(), func["benchmark"], func["config"]["runtime"]["language"], name
        )
        removed.append(name)
    for c in _docker().containers.list(filters={"label": "sebs-tutorial=stressor"}):
        c.remove(force=True)
    print(f"{OK} removed {len(removed)} functions: {', '.join(removed)}")
    return removed


# ---------------------------------------------------------------------------
# Friday: what does the system know about one invocation? (050.os-counters)
# ---------------------------------------------------------------------------


def benchmark_output(result) -> dict:
    """The benchmark's own output, independent of the platform's wrapper format."""
    out = result.output.get("result", {})
    if "output" in out:  # local backend
        out = out["output"]
    return out


def counters(result) -> dict:
    """Flattened 050.os-counters measurements of one invocation (times in ms)."""
    out = benchmark_output(result)
    res, meas = out["result"], out.get("measurement", {})
    row = {
        "workload": res["workload"],
        "size": res["size"],
        "cold": res["cold"],
        "wall_ms": meas["wall_time"] / 1000,
        "thread_cpu_ms": meas["thread_cpu_time"] / 1000,
    }
    for tier in ["posix", "papi"]:
        for name, value in res[tier]["events"].items():
            row[f"{tier}.{name}"] = value
    papi = res["papi"]["events"]
    if papi.get("PAPI_TOT_CYC") and "PAPI_TOT_INS" in papi:
        row["papi.IPC"] = round(papi["PAPI_TOT_INS"] / papi["PAPI_TOT_CYC"], 3)
    return row


def counters_table(results: Dict[str, object], with_client_time: bool = False) -> pd.DataFrame:
    """One column per invocation, one row per counter."""
    cols = {}
    for name, r in results.items():
        row = counters(r)
        if with_client_time:
            row = {"client_ms": r.times.client / 1000, **row}
        cols[name] = row
    return pd.DataFrame(cols)


def check_counters(results: Dict[str, object]) -> bool:
    """Sanity checks of the compute, memory, and sleep workloads (one line each)."""
    good = True
    for name, r in results.items():
        c = counters(r)
        if "posix.ru_minflt" not in c:
            _line(FAIL, name, "no POSIX counters: " + benchmark_output(r)["result"]["posix"]["errors"].get("posix", "")
                  + " - finish TODO 5, then run get_function again to redeploy")
            good = False
            continue
        ratio = c["wall_ms"] / max(c["thread_cpu_ms"], 1e-3)
        if c["workload"] == "sleep":
            ok, what = ratio >= 10, f"wall {c['wall_ms']:.1f} ms >= 10 x thread CPU {c['thread_cpu_ms']:.2f} ms"
        elif c["workload"] == "memory":
            expected = c["size"] * 1024 * 1024 // 4096
            faults = c["posix.ru_minflt"]
            ok, what = abs(faults - expected) <= 0.2 * expected, f"{faults} minor page faults, expected ~{expected}"
        else:
            ok, what = 0.8 <= ratio <= 1.2, f"thread CPU {c['thread_cpu_ms']:.1f} ms ~ wall {c['wall_ms']:.1f} ms"
        _line(OK if ok else FAIL, f"{name} ({c['workload']})", what)
        good = good and ok
    return good


def counter_tiers(result) -> pd.DataFrame:
    """Status and errors of each counter tier, plus what the machine allows."""
    res = benchmark_output(result)["result"]
    rows = []
    for tier in ["posix", "papi"]:
        t = res[tier]
        rows.append({
            "tier": tier,
            "status": t["status"],
            "counted": len(t["events"]),
            "errors": "; ".join(f"{k}: {v}" for k, v in t["errors"].items()) or "",
        })
    df = pd.DataFrame(rows).set_index("tier")
    m = res["machine"]
    print(f"machine: {m['cpu_model']} ({m['arch']}), kernel {m['kernel']}, "
          f"perf_event_paranoid={m['perf_event_paranoid']}, PMU exposed: {m['pmu_exposed']}")
    return df


def papi_samples(result) -> pd.DataFrame:
    """PAPI overflow samples of one 050.os-counters invocation: one row per sample.

    time_ms is relative to the first sample; the event columns are *cumulative* counts
    since the counters started (use .diff() for the counts between samples).
    """
    papi = benchmark_output(result)["result"]["papi"]
    if papi["status"] not in ("ok", "partial") or "overflow" not in papi:
        reason = "; ".join(f"{k}: {v}" for k, v in papi["errors"].items()) or "no overflow samples"
        raise RuntimeError(f"PAPI status {papi['status']}: {reason}")
    overflow = papi["overflow"]
    df = pd.DataFrame(overflow["samples"], columns=overflow["columns"])
    time_us = df.pop("time_us")
    df.insert(0, "time_ms", (time_us - time_us.min()) / 1000)
    if overflow["dropped"]:
        _line(FAIL, "samples", f"{overflow['dropped']} dropped: raise max_samples or the threshold")
    print(f"{len(df)} samples, one every {overflow['threshold']:,} {overflow['event']}")
    return df


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


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


def load(path: str) -> pd.DataFrame:
    return pd.read_csv(TUTORIAL_DIR / path)


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

    if day == "friday":
        try:
            _docker().images.get(STRESS_IMAGE)
            report(True, "stress-ng image", STRESS_IMAGE)
        except Exception:
            # Not a failure: section 2 builds it; the CPU check below needs it.
            _line(WARN, "stress-ng image", "not built yet; section 2, step 2 builds it")
        else:
            try:
                cpus = pick_cpus()
                report(True, "CPUs for pinning", f"function on {cpus['function']}, SMT sibling {cpus['smt_sibling']}, other core {cpus['other_core']}")
            except Exception as e:
                report(False, "CPUs for pinning", hint=str(e))
        try:
            cfg = start_storage()["object"][STORAGE_TYPE]
            report(True, "Storage", f"{STORAGE_TYPE}; local functions will use {cfg['address']}")
        except Exception as e:
            report(False, "Storage", hint=str(e))
        clouds = [c for c in CLOUDS if (RESULTS_DIR / f"thursday_{c}.csv").exists()]
        if clouds:
            report(True, "Thursday's results", ", ".join(f"results/thursday_{c}.csv" for c in clouds))
        else:
            _line(WARN, "Thursday's results", "results/thursday_<cloud>.csv missing; section 1 uses the instructor's data (reference/)")
        try:
            import sebs.local.local as local_module
            source = Path(local_module.__file__).read_text()
            pinned = "cpuset_cpus" in source.split("def _start_container", 1)[1].split("containers.run(", 1)[0].replace("# cpuset_cpus=cpuset,", "")
            _line(OK if pinned else WARN, "SeBS local pinning", "implemented" if pinned else "not implemented yet (that's your task!)")
        except Exception:
            pass

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
