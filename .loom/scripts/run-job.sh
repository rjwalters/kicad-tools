#!/usr/bin/env bash
# run-job.sh — the CLIENT half of the `run-job` seam (epic #6896 Phase 4,
# issue #7853).
#
# Runs a docker-backed workload (a build-gate toolchain, a Lean build, a SPICE
# simulation) from a place that has NO docker socket — in particular from
# inside a Loom worker container — by shipping a *job spec* to an executor
# that is a real docker host, and passing that job's logs and exit code back
# through faithfully.
#
# Per ADR-0017 Decision 3 (operator decision, 2026-08-24): a worker container
# gets no docker socket, mounted or otherwise. A mounted `/var/run/docker.sock`
# is host-root-equivalent — handing it to an agent inside a containment
# boundary defeats the boundary. This seam is the sanctioned alternative, and
# it refuses to carry a container-runtime socket mount in a job spec (see
# `loom_job_validate_spec` in `lib/run-job-exec.sh`).
#
# Full contract: `.loom/docs/run-job-seam.md`
#   (source: defaults/docs/run-job-seam.md)
#
# ## Usage
#
#   run-job.sh [options] -- <command> [args...]      # build a spec from flags
#   run-job.sh --spec <file>|-                       # run a spec from JSON
#   run-job.sh attach <job-id>                       # reattach to a live job
#   run-job.sh status <job-id>                       # one status line
#   run-job.sh cancel <job-id>                       # graceful stop
#
# ## Options
#
#   --image <ref>          Job image (required unless --spec).
#   --mount <path>[:ro|:rw]  Mount an ABSOLUTE host path at the identical
#                          absolute path inside the job container (path parity,
#                          docker/worker/MOUNT-CONTRACT.md §1). Repeatable.
#                          Default mode is rw. A SYMLINKED source is refused
#                          (it would break parity, and it is how a refused path
#                          could otherwise be smuggled past validation) — pass
#                          the resolved path instead.
#   --workdir <abs-path>   Working directory inside the job container.
#   --env KEY=VALUE        Environment variable for the job. Repeatable.
#   --cpus <n>             Job CPU cap (`docker run --cpus`).
#   --memory <size>        Job memory cap (`docker run --memory`), e.g. 4g.
#   --network none|bridge  Job network (default none; host is not offered).
#   --timeout <seconds>    Graceful stop after N seconds (0 = no timeout).
#   --id <job-id>          Explicit job id (default: generated).
#   --executor <mode>      auto|local|ssh (see resolution below).
#   --print-spec           Print the normalized job spec JSON and exit.
#   --dry-run              Print the exact `docker run` argv the executor
#                          would run (one arg per line) and exit.
#
# ## Executor resolution (precedence, highest first)
#
#   1. --executor <mode>
#   2. LOOM_JOB_EXECUTOR env var
#   3. .loom/config.json -> jobs.executor.mode
#   4. "auto" — `local` when a docker daemon is reachable from HERE, else
#      `ssh` to the resolved executor host (the loopback/same-host case).
#
# The loopback/ssh executor is the MANDATORY baseline: a single-host install
# must be self-sufficient, with no external executor dependency (ADR-0017
# Decision 3, "Negative" consequence). Elastic *placement* of executor hosts
# is out of scope and hands off to #3979.
#
# Host/user/ssh resolution (each: env -> config -> default):
#   LOOM_JOB_EXECUTOR_HOST  / jobs.executor.host  / loopback derivation
#                            (host.docker.internal, else the container's
#                             default gateway, else localhost)
#   LOOM_JOB_EXECUTOR_USER  / jobs.executor.user  / (ssh's own default)
#   LOOM_JOB_SSH_OPTIONS    / jobs.executor.sshOptions
#                            / "-o BatchMode=yes -o ConnectTimeout=10"
#   LOOM_JOB_SSH_CMD        / (none)              / "ssh"
#
# ## Exit codes
#
#   0..255  The job's own exit code, passed through faithfully.
#   69      EX_UNAVAILABLE — the executor was unreachable (see "faithfulness"
#           below for why this is never confused with a job that exited 69).
#   75      EX_TEMPFAIL — the job is still running but this client detached
#           (reattach with `run-job.sh attach <id>`).
#   78      EX_CONFIG — invalid job spec, or a misconfigured executor.
#
# ## Exit-code faithfulness
#
# The executor prints `# LOOM_RUN_JOB_EXIT id=<id> code=<n>` on stderr, and
# THAT line — not the transport's exit status — is authoritative here. It is
# what keeps "the job exited 255" distinguishable from "ssh itself failed with
# 255", which a naive `ssh …; exit $?` cannot do. When no sentinel arrives and
# the executor never spoke, the result is a transport failure (69), never a
# fabricated job exit code.
#
# ## Restart safety (issue #5119 drain semantics)
#
# The executor starts the job DETACHED and without `--rm`, so neither this
# client's death nor a daemon restart stops an in-flight job — teardown is not
# cancellation. On a catchable signal this client detaches (exit 75) and
# prints the reattach command; the job keeps running and its exit code stays
# retrievable via `attach`. Cancelling is an explicit act (`cancel`), and even
# then it is a graceful `docker stop`, never `docker kill`.

