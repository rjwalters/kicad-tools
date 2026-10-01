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

Configuration is read from two layers (see below): shared cloud credentials
from **`~/.config/repo/remote.env`** (reused across every repo), with the
target repo's **`.env`** layered on top for per-repo machine settings — and
free to override any shared value. The provisioning credentials are used
locally to drive the cloud CLI; they are **never** copied to the VM.

## Usage

```
/repo:remote                   # Read .env, bring up / reuse the host, open SSH
/repo:remote --configure       # Guided setup: shared creds (~/.config/repo/remote.env) + repo .env (machine)
/repo:remote gcp               # Override REPO_REMOTE_PROVIDER for this run
/repo:remote aws
/repo:remote --status          # List instances created by this command
/repo:remote --verify          # Prove the SSH alias still reaches THIS repo's instance
/repo:remote --down            # Stop instances created by this command
/repo:remote --down --delete   # Terminate/delete them
```

First time in a repo? Run `/repo:remote --configure` to build the `.env` below,
then `/repo:remote` to launch.

The provisioning contract below is implemented **once**, as an executable
script — `scripts/repo/repo-remote.sh`, installed to
`.claude/skills/repo/scripts/repo-remote.sh`. The interactive flow here is a
thin wrapper: it runs the wizard/cost-confirmation UX, and once you say yes it
calls that script rather than re-issuing `aws`/`gcloud` calls from prose. The
same script is the **headless entry point** a non-interactive caller (e.g.
loom's `fleet add-worker`) invokes directly:

```
repo-remote up [aws|gcp]              # DRY RUN: print the resolved plan + estimated cost as JSON, create NOTHING
repo-remote up --yes [--json]        # provision (or reuse) with no prompts; requires pre-supplied cost-relevant config
repo-remote up --yes --force         # additionally override the fleet-marker guard (see below)
repo-remote status [--json]          # list instances tagged repo-remote=<name>
repo-remote verify [--json]          # prove the SSH alias still reaches this repo's instance (exit 6 if not)
repo-remote down [--yes] [--delete]  # dry-run listing without --yes; stop (or --delete to terminate) with --yes
```

`--yes` **removes the prompt, not the consent.** A cost-relevant field missing
from config — the provider, its credentials, or `REPO_REMOTE_INSTANCE_TYPE` —
is a loud, non-zero-exit failure, never a silent default that could pick a
billable size on its own. Plain `repo-remote up` (no `--yes`) is a dry run that
prints the plan and estimated hourly cost and spends nothing, so a caller can
implement a "plan shown before money is spent" check against the `--json`
output (instance id, public IP, SSH alias, estimated hourly cost).

The `--json` output for `up` (both the dry-run plan and a real provision) also
carries `estimated_cost_approximate` (bool) and `estimated_cost_basis`
(`"table"` | `"vcpu-scaled"` | `"heuristic"`) alongside
`estimated_hourly_cost_usd`, so a caller can tell an exact price-table hit
apart from a vCPU-count-scaled guess or a last-resort flat heuristic for an
instance type with no price data at all — a confidently-wrong flat number is
worse for cost consent than an honestly-vague one.

`--force` is a **separate** override with a single job: it lets `up` reuse, or
`down` stop/terminate, an instance carrying a **fleet marker** (see
[Fleet-marked hosts: the reuse and teardown
guard](#fleet-marked-hosts-the-reuse-and-teardown-guard)). It does **not**
relax the cost gate — `--force` without a pre-supplied
`REPO_REMOTE_INSTANCE_TYPE` still fails loudly.

Exit codes: `0` success (including a dry-run plan), `2` missing/invalid required
config (the cost gate), `3` provider authentication failed, `4` cloud operation
failed, `5` refused to reuse a fleet-marked instance (pass `--force`), `6`
host-identity verification failed — the host reachable at the SSH alias is not
this repo's instance, or its identity could not be established (see [Stale SSH
aliases and released public IPs](#stale-ssh-aliases-and-released-public-ips-host-identity-verification);
**not** overridable with `--force`), `64` usage error.

## Configuration — two layers

Settings come from two files, loaded in order so the repo can override the
shared defaults:

1. **`~/.config/repo/remote.env`** — shared cloud identity, reused by every
   repo. Loaded **first**. This is where the provisioning credentials belong,
   plus any default you want everywhere (e.g. `REPO_REMOTE_PROVIDER`,
   `AWS_REGION`, `REPO_REMOTE_SSH_KEY`). Honors `$XDG_CONFIG_HOME` — the exact
   path is `${XDG_CONFIG_HOME:-$HOME/.config}/repo/remote.env`. Not in any git
   repo, so it is never at risk of being committed; keep it `chmod 600`.
2. **`<repo>/.env`** (at the git root) — per-repo machine settings. Loaded
   **second**, so any variable it sets overrides the shared file. This is where
   `REPO_REMOTE_INSTANCE_ID` and the hardware/software/session knobs live. A
   repo that needs a *different* cloud account/region can also override the
   credentials here.

**Keeping the per-repo file out of the tree (`REPO_REMOTE_ENV_FILE`).** Some
repos forbid *any* `.env` in the checkout or its worktrees — worktrees are
copied and rsynced constantly, and a gitignored file is one `git add -f` away
from leaking. Set `REPO_REMOTE_ENV_FILE=<absolute path>` (e.g.
`~/.config/<repo>/remote.env`) in the process environment or in the shared
`remote.env`, and layer 2 becomes that file instead of `<repo>/.env`: it is
loaded second (overriding the shared file) and it is where `up` writes the
instance id back. A leading `~/` is expanded; an environment value wins over one
set in the shared file. Setting it inside the per-repo file itself has no
effect (that file has already been chosen by then).

**Write-back never creates `<repo>/.env`.** After a successful provision `up`
writes `REPO_REMOTE_INSTANCE_ID=<id>` into the resolved per-repo file only when
`REPO_REMOTE_ENV_FILE` is set (that file, and its parent directory, are created
if missing) or when `<repo>/.env` already exists (`--configure` creates it with
an empty pin). Otherwise it logs the id with a hint to pin it and writes
nothing — the instance still carries its `repo-remote=<name>` tag, so the next
`up` finds it. `REPO_REMOTE_NO_WRITEBACK=1` disables the write-back entirely
(the id is still logged).

**Linked worktrees warn about an in-tree `.env`.** When the checkout is a
linked git worktree (`git worktree add`, e.g. `.loom/worktrees/issue-N` —
`git rev-parse --git-dir` differs from `--git-common-dir`) and
`REPO_REMOTE_ENV_FILE` is not set, loading a pre-existing `<repo>/.env` or
writing the instance id back into it prints a loud warning naming the file and
recommending `REPO_REMOTE_ENV_FILE`. The read/write still happens; the primary
checkout never warns. When operating by hand from a linked worktree, say the
same thing to the user rather than silently updating that file.

Variables are namespaced `REPO_REMOTE_*` so they don't collide with the app's
own vars; the provisioning credentials use their standard cloud names. Either
file may set any variable — the split below is the recommended home for each,
not a hard rule.

```bash
# ── ~/.config/repo/remote.env  (shared across all repos) ─────────────────
REPO_REMOTE_PROVIDER=aws                  # aws | gcp  (default; a repo or arg can override)

# --- provisioning credentials (used locally; NEVER copied to the VM) ---
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-west-2
# gcp instead: GCP_PROJECT, GCP_ZONE, GOOGLE_APPLICATION_CREDENTIALS=/abs/sa.json

REPO_REMOTE_SSH_KEY=~/.ssh/id_ed25519     # key used for the SSH session (fine to share)
                                           # AWS: its .pub is ALSO what resolves/imports the
                                           # EC2 key pair at launch (repo#177) — the same key,
                                           # two roles, so `up` never launches keyless.

# --- dev-session auth (optional; used ON the VM) ---
# Unlike the provisioning creds above, these DO travel to the VM so gh/claude
# work there. The gh token rides the SSH channel into the container env; the
# Claude account pool (token FILES) is copied to the VM's .loom/tokens/
# (chmod 600) so a Loom install there can rotate accounts. Prefer
# scoped/short-lived tokens.
REPO_REMOTE_GH_TOKEN=                      # GitHub PAT → gh + git-over-https on the VM.
                                           # Fine-grained, scoped to the target repo. For Loom-style
                                           # label workflows grant Contents + Issues + Pull requests
                                           # (all Read/write): issue labels need Issues:write, PR labels
                                           # need Pull requests:write. Sets existing labels only — no
                                           # label *creation* needed (Loom never invents labels).

# Claude Code multi-account pool (the Loom pattern — same triples as
# lean-genius/.env). Registry lives here; the raw 1-year OAuth tokens live in
# ~/.config/repo/tokens/<file>. Account 1 becomes the default
# CLAUDE_CODE_OAUTH_TOKEN for a plain `claude`; the whole pool is copied to the
# VM for Loom rotation. A current-repo pool (its own .env ACCOUNT_* +
# .loom/tokens/) OVERRIDES this shared one.
ACCOUNT_EMAIL_1=you@example.com
ACCOUNT_KEY_1=<key>
ACCOUNT_TOKEN_FILE_1=you-example.token    # relative to ~/.config/repo/tokens/
# ACCOUNT_EMAIL_2 / ACCOUNT_KEY_2 / ACCOUNT_TOKEN_FILE_2 = ...  (add more accounts)

# ── <repo>/.env  (per-repo; overrides the shared file) ───────────────────
# --- instance (hardware) ---
REPO_REMOTE_INSTANCE_TYPE=m5.2xlarge      # gcp: machineType; a GPU family (g6e.*, g2-*) implies a GPU host
REPO_REMOTE_INSTANCE_ID=                  # RECOMMENDED: pin the exact instance (ALWAYS per-repo).
                                           # `up` writes it back here after a successful provision
                                           # (if this file exists, or REPO_REMOTE_ENV_FILE names it).
                                           # It is the expectation `repo-remote verify` checks the
                                           # live host against, and the only handle that survives a
                                           # stop/start unambiguously — see "Stale SSH aliases and
                                           # released public IPs" below.
REPO_REMOTE_DISK_GB=100
REPO_REMOTE_IMAGE=                         # optional host-image override (else: Ubuntu LTS, or the GPU AMI on GPU hosts)
REPO_REMOTE_GPU=                          # GCP accelerator (e.g. nvidia-l4:1); AWS infers GPU from the instance family

# --- dev environment (software) ---
REPO_REMOTE_DOCKERFILE=./Dockerfile       # optional: build & run this checked-in Dockerfile as the dev env (--gpus all on GPU hosts)
REPO_REMOTE_SETUP="make setup"            # optional first-boot command; fallback when no Dockerfile

# --- session ---
REPO_REMOTE_IDLE_SHUTDOWN_MIN=120         # interactive-host default; use ~20 for daemon-managed/worker hosts; 0 disables the guard entirely (see below)
REPO_REMOTE_IDLE_MARKER=                   # optional idle-exit marker path (default: /var/run/repo-remote-daemon-idle.marker)

# --- fleet-marker guard (refuse to reuse a managed fleet host) ---
REPO_REMOTE_FLEET_TAG_KEY=Fleet           # tag (AWS) / label (GCP) key that marks a managed fleet host; empty disables the check
REPO_REMOTE_FLEET_TAG_VALUE=loom          # required value for that key; empty means "any non-empty value counts"

# --- SSH ingress (AWS only; see "Security group and SSH ingress" below) ---
REPO_REMOTE_SSH_CIDR=                     # optional: pin the SG's SSH-ingress CIDR (e.g. 203.0.113.7/32);
                                           # overrides current-IP detection outright. Validated — see below
REPO_REMOTE_SSH_MIN_PREFIX=32             # narrowest IPv4 prefix accepted for SSH ingress (default 32 = one address;
                                           # set e.g. 24 to allow a known ISP block)
REPO_REMOTE_ALLOW_WORLD_SSH=              # "1" opts in, loudly, to a CIDR wider than the minimum (0.0.0.0/0, ::/0)
```

Only `REPO_REMOTE_PROVIDER` (or a provider argument) and that provider's
credentials are required — from **either** layer. Everything else falls back to
built-in defaults: GCP `e2-standard-4` / AWS `m5.xlarge`, 50 GB disk, latest
Ubuntu LTS, no GPU, 120-minute idle shutdown.

**Pin `REPO_REMOTE_INSTANCE_ID` — treat it as the default, not an option.** Any
session expected to outlive a stop/start cycle should carry an explicit pin in
the per-repo config file (a successful `up` writes one back for you when that
file exists or `REPO_REMOTE_ENV_FILE` names it). Unpinned, this
tooling re-derives its target from the never-expiring `repo-remote=<name>`
tag — the same stale handle the fleet-marker guard exists to distrust — and
`repo-remote verify` has no explicit expectation to check the live host against.
Full rationale: [Stale SSH aliases and released public
IPs](#stale-ssh-aliases-and-released-public-ips-host-identity-verification).

**Two classes of secret — treat them differently:**
- **Provisioning credentials** (`AWS_*`, `GCP_*`) drive the cloud CLI *locally*
  and are **never** copied to the VM.
- **Dev-session auth** (`REPO_REMOTE_GH_TOKEN`, the `ACCOUNT_*` Claude pool) is
  **optional** and, when set, is **placed on the VM by design** so `gh` and
  `claude` work there. The gh token rides the SSH channel into the container
  env (no file on disk); the Claude pool's **token files** are copied to the
  VM's `.loom/tokens/` at `chmod 600` (they must be files for Loom to rotate
  them). Use scoped/short-lived tokens; the whole set is wiped when the box is
  terminated. If unset, the VM stays unauthenticated and you log in there
  interactively.

**Pool resolution (layered, like the config files):** the shared pool is
`~/.config/repo/remote.env`'s `ACCOUNT_*` registry + `~/.config/repo/tokens/`.
If the **current repo** already carries its own pool (`.env` `ACCOUNT_*` +
`.loom/tokens/`, as a Loom repo does), that repo pool **wins** and is the one
shipped — so remoting a Loom repo carries *its* accounts, not the shared set.

**Credential hygiene — check first, every run:**
- If the shared file `~/.config/repo/remote.env` exists but is group- or
  world-readable, warn and offer to `chmod 600` it — it holds secrets.
- If the repo's `.env` exists but is **not** gitignored, stop and warn: it may
  hold credentials and must never be committed. Offer to add it to
  `.gitignore`.
- If neither layer supplies the provider's credentials, say exactly which
  variables are needed and point the user at `/repo:remote --configure` — don't
  silently fall back to ambient cloud auth (that would be non-deterministic
  across machines).

## `--configure` — guided `.env` setup

An interactive wizard that builds (or updates) the two config files so a plain
`/repo:remote` just works afterward. Run it on first use, or to change the
machine.

By default it writes **credentials to the shared `~/.config/repo/remote.env`**
(so you set them up once for every repo) and **machine settings to the repo's
`.env`**. Offer to put credentials in the repo `.env` instead only if the user
wants a repo-specific account/region. If `REPO_REMOTE_ENV_FILE` is set (see
above), every "repo `.env`" below means that file instead — write machine
settings there and never create an in-tree `.env`.

1. **Protect both files first.** Before writing any credential: ensure the
   repo's `.env` is gitignored (add it and say so if not); and create
   `~/.config/repo/remote.env` with `chmod 600` (mkdir -p its parent). Never
   proceed with secrets going into a tracked or world-readable file.
2. **Read what's already there.** Parse the current values from **both** the
   shared file and the repo `.env` (repo wins) and use them as defaults so the
   wizard is non-destructive to unrelated vars in either file.
3. **Provider.** Ask `aws` or `gcp`.
4. **Credentials** (written to the shared file by default). Guide, don't
   mishandle:
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
   Also write `REPO_REMOTE_INSTANCE_ID=` into the repo `.env` (empty, with the
   explanatory comment above it) so the pin is visibly *expected* rather than
   absent — `up` fills it in on the first successful provision, and from then on
   it is what `repo-remote verify` checks the live host against. If the user
   expects a long-lived box, mention that an Elastic IP / static external IP
   removes public-IP churn across stop/start at a per-hour cost.
6. **Dev environment (software).** Detect a checked-in Dockerfile
   (`./Dockerfile`, `docker/Dockerfile`, …) and offer to use it as the dev
   environment (`REPO_REMOTE_DOCKERFILE`) — the recommended path, and what makes
   GPU work cleanly. If none, offer an optional first-boot `REPO_REMOTE_SETUP`
   command instead.
7. **SSH key.** Ask which SSH key to use (`REPO_REMOTE_SSH_KEY`); it goes in
   the shared file by default (a key path is usually the same everywhere).
8. **Validate & write.** Run the provider identity check with the entered
   credentials to prove they work. Then show **both** resulting files (shared
   creds and repo `.env`, secrets masked), get a yes, and write each — merging
   into any existing content, preserving unrelated lines. Credentials +
   shared defaults go to `~/.config/repo/remote.env`; `REPO_REMOTE_*` machine
   settings go to `<repo>/.env`.
9. Offer to run `/repo:remote` right away.

## Steps

Steps 1–4, plus `--status`/`--down`, describe the provisioning contract that
`scripts/repo/repo-remote.sh` implements. The interactive flow **delegates** to
that script: it performs the credential-hygiene checks and the wizard/cost
confirmation here, then hands the resolved plan to `repo-remote up --yes`
(passing the confirmed provider/instance-type through config) rather than
issuing the `aws`/`gcloud` calls itself. This keeps a single implementation, so
the headless path and the interactive path cannot drift. The cloud-CLI
specifics below document *what the script does* and remain the reference for it.

### 1. Load and validate config

Load both layers and resolve the effective settings (a provider argument
overrides `REPO_REMOTE_PROVIDER` from either file). Run the credential-hygiene
checks above. Echo the resolved plan (provider, instance type, disk, GPU,
region/zone) without printing secret values.

### 2. Authenticate the provider with the resolved credentials

Load the shared file first, then the per-repo file (`REPO_REMOTE_ENV_FILE` if
set, else the repo `.env`) on top, into the environment
for the provisioning calls only — scoped to this command, never persisted to
the VM. Repo values override shared ones because the repo file is sourced last.
`REPO_REMOTE_ENV_FILE` itself resolves the other way round — the caller's
environment value is captured *before* the shared file is sourced, so it wins
over one the shared file sets — and a leading `~`/`~/` is expanded explicitly
(the shell does not expand it inside a quoted or environment-supplied value):

```bash
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}/repo/remote.env"
ENV_FILE_FROM_ENV="${REPO_REMOTE_ENV_FILE:-}"                  # capture BEFORE sourcing
set -a
[ -f "$CONFIG_HOME" ] && . "$CONFIG_HOME"                      # shared cloud creds + defaults
set +a
# environment value first, then whatever the shared file set, else <git-root>/.env
REPO_FILE="${ENV_FILE_FROM_ENV:-${REPO_REMOTE_ENV_FILE:-$(git rev-parse --show-toplevel)/.env}}"
case "$REPO_FILE" in                                           # expand a leading ~ / ~/
  "~")   REPO_FILE="$HOME" ;;
  "~/"*) REPO_FILE="$HOME/${REPO_FILE#\~/}" ;;
esac
set -a
[ -f "$REPO_FILE" ] && . "$REPO_FILE"                          # per-repo (overrides)
set +a
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

3. **Before starting or SSH-aliasing anything you *reused*** (either branch
   above), check the resolved instance's tags/labels for a **fleet marker** and
   stop unless `--force` was given — see **Fleet-marked hosts: the reuse and
   teardown guard** below (the same guard applies to `--down`/`down`). A
   freshly created instance is never subject to this check.
4. **A reused instance is re-aliased and its SSH ingress re-authorized, every
   time.** A stop/start cycle (e.g. the idle guard powered the box off)
   **releases** the old auto-assigned public IP and assigns a **new** one — the
   released address goes on to serve some other instance, possibly another AWS
   customer's — and the operator's own IP may have changed since the
   group's tcp/22 rule was first authorized — so on AWS, reuse re-runs the full
   ingress chain for the *currently* detected CIDR (see **Security group and SSH
   ingress (AWS)** below) and re-points the SSH alias at the freshly resolved
   public IP. The public IP is polled with a short bounded retry
   (`REPO_REMOTE_IP_POLL_ATTEMPTS` × `REPO_REMOTE_IP_POLL_INTERVAL`, default 6 ×
   2s) because AWS does not always have the new address attached the moment the
   instance reports `running`. If that budget is exhausted with still no IP,
   `up` prints an explicit warning that the **alias was not refreshed** — the
   previously written `HostName` is therefore stale — rather than silently
   leaving the old value in place.
5. **The refreshed alias is then proved to reach the right box.** After writing
   the alias, `up` asks the host at the other end for its own instance id and
   refuses (exit `6`) if it is not the instance this run resolved — see **Stale
   SSH aliases and released public IPs** below. This only covers the alias `up`
   itself just wrote; a *later* reconnect over that alias must run
   `repo-remote verify`, which fails closed.

### 4. Create the instance (with confirmation)

**Before creating anything**, show the exact command, the machine spec
(including any GPU), and the estimated hourly cost, and get an explicit yes.

Requirements for the created instance:
- Label/tag it `repo-remote=<repo-name>` so `--status`/`--down` only ever touch
  instances this command created
- Ubuntu LTS image, disk size from config
- **AWS: always attach a key pair** (`--key-name`), resolved from
  `REPO_REMOTE_SSH_KEY`'s public key (`<REPO_REMOTE_SSH_KEY>.pub`) — an
  existing account key pair with a matching fingerprint is reused,
  otherwise the public key is imported as a new one. This must never be
  skipped: a `run-instances` call with no `--key-name` launches an instance
  with `KeyName: None`, which is unreachable via SSH by design (repo#177).
  After creation, verify the launched instance's `KeyName` came back
  non-null before reporting success. As belt-and-suspenders, the same
  public key is also injected into `~ubuntu/.ssh/authorized_keys` via
  cloud-init user-data on every boot, so the host stays reachable even if
  key-pair attachment itself ever regresses.
- For GPU hosts, see **GPU hosts** below — this needs a GPU-ready image and,
  on AWS, quota-aware handling.
- Install an idle-shutdown guard (cron checking SSH sessions + CPU, running
  `shutdown -h` after `REPO_REMOTE_IDLE_SHUTDOWN_MIN`) so a forgotten VM — GPU
  ones especially — doesn't burn money. See **The idle-shutdown guard** below for
  its exact activity model, the daemon-host short-window recommendation, and the
  idle-exit marker contract.
- AWS: security group allowing SSH from the user's IP only, using
  `REPO_REMOTE_SSH_KEY`'s public key — see **Security group and SSH ingress
  (AWS)** below for exactly how the CIDR is resolved and verified. GCP: prefer
  OS Login / IAP.

If the zone/region is stocked out (common for GPU types), offer the nearest
alternative zone or the next type down rather than failing.

#### Security group and SSH ingress (AWS)

A VPC's **default** security group has no SSH ingress rule at all, so
attaching it silently (the old behavior when `REPO_REMOTE_SECURITY_GROUP` was
unset) produces a box that times out on SSH indefinitely — `describe-security-groups`
shows an empty ingress set as the only symptom. `repo-remote up` now
resolve-or-creates a dedicated security group and proves SSH ingress actually
landed before it ever calls `run-instances`:

1. **Resolve the group.** `REPO_REMOTE_SECURITY_GROUP`, if set, wins outright
   (unchanged). Otherwise, reuse a security group already tagged
   `repo-remote=<repo-name>` from a prior run; otherwise create one with that
   tag. This makes repeated `up` runs idempotent — no new SG accumulates per
   invocation.
2. **Resolve the CIDR.** `REPO_REMOTE_SSH_CIDR`, if set, wins outright.
   Otherwise, detect the current IP via an HTTPS echo service
   (`checkip.amazonaws.com`) and use it as a `/32` — but treat that detection
   as **unverified**: there is no reliable way for this tooling to confirm the
   detected address is the one SSH egress will actually use. Behind an HTTPS
   proxy it commonly isn't (an increasingly common failure mode on agent
   hosts) — the echo service returns the proxy's address, producing a
   correct-looking `/32` rule that can never match. If detection fails
   outright (or you know it will be unreliable), set `REPO_REMOTE_SSH_CIDR`
   yourself.
3. **Validate the CIDR, and fail closed.** Whatever the source, the CIDR is
   checked before anything is authorized from it, and a CIDR that cannot be
   established is a **refusal**, not a wider rule:
   - An IPv4 prefix wider than `REPO_REMOTE_SSH_MIN_PREFIX` (default `32` — a
     single address) is **refused** (exit `2`). Raise the knob deliberately
     for a known block (`REPO_REMOTE_SSH_MIN_PREFIX=24`) rather than widening
     by accident.
   - `0.0.0.0/0`, `::/0`, and anything else wider than that minimum are
     accepted **only** with `REPO_REMOTE_ALLOW_WORLD_SSH=1`, and that opt-in
     prints a `WARNING` naming the exposure so it is visible in the run log.
   - A malformed value (`not-a-cidr`, a missing prefix length) is a loud
     config failure (exit `2`), never something that reaches the AWS API.
   - **If IP detection fails and `REPO_REMOTE_SSH_CIDR` is unset, the run
     fails closed** (exit `2`, naming `REPO_REMOTE_SSH_CIDR` as the fix) and
     creates **no** ingress rule and **no** instance. There is deliberately no
     `0.0.0.0/0` fallback: one flaky HTTPS call is not consent to open tcp/22
     to the internet, and an account-wide audit cannot tell an accidental
     opening from a deliberate one. (Before this, detection failure fell back
     to `0.0.0.0/0` with only a printed notice.)
4. **Authorize idempotently, and label the rule.** `authorize-security-group-ingress`
   for tcp/22 from the resolved CIDR, via `--ip-permissions` so the rule
   carries a `Description` of `repo-remote:<repo-name>:<YYYY-MM-DD>`; a
   duplicate rule on a reused group counts as success. That marker is what
   makes each rule traceable to its owner in an audit — and what step 4a keys
   on.

   4a. **Replace, don't accumulate.** Before authorizing the current address,
   any tcp/22 rule on the group whose `Description` carries this tooling's
   `repo-remote:<repo-name>:` marker — i.e. one *this* tooling wrote on an
   earlier run — is revoked. Without this, every `up` from a new address added
   a `/32` and removed none, so a few weeks of roaming left a group admitting
   every address the operator had ever had. A rule for the address being
   authorized now is kept (a repeat run from the same place does not flap it),
   and **rules without that marker are never touched** — an operator-added or
   pre-existing rule is not this tooling's to revoke. A revoke that fails
   (e.g. credentials without `RevokeSecurityGroupIngress`) is a loud notice
   with the manual command, not a failed `up`: it leaves a stale-but-narrow
   rule behind, never a wider one.
5. **Verify before spending money.** `describe-security-groups` on the
   resolved group must show a tcp/22 rule, or the run fails loudly (exit `4`)
   before `run-instances` is ever called — this is what would have caught the
   originally reported incident in-run instead of as a bare SSH timeout.
6. **End-of-run reachability check.** After the SSH alias is written, `up`
   probes it — `ssh -o ConnectTimeout=10 -o BatchMode=yes -o
   StrictHostKeyChecking=accept-new <alias> true` — and fails loudly (exit
   `4`) if it doesn't succeed, so an unreachable instance is caught in-run
   rather than discovered on the next manual SSH attempt.

   A freshly booted guest routinely refuses connections for tens of seconds
   while cloud-init and `sshd` come up, so the probe is **retried across a
   bounded window** rather than judged on a single attempt (repo#449):
   `REPO_REMOTE_SSH_READY_TIMEOUT` (default `120`) seconds in total, one
   attempt every `REPO_REMOTE_SSH_READY_POLL_INTERVAL` (default `5`) seconds.
   Only "the host is not listening yet" errors (`Connection refused`,
   `Operation timed out`, `No route to host`, …) are retried; an
   authentication or configuration failure (`Permission denied`, or any
   unrecognized error) fails **immediately** instead of burning the window on
   something waiting cannot fix. The retry re-runs only the `ssh` probe — it
   never relaunches or restarts the instance — and the instance id is written
   back to the per-repo config file *before* the first attempt, so even a readiness
   timeout leaves the created box addressable rather than orphaned. Raise
   `REPO_REMOTE_SSH_READY_TIMEOUT` for an image that is simply slow to boot.

**Steps 1–5 run on every `up`, including one that REUSES an existing
instance** — not only at creation. The authorized CIDR is pinned to whatever
the operator's IP was when the rule was first written, and that address moves
(an observed incident: `52.119.115.124` → `104.7.12.215` between sessions), so
an instance created days ago and restarted today would otherwise still be
admitting SSH only from an address that no longer reaches it — an indefinite
SSH timeout whose only fix was revoking and re-authorizing the `/32` by hand.
Re-running the chain on reuse re-authorizes for the current CIDR, so `up`
self-heals. It is safe to repeat: the group is resolved via
`REPO_REMOTE_SECURITY_GROUP` or the `repo-remote=<repo-name>` tag (never
created anew per run), and a duplicate tcp/22 rule counts as success.

**On reuse, the refresh never *widens* an existing rule.** No path widens on a
failed detection any more (step 3), but reuse is the case where *doing nothing*
is better than failing: when detection fails and the resolved group *already*
has a tcp/22 rule, `up` leaves that rule exactly as it is and prints a notice
pointing at `REPO_REMOTE_SSH_CIDR` — the previously-working `/32` is more
likely to be right than anything this run could guess. A group with **no**
tcp/22 rule at all has nothing to preserve and nothing safe to invent, so that
case fails closed (exit `2`) instead. An explicit, opted-in
`REPO_REMOTE_SSH_CIDR=0.0.0.0/0` is not a detection failure and is still
honored verbatim; the same validation gate in step 3 applies on this path too,
so an un-opted-in `0.0.0.0/0` is refused here exactly as it is at create time.

**On reuse, step 1 resolves only — it never creates.** If no group is pinned
and none carries the `repo-remote=<repo-name>` tag, `up` prints a notice
saying ingress was **not** refreshed (with the manual
`authorize-security-group-ingress` command) and carries on. Creating a group
there would be worse than doing nothing: a new group is not attached to an
already-running instance, so it would neither restore SSH nor be reachable —
it would just leak an unused group per repo while looking like a repair. The
same limitation applies whenever a reused instance is attached to some *other*
security group (created outside this tooling): the group refreshed here is not
the one guarding it, and that group must be fixed by hand.

#### The idle-shutdown guard

The guard is a cron watchdog (`/usr/local/bin/repo-remote-idle-check`, run every
minute) installed via cloud-init user-data. It powers the host off with
`shutdown -h` once it has been idle for `REPO_REMOTE_IDLE_SHUTDOWN_MIN` minutes.

**`REPO_REMOTE_IDLE_SHUTDOWN_MIN=0` (or any non-positive value) disables the
guard outright** — no cron/watchdog script is installed at all. This is
distinct from "a very long window": a long window still counts down and
eventually shuts the host off, while `0` means the guard is never installed in
the first place, so the host never self-shuts-down. Use this for hosts that
must never be powered off by this mechanism (e.g. a persistent fleet-tagged
worker) — do **not** rely on `0` as a stand-in for "infinite window," since
prior to repo#163 that value made the guard's fallback countdown fire almost
immediately instead.

**Activity model (what keeps the host alive).** "Activity" is exactly two local
signals: an **open SSH session** (`who`) **or** a **CPU load average > 0.2**.
There is deliberately **no process-name veto** — a running background process
(including `loom-daemon`) does **not**, on its own, keep the host alive. If a
future daemon-presence veto is ever wanted, it must be added as a deliberate,
documented change to `idle_guard_userdata()`; it is not implied by the current
text. (This corrects an earlier problem statement that assumed a
`pgrep -f loom-daemon` veto existed — it never did in this repo.)

**⚠️ A backgrounded/`nohup` job does NOT hold the guard open.** Running work as
`ssh <alias> 'nohup ./long-build.sh > build.log 2>&1 &'` is the classic way to
lose a host mid-job: the moment that single non-interactive SSH command
returns, the login shell that launched the job has already exited, so `who`
reports **no session at all** — the first of the two activity signals is gone
from that instant, not when the job finishes. The job is then protected *only*
by its own CPU usage keeping the load average above `0.2`, with no
process-name veto to fall back on, so any I/O-bound, download, or license-wait
lull long enough to cover a full `REPO_REMOTE_IDLE_SHUTDOWN_MIN` window powers
the box off mid-run. (Observed: a ~20-minute `nohup`'d build on an otherwise
idle host, discovered only when the next SSH attempt timed out.) For work that
may go CPU-idle, do one of:

- **Hold a real session for the job's duration** — run it inside `tmux` (or
  `screen`) on the host and keep the SSH connection open, or simply run it in
  the foreground of an interactive session. A held session keeps `who`
  non-empty regardless of CPU load, which is the only signal that is
  unconditionally under your control.
- **Size the window to the job** — set `REPO_REMOTE_IDLE_SHUTDOWN_MIN` to
  comfortably exceed the job's longest expected quiet stretch (and remember
  `0` disables the guard entirely, which is the right choice only for hosts
  that must never self-shut-down).

**Idle window — pick per host role:**

- **Interactive hosts** (a human SSHes in to work): keep the default
  `REPO_REMOTE_IDLE_SHUTDOWN_MIN=120`. The generous 2-hour margin is sized for
  "operator stepped away from a session" so the box isn't yanked out from under
  someone mid-task.
- **Daemon-managed / worker hosts** (e.g. a box running `loom-daemon` for a
  fleet, with no interactive operator): use a **short window such as
  `REPO_REMOTE_IDLE_SHUTDOWN_MIN=20`**. These hosts have no human session to
  protect, so the "operator forgot a session" margin is pure wasted spend — on a
  c7i.2xlarge-class worker (~$8.60/day) the difference between a 120- and a
  20-minute window is real money once the fleet goes quiet.

**Idle-exit marker contract (`REPO_REMOTE_IDLE_MARKER`).** For daemon-managed
hosts, the guard also honors an **idle-exit marker file** so a daemon can hand
the guard a precise "idle since" time instead of waiting for the guard's own
once-a-minute load sampling to first read idle. This is a self-contained
contract published by `repo:remote` — the guard works standalone whether or not
anything ever writes the file:

- **Path.** `REPO_REMOTE_IDLE_MARKER` sets the file path the guard watches;
  unset, it defaults to **`/var/run/repo-remote-daemon-idle.marker`**. The path
  is always embedded in the generated guard, so the branch is present-but-inert
  until the file exists on-host.
- **Semantics.** When the marker file **exists**, the guard treats its **mtime**
  as an authoritative "idle since" timestamp and powers off once that mtime is
  older than `REPO_REMOTE_IDLE_SHUTDOWN_MIN` minutes — **replacing** (not
  supplementing) its own internal stamp countdown for that pass. In other words:
  shutdown fires `IDLE_MIN` minutes after the marker's mtime, independent of when
  the guard's SSH/load sampling happened to first observe idleness.
- **Safety precedence.** An active SSH session or CPU load > 0.2 still vetoes
  shutdown *before* the marker is consulted — the guard never powers off a host
  someone is actively using, even if a stale marker is present. A marker with a
  **future** mtime (clock skew) yields a negative age and simply never triggers,
  so it can't cause a spurious power-off.
- **Producer contract (for a daemon side, e.g. loom's idle-exit).** On a clean
  idle-exit, `touch` the marker path (creating/updating its mtime). To reset the
  clock when work resumes, remove the file (or `touch` it again). The on-host
  image is Ubuntu (GNU coreutils), so the guard reads the mtime with
  `stat -c %Y`. This repo neither writes nor depends on any daemon writing the
  file — the convention stands alone and a daemon may adopt it whenever it ships.

On a daemon-managed host the two stages compose: the daemon's own idle-exit
(writing the marker) is the first stage; this guard, `IDLE_MIN` minutes later, is
the second and final stage that actually powers the box off.

#### Fleet-marked hosts: the reuse and teardown guard

The idle guard above is attached **once, at creation** — this command never
re-attaches user-data to an instance it reuses, so the guard a host carries is
whatever it got the day it was created. What `up` reuse *does* do is **start a
stopped instance and rewrite this repo's SSH alias to point at it**; what `down`
does to that same resolved instance is **stop it, or — with `--delete` —
terminate it, disk and all**. Both verbs resolve their target from the same
handles — a pinned `REPO_REMOTE_INSTANCE_ID`, or the `repo-remote=<name>`
tag/label — and neither handle ever expires. A box provisioned once for an
ephemeral dev session can since have been repurposed into a persistent,
daemon-managed fleet worker while still carrying the old tag, at which point
ephemeral dev-session tooling would silently start-and-re-alias it (`up`) or
stop/terminate it (`down`) as if it were still just a throwaway dev box. That is
the second finding of an operator incident (private tracker), where `repo-remote=anvil`
tooling kept rediscovering the host that had become a fleet host; `down
--delete` against that same stale handle is the strictly worse outcome — the
disk is gone, unrecoverable.

**The check.** Before starting or aliasing a **reused** instance on `up` — on
both AWS reuse branches (pinned id and `repo-remote=<name>` tag lookup) and on
the GCP existing-instance branch — or before stopping/terminating **any**
instance `down` resolves, the resolved instance's **AWS tags** / **GCP labels**
are read and matched against a fleet marker, by default **`Fleet=loom`** (the
tag 2am's own remediation sets on its persistent workers). If it matches:

- **Without `--force`**: the run **stops with exit `5`** and a message naming the
  instance, the marker it found, and the override. On `up`, nothing is started,
  nothing is terminated, and the SSH alias is left untouched. On `down`, nothing
  is stopped or terminated.
- **With `--force`**: it warns loudly that it is about to touch what looks like a
  managed fleet host, then proceeds as normal.

**`down`'s multi-id batch semantics.** Unlike `up` (always exactly one resolved
instance), AWS `down`'s tag-discovery path can resolve **multiple** instances at
once. If **any** resolved instance in that set carries the fleet marker, the
**whole batch is refused** — none of the resolved instances are stopped or
terminated, not even the unmarked ones — rather than silently acting on a
subset. `--force` overrides for the whole batch at once, the same as the
single-instance case. A **dry run** (`down` without `--yes`) never touches a
cloud resource, so it is never blocked by this guard even against a
fleet-marked instance; instead, the dry-run listing **annotates** which of the
listed instances carry the marker (via a `fleet_marked` array in `--json`
output, or an inline `NOTE:` line otherwise) so an operator can see, before
spending nothing, which instances a subsequent `--yes` run would refuse.

**Configuration.**

- `REPO_REMOTE_FLEET_TAG_KEY` — the tag/label key to look for. Defaults to
  `Fleet`. **Set it to the empty string to disable the check entirely.** GCP
  label keys are lower-case, so the configured key is lower-cased for the GCP
  lookup (`Fleet` → `labels.fleet`).
- `REPO_REMOTE_FLEET_TAG_VALUE` — the value that counts as a match. Defaults to
  `loom`; comparison is case-insensitive (GCP lower-cases label values). Set it
  to the empty string to treat *any* non-empty value of the key as a match.

**Scope — what this deliberately is not.** It is a **provisioning-time check
against declared metadata** an operator already had to set elsewhere, not an
on-host "is `loom-daemon` running" heuristic. The idle guard's activity model
above is unchanged, and gains no process-name veto from this; the two are
independent. It also does not fire on **creation** (a brand-new instance has no
prior role to protect) or on a **dry run** (`repo-remote up` without `--yes`
touches no cloud resource at all).

**Related but distinct**: if the fleet host should also never power itself off,
that is the idle guard's `REPO_REMOTE_IDLE_SHUTDOWN_MIN=0` opt-out above. This
guard stops *this tooling* from touching the host; that one stops the *host*
from shutting itself down.

#### Stale SSH aliases and released public IPs: host-identity verification

**An EC2 instance's auto-assigned public IPv4 address is released the moment the
instance stops**, and a *different* address is assigned on its next start. The
address it gave up does not sit idle — AWS hands it to whoever launches an
instance next, which is routinely **another AWS customer** entirely. Nothing
about "the disk is retained across a stop/start" extends to the address: the
volume survives, the IP does not. GCP behaves the same way for an ephemeral
external IP.

**The SSH alias this tooling writes is a cached copy of that address.** It is
**only as fresh as** the last `up` (or `verify`) run — nothing keeps it current
between sessions. `up` does re-resolve the public IP and rewrite the
`repo-remote-<name>` stanza on *every* invocation, including every reuse branch
(see step 3 above) — but if a session is stopped and restarted without an
intervening `up`, or an agent simply reconnects over an alias written an hour
ago, the stanza still carries the **old** address.

**Why every other check passes anyway.** This is what makes the failure mode
dangerous rather than merely annoying: the alias resolves, `ssh` connects, and
key authentication succeeds — on the *stranger's* box, because SSH ingress and
your public key are attributes of *your* config, and a wide-open `sshd` on a
third party's host will still complete a connection. In the incident behind this
check, three agents worked for a stretch against a machine that was not theirs,
wrote work products to it (lost), and drew conclusions from its disk contents.
The only thing that eventually refused was an unrelated per-file SHA-256
precondition.

**The check.** `repo-remote verify` (`/repo:remote --verify`) opens **one** SSH
session over `repo-remote-<name>`, asks the host for its **own instance id**, and
compares it with the id this repo expects — a pinned `REPO_REMOTE_INSTANCE_ID`,
else the instance discovered via the `repo-remote=<name>` tag (on GCP, the
derived instance *name*). The id is the one property that cannot be inherited
along with a recycled IP address. It is read, in order, from:

1. `/etc/repo-remote-instance-id` — a marker file this tool writes at provision
   time via cloud-init user-data. The value is read by the *host* from its own
   metadata service, so the marker can only ever name the box it sits on.
   Override the path with `REPO_REMOTE_HOST_ID_FILE`.
2. The instance metadata service — IMDSv2 (token first), then IMDSv1, then GCP's
   `computeMetadata/v1/instance/name`. This is why an instance provisioned
   *before* the marker existed is still verifiable.
3. cloud-init's `/var/lib/cloud/data/instance-id`.

Outcomes:

- **Match** → exit `0` (with `--json`: `"verified":true` plus
  `expected_instance_id` / `host_instance_id`).
- **Mismatch** → exit `6`, with a refusal naming both ids, the alias, and the
  remediation. **`--force` does not override this** — that flag is the
  fleet-marker override and nothing else.
- **Identity could not be established** (host unreachable, no marker and no
  reachable metadata service) → also exit `6`. It **fails closed**: "I could not
  tell" is not evidence that the host is the right one.

**It is also wired into `up`**, immediately after the SSH alias is written and
the reachability probe passes, so a normal session-start sequence exercises it
without anyone having to remember. There, a **mismatch is still fatal (exit
`6`)** — the alias `up` just wrote does not reach the instance `up` just
resolved — but an identity it *cannot establish* is a loud warning rather than a
failure: `up` re-resolved the address from the cloud API in that same run, so the
alias is fresh by construction, and failing the run would break provisioning on
any box with IMDS locked down and no marker yet. The fail-closed half is
`verify`, which exists precisely for the reconnect case where nothing re-derived
the address.

**Two things that make this a non-issue rather than a near-miss:**

- **Pin `REPO_REMOTE_INSTANCE_ID`.** This is the recommended default for any
  session expected to survive a stop/start cycle, not merely one option among
  several — see the configuration walkthrough above. `up` writes it back to the
  per-repo config file for you after a successful provision (see
  `REPO_REMOTE_ENV_FILE` above for when it does and does not). A pinned id makes the
  expectation *explicit* (and makes `verify` need no cloud call at all, so it is
  cheap enough to run before every session); an unpinned run re-derives the
  expectation from the same never-expiring `repo-remote=<name>` tag the
  fleet-marker guard exists to distrust.
- **Consider an Elastic IP (AWS) / static external IP (GCP)** for a long-lived
  box, which removes the churn at its source — the address stays yours across
  stop/start, so the alias cannot go stale. It bills per hour while *not*
  attached to a running instance, so it is a deliberate cost trade-off this
  tooling does not make on your behalf; allocate and associate one by hand if
  the box is long-lived enough to be worth it.

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

**After a successful create, write the new ID back to the per-repo config
file** — `REPO_REMOTE_ENV_FILE` if set, else the repo's `.env` at the git root;
never the shared file, the instance handle is per-repo — so the next run reuses
it automatically. Never *create* `<repo>/.env` for this: if neither the override
nor an existing `.env` is present (or `REPO_REMOTE_NO_WRITEBACK=1`), report the
id and suggest pinning it instead:

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

**Never copy the *provisioning* `.env` or cloud keys to the VM** — the
`.gitignore` filter already excludes a gitignored `.env`; double-check it's
excluded and call it out. This is the provisioning-credential class (Safety
Rule 3). The separate, opt-in **dev-session auth** (the gh token and the Claude
account pool) *is* placed on the VM deliberately — see step 6a; that path
replaces interactive `gh auth login` / `claude` login when the tokens are
configured.

### 6. Bootstrap the dev environment

How the repo declares its environment decides the path:

- **`REPO_REMOTE_DOCKERFILE` set (preferred)** — the repo carries its own
  environment. On the host, build that Dockerfile (context = repo root) and run
  it as a long-lived dev container with the synced repo mounted, adding
  `--gpus all` on GPU hosts:

  ```bash
  docker build -t repo-remote-<name> -f "$REPO_REMOTE_DOCKERFILE" ~/<repo-name>
  docker run -d --name repo-remote-<name> $GPUS \
    -e GH_TOKEN -e CLAUDE_CODE_OAUTH_TOKEN \
    -v ~/<repo-name>:/work -w /work repo-remote-<name> sleep infinity
  #   GPUS="--gpus all" on GPU hosts, empty otherwise
  #   GH_TOKEN / CLAUDE_CODE_OAUTH_TOKEN are read from the remote shell env,
  #   set inline over SSH from the resolved pool (see 6a) — never a file.
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

#### 6a. Wire dev-session auth (gh + Claude account pool)

Only if the corresponding secrets are configured — otherwise skip and leave the
VM to interactive login.

1. **Resolve the pool (repo wins over shared).** If the current repo carries its
   own pool (`.env` `ACCOUNT_*` **and** `.loom/tokens/`), use it; else fall back
   to the shared `~/.config/repo/remote.env` registry + `~/.config/repo/tokens/`.

2. **gh — inline, no file.** Export the resolved `REPO_REMOTE_GH_TOKEN` as
   `GH_TOKEN` in the remote shell for the `docker run` above, then inside the
   container `gh auth setup-git` and confirm with `gh auth status`. The token
   lives only in the container env. **Note:** `gh` infers the repo from the
   local `.git` remote — so `gh pr/issue` commands need the **clone** path
   (step 5), not a rsync-only tree (which excludes `.git`). If the VM has no
   `.git`, pass `-R <owner>/<repo>` explicitly for label/PR/issue operations.

3. **Claude pool — token files (Loom needs them as files).** Copy the resolved
   `*.token` files to the VM at `~/<repo-name>/.loom/tokens/` (`chmod 600`,
   `chmod 700` the dir) and append the `ACCOUNT_*` registry to
   `~/<repo-name>/.env` on the VM, reproducing the Loom layout so a Loom install
   there rotates accounts. Set `CLAUDE_CODE_OAUTH_TOKEN` (for the `docker run`
   env) to **account 1's** token so a plain `claude` works immediately.

   ```bash
   # local -> VM, over the SSH channel; never the provisioning creds.
   # NOTE: rsync --chmod is GNU-rsync only and fails on macOS's system rsync,
   # so set the perms in a follow-up ssh step instead of relying on it.
   rsync -az -e "ssh -i $REPO_REMOTE_SSH_KEY" \
     "<resolved-tokens-dir>/" <host>:~/<repo-name>/.loom/tokens/
   ssh -i "$REPO_REMOTE_SSH_KEY" <host> \
     'chmod 700 ~/<repo-name>/.loom/tokens && chmod 600 ~/<repo-name>/.loom/tokens/*.token'
   ```

4. **Verify:** `docker exec repo-remote-<name> bash -lc 'claude --version && gh auth status'`.
   Report which account is active and how many are in the pool.

Then, either path:
- Claude Code ships **in the Dockerfile** (container path); for the no-Dockerfile
  path, offer to install it (`curl -fsSL https://claude.ai/install.sh | bash`).
- Write/refresh a local SSH config entry so the connection is one word:

```
Host repo-remote-<name>
    HostName <ip-or-iap-alias>
    User <user>
    IdentityFile <REPO_REMOTE_SSH_KEY>
    # GCP+IAP: use a ProxyCommand via `gcloud compute start-iap-tunnel`
```

### 7. Open the SSH session

**Verify the host identity first — before you trust the session**, and do not
substitute a plain reachability ping for it. A bare `ssh … 'echo SSH OK'` proves
only that *something* answered; it passes just as happily on an unrelated AWS
customer's instance that inherited the public IP released when yours was last
stopped (see [Stale SSH aliases and released public
IPs](#stale-ssh-aliases-and-released-public-ips-host-identity-verification)).
This check subsumes reachability — it cannot pass without a working session — so
it replaces that ping rather than adding a step to it:

```bash
repo-remote verify            # exit 0 = this alias really is your instance
                              # exit 6 = WRONG HOST (or unverifiable) — stop here
```

**A non-zero exit is a full stop.** Do not open the session, do not read from the
host, and above all do not write to it: on a mismatch the box at the other end
belongs to someone else. Re-run `repo-remote up --yes` to re-resolve the public
IP and rewrite the alias, then verify again. This applies to *every* reconnect,
not just the first one after provisioning — an alias written before a stop/start
cycle is exactly the stale handle this guards against.

Once it passes, open a new terminal window with the session. Where it lands depends on the
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
On a freshly provisioned box, `/repo:sudo` is the companion step that unblocks
root-level remediation over SSH — a non-interactive SSH session can't answer a
`sudo` password prompt, so an agent stalls on it until a validated
passwordless-sudo drop-in is installed.

### 8. Report

End with a compact status block: instance name/ID, zone, machine type (and GPU),
hourly cost estimate, idle-shutdown window, the SSH alias, whether the ID was
written back to `.env`, and the teardown command (`/repo:remote --down`).

## `--verify`

Delegates to `repo-remote verify`. Opens one SSH session over
`repo-remote-<name>`, reads the host's own instance id, and compares it with the
id this repo expects. Exit `0` means the alias really does reach your instance;
exit `6` means it does not, or that the identity could not be established at all
(it fails closed). Mutates nothing and — with `REPO_REMOTE_INSTANCE_ID` pinned —
makes no cloud API call, so it is cheap enough to run before every session. Full
rationale: [Stale SSH aliases and released public
IPs](#stale-ssh-aliases-and-released-public-ips-host-identity-verification).

## `--status` and `--down`

Both delegate to the shared script (`repo-remote status` / `repo-remote down`),
which resolves the same two config layers and only ever touches instances
carrying this repo's `repo-remote` tag. `repo-remote down` without `--yes` is a
dry run that lists exactly what would stop; `--yes` acts, and `--delete`
terminates. `down` is subject to the same fleet-marker guard as `up`'s reuse
path — see **Fleet-marked hosts: the reuse and teardown guard** above.

- `--status`: list all instances labeled `repo-remote=<repo-name>` with state
  and uptime; estimate accumulated cost. Uses the resolved credentials (shared
  file + repo `.env`).
- `--down`: stop them (confirm first, listing exactly what will stop);
  `--down --delete` terminates/deletes instead — confirm with the instance
  names spelled out, since disks go with them (the Claude pool token files on
  the disk go with them too). On delete, offer to clear
  `REPO_REMOTE_INSTANCE_ID` from `.env` so the next run starts fresh.

## Safety Rules

1. **Never create, stop, or delete cloud resources without showing the exact
   plan and getting a yes** — including estimated cost for creation (call out
   GPU pricing especially)
2. **Only ever touch instances carrying this repo's `repo-remote` label** (or
   the pinned `REPO_REMOTE_INSTANCE_ID`) — never enumerate-and-guess
3. **Two secret classes, handled differently** — **provisioning credentials**
   (`AWS_*`/`GCP_*`) are local-only and **never** reach the VM; **dev-session
   auth** (`REPO_REMOTE_GH_TOKEN`, the `ACCOUNT_*` Claude pool) is opt-in and
   goes to the VM *by design* (gh token in the container env; pool token files
   at `chmod 600` under the VM's `.loom/tokens/`). Never copy the provisioning
   `.env` wholesale — carry only the resolved dev-session secrets.
4. **Keep both config files out of harm's way** — refuse to run if the repo's
   `.env` exists and is not gitignored (it may hold credentials); warn and offer
   to `chmod 600` the shared `~/.config/repo/remote.env` if it's readable by
   others
5. **Always install the idle-shutdown guard** — a VM that outlives the session
   should turn itself off, unless the guard is explicitly disabled via
   `REPO_REMOTE_IDLE_SHUTDOWN_MIN=0`
6. **Never trust the SSH alias without verifying the host identity** — run
   `repo-remote verify` before opening a session or writing anything through
   `repo-remote-<name>`, and treat exit `6` as a full stop. An auto-assigned
   public IP is released on stop and reassigned to another AWS customer, so a
   resolving alias and a successful login prove nothing about *which* machine
   you reached
