#!/usr/bin/env bash
# resync-installed.sh - Refresh installed .loom/ runtime copies from defaults/ (#3777).
#
# The installed .loom/hooks/ and .loom/scripts/ trees are copied from the Loom
# source repo's defaults/ tree at install time. After a `git pull` that merges a
# fix to those files, the INSTALLED copies the harness actually executes are NOT
# automatically updated — so a repo can run stale hooks/scripts indefinitely
# (see #3777: the guard-precision trio #3755/#3756/#3757 merged to main, but the
# installed guard-destructive.sh kept its pre-fix behavior until hand-copied).
#
# This is the REMEDIATION half of the drift problem. #3770
# (check-main-freshness.sh) DETECTS the drift with a warning; this script FIXES
# it. The intended flow is: "freshness warning says you're stale -> run resync."
#
# It is idempotent (a no-op when already in sync), reports per-file
# updated/unchanged/skipped, only ever touches files that exist in defaults/
# (repo-specific hooks with no defaults/ counterpart are left alone), and
# supports --dry-run.
#
# Local-override convention: list a relative path (e.g. `hooks/guard-destructive.sh`
# or `scripts/foo.sh`) — one per line — in `.loom/resync-ignore` to pin an
# intentional per-repo customization. Matching files are reported as `skipped`
# and never overwritten. Blank lines and `#` comments are ignored.
#
# Usage:
#   ./.loom/scripts/resync-installed.sh            # sync; report what changed
#   ./.loom/scripts/resync-installed.sh --dry-run  # preview only; make no changes
#   ./.loom/scripts/resync-installed.sh --quiet    # only report updated/skipped
#   ./.loom/scripts/resync-installed.sh --help     # show usage
#
# Exit codes:
#   0 - Success. Sync applied (or already in sync); or --dry-run found no drift.
#   1 - Error (not in a git repo, or the defaults/ source could not be located).
#   2 - --dry-run only: drift detected (one or more files WOULD be updated).
#       Lets callers (e.g. the #3770 warning) use --dry-run as a cheap check.
#
# See also: check-main-freshness.sh (#3770) — the advisory that suggests this.

set -uo pipefail

# ---------- output helpers ----------

if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    BOLD=''
    NC=''
fi

DRY_RUN=0
QUIET=0

err()  { printf '%b\n' "${RED}ERROR: $*${NC}" >&2; }
info() { printf '%b\n' "${BLUE}$*${NC}"; }
note() { [[ "$QUIET" -eq 1 ]] || printf '%b\n' "$*"; }

# ---------- args ----------

for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY_RUN=1 ;;
        --quiet|-q)   QUIET=1 ;;
        --help|-h)
            sed -n '2,37p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            err "Unknown argument: $arg (try --help)"
            exit 1
            ;;
    esac
done

# ---------- resolve the installed repo root (worktree-safe) ----------

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    err "Not inside a git repository."
    exit 1
fi

# git-common-dir points at the MAIN checkout's .git even from a linked worktree,
# so installed .loom/ is always resolved against the primary worktree — never a
# transient issue worktree.
REPO_ROOT=""
COMMON_DIR="$(git rev-parse --git-common-dir 2>/dev/null || true)"
if [[ -n "$COMMON_DIR" ]]; then
    case "$COMMON_DIR" in
        */.git) REPO_ROOT="${COMMON_DIR%/.git}" ;;
    esac
fi
if [[ -z "$REPO_ROOT" ]]; then
    REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
fi
if [[ -z "$REPO_ROOT" || ! -d "$REPO_ROOT/.loom" ]]; then
    err "Could not resolve the installed repo root (no .loom/ found)."
    exit 1
fi

INSTALLED_HOOKS="$REPO_ROOT/.loom/hooks"
INSTALLED_SCRIPTS="$REPO_ROOT/.loom/scripts"

# ---------- resolve the defaults/ source tree ----------
#
# Mirrors lib/loom-tools.sh find_loom_tools() resolution order:
#   1. Loom source repo (dogfood): $REPO_ROOT/defaults/
#   2. Recorded loom-source-path (target repo install)
#   3. install-metadata.json "loom_source"

DEFAULTS_DIR=""
resolve_defaults() {
    if [[ -d "$REPO_ROOT/defaults/hooks" || -d "$REPO_ROOT/defaults/scripts" ]]; then
        DEFAULTS_DIR="$REPO_ROOT/defaults"
        return 0
    fi
    if [[ -f "$REPO_ROOT/.loom/loom-source-path" ]]; then
        local src
        src="$(cat "$REPO_ROOT/.loom/loom-source-path" 2>/dev/null || true)"
        if [[ -n "$src" && -d "$src/defaults" ]]; then
            DEFAULTS_DIR="$src/defaults"
            return 0
        fi
    fi
    if [[ -f "$REPO_ROOT/.loom/install-metadata.json" ]]; then
        local src
        src="$(sed -n 's/.*"loom_source" *: *"\(.*\)".*/\1/p' "$REPO_ROOT/.loom/install-metadata.json" 2>/dev/null | head -1)"
        if [[ -n "$src" && -d "$src/defaults" ]]; then
            DEFAULTS_DIR="$src/defaults"
            return 0
        fi
    fi
    return 1
}

