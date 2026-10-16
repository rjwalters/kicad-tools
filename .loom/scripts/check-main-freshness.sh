#!/usr/bin/env bash
# check-main-freshness.sh - Warn when local default branch is behind OR ahead
# of origin.
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
# Ahead is the more dangerous direction (#5182): worktree.sh sets
# BASE_REF="origin/$DEFAULT_BRANCH", so every builder worktree branches off
# origin, not local. Unpushed local commits are invisible to every builder
# dispatched this session — a silent, wave-wide correctness gap, not just
# stale tooling.
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
            sed -n '2,33p' "$0" | sed 's/^# //; s/^#//'
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

# (#9106: loom_default_branch refuses an unsafe name at the source and returns
# non-zero, which leaves $BRANCH empty — so the "could not determine the default
# branch; skipping" arm above is also the unsafe-name arm, and this advisory
# script still never exits non-zero. The `--` below is the second mitigation.)

REMOTE_REF="origin/$BRANCH"

# ---------- bounded fetch (degrade gracefully) ----------

# Try to refresh origin's view of the default branch, bounded so a hung network
# can't stall the sweep. On any failure (offline, auth, rate-limit, no `timeout`
# binary) we fall back to whatever refs/remotes/origin/<branch> is already known
# locally — possibly stale, but the check stays cheap and never blocks.
# No `timeout` available (e.g. minimal macOS without coreutils)? Still fetch —
# git's own --quiet keeps it unobtrusive and a hung network is a rare edge — so
# the bound is a command PREFIX rather than a second copy of the same fetch.
# `${a[@]+"${a[@]}"}` not `"${a[@]}"`: this script runs under `set -u`, and on
# bash 3.2 (the macOS default) expanding an EMPTY array that way is an "unbound
# variable" error — the recurring 3.2 portability class in
# `.loom/docs/shell-language-policy.md`. The `+` form expands to nothing when
# the array is unset/empty and to the quoted elements otherwise.
_CMF_BOUND=(); command -v timeout >/dev/null 2>&1 && _CMF_BOUND=(timeout 5)
${_CMF_BOUND[@]+"${_CMF_BOUND[@]}"} git fetch origin --quiet -- "$BRANCH" >/dev/null 2>&1 || true

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

# ---------- compute how far behind AND how far ahead ----------

N="$(git rev-list --count "${BRANCH}..${REMOTE_REF}" 2>/dev/null || echo 0)"
if ! [[ "$N" =~ ^[0-9]+$ ]]; then
    N=0
fi

# Ahead is the dangerous direction (#5182): worktree.sh sets
# BASE_REF="origin/$DEFAULT_BRANCH", so every builder worktree branches off
# REMOTE_REF, not local BRANCH. Unpushed local commits are simply absent from
# every builder's base — silently, with no error anywhere in the pipeline.
A="$(git rev-list --count "${REMOTE_REF}..${BRANCH}" 2>/dev/null || echo 0)"
if ! [[ "$A" =~ ^[0-9]+$ ]]; then
    A=0
fi

