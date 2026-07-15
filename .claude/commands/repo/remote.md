---
name: "remote"
description: "Launch a cloud dev session on GCP or AWS with this repo ready to go, then open an SSH session"
domain: repo
type: command
user-invocable: true
---

# /repo:remote — Remote Dev Session

Stand up (or reuse) a cloud VM with this repository cloned and synced, then
open a live SSH session in a new terminal window — landing in the repo, ready
to run `claude` and continue the work on the remote host.

Configuration and credentials are read from the target repo's **`.env`**
(see below). The provisioning credentials there are used locally to drive the
cloud CLI; they are **never** copied to the VM.

## Usage

```
/repo:remote                   # Read .env, bring up / reuse the host, open SSH
/repo:remote --configure       # Guided setup: create/update .env (provider, creds, machine)
/repo:remote gcp               # Override REPO_REMOTE_PROVIDER for this run
/repo:remote aws
/repo:remote --status          # List instances created by this command
/repo:remote --down            # Stop instances created by this command
/repo:remote --down --delete   # Terminate/delete them
```

First time in a repo? Run `/repo:remote --configure` to build the `.env` below,
then `/repo:remote` to launch.

## Configuration — `.env`

Read settings from the target repo's `.env` (at the git root). Variables are
namespaced `REPO_REMOTE_*` so they don't collide with the app's own vars; the
provisioning credentials use their standard cloud names.

```bash
REPO_REMOTE_PROVIDER=aws                  # aws | gcp

# --- provisioning credentials (used locally; NEVER copied to the VM) ---
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-west-2
# gcp instead: GCP_PROJECT, GCP_ZONE, GOOGLE_APPLICATION_CREDENTIALS=/abs/sa.json

# --- instance (hardware) ---
REPO_REMOTE_INSTANCE_TYPE=m5.2xlarge      # gcp: machineType; a GPU family (g6e.*, g2-*) implies a GPU host
REPO_REMOTE_INSTANCE_ID=                  # reuse this exact instance when set
REPO_REMOTE_DISK_GB=100
REPO_REMOTE_IMAGE=                         # optional host-image override (else: Ubuntu LTS, or the GPU AMI on GPU hosts)
REPO_REMOTE_GPU=                          # GCP accelerator (e.g. nvidia-l4:1); AWS infers GPU from the instance family

# --- dev environment (software) ---
REPO_REMOTE_DOCKERFILE=./Dockerfile       # optional: build & run this checked-in Dockerfile as the dev env (--gpus all on GPU hosts)
REPO_REMOTE_SETUP="make setup"            # optional first-boot command; fallback when no Dockerfile

# --- session ---
REPO_REMOTE_IDLE_SHUTDOWN_MIN=120
REPO_REMOTE_SSH_KEY=~/.ssh/id_ed25519     # key used for the SSH session
```

Only `REPO_REMOTE_PROVIDER` (or a provider argument) and that provider's
credentials are required. Everything else falls back to built-in defaults: GCP
`e2-standard-4` / AWS `m5.xlarge`, 50 GB disk, latest Ubuntu LTS, no GPU,
120-minute idle shutdown.

**Credential hygiene — check first, every run:**
- If `.env` exists but is **not** gitignored, stop and warn: it holds
  credentials and must never be committed. Offer to add it to `.gitignore`.
- If there's no `.env`, or the provider's credentials are missing, say exactly
  which variables are needed and point the user at `/repo:remote --configure`
  — don't silently fall back to ambient cloud auth (that would be
  non-deterministic across machines).

## `--configure` — guided `.env` setup

An interactive wizard that builds (or updates) the target repo's `.env` so a
plain `/repo:remote` just works afterward. Run it on first use, or to change
the machine.

1. **Protect the file first.** Before writing any credential, ensure `.env` is
   gitignored; if it isn't, add it and say so. Never proceed with secrets going
   into a tracked file.
2. **Read what's already there.** If `.env` exists, parse the current
   `REPO_REMOTE_*` and cloud-cred values and use them as defaults so the wizard
   is non-destructive to unrelated vars.
3. **Provider.** Ask `aws` or `gcp`.
4. **Credentials.** Guide, don't mishandle:
   - If a working CLI session or profile already exists (`aws sts
     get-caller-identity`, `gcloud auth list`), offer to reuse its
     account/region/project and derive what you can.
   - For the secret keys themselves, prompt the user to paste them (or point
     them at where to generate an IAM key / service-account JSON). **Never echo
     a secret value back**; confirm by identity check, not by printing.
5. **Machine (hardware).** Ask instance type (offer a couple of sensible sizes
   with rough hourly prices), disk size, and idle-shutdown window. A GPU
   instance family (AWS `g6e.*`, GCP `g2-*`) implies a GPU host — on GCP also
   ask the accelerator (`REPO_REMOTE_GPU`, e.g. `nvidia-l4:1`) with rough cost.
6. **Dev environment (software).** Detect a checked-in Dockerfile
   (`./Dockerfile`, `docker/Dockerfile`, …) and offer to use it as the dev
   environment (`REPO_REMOTE_DOCKERFILE`) — the recommended path, and what makes
   GPU work cleanly. If none, offer an optional first-boot `REPO_REMOTE_SETUP`
   command instead.
