#!/usr/bin/env bash
# repo-remote.sh — headless, scriptable entry point for /repo:remote provisioning.
#
# This is the non-interactive implementation of the provisioning contract
# documented (as prose) in commands/repo/remote.md. The interactive skill wraps
# this script with its wizard / cost-confirmation UX and, once the human has
# confirmed, calls `repo-remote up --yes`; a caller such as loom's
# `fleet add-worker` invokes the same `up --yes --json` path directly. There is
# ONE implementation of the contract so the two paths cannot drift (repo#52).
#
# The seam this produces, consumed by loom fleet orchestration: "a reachable
# Ubuntu box, this repo's SSH alias written, instance id recorded" — emitted as
# machine-readable JSON (instance id, public IP, SSH alias, estimated hourly
# cost) so a caller can implement loom's "plan shown before money is spent" rule.
#
# ─────────────────────────────────────────────────────────────────────────────
# STABLE INTERFACE (downstream tooling gates on this via the package version)
# ─────────────────────────────────────────────────────────────────────────────
#
# Subcommands (both the `up`/`down`/`status` verbs and the remote.md-style
# `--status`/`--down` flags are accepted, for parity with the prose command):
#
#   repo-remote up [--yes] [--json] [aws|gcp]     Provision (or reuse) an instance.
#       Without --yes: a DRY-RUN plan (resolved spec + estimated cost) is emitted
#       and NOTHING is created — this is the "plan shown before money spent" path.
#       With --yes: the plan is executed. --yes removes the *prompt*, never the
#       *consent requirement*: a cost-relevant field missing from config is a
#       loud, non-zero-exit failure, never a silent default (repo#52 cost gate).
#
#   repo-remote status|--status [--json] [aws|gcp]   List instances this command
#       created (tagged repo-remote=<name>) with state; no mutation.
#
#   repo-remote down|--down [--yes] [--delete] [--json] [aws|gcp]   Teardown.
#       Without --yes: a DRY-RUN listing of exactly what would stop/terminate.
#       With --yes: stop them; add --delete to terminate (disk goes with it).
#
# Config: two layers, shared first then repo (repo overrides), matching the
# skill exactly:
#   1. ${XDG_CONFIG_HOME:-$HOME/.config}/repo/remote.env   (shared cloud creds)
#   2. <git-root>/.env                                     (per-repo machine)
#
# Cost gate (repo#52 — the highest-cost-of-being-wrong element): `up` (with or
# without --yes) REQUIRES the provider, that provider's credentials, and
# REPO_REMOTE_INSTANCE_TYPE to be present in config. Instance type is the
# cost-relevant field and is NEVER defaulted here — that removes interactivity,
# not consent. Non-cost-relevant fields (disk, idle window, image) do fall back
# to built-in defaults, matching the prose command.
#
# Exit codes:
#   0  success (including a dry-run plan)
#   2  missing / invalid required config (the cost gate; loud failure)
#   3  provider authentication failed
#   4  cloud operation failed
#   64 usage error
#
# Testability hooks (honored so the suite can exercise the full contract against
# mocked cloud CLIs without touching real infrastructure or a real ~/.ssh):
#   XDG_CONFIG_HOME            locates the shared remote.env (already standard)
#   REPO_REMOTE_SSH_CONFIG     SSH config file to write the alias into
#                              (default: ~/.ssh/config)
#   PATH                       mock `aws`/`gcloud` are picked up from PATH
#
set -uo pipefail

# ── output helpers ──────────────────────────────────────────────────────────
_is_tty() { [[ -t 2 ]]; }
log()  { printf '%s\n' "repo-remote: $*" >&2; }
die()  { local code="$1"; shift; printf '%s\n' "repo-remote: ERROR: $*" >&2; exit "$code"; }

JSON_OUT=false   # --json
YES=false        # --yes
DELETE=false     # --down --delete
ACTION=""        # up | down | status
PROVIDER_ARG=""  # aws | gcp (positional override)

# ── JSON emission (no jq dependency for output; values are controlled) ──────
# json_escape <string> -> a JSON string body (without surrounding quotes)
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\t'/\\t}"
  s="${s//$'\r'/}"
  printf '%s' "$s"
}

# ── config loading ──────────────────────────────────────────────────────────
# Load the two env layers into the environment, shared first then repo (repo
# wins because it is sourced last). Mirrors commands/repo/remote.md step 2.
SHARED_ENV=""
REPO_ENV=""
GIT_ROOT=""

resolve_paths() {
  SHARED_ENV="${XDG_CONFIG_HOME:-$HOME/.config}/repo/remote.env"
  GIT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  [[ -n "$GIT_ROOT" ]] && REPO_ENV="$GIT_ROOT/.env"
}

