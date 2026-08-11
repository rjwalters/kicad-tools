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
#   repo-remote up [--yes] [--force] [--json] [aws|gcp]   Provision (or reuse)
#       an instance.
#       Without --yes: a DRY-RUN plan (resolved spec + estimated cost) is emitted
#       and NOTHING is created — this is the "plan shown before money spent" path.
#       With --yes: the plan is executed. --yes removes the *prompt*, never the
#       *consent requirement*: a cost-relevant field missing from config is a
#       loud, non-zero-exit failure, never a silent default (repo#52 cost gate).
#       --force overrides the fleet-marker guard described below. It does NOT
#       relax the cost gate.
#
#   repo-remote status|--status [--json] [aws|gcp]   List instances this command
#       created (tagged repo-remote=<name>) with state; no mutation.
#
#   repo-remote down|--down [--yes] [--force] [--json] [aws|gcp]   Teardown.
#       Without --yes: a DRY-RUN listing of exactly what would stop/terminate
#       (fleet-marked instances, if any, are annotated but never block a dry
#       run — see the fleet-marker guard below).
#       With --yes: stop them; add --delete to terminate (disk goes with it).
#       --force overrides the fleet-marker guard described below, same as `up`.
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
# Fleet-marker guard (repo#164, repo#170): both `up` and `down` resolve an
# instance from the SAME two never-expiring handles — a pinned
# REPO_REMOTE_INSTANCE_ID, or one carrying the repo-remote=<name> tag/label
# (`down`'s tag-discovery path can resolve more than one). That resolution is
# stale-tag-prone: a host provisioned once for an ephemeral dev session can
# later become a persistent fleet worker while still carrying the repo-remote
# tag, at which point this ephemeral tooling would happily reuse it
# (2AMLogic/2am#52). `down` is the strictly worse case: it STOPS the resolved
# instance, or — with --delete — TERMINATES it, disk and all, unrecoverable.
# So before `up` starts/aliases a REUSED instance, or `down` stops/terminates
# any resolved instance, its tags (AWS) / labels (GCP) are checked for a fleet
# marker — by default Fleet=loom, configurable via REPO_REMOTE_FLEET_TAG_KEY /
# REPO_REMOTE_FLEET_TAG_VALUE. If present, the run STOPS (exit 5) with a clear
# message unless --force is given; with --force it proceeds after a loud
# warning. `down` refuses the WHOLE resolved batch if ANY id in it carries the
# marker, rather than silently acting on a subset. Setting
# REPO_REMOTE_FLEET_TAG_KEY= (empty) disables the check entirely. The guard
# never applies to a freshly created instance (nothing to inherit) and never to
# a dry run (which touches no cloud resource at all) — a `down` dry run
# annotates any fleet-marked instances in its listing instead of blocking.
#
# Exit codes:
#   0  success (including a dry-run plan)
#   2  missing / invalid required config (the cost gate; loud failure)
#   3  provider authentication failed
#   4  cloud operation failed
#   5  refused to act (reuse via `up`, stop/terminate via `down`) on a
#      fleet-marked instance (pass --force to override)
#   64 usage error
#
# Testability hooks (honored so the suite can exercise the full contract against
# mocked cloud CLIs without touching real infrastructure or a real ~/.ssh):
#   XDG_CONFIG_HOME            locates the shared remote.env (already standard)
#   REPO_REMOTE_SSH_CONFIG     SSH config file to write the alias into
#                              (default: ~/.ssh/config)
#   PATH                       mock `aws`/`gcloud`/`curl`/`ssh` are picked up
#                              from PATH (curl backs current-IP detection,
#                              ssh backs the end-of-run reachability check)
#   REPO_REMOTE_IP_ECHO_URL    override the HTTPS echo service used for
#                              current-IP detection (default:
#                              checkip.amazonaws.com); see aws_resolve_ssh_cidr
#   REPO_REMOTE_SSH_LOCK_TIMEOUT       seconds to wait for the write_ssh_alias
#                                      lock before failing loudly (default 15;
#                                      see "SSH alias lock" below, repo#213)
#   REPO_REMOTE_SSH_LOCK_POLL_INTERVAL seconds between lock-acquisition
#                                      retries (default 1)
#
set -uo pipefail

# ── output helpers ──────────────────────────────────────────────────────────
_is_tty() { [[ -t 2 ]]; }
log()  { printf '%s\n' "repo-remote: $*" >&2; }
die()  { local code="$1"; shift; printf '%s\n' "repo-remote: ERROR: $*" >&2; exit "$code"; }

JSON_OUT=false   # --json
YES=false        # --yes
FORCE=false      # --force (override the fleet-marker guard)
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
FLEET_TAG_KEY=""   # tag/label key that marks a managed fleet host ("" disables)
FLEET_TAG_VALUE="" # required value for that key ("" = any non-empty value)
SSH_CIDR=""        # AWS only: pinned SSH-ingress CIDR override (see aws_resolve_ssh_cidr)
REGION=""
COST_HOURLY=""
COST_APPROX=false
COST_BASIS=""      # "table" | "vcpu-scaled" | "heuristic" — how COST_HOURLY was derived

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

# AWS instance-type size suffixes double vCPU count in a well-known, fixed
# progression (large=2 ... 32xlarge=128). This holds for AWS's current
# general-purpose/compute/memory families named `<family>.<size>` — but NOT
# for the burstable `t`-family (a differently-priced CPU-credit model, not a
# flat vCPU rate) or for nano/micro/small/medium sizes (they don't follow the
# doubling pattern). Prints the vCPU count on stdout and returns 0 when the
# type is confidently parseable this way; returns 1 (no output) otherwise so
# the caller falls through to the last-resort flat heuristic.
aws_vcpu_from_size() {  # <instance-type> -> vcpu count
  local t="$1" family size
  family="${t%%.*}"
  size="${t#*.}"
  [[ "$family" == "$size" ]] && return 1   # no "family.size" dot -> not AWS-style
  [[ "$family" =~ ^t[0-9] ]] && return 1   # burstable credit model, not a flat rate
  case "$size" in
    large)    printf '2'   ;;
    xlarge)   printf '4'   ;;
    2xlarge)  printf '8'   ;;
    4xlarge)  printf '16'  ;;
    8xlarge)  printf '32'  ;;
    12xlarge) printf '48'  ;;
    16xlarge) printf '64'  ;;
    24xlarge) printf '96'  ;;
    32xlarge) printf '128' ;;
    *) return 1 ;;
  esac
}

