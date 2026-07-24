#!/usr/bin/env bash
# check-main-freshness.sh - Warn when local default branch is behind origin.
#
# This is a NON-BLOCKING, advisory check. During a long-running /loom:sweep
# session, other PRs can merge to origin's default branch — and because the
# installed .loom/scripts/ and .loom/hooks/ copies are synced from defaults/
# at install time, a local default branch that has drifted behind origin means
# the session may be executing STALE orchestration scripts that silently lack
# recently-merged logic (see #3770 for the incident: worktree.sh --base (#3742)
# and merge-pr.sh auto-reconcile (#3752) were absent from the copies the session
# was actually running, even though both had merged to origin/main).
#
# It is invoked at the start of /loom:sweep, alongside check-host-sleep.sh
# (#3350). It MUST NOT block — even if git / the network fails, it returns 0 and
# orchestration proceeds. It NEVER auto-pulls, merges, or resets — read-only.
#
# Usage:
#   ./.loom/scripts/check-main-freshness.sh         # print warning (or nothing) and exit 0
#   ./.loom/scripts/check-main-freshness.sh --quiet # suppress the stdout one-liner
#   ./.loom/scripts/check-main-freshness.sh --help  # show usage
#
# Exit codes:
#   0 - Always. This script is advisory; it never blocks Loom.
#
# See also: check-host-sleep.sh (#3350) — the sibling pre-flight advisory this
# script mirrors in structure and contract.

set -uo pipefail  # NOTE: no -e — this script must never exit non-zero

# ---------- source the default-branch helper ----------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null || echo .)"
# shellcheck source=lib/default-branch.sh
if [[ -r "$SCRIPT_DIR/lib/default-branch.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/lib/default-branch.sh" 2>/dev/null || true
fi

# ---------- output helpers ----------

# Colors (only when stderr is a tty)
if [[ -t 2 ]]; then
    YELLOW='\033[1;33m'
    GREEN='\033[1;32m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    YELLOW=''
    GREEN=''
    BOLD=''
    NC=''
fi

QUIET=0
for arg in "$@"; do
    case "$arg" in
        --quiet|-q)
            QUIET=1
            ;;
        --help|-h)
            sed -n '2,27p' "$0" | sed 's/^# //; s/^#//'
            exit 0
            ;;
        *)
            # Unknown args are ignored — this script must never fail.
            ;;
    esac
done

warn() {
    # Print a multi-line warning block to stderr. Always returns 0.
    printf '%b\n' "$*" >&2 || true
}

info_oneliner() {
    # Print a single status line to stdout (suppressed by --quiet).
    if [[ "$QUIET" -eq 0 ]]; then
        printf '%b\n' "$*" || true
    fi
}

# ---------- pre-flight: must be inside a git repo ----------

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    info_oneliner "${YELLOW}[freshness-check] not inside a git repository; skipping.${NC}"
    exit 0
fi

# ---------- resolve default branch ----------

BRANCH=""
if declare -F loom_default_branch >/dev/null 2>&1; then
    BRANCH="$(loom_default_branch origin 2>/dev/null || true)"
fi

if [[ -z "$BRANCH" ]]; then
    info_oneliner "${YELLOW}[freshness-check] could not determine the default branch; skipping.${NC}"
    exit 0
fi

REMOTE_REF="origin/$BRANCH"

# ---------- bounded fetch (degrade gracefully) ----------

# Try to refresh origin's view of the default branch, bounded so a hung network
# can't stall the sweep. On any failure (offline, auth, rate-limit, no `timeout`
# binary) we fall back to whatever refs/remotes/origin/<branch> is already known
# locally — possibly stale, but the check stays cheap and never blocks.
if command -v timeout >/dev/null 2>&1; then
    timeout 5 git fetch origin "$BRANCH" --quiet >/dev/null 2>&1 || true
else
    # No `timeout` available (e.g. minimal macOS without coreutils). Still try,
    # but git's own --quiet keeps it unobtrusive; a hung network is a rare edge.
    git fetch origin "$BRANCH" --quiet >/dev/null 2>&1 || true
fi

# ---------- verify we have both refs to compare ----------

if ! git show-ref --verify --quiet "refs/heads/$BRANCH" 2>/dev/null; then
    # No local default branch (e.g. detached checkout of a worktree). Nothing to
    # compare against — skip silently-ish.
    info_oneliner "${YELLOW}[freshness-check] no local '${BRANCH}' branch to compare; skipping.${NC}"
    exit 0
fi

