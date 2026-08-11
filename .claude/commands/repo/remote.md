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
failed, `5` refused to reuse a fleet-marked instance (pass `--force`), `64`
usage error.

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
REPO_REMOTE_INSTANCE_ID=                  # reuse this exact instance when set (ALWAYS per-repo)
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
REPO_REMOTE_SSH_CIDR=                     # optional: pin the SG's SSH-ingress CIDR (e.g. 203.0.113.7/32, or
                                           # 0.0.0.0/0 as a deliberate opt-in); overrides current-IP detection outright
```

Only `REPO_REMOTE_PROVIDER` (or a provider argument) and that provider's
credentials are required — from **either** layer. Everything else falls back to
built-in defaults: GCP `e2-standard-4` / AWS `m5.xlarge`, 50 GB disk, latest
Ubuntu LTS, no GPU, 120-minute idle shutdown.

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
wants a repo-specific account/region.

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

Load the shared file first, then the repo `.env` on top, into the environment
for the provisioning calls only — scoped to this command, never persisted to
the VM. Repo values override shared ones because the repo file is sourced last:

```bash
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}/repo/remote.env"
set -a
[ -f "$CONFIG_HOME" ] && . "$CONFIG_HOME"                      # shared cloud creds + defaults
[ -f "$(git rev-parse --show-toplevel)/.env" ] && . "$(git rev-parse --show-toplevel)/.env"  # per-repo (overrides)
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
2. **Resolve the CIDR.** `REPO_REMOTE_SSH_CIDR`, if set, wins outright
   (including an explicit `0.0.0.0/0` opt-in). Otherwise, detect the current
   IP via an HTTPS echo service (`checkip.amazonaws.com`) and use it as a
   `/32` — but treat that detection as **unverified**: there is no reliable
   way for this tooling to confirm the detected address is the one SSH egress
   will actually use. Behind an HTTPS proxy it commonly isn't (an increasingly
   common failure mode on agent hosts) — the echo service returns the proxy's
   address, producing a correct-looking `/32` rule that can never match. If
   detection fails outright (or you know it will be unreliable), set
   `REPO_REMOTE_SSH_CIDR` yourself.
3. **Fall back loudly, never silently.** If IP detection fails and
   `REPO_REMOTE_SSH_CIDR` is unset, the rule falls back to `0.0.0.0/0` (SSH
   remains key-only auth, so this is a scan-noise tradeoff, not an auth
   bypass) with an explicit printed notice — never a `/32` that looks correct
   but can never match.
4. **Authorize idempotently.** `authorize-security-group-ingress` for tcp/22
   from the resolved CIDR; a duplicate rule on a reused group counts as
   success.
5. **Verify before spending money.** `describe-security-groups` on the
   resolved group must show a tcp/22 rule, or the run fails loudly (exit `4`)
   before `run-instances` is ever called — this is what would have caught the
   originally reported incident in-run instead of as a bare SSH timeout.
6. **End-of-run reachability check.** After the SSH alias is written, `up`
   probes it — `ssh -o ConnectTimeout=10 -o BatchMode=yes -o
   StrictHostKeyChecking=accept-new <alias> true` — and fails loudly (exit
   `4`) if it doesn't succeed, so an unreachable instance is caught in-run
   rather than discovered on the next manual SSH attempt.

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
the second finding of the 2AMLogic/2am#52 incident, where `repo-remote=anvil`
tooling kept rediscovering the host that had become `loom-worker-1`; `down
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

**After a successful create, write the new ID back to the repo's `.env`** (the
git root, never the shared file — the instance handle is per-repo) so the next
run reuses it automatically:

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
On a freshly provisioned box, `/repo:sudo` is the companion step that unblocks
root-level remediation over SSH — a non-interactive SSH session can't answer a
`sudo` password prompt, so an agent stalls on it until a validated
passwordless-sudo drop-in is installed.

### 8. Report

End with a compact status block: instance name/ID, zone, machine type (and GPU),
hourly cost estimate, idle-shutdown window, the SSH alias, whether the ID was
written back to `.env`, and the teardown command (`/repo:remote --down`).

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
