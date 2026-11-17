#!/usr/bin/env bash
# check-runtime-capabilities.sh — Match a role's runtimeRequirements against a
# runtime's capability manifest (epic #4167, design pillar 2; issue #4170).
#
# This is a STANDALONE checker. It is deliberately NOT wired into
# spawn-worker.sh or any dispatch path -- enforcement is a follow-up decision
# (see defaults/docs/runtime-adapters.md §7 "Capability declaration"). It also
# does NOT read `.loom/config.json`'s `runtimes` block itself; the caller
# resolves the runtime name and passes it via `--runtime`.
#
# Schema (tri-state capability values -- yes | no | partial):
#
#   Runtime manifest (defaults/runtimes/<name>.json):
#     { "runtime": "claude", "capabilities": { "mcp": "yes", ... } }
#
#   Role sidecar (defaults/roles/<name>.json), optional key:
#     { ..., "runtimeRequirements": ["worktreeIsolation", "mcp"] }
#
# A requirement is satisfied only when the runtime declares that capability
# as exactly "yes". A declared "partial" fails closed (the mismatch message
# names the capability and its declared value, same as "no" / missing).
#
# Exit codes:
#   0  - all requirements satisfied (including the no-requirements case)
#   78 - EX_CONFIG: one or more requirements unmet (capability mismatch)
#   1  - unknown/missing role or runtime file, or other usage error (a
#        message DISTINCT from the mismatch message, so callers/tests can
#        tell "misconfigured" apart from "mismatched")
#
# Usage:
#   check-runtime-capabilities.sh --role <name> --runtime <name>
#   check-runtime-capabilities.sh --role <name> --runtime <name> \
#       [--role-file <path>] [--runtime-file <path>] [--dir <defaults-root>] \
#       [--json]
#
# --json (issue #4494) adds a stable, machine-readable single-line JSON object
# on STDOUT at every terminal decision, so a caller (notably the daemon's
# native-vs-shell conformance test) can compare the exact unmet-capability SET,
# not just the exit code:
#
#   {"role":"builder","runtime":"codex","decision":"reject",
#    "unmet":["worktreeIsolation"],"reason":"..."}
#
# `decision` is one of admit | reject | error, mapping 1:1 onto exit 0 | 78 | 1.
# The human-readable log lines (stderr) and every exit code are UNCHANGED —
# --json is purely additive, and semantics are untouched.
#
# Resolution (when --role-file / --runtime-file are not given):
#   <dir>/roles/<role>.json      (default <dir>: repo .loom/ falling back to
#   <dir>/runtimes/<runtime>.json defaults/, see resolve_dir() below)

set -euo pipefail

RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[check-runtime-capabilities]${NC} $*" >&2; }
log_warn() { echo -e "${YELLOW}[check-runtime-capabilities] WARN${NC} $*" >&2; }
log_error() { echo -e "${RED}[check-runtime-capabilities] ERROR${NC} $*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'EOF'
Usage: check-runtime-capabilities.sh --role <name> --runtime <name> [options]

Required:
  --role <name>          Role name (sidecar file <dir>/roles/<name>.json)
  --runtime <name>       Runtime name (manifest <dir>/runtimes/<name>.json)

Options:
  --role-file <path>     Explicit path to the role sidecar JSON (overrides
                          role-name resolution)
  --runtime-file <path>  Explicit path to the runtime manifest JSON (overrides
                          runtime-name resolution)
  --dir <path>           Root directory containing roles/ and runtimes/
                          subdirectories (default: repo .loom/, falling back
                          to defaults/ next to this script)
  --json                 Also print a stable single-line JSON decision object
                          on stdout: {role, runtime, decision, unmet, reason}
                          where decision is admit|reject|error (#4494)
  -h, --help             Show this help

Exit codes:
  0   All requirements satisfied (or role declares no runtimeRequirements)
  78  Capability mismatch (EX_CONFIG) -- requirement not met
  1   Usage error, or unknown/missing role or runtime file
EOF
}

ROLE=""
RUNTIME=""
ROLE_FILE=""
RUNTIME_FILE=""
DIR_OVERRIDE=""
JSON_OUTPUT=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --role)
            ROLE="${2:-}"
            shift 2
            ;;
        --runtime)
            RUNTIME="${2:-}"
            shift 2
            ;;
        --role-file)
            ROLE_FILE="${2:-}"
            shift 2
            ;;
        --runtime-file)
            RUNTIME_FILE="${2:-}"
            shift 2
            ;;
        --dir)
            DIR_OVERRIDE="${2:-}"
            shift 2
            ;;
        --json)
            JSON_OUTPUT=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            log_error "Unknown argument: $1"
            usage >&2
            exit 1
            ;;
    esac
done

if [[ -z "$ROLE" || -z "$RUNTIME" ]]; then
    log_error "Both --role and --runtime are required."
    usage >&2
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    log_error "jq is required but not found on PATH."
    exit 1
fi

