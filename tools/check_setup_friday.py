"""Friday's steps of the setup check: ./check_setup.sh friday (see check_setup.py).

  1. Docker, Python, SeBS, the Jupyter kernel
  2. storage + 110.dynamic-html on the local backend, in Python and Node.js
  3. the stress-ng image
  4. the 050.os-counters benchmark
  5. cloud credentials (AWS or GCP; only checks that they exist)
"""

from check_setup import check_cloud, check_environment, require


def check_local(ctx):
    from sebs.faas.function import Trigger

    st, client = ctx.st, ctx.client
    config = st.load_config("local")
    config["deployment"]["local"]["storage"] = st.start_storage()
    for language, version in [("python", "3.11"), ("nodejs", "20")]:
        config["experiments"]["runtime"] = {"language": language, "version": version}
        local = client.get_deployment(config)
        local.initialize()
        experiment = client.get_experiment_config(config["experiments"])
        benchmark = client.get_benchmark("110.dynamic-html", local, experiment)
        payload = benchmark.prepare_input(local.system_resources, size="test")
        func = local.get_function(benchmark)
        trigger = (func.triggers(Trigger.TriggerType.HTTP) or [local.create_trigger(func, Trigger.TriggerType.HTTP)])[0]
        result = trigger.sync_invoke(payload)
        require(not result.stats.failure, f"110.dynamic-html failed on the local backend ({language})")
        print(f"  ✔ local {language} {version}: {result.times.client / 1000:.0f} ms")
        st.cleanup_local(client, local)


# The same image as in friday.ipynb (section 2, step 2), built in advance so that the course doesn't need the download.
STRESS_DOCKERFILE = '''
FROM alpine:3.20
RUN apk add --no-cache stress-ng
ENTRYPOINT ["stress-ng"]
'''


def check_stress_image(ctx):
    import io

    import docker

    client = docker.from_env()
    try:
        client.images.get(ctx.st.STRESS_IMAGE)
    except docker.errors.ImageNotFound:
        client.images.build(fileobj=io.BytesIO(STRESS_DOCKERFILE.encode()), tag=ctx.st.STRESS_IMAGE, rm=True)
    print(f"  ✔ {ctx.st.STRESS_IMAGE}")


def check_os_counters(ctx):
    ctx.st.register_benchmark("050.os-counters")
    print("  ✔ 050.os-counters registered in SeBS")


FRIDAY = [
    ("Docker, Python, SeBS, Jupyter", check_environment),
    ("local backend (110.dynamic-html, Python and Node.js)", check_local),
    ("stress-ng image", check_stress_image),
    ("050.os-counters", check_os_counters),
    ("cloud credentials", check_cloud),
]