load_config() {
  set -a
  # shellcheck disable=SC1090
  [[ -f "$SHARED_ENV" ]] && . "$SHARED_ENV"
  # shellcheck disable=SC1090
  [[ -n "$REPO_ENV" && -f "$REPO_ENV" ]] && . "$REPO_ENV"
  set +a
}

# ── effective settings ──────────────────────────────────────────────────────
PROVIDER=""
NAME=""
INSTANCE_TYPE=""
INSTANCE_ID=""
DISK_GB=""
IMAGE=""
GPU_ACCEL=""       # GCP accelerator string, e.g. nvidia-l4:1
IDLE_MIN=""
IS_GPU=false
REGION=""
COST_HOURLY=""
COST_APPROX=false

# GPU-family detection: infer a GPU host from the instance family so the caller
# needn't set a separate flag (remote.md "GPU hosts").
is_gpu_family() {  # <provider> <instance-type>
  local p="$1" t="$2"
  [[ -n "${GPU_ACCEL:-}" ]] && return 0
  case "$p" in
    aws) [[ "$t" =~ ^(g3|g4|g4dn|g5|g5g|g6|g6e|p2|p3|p4|p4d|p5)\. ]] && return 0 ;;
    gcp) [[ "$t" =~ ^(g2|a2|a3)- ]] && return 0 ;;
  esac
  return 1
}

# Approximate on-demand USD/hour by instance type. Unknown types fall back to a
# GPU/non-GPU heuristic flagged approximate — the JSON always carries a number
# so a caller can implement a budget check, and never silently claims precision.
estimate_cost() {  # sets COST_HOURLY, COST_APPROX
  local t="$1"
  COST_APPROX=false
  case "$t" in
    # AWS general purpose / compute
    t3.medium)   COST_HOURLY=0.0416 ;;
    t3.large)    COST_HOURLY=0.0832 ;;
    t3.xlarge)   COST_HOURLY=0.1664 ;;
    t3.2xlarge)  COST_HOURLY=0.3328 ;;
    m5.large)    COST_HOURLY=0.096  ;;
    m5.xlarge)   COST_HOURLY=0.192  ;;
    m5.2xlarge)  COST_HOURLY=0.384  ;;
    m5.4xlarge)  COST_HOURLY=0.768  ;;
    m6i.xlarge)  COST_HOURLY=0.192  ;;
    m6i.2xlarge) COST_HOURLY=0.384  ;;
    c5.xlarge)   COST_HOURLY=0.17   ;;
    c5.2xlarge)  COST_HOURLY=0.34   ;;
    c5.4xlarge)  COST_HOURLY=0.68   ;;
    # AWS GPU
    g4dn.xlarge) COST_HOURLY=0.526  ;;
    g5.xlarge)   COST_HOURLY=1.006  ;;
    g5.2xlarge)  COST_HOURLY=1.212  ;;
    g6.xlarge)   COST_HOURLY=0.8048 ;;
    g6e.xlarge)  COST_HOURLY=1.861  ;;
    g6e.2xlarge) COST_HOURLY=2.242  ;;
    p4d.24xlarge) COST_HOURLY=32.77 ;;
    # GCP predefined
    e2-standard-2)  COST_HOURLY=0.067 ;;
    e2-standard-4)  COST_HOURLY=0.134 ;;
    e2-standard-8)  COST_HOURLY=0.268 ;;
    n1-standard-4)  COST_HOURLY=0.19  ;;
    n1-standard-8)  COST_HOURLY=0.38  ;;
    g2-standard-4)  COST_HOURLY=0.71  ;;
    g2-standard-8)  COST_HOURLY=0.85  ;;
    a2-highgpu-1g)  COST_HOURLY=3.67  ;;
    *)
      COST_APPROX=true
      if [[ "$IS_GPU" == true ]]; then COST_HOURLY=1.50; else COST_HOURLY=0.20; fi
      ;;
  esac
  # A GCP accelerator adds to the machine price; fold in a rough per-card cost so
  # the estimate for GPU-on-GCP is not silently the bare-machine price.
  if [[ -n "${GPU_ACCEL:-}" && "$PROVIDER" == gcp ]]; then
    COST_APPROX=true
    COST_HOURLY="$(awk -v c="$COST_HOURLY" 'BEGIN{printf "%.4f", c + 0.70}')"
  fi
}

