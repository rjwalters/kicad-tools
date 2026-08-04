#!/usr/bin/env bash
# spawn-generic.sh - Tier-3 "generic passthrough" runtime adapter TEMPLATE
# (issue #4780, epic #4167).
#
# This is NOT a tier-1/tier-2 adapter. It is the shared shape a low-trust,
# unverified CLI is wired through when the operator wants to try it against a
# READ-ONLY role (Judge, Curator, Guide, Auditor) without first writing a full
# guardrail-parity document. It satisfies only TWO of the seven contract
# points in `defaults/docs/runtime-adapters.md`:
#
#   1. Spawn                 — headless prompt execution -> exit code.
#   3. Error classification  — generic-transient FALLTHROUGH only; no
#                               per-provider pattern table is written for a
#                               tier-3 runtime (see classify-error.sh).
#
# Every other contract point (model mapping, usage accounting, instruction
# format, permission/sandbox mapping, capability declaration) is deliberately
# NOT implemented for tier-3 — that is what keeps it "unverified" rather than
# "tier-2, gapped". The mechanism that makes this safe is contract point 7:
# a tier-3 runtime's capability manifest (`defaults/runtimes/<name>.json`)
# declares `worktreeIsolation: "no"` EXPLICITLY (not "partial" — there is no
# ambiguity to record), so `check-runtime-capabilities.sh` fails closed for
# every role whose `runtimeRequirements` includes `worktreeIsolation`
# (Builder, Doctor) by the SAME mechanism that already keeps Codex's
# `partial` out of Builder. No new enforcement code was needed for this: the
# checker already treats anything other than exactly "yes" as unmet.
#
# This script is a TEMPLATE, not a single fixed adapter: it is driven
# entirely by environment variables so one shared implementation backs many
# thin per-CLI wrappers (`spawn-<cli>.sh`, e.g. `spawn-aider.sh`, the worked
# example shipped alongside this file). A wrapper pins the underlying binary
# name and prompt-delivery flag, then `exec`s this script:
#
#   #!/usr/bin/env bash
#   LOOM_GENERIC_RUNTIME_NAME="aider" \
#   LOOM_GENERIC_CLI_BIN="${LOOM_GENERIC_CLI_BIN:-aider}" \
#   LOOM_GENERIC_PROMPT_FLAG="${LOOM_GENERIC_PROMPT_FLAG:---message}" \
#       exec "$(dirname "${BASH_SOURCE[0]}")/spawn-generic.sh" "$@"
#
# `spawn-worker.sh` resolves `spawn-<runtime>.sh` by name and needs no change
# to dispatch a tier-3 runtime — the dispatch seam already generalizes.
#
# Env vars:
#   LOOM_GENERIC_RUNTIME_NAME  Required. The logical runtime name used in log
#                              lines and as the `classify_error` provider
#                              argument (which resolves to NO pattern table,
#                              i.e. the generic-transient fallthrough).
#   LOOM_GENERIC_CLI_BIN       Required. The underlying CLI binary to exec
#                              (looked up on PATH).
#   LOOM_GENERIC_PROMPT_FLAG   Optional. The flag the underlying CLI uses for
#                              non-interactive prompt delivery (e.g.
#                              `--message`). When unset, the prompt is
#                              delivered as the final POSITIONAL argument
#                              instead (no flag emitted).
#   LOOM_GENERIC_MODEL_FLAG    Optional. The flag the underlying CLI uses for
#                              model selection (e.g. `--model`). When unset,
#                              `LOOM_MODEL` is silently NOT forwarded (the
#                              contract's "omit, no error" facet — tier-3 has
#                              no logical-tier mapping).
#   LOOM_GENERIC_NO_EXEC       Test/CI hook: print the argv this script WOULD
#                              run (prefixed `spawn-generic would-exec:`) and
#                              exit 0. Never touches the real CLI.
#   LOOM_SWEEP_NICE / LOOM_SWEEP_NICENESS / LOOM_SWEEP_TASKPOLICY_CLASS
#                              Scheduling priority, applied the same way
#                              spawn-claude.sh / spawn-codex.sh apply it
#                              (issue #4233) — a runner-level re-exec covers
#                              the whole tree with no double-apply.
#
# Usage:
#   -p "your prompt" / --prompt "your prompt"   Deliver a headless prompt.
#   Every other argument is forwarded to the underlying CLI verbatim.
#
# NOTE: this file's own basename makes `LOOM_RUNTIME=generic` resolve through
# spawn-worker.sh's dispatch seam, same as any other spawn-<runtime>.sh. That
# is expected but not directly useful: with neither env var pinned it exits
# 78 immediately (see below). Always dispatch through a per-CLI wrapper (like
# spawn-aider.sh) that pins both, not through `LOOM_RUNTIME=generic` directly.
#
# NOT implemented (by design — this is the point of tier-3):
#   - No token pool / account rotation (usage accounting, contract point 4).
#   - No transcript (cost fidelity degrades to `none`, per the usage-
#     accounting contract's degraded-fidelity clause).
#   - No sandbox/guard mapping (contract point 6) and therefore NO guardrail-
#     parity document — refused for worktree-mutating roles by construction
#     via the capability manifest instead of by a documented residual gap.
#   - No custom error-classification pattern table (contract point 3 beyond
#     the generic fallthrough) — a tier-3 runtime's failures resolve through
#     `classify_error`'s exit-code-first engine and its unmatched-provider
#     fallthrough (RECOVERABLE), same as any truly unknown provider today.