if ! git show-ref --verify --quiet "refs/remotes/$REMOTE_REF" 2>/dev/null; then
    info_oneliner "${YELLOW}[freshness-check] no '${REMOTE_REF}' ref known locally; skipping (offline?).${NC}"
    exit 0
fi

# ---------- compute how far behind ----------

N="$(git rev-list --count "${BRANCH}..${REMOTE_REF}" 2>/dev/null || echo 0)"
if ! [[ "$N" =~ ^[0-9]+$ ]]; then
    N=0
fi

if [[ "$N" -eq 0 ]]; then
    info_oneliner "${GREEN}[freshness-check] local ${BRANCH} is up to date with ${REMOTE_REF}.${NC}"
    exit 0
fi

# ---------- N > 0: warn (non-blocking) ----------

warn ""
warn "${YELLOW}${BOLD}========================================================================${NC}"
warn "${YELLOW}${BOLD}  WARNING: local ${BRANCH} is behind ${REMOTE_REF} (#3770)${NC}"
warn "${YELLOW}${BOLD}========================================================================${NC}"
warn "${YELLOW}Local ${BRANCH} is ${N} commit(s) behind ${REMOTE_REF}.${NC}"
warn "${YELLOW}The installed .loom/scripts/ and .loom/hooks/ copies are synced from${NC}"
warn "${YELLOW}defaults/ at install time, so this session may be executing STALE${NC}"
warn "${YELLOW}orchestration scripts that silently lack recently-merged logic.${NC}"
warn ""
warn "${BOLD}Remediation (read-only advisory — this script never pulls for you):${NC}"
warn "      ${BOLD}git merge --ff-only ${REMOTE_REF}${NC}"
warn "  then refresh the installed .loom/ copies from defaults/ (#3777):"
warn "      ${BOLD}./.loom/scripts/resync-installed.sh${NC}"
warn "  (preview first with ${BOLD}--dry-run${NC}; it only touches files present in defaults/)."
warn ""

# ---------- stretch goal: best-effort installed-vs-defaults drift note ----------
#
# When N > 0, additionally flag files present in BOTH .loom/scripts (installed)
# and defaults/scripts whose content differs. Best-effort: if either tree can't
# be resolved, skip silently. We only flag content differences for files present
# in both trees — never "only on one side" (repo-specific hooks like
# guard-worktree-paths.sh have no defaults/ counterpart and are not drift).
report_tree_drift() {
    local installed_dir="$1" defaults_dir="$2" label="$3"
    [[ -d "$installed_dir" && -d "$defaults_dir" ]] || return 0

    local f name
    for f in "$installed_dir"/*; do
        [[ -f "$f" ]] || continue
        name="$(basename "$f")"
        if [[ -f "$defaults_dir/$name" ]]; then
            if ! cmp -s "$f" "$defaults_dir/$name" 2>/dev/null; then
                warn "${YELLOW}  installed ${label}/${name} differs from defaults/${label}/${name}${NC}"
            fi
        fi
    done
}

# Resolve the repo root so we can find both trees regardless of cwd. Prefer the
# common dir (worktree-safe); fall back to toplevel.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
COMMON_DIR="$(git rev-parse --git-common-dir 2>/dev/null || true)"
if [[ -n "$COMMON_DIR" ]]; then
    # git-common-dir points at the main checkout's .git; its parent is the main
    # worktree root, where installed .loom/ and source defaults/ both live.
    case "$COMMON_DIR" in
        */.git) REPO_ROOT="${COMMON_DIR%/.git}" ;;
    esac
fi

if [[ -n "$REPO_ROOT" ]]; then
    drift_found=0
    if [[ -d "$REPO_ROOT/.loom/scripts" && -d "$REPO_ROOT/defaults/scripts" ]]; then
        drift_found=1
    fi
    if [[ -d "$REPO_ROOT/.loom/hooks" && -d "$REPO_ROOT/defaults/hooks" ]]; then
        drift_found=1
    fi
    if [[ "$drift_found" -eq 1 ]]; then
        warn "${YELLOW}Installed-copy drift check (files present in both trees):${NC}"
        report_tree_drift "$REPO_ROOT/.loom/scripts" "$REPO_ROOT/defaults/scripts" "scripts"
        report_tree_drift "$REPO_ROOT/.loom/hooks" "$REPO_ROOT/defaults/hooks" "hooks"
        warn ""
    fi
fi

warn "${YELLOW}========================================================================${NC}"
warn ""

info_oneliner "${YELLOW}[freshness-check] WARNING: local ${BRANCH} is ${N} commit(s) behind ${REMOTE_REF}. See stderr for details.${NC}"

# Always succeed — this script is advisory only.
exit 0
