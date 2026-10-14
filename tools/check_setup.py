#!/usr/bin/env python3
"""Check the setup for one day of the tutorial; run through ../check_setup.sh.

    ./check_setup.sh            # Thursday
    ./check_setup.sh friday     # Friday (tools/check_setup_friday.py)

Prints ALL OK, or FAILED at step N: <reason>. Thursday's steps:
  1. Docker, Python, SeBS, the Jupyter kernel
  2. build 010.sleep for OpenWhisk, push it to the local registry, invoke it (deploys OpenWhisk
     for the test if it isn't running, and removes the cluster afterwards)
  3. cloud credentials (AWS or GCP; only checks that they exist)
"""

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

TUTORIAL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TUTORIAL_DIR))
os.chdir(TUTORIAL_DIR)

LOG = TUTORIAL_DIR / "outputs" / "check_setup.log"

# check_setup_friday imports this module by name: let it get this one, not a second copy
# (with a different StepFailed).
sys.modules.setdefault("check_setup", sys.modules[__name__])


class StepFailed(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise StepFailed(reason)


class Context:
    """What the steps share: the SeBS client and the tutorial helpers, after the first step."""

    client = None
    st = None


# --------------------------------------------------------------------- shared steps


def check_environment(ctx):
    require(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}; 3.10 or newer required")
    import docker

    try:
        docker.from_env().ping()
    except Exception as e:
        raise StepFailed(f"cannot reach Docker ({e}); can you run `docker ps` without sudo?")
    try:
        import sebs  # noqa: F401
    except Exception as e:
        raise StepFailed(f"cannot import SeBS ({e}); in the tutorial directory, run: uv run --project serverless-benchmarks ./check_setup.sh")
    kernels = subprocess.run(
        [sys.executable, "-m", "jupyter", "kernelspec", "list", "--json"], capture_output=True, text=True
    )
    require(kernels.returncode == 0, "Jupyter is not installed in the SeBS environment (README, step 3)")
    require("sebs" in json.loads(kernels.stdout)["kernelspecs"], "Jupyter kernel 'sebs' missing (README, step 3)")

    import sebs_tutorial
    from sebs import SeBS

    ctx.st = sebs_tutorial
    ctx.client = SeBS(cache_dir="cache", output_dir="outputs", verbose=False, logging_filename=str(LOG))


def check_cloud(ctx):
    found = []
    for cloud in ctx.st.CLOUDS:
        try:
            _, source = ctx.st._cloud_credentials(cloud)
        except FileNotFoundError:
            continue
        found.append(cloud)
        print(f"  ✔ {cloud}: {source}")
    require(found, "no AWS or GCP credentials in configs/ (README, step 6); "
                   "can't create an account? tell the instructor, we'll provide credentials")


# --------------------------------------------------------------------- Thursday


def openwhisk_script(command):
    """Run a command with the settings of openwhisk/env.sh; True if it succeeded."""
    env_sh = TUTORIAL_DIR / "openwhisk" / "env.sh"
    with open(LOG, "a") as log:
        return subprocess.run(["bash", "-c", f"source '{env_sh}' && {command}"], stdout=log, stderr=log).returncode == 0


def check_openwhisk(ctx):
    # Without a running OpenWhisk, deploy it as on Thursday (openwhisk/deploy.sh), test it,
    # and remove the cluster again: on Thursday, deploy.sh starts from a fresh cluster.
    deployed_here = False
    try:
        ctx.st.openwhisk_api("namespaces/_/actions", limit=1)
    except Exception:
        print("  deploying OpenWhisk for the test, as on Thursday (a few minutes)", flush=True)
        if not openwhisk_script(f"'{TUTORIAL_DIR}/openwhisk/deploy.sh'"):
            raise StepFailed(f"./openwhisk/deploy.sh failed (details: {LOG.relative_to(TUTORIAL_DIR)}); "
                             "did ./openwhisk/prepare.sh finish?")
        deployed_here = True
    try:
        _test_openwhisk(ctx.client, ctx.st)
    finally:
        if deployed_here:
            openwhisk_script('kind delete cluster --name "$CLUSTER_NAME"')
            print("  ✔ test cluster removed; on Thursday, the notebook deploys OpenWhisk again")


def _test_openwhisk(client, st):
    from sebs.faas.function import Trigger

    config = st.load_config("openwhisk")
    config["deployment"]["openwhisk"]["storage"] = st.start_storage()
    openwhisk = client.get_deployment(config)
    openwhisk.initialize(resource_prefix="check")
    experiment = client.get_experiment_config(config["experiments"])
    benchmark = client.get_benchmark("010.sleep", openwhisk, experiment)
    payload = benchmark.prepare_input(openwhisk.system_resources, size="test")
    func = openwhisk.get_function(benchmark)
    image = st.action(func)["image"]
    require(image.startswith(st.REGISTRY), f"the action's image {image} is not from {st.REGISTRY}")
    result = func.triggers(Trigger.TriggerType.LIBRARY)[0].sync_invoke(payload)
    require(not result.stats.failure, "010.sleep failed on OpenWhisk; see ./openwhisk/status.sh")
    print(f"  ✔ OpenWhisk: built, pushed to {st.REGISTRY}, invoked ({result.times.client / 1000:.0f} ms, cold)")
    st.cleanup_openwhisk(client, openwhisk)


THURSDAY = [
    ("Docker, Python, SeBS, Jupyter", check_environment),
    ("OpenWhisk: build, push, invoke (010.sleep)", check_openwhisk),
    ("cloud credentials", check_cloud),
]


def steps_for(day):
    if day == "thursday":
        return THURSDAY
    if day == "friday":
        try:
            import check_setup_friday
        except ImportError:
            raise StepFailed("the Friday check isn't available yet (tools/check_setup_friday.py); "
                             "update the tutorial: git pull") from None
        return check_setup_friday.FRIDAY
    raise StepFailed(f"unknown day {day!r}; usage: ./check_setup.sh [thursday|friday]")


def main(day):
    LOG.parent.mkdir(exist_ok=True)
    try:
        steps = steps_for(day)
    except StepFailed as e:
        print(f"FAILED: {e}")
        return 1
    print(f"Checking the setup for {day.capitalize()}")
    ctx = Context()
    for number, (name, step) in enumerate(steps, start=1):
        print(f"[{number}/{len(steps)}] {name}", flush=True)
        try:
            step(ctx)
        except StepFailed as e:
            print(f"FAILED at step {number}: {e}")
            return 1
        except Exception as e:
            with open(LOG, "a") as f:
                traceback.print_exc(file=f)
            print(f"FAILED at step {number}: {type(e).__name__}: {e} (details: {LOG.relative_to(TUTORIAL_DIR)})")
            return 1
    print(f"ALL OK ({day.capitalize()})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1].lower() if len(sys.argv) > 1 else "thursday"))