resolve_settings() {
  PROVIDER="${PROVIDER_ARG:-${REPO_REMOTE_PROVIDER:-}}"
  # lower-case the provider
  PROVIDER="$(printf '%s' "$PROVIDER" | tr '[:upper:]' '[:lower:]')"

  NAME="$(basename "${GIT_ROOT:-$PWD}")"

  INSTANCE_TYPE="${REPO_REMOTE_INSTANCE_TYPE:-}"
  INSTANCE_ID="${REPO_REMOTE_INSTANCE_ID:-}"
  GPU_ACCEL="${REPO_REMOTE_GPU:-}"
  IMAGE="${REPO_REMOTE_IMAGE:-}"

  # Non-cost-relevant fields DO fall back to defaults (matches the prose command).
  DISK_GB="${REPO_REMOTE_DISK_GB:-50}"
  IDLE_MIN="${REPO_REMOTE_IDLE_SHUTDOWN_MIN:-120}"
  # Idle-exit marker contract (see commands/repo/remote.md). A daemon-managed
  # host (e.g. one running loom-daemon) may write this file on clean idle-exit;
  # the guard treats its mtime as an authoritative "idle since" timestamp. The
  # path is always embedded in the guard so the contract is self-contained and
  # works standalone — it stays inert until the file actually exists on-host.
  IDLE_MARKER="${REPO_REMOTE_IDLE_MARKER:-/var/run/repo-remote-daemon-idle.marker}"

  case "$PROVIDER" in
    aws) REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}" ;;
    gcp) REGION="${GCP_ZONE:-}" ;;
  esac

  if [[ -n "$INSTANCE_TYPE" ]] && is_gpu_family "$PROVIDER" "$INSTANCE_TYPE"; then
    IS_GPU=true
  fi
  [[ -n "$INSTANCE_TYPE" ]] && estimate_cost "$INSTANCE_TYPE"
}