# ---------- installed-copy drift check (files present in both trees) ----------
#
# Compares the INSTALLED surfaces (.loom/scripts, .loom/hooks,
# .claude/commands/loom, .loom/docs) against the LOCAL defaults/ tree they
# were copied from — independent of whether local ${BRANCH} is behind/ahead
# of ${REMOTE_REF}. This is deliberately computed unconditionally, not just
# when N > 0 (#5874): a defaults/ change that has already merged to local
# ${BRANCH} (so N == 0, "up to date") but was never propagated by a resync
# is invisible to the behind/ahead comparison above entirely — that was the
# exact blind spot behind #5846 shipping five role-prompt edits + a doc
# change with installed copies never refreshed, while every version-based
# currency check kept reporting the fleet as current. Only files present in
# BOTH trees are compared — never "only on one side" (repo-specific hooks
# like post-worktree.sh have no defaults/ counterpart and are not drift; as
# of #4007 guard-worktree-paths.sh DOES have one and is drift-checked like
# any other hook). Also covers .claude/commands/loom/ (role prompts) and
# .loom/docs/ (the two installed-surface classes #5846 touched that scripts/
# and hooks/ alone never would have caught).
report_tree_drift() {
    local installed_dir="$1" defaults_dir="$2" label="$3"
    [[ -d "$installed_dir" && -d "$defaults_dir" ]] || return 0

    local f name
    for f in "$installed_dir"/*; do
        [[ -f "$f" ]] || continue
        name="$(basename "$f")"
        if [[ -f "$defaults_dir/$name" ]]; then
            if ! cmp -s "$f" "$defaults_dir/$name" 2>/dev/null; then
                printf '%b\n' "${YELLOW}  installed ${label}/${name} differs from defaults/${label}/${name}${NC}"
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

DRIFT_LINES=""
if [[ -n "$REPO_ROOT" ]]; then
    DRIFT_LINES="$(
        report_tree_drift "$REPO_ROOT/.loom/scripts" "$REPO_ROOT/defaults/scripts" "scripts"
        report_tree_drift "$REPO_ROOT/.loom/hooks" "$REPO_ROOT/defaults/hooks" "hooks"
        report_tree_drift "$REPO_ROOT/.claude/commands/loom" "$REPO_ROOT/defaults/.claude/commands/loom" "roles"
        report_tree_drift "$REPO_ROOT/.loom/docs" "$REPO_ROOT/defaults/docs" "docs"
    )"
fi

# ---------- resync precondition check (#6202) ----------
#
# Every remediation this script prints for "stale installed surfaces" tells
# the reader to run `resync-installed.sh`. That script needs a defaults/
# SOURCE tree to sync from, resolved (in priority order) from:
#   1. $REPO_ROOT/defaults/{hooks,scripts}   (this checkout IS the Loom source repo)
#   2. .loom/loom-source-path                (a sidecar written by install.sh /
#                                              install-loom.sh; gitignored by design
#                                              — it holds a machine-local path)
#   3. install-metadata.json's "loom_source" (legacy compat; #5624 stopped writing
#                                              this field, so it is dead for any
#                                              post-#5624 install)
#
# None of these exist on a checkout that never ran the Loom installer locally —
# a fresh developer clone, a CI checkout, or any machine that received the repo
# rather than installing into it. There `resync-installed.sh` hands off to
# `loom-daemon resync-payload` (the installed daemon's embedded payload, #8961)
# and fails with "Could not locate a defaults/ source tree to sync from" only
# when no daemon has that subcommand (#6202). Mirror resolve_defaults()'s
# order here (read-only, best-effort) so the reader knows which path is next.
#
# A candidate root is only usable if it has SOMETHING under defaults/ to sync
# from — `-d "$root/defaults"` alone is not sufficient (#6780): a sidecar or
# metadata path can point at a directory that still exists (unlike a fully
# vanished clone) but is stale, empty, or was never a real Loom checkout, e.g.
# a scratch clone emptied without removing the directory itself. Requiring a
# populated `defaults/hooks` or `defaults/scripts` mirrors the dogfood rung's
# own check and keeps this read-only mirror in lockstep with
# resolve_defaults() in resync-installed.sh — otherwise this precondition
# check can report "met" for a root that resync-installed.sh would actually
# resolve to a source tree with nothing under it, silently producing a false
# "already in sync" result instead of the loud failure an unresolvable source
# should produce.
resync_precondition_met() {
    local root="$1"
    [[ -n "$root" ]] || return 1
    if [[ -d "$root/defaults/hooks" || -d "$root/defaults/scripts" ]]; then
        return 0
    fi
    if [[ -f "$root/.loom/loom-source-path" ]]; then
        local src
        src="$(cat "$root/.loom/loom-source-path" 2>/dev/null || true)"
        [[ -n "$src" && ( -d "$src/defaults/hooks" || -d "$src/defaults/scripts" ) ]] && return 0
    fi
    if [[ -f "$root/.loom/install-metadata.json" ]]; then
        local src
        src="$(sed -n 's/.*"loom_source" *: *"\(.*\)".*/\1/p' "$root/.loom/install-metadata.json" 2>/dev/null | head -1)"
        [[ -n "$src" && ( -d "$src/defaults/hooks" || -d "$src/defaults/scripts" ) ]] && return 0
    fi
    return 1
}

RESYNC_PRECONDITION_MET=1
if [[ -n "$REPO_ROOT" ]] && ! resync_precondition_met "$REPO_ROOT"; then
    RESYNC_PRECONDITION_MET=0
fi

# Appended immediately after every "run resync-installed.sh" remediation line
# above, only when the precondition is actually missing.
warn_resync_precondition_gap() {
    [[ "$RESYNC_PRECONDITION_MET" -eq 0 ]] || return 0
    warn "${YELLOW}  NOTE (#6202, #8961): resync-installed.sh has no Loom source tree to${NC}"
    warn "${YELLOW}  sync from on this checkout: it is not the Loom source repo, and the${NC}"
    warn "${YELLOW}  gitignored .loom/loom-source-path sidecar (written by the installer,${NC}"
    warn "${YELLOW}  so it never arrives with a plain \`git clone\`) names no usable clone.${NC}"
    warn "${YELLOW}  It will resync from the payload embedded in the installed loom-daemon${NC}"
    warn "${YELLOW}  instead (\`loom-daemon resync-payload\`: official release builds only,${NC}"
    warn "${YELLOW}  never a downgrade). With no loom-daemon that has that subcommand it${NC}"
    warn "${YELLOW}  fails with 'Could not locate a defaults/ source tree to sync from'.${NC}"
    warn "${YELLOW}  Fix for that case: install or update loom-daemon, or clone${NC}"
    warn "${YELLOW}  https://github.com/rjwalters/loom and point the sidecar at it:${NC}"
    warn "${YELLOW}      echo /path/to/local/loom-clone > .loom/loom-source-path${NC}"
}