# Approximate on-demand USD/hour by instance type. COST_BASIS records how the
# number was derived so a caller (and the plan/up output) can distinguish a
# real price-table hit from a scaled or last-resort guess:
#   table        — exact match in the case table below
#   vcpu-scaled  — no table entry, but the AWS size suffix parsed to a vCPU
#                  count, scaled by a blended $/vCPU-hr rate
#   heuristic    — no table entry and no parseable vCPU count (or a GPU type
#                  with no table entry — vCPU count is a poor proxy for GPU
#                  instance price, so those stay on the flat GPU heuristic)
# The JSON always carries a number so a caller can implement a budget check,
# and never silently claims precision it doesn't have.
estimate_cost() {  # sets COST_HOURLY, COST_APPROX, COST_BASIS
  local t="$1"
  COST_APPROX=false
  COST_BASIS="table"
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
    # AWS current-gen (7th-gen) compute/general/memory, x86 (c7i/m7i/r7i).
    # Approximate on-demand, us-east-1, derived from a ~$0.0446/vCPU-hr
    # blended rate (cross-checked against this issue's reported
    # c7i.24xlarge ~$4.28/hr anchor: 4.28 / 96 vCPU ≈ $0.0446/vCPU-hr) —
    # re-verify against live AWS pricing before relying on these for a
    # budget-critical decision; larger/unlisted sizes fall through to the
    # vcpu-scaled fallback below rather than being hand-populated here.
    c7i.large)   COST_HOURLY=0.0893 ;;
    c7i.xlarge)  COST_HOURLY=0.1785 ;;
    c7i.2xlarge) COST_HOURLY=0.357  ;;
    m7i.large)   COST_HOURLY=0.1008 ;;
    m7i.xlarge)  COST_HOURLY=0.2016 ;;
    r7i.large)   COST_HOURLY=0.1323 ;;
    r7i.xlarge)  COST_HOURLY=0.2646 ;;
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
      local vcpu
      if [[ "$IS_GPU" != true ]] && vcpu="$(aws_vcpu_from_size "$t")"; then
        COST_BASIS="vcpu-scaled"
        COST_HOURLY="$(awk -v v="$vcpu" 'BEGIN{printf "%.4f", v * 0.045}')"
      else
        COST_BASIS="heuristic"
        if [[ "$IS_GPU" == true ]]; then COST_HOURLY=1.50; else COST_HOURLY=0.20; fi
      fi
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

  # Fleet marker (repo#164). Defaults match the tag 2am's remediation already
  # sets on its persistent workers, so the guard is useful with zero config.
  # An empty key is the deliberate opt-out: no marker lookup is performed.
  FLEET_TAG_KEY="${REPO_REMOTE_FLEET_TAG_KEY-Fleet}"
  FLEET_TAG_VALUE="${REPO_REMOTE_FLEET_TAG_VALUE-loom}"

  # AWS-only SSH-ingress CIDR override (repo#176). Unset (the default) means
  # "detect it"; see aws_resolve_ssh_cidr for the detection + fallback logic.
  # An explicit 0.0.0.0/0 is a valid, deliberate opt-in (still key-only auth).
  SSH_CIDR="${REPO_REMOTE_SSH_CIDR:-}"

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

# ── the fleet-marker guard (reuse discovery, repo#164) ──────────────────────
# `up` never re-attaches user-data to an instance it reuses — the guard a host
# carries is whatever it got at its one-time creation. What reuse DOES do is
# start a stopped instance and rewrite this repo's SSH alias to point at it. The
# instance it reaches is resolved from a pinned REPO_REMOTE_INSTANCE_ID or from
# the repo-remote=<name> tag/label, and neither of those handles expires: a box
# provisioned once as an ephemeral dev session can since have become a
# persistent, daemon-managed fleet worker while still carrying the old tag. That
# is exactly how `repo-remote=anvil` tooling kept rediscovering `loom-worker-1`
# after it became a fleet host (2AMLogic/2am#52).
#
# So: before a REUSED instance is started or aliased, look for a fleet marker
# the fleet-management side already had to set deliberately elsewhere. This is a
# provisioning-time check against declared metadata — deliberately NOT an
# on-host "is some process running" heuristic, which repo#79 rejected for the
# guard's runtime logic and which nothing here reopens.

# True when a discovered marker value counts as "this is a fleet host".
# Compared case-insensitively: GCP lower-cases label values, AWS tags don't.
# An empty REPO_REMOTE_FLEET_TAG_VALUE means "any non-empty value matches".
fleet_marker_matches() {  # <discovered-value>
  local got want
  [[ -n "$1" && "$1" != "None" ]] || return 1
  [[ -n "$FLEET_TAG_VALUE" ]] || return 0
  got="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  want="$(printf '%s' "$FLEET_TAG_VALUE" | tr '[:upper:]' '[:lower:]')"
  [[ "$got" == "$want" ]]
}

# Warn-or-refuse once a marker has been read off a resource being reused.
# Dies with exit 5 unless --force; with --force it warns loudly and continues.
fleet_marker_gate() {  # <resource-id> <marker-value> <"tag"|"label">
  local id="$1" val="$2" kind="$3"
  fleet_marker_matches "$val" || return 0
  if [[ "$FORCE" == true ]]; then
    log "WARNING: ${id} carries the fleet marker ${kind} ${FLEET_TAG_KEY}=${val} — it looks like a managed fleet/daemon host, not an ephemeral dev box. Proceeding anyway because --force was given; this run will start and/or re-alias a production host."
    return 0
  fi
  # Same "repo-remote: ERROR:" shape as die(), but die() is a single line and
  # this refusal is only actionable with the remediation lines that follow it.
  printf '%s\n' "repo-remote: ERROR: refusing to reuse ${id}: it carries the fleet marker ${kind} ${FLEET_TAG_KEY}=${val}." >&2
  log "  That marker means the host is managed as part of a fleet (e.g. a persistent loom-daemon worker), so starting or re-aliasing it from ephemeral dev-session tooling is almost certainly not what you want (2AMLogic/2am#52)."
  log "  If you really mean to target it, re-run with --force."
  log "  To use a different box instead, clear REPO_REMOTE_INSTANCE_ID from ${REPO_ENV:-<git-root>/.env} (and/or remove the repo-remote=${NAME} tag from the fleet host)."
  log "  To disable this check entirely, set REPO_REMOTE_FLEET_TAG_KEY= (empty)."
  exit 5
}