# ── the cost gate ───────────────────────────────────────────────────────────
# Enforce that every field whose absence could cause an *unexpected* bill is
# present in config. This is what makes `--yes` safe: it removes the interactive
# prompt but not the requirement that the human pre-supplied the budget-relevant
# choices. Missing config fails loudly (exit 2), never a silent default.
require_cost_config() {
  local missing=()

  [[ -n "$PROVIDER" ]] || missing+=("REPO_REMOTE_PROVIDER (or an aws|gcp argument)")
  case "$PROVIDER" in
    aws)
      [[ -n "${AWS_ACCESS_KEY_ID:-}" ]]     || missing+=("AWS_ACCESS_KEY_ID")
      [[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]] || missing+=("AWS_SECRET_ACCESS_KEY")
      [[ -n "$REGION" ]]                    || missing+=("AWS_REGION")
      ;;
    gcp)
      [[ -n "${GCP_PROJECT:-}" ]]                     || missing+=("GCP_PROJECT")
      [[ -n "$REGION" ]]                              || missing+=("GCP_ZONE")
      [[ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]  || missing+=("GOOGLE_APPLICATION_CREDENTIALS")
      ;;
    "") : ;;  # provider itself already reported above
    *)  die 2 "unknown provider '$PROVIDER' (expected aws or gcp)" ;;
  esac

  # THE cost-relevant field. Never defaulted — an unpinned instance type is
  # exactly how an unexpected bill happens.
  [[ -n "$INSTANCE_TYPE" ]] || missing+=("REPO_REMOTE_INSTANCE_TYPE (required — no default, so a run never silently picks a billable size)")

  if [[ ${#missing[@]} -gt 0 ]]; then
    local m
    log "cannot proceed — required config is missing (no silent defaults for cost-relevant fields):"
    for m in "${missing[@]}"; do log "  - $m"; done
    log "set them in ${SHARED_ENV} (shared) or ${REPO_ENV:-<git-root>/.env} (per-repo), or run /repo:remote --configure."
    exit 2
  fi
}

# ── the idle-shutdown guard (cloud-init user-data) ──────────────────────────
# A forgotten VM — GPU ones especially — must turn itself off. Emitted as a
# cloud-init script that installs a cron watchdog running `shutdown -h` after
# IDLE_MIN minutes with no active SSH session and low CPU.
#
# "Activity" is defined by exactly two local signals: an open SSH session (`who`)
# OR CPU load average > 0.2. There is NO process-name veto — a running daemon
# (loom-daemon or otherwise) does NOT, by itself, keep this host alive. If a
# future daemon-presence veto is ever wanted it must be added deliberately here
# and documented; it is not implied by the current logic.
#
# Idle-exit marker contract (published by this repo so a daemon side can conform
# without this repo depending on it): when $IDLE_MARKER exists on-host, the guard
# treats its mtime as an authoritative "idle since" timestamp and shuts down
# IDLE_MIN minutes after that mtime — REPLACING (not supplementing) its own
# $STAMP-based countdown start for that pass. A daemon that idle-exits cleanly can
# `touch` this file to hand the guard a precise idle-start instead of waiting for
# the guard's own load-average sampling to first read idle. The guard works
# standalone: with no marker file present it falls back to the unchanged
# who/load/$STAMP behavior. The marker path is always embedded (default below,
# overridable via REPO_REMOTE_IDLE_MARKER) so the branch is inert-but-ready.
idle_guard_userdata() {
  cat <<EOF
#!/bin/bash
# repo-remote idle-shutdown guard (idle window: ${IDLE_MIN} min)
cat >/usr/local/bin/repo-remote-idle-check <<'GUARD'
#!/bin/bash
IDLE_MIN=${IDLE_MIN}
STAMP=/var/run/repo-remote-idle.stamp
# Idle-exit marker: mtime = authoritative "idle since" (e.g. written by
# loom-daemon on clean idle-exit). Overridable via REPO_REMOTE_IDLE_MARKER.
MARKER=${IDLE_MARKER}
# An active SSH session or non-trivial CPU load is real activity: reset the
# idle timer and veto shutdown regardless of any marker (never power off a box
# someone is actively using). No process-name check — see the note in the
# generating script.
if who | grep -q . || [ "\$(awk '{print (\$1 > 0.2)}' /proc/loadavg)" = "1" ]; then
  date +%s > "\$STAMP"; exit 0
fi
# Marker present ⇒ its mtime REPLACES the local \$STAMP countdown start. The
# on-host image is Ubuntu (GNU coreutils), so \`stat -c %Y\` is authoritative;
# the \`|| echo 0\` guards a vanished/unreadable file. A future mtime (clock
# skew) yields a negative age, which is never >= IDLE_MIN, so it can't trigger a
# spurious shutdown.
if [ -f "\$MARKER" ]; then
  MARKER_AGE_MIN=\$(( ( \$(date +%s) - \$(stat -c %Y "\$MARKER" 2>/dev/null || echo 0) ) / 60 ))
  if [ "\$MARKER_AGE_MIN" -ge "\$IDLE_MIN" ]; then
    /sbin/shutdown -h now "repo-remote: daemon idle-exit marker aged \${MARKER_AGE_MIN}m"
  fi
  exit 0
fi
# No marker ⇒ unchanged local stamp-based countdown.
[ -f "\$STAMP" ] || { date +%s > "\$STAMP"; exit 0; }
NOW=\$(date +%s); LAST=\$(cat "\$STAMP")
if [ \$(( (NOW - LAST) / 60 )) -ge "\$IDLE_MIN" ]; then
  /sbin/shutdown -h now "repo-remote: idle for \${IDLE_MIN}m"
fi
GUARD
chmod +x /usr/local/bin/repo-remote-idle-check
echo "* * * * * root /usr/local/bin/repo-remote-idle-check" >/etc/cron.d/repo-remote-idle
EOF
}

# ── AWS provider ────────────────────────────────────────────────────────────
aws_authenticate() {
  aws sts get-caller-identity >/dev/null 2>&1 \
    || die 3 "AWS authentication failed with the resolved credentials (aws sts get-caller-identity). Not falling back to ambient/other credentials."
}

# Resolve the AMI into RESOLVED_AMI: an explicit override wins; a GPU host
# defaults to the AWS Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu
# 22.04); otherwise the latest Ubuntu 22.04 LTS. (remote.md "GPU hosts".)
# Sets a global (rather than echoing) so a `die` here propagates to the whole
# process instead of being swallowed by a `$(...)` subshell.
RESOLVED_AMI=""
aws_resolve_image() {
  if [[ -n "$IMAGE" ]]; then RESOLVED_AMI="$IMAGE"; return 0; fi
  local q name
  if [[ "$IS_GPU" == true ]]; then
    name='Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*'
    q="$(aws ec2 describe-images --owners amazon \
      --filters "Name=name,Values=$name" \
      --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null)"
  else
    name='ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*'
    q="$(aws ec2 describe-images --owners 099720109477 \
      --filters "Name=name,Values=$name" \
      --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null)"
  fi
  [[ -n "$q" && "$q" != "None" ]] || die 4 "could not resolve an AMI for ${INSTANCE_TYPE} (GPU=${IS_GPU}). Set REPO_REMOTE_IMAGE to override."
  RESOLVED_AMI="$q"
}

# Describe a single instance's state; echoes state name or "missing".
aws_instance_state() {  # <instance-id>
  local out
  out="$(aws ec2 describe-instances --instance-ids "$1" \
        --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null)" || { echo missing; return; }
  [[ -n "$out" && "$out" != "None" ]] && echo "$out" || echo missing
}

# Find a running|stopped instance previously created for this repo (by tag).
aws_find_tagged() {  # echoes "<id> <state>" or empty
  aws ec2 describe-instances \
    --filters "Name=tag:repo-remote,Values=${NAME}" \
              "Name=instance-state-name,Values=running,stopped,stopping,pending" \
    --query 'Reservations[].Instances[].[InstanceId,State.Name]' --output text 2>/dev/null \
    | grep -v '^None' | head -n1
}

aws_public_ip() {  # <instance-id>
  aws ec2 describe-instances --instance-ids "$1" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null
}

# Create a fresh instance into CREATED_ID (global, so a `die` propagates rather
# than being swallowed by a command-substitution subshell). run-instances is
# invoked EXACTLY ONCE — capturing stdout and stderr in one call — because a
# retry-to-read-the-error would risk launching a second billable instance.
CREATED_ID=""
aws_create() {
  local ami sg key udfile errfile iid rc err
  aws_resolve_image; ami="$RESOLVED_AMI"
  key="${REPO_REMOTE_SSH_KEY_NAME:-}"
  sg="${REPO_REMOTE_SECURITY_GROUP:-}"

  udfile="$(mktemp)"; idle_guard_userdata >"$udfile"
  errfile="$(mktemp)"

  local -a args=(ec2 run-instances
    --image-id "$ami"
    --instance-type "$INSTANCE_TYPE"
    --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=${DISK_GB}}"
    --tag-specifications "ResourceType=instance,Tags=[{Key=repo-remote,Value=${NAME}}]"
    --user-data "file://${udfile}"
    --query 'Instances[0].InstanceId' --output text)
  [[ -n "$key" ]] && args+=(--key-name "$key")
  [[ -n "$sg" ]]  && args+=(--security-group-ids "$sg")

  iid="$(aws "${args[@]}" 2>"$errfile")"; rc=$?
  err="$(cat "$errfile" 2>/dev/null)"
  rm -f "$udfile" "$errfile"

  if [[ $rc -ne 0 || -z "$iid" || "$iid" == "None" ]]; then
    # Surface the quota-exceeded case with the exact remediation (remote.md).
    if printf '%s' "$err" | grep -q 'VcpuLimitExceeded'; then
      die 4 "AWS GPU vCPU quota is 0 by default (VcpuLimitExceeded). Request a limit >= this type's vCPUs at Service Quotas -> EC2 -> quota code L-DB2E81BA, then retry."
    fi
    die 4 "aws ec2 run-instances failed: ${err:-unknown error}"
  fi
  CREATED_ID="$iid"
}

aws_up() {
  aws_authenticate
  local iid="" state="" reused=false

  if [[ -n "$INSTANCE_ID" ]]; then
    state="$(aws_instance_state "$INSTANCE_ID")"
    case "$state" in
      running)          iid="$INSTANCE_ID"; reused=true ;;
      stopped|stopping) aws ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null 2>&1 \
                          || die 4 "failed to start stopped instance $INSTANCE_ID"
                        iid="$INSTANCE_ID"; reused=true ;;
      missing)          log "pinned REPO_REMOTE_INSTANCE_ID=$INSTANCE_ID no longer exists; creating a fresh instance."
                        INSTANCE_ID="" ;;
      *)                iid="$INSTANCE_ID"; reused=true ;;
    esac
  fi

  if [[ -z "$iid" ]]; then
    local found; found="$(aws_find_tagged)"
    if [[ -n "$found" ]]; then
      iid="$(printf '%s' "$found" | awk '{print $1}')"
      state="$(printf '%s' "$found" | awk '{print $2}')"
      reused=true
      if [[ "$state" == stopped || "$state" == stopping ]]; then
        aws ec2 start-instances --instance-ids "$iid" >/dev/null 2>&1 \
          || die 4 "failed to start reused instance $iid"
      fi
    fi
  fi

  if [[ -z "$iid" ]]; then
    aws_create           # sets CREATED_ID or dies (main-shell context)
    iid="$CREATED_ID"
    reused=false
  fi

  aws ec2 wait instance-running --instance-ids "$iid" >/dev/null 2>&1 || true
  local ip; ip="$(aws_public_ip "$iid")"
  [[ "$ip" == "None" ]] && ip=""

  writeback_instance_id "$iid"
  local alias; alias="$(write_ssh_alias "$ip")"

  emit_up_result "$iid" "$ip" "$alias" "$reused"
}