if ! resolve_defaults; then
    err "Could not locate a defaults/ source tree to sync from."
    err "Looked in: \$REPO_ROOT/defaults, .loom/loom-source-path, .loom/install-metadata.json."
    err "Re-run the Loom installer, or set .loom/loom-source-path to the Loom source repo."
    exit 1
fi

# ---------- local-override ignore list ----------

IGNORE_FILE="$REPO_ROOT/.loom/resync-ignore"
is_ignored() {
    # $1 = relative path like "hooks/foo.sh" or "scripts/bar.sh"
    [[ -f "$IGNORE_FILE" ]] || return 1
    local rel="$1" line
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"                       # strip trailing comment
        line="${line#"${line%%[![:space:]]*}"}"  # ltrim
        line="${line%"${line##*[![:space:]]}"}"   # rtrim
        [[ -z "$line" ]] && continue
        [[ "$line" == "$rel" ]] && return 0
    done < "$IGNORE_FILE"
    return 1
}

# ---------- counters ----------

N_UPDATED=0
N_UNCHANGED=0
N_SKIPPED=0

# ---------- per-file sync ----------
#
# sync_one <src_file> <dst_file> <rel_label>
#   Copies src -> dst when they differ (unless --dry-run), preserving the
#   installed file's executable bit expectation. Only files that exist in
#   defaults/ ever reach this function, so repo-specific installed files with no
#   defaults/ counterpart are never touched.
sync_one() {
    local src="$1" dst="$2" rel="$3"

    if is_ignored "$rel"; then
        note "  ${YELLOW}skipped${NC}   $rel ${YELLOW}(pinned in .loom/resync-ignore)${NC}"
        N_SKIPPED=$((N_SKIPPED + 1))
        return 0
    fi

    if [[ -f "$dst" ]] && cmp -s "$src" "$dst" 2>/dev/null; then
        note "  ${GREEN}unchanged${NC} $rel"
        N_UNCHANGED=$((N_UNCHANGED + 1))
        return 0
    fi

    # src and dst differ (or dst is missing) — this is an update.
    N_UPDATED=$((N_UPDATED + 1))
    local verb_past="updated" verb_pres="update"
    if [[ ! -f "$dst" ]]; then
        verb_past="created"
        verb_pres="create"
    fi

    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf '%b\n' "  ${BOLD}would ${verb_pres}${NC} $rel"
        return 0
    fi

    mkdir -p "$(dirname "$dst")"
    if cp "$src" "$dst" 2>/dev/null; then
        # Match the executable bit of the source (defaults/ scripts/hooks are +x).
        if [[ -x "$src" ]]; then
            chmod +x "$dst" 2>/dev/null || true
        fi
        printf '%b\n' "  ${GREEN}${verb_past}${NC}   $rel"
    else
        err "failed to copy $rel"
        return 1
    fi
}

# ---------- walk hooks (top-level *.sh, matching the installer) ----------

if [[ -d "$DEFAULTS_DIR/hooks" && -d "$INSTALLED_HOOKS" ]]; then
    info "Resyncing .loom/hooks/ from ${DEFAULTS_DIR#"$REPO_ROOT/"}/hooks/ ..."
    shopt -s nullglob
    for src in "$DEFAULTS_DIR/hooks/"*.sh; do
        name="$(basename "$src")"
        sync_one "$src" "$INSTALLED_HOOKS/$name" "hooks/$name"
    done
    shopt -u nullglob
fi

# ---------- walk scripts (recursive, matching the installer's verify walk) ----------

if [[ -d "$DEFAULTS_DIR/scripts" && -d "$INSTALLED_SCRIPTS" ]]; then
    info "Resyncing .loom/scripts/ from ${DEFAULTS_DIR#"$REPO_ROOT/"}/scripts/ ..."
    while IFS= read -r -d '' src; do
        rel="${src#"$DEFAULTS_DIR/scripts/"}"
        sync_one "$src" "$INSTALLED_SCRIPTS/$rel" "scripts/$rel"
    done < <(find "$DEFAULTS_DIR/scripts" -type f -print0 | sort -z)
fi

# ---------- summary ----------

echo ""
if [[ "$DRY_RUN" -eq 1 ]]; then
    if [[ "$N_UPDATED" -gt 0 ]]; then
        printf '%b\n' "${YELLOW}${BOLD}[resync] DRY RUN: ${N_UPDATED} file(s) would be updated, ${N_UNCHANGED} unchanged, ${N_SKIPPED} skipped.${NC}"
        printf '%b\n' "${YELLOW}Run without --dry-run to apply.${NC}"
        exit 2
    fi
    printf '%b\n' "${GREEN}[resync] DRY RUN: already in sync (${N_UNCHANGED} unchanged, ${N_SKIPPED} skipped).${NC}"
    exit 0
fi

if [[ "$N_UPDATED" -gt 0 ]]; then
    printf '%b\n' "${GREEN}${BOLD}[resync] ${N_UPDATED} file(s) updated, ${N_UNCHANGED} unchanged, ${N_SKIPPED} skipped.${NC}"
else
    printf '%b\n' "${GREEN}[resync] Already in sync (${N_UNCHANGED} unchanged, ${N_SKIPPED} skipped).${NC}"
fi
exit 0