7. **SSH key.** Ask which SSH key to use (`REPO_REMOTE_SSH_KEY`).
8. **Validate & write.** Run the provider identity check with the entered
   credentials to prove they work. Then show the resulting `.env` (secrets
   masked), get a yes, and write it — merging into any existing `.env`,
   preserving unrelated lines.
9. Offer to run `/repo:remote` right away.

## Steps

### 1. Load and validate config

Read `.env` from the git root and resolve the effective settings (a provider
argument overrides `REPO_REMOTE_PROVIDER`). Run the credential-hygiene checks
above. Echo the resolved plan (provider, instance type, disk, GPU, region/zone)
without printing secret values.

### 2. Authenticate the provider with the `.env` credentials

Load the credentials into the environment for the provisioning calls only —
scoped to this command, never persisted to the VM:

```bash
set -a; . "$(git rev-parse --show-toplevel)/.env"; set +a
```

- **AWS**: the exported `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` /
  `AWS_REGION` are picked up by the CLI. Confirm identity:
  `aws sts get-caller-identity`.
- **GCP**: activate the service account key, then confirm:
  ```bash
  gcloud auth activate-service-account --key-file="$GOOGLE_APPLICATION_CREDENTIALS"
  gcloud config set project "$GCP_PROJECT"
  ```

If authentication fails, report the provider's error and stop — do not fall
back to a different account.

### 3. Reuse or find the instance

1. **`REPO_REMOTE_INSTANCE_ID` is set** → target that instance directly.
   - RUNNING → reuse it (skip to step 5).
   - STOPPED → offer to start it (faster and cheaper than creating).
   - Gone/terminated → say so, clear the stale ID, and continue to create.
2. **No pinned ID** → look for one this command created, labeled/tagged
   `repo-remote=<repo-name>` (repo name = basename of the git root):

```bash
aws ec2 describe-instances \
  --filters "Name=tag:repo-remote,Values=<name>" "Name=instance-state-name,Values=running,stopped"
gcloud compute instances list --filter="labels.repo-remote=<name>" \
  --format="table(name,zone,status,machineType)"
```

RUNNING → offer reuse; STOPPED → offer to start.

### 4. Create the instance (with confirmation)

**Before creating anything**, show the exact command, the machine spec
(including any GPU), and the estimated hourly cost, and get an explicit yes.

Requirements for the created instance:
- Label/tag it `repo-remote=<repo-name>` so `--status`/`--down` only ever touch
  instances this command created
- Ubuntu LTS image, disk size from config
- For GPU hosts, see **GPU hosts** below — this needs a GPU-ready image and,
  on AWS, quota-aware handling.
- Install an idle-shutdown guard (cron checking SSH sessions + CPU, running
  `shutdown -h` after `REPO_REMOTE_IDLE_SHUTDOWN_MIN`) so a forgotten VM — GPU
  ones especially — doesn't burn money
- AWS: security group allowing SSH from the user's IP only, using
  `REPO_REMOTE_SSH_KEY`'s public key. GCP: prefer OS Login / IAP.

If the zone/region is stocked out (common for GPU types), offer the nearest
alternative zone or the next type down rather than failing.

#### GPU hosts

Treat the host as a GPU box when the instance type is a GPU family (AWS
`g5`/`g6`/`g6e`/`p4`/`p5`; GCP `g2`/`a2`) **or** `REPO_REMOTE_GPU` is set —
infer `gpu=true` from the family so the user needn't set a separate flag.

**Image — the key to a *working* GPU box; don't hand-roll a driver install:**

- **AWS:** unless `REPO_REMOTE_IMAGE` overrides, default to the latest *Deep
  Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*. It ships the NVIDIA
  driver, Docker, and `nvidia-container-toolkit`, so `nvidia-smi` and
  `docker run --gpus all` work out of the box:

  ```bash
  aws ec2 describe-images --owners amazon \
    --filters 'Name=name,Values=Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*' \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text
  ```

- **GCP:** attach the accelerator (`--accelerator type=<type>,count=<n>` from
  `REPO_REMOTE_GPU`) and use a GPU-ready image, or add the documented NVIDIA
  driver + `nvidia-container-toolkit` install as a startup script.

**Quota-aware error (AWS):** the "Running On-Demand G and VT instances" quota
defaults to **0**, so the first GPU launch fails with `VcpuLimitExceeded`.
Detect that specific error and print the exact remediation instead of the raw
message: **Service Quotas → EC2 → quota code `L-DB2E81BA`** → request a limit
≥ the instance's vCPU count, then retry once approved.

**After a successful create, write the new ID back to `.env`** so the next run
reuses it automatically:

```
REPO_REMOTE_INSTANCE_ID=<new-id>
```

Update the line in place if present, else append it. Report the edit.

### 5. Get the repo onto the instance

- **Repo has an `origin` remote** and forge auth can be used non-interactively
  (e.g. `gh auth token` for GitHub over HTTPS): clone on the VM, check out the
  current branch.