if [[ "$N" -eq 0 && "$A" -eq 0 ]]; then
    if [[ -z "$DRIFT_LINES" ]]; then
        info_oneliner "${GREEN}[freshness-check] local ${BRANCH} is up to date with ${REMOTE_REF}.${NC}"
        exit 0
    fi

    # Local matches origin, but the installed surfaces have drifted from local
    # defaults/ (#5874) — the version-comparison blind spot this check exists
    # to close. See the report_tree_drift() comment above for why this is
    # checked regardless of N/A.
    warn ""
    warn "${YELLOW}${BOLD}========================================================================${NC}"
    warn "${YELLOW}${BOLD}  WARNING: installed surfaces are behind local defaults/ (#5874)${NC}"
    warn "${YELLOW}${BOLD}========================================================================${NC}"
    warn "${YELLOW}Local ${BRANCH} matches ${REMOTE_REF} — but the installed .loom/ and${NC}"
    warn "${YELLOW}.claude/commands/loom/ copies differ from local defaults/. A merged${NC}"
    warn "${YELLOW}defaults/ change (e.g. a role-prompt edit) was never propagated by a${NC}"
    warn "${YELLOW}resync, so this session may be executing STALE instructions even though${NC}"
    warn "${YELLOW}git itself reports everything current.${NC}"
    warn ""
    warn "${BOLD}Remediation (read-only advisory — this script never resyncs for you):${NC}"
    warn "      ${BOLD}./.loom/scripts/resync-installed.sh${NC}"
    warn "  (preview first with ${BOLD}--dry-run${NC}; it only touches files present in defaults/)."
    warn_resync_precondition_gap
    warn ""
    warn "${YELLOW}Installed-copy drift (files present in both trees):${NC}"
    warn "$DRIFT_LINES"
    warn ""
    warn "${YELLOW}========================================================================${NC}"
    warn ""
    info_oneliner "${YELLOW}[freshness-check] WARNING: installed surfaces differ from local defaults/ even though ${BRANCH} matches ${REMOTE_REF}. See stderr for details.${NC}"
    exit 0
fi

# ---------- behind and/or ahead: warn (non-blocking) ----------

if [[ "$N" -gt 0 ]]; then
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
    warn_resync_precondition_gap
    warn ""
fi

if [[ "$A" -gt 0 ]]; then
    warn ""
    warn "${YELLOW}${BOLD}========================================================================${NC}"
    warn "${YELLOW}${BOLD}  WARNING: local ${BRANCH} is ahead of ${REMOTE_REF} (#5182)${NC}"
    warn "${YELLOW}${BOLD}========================================================================${NC}"
    warn "${YELLOW}Local ${BRANCH} is ${A} commit(s) ahead of ${REMOTE_REF} — unpushed.${NC}"
    warn "${YELLOW}worktree.sh sets BASE_REF=\"origin/\$DEFAULT_BRANCH\", so every builder${NC}"
    warn "${YELLOW}worktree branches off ${REMOTE_REF}, NOT local ${BRANCH}. These unpushed${NC}"
    warn "${YELLOW}commits are invisible to every builder dispatched this session — a${NC}"
    warn "${YELLOW}silent, wave-wide correctness gap, not just stale tooling.${NC}"
    warn ""
    warn "${BOLD}Remediation (read-only advisory — this script never pushes for you):${NC}"
    warn "      ${BOLD}git push origin ${BRANCH}${NC}"
    warn ""
fi

# ---------- installed-vs-defaults drift note (reuses DRIFT_LINES above) ----------
#
# DRIFT_LINES was already computed unconditionally, before the N==0 && A==0
# early-return above, so it is simply reported here rather than recomputed.
if [[ -n "$DRIFT_LINES" ]]; then
    warn "${YELLOW}Installed-copy drift check (files present in both trees):${NC}"
    warn "$DRIFT_LINES"
    warn ""
fi

warn "${YELLOW}========================================================================${NC}"
warn ""

if [[ "$N" -gt 0 && "$A" -gt 0 ]]; then
    info_oneliner "${YELLOW}[freshness-check] WARNING: local ${BRANCH} has DIVERGED from ${REMOTE_REF} (${N} behind, ${A} ahead/unpushed). See stderr for details.${NC}"
elif [[ "$N" -gt 0 ]]; then
    info_oneliner "${YELLOW}[freshness-check] WARNING: local ${BRANCH} is ${N} commit(s) behind ${REMOTE_REF}. See stderr for details.${NC}"
else
    info_oneliner "${YELLOW}[freshness-check] WARNING: local ${BRANCH} is ${A} commit(s) ahead of ${REMOTE_REF} (unpushed). See stderr for details.${NC}"
fi

# Always succeed — this script is advisory only.
exit 0