aws_status() {
  aws_authenticate
  local rows
  rows="$(aws ec2 describe-instances \
    --filters "Name=tag:repo-remote,Values=${NAME}" \
    --query 'Reservations[].Instances[].[InstanceId,State.Name,InstanceType,PublicIpAddress,LaunchTime]' \
    --output text 2>/dev/null | grep -v '^None' || true)"
  emit_status_result "$rows"
}

aws_down() {
  aws_authenticate
  local ids
  if [[ -n "$INSTANCE_ID" ]]; then
    ids="$INSTANCE_ID"
  else
    ids="$(aws ec2 describe-instances \
      --filters "Name=tag:repo-remote,Values=${NAME}" \
                "Name=instance-state-name,Values=running,stopped,stopping,pending" \
      --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null | grep -v '^None' || true)"
  fi
  ids="$(printf '%s' "$ids" | tr '\t' ' ' | xargs 2>/dev/null || true)"

  if [[ -z "$ids" ]]; then
    emit_down_result "" "noop"
    return 0
  fi

  if [[ "$YES" != true ]]; then
    emit_down_result "$ids" "dry-run"
    return 0
  fi

  if [[ "$DELETE" == true ]]; then
    # shellcheck disable=SC2086
    aws ec2 terminate-instances --instance-ids $ids >/dev/null 2>&1 \
      || die 4 "aws ec2 terminate-instances failed for: $ids"
    emit_down_result "$ids" "terminated"
  else
    # shellcheck disable=SC2086
    aws ec2 stop-instances --instance-ids $ids >/dev/null 2>&1 \
      || die 4 "aws ec2 stop-instances failed for: $ids"
    emit_down_result "$ids" "stopped"
  fi
}