- **Then sync uncommitted work** (or, with no usable remote, sync the whole
  tree): rsync the working tree over SSH, excluding gitignored content:

```bash
rsync -az --delete \
  --filter=':- .gitignore' --exclude '.git/' \
  ./ <host>:~/<repo-name>/
```

**Never copy `.env`, keys, or anything credential-like to the VM** — the
`.gitignore` filter already excludes a gitignored `.env`; double-check it's
excluded and call it out. The VM authenticates to the forge on its own (e.g.
`gh auth login` there), not with your local secrets.

### 6. Bootstrap the dev environment

How the repo declares its environment decides the path:

- **`REPO_REMOTE_DOCKERFILE` set (preferred)** — the repo carries its own
  environment. On the host, build that Dockerfile (context = repo root) and run
  it as a long-lived dev container with the synced repo mounted, adding
  `--gpus all` on GPU hosts:

  ```bash
  docker build -t repo-remote-<name> -f "$REPO_REMOTE_DOCKERFILE" ~/<repo-name>
  docker run -d --name repo-remote-<name> $GPUS \
    -v ~/<repo-name>:/work -w /work repo-remote-<name> sleep infinity
  #   GPUS="--gpus all" on GPU hosts, empty otherwise
  ```

  This is what makes GPU clean: the **host** (GPU AMI) supplies the driver +
  `nvidia-container-toolkit`; the **repo's Dockerfile** supplies CUDA and the
  toolchain — nothing per-repo is guessed or installed by hand.

- **No Dockerfile** — install baseline tooling on first boot (git,
  build-essential, and what the repo obviously needs from `pyproject.toml`,
  `package.json`, `Cargo.toml`, …); run `REPO_REMOTE_SETUP` if set.

**GPU sanity check (GPU hosts)** — before handing over, prove the GPU is live
and surface it; don't let the user find a dead GPU after they start:

```bash
nvidia-smi                                            # host driver
docker exec repo-remote-<name> nvidia-smi             # the dev container sees it
# no container: docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

If it fails, report it (driver/AMI or toolkit mismatch) and stop rather than
proceeding as if the box were ready.

Then, either path:
- Offer to install Claude Code so you can pick the work right back up
  (`curl -fsSL https://claude.ai/install.sh | sh`, or the npm package)
- Write/refresh a local SSH config entry so the connection is one word:

```
Host repo-remote-<name>
    HostName <ip-or-iap-alias>
    User <user>
    IdentityFile <REPO_REMOTE_SSH_KEY>
    # GCP+IAP: use a ProxyCommand via `gcloud compute start-iap-tunnel`
```

### 7. Open the SSH session

Verify reachability first:

```bash
ssh -o ConnectTimeout=30 repo-remote-<name> 'echo "SSH OK: $(hostname)"'
```

Then open a new terminal window with the session. Where it lands depends on the
environment path:

- **Dev container running** → drop straight into it, at the mounted repo:
  `ssh -t repo-remote-<name> 'docker exec -it -w /work repo-remote-<name> bash -l'`
- **No container** → land in the repo dir on the host:
  `ssh -t repo-remote-<name> 'cd ~/<repo-name>; exec $SHELL -l'`

Claude Code cannot host an interactive SSH session itself, so hand it to the OS
(substituting the appropriate command above):

- **macOS**: `osascript -e 'tell app "Terminal" to do script "<ssh command>"' -e 'tell app "Terminal" to activate'`
  (if the user runs iTerm2, use the equivalent iTerm AppleScript)
- **Linux**: try `x-terminal-emulator -e <ssh command>`
- **Fallback**: print the command and tell the user to run it in a separate
  terminal

Tell the user they can start `claude` in that session to continue on the remote.

### 8. Report

End with a compact status block: instance name/ID, zone, machine type (and GPU),
hourly cost estimate, idle-shutdown window, the SSH alias, whether the ID was
written back to `.env`, and the teardown command (`/repo:remote --down`).

## `--status` and `--down`

- `--status`: list all instances labeled `repo-remote=<repo-name>` with state
  and uptime; estimate accumulated cost. Uses the `.env` credentials.
- `--down`: stop them (confirm first, listing exactly what will stop);
  `--down --delete` terminates/deletes instead — confirm with the instance
  names spelled out, since disks go with them. On delete, offer to clear
  `REPO_REMOTE_INSTANCE_ID` from `.env` so the next run starts fresh.

## Safety Rules

1. **Never create, stop, or delete cloud resources without showing the exact
   plan and getting a yes** — including estimated cost for creation (call out
   GPU pricing especially)
2. **Only ever touch instances carrying this repo's `repo-remote` label** (or
   the pinned `REPO_REMOTE_INSTANCE_ID`) — never enumerate-and-guess
3. **Never copy `.env`, credentials, or keys to the VM** — provisioning creds
   are local-only; the VM authenticates to the forge on its own
4. **Refuse to run if `.env` is not gitignored** — it holds credentials
5. **Always install the idle-shutdown guard** — a VM that outlives the session
   should turn itself off