set -euo pipefail

RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')]${NC} $*" >&2; }
log_warn() { echo -e "${YELLOW}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WARN${NC} $*" >&2; }
log_error() { echo -e "${RED}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] ERROR${NC} $*" >&2; }

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' | sed '$d'
    exit 0
fi

RUNTIME_NAME="${LOOM_GENERIC_RUNTIME_NAME:-}"
CLI_BIN="${LOOM_GENERIC_CLI_BIN:-}"

if [[ -z "$RUNTIME_NAME" ]]; then
    log_error "LOOM_GENERIC_RUNTIME_NAME is required (a tier-3 wrapper must pin it before exec'ing this template)."
    exit 78  # EX_CONFIG
fi
if [[ -z "$CLI_BIN" ]]; then
    log_error "LOOM_GENERIC_CLI_BIN is required (a tier-3 wrapper must pin the underlying CLI binary name)."
    exit 78  # EX_CONFIG
fi

# --- Sweep/role-runner scheduling priority (issue #4233) ---
if [[ -z "${LOOM_SWEEP_NICED:-}" && "${LOOM_SWEEP_NICE:-1}" != "0" ]]; then
    _sweep_niceness="${LOOM_SWEEP_NICENESS:-10}"
    _sweep_taskpolicy_class="${LOOM_SWEEP_TASKPOLICY_CLASS:-}"
    if [[ -n "$_sweep_taskpolicy_class" ]] && command -v taskpolicy >/dev/null 2>&1; then
        taskpolicy -c "$_sweep_taskpolicy_class" -p $$ >/dev/null 2>&1 \
            && log_info "spawn-generic($RUNTIME_NAME): applied taskpolicy -c $_sweep_taskpolicy_class (issue #4233)" \
            || log_warn "spawn-generic($RUNTIME_NAME): taskpolicy -c $_sweep_taskpolicy_class failed (non-fatal)"
    fi
    if [[ "$_sweep_niceness" != "0" ]] && command -v nice >/dev/null 2>&1; then
        export LOOM_SWEEP_NICED=1
        exec nice -n "$_sweep_niceness" "$0" "$@"
    fi
fi

log_info "spawn-generic: runtime=$RUNTIME_NAME bin=$CLI_BIN tier=3 LOOM_SWEEP_CLAIM_OWNED=${LOOM_SWEEP_CLAIM_OWNED:-unset}"

# --- Argument parsing (contract point 1: args passthrough + prompt delivery) ---
PROMPT=""
HAS_PROMPT=false
PASSTHROUGH_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--prompt)
            if [[ $# -lt 2 ]]; then
                log_error "$1 requires a value"
                exit 78  # EX_CONFIG
            fi
            PROMPT="$2"
            HAS_PROMPT=true
            shift 2
            ;;
        -p=*)
            PROMPT="${1#-p=}"
            HAS_PROMPT=true
            shift
            ;;
        --prompt=*)
            PROMPT="${1#--prompt=}"
            HAS_PROMPT=true
            shift
            ;;
        --dangerously-skip-permissions|--use-wrapper|--effort|--effort=*)
            # Loom's runner-neutral conventions. A tier-3 template has no
            # sandbox/effort mapping to translate them into, so they are
            # CONSUMED (never forwarded) rather than passed through
            # unrecognized to an underlying CLI that does not understand
            # them. --effort takes a value; consume it too.
            case "$1" in
                --effort)
                    if [[ $# -ge 2 ]]; then shift; fi
                    ;;
            esac
            shift
            ;;
        --)
            shift
            PASSTHROUGH_ARGS+=("$@")
            break
            ;;
        *)
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
    esac