# ── GCP provider ────────────────────────────────────────────────────────────
gcp_authenticate() {
  gcloud auth activate-service-account --key-file="${GOOGLE_APPLICATION_CREDENTIALS}" >/dev/null 2>&1 \
    || die 3 "GCP authentication failed (gcloud auth activate-service-account)."
  gcloud config set project "${GCP_PROJECT}" >/dev/null 2>&1 || true
}

gcp_up() {
  gcp_authenticate
  local vm="repo-remote-${NAME}" ip="" reused=false state
  state="$(gcloud compute instances describe "$vm" --zone "$REGION" \
    --format='value(status)' 2>/dev/null || true)"
  if [[ -n "$state" ]]; then
    reused=true
    if [[ "$state" != "RUNNING" ]]; then
      gcloud compute instances start "$vm" --zone "$REGION" >/dev/null 2>&1 \
        || die 4 "failed to start existing instance $vm"
    fi
  else
    local -a args=(compute instances create "$vm"
      --zone "$REGION"
      --machine-type "$INSTANCE_TYPE"
      --boot-disk-size "${DISK_GB}GB"
      --labels "repo-remote=${NAME}"
      --image-family "${IMAGE:-ubuntu-2204-lts}" --image-project "${REPO_REMOTE_IMAGE_PROJECT:-ubuntu-os-cloud}")
    [[ -n "$GPU_ACCEL" ]] && args+=(--accelerator "type=${GPU_ACCEL%%:*},count=${GPU_ACCEL##*:}" --maintenance-policy TERMINATE)
    local ud; ud="$(mktemp)"; idle_guard_userdata >"$ud"
    args+=(--metadata-from-file "startup-script=${ud}")
    gcloud "${args[@]}" >/dev/null 2>&1 || { rm -f "$ud"; die 4 "gcloud compute instances create failed for $vm"; }
    rm -f "$ud"
  fi
  ip="$(gcloud compute instances describe "$vm" --zone "$REGION" \
    --format='value(networkInterfaces[0].accessConfigs[0].natIP)' 2>/dev/null || true)"

  writeback_instance_id "$vm"
  local alias; alias="$(write_ssh_alias "$ip")"
  emit_up_result "$vm" "$ip" "$alias" "$reused"
}

gcp_status() {
  gcp_authenticate
  local rows
  rows="$(gcloud compute instances list \
    --filter="labels.repo-remote=${NAME}" \
    --format='value(name,status,machineType.basename(),networkInterfaces[0].accessConfigs[0].natIP,creationTimestamp)' 2>/dev/null || true)"
  emit_status_result "$rows"
}

gcp_down() {
  gcp_authenticate
  local vm="repo-remote-${NAME}"
  [[ -n "$INSTANCE_ID" ]] && vm="$INSTANCE_ID"
  local exists
  exists="$(gcloud compute instances describe "$vm" --zone "$REGION" --format='value(name)' 2>/dev/null || true)"
  if [[ -z "$exists" ]]; then emit_down_result "" "noop"; return 0; fi
  if [[ "$YES" != true ]]; then emit_down_result "$vm" "dry-run"; return 0; fi
  if [[ "$DELETE" == true ]]; then
    gcloud compute instances delete "$vm" --zone "$REGION" --quiet >/dev/null 2>&1 \
      || die 4 "gcloud compute instances delete failed for $vm"
    emit_down_result "$vm" "terminated"
  else
    gcloud compute instances stop "$vm" --zone "$REGION" --quiet >/dev/null 2>&1 \
      || die 4 "gcloud compute instances stop failed for $vm"
    emit_down_result "$vm" "stopped"
  fi
}

# ── write-back + SSH alias ──────────────────────────────────────────────────
# Write the new instance id back to the repo's .env (git root, never the shared
# file — the handle is per-repo), updating in place or appending. remote.md §4.
writeback_instance_id() {  # <instance-id>
  local id="$1"
  [[ -n "$REPO_ENV" ]] || return 0
  if [[ -f "$REPO_ENV" ]] && grep -q '^REPO_REMOTE_INSTANCE_ID=' "$REPO_ENV"; then
    local tmp; tmp="$(mktemp)"
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == REPO_REMOTE_INSTANCE_ID=* ]]; then
        printf 'REPO_REMOTE_INSTANCE_ID=%s\n' "$id"
      else
        printf '%s\n' "$line"
      fi
    done <"$REPO_ENV" >"$tmp"
    mv "$tmp" "$REPO_ENV"
  else
    printf 'REPO_REMOTE_INSTANCE_ID=%s\n' "$id" >>"$REPO_ENV"
  fi
}

