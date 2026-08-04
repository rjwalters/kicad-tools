#!/usr/bin/env bash
# resync-installed.sh - Refresh installed Loom surfaces from the recorded source (#3777, #4239).
#
# The installed Loom surfaces the harness actually executes/reads are copied from
# the Loom source repo's defaults/ tree at install time. After a `git pull` that
# merges a fix to those files, the INSTALLED copies are NOT automatically updated
# — so a repo can run stale hooks/scripts/roles/docs/commands indefinitely (see
# #3777: the guard-precision trio #3755/#3756/#3757 merged to main, but the
# installed guard-destructive.sh kept its pre-fix behavior until hand-copied).
#
# This is the REMEDIATION half of the drift problem. #3770
# (check-main-freshness.sh) DETECTS the drift with a warning; this script FIXES
# it. The intended flow is: "freshness warning says you're stale -> run resync."
#
# It is idempotent (a no-op when already in sync), reports per-file
# updated/created/unchanged/skipped, only ever touches files that exist in the
# source tree (repo-specific files with no source counterpart are left alone),
# never clobbers a symlinked install target, and supports --dry-run.
#
# Surfaces resynced (#4239 widened this from hooks+scripts to the full pure-copy
# surface map — note the asymmetric source->target mapping):
#   .loom/hooks/            <- defaults/hooks/            (top-level *.sh)
#   .loom/scripts/          <- defaults/scripts/          (recursive)
#   .loom/roles/            <- defaults/roles/            (recursive; SOURCE-side
#                                                          symlinks resolved to
#                                                          content, #5222 — all 17
#                                                          defaults/roles/*.md are
#                                                          symlinks to
#                                                          .claude/commands/loom/*.md)
#   .loom/docs/             <- defaults/docs/             (recursive; DESTINATION-side
#                                                          symlinks skipped, e.g. this
#                                                          dogfood repo's own
#                                                          .loom/docs/*.md)
#   .loom/runtimes/         <- defaults/runtimes/         (recursive; BACKFILLED if absent, #4688)
#   .loom/bin/              <- defaults/.loom/bin/        (recursive; live consumer CLI)
#   .claude/commands/loom/  <- defaults/.claude/commands/loom/ (recursive)
#
# It also applies one targeted field edit outside the pure-copy model (#4285):
# a root package.json whose "name" is exactly "loom-workspace" (the Loom
# installer's workspace-scaffolding stub, `defaults/package.json`) has its
# decoy "version" field deleted in place if present — this is a `jq
# 'del(.version)'` field edit, NOT a whole-file resync, so a consumer's
# customized "scripts" block in the stub is preserved. A consumer's OWN
# package.json (any other "name") is never touched.
#
# On a successful non-dry-run it also re-stamps loom_version, loom_commit, and a
# last_resync date into .loom/install-metadata.json (requires jq or python3;
# skipped with a warning if neither is present — the file sync still succeeds).
# It also ensures a `merge=ours` driver is wired up for that same file (#4528)
# — a machine-local stamp every host re-writes, guaranteeing merge conflicts
# between hosts otherwise — via a Loom-managed .gitattributes block plus local
# git config (never committed; runs every time so a pre-#4528 install
# self-heals on its next resync).
#
# It also refreshes the marker-delimited Loom-managed `.gitignore` block via the
# daemon's `update-gitignore` subcommand (#4280) — the ephemeral-pattern list is
# single-sourced in loom-daemon, so existing installs converge on newly-ignored
# runtime paths (e.g. .loom/sweep-checkpoint/, .loom/worktrees-local/) at resync
# time. A missing daemon binary is a loud warning, not a silent skip. Any path
# still untracked-and-unignored under .loom/ afterward is reported as an audit
# warning so the pattern list can be extended.
#
# In the loom source repo itself (tracked installed surfaces + a local
# defaults/ tree), a non-dry-run that updates a file leaves the tree dirty
# until that resync output is committed. If the only remaining dirt is resync
# output, the summary block prints the exact `git add … && git commit`
# command to run (#4332) — worth doing, since `main_health_gate.rs`'s
# dirty-tree check treats byte-identical installed-surface dirt as ignorable
# (never a reason to skip the gate) but does not commit on the operator's
# behalf.
#
# ATOMIC WRITES + DEFERRED SELF-UPDATE (#4669): every file is installed by
# staging a copy NEXT TO its destination and rename(2)-ing it into place, never
# by truncating and rewriting the destination in place. This matters most for
# THIS script: resync-installed.sh is itself a file under defaults/scripts/, so
# a resync copies it over the very path the running bash process is still
# reading from. The old in-place `cp` let bash resume reading a half-rewritten
# file at a now-meaningless byte offset — the reported `syntax error near
# unexpected token` mid-run, which aborted the run and left dozens of surfaces
# partially refreshed. A rename swaps the directory entry and leaves the
# already-open inode intact, so the running process keeps reading the exact
# bytes it started with. Belt-and-suspenders, the self-copy is also DEFERRED:
# it is applied only after every other surface has settled.
#
# A file that cannot be staged or renamed is counted as a FAILURE: the run
# still finishes the remaining files, then prints an explicit PARTIAL summary
# naming every failed path and exits 1. No file is ever left half-written
# (staging happens off to the side), so re-running after fixing the cause
# completes the refresh — a partial refresh is never silent.
#
# EXPLICITLY OUT OF SCOPE (never touched by resync — updated by other mechanisms):
#   .loom/config.json       - operator-owned; needs merge-semantics design
#   CLAUDE.md               - repo-customized at install; needs managed-section markers
#   AGENTS.md               - repo-customized at install; needs managed-section markers
#                             (same as CLAUDE.md; #4479). Its full-guide sibling
#                             .loom/AGENTS.md is regenerated by install scaffolding
#                             from defaults/.loom/AGENTS.md, not by resync — same
#                             posture as .loom/CLAUDE.md.
#   .github/labels.yml,     - covered by `gh label sync` + install-time workflow opt-ins
#     .github/workflows/*
#   loom-daemon binary      - owned by the #4055 self-update mechanism
#   .mcp.json               - vestigial post-#4230 (loom is user-scoped); setup-mcp.sh
#     is demoted to a bundle-rebuild/legacy-migration tool with a safehouse-only
#     residual emission role
#   install-metadata.json's install_date + installed_files - owned by the installer
#
# WORKTREE RESTRICTION (#4563): the installed .loom/ is ALWAYS resolved against
# the PRIMARY worktree (via `git rev-parse --git-common-dir`), so running this
# from a linked worktree — an issue/PR worktree under .loom/worktrees/ — writes
# to the MAIN checkout, not to the worktree you are standing in. A Builder that
# does so contaminates main mid-sweep (the 2026-07-30 incident: four installed
# paths written into main from a wave-2 builder's worktree and quarantined by
# check-main-clean.sh). The script therefore REFUSES to run when its own
# `--show-toplevel` differs from the resolved main-checkout root, and exits 1.
# Installed-copy propagation is the periodic resync commit's job: land the
# defaults/ change, then resync from the main checkout. An operator who really
# does mean "write the main checkout's installed copies from here" can pass
# --allow-worktree (or export LOOM_RESYNC_ALLOW_WORKTREE=1). Running from the
# main checkout itself — including any subdirectory of it — is unaffected.
#
# Local-override convention: list a relative path (e.g. `hooks/guard-destructive.sh`,
# `scripts/foo.sh`, `roles/custom-role.md`, `docs/notes.md`, `bin/loom`,
# `commands/loom/mine.md`, or `package.json` to pin the #4285 stub version edit)
# — one per line — in `.loom/resync-ignore` to pin an intentional per-repo
# customization. Matching files are reported as `skipped` and never overwritten.
# Blank lines and `#` comments are ignored.
#
# Usage:
#   ./.loom/scripts/resync-installed.sh            # sync; report what changed
#   ./.loom/scripts/resync-installed.sh --dry-run  # preview only; make no changes
#   ./.loom/scripts/resync-installed.sh --quiet    # only report updated/skipped
#   ./.loom/scripts/resync-installed.sh --allow-worktree
#                                                  # permit running from a linked
#                                                  # worktree (still writes the MAIN
#                                                  # checkout's installed copies)
#   ./.loom/scripts/resync-installed.sh --help     # show usage
#
# Environment:
#   LOOM_RESYNC_ALLOW_WORKTREE=1  - same as --allow-worktree (for non-interactive
#                                   callers), matching the LOOM_ALLOW_* override
#                                   convention used elsewhere in .loom/scripts.
#
# Exit codes:
#   0 - Success. Sync applied (or already in sync); or --dry-run found no drift.
#   1 - Error (not in a git repo, the source tree could not be located,
#       invoked from a linked worktree without --allow-worktree, or one or more
#       files could not be synced — see the PARTIAL summary block, #4669).
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
# #4563: refuse to run from a linked worktree unless explicitly overridden.
ALLOW_WORKTREE=0
[[ "${LOOM_RESYNC_ALLOW_WORKTREE:-}" == "1" ]] && ALLOW_WORKTREE=1