# AWS: read the fleet tag off an instance. Echoes "" when absent/disabled.
aws_fleet_marker() {  # <instance-id>
  [[ -n "$FLEET_TAG_KEY" ]] || return 0
  local v
  v="$(aws ec2 describe-instances --instance-ids "$1" \
      --query "Reservations[0].Instances[0].Tags[?Key=='${FLEET_TAG_KEY}'].Value | [0]" \
      --output text 2>/dev/null)" || return 0
  [[ "$v" == "None" ]] && v=""
  printf '%s' "$v"
}

# GCP: read the fleet label off an instance. Label keys are lower-case on GCP,
# so the configured key is lower-cased for the lookup. Echoes "" when absent.
gcp_fleet_marker() {  # <instance-name>
  [[ -n "$FLEET_TAG_KEY" ]] || return 0
  local k v
  k="$(printf '%s' "$FLEET_TAG_KEY" | tr '[:upper:]' '[:lower:]')"
  v="$(gcloud compute instances describe "$1" --zone "$REGION" \
      --format="value(labels.${k})" 2>/dev/null || true)"
  printf '%s' "$v"
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
#
# IDLE_MIN <= 0 means "guard disabled" (repo#163) — the opt-out an operator
# reaches for via REPO_REMOTE_IDLE_SHUTDOWN_MIN=0, e.g. for a fleet-tagged host
# that should never self-shutdown. This must NOT be handled by feeding 0 into
# the generated script's `(NOW - LAST) / 60 -ge IDLE_MIN` arithmetic — that
# makes the guard fire almost immediately (0 >= 0 is true on the very first
# post-$STAMP tick) instead of never. So the window is validated here, before
# any guard/cron script is emitted at all: a non-positive (or non-numeric)
# IDLE_MIN short-circuits to no output, and callers (aws_create, gcp_up) must
# check idle_guard_enabled too so they skip embedding user-data entirely.
idle_guard_enabled() {
  [[ "$IDLE_MIN" =~ ^[0-9]+$ ]] && (( IDLE_MIN > 0 ))
}

idle_guard_userdata() {
  idle_guard_enabled || return 0
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

# Expand a leading '~' to $HOME. REPO_REMOTE_SSH_KEY's documented default
# (and any operator override) commonly uses '~/...', but bash only
# tilde-expands a literal token — not a value that has been through a
# variable — so anywhere THIS script opens/stats the file itself (unlike
# write_ssh_alias, which hands the raw string to the SSH config's
# IdentityFile and lets ssh expand it) needs this first.
expand_home() {  # <path>
  local p="$1"
  [[ "$p" == "~"* ]] && p="${HOME}${p#\~}"
  printf '%s' "$p"
}

# Compute the EC2 "imported key pair" fingerprint for a public key file, using
# the same DER-encoded-SubjectPublicKeyInfo basis AWS uses for an imported
# (not AWS-generated) key pair: MD5 for RSA, SHA256/base64 for ED25519
# (repo#177). Echoes empty — never dies — on an unsupported key type or a
# missing ssh-keygen/openssl: the caller treats that as "can't dedupe by
# fingerprint" and falls through to import-key-pair, which is always safe (a
# genuine duplicate --key-name is handled by the caller too).
aws_keypair_fingerprint() {  # <path-to-.pub>
  local pub="$1" type
  command -v ssh-keygen >/dev/null 2>&1 && command -v openssl >/dev/null 2>&1 || return 0
  type="$(awk '{print $1}' "$pub" 2>/dev/null)"
  case "$type" in
    ssh-rsa)
      ssh-keygen -f "$pub" -e -m PKCS8 2>/dev/null \
        | openssl pkey -pubin -outform DER 2>/dev/null \
        | openssl md5 -c 2>/dev/null | awk '{print $NF}'
      ;;
    ssh-ed25519)
      # ssh-keygen cannot -e/PKCS8-export an ED25519 key, so the DER
      # SubjectPublicKeyInfo is built by hand: the fixed 12-byte ASN.1 header
      # for an Ed25519 SPKI (RFC 8410) followed by the raw 32-byte public
      # key, which is always the LAST 32 bytes of the OpenSSH wire-format
      # blob (4-byte-length-prefixed "ssh-ed25519" + 4-byte-length-prefixed
      # key material).
      {
        printf '\x30\x2a\x30\x05\x06\x03\x2b\x65\x70\x03\x21\x00'
        awk '{print $2}' "$pub" | openssl base64 -d -A 2>/dev/null | tail -c 32
      } | openssl dgst -sha256 -binary 2>/dev/null | openssl base64 -A 2>/dev/null
      ;;
    *)
      return 0
      ;;
  esac
}

