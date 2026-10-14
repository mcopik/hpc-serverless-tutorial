#!/usr/bin/env python3
"""Build the OpenWhisk function image of 210.thumbnailer with SeBS and push it to the local registry.

Thursday's notebook then deploys it without a build. 110.dynamic-html is deliberately not
built here: students watch SeBS build and push it. Uses the notebooks' SeBS cache.
"""

import os
import sys
from pathlib import Path

TUTORIAL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TUTORIAL_DIR))
os.chdir(TUTORIAL_DIR)  # the notebooks' cache and outputs directories

import sebs_tutorial as st  # noqa: E402
from sebs import SeBS  # noqa: E402

BENCHMARK = "210.thumbnailer"

config = st.load_config("openwhisk")
config["deployment"]["openwhisk"]["storage"] = st.start_storage()

client = SeBS(cache_dir="cache", output_dir="outputs", verbose=False, logging_filename=None)
openwhisk = client.get_deployment(config)
openwhisk.initialize(resource_prefix="phd")
experiment = client.get_experiment_config(config["experiments"])
benchmark = client.get_benchmark(BENCHMARK, openwhisk, experiment)
openwhisk.build_function(benchmark)

tag = benchmark.container_uri.rsplit(":", 1)[-1]
tags = st.registry_tags()
if tag and tag in tags:
    print(f"✔ {benchmark.container_uri} is in the registry")
else:
    sys.exit(f"✘ {BENCHMARK} is not in the registry (tags: {tags})")