err()  { printf '%b\n' "${RED}ERROR: $*${NC}" >&2; }
warn() { printf '%b\n' "${YELLOW}WARN: $*${NC}" >&2; }
info() { printf '%b\n' "${BLUE}$*${NC}"; }
note() { [[ "$QUIET" -eq 1 ]] || printf '%b\n' "$*"; }

# ---------- args ----------

for arg in "$@"; do
    case "$arg" in
        --dry-run|-n)     DRY_RUN=1 ;;
        --quiet|-q)       QUIET=1 ;;
        --allow-worktree) ALLOW_WORKTREE=1 ;;
        --help|-h)
            # Print the whole leading comment block (line 2 through the last
            # consecutive `#` line). Derived, not a hard-coded line range — the
            # previous `sed -n '2,69p'` silently truncated the Usage/Exit-codes
            # sections as the header grew past it.
            awk 'NR==1 { next }
                 /^#/  { sub(/^# ?/, ""); print; next }
                 { exit }' "$0"
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

# ---------- refuse to run from a linked worktree (#4563) ----------
#
# The resolution above is the whole point of the refusal: from an issue/PR
# worktree it hands back the MAIN checkout, so every write below lands in main
# rather than in the worktree the caller is standing in. That is a
# worktree-isolation escape a Builder cannot see (nothing in its own `git
# status` changes) — it surfaces only as contamination of main.
#
# Detection is generic: compare this invocation's own worktree top against the
# resolved main-checkout root. No path pattern is hard-coded, so a repo that
# relocates its worktree root (worktree.root / lib/worktree-root.sh) is covered
# too. Both sides are normalized to physical absolute paths first, because
# `git rev-parse --git-common-dir` returns a RELATIVE path (e.g. "../../.git")
# from a subdirectory of the main checkout — a raw string compare there would
# refuse a perfectly legitimate run.

abs_path() {
    local p="$1"
    [[ -d "$p" ]] || { printf '%s' "$p"; return 0; }
    (cd "$p" 2>/dev/null && pwd -P) || printf '%s' "$p"
}

WORKTREE_TOP="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -n "$WORKTREE_TOP" && "$(abs_path "$WORKTREE_TOP")" != "$(abs_path "$REPO_ROOT")" ]]; then
    if [[ "$ALLOW_WORKTREE" -eq 1 ]]; then
        warn "Running from a linked worktree ($WORKTREE_TOP) — writes target the MAIN checkout at $REPO_ROOT (--allow-worktree)."
    else
        err "Refusing to run: invoked from a linked git worktree."
        err "  this worktree : $WORKTREE_TOP"
        err "  would write to: $REPO_ROOT  (the MAIN checkout — NOT this worktree)"
        printf '\n' >&2
        err "The installed .loom/ surfaces are always resolved against the primary"
        err "worktree, so a resync from here silently modifies the main checkout and"
        err "contaminates it mid-sweep (#4563)."
        printf '\n' >&2
        err "Installed-copy propagation is the periodic resync commit's job: commit your"
        err "defaults/ change, get it merged, then run this from the main checkout:"
        err "  cd $REPO_ROOT && ./.loom/scripts/resync-installed.sh"
        printf '\n' >&2
        err "If you genuinely intend to rewrite the MAIN checkout's installed copies from"
        err "here, re-run with --allow-worktree (or LOOM_RESYNC_ALLOW_WORKTREE=1)."
        exit 1
    fi