# Write/refresh the one-word SSH alias so the connection is `ssh repo-remote-<name>`.
# Honors REPO_REMOTE_SSH_CONFIG (default ~/.ssh/config) so tests never touch a
# real config. Echoes the alias name.
write_ssh_alias() {  # <ip>
  local ip="$1" alias="repo-remote-${NAME}"
  local cfg="${REPO_REMOTE_SSH_CONFIG:-$HOME/.ssh/config}"
  local key="${REPO_REMOTE_SSH_KEY:-~/.ssh/id_ed25519}"
  local user="${REPO_REMOTE_SSH_USER:-ubuntu}"
  [[ -z "$ip" ]] && { printf '%s' "$alias"; return 0; }

  mkdir -p "$(dirname "$cfg")" 2>/dev/null || true
  local tmp; tmp="$(mktemp)"
  # Strip any prior block for this alias, then append a fresh one.
  if [[ -f "$cfg" ]]; then
    awk -v a="Host $alias" '
      $0 == a {skip=1; next}
      skip && /^Host / {skip=0}
      skip {next}
      {print}
    ' "$cfg" >"$tmp"
  fi
  {
    [[ -s "$tmp" ]] && printf '\n'
    printf 'Host %s\n' "$alias"
    printf '    HostName %s\n' "$ip"
    printf '    User %s\n' "$user"
    printf '    IdentityFile %s\n' "$key"
  } >>"$tmp"
  mv "$tmp" "$cfg"
  chmod 600 "$cfg" 2>/dev/null || true
  printf '%s' "$alias"
}

# ── result emitters ─────────────────────────────────────────────────────────
emit_plan() {  # dry-run plan (no cloud mutation)
  if [[ "$JSON_OUT" == true ]]; then
    printf '{'
    printf '"action":"plan",'
    printf '"dry_run":true,'
    printf '"provider":"%s",' "$(json_escape "$PROVIDER")"
    printf '"name":"%s",' "$(json_escape "$NAME")"
    printf '"instance_type":"%s",' "$(json_escape "$INSTANCE_TYPE")"
    printf '"region":"%s",' "$(json_escape "$REGION")"
    printf '"disk_gb":%s,' "$DISK_GB"
    printf '"gpu":%s,' "$IS_GPU"
    printf '"idle_shutdown_min":%s,' "$IDLE_MIN"
    printf '"ssh_alias":"repo-remote-%s",' "$(json_escape "$NAME")"
    printf '"estimated_hourly_cost_usd":%s,' "$COST_HOURLY"
    printf '"estimated_cost_approximate":%s' "$COST_APPROX"
    printf '}\n'
  else
    log "PLAN (dry run — nothing created; pass --yes to provision):"
    log "  provider:            $PROVIDER"
    log "  instance type:       $INSTANCE_TYPE${IS_GPU:+ (GPU)}"
    log "  region/zone:         $REGION"
    log "  disk:                ${DISK_GB} GB"
    log "  idle shutdown:       ${IDLE_MIN} min"
    log "  est. hourly cost:    \$${COST_HOURLY}/hr$([[ "$COST_APPROX" == true ]] && echo ' (approximate)')"
    log "  ssh alias:           repo-remote-${NAME}"
  fi
}

emit_up_result() {  # <id> <ip> <alias> <reused>
  local id="$1" ip="$2" alias="$3" reused="$4"
  if [[ "$JSON_OUT" == true ]]; then
    printf '{'
    printf '"action":"up",'
    printf '"provider":"%s",' "$(json_escape "$PROVIDER")"
    printf '"name":"%s",' "$(json_escape "$NAME")"
    printf '"instance_id":"%s",' "$(json_escape "$id")"
    printf '"public_ip":"%s",' "$(json_escape "$ip")"
    printf '"ssh_alias":"%s",' "$(json_escape "$alias")"
    printf '"instance_type":"%s",' "$(json_escape "$INSTANCE_TYPE")"
    printf '"region":"%s",' "$(json_escape "$REGION")"
    printf '"gpu":%s,' "$IS_GPU"
    printf '"idle_shutdown_min":%s,' "$IDLE_MIN"
    printf '"reused":%s,' "$reused"
    printf '"estimated_hourly_cost_usd":%s,' "$COST_HOURLY"
    printf '"estimated_cost_approximate":%s' "$COST_APPROX"
    printf '}\n'
  else
    log "$([[ "$reused" == true ]] && echo reused || echo created) instance $id (${INSTANCE_TYPE}) @ ${ip:-<no public ip>}"
    log "  ssh alias:        $alias"
    log "  est. hourly cost: \$${COST_HOURLY}/hr$([[ "$COST_APPROX" == true ]] && echo ' (approximate)')"
    log "  teardown:         repo-remote down --yes   (or /repo:remote --down)"
  fi
}