# --- Machine-readable decision output (#4494, opt-in via --json) ------------
# emit_decision <decision> <reason> [unmet...]
#
# Prints ONE line of JSON on stdout when --json is set, and nothing otherwise,
# so the default output/exit contract is byte-for-byte unchanged. jq builds the
# object so role/runtime/reason values are escaped correctly.
emit_decision() {
    if [[ "$JSON_OUTPUT" != "1" ]]; then
        return 0
    fi
    local decision="$1" reason="$2"
    shift 2
    local unmet_json='[]'
    if [[ $# -gt 0 ]]; then
        unmet_json="$(printf '%s\n' "$@" | jq -R . | jq -sc .)"
    fi
    jq -nc \
        --arg role "$ROLE" \
        --arg runtime "$RUNTIME" \
        --arg decision "$decision" \
        --arg reason "$reason" \
        --argjson unmet "$unmet_json" \
        '{role: $role, runtime: $runtime, decision: $decision, unmet: $unmet, reason: $reason}'
}

# --- Resolve the roles/runtimes root directory ---
# Precedence: --dir override > repo .loom/ (if present) > defaults/ next to
# this script (so the check works standalone in this repo before install).
resolve_dir() {
    if [[ -n "$DIR_OVERRIDE" ]]; then
        printf '%s\n' "$DIR_OVERRIDE"
        return
    fi

    local git_common_dir repo_root
    if git_common_dir="$(git rev-parse --git-common-dir 2>/dev/null)"; then
        if [[ ! "$git_common_dir" = /* ]]; then
            git_common_dir="$(cd "$git_common_dir" && pwd)"
        fi
        repo_root="$(dirname "$git_common_dir")"
        if [[ -d "$repo_root/.loom/roles" && -d "$repo_root/.loom/runtimes" ]]; then
            printf '%s\n' "$repo_root/.loom"
            return
        fi
    fi

    # Fallback: defaults/ next to this script (defaults/scripts -> defaults/).
    printf '%s\n' "$(cd "$SCRIPT_DIR/.." && pwd)"
}

BASE_DIR="$(resolve_dir)"

if [[ -z "$ROLE_FILE" ]]; then
    ROLE_FILE="$BASE_DIR/roles/$ROLE.json"
fi
if [[ -z "$RUNTIME_FILE" ]]; then
    RUNTIME_FILE="$BASE_DIR/runtimes/$RUNTIME.json"
fi

# --- Load + validate files (distinct exit path from mismatch) ---
if [[ ! -f "$ROLE_FILE" ]]; then
    log_error "Unknown role: no sidecar file found at '$ROLE_FILE'."
    emit_decision error "unknown role: no sidecar file found at '$ROLE_FILE'"
    exit 1
fi
if ! jq -e . "$ROLE_FILE" >/dev/null 2>&1; then
    log_error "Role sidecar '$ROLE_FILE' is not valid JSON."
    emit_decision error "role sidecar '$ROLE_FILE' is not valid JSON"
    exit 1
fi

if [[ ! -f "$RUNTIME_FILE" ]]; then
    log_error "Unknown runtime: no manifest file found at '$RUNTIME_FILE'."
    emit_decision error "unknown runtime: no manifest file found at '$RUNTIME_FILE'"
    exit 1
fi
if ! jq -e . "$RUNTIME_FILE" >/dev/null 2>&1; then
    log_error "Runtime manifest '$RUNTIME_FILE' is not valid JSON."
    emit_decision error "runtime manifest '$RUNTIME_FILE' is not valid JSON"
    exit 1
fi

# --- No requirements declared -> trivially satisfied ---
has_requirements="$(jq -r 'has("runtimeRequirements")' "$ROLE_FILE")"
if [[ "$has_requirements" != "true" ]]; then
    log_info "Role '$ROLE' declares no runtimeRequirements -- any runtime is compatible."
    emit_decision admit "role '$ROLE' declares no runtimeRequirements"
    exit 0
fi

# --- Compute mismatches: required capability not declared exactly "yes" ---
mismatches=""
unmet=()
while IFS= read -r cap; do
    [[ -z "$cap" ]] && continue
    value="$(jq -r --arg cap "$cap" '.capabilities[$cap] // "no"' "$RUNTIME_FILE")"
    if [[ "$value" != "yes" ]]; then
        mismatches+="  - $cap: runtime '$RUNTIME' declares '$value' (requires 'yes')"$'\n'
        unmet+=("$cap")
    fi
done < <(jq -r '.runtimeRequirements[]?' "$ROLE_FILE")

if [[ -n "$mismatches" ]]; then
    log_error "Role '$ROLE' is not compatible with runtime '$RUNTIME':"
    printf '%s' "$mismatches" >&2
    emit_decision reject "unmet capabilities: $(
        IFS=,
        echo "${unmet[*]}"
    )" "${unmet[@]}"
    exit 78
fi

log_info "Role '$ROLE' is compatible with runtime '$RUNTIME' (all requirements met)."
emit_decision admit "role '$ROLE' is compatible with runtime '$RUNTIME'"
exit 0
