# SeBS tutorials: installation

PhDOpen MIMUW, 15–16 October 2026

When you're done, run the setup check (step 7) and check the last line of its output (`ALL OK (Thursday)`, or `FAILED at step N: …`).

**What you need:**

| | Thursday | Friday |
|---|---|---|
| Docker | ✔ | ✔ |
| SeBS + Jupyter | ✔ | ✔ |
| OpenWhisk on kind (local) | ✔ | |
| Cloud account: AWS or GCP | ✔ (free tier) | ✔ (the same) |

You need an **AWS or GCP account**: tutorial runs should fit into the free tier ([step 6](#6-cloud-account-aws-or-gcp)). If you can't create one, tell the instructor and we'll provide credentials. You don't need a DockerHub account.

**Hardware:**
- 16 GB of RAM recommended (Docker needs about 8 GB for OpenWhisk);
- 4+ CPU cores;
- 20 GB of free disk space.

Pick your platform:
- [Linux (x86_64)](#linux-x86_64)
- [Windows (WSL2)](#windows-wsl2)
- [macOS on Intel](#macos-on-intel)
- [macOS on Apple Silicon (M1–M4)](#macos-on-apple-silicon-m1m4): **separate instructions, please read them in full**

Then follow [Common steps](#common-steps).

## Linux (x86_64)

Tested on Ubuntu-based distributions, but should work on other distributions with equivalent packages.

```bash
sudo apt-get update
sudo apt-get install -y docker.io git curl zip libcurl4-openssl-dev libssl-dev build-essential
sudo usermod -aG docker $USER      # then log out and log back in
docker run --rm hello-world        # must work without sudo
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv: Python package manager, then open a new terminal
```

You don't need a system Python: uv downloads Python 3.12 itself when it's missing.

Continue with [Common steps](#common-steps).

## Windows (WSL2)

Everything runs **inside WSL2 (Ubuntu)**.

1. Install WSL2 with Ubuntu: `wsl --install -d Ubuntu-24.04`.
2. Install [**Docker Desktop**](https://docs.docker.com/desktop/features/wsl/). Check Docker Desktop's Resource settings such that container has a sufficient memory (4 GB).
3. Open the Ubuntu terminal and install the tools:
   ```bash
   sudo apt-get update
   sudo apt-get install -y git curl zip libcurl4-openssl-dev libssl-dev build-essential
   docker run --rm hello-world
   curl -LsSf https://astral.sh/uv/install.sh | sh   # uv: Python package manager, then open a new terminal
   ```
4. Clone everything inside the WSL file system.

Continue with [Common steps](#common-steps).

## macOS on Intel

1. Install **Docker Desktop**. Configure Docker to have at least 8 GB of memory and 4 CPUs.
2. Install the tools with [Homebrew](https://brew.sh):
   ```bash
   brew install git curl uv
   docker run --rm hello-world
   ```

Continue with [Common steps](#common-steps).

---

## macOS on Apple Silicon

**Why this is different:** SeBS's local images (Friday) exist for both x86_64 and ARM, so Docker pulls the ARM version and they run natively. OpenWhisk's images (Thursday) exist only for x86_64 and run under emulation with Rosetta on Docker Desktop. Since this is unlikely to work well, we recommend to do the tutorial in a cloud virtual machine.

---

## Common steps

All commands run in a terminal: Linux, the WSL2 Ubuntu terminal, or macOS Terminal.

### 1. Repositories

Clone the tutorial wherever you like, and SeBS inside it: everything the course needs then lives in this one directory (SeBS, its Python environment, the OpenWhisk tools), and nothing is installed system-wide.

```bash
git clone https://github.com/spcl/sebs-tutorial.git
cd sebs-tutorial
git clone --recursive https://github.com/spcl/serverless-benchmarks.git
```

**All commands below run in the `sebs-tutorial` directory.**

### 2. Install SeBS

SeBS is installed from its git clone in editable mode: on Friday you'll modify its code, and the changes apply immediately. `uv` creates a Python environment for it in `serverless-benchmarks/.venv`, with Python 3.12 (downloaded by `uv` if needed).

```bash
uv sync --project serverless-benchmarks --inexact --python 3.12
uv run --project serverless-benchmarks sebs --help
# should list: benchmark, docker, experiment, local, resources, storage
```

**You never activate this environment.** Every command that needs SeBS starts with `uv run --project serverless-benchmarks`, which runs it inside the environment.

> **Always use `uv sync --inexact`** in this course. A plain `uv sync` makes the environment match SeBS's dependency list *exactly* and uninstalls everything else, including Jupyter from step 3. (`uv run` keeps it.)

### 3. Jupyter, inside the SeBS environment

Install Jupyter into SeBS's environment, and register that environment as a Jupyter kernel named **SeBS (PhDOpen)**:

```bash
uv pip install --python serverless-benchmarks/.venv/bin/python jupyterlab ipykernel ipywidgets pandas matplotlib
uv run --project serverless-benchmarks python -m ipykernel install --user --name sebs --display-name "SeBS (PhDOpen)"
```

### 4. Starting Jupyter

Every time you work on the tutorial, at home and on both days, in the `sebs-tutorial` directory:

```bash
uv run --project serverless-benchmarks jupyter lab
```

In the notebooks, the kernel must be **SeBS (PhDOpen)** (top right; Kernel → Change Kernel). The notebooks' setup check reports it otherwise.

### 5. OpenWhisk on kind (Thursday)

On Thursday you deploy OpenWhisk - an actual serverless platform - on your laptop, in a small Kubernetes cluster (kind). To accelerate the process, you can download necessary Docker images at home.
The deployment itself happens at the start of Thursday's session, from these local copies.

`./openwhisk/prepare.sh` (in the tutorial repository):
1. downloads kind, kubectl, helm, the `wsk` CLI, and `crane` into `openwhisk/deps/` of the tutorial repository (nothing is installed system-wide; no root needed);
2. starts a **local Docker registry** (`localhost:5001`), so function images never go through Docker Hub and you need no account;
3. copies OpenWhisk's images from Docker Hub into that registry (about 3 GB, once): on Thursday, the cluster takes them from there, not from the internet;
4. configures `wsk` (`~/.wskprops`; it replaces an existing one);
5. builds the function image of `210.thumbnailer` with SeBS and pushes it to the registry (so run the script through `uv run`, as below).

If a step fails (e.g. a download breaks off), rerun the script: finished steps and copied images are skipped.

On Thursday, the notebook's first cell runs `./openwhisk/deploy.sh`: it creates the cluster (`phdopen`: three nodes running as Docker containers, which pull images from the local registry), deploys OpenWhisk with the course configuration, and waits until it's ready. The setup check (step 7) runs the same deployment once at home to test it, then removes the cluster again. If `prepare.sh` didn't run or didn't finish, `deploy.sh` does the missing downloads itself; it works.

```bash
uv run --project serverless-benchmarks ./openwhisk/prepare.sh
```

#### Extra Information 

During the tutorial, we will discuss how container images are distributed and how storage is configured.

SeBS uses two image sources:
- **its own build and base images** come from Docker Hub (`spcleth/serverless-benchmarks`). Don't change `general.docker_repository` in `configs/systems.json`.
- **function images** go to the local registry at `localhost:5001`. The notebooks set this in SeBS's configuration. This significantly accelerates deployment.

**Why storage needs extra configuration.** SeBS starts the object storage (RustFS) as a Docker container on the default bridge network and records its bridge address (e.g. `172.17.0.2:9000`). The OpenWhisk functions run in pods inside kind, on a different Docker network, and cannot reach that address. SeBS therefore also records an *external address* for functions outside the bridge: your machine's IP address and the storage's published port (`9011`), as described in [SeBS's documentation](https://github.com/spcl/serverless-benchmarks/blob/master/docs/storage.md#network-configuration). This is also how you'd connect functions to storage on another machine. Your IP changes between home and the department's network, so the notebook detects it every time it starts the storage (Thursday's notebook explains what to do when it changes mid-session). On macOS and Windows, allow Docker to accept incoming connections if your firewall asks.

Other scripts, if you need them:
- **Deploy or start:** `./openwhisk/deploy.sh`. `./openwhisk/status.sh` shows whether everything is up.
- **Stop:** `./openwhisk/stop.sh`. This frees memory; the cluster is kept, and `deploy.sh` starts it again.
- **Remove everything** after the course: `./openwhisk/delete.sh` (cluster and registry).
- **Fallback:** `./openwhisk/setup.sh` deploys OpenWhisk directly from Docker Hub, without the registry copy, and `./openwhisk/start.sh` restarts that cluster.

### 6. Cloud account (AWS or GCP)

On Thursday you deploy the same function to a cloud and run a memory sweep; on Friday you rerun it. Use **AWS Lambda** or **Google Cloud Functions**, whichever you prefer: the notebooks run the same SeBS code on both (`CLOUD = "aws"` or `"gcp"`). Everything the tutorial runs, about 100 invocations, fits into the free tiers, but both clouds ask for a payment card when you sign up. If you can't create an account, tell the instructor: we'll provide credentials, which go into the same file.

**AWS:**
1. Create an account at [aws.amazon.com](https://aws.amazon.com/).
2. In the IAM console, create a user (e.g. `sebs`) with permissions to manage Lambda functions, S3 buckets, IAM roles (SeBS creates the functions' role), and CloudWatch Logs, e.g. the managed policies `AWSLambda_FullAccess`, `AmazonS3FullAccess`, `AmazonDynamoDBFullAccess`, `IAMFullAccess`, and `CloudWatchLogsFullAccess`. In a personal account used only for this `AdministratorAccess` is simpler.
3. Create an access key for that user, and save it as `configs/aws-credentials.json` in the tutorial directory (see the template):
   ```json
   {"access_key": "AKIA...", "secret_key": "..."}
   ```

**GCP:**
1. Create a project at [console.cloud.google.com](https://console.cloud.google.com/) and link a billing account (new accounts get free credits).
2. Enable the Cloud Functions, Cloud Build, Artifact Registry, Cloud Storage, and Cloud Logging APIs.
3. Create a service account with the roles Cloud Functions Admin, Storage Admin, Logs Viewer, and Service Account User; create a JSON key for it, and save it as `configs/gcp-credentials.json` in the tutorial directory (see the template).

Both clouds work the same way: a credentials file in `configs/`, a region in the cloud's SeBS configuration, and one line in the notebooks. `configs/` contains a template of each credentials file.

| | AWS | GCP |
|---|---|---|
| Credentials file | `configs/aws-credentials.json` | `configs/gcp-credentials.json` |
| Template | `configs/aws-credentials.json.template` | `configs/gcp-credentials.json.template` (shows the fields only; use the key you downloaded) |
| Or, before `jupyter lab` | `export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...` | `export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json` |
| Region (SeBS configuration) | `configs/aws.json`: `"region": "eu-central-1"` | `configs/gcp.json`: `"region": "europe-west1"` |
| In Thursday's and Friday's notebooks | `CLOUD = "aws"` | `CLOUD = "gcp"` |

AWS: copy the template and paste your access key into it:
```bash
cp configs/aws-credentials.json.template configs/aws-credentials.json
# edit configs/aws-credentials.json:
#   {"access_key": "AKIA...", "secret_key": "..."}
```

GCP: copy the JSON key you downloaded for the service account; the project is read from its `project_id`:
```bash
cp ~/Downloads/<your-project>-<key-id>.json configs/gcp-credentials.json
```

Then set the cloud in the first cloud cell of each notebook (Thursday section 4, Friday C2):
```python
CLOUD = "aws"   # or "gcp"
```

and check that SeBS finds the credentials (in a notebook cell; it doesn't contact the cloud):
```python
import sebs_tutorial as st
st.cloud_config("aws")   # or "gcp": prints where the credentials come from, and the region
```

The setup check (step 7) does the same check for both clouds. Files named `configs/*credentials*.json` are git-ignored; never commit them, and don't edit the `.template` files themselves. If we provide credentials, they go into the same file. More details: [SeBS platforms](https://github.com/spcl/serverless-benchmarks/blob/master/docs/platforms.md). SeBS supports more platforms, such as Azure, with the same API; the notebooks only ship configurations for AWS and GCP.

**Public URLs and cleanup:** SeBS makes the functions invocable over public HTTPS URLs without authentication (a Lambda function URL, or GCP's `allUsers`). The last cell of Friday's notebook deletes the functions; delete SeBS's storage buckets in the cloud console when you no longer need them.

### 7. Check your setup (Thursday)

In the tutorial directory:

```bash
uv run --project serverless-benchmarks ./check_setup.sh
```

The script checks what you need on Thursday:
1. Docker, Python, SeBS, and the Jupyter kernel;
2. **OpenWhisk:** it deploys OpenWhisk as on Thursday (a few minutes), builds a small test function (`010.sleep`), pushes it to the local registry, invokes it, and removes the cluster again. This tests the deployment and the whole build → push → pull path. If your cluster is already running, it uses it and leaves it running;
3. your cloud credentials (step 6; it doesn't contact the cloud).

It ends with `ALL OK (Thursday)`, or `FAILED at step N: <reason>`, show that line to the instructor. A separate check for Friday (`./check_setup.sh friday`) follows before Friday.

## On the day

- **Thursday:** start Jupyter (step 4) and open `thursday.ipynb`. When the first talk begins, run the notebook's first cell: it starts OpenWhisk, which takes a few minutes.
- **Friday:** keep the `results/` folder from Thursday; Friday compares against it. Open `friday.ipynb`. OpenWhisk isn't needed on Friday, so you can stop it to free memory.
- **Cloud credentials** stay in `configs/` (step 6); the notebooks load them in the cloud sections. Credentials we provide go into the same files.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `permission denied` on `/var/run/docker.sock` (Linux) | `sudo usermod -aG docker $USER`, then log out and log back in |
| `pycurl` fails to install | Install `libcurl4-openssl-dev libssl-dev` (Linux/WSL2) or `brew install curl` (macOS), then rerun `uv sync --inexact` |
| `jupyter: command not found` after updating SeBS | You ran `uv sync` without `--inexact`; repeat step 3 |
| Jupyter can't find the `SeBS (PhDOpen)` kernel, or the setup check says the notebook runs on another Python | Rerun step 3, restart Jupyter as in step 4, and select the kernel **SeBS (PhDOpen)** |
| OpenWhisk functions fail with storage connection errors or time out | Your IP address changed: rerun the notebook's configuration cells and `get_function` (see Thursday's notebook) |
| The notebook says Docker isn't reachable | Start Jupyter from a terminal where `docker ps` works |
| `kind create cluster` fails with "could not find a log line that matches 'Reached target .*Multi-User System'" (Linux) | Too few inotify instances for the cluster's nodes: `sudo sysctl fs.inotify.max_user_instances=512` (until reboot; add it to `/etc/sysctl.conf` to keep it), then rerun `./openwhisk/deploy.sh` |
| OpenWhisk pods stuck in `Pending` or `CrashLoopBackOff` | Give Docker more memory (8 GB+), then `./openwhisk/stop.sh && ./openwhisk/deploy.sh` |
| `wsk` connection refused | The cluster isn't ready yet: wait, then `./openwhisk/status.sh` |
| Very slow everything on WSL2 | Clone under `~/`, not `/mnt/c/` |
| Apple Silicon: `exec format error` | Rosetta isn't enabled in Docker Desktop (section 1 of the Apple Silicon instructions) |
| Disk full | `docker system prune` removes unused images; keep at least 20 GB free |