fi

# ---------- resolve the defaults/ source tree ----------
#
# Mirrors the resolution order lib/loom-tools.sh's find_loom_tools() used before
# epic #4081 Phase 4 (#4557) retired it with the Python package:
#   1. Loom source repo (dogfood): $REPO_ROOT/defaults/
#   2. Recorded loom-source-path (target repo install)
#   3. install-metadata.json "loom_source"
#
# SOURCE_ROOT is the parent of DEFAULTS_DIR (always <root>/defaults) and is used
# to read the current loom_version (package.json) + loom_commit (git HEAD) for
# the metadata re-stamp.

DEFAULTS_DIR=""
SOURCE_ROOT=""
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
SOURCE_ROOT="$(dirname "$DEFAULTS_DIR")"

INSTALLED_HOOKS="$REPO_ROOT/.loom/hooks"
INSTALLED_SCRIPTS="$REPO_ROOT/.loom/scripts"

# ---------- local-override ignore list ----------

IGNORE_FILE="$REPO_ROOT/.loom/resync-ignore"
is_ignored() {
    # $1 = relative path like "hooks/foo.sh", "roles/bar.md", "bin/loom", etc.
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

# ---------- Loom-internal ownership boundary (#3464) ----------
#
# defaults/.loom-internal.list names defaults-relative paths the installer MUST
# NOT copy into a consumer repo (e.g. Loom-internal /loom:* skills). Resync must
# honor the same boundary or it would resurrect an internal file into a consumer
# on the next run. Same declarative list + exact-match semantics manifest.sh and
# the Rust installer consume.
INTERNAL_LIST="$DEFAULTS_DIR/.loom-internal.list"
is_loom_internal() {
    # $1 = defaults-relative path like ".claude/commands/loom/imagine.md"
    [[ -f "$INTERNAL_LIST" ]] || return 1
    local rel="$1" line
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" ]] && continue
        [[ "$line" == "$rel" ]] && return 0
    done < "$INTERNAL_LIST"
    return 1
}

# ---------- counters ----------

N_UPDATED=0
N_UNCHANGED=0
N_SKIPPED=0
# #4669: files that could not be staged/renamed into place. A non-empty list
# means the refresh is PARTIAL and must be reported as such (exit 1), never
# swallowed into a "success" summary.
N_FAILED=0
FAILED_RELS=()

record_failure() {
    N_FAILED=$((N_FAILED + 1))
    FAILED_RELS+=("$1")
}

# ---------- atomic staging + self-update deferral (#4669) ----------

# Physical absolute path of a FILE (abs_path above only resolves directories).
abs_file_path() {
    local p="$1" d b dir_abs
    d="$(dirname "$p")"
    b="$(basename "$p")"
    dir_abs="$(abs_path "$d")"
    [[ "$dir_abs" == "/" ]] && dir_abs=""
    printf '%s/%s' "$dir_abs" "$b"
}

# Octal permission bits of a path, or "" when unavailable (GNU stat, then BSD).
# -L (dereference) so a symlinked source (e.g. defaults/roles/*.md -> the
# .claude/commands/loom/*.md skillification target, #5222) reports the
# REFERENT's mode rather than the symlink's own (typically 755/lrwxrwxrwx)
# mode -- both GNU and BSD `stat` report the link's own bits without -L.
file_mode() {
    local p="$1" m
    m="$(stat -L -c '%a' "$p" 2>/dev/null)" || m=""
    if [[ -z "$m" ]]; then
        m="$(stat -L -f '%OLp' "$p" 2>/dev/null)" || m=""
    fi
    printf '%s' "$m"
}

# The file this bash process is executing. Its installed counterpart is synced
# LAST (apply_deferred_self_sync), so nothing rewrites it mid-run.
SELF_PATH=""
SELF_BASE=""
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    SELF_PATH="$(abs_file_path "${BASH_SOURCE[0]}")"
    SELF_BASE="${SELF_PATH##*/}"
fi
DEFER_SELF=1
SELF_SRC=""
SELF_DST=""
SELF_REL=""

# The staging file currently in flight, removed on any exit so a killed run
# never leaves `.resync-stage.*` dirt behind in an installed surface directory.
STAGED_TMP=""
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup_staged_tmp() {
    [[ -n "$STAGED_TMP" && -e "$STAGED_TMP" ]] && rm -f "$STAGED_TMP" 2>/dev/null
    STAGED_TMP=""
    return 0
}
trap cleanup_staged_tmp EXIT
trap 'cleanup_staged_tmp; exit 130' INT
trap 'cleanup_staged_tmp; exit 143' TERM
trap 'cleanup_staged_tmp; exit 129' HUP

# ---------- per-file sync ----------
#
# sync_one <src_file> <dst_file> <rel_label>
#   Copies src -> dst when they differ (unless --dry-run), preserving the
#   installed file's executable bit expectation. Only files that exist in the
#   source tree ever reach this function, so repo-specific installed files with
#   no source counterpart are never touched.
#
#   The copy is ALWAYS staged beside the destination and renamed into place
#   (#4669) — never written in place — so no reader can observe a partial file.
sync_one() {
    local src="$1" dst="$2" rel="$3"

    # #4669: never rewrite the script this process is executing while the rest
    # of the run is still in flight. Record it and apply it once every other
    # surface has settled (apply_deferred_self_sync). The basename test is a
    # cheap pre-filter so the path resolution (two subshells) runs at most once
    # per surface walk instead of once per file.
    if [[ "$DEFER_SELF" -eq 1 && -n "$SELF_PATH" && "${dst##*/}" == "$SELF_BASE" && \
          "$(abs_file_path "$dst")" == "$SELF_PATH" ]]; then
        SELF_SRC="$src"
        SELF_DST="$dst"
        SELF_REL="$rel"
        return 0
    fi

    if is_ignored "$rel"; then
        note "  ${YELLOW}skipped${NC}   $rel ${YELLOW}(pinned in .loom/resync-ignore)${NC}"
        N_SKIPPED=$((N_SKIPPED + 1))
        return 0
    fi

    # Never clobber a symlinked install target. In THIS dogfood repo the
    # .loom/docs/*.md entries are symlinks pointing back into defaults/;
    # overwriting them would corrupt the source of truth. Consumers get real
    # file copies, so this only ever short-circuits the dogfood case.
    if [[ -L "$dst" ]]; then
        note "  ${YELLOW}skipped${NC}   $rel ${YELLOW}(symlink -> $(readlink "$dst" 2>/dev/null))${NC}"
        N_SKIPPED=$((N_SKIPPED + 1))
        return 0
    fi

    if [[ -f "$dst" ]] && cmp -s "$src" "$dst" 2>/dev/null; then
        note "  ${GREEN}unchanged${NC} $rel"
        N_UNCHANGED=$((N_UNCHANGED + 1))
        return 0
    fi

    # src and dst differ (or dst is missing) — this is an update.
    local verb_past="updated" verb_pres="update"
    if [[ ! -f "$dst" ]]; then
        verb_past="created"
        verb_pres="create"
    fi

    if [[ "$DRY_RUN" -eq 1 ]]; then
        N_UPDATED=$((N_UPDATED + 1))
        printf '%b\n' "  ${BOLD}would ${verb_pres}${NC} $rel"
        return 0
    fi

    local dst_dir mode
    dst_dir="$(dirname "$dst")"
    mkdir -p "$dst_dir" 2>/dev/null

    # #4669: stage beside the destination, then rename. rename(2) is atomic
    # within a filesystem and replaces the DIRECTORY ENTRY rather than
    # truncating the destination's inode, so a process that already has the
    # destination open (most importantly: this script syncing itself) keeps
    # reading intact bytes, and an interrupted run can never leave a truncated
    # installed file behind.
    STAGED_TMP="$(mktemp "$dst_dir/.resync-stage.XXXXXX" 2>/dev/null)" || STAGED_TMP=""
    if [[ -z "$STAGED_TMP" ]]; then
        err "failed to stage $rel (cannot create a temp file in $dst_dir)"
        record_failure "$rel"
        return 1
    fi

    if ! cp "$src" "$STAGED_TMP" 2>/dev/null; then
        err "failed to copy $rel"
        cleanup_staged_tmp
        record_failure "$rel"
        return 1
    fi

    # mktemp creates the staging file 0600, so the rename would otherwise change
    # the installed file's permissions: restore the destination's current mode
    # (or the source's, when creating a new file) before swapping it in...
    mode="$(file_mode "$dst")"
    [[ -n "$mode" ]] || mode="$(file_mode "$src")"
    if [[ -n "$mode" ]]; then
        chmod "$mode" "$STAGED_TMP" 2>/dev/null || true
    fi
    # ...then match the executable bit of the source (defaults/ scripts/hooks are +x).
    if [[ -x "$src" ]]; then
        chmod +x "$STAGED_TMP" 2>/dev/null || true
    fi

    if mv -f "$STAGED_TMP" "$dst" 2>/dev/null; then
        STAGED_TMP=""
        N_UPDATED=$((N_UPDATED + 1))
        printf '%b\n' "  ${GREEN}${verb_past}${NC}   $rel"
        return 0
    fi

    err "failed to install $rel (could not rename the staged copy into place)"
    cleanup_staged_tmp
    record_failure "$rel"
    return 1
}

# ---------- deferred self-update (#4669) ----------
#
# Applied after every other surface has settled: at this point nothing else in
# the run needs the old copy, and the atomic rename in sync_one leaves this
# process's already-open inode untouched, so the remaining steps below
# (metadata re-stamp, .gitignore refresh, audit, summary) still execute the
# exact bytes this run started with.
apply_deferred_self_sync() {
    [[ -n "$SELF_DST" ]] || return 0
    local src="$SELF_SRC" dst="$SELF_DST" rel="$SELF_REL" rc=0
    SELF_SRC=""
    SELF_DST=""
    SELF_REL=""
    DEFER_SELF=0

    if ! is_ignored "$rel" && ! cmp -s "$src" "$dst" 2>/dev/null; then
        note "  ${BLUE}(deferred to last: $rel is the script running this resync)${NC}"
    fi

    sync_one "$src" "$dst" "$rel" || rc=$?
    DEFER_SELF=1

    if [[ $rc -ne 0 ]]; then
        err "The running resync script could NOT update itself: $rel"
        err "  Other installed surfaces were refreshed, so this install is now MIXED."
        err "  Recover with: cp '$src' '$dst'"
    fi
    return 0
}

# ---------- generic recursive surface resync (#4239) ----------
#
# resync_tree <src_dir> <dst_dir> <report_prefix> <defaults_prefix>
#   Recursively resyncs every file under src_dir into dst_dir.
#   - report_prefix   : prepended to the relative path for per-file reporting AND
#                       for .loom/resync-ignore matching (e.g. "roles", "docs",
#                       "bin", "commands/loom").
#   - defaults_prefix : defaults-relative prefix for the .loom-internal.list
#                       ownership-boundary check (e.g. "roles", "docs",
#                       ".claude/commands/loom", ".loom/bin").
#   A missing src_dir is a silent no-op. Existing sync_one semantics (ignore
#   list, symlink skip, idempotent copy, --dry-run) apply per file.
#
#   SOURCE-SIDE symlinks (#5222): all 17 defaults/roles/*.md files are
#   symlinks to ../.claude/commands/loom/*.md (the skillification dedup, so
#   the two copies of each role prompt never drift). Plain `find -type f`
#   lstats each entry and a symlink never matches `-type f`, so those 17
#   files silently fell out of the walk entirely -- not updated, not
#   skipped, not counted -- and a consumer repo's installed .loom/roles/*.md
#   (real file copies there, not symlinks) went stale forever while resync
#   reported success. `find -L` dereferences before the type test, so a
#   symlinked source is walked as the regular file it resolves to; `cp`
#   (sync_one) and `cmp` both already dereference by default, so the
#   destination gets the RESOLVED CONTENT, never a copied link. This is a
#   source-side concern only -- the destination-side symlink guard in
#   sync_one (`[[ -L "$dst" ]]`, protecting e.g. this dogfood repo's own
#   .loom/roles/*.md and .loom/docs/*.md, which are themselves symlinks back
#   into defaults/) is untouched and still runs first.
resync_tree() {
    local src_dir="$1" dst_dir="$2" report_prefix="$3" defaults_prefix="$4"
    [[ -d "$src_dir" ]] || return 0
    info "Resyncing ${dst_dir#"$REPO_ROOT/"}/ from ${src_dir#"$REPO_ROOT/"}/ ..."
    local src rel
    while IFS= read -r -d '' src; do
        rel="${src#"$src_dir/"}"
        # Honor the installer's Loom-internal skip boundary (#3464) so resync
        # never resurrects an internal file into a consumer repo.
        if is_loom_internal "$defaults_prefix/$rel"; then
            continue
        fi
        sync_one "$src" "$dst_dir/$rel" "$report_prefix/$rel"
    done < <(find -L "$src_dir" -type f -print0 2>/dev/null | sort -z)
}

# ---------- canonical Repo Skills guard detection (#4041, #4894) ----------
#
# When the canonical generic guard is installed in this repo AND passes BOTH
# runtime probes the guard-destructive.sh dispatcher requires — the
# rjwalters/repo#29 VERSION marker and the `worktree-write-confinement`
# CAPABILITY marker (proving it actually implements the Loom-only Bash-tool
# write-confinement category, issue #4178, not just the unrelated repo#29 fix)
# — Loom's vendored generic guard (guard-destructive-generic.sh) is
# intentionally NOT installed — the guard-destructive.sh dispatcher defers to the
# canonical guard at runtime. Resync must therefore neither resurrect the vendored
# copy nor leave a stale one behind. Same two-probe check the dispatcher/installer
# use, so all three agree on which guard wins (#4894: requiring only the version
# probe here would strip the vendored fallback out from under the dispatcher the
# moment a canonical guard picked up repo#29 without write-confinement, leaving
# zero coverage instead of the intended fallback).
#
# Guard against #4403: the canonical Repo Skills guard is a LOCAL, typically
# gitignored, per-host install (`.claude/skills/repo/`), but the vendored guard
# it defers to (`.loom/hooks/guard-destructive-generic.sh`) can be git-tracked in
# a repo that commits `.loom/` (this repo dogfoods that layout). Removing a
# tracked file based purely on one contributor's local skill install deletes a
# repo-shared file for everyone else. So below, before removing the vendored
# guard, we check whether the target is git-tracked and skip the removal if so —
# only untracked (the normal consumer-repo) targets are removed.
#
# #4566: that skip is reported as an informational `note`, NOT a `warn`. The
# condition is a *steady state*, not an anomaly: it can only arise where a
# maintainer deliberately committed the vendored fallback (posture (a) — keep the
# tracked copy so contributors/CI without Repo Skills still get full generic-guard
# coverage; see defaults/docs/guard-hooks.md). Resync already takes the only
# correct action automatically, every run, forever — so an alarm-level line that
# reprints on every resync with no way to acknowledge it is pure noise. A repo
# that genuinely wants posture (b) drops the vendored copy deliberately with
# `git rm .loom/hooks/guard-destructive-generic.sh`, after which this branch stops
# firing entirely.
CANONICAL_GUARD_PRESENT=0
if [[ -r "$REPO_ROOT/.claude/skills/repo/hooks/guard-destructive.sh" ]] && \
   grep -q 'repo#29' "$REPO_ROOT/.claude/skills/repo/hooks/guard-destructive.sh" 2>/dev/null && \
   grep -q 'worktree-write-confinement' "$REPO_ROOT/.claude/skills/repo/hooks/guard-destructive.sh" 2>/dev/null; then
    CANONICAL_GUARD_PRESENT=1
fi

# ---------- walk hooks (top-level *.sh, matching the installer) ----------

if [[ -d "$DEFAULTS_DIR/hooks" && -d "$INSTALLED_HOOKS" ]]; then
    info "Resyncing .loom/hooks/ from ${DEFAULTS_DIR#"$REPO_ROOT/"}/hooks/ ..."
    shopt -s nullglob
    for src in "$DEFAULTS_DIR/hooks/"*.sh; do
        name="$(basename "$src")"
        # The vendored generic guard is conditional on the canonical guard (#4041).
        if [[ "$name" == "guard-destructive-generic.sh" && "$CANONICAL_GUARD_PRESENT" -eq 1 ]]; then
            if [[ -f "$INSTALLED_HOOKS/$name" ]]; then
                if git -C "$REPO_ROOT" ls-files --error-unmatch -- ".loom/hooks/$name" >/dev/null 2>&1; then
                    # #4403: this target is git-tracked in the consuming repo, so it's
                    # repo-shared state, not this host's local install. Removing it
                    # would delete a committed file for every other contributor based
                    # solely on this host's local, typically-gitignored Repo Skills
                    # install. Leave it alone.
                    #
                    # #4566: report this as a `note`, not a `warn` — a committed
                    # vendored fallback is a deliberate, documented posture, so this
                    # is the expected steady state on every run, not an anomaly.
                    note "  ${GREEN}unchanged${NC} hooks/$name ${YELLOW}(git-tracked vendored fallback kept — canonical Repo Skills guard present; see defaults/docs/guard-hooks.md)${NC}"
                elif [[ "$DRY_RUN" -eq 1 ]]; then
                    printf '%b\n' "  ${BOLD}would remove${NC} hooks/$name ${YELLOW}(canonical Repo Skills guard present)${NC}"
                else
                    rm -f "$INSTALLED_HOOKS/$name" 2>/dev/null || true
                    printf '%b\n' "  ${GREEN}removed${NC}   hooks/$name ${YELLOW}(canonical Repo Skills guard present)${NC}"
                fi
            else
                note "  ${GREEN}unchanged${NC} hooks/$name ${YELLOW}(canonical Repo Skills guard present — not installed)${NC}"
            fi
            continue
        fi
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

# ---------- walk the widened pure-copy surfaces (#4239) ----------
#
# Each only runs when both its source and its installed destination exist, so a
# consumer that never received a surface is not force-populated. Local-only files
# (custom roles, repo-specific skills) have no source counterpart and are left
# untouched — the same rule that protects repo-specific hooks/scripts.

if [[ -d "$REPO_ROOT/.loom/roles" ]]; then
    resync_tree "$DEFAULTS_DIR/roles" "$REPO_ROOT/.loom/roles" "roles" "roles"
fi
if [[ -d "$REPO_ROOT/.loom/docs" ]]; then
    resync_tree "$DEFAULTS_DIR/docs" "$REPO_ROOT/.loom/docs" "docs" "docs"
fi
# `.loom/runtimes/` is deliberately UNCONDITIONAL, unlike the surfaces above
# (#4688): every one of the gated blocks only backfills a surface the
# consumer already opted into (destination pre-exists). `runtimes/` is not
# an opt-in surface — it is a provisioning gap that both the Rust-native
# `loom-daemon init` path and this script itself failed to populate before
# this fix, so a host resynced any number of times has NO other path to ever
# obtain the directory. `resync_tree`'s per-file `sync_one` already creates
# `$dst_dir` via `mkdir -p` as it copies (skipped harmlessly under
# `--dry-run`, which only reports "would create"), so this call alone is
# sufficient to both create `.loom/runtimes/` on hosts that never had it and
# keep it fresh on hosts that already do.
resync_tree "$DEFAULTS_DIR/runtimes" "$REPO_ROOT/.loom/runtimes" "runtimes" "runtimes"
if [[ -d "$REPO_ROOT/.loom/bin" ]]; then
    resync_tree "$DEFAULTS_DIR/.loom/bin" "$REPO_ROOT/.loom/bin" "bin" ".loom/bin"
fi
if [[ -d "$REPO_ROOT/.claude/commands/loom" ]]; then
    resync_tree "$DEFAULTS_DIR/.claude/commands/loom" "$REPO_ROOT/.claude/commands/loom" "commands/loom" ".claude/commands/loom"
fi

# ---------- install the deferred self-copy, now that every surface settled ----
#
# The scripts walk above records (rather than applies) a sync of the script this
# process is executing; apply it here, last, via the same atomic staging path.
apply_deferred_self_sync

# ---------- targeted field edit: loom-workspace package.json version (#4285) ----------
#
# defaults/package.json ships without a "version" field — the field was a decoy
# for version-detection tooling (npm-shape probes, /loom:bump) that mistook the
# installer's workspace stub for a real project version source. Consumers who
# installed the OLD stub (with "version": "1.0.0") still carry it on disk. A
# whole-file resync (like the surfaces above) would clobber the consumer's
# customized "scripts" block (check:ci, test, lint, ...), so this does a
# targeted field deletion instead: strip ".version" from the root package.json
# ONLY when ".name" is exactly "loom-workspace" and a "version" field is
# present. A consumer's own package.json (any other name) is left untouched.
resync_workspace_stub_version() {
    local pj="$REPO_ROOT/package.json"
    [[ -f "$pj" ]] || return 0

    if is_ignored "package.json"; then
        note "  ${YELLOW}skipped${NC}   package.json ${YELLOW}(pinned in .loom/resync-ignore)${NC}"
        N_SKIPPED=$((N_SKIPPED + 1))
        return 0
    fi

    if ! command -v jq >/dev/null 2>&1; then
        warn "Skipped package.json version-stub check (need jq). Surface sync still applied."
        return 0
    fi

    local name
    name="$(jq -r '.name // empty' "$pj" 2>/dev/null)"
    [[ "$name" == "loom-workspace" ]] || return 0

    local has_version
    has_version="$(jq -r 'has("version")' "$pj" 2>/dev/null)"
    if [[ "$has_version" != "true" ]]; then
        note "  ${GREEN}unchanged${NC} package.json (no decoy version field)"
        N_UNCHANGED=$((N_UNCHANGED + 1))
        return 0
    fi

    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf '%b\n' "  ${BOLD}would update${NC} package.json (remove decoy \"version\" field, #4285)"
        N_UPDATED=$((N_UPDATED + 1))
        return 0
    fi

    local tmp="${pj}.tmp.$$"
    if jq 'del(.version)' "$pj" > "$tmp" 2>/dev/null && [[ -s "$tmp" ]]; then
        mv "$tmp" "$pj"
        printf '%b\n' "  ${GREEN}updated${NC}   package.json (removed decoy \"version\" field, #4285)"
        N_UPDATED=$((N_UPDATED + 1))
    else
        rm -f "$tmp"
        err "failed to update package.json (jq del(.version))"
    fi
}

resync_workspace_stub_version

# ---------- re-stamp install-metadata.json (#4239, non-dry-run only) ----------
#
# Refresh loom_version + loom_commit (from the resolved source tree) and record a
# last_resync date. install_date and installed_files are left to the installer.
# jq or python3 is required for a safe in-place JSON edit; if neither is present
# we warn and skip — the surface sync above still succeeded.

read_source_version() {
    local pj="$SOURCE_ROOT/package.json" v
    [[ -f "$pj" ]] || { echo "unknown"; return 0; }
    v="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$pj" | head -1)"
    [[ -n "$v" ]] && echo "$v" || echo "unknown"
}