set -uo pipefail

RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'
log_info() { echo -e "${BLUE}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')]${NC} $*" >&2; }
log_warn() { echo -e "${YELLOW}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WARN${NC} $*" >&2; }
log_error() { echo -e "${RED}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] ERROR${NC} $*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXEC_LIB="$SCRIPT_DIR/lib/run-job-exec.sh"

if [[ ! -f "$EXEC_LIB" ]]; then
    log_error "run-job: executor library not found at $EXEC_LIB"
    exit 78
fi
if ! command -v jq >/dev/null 2>&1; then
    log_error "run-job: 'jq' is required to build and validate a job spec"
    exit 78
fi

# Library mode: reuse the executor's OWN validation and argv builder, so the
# client's pre-flight and the executor's defence-in-depth check can never
# disagree about what a valid, safe spec is.
# shellcheck disable=SC2034  # read by run-job-exec.sh when it is sourced below
LOOM_RUN_JOB_SOURCE_ONLY=1
# shellcheck source=./lib/run-job-exec.sh
source "$EXEC_LIB"
unset LOOM_RUN_JOB_SOURCE_ONLY

# --- Workspace resolution (only to locate .loom/config.json) -----------------
_resolve_workspace() {
    if [[ -n "${LOOM_WORKSPACE:-}" ]]; then
        printf '%s\n' "$LOOM_WORKSPACE"
        return
    fi
    local git_common_dir
    if git_common_dir="$(git rev-parse --git-common-dir 2>/dev/null)"; then
        [[ "$git_common_dir" = /* ]] || git_common_dir="$(cd "$git_common_dir" && pwd)"
        printf '%s\n' "$(dirname "$git_common_dir")"
        return
    fi
    cd "$SCRIPT_DIR/../.." && pwd
}
WORKSPACE="$(_resolve_workspace)"

_cfg() {
    local key="$1" default="${2:-}"
    local lib="$SCRIPT_DIR/lib/config-resolver.sh"
    if [[ -f "$lib" ]]; then
        # shellcheck source=./lib/config-resolver.sh
        source "$lib"
        loom_config_get "$WORKSPACE" "$key" "$default"
    else
        printf '%s' "$default"
    fi
}

# --- Argument parsing --------------------------------------------------------
VERB="run"
SPEC_FILE=""
IMAGE=""
WORKDIR=""
NETWORK=""
CPUS=""
MEMORY=""
TIMEOUT=""
JOB_ID=""
EXECUTOR_MODE=""
PRINT_SPEC=0
DRY_RUN=0
MOUNTS=()
ENVS=()
COMMAND=()

case "${1:-}" in
    attach | status | cancel)
        VERB="$1"
        JOB_ID="${2:-}"
        shift 2 2>/dev/null || shift $#
        ;;
esac

# _require_value <flag> <remaining-argc>
#
# Every branch below that consumes a value MUST call this first. `shift 2` with
# only one positional left does NOT shift — bash leaves the parameters untouched
# and returns non-zero — so `run-job.sh --image` (no value) would spin this
# `while` loop at 100% CPU forever. `set -e` is deliberately off here, so
# nothing else catches it. For a CLI that agents and the daemon invoke
# programmatically, a silent spin is a far worse failure shape than an error.
_require_value() {
    if (($2 < 2)); then
        log_error "run-job: '$1' requires a value"
        exit 78
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --spec)
            _require_value "$1" $#
            SPEC_FILE="$2"
            shift 2
            ;;
        --image)
            _require_value "$1" $#
            IMAGE="$2"
            shift 2
            ;;
        --mount)
            _require_value "$1" $#
            MOUNTS+=("$2")
            shift 2
            ;;
        --workdir)
            _require_value "$1" $#
            WORKDIR="$2"
            shift 2
            ;;
        --env)
            _require_value "$1" $#
            ENVS+=("$2")
            shift 2
            ;;
        --cpus)
            _require_value "$1" $#
            CPUS="$2"
            shift 2
            ;;
        --memory)
            _require_value "$1" $#
            MEMORY="$2"
            shift 2
            ;;
        --network)
            _require_value "$1" $#
            NETWORK="$2"
            shift 2
            ;;
        --timeout)
            _require_value "$1" $#
            TIMEOUT="$2"
            shift 2
            ;;
        --id)
            _require_value "$1" $#
            JOB_ID="$2"
            shift 2
            ;;
        --executor)
            _require_value "$1" $#
            EXECUTOR_MODE="$2"
            shift 2
            ;;
        --print-spec)
            PRINT_SPEC=1
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h | --help)
            sed -n '2,110p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        --)
            shift
            COMMAND=("$@")
            break
            ;;
        -*)
            log_error "run-job: unknown option '$1'"
            exit 78
            ;;
        attach | status | cancel)
            # A lifecycle verb is only a verb while no job is being DEFINED —
            # once --image/--spec or a `--` command is present, the same word
            # is just part of the job's own argv.
            if [[ "$VERB" == "run" && -z "$IMAGE" && -z "$SPEC_FILE" && ${#COMMAND[@]} -eq 0 ]]; then
                VERB="$1"
                JOB_ID="${2:-}"
                shift 2 2>/dev/null || shift $#
            else
                COMMAND=("$@")
                break
            fi
            ;;
        *)
            COMMAND=("$@")
            break
            ;;
    esac
done

# --- Spec construction -------------------------------------------------------
_generate_id() {
    printf 'job-%s-%s' "$(date -u '+%Y%m%dT%H%M%SZ')" "$(od -An -N3 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n' || printf '%06x' $$)"
}

_build_spec_from_flags() {
    local mounts_json env_json command_json
    local m path mode
    mounts_json='[]'
    for m in ${MOUNTS[@]+"${MOUNTS[@]}"}; do
        case "$m" in
            *:ro)
                path="${m%:ro}"
                mode="ro"
                ;;
            *:rw)
                path="${m%:rw}"
                mode="rw"
                ;;
            *)
                path="$m"
                mode="rw"
                ;;
        esac
        mounts_json="$(jq -c --arg p "$path" --arg mo "$mode" '. + [{path:$p, mode:$mo}]' <<<"$mounts_json")"
    done

    env_json='{}'
    local kv key value
    for kv in ${ENVS[@]+"${ENVS[@]}"}; do
        if [[ "$kv" != *=* ]]; then
            log_error "run-job: --env expects KEY=VALUE (got '$kv')"
            return 78
        fi
        key="${kv%%=*}"
        value="${kv#*=}"
        env_json="$(jq -c --arg k "$key" --arg v "$value" '. + {($k): $v}' <<<"$env_json")"
    done

    if ((${#COMMAND[@]} == 0)); then
        command_json='[]'
    else
        command_json="$(printf '%s\n' "${COMMAND[@]}" | jq -R . | jq -sc .)"
    fi

    jq -nc \
        --arg schema "$LOOM_RUN_JOB_SCHEMA" \
        --arg id "$JOB_ID" \
        --arg image "$IMAGE" \
        --arg workdir "$WORKDIR" \
        --arg network "${NETWORK:-none}" \
        --arg cpus "$CPUS" \
        --arg memory "$MEMORY" \
        --argjson timeout "${TIMEOUT:-0}" \
        --argjson command "$command_json" \
        --argjson mounts "$mounts_json" \
        --argjson env "$env_json" \
        '{schema:$schema, id:$id, image:$image, command:$command, workdir:$workdir,
          network:$network, mounts:$mounts, env:$env,
          limits:{cpus:$cpus, memory:$memory}, timeoutSeconds:$timeout}'
}

SPEC=""
if [[ "$VERB" == "run" ]]; then
    [[ -n "$JOB_ID" ]] || JOB_ID="$(_generate_id)"

    if [[ -n "$SPEC_FILE" ]]; then
        if ((${#COMMAND[@]} > 0)) || [[ -n "$IMAGE" ]]; then
            log_error "run-job: --spec is mutually exclusive with --image/<command> (a spec is complete on its own)"
            exit 78
        fi
        local_raw=""
        if [[ "$SPEC_FILE" == "-" ]]; then
            local_raw="$(cat)"
        elif [[ -f "$SPEC_FILE" ]]; then
            local_raw="$(cat "$SPEC_FILE")"
        else
            log_error "run-job: spec file not found: $SPEC_FILE"
            exit 78
        fi
        # An id in the spec wins; otherwise the generated one is filled in, so
        # every job is addressable by `attach`/`status`/`cancel`.
        SPEC="$(jq -c --arg id "$JOB_ID" '. + {id: (if (.id // "") == "" then $id else .id end)}' <<<"$local_raw" 2>/dev/null)" || {
            log_error "run-job: spec file is not valid JSON: $SPEC_FILE"
            exit 78
        }
    else
        SPEC="$(_build_spec_from_flags)" || exit 78
    fi

    if ! SPEC="$(loom_job_spec_defaults "$SPEC")"; then
        log_error "run-job: job spec is not valid JSON"
        exit 78
    fi
    JOB_ID="$(jq -r '.id' <<<"$SPEC")"

    if ((PRINT_SPEC)); then
        jq . <<<"$SPEC"
        exit 0
    fi

    # Client-side pre-flight. The executor validates again on its own host —
    # this copy just fails fast, with the same rules and the same messages.
    loom_job_validate_spec "$SPEC" || exit 78

    if ((DRY_RUN)); then
        loom_job_docker_argv "$SPEC"
        exit 0
    fi
else
    if [[ -z "$JOB_ID" ]]; then
        log_error "run-job: '$VERB' requires a job id"
        exit 78
    fi
fi

# --- Executor resolution -----------------------------------------------------
DOCKER_BIN="${LOOM_RUN_JOB_DOCKER:-docker}"

_local_docker_available() {
    command -v "$DOCKER_BIN" >/dev/null 2>&1 && "$DOCKER_BIN" info >/dev/null 2>&1
}

_derive_loopback_host() {
    # The degenerate, mandatory case: reach the worker's OWN host. Inside a
    # container the host is not "localhost" — Docker Desktop publishes
    # `host.docker.internal`, and a plain Linux bridge network reaches it at
    # the default gateway.
    if command -v getent >/dev/null 2>&1; then
        if getent hosts host.docker.internal >/dev/null 2>&1; then
            printf 'host.docker.internal'
            return
        fi
    elif ! command -v ip >/dev/null 2>&1; then
        # No getent and no iproute2: a macOS/Docker-Desktop-shaped host, where
        # `host.docker.internal` is the canonical name for "my own host".
        printf 'host.docker.internal'
        return
    fi
    local gw=""
    if command -v ip >/dev/null 2>&1; then
        gw="$(ip route 2>/dev/null | awk '/^default/ {print $3; exit}')"
    fi
    printf '%s' "${gw:-localhost}"
}

EXECUTOR_SOURCE=""
if [[ -n "$EXECUTOR_MODE" ]]; then
    EXECUTOR_SOURCE="--executor"
elif [[ -n "${LOOM_JOB_EXECUTOR:-}" ]]; then
    EXECUTOR_MODE="$LOOM_JOB_EXECUTOR"
    EXECUTOR_SOURCE="env (LOOM_JOB_EXECUTOR)"
else
    EXECUTOR_MODE="$(_cfg "jobs.executor.mode" "")"
    if [[ -n "$EXECUTOR_MODE" ]]; then
        EXECUTOR_SOURCE="config (jobs.executor.mode)"
    else
        EXECUTOR_MODE="auto"
        EXECUTOR_SOURCE="default"
    fi
fi

case "$EXECUTOR_MODE" in
    auto)
        if _local_docker_available; then
            EXECUTOR_MODE="local"
        else
            EXECUTOR_MODE="ssh"
        fi
        EXECUTOR_SOURCE="$EXECUTOR_SOURCE -> auto:$EXECUTOR_MODE"
        ;;
    local | ssh) ;;
    *)
        log_error "run-job: unknown executor mode '$EXECUTOR_MODE' (from $EXECUTOR_SOURCE); expected auto|local|ssh"
        exit 78
        ;;
esac

TRANSPORT=()
TRANSPORT_STDIN="/dev/null"
EXECUTOR_LABEL=""

if [[ "$EXECUTOR_MODE" == "local" ]]; then
    if ! command -v "$DOCKER_BIN" >/dev/null 2>&1; then
        log_error "run-job: executor mode 'local' needs '$DOCKER_BIN' on PATH, and it is not."
        log_error "Inside a worker container this is expected and correct — no docker socket is ever mounted there."
        log_error "Configure an ssh executor (jobs.executor.mode=ssh + jobs.executor.host) or leave the mode at 'auto'."
        exit 78
    fi
    EXECUTOR_LABEL="local"
    TRANSPORT=(bash "$EXEC_LIB")
else
    SSH_CMD="${LOOM_JOB_SSH_CMD:-ssh}"
    EXEC_HOST="${LOOM_JOB_EXECUTOR_HOST:-}"
    [[ -n "$EXEC_HOST" ]] || EXEC_HOST="$(_cfg "jobs.executor.host" "")"
    [[ -n "$EXEC_HOST" ]] || EXEC_HOST="$(_derive_loopback_host)"

    EXEC_USER="${LOOM_JOB_EXECUTOR_USER:-}"
    [[ -n "$EXEC_USER" ]] || EXEC_USER="$(_cfg "jobs.executor.user" "")"

    SSH_OPTS_RAW="${LOOM_JOB_SSH_OPTIONS:-}"
    if [[ -z "$SSH_OPTS_RAW" ]]; then
        SSH_OPTS_RAW="$(_cfg "jobs.executor.sshOptions" "" | jq -r 'if type == "array" then join(" ") else . end' 2>/dev/null || true)"
    fi
    [[ -n "$SSH_OPTS_RAW" && "$SSH_OPTS_RAW" != "null" ]] || SSH_OPTS_RAW="-o BatchMode=yes -o ConnectTimeout=10"
    read -r -a SSH_OPTS <<<"$SSH_OPTS_RAW"

    if [[ -n "$EXEC_USER" ]]; then
        EXECUTOR_LABEL="ssh ${EXEC_USER}@${EXEC_HOST}"
        SSH_TARGET="${EXEC_USER}@${EXEC_HOST}"
    else
        EXECUTOR_LABEL="ssh ${EXEC_HOST}"
        SSH_TARGET="$EXEC_HOST"
    fi

    # The executor program travels on stdin to `bash -s`, so the executor host
    # needs no Loom installation at all — bash, docker, jq and coreutils
    # (base64, mktemp, mkfifo, readlink) are the whole remote dependency list.
    TRANSPORT=("$SSH_CMD" ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "$SSH_TARGET" bash -s --)
    TRANSPORT_STDIN="$EXEC_LIB"
fi

# --- Verb arguments ----------------------------------------------------------
if [[ "$VERB" == "run" ]]; then
    SPEC_B64="$(printf '%s' "$SPEC" | base64 | tr -d '\n')"
    VERB_ARGS=(run "$SPEC_B64")
else
    VERB_ARGS=("$VERB" "$JOB_ID")
fi

# --- Client-side restart safety ---------------------------------------------
# A SIGTERM here (daemon drain, operator Ctrl-C) detaches from the stream; it
# does NOT stop the job. The executor's own trap does the same on its side,
# and the job container outlives both by construction.
_client_detach() {
    log_warn "run-job: detaching from job '$JOB_ID' (it keeps running on the executor)."
    log_warn "run-job: reattach with: $(basename "${BASH_SOURCE[0]}") attach $JOB_ID"
    exit 75
}
trap _client_detach TERM INT HUP

# --- Transport invocation ----------------------------------------------------
CAPTURE_DIR="$(mktemp -d)"
CAPTURE="$CAPTURE_DIR/stderr"
cleanup() { rm -rf "$CAPTURE_DIR"; }
trap cleanup EXIT

log_info "run-job: executor=${EXECUTOR_LABEL} (mode=${EXECUTOR_MODE} from ${EXECUTOR_SOURCE}) verb=${VERB} id=${JOB_ID}"

FIFO="$CAPTURE_DIR/err.fifo"
mkfifo "$FIFO"
tee "$CAPTURE" <"$FIFO" >&2 &
TEE_PID=$!

RC=0
"${TRANSPORT[@]}" "${VERB_ARGS[@]}" <"$TRANSPORT_STDIN" 2>"$FIFO" || RC=$?
wait "$TEE_PID" 2>/dev/null || true

# --- Result interpretation ---------------------------------------------------
if [[ "$VERB" == "status" || "$VERB" == "cancel" ]]; then
    exit "$RC"
fi

# Matched unanchored on purpose: a job whose final output line has no trailing
# newline would otherwise leave the executor's sentinel glued to the end of
# that line and unmatchable. `tail -n 1` keeps the LAST match, which is always
# the executor's own sentinel — it is written after the log stream has drained.
SENTINEL="$(grep -ao '# LOOM_RUN_JOB_EXIT id=[^ ]* code=[0-9]\{1,3\}' "$CAPTURE" 2>/dev/null | tail -n 1 || true)"
if [[ -n "$SENTINEL" ]]; then
    CODE="${SENTINEL##*code=}"
    CODE="${CODE%% *}"
    if [[ "$CODE" =~ ^[0-9]+$ ]]; then
        exit "$CODE"
    fi
fi

if grep -qa '# LOOM_RUN_JOB_DETACHED ' "$CAPTURE" 2>/dev/null; then
    log_warn "run-job: job '$JOB_ID' detached and is still running on the executor."
    log_warn "run-job: reattach with: $(basename "${BASH_SOURCE[0]}") attach $JOB_ID"
    exit 75
fi

# The executor spoke (it printed its own diagnostics) but produced no exit
# sentinel: a spec rejection or a docker failure on the executor host. Pass its
# exit code through rather than masking it as a transport failure.
if grep -qa 'run-job-exec: ' "$CAPTURE" 2>/dev/null; then
    exit "$RC"
fi

log_error "run-job: executor ${EXECUTOR_LABEL} is unreachable (transport exit ${RC}, no job result received)."
log_error "run-job: the job did NOT run — this is a transport failure, not a job exit code."
if [[ "$EXECUTOR_MODE" == "ssh" ]]; then
    log_error "run-job: check ssh reachability of ${EXEC_HOST}, and that it has bash + docker + jq + coreutils."
fi
exit 69 # EX_UNAVAILABLE