# Resolve (or import) an EC2 key pair name from the local SSH public key
# derived from REPO_REMOTE_SSH_KEY, so aws_create() ALWAYS has a --key-name to
# pass (repo#177: a launch with no key pair attached is unreachable by
# design — this is the fix for that). Sets RESOLVED_KEY_NAME and
# RESOLVED_PUB_KEY_LINE (globals, so a `die` here propagates instead of being
# swallowed by a `$(...)` subshell, matching aws_resolve_image's pattern).
RESOLVED_KEY_NAME=""
RESOLVED_PUB_KEY_LINE=""
aws_resolve_keypair() {
  local priv pub fp existing kname impf imperr
  priv="$(expand_home "${REPO_REMOTE_SSH_KEY:-~/.ssh/id_ed25519}")"
  pub="${priv}.pub"
  [[ -f "$pub" ]] \
    || die 2 "SSH public key not found at ${pub} (derived from REPO_REMOTE_SSH_KEY=${priv}). Generate one (ssh-keygen) or point REPO_REMOTE_SSH_KEY at an existing key pair before provisioning -- a launch with no key pair attached is unreachable by design."
  RESOLVED_PUB_KEY_LINE="$(head -n1 "$pub")"

  fp="$(aws_keypair_fingerprint "$pub")"
  if [[ -n "$fp" ]]; then
    existing="$(aws ec2 describe-key-pairs \
      --filters "Name=fingerprint,Values=${fp}" \
      --query 'KeyPairs[0].KeyName' --output text 2>/dev/null)"
    if [[ -n "$existing" && "$existing" != "None" ]]; then
      RESOLVED_KEY_NAME="$existing"
      return 0
    fi
  fi

  kname="repo-remote-${NAME}"
  impf="$(mktemp)"
  if aws ec2 import-key-pair --key-name "$kname" \
      --public-key-material "fileb://${pub}" >/dev/null 2>"$impf"; then
    RESOLVED_KEY_NAME="$kname"
  else
    imperr="$(cat "$impf" 2>/dev/null)"
    # A prior run may already have imported this exact name (e.g. the
    # fingerprint lookup above missed it) -- AWS rejects that as a duplicate,
    # which is safe to just reuse rather than treat as a hard failure.
    if printf '%s' "$imperr" | grep -q 'InvalidKeyPair.Duplicate'; then
      RESOLVED_KEY_NAME="$kname"
    else
      rm -f "$impf"
      die 4 "aws ec2 import-key-pair failed for ${pub} (key-name ${kname}): ${imperr:-unknown error}"
    fi
  fi
  rm -f "$impf"
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

# aws_public_ip: echoes the instance's public IP (or the literal "None" when
# AWS has not assigned one yet) on success. On an API-call failure (an
# unfulfilled spot request, throttling, a transient AWS error, etc.) it
# returns the underlying `aws` exit code and logs the captured stderr instead
# of silently swallowing it -- repo#216: the two failure modes ("instance
# exists but has no public IP yet" vs "the describe-instances call itself
# failed") are NOT the same thing and must not be collapsed into the same
# empty-string return the caller cannot tell apart. Callers that only care
# about "no IP yet" can keep ignoring the exit status (an empty/"None" string
# is still returned in that case); callers that want to distinguish an actual
# API failure should check `$?`.
aws_public_ip() {  # <instance-id>
  local out rc errf
  errf="$(mktemp)"
  out="$(aws ec2 describe-instances --instance-ids "$1" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>"$errf")"
  rc=$?
  if [[ $rc -ne 0 ]]; then
    log "aws ec2 describe-instances (public IP lookup for ${1}) failed: $(cat "$errf" 2>/dev/null)"
  fi
  rm -f "$errf"
  printf '%s' "$out"
  return "$rc"
}

# ── AWS: security group resolve-or-create + SSH ingress (repo#176) ─────────
# aws_create() previously only conditionally attached a PRE-EXISTING security
# group via REPO_REMOTE_SECURITY_GROUP; if unset, run-instances fell back to
# the VPC's default security group, which has no SSH ingress rule at all — an
# instance provisioned that way times out on SSH indefinitely (the reported
# incident: describe-security-groups showed an empty ingress set). The
# functions below resolve-or-create a security group (idempotent across `up`
# runs, mirroring aws_find_tagged's tag-based instance reuse), authorize SSH
# ingress into it, and verify the rule actually landed before run-instances is
# ever called.

# Find a security group previously created for this repo (by tag). Echoes the
# group id, or empty when none exists yet.
aws_find_tagged_sg() {
  aws ec2 describe-security-groups \
    --filters "Name=tag:repo-remote,Values=${NAME}" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null \
    | grep -v '^None' || true
}

# Resolve the security group to attach into RESOLVED_SG (global, so a `die`
# here propagates rather than being swallowed by a command-substitution
# subshell): an explicit REPO_REMOTE_SECURITY_GROUP wins outright (unchanged
# prior behavior — the operator already chose one); else reuse a
# previously-tagged SG (idempotent across repeated `up` runs); else create one
# and tag it the same way instances are tagged.
RESOLVED_SG=""
aws_resolve_or_create_sg() {
  local sg="${REPO_REMOTE_SECURITY_GROUP:-}"
  if [[ -n "$sg" ]]; then
    RESOLVED_SG="$sg"
    return 0
  fi

  sg="$(aws_find_tagged_sg)"
  if [[ -n "$sg" ]]; then
    RESOLVED_SG="$sg"
    log "reusing existing security group ${sg} (tagged repo-remote=${NAME})"
    return 0
  fi

  local out rc
  out="$(aws ec2 create-security-group \
    --group-name "repo-remote-${NAME}" \
    --description "repo-remote: SSH access for ${NAME}" \
    --tag-specifications "ResourceType=security-group,Tags=[{Key=repo-remote,Value=${NAME}}]" \
    --query 'GroupId' --output text 2>&1)"; rc=$?
  if [[ $rc -ne 0 || -z "$out" || "$out" == "None" ]]; then
    die 4 "aws ec2 create-security-group failed: ${out:-unknown error}"
  fi
  RESOLVED_SG="$out"
  log "created security group ${RESOLVED_SG} (tagged repo-remote=${NAME})"
}

# Resolve the CIDR to authorize for SSH ingress into RESOLVED_SSH_CIDR. An
# explicit REPO_REMOTE_SSH_CIDR always wins (including an explicit 0.0.0.0/0
# opt-in). Otherwise a best-effort current-IP lookup via an HTTPS echo service
# is treated as UNVERIFIED: there is no reliable way for this script to
# confirm the detected address is the one SSH egress will actually use —
# behind an HTTPS proxy it commonly isn't (the reported incident: the echo
# service returned the proxy's address, not the SSH egress address, producing
# a correct-looking /32 that could never match). When detection itself fails
# outright, fall back to 0.0.0.0/0 (SSH stays key-only auth, so this is a
# scan-noise tradeoff, not an auth bypass) with an explicit notice rather than
# silently creating a /32 that can never match.
RESOLVED_SSH_CIDR=""
aws_resolve_ssh_cidr() {
  if [[ -n "${SSH_CIDR:-}" ]]; then
    RESOLVED_SSH_CIDR="$SSH_CIDR"
    log "using REPO_REMOTE_SSH_CIDR override for SSH ingress: ${RESOLVED_SSH_CIDR}"
    return 0
  fi

  local url ip
  url="${REPO_REMOTE_IP_ECHO_URL:-https://checkip.amazonaws.com}"
  ip="$(curl -fsS --max-time 5 "$url" 2>/dev/null | tr -d '[:space:]')"
  if [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    RESOLVED_SSH_CIDR="${ip}/32"
    log "detected current IP ${ip} via ${url} for SSH ingress (unverified — behind an HTTPS proxy this can be a different address than the one SSH egress actually uses; if SSH cannot connect afterward, set REPO_REMOTE_SSH_CIDR explicitly)"
  else
    RESOLVED_SSH_CIDR="0.0.0.0/0"
    log "NOTICE: could not detect current IP via ${url}; falling back to SSH ingress from 0.0.0.0/0 (SSH remains key-only auth). Set REPO_REMOTE_SSH_CIDR to pin a specific CIDR instead."
  fi
}

# Idempotently authorize tcp/22 from the resolved CIDR. A duplicate rule on a
# reused security group is success, not an error.
aws_authorize_ssh_ingress() {  # <sg-id> <cidr>
  local sg="$1" cidr="$2" out rc
  out="$(aws ec2 authorize-security-group-ingress \
    --group-id "$sg" --protocol tcp --port 22 --cidr "$cidr" 2>&1)"; rc=$?
  if [[ $rc -ne 0 ]] && ! printf '%s' "$out" | grep -q 'InvalidPermission.Duplicate'; then
    die 4 "aws ec2 authorize-security-group-ingress failed for ${sg} (tcp/22 from ${cidr}): ${out:-unknown error}"
  fi
}

# Post-authorize verification — this is what would have caught the reported
# incident in-run: a security group whose ingress set was empty
# ({port: null, cidr: []}), with SSH timing out indefinitely as the only
# symptom. Fail loudly here instead, before any instance is even launched.
aws_verify_ssh_ingress() {  # <sg-id>
  local sg="$1" out
  out="$(aws ec2 describe-security-groups --group-ids "$sg" \
    --query 'SecurityGroups[0].IpPermissions[?ToPort==`22`]' --output text 2>/dev/null)"
  [[ -n "$out" && "$out" != "None" ]] \
    || die 4 "security group ${sg} has no tcp/22 ingress rule after provisioning — SSH would time out indefinitely. Check REPO_REMOTE_SECURITY_GROUP / REPO_REMOTE_SSH_CIDR, or add the rule manually with: aws ec2 authorize-security-group-ingress --group-id ${sg} --protocol tcp --port 22 --cidr <your-ip>/32"
}

# Belt-and-suspenders SSH access (repo#177): append the resolved public key to
# ~ubuntu/.ssh/authorized_keys on every boot (this runs on every boot, same as
# the idle-guard cron install below), so the box stays reachable even if
# key-pair attachment itself ever regresses. grep -qxF guards against a
# duplicate line across repeat boots.
authorized_keys_userdata() {  # <pubkey-line>
  local pubkey="$1"
  [[ -n "$pubkey" ]] || return 0
  cat <<EOF
mkdir -p ~ubuntu/.ssh
touch ~ubuntu/.ssh/authorized_keys
grep -qxF '${pubkey}' ~ubuntu/.ssh/authorized_keys || echo '${pubkey}' >>~ubuntu/.ssh/authorized_keys
chown -R ubuntu:ubuntu ~ubuntu/.ssh
chmod 700 ~ubuntu/.ssh
chmod 600 ~ubuntu/.ssh/authorized_keys
EOF
}

# Build the full AWS EC2 user-data script for a newly created instance:
# ALWAYS injects the resolved SSH public key into authorized_keys
# (unconditional belt-and-suspenders, repo#177), then folds in the
# idle-shutdown guard's cron watchdog when idle_guard_enabled (repo#163's
# IDLE_MIN<=0 opt-out still applies to THAT section only).
aws_userdata() {  # <pubkey-line>
  printf '#!/bin/bash\n'
  authorized_keys_userdata "$1"
  if idle_guard_enabled; then
    # idle_guard_userdata() emits its own leading shebang; strip it since the
    # combined script only needs the ONE shebang emitted above.
    idle_guard_userdata | tail -n +2
  fi
}

# Create a fresh instance into CREATED_ID (global, so a `die` propagates rather
# than being swallowed by a command-substitution subshell). run-instances is
# invoked EXACTLY ONCE — capturing stdout and stderr in one call — because a
# retry-to-read-the-error would risk launching a second billable instance.
CREATED_ID=""
aws_create() {
  local ami key udfile errfile iid rc err attached
  aws_resolve_image; ami="$RESOLVED_AMI"
  aws_resolve_keypair; key="$RESOLVED_KEY_NAME"

  # Resolve-or-create the security group and prove it actually allows SSH
  # BEFORE spending money on run-instances (repo#176).
  aws_resolve_or_create_sg                              # sets RESOLVED_SG
  aws_resolve_ssh_cidr                                  # sets RESOLVED_SSH_CIDR
  aws_authorize_ssh_ingress "$RESOLVED_SG" "$RESOLVED_SSH_CIDR"
  aws_verify_ssh_ingress "$RESOLVED_SG"

  udfile="$(mktemp)"; aws_userdata "$RESOLVED_PUB_KEY_LINE" >"$udfile"
  errfile="$(mktemp)"

  local -a args=(ec2 run-instances
    --image-id "$ami"
    --instance-type "$INSTANCE_TYPE"
    --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=${DISK_GB}}"
    --security-group-ids "$RESOLVED_SG"
    --tag-specifications "ResourceType=instance,Tags=[{Key=repo-remote,Value=${NAME}}]"
    # NOTE (repo#177): `--user-data` here takes `file://<path>` at LAUNCH time.
    # A POST-launch update (e.g. a future repair-in-place tool) is a different
    # call — `modify-instance-attribute --user-data Value=<base64>` — NOT
    # `--attribute userData --value fileb://...`, which fails AWS CLI
    # parameter validation.
    --user-data "file://${udfile}"
    # Always pass --key-name — never conditionally. A key-less launch is
    # exactly the `KeyName: None` / unreachable-by-design failure this fix
    # exists to prevent (repo#177); aws_resolve_keypair() above either
    # resolves one or dies loudly, so $key is never empty here.
    --key-name "$key"
    --query 'Instances[0].InstanceId' --output text)

  iid="$(aws "${args[@]}" 2>"$errfile")"; rc=$?
  err="$(cat "$errfile" 2>/dev/null)"
  rm -f "$udfile" "$errfile"

  if [[ $rc -ne 0 || -z "$iid" || "$iid" == "None" ]]; then
    # Surface the quota-exceeded case with the exact remediation (remote.md).
    if printf '%s' "$err" | grep -q 'VcpuLimitExceeded'; then
      if [[ "$IS_GPU" == true ]]; then
        die 4 "AWS GPU vCPU quota is 0 by default (VcpuLimitExceeded). Request a limit >= this type's vCPUs at Service Quotas -> EC2 -> quota code L-DB2E81BA, then retry."
      else
        die 4 "AWS standard vCPU quota exceeded (VcpuLimitExceeded). Request a limit >= this type's vCPUs at Service Quotas -> EC2 -> quota code L-1216C47A (Running On-Demand Standard instances), then retry."
      fi
    fi
    die 4 "aws ec2 run-instances failed: ${err:-unknown error}"
  fi

  # Post-create verification (repo#177): confirm the instance actually came up
  # with a key pair attached before reporting success — catches a regression
  # in-run instead of surfacing later as a silent `Permission denied
  # (publickey)`. Deliberately checked here (creation only); a REUSED instance
  # is not re-verified since this run never touched its key-pair attachment.
  attached="$(aws ec2 describe-instances --instance-ids "$iid" \
    --query 'Reservations[0].Instances[0].KeyName' --output text 2>/dev/null)"
  if [[ -z "$attached" || "$attached" == "None" ]]; then
    die 4 "instance ${iid} launched with no KeyName attached (expected '${key}') — refusing to report success on a host that is unreachable by design. It was NOT auto-terminated; clean it up manually: aws ec2 terminate-instances --instance-ids ${iid}"
  fi

  CREATED_ID="$iid"
}

# End-of-run reachability probe (repo#176 AC3): a correctly-authorized
# security group can still leave an unreachable box (a detected-but-wrong
# CIDR, no route, an unexpected image/user, etc.) — this catches that in-run,
# right after the SSH alias is written, instead of it surfacing as a bare
# timeout on the caller's next attempt. AWS-only, mirroring the scope of the
# rest of this fix (GCP already documents OS Login / IAP instead).
aws_check_reachability() {  # <ssh-alias> <ip>
  if [[ -z "$2" ]]; then
    log "no public IP resolved yet; skipping the end-of-run SSH reachability check"
    return 0
  fi
  if ssh -o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$1" true >/dev/null 2>&1; then
    log "SSH reachability check passed (${1})"
  else
    die 4 "SSH reachability check failed for ${1} (${2}) after provisioning. The instance was created/started and its SSH alias written, but SSH did not respond within 10s. Check the security group ingress rule (REPO_REMOTE_SSH_CIDR), REPO_REMOTE_SSH_KEY, and REPO_REMOTE_SSH_USER, or retry: ssh ${1}"
  fi
}

aws_up() {
  aws_authenticate
  local iid="" state="" reused=false

  if [[ -n "$INSTANCE_ID" ]]; then
    state="$(aws_instance_state "$INSTANCE_ID")"
    # Fleet-marker guard BEFORE any start/alias: a pinned id can outlive the
    # host's role as an ephemeral dev box (repo#164). Skipped when the pin is
    # already stale (missing) — there is nothing to protect.
    if [[ "$state" != missing ]]; then
      fleet_marker_gate "$INSTANCE_ID" "$(aws_fleet_marker "$INSTANCE_ID")" tag
    fi
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
      # Same guard on the tag-discovery path — the repo-remote=<name> tag is
      # exactly the stale handle that let dev tooling rediscover a fleet host.
      fleet_marker_gate "$iid" "$(aws_fleet_marker "$iid")" tag
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
  local ip
  if ! ip="$(aws_public_ip "$iid")"; then
    # aws_public_ip already logged the underlying API error; treat it the
    # same as "no public IP yet" here rather than dying -- the instance is up
    # and reachable state may still resolve on a later `status`/`up` run, and
    # write_ssh_alias() below independently refuses to write a broken stanza
    # for an empty/None IP either way (repo#216).
    log "continuing with no public IP for ${iid} (see error above)"
    ip=""
  fi
  [[ "$ip" == "None" ]] && ip=""

  writeback_instance_id "$iid"
  local alias
  if ! alias="$(write_ssh_alias "$ip")"; then
    log "SSH alias write for ${alias} was rejected (see error above); the SSH config was left untouched -- ssh ${alias} (or git-over-SSH via it) will not work until this is retried"
  fi

  aws_check_reachability "$alias" "$ip"

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

  # Fleet-marker guard (repo#164, repo#170) — `down` resolves instances from
  # the SAME never-expiring handles `up` does (a pinned REPO_REMOTE_INSTANCE_ID,
  # or the repo-remote=<name> tag, which can resolve MULTIPLE ids here unlike
  # `up`'s single-id resolution). A dry run touches no cloud resource, so it is
  # only annotated below, never blocked.
  if [[ "$YES" != true ]]; then
    local marked="" id mv
    for id in $ids; do
      mv="$(aws_fleet_marker "$id")"
      fleet_marker_matches "$mv" && marked="${marked:+$marked }$id"
    done
    emit_down_result "$ids" "dry-run" "$marked"
    return 0
  fi

  # About to actually mutate: check EVERY resolved id BEFORE any
  # stop/terminate call is made. fleet_marker_gate is a no-op for an unmarked
  # id; for a marked one it dies (exit 5) unless --force, in which case it
  # warns and returns. Checking the whole list up front — rather than
  # skipping just the marked ids and acting on the rest — means a refusal
  # here leaves every resolved instance untouched (refuse the WHOLE batch,
  # the safer default per repo#170: a partial stop/terminate is a worse
  # operator surprise than an outright refusal).
  local id
  for id in $ids; do
    fleet_marker_gate "$id" "$(aws_fleet_marker "$id")" tag
  done

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
    # Fleet-marker guard BEFORE any start/alias (repo#164) — the GCP analogue of
    # the AWS reuse check, against instance labels instead of tags.
    fleet_marker_gate "$vm" "$(gcp_fleet_marker "$vm")" label
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
    # IDLE_MIN<=0 means the guard is disabled (repo#163) — skip embedding the
    # startup-script metadata at all rather than passing an empty/no-op script.
    idle_guard_enabled && args+=(--metadata-from-file "startup-script=${ud}")
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

  # Fleet-marker guard (repo#164, repo#170) — GCP analogue of aws_down's guard,
  # against the resolved instance's labels. `down` here only ever resolves a
  # single vm (derived name or pinned id), so no batch semantics are needed —
  # this mirrors gcp_up's single fleet_marker_gate call. A dry run touches no
  # cloud resource, so it is only annotated, never blocked.
  if [[ "$YES" != true ]]; then
    local marked=""
    fleet_marker_matches "$(gcp_fleet_marker "$vm")" && marked="$vm"
    emit_down_result "$vm" "dry-run" "$marked"
    return 0
  fi
  fleet_marker_gate "$vm" "$(gcp_fleet_marker "$vm")" label

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

# ── SSH alias lock (repo#213) ───────────────────────────────────────────────
# write_ssh_alias()'s read-modify-write below (strip any existing "Host
# <alias>" block from $cfg, then append a fresh one) is NOT safe against a
# second, concurrent write_ssh_alias() call -- e.g. two overlapping
# `/repo:remote` launches, or any other writer of the same $cfg. Each
# invocation snapshots the file, appends its own block, and `mv`s its own copy
# over $cfg -- last writer wins, silently dropping the other alias block. This
# `mkdir`-based lock (the same POSIX-atomic primitive
# .loom/scripts/worktree.sh uses for its own concurrency guard -- chosen there
# because `flock` is unavailable on stock macOS, the same platform the
# incident that motivated this fix was observed on) wraps the ENTIRE
# read-modify-write, not just the final `mv`; locking only the `mv` would
# still let two writers race the `awk` read and clobber each other's edits.
REPO_REMOTE_SSH_LOCK_TIMEOUT="${REPO_REMOTE_SSH_LOCK_TIMEOUT:-15}"
REPO_REMOTE_SSH_LOCK_POLL_INTERVAL="${REPO_REMOTE_SSH_LOCK_POLL_INTERVAL:-1}"

# acquire_ssh_alias_lock <cfg> -- atomically creates "<cfg>.lock" (mkdir),
# retrying until REPO_REMOTE_SSH_LOCK_TIMEOUT elapses. A lock left behind by a
# process that no longer exists (stale, e.g. killed mid-write) is cleared once
# and retried. Fails LOUDLY (die, non-zero exit) on timeout rather than
# hanging forever or silently skipping the write.
acquire_ssh_alias_lock() {  # <cfg>
  local cfg="$1" lock="$1.lock"
  local deadline=$(( $(date +%s) + REPO_REMOTE_SSH_LOCK_TIMEOUT ))
  local stale_retry_done=0
  while true; do
    if mkdir "$lock" 2>/dev/null; then
      echo "$$" >"$lock/owner.pid" 2>/dev/null || true
      return 0
    fi

    local owner_pid=""
    [[ -f "$lock/owner.pid" ]] && owner_pid="$(cat "$lock/owner.pid" 2>/dev/null || true)"
    if [[ -n "$owner_pid" ]] && [[ "$stale_retry_done" -eq 0 ]] && ! kill -0 "$owner_pid" 2>/dev/null; then
      rm -rf "$lock" 2>/dev/null || true
      stale_retry_done=1
      continue
    fi

    if [[ $(date +%s) -ge $deadline ]]; then
      die 4 "timed out after ${REPO_REMOTE_SSH_LOCK_TIMEOUT}s waiting for the SSH config lock (${lock}) -- a concurrent repo-remote invocation may be writing ${cfg}; remove the lock dir manually if no such process is actually running"
    fi
    sleep "$REPO_REMOTE_SSH_LOCK_POLL_INTERVAL"
  done
}

release_ssh_alias_lock() {  # <cfg>
  rm -rf "$1.lock" 2>/dev/null || true
}

# Write/refresh the one-word SSH alias so the connection is `ssh repo-remote-<name>`.
# Honors REPO_REMOTE_SSH_CONFIG (default ~/.ssh/config) so tests never touch a
# real config. Echoes the alias name. Returns non-zero (without touching
# $cfg) if the generated stanza fails validation -- see below.
write_ssh_alias() {  # <ip>
  local ip="$1" alias="repo-remote-${NAME}"
  local cfg="${REPO_REMOTE_SSH_CONFIG:-$HOME/.ssh/config}"
  local key="${REPO_REMOTE_SSH_KEY:-~/.ssh/id_ed25519}"
  local user="${REPO_REMOTE_SSH_USER:-ubuntu}"

  # Treat empty, whitespace-only, and the literal "None" (the AWS CLI's
  # `--output text` rendering of a null scalar -- see aws_public_ip()) as "no
  # IP yet" identically: skip writing a stanza and return the alias
  # unchanged. The bare `-z "$ip"` guard this replaces caught only the empty
  # string, so a whitespace value or the literal "None" could slip through
  # and produce a HostName-less stanza -- which OpenSSH does not skip, it
  # refuses to parse the ENTIRE config file, taking every other Host block
  # (and therefore git-over-SSH) down with it (repo#216).
  local ip_trimmed="$ip"
  ip_trimmed="${ip_trimmed#"${ip_trimmed%%[![:space:]]*}"}"
  ip_trimmed="${ip_trimmed%"${ip_trimmed##*[![:space:]]}"}"
  if [[ -z "$ip_trimmed" || "$ip_trimmed" == "None" ]]; then
    printf '%s' "$alias"
    return 0
  fi
  ip="$ip_trimmed"

  mkdir -p "$(dirname "$cfg")" 2>/dev/null || true
  acquire_ssh_alias_lock "$cfg"

  # mktemp INSIDE $(dirname "$cfg") (never the default $TMPDIR) so the final
  # `mv` below is guaranteed a same-filesystem atomic rename regardless of
  # where $TMPDIR points -- a cross-filesystem mv degrades to
  # copy-then-unlink, which exposes a window where a concurrent reader (e.g.
  # ssh itself) could observe a partial file.
  local tmp; tmp="$(mktemp "$(dirname "$cfg")/.ssh_config.XXXXXX")"
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

  # Validate the WRITTEN temp file is actually parseable before it ever
  # replaces the real config -- `ssh -G` resolves/prints the effective config
  # for the given host without connecting, so this is a pure syntax check
  # (repo#216). A tightened value check above should make this unreachable
  # in practice, but this is the backstop that closes the bug class
  # regardless of which upstream path produced a bad value: a tool that
  # edits ~/.ssh/config must never be able to leave it unparseable.
  if ! ssh -G -F "$tmp" "$alias" >/dev/null 2>&1; then
    log "generated SSH config block for '${alias}' (ip='${ip}') failed to parse -- leaving ${cfg} untouched"
    rm -f "$tmp"
    release_ssh_alias_lock "$cfg"
    printf '%s' "$alias"
    return 1
  fi

  mv "$tmp" "$cfg"
  chmod 600 "$cfg" 2>/dev/null || true
  release_ssh_alias_lock "$cfg"
  printf '%s' "$alias"
}

# ── result emitters ─────────────────────────────────────────────────────────
# cost_note_human -> a human-readable suffix explaining COST_BASIS/COST_APPROX
# on the printed cost line. A vcpu-scaled or heuristic guess says so
# explicitly rather than the generic "(approximate)" — a confidently-wrong
# flat number is worse for the cost-consent gate than an honestly-vague one.
cost_note_human() {
  [[ "$COST_APPROX" != true ]] && return 0
  case "$COST_BASIS" in
    vcpu-scaled) printf ' (no price data for this type — rough vCPU-scaled guess)' ;;
    heuristic)   printf ' (approximate — no price data for this type)' ;;
    *)           printf ' (approximate)' ;;  # e.g. a table price + GCP accelerator surcharge
  esac
}

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
    printf '"estimated_cost_approximate":%s,' "$COST_APPROX"
    printf '"estimated_cost_basis":"%s"' "$(json_escape "$COST_BASIS")"
    printf '}\n'
  else
    log "PLAN (dry run — nothing created; pass --yes to provision):"
    log "  provider:            $PROVIDER"
    log "  instance type:       $INSTANCE_TYPE$([[ "$IS_GPU" == true ]] && echo ' (GPU)')"
    log "  region/zone:         $REGION"
    log "  disk:                ${DISK_GB} GB"
    log "  idle shutdown:       ${IDLE_MIN} min"
    log "  est. hourly cost:    \$${COST_HOURLY}/hr$(cost_note_human)"
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
    printf '"estimated_cost_approximate":%s,' "$COST_APPROX"
    printf '"estimated_cost_basis":"%s"' "$(json_escape "$COST_BASIS")"
    printf '}\n'
  else
    log "$([[ "$reused" == true ]] && echo reused || echo created) instance $id (${INSTANCE_TYPE}) @ ${ip:-<no public ip>}"
    log "  ssh alias:        $alias"
    log "  est. hourly cost: \$${COST_HOURLY}/hr$(cost_note_human)"
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