restamp_metadata() {
    local meta="$REPO_ROOT/.loom/install-metadata.json"
    [[ -f "$meta" ]] || return 0

    local version commit today tmp
    version="$(read_source_version)"
    commit="$(git -C "$SOURCE_ROOT" rev-parse --short HEAD 2>/dev/null || echo "unknown")"
    today="$(date +%Y-%m-%d)"
    tmp="${meta}.tmp.$$"

    if command -v jq >/dev/null 2>&1; then
        if jq --arg v "$version" --arg c "$commit" --arg r "$today" \
              '.loom_version=$v | .loom_commit=$c | .last_resync=$r' \
              "$meta" > "$tmp" 2>/dev/null && [[ -s "$tmp" ]]; then
            mv "$tmp" "$meta"
            note "  ${GREEN}re-stamped${NC} install-metadata.json (loom_version=$version, loom_commit=$commit, last_resync=$today)"
            return 0
        fi
        rm -f "$tmp"
    fi

    if command -v python3 >/dev/null 2>&1; then
        if META="$meta" VERSION="$version" COMMIT="$commit" TODAY="$today" \
           python3 - "$tmp" <<'PY' 2>/dev/null && [[ -s "$tmp" ]]; then
import json, os, sys
with open(os.environ["META"]) as f:
    data = json.load(f)
data["loom_version"] = os.environ["VERSION"]
data["loom_commit"] = os.environ["COMMIT"]
data["last_resync"] = os.environ["TODAY"]
with open(sys.argv[1], "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
            mv "$tmp" "$meta"
            note "  ${GREEN}re-stamped${NC} install-metadata.json (loom_version=$version, loom_commit=$commit, last_resync=$today)"
            return 0
        fi
        rm -f "$tmp"
    fi

    warn "Skipped install-metadata.json re-stamp (need jq or python3). Surface sync still applied."
    return 0
}

if [[ "$DRY_RUN" -ne 1 ]]; then
    restamp_metadata
fi

# ---------- refresh the Loom-managed .gitignore block (#4280) ----------
#
# The marker-delimited managed block in the consumer's .gitignore is written by
# `loom-daemon init` at install time and was NEVER refreshed by resync — so a
# repo installed by a stale binary (or before a pattern was added) keeps ignoring
# the old set forever, leaving newer runtime dirs (e.g. .loom/sweep-checkpoint/,
# .loom/worktrees-local/) untracked-and-unignored. The ephemeral-pattern list is
# single-sourced in the daemon (EPHEMERAL_PATTERNS), so we invoke the daemon's
# `update-gitignore` subcommand rather than duplicating the list in shell. A
# missing/too-old binary is a LOUD stderr warning, never a silent skip.

# ---------- ensure the install-metadata.json merge=ours driver (#4528) ----------
#
# .loom/install-metadata.json is a machine-local install stamp: every host's
# resync (the restamp_metadata step above) re-writes loom_version,
# loom_commit, and last_resync (plus loom_source, an absolute host-specific
# path) on every run. Because the file must stay tracked (it is the
# authoritative ownership manifest consumed by verify-install.sh and
# uninstall-loom.sh — see is_untracked_runtime_file() in verify-install.sh,
# which already treats its exact byte content as non-checksum-tracked
# runtime state), any two hosts that each commit a resync and then
# `git merge`/`git pull` the other's commit collide on this file, every
# time, on the exact same lines.
#
# The fix: a `merge=ours` attribute for the path in a Loom-managed marker
# block in .gitattributes (committed, shared) plus the `ours` driver enabled
# in LOCAL (never committed) git config -- `git config merge.ours.driver
# true` -- which git-attributes(5) requires a `merge=ours` attribute to be
# paired with. This is safe because the file is fully re-derived by the
# next resync regardless of which side "wins" a given merge conflict.
#
# Runs on every resync (including a fresh, non-dry-run first run on an
# existing install predating this fix) so existing hosts self-heal the
# first time they resync after upgrading past #4528, with no separate
# migration step required.

ensure_install_metadata_merge_driver() {
    local ga="$REPO_ROOT/.gitattributes"
    local begin="# BEGIN LOOM-MANAGED (merge drivers, #4528)"
    local end="# END LOOM-MANAGED (merge drivers, #4528)"
    local rule=".loom/install-metadata.json merge=ours"
    local changed=0

    if [[ ! -f "$ga" ]] || ! grep -qF "$rule" "$ga" 2>/dev/null; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            note "  ${BOLD}would add${NC} .loom/install-metadata.json merge=ours rule to .gitattributes"
        else
            {
                [[ -s "$ga" ]] && printf '\n'
                printf '%s\n' "$begin"
                printf '%s\n' "# install-metadata.json is a machine-local install stamp (loom_version,"
                printf '%s\n' "# loom_commit, last_resync, loom_source) that every host's resync"
                printf '%s\n' "# re-writes -- always keep our side on a merge conflict; the file is"
                printf '%s\n' "# fully re-derived by the next resync regardless of which side \"wins\"."
                printf '%s\n' "$rule"
                printf '%s\n' "$end"
            } >> "$ga"
            changed=1
        fi
    fi

    local current
    current="$(git -C "$REPO_ROOT" config --get merge.ours.driver 2>/dev/null || true)"
    if [[ "$current" != "true" ]]; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            note "  ${BOLD}would set${NC} local git config merge.ours.driver=true"
        else
            git -C "$REPO_ROOT" config merge.ours.driver true 2>/dev/null || true
            changed=1
        fi
    fi

    if [[ "$changed" -eq 1 ]]; then
        note "  ${GREEN}configured${NC} install-metadata.json merge=ours driver (.gitattributes + local git config)"
    fi
}
ensure_install_metadata_merge_driver

refresh_gitignore_block() {
    local locate_lib bin
    locate_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/locate-daemon-bin.sh"
    if [[ ! -f "$locate_lib" ]]; then
        warn ".gitignore refresh skipped: locate-daemon-bin.sh not found next to resync-installed.sh."
        warn "  Newer runtime paths may stay untracked-and-unignored until the next full install."
        return 0
    fi
    # shellcheck source=lib/locate-daemon-bin.sh
    # shellcheck disable=SC1091
    source "$locate_lib"
    # Prefer a binary in the source checkout (matches the version being synced);
    # the resolver falls back to $LOOM_DAEMON_BIN / `loom-daemon` on PATH.
    bin="$(loom_locate_daemon_bin "$SOURCE_ROOT")"
    if [[ -z "$bin" ]]; then
        warn "Could not refresh the Loom-managed .gitignore block: no loom-daemon binary resolved"
        warn "  (\$LOOM_DAEMON_BIN -> 'loom-daemon' on PATH -> build-output under $SOURCE_ROOT)."
        warn "  Newer runtime paths (e.g. .loom/sweep-checkpoint/, .loom/worktrees-local/) may stay untracked-and-unignored."
        return 0
    fi
    # `update-gitignore` has no dedicated --dry-run; on a dry run we only probe
    # that the subcommand exists (never writing), so the preview neither mutates
    # nor claims a refresh a pre-#4280 binary cannot perform.
    if [[ "$DRY_RUN" -eq 1 ]]; then
        if "$bin" update-gitignore --help >/dev/null 2>&1; then
            note "  ${BOLD}would refresh${NC} .gitignore (loom-managed block, via $bin)"
        else
            warn ".gitignore refresh unavailable: '$bin' has no 'update-gitignore' subcommand (rebuild the daemon)."
        fi
        return 0
    fi
    if "$bin" update-gitignore "$REPO_ROOT" >/dev/null 2>&1; then
        note "  ${GREEN}refreshed${NC} .gitignore (loom-managed block, via $bin)"
    else
        warn ".gitignore refresh failed: '$bin update-gitignore' errored"
        warn "  (a pre-#4280 daemon lacks this subcommand — rebuild loom-daemon)."
    fi
}
refresh_gitignore_block

# ---------- audit: untracked-and-unignored paths under .loom/ (#4280) ----------
#
# After the block refresh, anything STILL surfacing as untracked-and-unignored
# under .loom/ is a Loom-owned runtime path the pattern list does not yet cover
# (an enumerated list always trails reality). Surface it as a warning so it can
# be added to EPHEMERAL_PATTERNS, instead of silently dirtying the consumer's
# `git status` (or being swept into a commit by `git add -A`). `git status
# --porcelain` already excludes ignored files, so every `??` entry here is by
# definition untracked-and-unignored; tracked install-owned files never appear.

audit_untracked_loom_paths() {
    [[ -d "$REPO_ROOT/.loom" ]] || return 0
    local out
    out="$(git -C "$REPO_ROOT" status --porcelain -- .loom/ 2>/dev/null | sed -n 's/^?? //p')"
    [[ -z "$out" ]] && return 0
    warn "Untracked-and-unignored path(s) under .loom/ (not covered by the managed .gitignore block):"
    local p
    while IFS= read -r p; do
        [[ -z "$p" ]] && continue
        printf '%b\n' "${YELLOW}    $p${NC}" >&2
    done <<< "$out"
    warn "If these are Loom runtime state, add them to EPHEMERAL_PATTERNS (loom-daemon/src/init/post_init.rs)."
}
audit_untracked_loom_paths

# ---------- hint: stage + commit resync-only dirt (#4332) ----------
#
# In the loom source repo itself (DEFAULTS_DIR resolved locally, i.e. this
# repo tracks its own installed surfaces under git), a resync that changed
# tracked files leaves the tree dirty until that dirt is committed — and
# `main_health_gate.rs`'s dirty-tree check (#4332) only recognizes it as safe
# *resync* dirt (ignorable, not an operator edit worth halting the gate for),
# it never commits on the operator's behalf. Print the exact command so this
# doesn't linger as a standing "not evaluated (dirty-tree)" skip. Cheap and
# best-effort: only fires when every dirty/untracked path is one this run's
# surfaces cover (or the re-stamped install-metadata.json); any other dirt
# (a genuine operator edit) suppresses the hint entirely.
suggest_commit_if_resync_only_dirt() {
    [[ "$REPO_ROOT/defaults" == "$DEFAULTS_DIR" ]] || return 0
    local status
    status="$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)"
    [[ -z "$status" ]] && return 0

    local line path
    local -a resync_paths=()
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        path="${line:3}"
        [[ "$path" == *" -> "* ]] && path="${path##* -> }"
        path="${path%\"}"
        path="${path#\"}"
        case "$path" in
            .loom/hooks/*|.loom/scripts/*|.loom/roles/*|.loom/docs/*|.loom/runtimes/*|.loom/bin/*|.claude/commands/loom/*|.loom/install-metadata.json|.gitattributes)
                resync_paths+=("$path")
                ;;
            *)
                # Non-resync dirt present — do not suggest a commit that would
                # also stage an unrelated (possibly operator) change.
                return 0
                ;;
        esac
    done <<< "$status"
    [[ "${#resync_paths[@]}" -eq 0 ]] && return 0

    echo ""
    note "${BLUE}[resync] The tree is dirty with only resync output above — stage and commit it so the main-health gate doesn't skip on it:${NC}"
    printf '%b\n' "    ${BOLD}git add ${resync_paths[*]} && git commit -m 'chore: resync installed Loom surfaces'${NC}"
}
[[ "$DRY_RUN" -eq 1 || "$N_FAILED" -gt 0 ]] || suggest_commit_if_resync_only_dirt

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

# A failed file makes the refresh PARTIAL — say so explicitly and exit non-zero
# rather than folding it into a success summary (#4669). Nothing is ever left
# half-written (every copy is staged off to the side and renamed), so the
# recovery is simply "fix the cause, re-run".
if [[ "$N_FAILED" -gt 0 ]]; then
    printf '%b\n' "${RED}${BOLD}[resync] PARTIAL REFRESH: ${N_FAILED} file(s) could NOT be synced (${N_UPDATED} updated, ${N_UNCHANGED} unchanged, ${N_SKIPPED} skipped).${NC}"
    printf '%b\n' "${RED}Failed to sync:${NC}"
    for failed_rel in "${FAILED_RELS[@]}"; do
        printf '%b\n' "${RED}    $failed_rel${NC}"
    done
    printf '%b\n' "${YELLOW}This install is now MIXED: the ${N_UPDATED} file(s) reported above are current, the failed ones are still stale.${NC}"
    printf '%b\n' "${YELLOW}No file was left half-written (each copy is staged beside its destination and renamed atomically),${NC}"
    printf '%b\n' "${YELLOW}so fixing the cause (permissions, disk space, read-only mount) and re-running completes the refresh.${NC}"
    exit 1
fi

if [[ "$N_UPDATED" -gt 0 ]]; then
    printf '%b\n' "${GREEN}${BOLD}[resync] ${N_UPDATED} file(s) updated, ${N_UNCHANGED} unchanged, ${N_SKIPPED} skipped.${NC}"
else
    printf '%b\n' "${GREEN}[resync] Already in sync (${N_UNCHANGED} unchanged, ${N_SKIPPED} skipped).${NC}"
fi
exit 0