done

# --- Model mapping (contract point 2: minimalism, matches spawn-codex.sh) ---
if [[ -n "${LOOM_GENERIC_MODEL_FLAG:-}" && -n "${LOOM_MODEL:-}" ]]; then
    PASSTHROUGH_ARGS+=("$LOOM_GENERIC_MODEL_FLAG" "$LOOM_MODEL")
    log_info "spawn-generic($RUNTIME_NAME): model=$LOOM_MODEL (via $LOOM_GENERIC_MODEL_FLAG)"
else
    log_info "spawn-generic($RUNTIME_NAME): model=default (no LOOM_GENERIC_MODEL_FLAG configured; LOOM_MODEL not forwarded)"
fi

# --- Prompt delivery: configured flag, else positional (last argv element) ---
# The guarded ${arr[@]+"${arr[@]}"} expansion (matches spawn-codex.sh) avoids
# an "unbound variable" error under `set -u` on bash 3.2 (macOS's default
# /bin/bash) when PASSTHROUGH_ARGS is a declared-but-empty array.
CLI_ARGS=(${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"})
if [[ "$HAS_PROMPT" == "true" ]]; then
    if [[ -n "${LOOM_GENERIC_PROMPT_FLAG:-}" ]]; then
        CLI_ARGS+=("$LOOM_GENERIC_PROMPT_FLAG" "$PROMPT")
    else
        CLI_ARGS+=("$PROMPT")
    fi
fi

# --- Test/CI hook: surface the resolved argv without touching the real CLI ---
if [[ -n "${LOOM_GENERIC_NO_EXEC:-}" ]]; then
    echo "# LOOM_CLI_START runtime=$RUNTIME_NAME" >&2
    echo "spawn-generic would-exec: $CLI_BIN ${CLI_ARGS[*]}"
    exit 0
fi

# --- Runtime-missing failure (contract point 1) ---
if ! command -v "$CLI_BIN" >/dev/null 2>&1; then
    log_error "'$CLI_BIN' command not found in PATH."
    log_error "Tier-3 runtime '$RUNTIME_NAME' has no Loom-managed install step — install it yourself and retry."
    exit 127
fi

# --- Dispatch ---
# Interactive (no prompt): a plain pid-preserving exec, stdin untouched.
if [[ "$HAS_PROMPT" != "true" ]]; then
    echo "# LOOM_CLI_START runtime=$RUNTIME_NAME" >&2
    exec "$CLI_BIN" ${CLI_ARGS[@]+"${CLI_ARGS[@]}"}
fi

# Headless run: neutralize stdin (a dispatched worker's stdin is a pipe
# nobody writes to; see spawn-codex.sh's "Neutralize stdin" adapter-author
# lesson in runtime-adapters.md). Captured as a child (not `exec`) so the
# exit code can still be classified below.
#
# Deliberate tier-3 simplification: stdout and stderr are COMBINED (2>&1)
# before being replayed on this script's stdout. A verified tier-2 adapter
# must empirically check its stream split (spawn-codex.sh's "Check which
# stream your runtime's metadata is on" lesson) because a caller pipes its
# stdout; a tier-3 template has no verified split to preserve, so it makes
# the conservative choice of never silently dropping half the runtime's
# output rather than guessing which stream carries what.
set +e
echo "# LOOM_CLI_START runtime=$RUNTIME_NAME" >&2
_output="$("$CLI_BIN" ${CLI_ARGS[@]+"${CLI_ARGS[@]}"} </dev/null 2>&1)"
_exit_code=$?
set -e
printf '%s\n' "$_output"

# --- Error classification (contract point 3: generic-transient fallthrough
# ONLY — a tier-3 runtime deliberately gets no bespoke pattern table). Passing
# $RUNTIME_NAME as the provider is safe by construction: classify_error
# matches no table for an unregistered provider and falls straight through to
# the shared engine's exit-code-first ordering and generic transients. ---
_classifier_lib="${_SCRIPT_DIR}/lib/classify-error.sh"
if [[ -f "$_classifier_lib" ]]; then
    # shellcheck source=./lib/classify-error.sh
    source "$_classifier_lib"
    _category="$(classify_error "$_output" "$_exit_code" "$RUNTIME_NAME")"
    printf '# LOOM_TERMINAL_RESULT v=1 provider=%s account=unknown category=%s exit_code=%s\n' \
        "$RUNTIME_NAME" "$_category" "$_exit_code" >&2
fi

exit "$_exit_code"