emit_down_result() {  # <ids> <disposition: noop|dry-run|stopped|terminated> [fleet-marked ids]
  # The 3rd arg (repo#170) is populated only for a "dry-run" disposition — a
  # dry run never blocks on the fleet-marker guard (see aws_down/gcp_down), so
  # this is how it surfaces which of the listed ids WOULD be refused (absent
  # --force) if the caller re-ran with --yes.
  local ids="$1" disp="$2" marked="${3:-}"
  if [[ "$JSON_OUT" == true ]]; then
    printf '{"action":"down","provider":"%s","name":"%s","disposition":"%s","instances":[' \
      "$(json_escape "$PROVIDER")" "$(json_escape "$NAME")" "$(json_escape "$disp")"
    local first=true id
    for id in $ids; do
      [[ "$first" == true ]] && first=false || printf ','
      printf '"%s"' "$(json_escape "$id")"
    done
    printf '],"fleet_marked":['
    first=true
    for id in $marked; do
      [[ "$first" == true ]] && first=false || printf ','
      printf '"%s"' "$(json_escape "$id")"
    done
    printf ']}\n'
  else
    case "$disp" in
      noop)     log "no instances tagged repo-remote=${NAME} to stop" ;;
      dry-run)  log "DRY RUN — would stop$([[ "$DELETE" == true ]] && echo /terminate): $ids (pass --yes to act)"
                [[ -n "$marked" ]] && log "  NOTE: fleet-marked (would be refused without --force): $marked" ;;
      stopped)  log "stopped: $ids" ;;
      terminated) log "terminated (disks removed): $ids" ;;
    esac
  fi
}

# ── argument parsing ────────────────────────────────────────────────────────
usage() {
  # Print the whole leading comment block (from line 4 to the first non-comment
  # line) rather than a hard-coded line range, so --help cannot silently start
  # truncating the header when documentation is added to it.
  awk 'NR >= 4 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      up|status|down) [[ -z "$ACTION" ]] && ACTION="$1" || die 64 "multiple actions given ($ACTION, $1)" ;;
      --status)       ACTION="status" ;;
      --down)         ACTION="down" ;;
      --yes|-y)       YES=true ;;
      --force)        FORCE=true ;;
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

# Guard so this file can be `source`d (e.g. by the test suite, to call
# write_ssh_alias() directly for the concurrency test in repo#213) without
# also invoking main() -- mirrors the identical idiom in
# .loom/scripts/lib/github-app-token.sh.
if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
  main "$@"
fi