emit_status_result() {  # <rows: id state type ip launch, tab/space separated per line>
  local rows="$1"
  if [[ "$JSON_OUT" == true ]]; then
    printf '{"action":"status","provider":"%s","name":"%s","instances":[' \
      "$(json_escape "$PROVIDER")" "$(json_escape "$NAME")"
    local first=true line id state type ip launch
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      id="$(printf '%s' "$line" | awk '{print $1}')"
      state="$(printf '%s' "$line" | awk '{print $2}')"
      type="$(printf '%s' "$line" | awk '{print $3}')"
      ip="$(printf '%s' "$line" | awk '{print $4}')"
      launch="$(printf '%s' "$line" | awk '{print $5}')"
      [[ "$ip" == "None" ]] && ip=""
      [[ "$first" == true ]] && first=false || printf ','
      printf '{"instance_id":"%s","state":"%s","instance_type":"%s","public_ip":"%s","launch_time":"%s"}' \
        "$(json_escape "$id")" "$(json_escape "$state")" "$(json_escape "$type")" "$(json_escape "$ip")" "$(json_escape "$launch")"
    done <<<"$rows"
    printf ']}\n'
  else
    if [[ -z "$rows" ]]; then
      log "no instances tagged repo-remote=${NAME}"
    else
      log "instances tagged repo-remote=${NAME}:"
      printf '%s\n' "$rows" >&2
    fi
  fi
}

emit_down_result() {  # <ids> <disposition: noop|dry-run|stopped|terminated>
  local ids="$1" disp="$2"
  if [[ "$JSON_OUT" == true ]]; then
    printf '{"action":"down","provider":"%s","name":"%s","disposition":"%s","instances":[' \
      "$(json_escape "$PROVIDER")" "$(json_escape "$NAME")" "$(json_escape "$disp")"
    local first=true id
    for id in $ids; do
      [[ "$first" == true ]] && first=false || printf ','
      printf '"%s"' "$(json_escape "$id")"
    done
    printf ']}\n'
  else
    case "$disp" in
      noop)     log "no instances tagged repo-remote=${NAME} to stop" ;;
      dry-run)  log "DRY RUN — would stop$([[ "$DELETE" == true ]] && echo /terminate): $ids (pass --yes to act)" ;;
      stopped)  log "stopped: $ids" ;;
      terminated) log "terminated (disks removed): $ids" ;;
    esac
  fi
}

# ── argument parsing ────────────────────────────────────────────────────────
usage() {
  sed -n '4,60p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      up|status|down) [[ -z "$ACTION" ]] && ACTION="$1" || die 64 "multiple actions given ($ACTION, $1)" ;;
      --status)       ACTION="status" ;;
      --down)         ACTION="down" ;;
      --yes|-y)       YES=true ;;
      --json)         JSON_OUT=true ;;
      --delete)       DELETE=true ;;
      aws|gcp)        PROVIDER_ARG="$1" ;;
      -h|--help)      usage; exit 0 ;;
      *)              die 64 "unknown argument: $1 (see --help)" ;;
    esac
    shift
  done
  [[ -n "$ACTION" ]] || die 64 "no action given (expected: up | status | down; see --help)"
}

# ── main ────────────────────────────────────────────────────────────────────
main() {
  parse_args "$@"
  resolve_paths
  load_config
  resolve_settings

  case "$ACTION" in
    up)
      require_cost_config
      if [[ "$YES" != true ]]; then
        emit_plan          # dry-run: the plan (with cost) is shown, nothing spent
        exit 0
      fi
      case "$PROVIDER" in
        aws) aws_up ;;
        gcp) gcp_up ;;
        *)   die 2 "unknown provider '$PROVIDER'" ;;
      esac
      ;;
    status)
      [[ -n "$PROVIDER" ]] || die 2 "REPO_REMOTE_PROVIDER (or an aws|gcp argument) is required for status"
      case "$PROVIDER" in
        aws) aws_status ;;
        gcp) gcp_status ;;
        *)   die 2 "unknown provider '$PROVIDER'" ;;
      esac
      ;;
    down)
      [[ -n "$PROVIDER" ]] || die 2 "REPO_REMOTE_PROVIDER (or an aws|gcp argument) is required for down"
      case "$PROVIDER" in
        aws) aws_down ;;
        gcp) gcp_down ;;
        *)   die 2 "unknown provider '$PROVIDER'" ;;
      esac
      ;;
  esac
}

main "$@"
