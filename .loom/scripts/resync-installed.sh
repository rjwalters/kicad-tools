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
# PRECONDITION (#6202): that flow only works when a defaults/ SOURCE tree can
# be resolved (see resolve_defaults() below) — this checkout IS the Loom
# source repo, OR the gitignored `.loom/loom-source-path` sidecar points at a
# local clone of it. Neither holds on a checkout that never ran the Loom
# installer locally (a fresh developer clone, a CI checkout, a machine that
# received the repo rather than installing into it) — the exact population
# most likely to be running stale surfaces, since they never ran the
# installer that would have refreshed them. On that population this script
# fails on first use with "Could not locate a defaults/ source tree to sync
# from"; check-main-freshness.sh now detects the same gap and says so before
# you get here (see its own #6202 note), but if you landed on this file
# directly: clone https://github.com/rjwalters/loom locally, then either
# re-run its installer against this repo or write the sidecar yourself
# (`echo /path/to/local/loom-clone > .loom/loom-source-path`).
#
# It is idempotent (a no-op when already in sync), reports per-file
# updated/created/removed/unchanged/skipped, only ever touches files that
# either exist in the source tree or are explicitly declared retired (see
# "RETIRED PAYLOAD FILES" below) — repo-specific files with no source
# counterpart and no retirement entry are left alone — never clobbers a
# symlinked install target, and supports --dry-run.
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
#   .claude/README.md       <- defaults/.claude/README.md      (single file, #5264)
#   .github/CONFIGURATION.md <- defaults/.github/CONFIGURATION.md (single file, #5264)
#   .loom/biome.jsonc       <- defaults/.loom/biome.jsonc      (single file, BACKFILLED
#                                                              if absent, #6031)
#   .claude/biome.jsonc     <- defaults/.claude/biome.jsonc    (single file, BACKFILLED
#                                                              if absent, #6031)
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
# behalf. Running this script here — including at a clean checkout pinned to
# origin/main — is supported and expected, not a bug: `.loom/` in this repo is
# a periodically-resynced snapshot of `defaults/`, not a live mirror, so any
# `defaults/`-only merge that lands after the last "chore: resync installed
# Loom surfaces" commit reintroduces drift that is real, deterministic, and
# bounded only by how long it has been since that last resync commit (#5510).
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
# CRASH-DETECTION MARKER (#5980): #4669 above protects individual files from
# being torn mid-write, but it does not protect the RUN as a whole from dying
# outright — e.g. hitting a bug in the OLD installed copy of this script
# (before the fixed copy has been synced in) aborts bash entirely, with an
# arbitrary number of surfaces already refreshed and the rest still stale.
# Before #5980, nothing recorded that a run was ever in progress, so a
# crashed run left the working tree silently half-updated while
# .loom/install-metadata.json kept reporting the OLD loom_version — the
# install looked simply "never updated" rather than "partially updated".
#
# `.loom/.resync-in-progress` (gitignored) closes that gap: it is written with
# the target version BEFORE any surface is touched (non-dry-run only) and
# removed only once the run reaches a full, non-partial success. On EVERY
# invocation (including --dry-run, so it doubles as a zero-side-effect
# detector) a leftover marker from a prior run is reported as a loud WARN
# naming the target version and start time it never finished. No separate
# "resume from where it left off" bookkeeping is needed: the whole script is
# already idempotent (see above), so a fresh restart after a crash converges
# on exactly the state a completed run would have reached — files the crashed
# run already finished are simply re-verified as unchanged, not re-copied.
#
# Not done here (left as a follow-up, #5980): inverting the self-update order
# so the script resyncs ITSELF and re-execs into the fixed copy before
# touching any other surface, which would keep a buggy OLD script off the
# critical path entirely instead of only detecting after the fact that it ran
# into one. The marker is the tractable, low-risk half of the fix; the
# reordering is a larger, riskier restructuring of the self-update deferral
# #4669 established.
#
# EXPLICITLY OUT OF SCOPE (never touched by resync — updated by other mechanisms):
#   .loom/config.json       - operator-owned; needs merge-semantics design
#   CLAUDE.md               - repo-customized at install; needs managed-section markers,
#                             WITH ONE NARROW EXCEPTION (#6612), mirroring
#                             .loom/CLAUDE.md's #5559 exception below: resync DOES
#                             restamp just the "**Loom Version**" / "Last updated"
#                             header lines — a targeted field edit (see
#                             resync_root_claude_md_version_header() below), NOT a
#                             whole-file resync/regenerate. That still needs the
#                             managed-section-markers design this comment
#                             references; the rest of the file's body content is
#                             left untouched.
#   AGENTS.md               - repo-customized at install; needs managed-section markers
#                             (same as CLAUDE.md; #4479). Its full-guide sibling
#                             .loom/AGENTS.md is regenerated by install scaffolding
#                             from defaults/.loom/AGENTS.md, not by resync — same
#                             posture as .loom/CLAUDE.md, WITH ONE NARROW EXCEPTION
#                             (#5559): resync DOES restamp just the "**Loom
#                             Version**" / "Last updated" header lines in
#                             .loom/CLAUDE.md — a targeted field edit (see
#                             resync_claude_md_version_header() below, which
#                             mirrors the package.json version-stub edit's
#                             pattern), NOT a whole-file resync/regenerate. That
#                             still needs the managed-section-markers design
#                             this comment references; the rest of the file's
#                             body content is left untouched.
#   .github/labels.yml (the FILE itself), .github/workflows/*
#                            - the FILE content is repo-customized (a consumer
#                              may add their own labels above/below the
#                              LOOM-MANAGED block, #4187) and is not resynced
#                              here. Its role as the source of truth for the
#                              LIVE FORGE label set is a different story,
#                              though (#6716): resync DOES now re-check the
#                              target repo's live labels against this file
#                              and safely creates/refreshes what's missing or
#                              stale (never deletes/renames) -- see the
#                              "forge label drift check" step near the end of
#                              this script. `.github/workflows/*` remains
#                              fully out of scope either way.
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
# STAGING MODE (#6106): --allow-worktree (or a bare re-run from the main
# checkout) is unsafe while the fleet is live — the daemon may be dispatching
# sweeps in that same checkout, so writing dozens of installed files there
# mid-sweep risks exactly the contamination this whole restriction exists to
# prevent. --output <dir> is the safe alternative: it creates a disposable,
# DETACHED `git worktree` at HEAD under <dir> (never inside .loom/worktrees/,
# and never touching the primary checkout's own files) and resyncs INTO that
# staging worktree instead of REPO_ROOT — so it can be run from anywhere
# (primary checkout or any linked worktree) at any time, including mid-sweep,
# with zero risk to the live checkout. The staging worktree is a real,
# independent git checkout: once the sync is complete you `cd` into it,
# `git add -A && git commit` (and `git push` / open a PR) from there, then
# `git worktree remove` it. See "OUTPUT-DIR STAGING MODE" further below for
# the full mechanics. --dry-run + --output still creates (and then
# auto-removes) the staging worktree, so a preview never leaves any residue.
#
# Local-override convention: list a relative path (e.g. `hooks/guard-destructive.sh`,
# `scripts/foo.sh`, `roles/custom-role.md`, `docs/notes.md`, `bin/loom`,
# `commands/loom/mine.md`, `package.json` to pin the #4285 stub version edit,
# `.loom/CLAUDE.md` to pin the #5559 version-header restamp, or `CLAUDE.md` to
# pin the #6612 root version-header restamp) — one per line — in
# `.loom/resync-ignore` to pin an intentional per-repo customization. Matching
# files are reported as `skipped` and never overwritten. Blank lines and `#`
# comments are ignored.
#
# Path form (#6515): entries may be written either ".loom/"-relative (the form
# above, e.g. `scripts/foo.sh`) OR the more natural repo-relative spelling
# (e.g. `.loom/scripts/foo.sh`, optionally with a leading `./`) — is_ignored()
# tries the exact string first, then retries with a leading `./` and `.loom/`
# stripped, so both spellings pin the same file. An entry that still matches
# NEITHER form (a typo, or a pin for a file upstream has since retired) is a
# silent no-op no longer: every run ends with a `pin had no effect: '<entry>'`
# warning for each dead entry, naming the closest walked path when one shares
# its basename.
#
# RETIRED PAYLOAD FILES (#5981): the walk above only ever visits files that
# CURRENTLY exist under defaults/, so a file retired upstream (deleted from
# defaults/ entirely, e.g. defaults/scripts/status.sh in #5710) has no
# source to walk from and is therefore never noticed, let alone removed —
# it survives indefinitely in every already-installed repo. `defaults/.loom-retired.list`
# is the declarative fix: one target-relative path per line (same
# report-relative form as the per-file report and `.loom/resync-ignore`,
# e.g. `scripts/status.sh`), naming a file that WAS Loom payload and has
# since been deleted from defaults/. Every run, `remove_retired_files()`
# removes each listed path that is still present in the installed tree,
# reporting it with the `removed` verb — honoring the same `.loom/resync-ignore`
# pin and destination-symlink guard the update path uses, so a consumer who
# deliberately kept a fork of a retired file is never touched. A file with
# no retirement entry and no source counterpart is untouched, exactly as
# before — this is additive, not a general directory diff, so it can never
# guess-delete an unrelated repo-specific file.
#
# OUTPUT-DIR STAGING MODE (#6106): --output <dir> resyncs into an isolated
# location instead of the primary checkout, so a COMPLETE resync can be
# generated on demand — including while the fleet is live and mid-sweep —
# without the "run it from the main checkout" remedy the #4563 refusal above
# normally prescribes (which is unsafe precisely when the daemon is actively
# dispatching sweeps there). Mechanics:
#   1. <dir> must not already exist. It is created via
#      `git worktree add --detach <dir> HEAD` against the PRIMARY checkout's
#      repository — a real, independent git checkout at the primary's current
#      HEAD, registered as a linked worktree but living wherever the caller
#      pointed <dir> (never inside .loom/worktrees/, so it can never collide
#      with worktree.sh's bookkeeping). Creating it only touches git's
#      worktree-registry metadata (.git/worktrees/) — it does not read, write,
#      or lock any file in the primary checkout's own working tree.
#   2. Every destination this script would otherwise resolve under the
#      primary checkout (.loom/hooks, .loom/scripts, .loom/roles, .loom/docs,
#      .loom/runtimes, .loom/bin, .claude/commands/loom, the single-file docs,
#      install-metadata.json, .loom/CLAUDE.md, package.json, .gitattributes,
#      .gitignore) is instead resolved under <dir>. defaults/ itself (the
#      SOURCE of the sync) is still read from the primary checkout — that is
#      a read, never a write, so it carries none of the #4563 hazard.
#   3. On success the run prints the exact `cd <dir> && git add -A && git
#      commit ... && git push` sequence to turn the staged tree into a
#      resync commit (and PR) from a location that was never live-mid-sweep,
#      plus the `git worktree remove` to clean up afterward.
# Because step 1 creates a real worktree, the #4563 linked-worktree refusal
# itself never applies when --output is given — there is nothing left for it
# to protect, since nothing is written to the primary checkout either way.
# --dry-run + --output still creates the staging worktree (needed as the
# preview's target) but auto-removes it before exiting, since a preview must
# leave no residue.
#
# The SAME `.loom/resync-ignore` list is read by the installer (issue #5971) as
# a declaration that a path inside `.loom/` is repo-owned: the reinstall clean
# sweep in `loom-daemon init` and `uninstall-loom.sh --clean` will not delete
# it. One list, one meaning — "this path is the repo's, not Loom's". Full
# ownership rule: `.loom/docs/repo-owned-files.md`.
#
# Usage:
#   ./.loom/scripts/resync-installed.sh            # sync; report what changed
#   ./.loom/scripts/resync-installed.sh --dry-run  # preview only; make no changes
#   ./.loom/scripts/resync-installed.sh --quiet    # only report updated/skipped
#   ./.loom/scripts/resync-installed.sh --allow-worktree
#                                                  # permit running from a linked
#                                                  # worktree (still writes the MAIN
#                                                  # checkout's installed copies —
#                                                  # unsafe while the fleet is live;
#                                                  # prefer --output below)
#   ./.loom/scripts/resync-installed.sh --output <dir>
#                                                  # generate a COMPLETE resync in an
#                                                  # isolated staging worktree at <dir>
#                                                  # instead — safe from anywhere, any
#                                                  # time, including mid-sweep (#6106)
#   ./.loom/scripts/resync-installed.sh --help     # show usage
#
# Environment:
#   LOOM_RESYNC_ALLOW_WORKTREE=1  - same as --allow-worktree (for non-interactive
#                                   callers), matching the LOOM_ALLOW_* override
#                                   convention used elsewhere in .loom/scripts.
#   LOOM_RESYNC_OUTPUT=<dir>      - same as --output <dir> (for non-interactive
#                                   callers). An explicit --output flag wins if
#                                   both are given.
#
# Exit codes:
#   0 - Success. Sync applied (or already in sync); or --dry-run found no drift.
#   1 - Error (not in a git repo, the source tree could not be located,
#       invoked from a linked worktree without --allow-worktree or --output, the
#       --output directory already exists or its staging worktree could not be
#       created, or one or more files could not be synced — see the PARTIAL
#       summary block, #4669).
#   2 - --dry-run only: drift detected (one or more files WOULD be updated,
#       created, or removed as a retired payload file, see RETIRED PAYLOAD
#       FILES above).
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
# #6106: generate a complete resync in an isolated staging worktree instead of
# writing to the primary checkout. Empty means "not requested".
OUTPUT_DIR="${LOOM_RESYNC_OUTPUT:-}"

err()  { printf '%b\n' "${RED}ERROR: $*${NC}" >&2; }
warn() { printf '%b\n' "${YELLOW}WARN: $*${NC}" >&2; }
info() { printf '%b\n' "${BLUE}$*${NC}"; }
note() { [[ "$QUIET" -eq 1 ]] || printf '%b\n' "$*"; }

# ---------- args ----------
#
# A while/shift loop (rather than `for arg in "$@"`) because --output takes a
# following positional value.

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run|-n)     DRY_RUN=1; shift ;;
        --quiet|-q)       QUIET=1; shift ;;
        --allow-worktree) ALLOW_WORKTREE=1; shift ;;
        --output)
            if [[ $# -lt 2 || -z "$2" ]]; then
                err "--output requires a directory argument (try --help)"
                exit 1
            fi
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --output=*)
            OUTPUT_DIR="${1#--output=}"
            if [[ -z "$OUTPUT_DIR" ]]; then
                err "--output requires a directory argument (try --help)"
                exit 1
            fi
            shift
            ;;
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
            err "Unknown argument: $1 (try --help)"
            exit 1
            ;;
    esac
done

# --output is resolved to an absolute path up front (before any `cd`-adjacent
# resolution below) so a relative value like `--output ../staging` is anchored
# to the caller's actual invocation directory.
if [[ -n "$OUTPUT_DIR" ]]; then
    case "$OUTPUT_DIR" in
        /*) ;;
        *)  OUTPUT_DIR="$PWD/$OUTPUT_DIR" ;;
    esac
fi

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
#
# #6106: entirely skipped when --output is given. Nothing below writes to
# REPO_ROOT in that mode (every destination resolves under the disposable
# staging worktree created below instead), so there is nothing left to
# refuse — the whole point of --output is to make the refusal unnecessary.

abs_path() {
    local p="$1"
    [[ -d "$p" ]] || { printf '%s' "$p"; return 0; }
    (cd "$p" 2>/dev/null && pwd -P) || printf '%s' "$p"
}

WORKTREE_TOP="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$OUTPUT_DIR" && -n "$WORKTREE_TOP" && "$(abs_path "$WORKTREE_TOP")" != "$(abs_path "$REPO_ROOT")" ]]; then
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
        err "That is unsafe to do 'the obvious way' whenever the fleet may be live (the"
        err "daemon dispatching sweeps in the main checkout) — which on a fleet host is"
        err "most of the time. The SAFE way to generate a complete resync on demand,"
        err "from here, right now, is --output (#6106):"
        err "  ./.loom/scripts/resync-installed.sh --output /tmp/loom-resync-staging"
        err "This stages the full resync in a disposable git worktree and never touches"
        err "the main checkout — see the OUTPUT-DIR STAGING MODE header comment in this"
        err "script for the mechanics, then commit/push from the staging directory."
        printf '\n' >&2
        err "Installed-copy propagation is otherwise the periodic resync commit's job:"
        err "commit your defaults/ change, get it merged, then run this from the main"
        err "checkout during a quiet window:"
        err "  cd $REPO_ROOT && ./.loom/scripts/resync-installed.sh"
        printf '\n' >&2
        err "If you genuinely intend to rewrite the MAIN checkout's installed copies from"
        err "here (a quiet window, no sweep in flight), re-run with --allow-worktree (or"
        err "LOOM_RESYNC_ALLOW_WORKTREE=1)."
        exit 1
    fi
fi

# ---------- output-dir staging worktree (#6106) ----------
#
# WRITE_ROOT is the root every destination path below resolves against — the
# PRIMARY checkout by default, or a disposable staging worktree when --output
# was given. REPO_ROOT itself is left untouched either way: it is still used
# to locate defaults/ (read-only) and to create the staging worktree.
#
# The staging worktree is a REAL, independent git checkout (`git worktree add
# --detach <dir> HEAD`), not a bare file copy — so once the sync below
# completes, <dir> is immediately a normal place to `git add`, `commit`, and
# `push` from. Creating it only registers a new entry under the PRIMARY
# checkout's `.git/worktrees/`; nothing in the primary checkout's own working
# tree is read, locked, or written by this step.
WRITE_ROOT="$REPO_ROOT"
STAGING_WORKTREE_CREATED=0
# #6138: set to 1 only at the two points that intentionally keep a completed
# staging worktree around for the operator (the N_FAILED partial-refresh exit
# and the final success exit). Everywhere else — including every early-exit
# failure path between worktree creation and those two points, plus any
# signal — the EXIT trap below removes it so a failed run never leaks a
# `.git/worktrees/` registration.
KEEP_STAGING_WORKTREE=0

remove_staging_worktree() {
    [[ "$STAGING_WORKTREE_CREATED" -eq 1 && -n "$OUTPUT_DIR" && "$KEEP_STAGING_WORKTREE" -eq 0 ]] || return 0
    git -C "$REPO_ROOT" worktree remove --force "$OUTPUT_DIR" >/dev/null 2>&1 \
        || rm -rf "$OUTPUT_DIR" 2>/dev/null
    STAGING_WORKTREE_CREATED=0
}

if [[ -n "$OUTPUT_DIR" ]]; then
    if [[ -e "$OUTPUT_DIR" ]]; then
        err "--output directory already exists: $OUTPUT_DIR"
        err "Point --output at a path that does not yet exist (it becomes a fresh git worktree)."
        exit 1
    fi
    mkdir -p "$(dirname "$OUTPUT_DIR")" 2>/dev/null || true
    if ! git -C "$REPO_ROOT" worktree add --detach -q "$OUTPUT_DIR" HEAD >/dev/null 2>&1; then
        err "Failed to create the staging worktree at $OUTPUT_DIR"
        err "  (git -C $REPO_ROOT worktree add --detach $OUTPUT_DIR HEAD)"
        exit 1
    fi
    STAGING_WORKTREE_CREATED=1
    WRITE_ROOT="$OUTPUT_DIR"
    info "Staging a complete resync in a disposable worktree — the primary checkout is untouched:"
    info "  $OUTPUT_DIR"
    # #6138: cover every exit path from this point forward (resolve_defaults
    # failure below, any later early exit, or a signal) until either the
    # dedicated cleanup_staged_tmp+remove_staging_worktree trap is installed
    # further down (which supersedes this one and keeps calling
    # remove_staging_worktree — it is a no-op once KEEP_STAGING_WORKTREE is
    # set or the worktree is already gone) or KEEP_STAGING_WORKTREE is set at
    # one of the two intentional-keep points.
    trap remove_staging_worktree EXIT
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
#
# #5624: none of the current writers (install.sh, scripts/install-loom.sh,
# loom-daemon's write_install_metadata) put "loom_source" into
# install-metadata.json anymore — that field leaked the installing machine's
# absolute path (including username) into a committed file. Priority 3 below
# is therefore now a read-only compatibility path: it only helps a repo that
# already committed the field before this fix AND whose gitignored
# `.loom/loom-source-path` sidecar (priority 2) has since gone missing on the
# same machine. It cannot fire at all for a post-fix install. This is an
# accepted, intentional narrowing of the recovery path (Acceptance Criteria,
# #5624) — no replacement fallback is added.

# A candidate source root is only usable if it actually has SOMETHING to sync
# from — `-d "$root/defaults"` alone is not sufficient (#6780): a sidecar or
# metadata path can point at a directory that still exists (unlike the
# "vanished clone" case, which already fails loud below) but is stale, empty,
# or was never a real Loom checkout — e.g. a scratch clone whose contents were
# emptied without removing the directory itself, or an unrelated directory
# that merely happens to contain an empty `defaults/`. Requiring a populated
# `defaults/hooks` or `defaults/scripts` mirrors the dogfood rung's own check
# immediately below and closes the gap that let resolve_defaults() "succeed"
# against a source tree with nothing under it — which then made the sync walk
# below iterate zero files and report "already in sync", a false "current"
# verdict rather than the loud, honest failure an unresolvable source should
# produce.
is_usable_defaults_root() {
    local root="$1"
    [[ -n "$root" && ( -d "$root/defaults/hooks" || -d "$root/defaults/scripts" ) ]]
}

DEFAULTS_DIR=""
SOURCE_ROOT=""
resolve_defaults() {
    if is_usable_defaults_root "$REPO_ROOT"; then
        DEFAULTS_DIR="$REPO_ROOT/defaults"
        return 0
    fi
    if [[ -f "$REPO_ROOT/.loom/loom-source-path" ]]; then
        local src
        src="$(cat "$REPO_ROOT/.loom/loom-source-path" 2>/dev/null || true)"
        if is_usable_defaults_root "$src"; then
            DEFAULTS_DIR="$src/defaults"
            return 0
        fi
    fi
    if [[ -f "$REPO_ROOT/.loom/install-metadata.json" ]]; then
        local src
        src="$(sed -n 's/.*"loom_source" *: *"\(.*\)".*/\1/p' "$REPO_ROOT/.loom/install-metadata.json" 2>/dev/null | head -1)"
        if is_usable_defaults_root "$src"; then
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

# ---------- pre-resync shell-syntax gate (#6162 AC2) ----------------------
#
# #6162: an abandoned `git stash pop` conflict left live conflict markers in
# defaults/scripts/spawn-claude.sh in the primary checkout. Nothing validated
# that a source file about to be copied actually PARSES, so a resync would
# have shipped that non-parsing script into every consumer repo's installed
# .loom/scripts/. This runs check-shell-syntax.sh (#6162 AC1) against the
# SOURCE tree (defaults/hooks, defaults/scripts) — the exact files the two
# walks below are about to read — BEFORE any sync_one call and before the
# crash-detection marker is written, so a syntax failure aborts with nothing
# touched, not even a partial write. Scope is intentionally the whole
# defaults/hooks and defaults/scripts trees (recursive), a superset of what
# the hooks walk actually copies (top-level *.sh only) — catching a broken
# script anywhere under either tree is strictly safer than matching the copy
# walk's scope exactly, and matches the issue's "bash -n every installed
# shell surface" framing. A missing check-shell-syntax.sh (e.g. an
# unusually old defaults/ tree) degrades to a warning, never a silent skip,
# and never blocks the sync — the gate can only get MORE strict over time.
SYNTAX_CHECK_SCRIPT="$DEFAULTS_DIR/scripts/check-shell-syntax.sh"
if [[ -x "$SYNTAX_CHECK_SCRIPT" ]]; then
    syntax_check_dirs=()
    [[ -d "$DEFAULTS_DIR/hooks" ]] && syntax_check_dirs+=(--dir "$DEFAULTS_DIR/hooks")
    [[ -d "$DEFAULTS_DIR/scripts" ]] && syntax_check_dirs+=(--dir "$DEFAULTS_DIR/scripts")
    if [[ "${#syntax_check_dirs[@]}" -gt 0 ]]; then
        if ! syntax_check_out="$("$SYNTAX_CHECK_SCRIPT" --quiet "${syntax_check_dirs[@]}" 2>&1)"; then
            err "Refusing to resync: one or more source shell scripts do not parse (bash -n)."
            printf '%s\n' "$syntax_check_out" >&2
            err "Fix the offending file(s) under $DEFAULTS_DIR before re-running this script — nothing was copied."
            exit 1
        fi
    fi
else
    warn "check-shell-syntax.sh not found at $SYNTAX_CHECK_SCRIPT — skipping the pre-resync shell-syntax gate (#6162)."
fi

# ---------- pre-resync conflict-marker gate (#6499) -----------------------
#
# The gate above proves shell sources PARSE, but it can only speak for `*.sh`
# — `bash -n` has nothing to say about a doc, a role prompt, or a runtime
# `*.json`. #6499 is the same corruption shape (an abandoned `git stash pop`
# leaving live `<<<<<<<` / `=======` / `>>>>>>>` markers) landing in a
# non-shell file, where it stayed invisible until a daemon boot failed to
# parse it and silently fell back to built-in defaults. Every root this
# script copies is in scope: a marker-corrupted role prompt or runtime
# descriptor would be replicated into every consumer's `.loom/` exactly as
# #6162's non-parsing spawn script would have been. Same failure posture as
# the gate above: refuse before any write, and degrade to a warning (never a
# silent skip) if the checker is missing from an older defaults/ tree.
MARKER_CHECK_SCRIPT="$DEFAULTS_DIR/scripts/check-conflict-markers.sh"
if [[ -x "$MARKER_CHECK_SCRIPT" ]]; then
    marker_check_dirs=()
    for _marker_root in hooks scripts docs roles runtimes bin .claude; do
        [[ -d "$DEFAULTS_DIR/$_marker_root" ]] && marker_check_dirs+=(--dir "$DEFAULTS_DIR/$_marker_root")
    done
    if [[ "${#marker_check_dirs[@]}" -gt 0 ]]; then
        if ! marker_check_out="$("$MARKER_CHECK_SCRIPT" --quiet "${marker_check_dirs[@]}" 2>&1)"; then
            err "Refusing to resync: one or more source files carry live git conflict markers."
            printf '%s\n' "$marker_check_out" >&2
            err "Resolve the conflict(s) under $DEFAULTS_DIR before re-running this script — nothing was copied."
            exit 1
        fi
    fi
else
    warn "check-conflict-markers.sh not found at $MARKER_CHECK_SCRIPT — skipping the pre-resync conflict-marker gate (#6499)."
fi

# Current source version (from the resolved SOURCE_ROOT's package.json). Used
# by restamp_metadata() / resync_claude_md_version_header() /
# resync_root_claude_md_version_header() below AND by the
# #5980 crash-detection marker, so it is defined here — as soon as
# SOURCE_ROOT is known — rather than down by its other callers.
read_source_version() {
    local pj="$SOURCE_ROOT/package.json" v
    [[ -f "$pj" ]] || { echo "unknown"; return 0; }
    v="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$pj" | head -1)"
    [[ -n "$v" ]] && echo "$v" || echo "unknown"
}

INSTALLED_HOOKS="$WRITE_ROOT/.loom/hooks"
INSTALLED_SCRIPTS="$WRITE_ROOT/.loom/scripts"

# ---------- local-override ignore list ----------

IGNORE_FILE="$WRITE_ROOT/.loom/resync-ignore"

# #6515: every distinct "$rel" ever checked against is_ignored (the "did you
# mean" candidate pool for report_dead_pins below), and every pin LINE that
# matched at least one of them this run (keyed by the trimmed, comment-
# stripped line — same string report_dead_pins re-derives when it walks
# IGNORE_FILE a second time at the end).
declare -A SEEN_RELS=()
declare -A PIN_HIT=()

is_ignored() {
    # $1 = relative path like "hooks/foo.sh", "roles/bar.md", "bin/loom", etc.
    [[ -f "$IGNORE_FILE" ]] || return 1
    local rel="$1" line normalized
    SEEN_RELS["$rel"]=1
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"                       # strip trailing comment
        line="${line#"${line%%[![:space:]]*}"}"  # ltrim
        line="${line%"${line##*[![:space:]]}"}"   # rtrim
        [[ -z "$line" ]] && continue
        if [[ "$line" == "$rel" ]]; then
            PIN_HIT["$line"]=1
            return 0
        fi
        # #6515: also accept the natural repo-relative spelling. This
        # function's "$rel" arguments are already ".loom/"-relative (e.g.
        # "scripts/foo.sh"), but a pin written the way a human would
        # naturally type it — repo-relative, e.g. ".loom/scripts/foo.sh" or
        # "./.loom/scripts/foo.sh" — never matched that exact-equality check
        # and was therefore a silent, permanent no-op. Strip one leading
        # "./" and then one leading ".loom/" from the pin and retry; this is
        # purely additive (the exact match above still fires first) so it
        # cannot change the outcome for any pin that already worked, e.g. the
        # ".loom/CLAUDE.md" and ".loom/biome.jsonc" pins whose "$rel" values
        # already carry that same ".loom/" prefix verbatim.
        #
        # The retry requires the normalized form to still contain a "/".
        # Every surface this normalization is meant for is nested
        # (hooks/scripts/roles/docs/bin/commands), but a few call sites
        # compare against a bare top-level rel with no directory component
        # at all, e.g. "package.json" (the root workspace stub, #4285).
        # Without this guard, a pin of ".loom/package.json" would strip down
        # to "package.json" and silently start matching that unrelated root
        # file — the same class of silent misfire this PR exists to
        # eliminate, just reintroduced by the normalization itself (caught
        # in review: a sibling PR adds an analogous bare "CLAUDE.md" rel for
        # the root guide, which would collide with a ".loom/CLAUDE.md" pin
        # the same way). Requiring a "/" in the normalized form keeps every
        # real nested-path case working (e.g. "hooks/foo.sh") while
        # refusing to collapse onto a bare top-level identifier.
        normalized="${line#./}"
        normalized="${normalized#.loom/}"
        if [[ "$normalized" != "$line" && "$normalized" == */* && "$normalized" == "$rel" ]]; then
            PIN_HIT["$line"]=1
            return 0
        fi
    done < "$IGNORE_FILE"
    return 1
}

# #6515: warn loudly, once per run, for every `.loom/resync-ignore` entry
# that matched nothing this run — either a typo or a pin for a file upstream
# has since retired. Either way the pin is doing nothing, and previously that
# was silent forever: the pin *looks* installed and does nothing. Must run
# AFTER the full walk (every is_ignored call site has already populated
# SEEN_RELS/PIN_HIT by then).
report_dead_pins() {
    [[ -f "$IGNORE_FILE" ]] || return 0
    local line base rel closest=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" ]] && continue
        [[ -n "${PIN_HIT[$line]:-}" ]] && continue

        # "did you mean" hint: the walked "$rel" (if any) sharing this pin's
        # basename — cheap and good enough to catch the common cases (a
        # path-form mismatch fix 1 above didn't cover, or a one-directory
        # typo) without pulling in a real fuzzy-match dependency.
        base="${line##*/}"
        closest=""
        for rel in "${!SEEN_RELS[@]}"; do
            if [[ "${rel##*/}" == "$base" ]]; then
                closest="$rel"
                break
            fi
        done

        if [[ -n "$closest" ]]; then
            printf '%b\n' "${YELLOW}${BOLD}[resync] pin had no effect: '${line}' (did you mean '${closest}'?)${NC}"
        else
            printf '%b\n' "${YELLOW}${BOLD}[resync] pin had no effect: '${line}' (no file matched this run — typo, or retired upstream?)${NC}"
        fi
    done < "$IGNORE_FILE"
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
# #5981: retired payload files removed this run (see remove_retired_files()).
N_REMOVED=0
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
# #6138: a plain `trap ... EXIT` REPLACES any prior EXIT trap rather than
# stacking with it, so this combined handler folds in remove_staging_worktree
# (installed further up, right after the staging worktree is created) instead
# of clobbering it — both cleanups always run on any exit from here on,
# whether normal, an early `exit`, or a signal.
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup_on_exit() {
    cleanup_staged_tmp
    remove_staging_worktree
}
trap cleanup_on_exit EXIT
trap 'cleanup_on_exit; exit 130' INT
trap 'cleanup_on_exit; exit 143' TERM
trap 'cleanup_on_exit; exit 129' HUP

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
    info "Resyncing ${dst_dir#"$WRITE_ROOT/"}/ from ${src_dir#"$REPO_ROOT/"}/ ..."
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

# ---------- retired payload files (#5981) ----------
#
# The walks above (sync_one/resync_tree) only ever visit files that exist
# under defaults/ TODAY — a file retired upstream (deleted from defaults/
# entirely) has no source counterpart to walk from, so it is silently never
# noticed and survives forever in every already-installed repo. This is the
# delete-side counterpart: defaults/.loom-retired.list declaratively names
# every target-relative path (report-relative form, e.g. "scripts/status.sh")
# that WAS Loom payload and has since been removed from defaults/.
#
# retired_target_path() maps a retired-list entry back to its destination
# path using EXACTLY the same report_prefix -> destination directory mapping
# the walks above use (hooks -> .loom/hooks, scripts -> .loom/scripts,
# roles -> .loom/roles, docs -> .loom/docs, runtimes -> .loom/runtimes,
# bin -> .loom/bin, commands/loom -> .claude/commands/loom, plus the two
# single-file consumer-install docs, #5264). An entry that matches none of
# these prefixes maps to "" and is skipped rather than guessed at.
retired_target_path() {
    local rel="$1"
    case "$rel" in
        hooks/*)                  printf '%s/%s' "$INSTALLED_HOOKS" "${rel#hooks/}" ;;
        scripts/*)                printf '%s/%s' "$INSTALLED_SCRIPTS" "${rel#scripts/}" ;;
        roles/*)                  printf '%s/.loom/roles/%s' "$WRITE_ROOT" "${rel#roles/}" ;;
        docs/*)                   printf '%s/.loom/docs/%s' "$WRITE_ROOT" "${rel#docs/}" ;;
        runtimes/*)                printf '%s/.loom/runtimes/%s' "$WRITE_ROOT" "${rel#runtimes/}" ;;
        bin/*)                    printf '%s/.loom/bin/%s' "$WRITE_ROOT" "${rel#bin/}" ;;
        commands/loom/*)          printf '%s/.claude/commands/loom/%s' "$WRITE_ROOT" "${rel#commands/loom/}" ;;
        .claude/README.md)        printf '%s/.claude/README.md' "$WRITE_ROOT" ;;
        .github/CONFIGURATION.md) printf '%s/.github/CONFIGURATION.md' "$WRITE_ROOT" ;;
        *)                        printf '' ;;
    esac
}

# remove_retired_files: reads defaults/.loom-retired.list (if present) and
# removes each listed path that is still present in the installed tree,
# reporting it with the `removed` verb. A missing/absent-here destination is
# a silent no-op (nothing to remove — the common steady state once a repo
# has caught up). Honors the SAME `.loom/resync-ignore` pin and
# destination-symlink guard sync_one applies, so a consumer that
# deliberately kept a fork of a retired file is never touched, and a
# dogfood-style symlinked install target is never unlinked out from under
# its source of truth.
remove_retired_files() {
    local list="$DEFAULTS_DIR/.loom-retired.list"
    [[ -f "$list" ]] || return 0
    local line rel dst
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" ]] && continue
        rel="$line"
        dst="$(retired_target_path "$rel")"
        [[ -n "$dst" ]] || continue
        [[ -e "$dst" || -L "$dst" ]] || continue

        if is_ignored "$rel"; then
            note "  ${YELLOW}skipped${NC}   $rel ${YELLOW}(pinned in .loom/resync-ignore)${NC}"
            N_SKIPPED=$((N_SKIPPED + 1))
            continue
        fi

        if [[ -L "$dst" ]]; then
            note "  ${YELLOW}skipped${NC}   $rel ${YELLOW}(symlink -> $(readlink "$dst" 2>/dev/null), not removed)${NC}"
            N_SKIPPED=$((N_SKIPPED + 1))
            continue
        fi

        if [[ "$DRY_RUN" -eq 1 ]]; then
            printf '%b\n' "  ${BOLD}would remove${NC} $rel ${YELLOW}(retired from defaults/, #5981)${NC}"
            N_REMOVED=$((N_REMOVED + 1))
            continue
        fi

        if rm -f "$dst" 2>/dev/null; then
            printf '%b\n' "  ${GREEN}removed${NC}   $rel ${YELLOW}(retired from defaults/, #5981)${NC}"
            N_REMOVED=$((N_REMOVED + 1))
        else
            err "failed to remove retired file $rel"
            record_failure "$rel"
        fi
    done < "$list"
}

# ---------- canonical Repo Skills guard detection (#4041, #4894, #5916, #5974) ----------
#
# When the canonical generic guard is installed in this repo AND passes ALL
# FOUR runtime probes the guard-destructive.sh dispatcher requires — the
# rjwalters/repo#29 VERSION marker, the `worktree-write-confinement`
# CAPABILITY marker (proving it actually implements the Loom-only Bash-tool
# write-confinement category, issue #4178, not just the unrelated repo#29 fix),
# the `--comment|--search` / `--arg|--argjson` CAPABILITY markers (proving
# it actually masks `gh --search`/`jq --arg`/`--argjson` quoted values before
# the catastrophic/ask scans, issue #5916, not just the unrelated
# version/write-confinement fixes), and the `gh-comment-body-literal-at`
# CAPABILITY marker (proving it actually carries the `--body @path`
# literal-string hard deny, issue #4523/#5974, not just the unrelated
# version/write-confinement/search-mask fixes) — Loom's vendored generic guard
# (guard-destructive-generic.sh) is intentionally NOT installed — the
# guard-destructive.sh dispatcher defers to the canonical guard at runtime.
# Resync must therefore neither resurrect the vendored copy nor leave a stale
# one behind. Same four-probe check the dispatcher/installer use, so all
# four agree on which guard wins (#4894: requiring only the version probe
# here would strip the vendored fallback out from under the dispatcher the
# moment a canonical guard picked up repo#29 without write-confinement,
# leaving zero coverage instead of the intended fallback; #5916 closes the
# same class of gap for the search/jq masking capability; #5974 closes it
# again for the --body @path hard-deny capability — see
# defaults/hooks/guard-destructive.sh's header comment for why a single
# `gh-comment-body-literal-at` marker is an adequate proxy for that whole
# rule family rather than a probe per decision-tag).
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
   grep -q 'worktree-write-confinement' "$REPO_ROOT/.claude/skills/repo/hooks/guard-destructive.sh" 2>/dev/null && \
   grep -qF -- '--comment|--search' "$REPO_ROOT/.claude/skills/repo/hooks/guard-destructive.sh" 2>/dev/null && \
   grep -qF -- '--arg|--argjson' "$REPO_ROOT/.claude/skills/repo/hooks/guard-destructive.sh" 2>/dev/null && \
   grep -q -- 'gh-comment-body-literal-at' "$REPO_ROOT/.claude/skills/repo/hooks/guard-destructive.sh" 2>/dev/null; then
    CANONICAL_GUARD_PRESENT=1
fi

# ---------- crash-detection marker (#5980) ----------
#
# See the "CRASH-DETECTION MARKER" header comment above for the full
# rationale. Everything above this point only ever READS (git/file-system
# probes, arg parsing) — this is the first point in the script where a write
# is about to happen, so it is where the in-progress marker is written, and
# where a leftover marker from a run that never got this far is reported.

RESYNC_MARKER="$WRITE_ROOT/.loom/.resync-in-progress"

# Detects (and reports) a marker left behind by a run that crashed before
# reaching clear_resync_marker(). Runs on EVERY invocation, including
# --dry-run, so `resync-installed.sh --dry-run` doubles as a side-effect-free
# way to check "did the last resync actually finish?" per #5980's suggested
# acceptance criteria.
check_resync_marker() {
    [[ -f "$RESYNC_MARKER" ]] || return 0
    local prior_version prior_started
    prior_version="$(sed -n 's/^target_version=//p' "$RESYNC_MARKER" 2>/dev/null | head -1)"
    prior_started="$(sed -n 's/^started_at=//p' "$RESYNC_MARKER" 2>/dev/null | head -1)"
    warn "A previous resync did not complete (targeting v${prior_version:-unknown}, started ${prior_started:-an unknown time}) — .loom/ may be left half-updated while install-metadata.json still reports the OLD version (#5980)."
    warn "  This run will restart from scratch; resync-installed.sh is idempotent, so already-current files are simply reported unchanged, not redone."
}
check_resync_marker

# Writes the marker with the version this run is targeting, before the first
# sync_one call below. --dry-run never writes it (a preview makes no claim to
# be "in progress"). A write failure degrades crash detection for this run
# only — it must never block the sync itself.
write_resync_marker() {
    [[ "$DRY_RUN" -eq 1 ]] && return 0
    local version
    version="$(read_source_version)"
    if ! {
        printf 'target_version=%s\n' "$version"
        printf 'started_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'pid=%s\n' "$$"
    } > "$RESYNC_MARKER" 2>/dev/null; then
        warn "Could not write the resync-in-progress marker at $RESYNC_MARKER (crash detection for this run is degraded; the sync itself still proceeds)."
    fi
}
write_resync_marker

# Cleared as soon as a full, non-partial success is known (right after
# N_FAILED is finalized, before the .gitignore refresh / untracked-path audit
# run — see the call site below) — never on a PARTIAL refresh or a crash.
clear_resync_marker() {
    [[ "$DRY_RUN" -eq 1 ]] && return 0
    rm -f "$RESYNC_MARKER" 2>/dev/null || true
}

# ---------- walk hooks (top-level *.sh, matching the installer) ----------

if [[ -d "$DEFAULTS_DIR/hooks" && -d "$INSTALLED_HOOKS" ]]; then
    info "Resyncing .loom/hooks/ from ${DEFAULTS_DIR#"$REPO_ROOT/"}/hooks/ ..."
    shopt -s nullglob
    for src in "$DEFAULTS_DIR/hooks/"*.sh; do
        name="$(basename "$src")"
        # The vendored generic guard is conditional on the canonical guard (#4041).
        if [[ "$name" == "guard-destructive-generic.sh" && "$CANONICAL_GUARD_PRESENT" -eq 1 ]]; then
            if [[ -f "$INSTALLED_HOOKS/$name" ]]; then
                if git -C "$WRITE_ROOT" ls-files --error-unmatch -- ".loom/hooks/$name" >/dev/null 2>&1; then
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

if [[ -d "$WRITE_ROOT/.loom/roles" ]]; then
    resync_tree "$DEFAULTS_DIR/roles" "$WRITE_ROOT/.loom/roles" "roles" "roles"
fi
if [[ -d "$WRITE_ROOT/.loom/docs" ]]; then
    resync_tree "$DEFAULTS_DIR/docs" "$WRITE_ROOT/.loom/docs" "docs" "docs"
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
resync_tree "$DEFAULTS_DIR/runtimes" "$WRITE_ROOT/.loom/runtimes" "runtimes" "runtimes"
if [[ -d "$WRITE_ROOT/.loom/bin" ]]; then
    resync_tree "$DEFAULTS_DIR/.loom/bin" "$WRITE_ROOT/.loom/bin" "bin" ".loom/bin"
fi
if [[ -d "$WRITE_ROOT/.claude/commands/loom" ]]; then
    resync_tree "$DEFAULTS_DIR/.claude/commands/loom" "$WRITE_ROOT/.claude/commands/loom" "commands/loom" ".claude/commands/loom"
fi

# ---------- single-file consumer-install docs (#5264) ----------
#
# .claude/README.md and .github/CONFIGURATION.md are copied verbatim into
# every consumer repo at install time (scripts/install/manifest.sh) but, prior
# to #5264, were never covered by this script's surface map — a fix to either
# file landed on main but never reached an already-installed repo. Both are
# single files (not directories), so resync_tree doesn't apply; sync_one
# handles them directly, each gated on the destination already existing so a
# consumer that never received the file (or deliberately removed it) is not
# force-populated.
if [[ -f "$WRITE_ROOT/.claude/README.md" ]]; then
    sync_one "$DEFAULTS_DIR/.claude/README.md" "$WRITE_ROOT/.claude/README.md" ".claude/README.md"
fi
if [[ -f "$WRITE_ROOT/.github/CONFIGURATION.md" ]]; then
    sync_one "$DEFAULTS_DIR/.github/CONFIGURATION.md" "$WRITE_ROOT/.github/CONFIGURATION.md" ".github/CONFIGURATION.md"
fi

# ---------- single-file nested Biome configs (#6031) ----------
#
# `.loom/biome.jsonc` and `.claude/biome.jsonc` take the Loom-managed paths out
# of a consumer's repo-wide `biome check .` — without them the shipped
# Workflow-tool experiment script is a hard PARSE error and the installer's JSON
# stamps are perpetual format diffs, in files the consumer never wrote.
#
# Deliberately UNCONDITIONAL (like `.loom/runtimes/` above, #4688) rather than
# gated on the destination already existing: these are NEW payload files, so
# every repo installed before #6031 has no copy and would never obtain one from
# a destination-gated sync. `sync_one` still honors `.loom/resync-ignore` and
# still refuses to clobber a symlinked target, so a consumer who deliberately
# forked or pinned either file is untouched.
#
# Each call is gated on the SOURCE existing (not the destination) so a resync
# run against an older `defaults/` checkout is a clean no-op rather than a
# `cp`-failure.
if [[ -f "$DEFAULTS_DIR/.loom/biome.jsonc" ]]; then
    sync_one "$DEFAULTS_DIR/.loom/biome.jsonc" "$WRITE_ROOT/.loom/biome.jsonc" ".loom/biome.jsonc"
fi
if [[ -f "$DEFAULTS_DIR/.claude/biome.jsonc" ]]; then
    sync_one "$DEFAULTS_DIR/.claude/biome.jsonc" "$WRITE_ROOT/.claude/biome.jsonc" ".claude/biome.jsonc"
fi

# ---------- remove retired payload files (#5981) ----------
#
# Every surface above has now been walked, so it's safe to prune files that
# no longer have ANY source counterpart because they were deliberately
# retired (see "RETIRED PAYLOAD FILES" in the header and remove_retired_files()
# above) — as opposed to a repo-specific file with no source counterpart,
# which the walks above already leave untouched by construction.
remove_retired_files

# ---------- install the deferred self-copy, now that every surface settled ----
#
# The scripts walk above records (rather than applies) a sync of the script this
# process is executing; apply it here, last, via the same atomic staging path.
apply_deferred_self_sync

# ---------- clear the #5980 crash-detection marker (as soon as it is safe) ----
#
# N_FAILED is now fully determined — every sync_one/remove_retired_files call
# that could ever record a failure has already run above, and nothing below
# this point writes a payload file. Clear the marker HERE, before
# refresh_gitignore_block()/audit_untracked_loom_paths() run, rather than
# waiting for the very end of the script: those two read the CURRENT
# untracked-and-unignored state of .loom/, and the marker itself is
# untracked-and-unignored by construction until a consumer's installed
# .gitignore has caught up to this fix — leaving it in place through the
# audit would make a routine, fully successful run spuriously warn about its
# own transient control file. A PARTIAL refresh (N_FAILED > 0) intentionally
# skips this — the marker must survive so the crash/partial state stays
# detectable, exactly as the final summary block does at the bottom of the
# script.
[[ "$DRY_RUN" -eq 1 || "$N_FAILED" -gt 0 ]] || clear_resync_marker

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
    local pj="$WRITE_ROOT/package.json"
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
#
# (read_source_version() moved up to right after SOURCE_ROOT is resolved,
# #5980 — the crash-detection marker needs it before this section runs.)

# ---------- targeted field edit: .loom/CLAUDE.md version header (#5559) ----------
#
# .loom/CLAUDE.md is the full vendored guide, generated ONCE from
# defaults/.loom/CLAUDE.md's {{LOOM_VERSION}}/{{INSTALL_DATE}} template by
# install-time scaffolding (`loom-daemon init`) — not by this script (see the
# "EXPLICITLY OUT OF SCOPE" header comment above). Because resync never
# touches it, but DOES keep .loom/install-metadata.json's loom_version current
# every run (restamp_metadata() below), the two stamps silently drift apart:
# install-metadata.json always reports the freshly-resynced version while the
# guide's own "**Loom Version**" header keeps showing whatever version was
# installed originally. scripts/install-loom.sh's idempotency check prefers
# install-metadata.json, so it never notices the stale header (#5559).
#
# Rather than regenerating the whole file (which would need the
# managed-section-markers design the OUT OF SCOPE comment references, and
# .loom/CLAUDE.md is not otherwise repo-customized so a full regenerate is
# arguably safe but out of this fix's scope), this does a targeted field edit
# of just the "**Loom Version**" and "Last updated" lines — mirroring the
# resync_workspace_stub_version() pattern above. The "**Installation Date**"
# header line is deliberately left untouched: it records the ORIGINAL install
# date, not a last-touched date, and resync has no business rewriting that.
resync_claude_md_version_header() {
    local target="$WRITE_ROOT/.loom/CLAUDE.md"
    [[ -f "$target" ]] || return 0  # pre-#4239 layout: nothing to restamp

    if is_ignored ".loom/CLAUDE.md"; then
        note "  ${YELLOW}skipped${NC}   .loom/CLAUDE.md ${YELLOW}(pinned in .loom/resync-ignore)${NC}"
        N_SKIPPED=$((N_SKIPPED + 1))
        return 0
    fi

    local version current_version
    version="$(read_source_version)"
    if [[ "$version" == "unknown" ]]; then
        warn "Skipped .loom/CLAUDE.md version-header restamp (could not resolve source version). Surface sync still applied."
        return 0
    fi
    current_version="$(sed -n 's/^\*\*Loom Version\*\*: *//p' "$target" | head -1)"

    if [[ "$current_version" == "$version" ]]; then
        note "  ${GREEN}unchanged${NC} .loom/CLAUDE.md (version header already v${version})"
        N_UNCHANGED=$((N_UNCHANGED + 1))
        return 0
    fi

    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf '%b\n' "  ${BOLD}would update${NC} .loom/CLAUDE.md (restamp version header ${current_version:-unknown} -> ${version}, #5559)"
        N_UPDATED=$((N_UPDATED + 1))
        return 0
    fi

    local today tmp
    today="$(date +%Y-%m-%d)"
    tmp="${target}.tmp.$$"
    if sed -e "s/^\*\*Loom Version\*\*: .*/**Loom Version**: ${version}/" \
           -e "s/^Last updated: .*/Last updated: ${today}/" \
           "$target" > "$tmp" 2>/dev/null && [[ -s "$tmp" ]]; then
        mv "$tmp" "$target"
        printf '%b\n' "  ${GREEN}updated${NC}   .loom/CLAUDE.md (restamped version header ${current_version:-unknown} -> ${version}, #5559)"
        N_UPDATED=$((N_UPDATED + 1))
    else
        rm -f "$tmp"
        err "failed to restamp .loom/CLAUDE.md version header"
    fi
}

resync_claude_md_version_header

# ---------- targeted field edit: root CLAUDE.md version header (#6612) ----------
#
# Root CLAUDE.md (as opposed to .loom/CLAUDE.md, the full vendored guide
# handled by resync_claude_md_version_header() above) is the repo-customized
# operating-core guide every consumer repo gets scaffolded at install time and
# then freely hand-edits (see the "EXPLICITLY OUT OF SCOPE" header comment
# above) -- resync never regenerates or otherwise touches it. But exactly like
# .loom/CLAUDE.md before #5559, its own "**Loom Version**" / "Last updated"
# header lines silently drift from install-metadata.json's loom_version after
# every resync, since nothing else re-stamps them (#6612).
#
# Mirrors resync_claude_md_version_header() closely: same is_ignored /
# read_source_version / dry-run / counter handling, but targets root
# CLAUDE.md instead of .loom/CLAUDE.md, and -- just like that function --
# touches ONLY the version-line field. Root CLAUDE.md remains genuinely
# repo-customized everywhere else, so a full regenerate (or a broader
# managed-section-markers design) is unsafe and out of scope for this fix,
# exactly as #5559 scoped .loom/CLAUDE.md's fix narrowly rather than doing a
# full regenerate. The "**Installation Date**" header line is deliberately
# left untouched for the same reason as .loom/CLAUDE.md: it records the
# ORIGINAL install date, not a last-touched date.
resync_root_claude_md_version_header() {
    local target="$WRITE_ROOT/CLAUDE.md"
    [[ -f "$target" ]] || return 0  # no root CLAUDE.md installed: nothing to restamp

    if is_ignored "CLAUDE.md"; then
        note "  ${YELLOW}skipped${NC}   CLAUDE.md ${YELLOW}(pinned in .loom/resync-ignore)${NC}"
        N_SKIPPED=$((N_SKIPPED + 1))
        return 0
    fi

    local version current_version
    version="$(read_source_version)"
    if [[ "$version" == "unknown" ]]; then
        warn "Skipped CLAUDE.md version-header restamp (could not resolve source version). Surface sync still applied."
        return 0
    fi
    current_version="$(sed -n 's/^\*\*Loom Version\*\*: *//p' "$target" | head -1)"

    # No Loom version header in this root CLAUDE.md at all: nothing to
    # restamp. Return BEFORE the sed below, so we neither rewrite the file
    # nor count a phantom N_UPDATED, and never touch an unrelated
    # `Last updated:` line that has nothing to do with a Loom version header.
    if [[ -z "$current_version" ]]; then
        return 0
    fi

    if [[ "$current_version" == "$version" ]]; then
        note "  ${GREEN}unchanged${NC} CLAUDE.md (version header already v${version})"
        N_UNCHANGED=$((N_UNCHANGED + 1))
        return 0
    fi

    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf '%b\n' "  ${BOLD}would update${NC} CLAUDE.md (restamp version header ${current_version:-unknown} -> ${version}, #6612)"
        N_UPDATED=$((N_UPDATED + 1))
        return 0
    fi

    local today tmp
    today="$(date +%Y-%m-%d)"
    tmp="${target}.tmp.$$"
    if sed -e "s/^\*\*Loom Version\*\*: .*/**Loom Version**: ${version}/" \
           -e "s/^Last updated: .*/Last updated: ${today}/" \
           "$target" > "$tmp" 2>/dev/null && [[ -s "$tmp" ]]; then
        mv "$tmp" "$target"
        printf '%b\n' "  ${GREEN}updated${NC}   CLAUDE.md (restamped version header ${current_version:-unknown} -> ${version}, #6612)"
        N_UPDATED=$((N_UPDATED + 1))
    else
        rm -f "$tmp"
        err "failed to restamp CLAUDE.md version header"
    fi
}

resync_root_claude_md_version_header

restamp_metadata() {
    local meta="$WRITE_ROOT/.loom/install-metadata.json"
    [[ -f "$meta" ]] || return 0

    local version commit today tmp remote
    version="$(read_source_version)"
    commit="$(git -C "$SOURCE_ROOT" rev-parse --short HEAD 2>/dev/null || echo "unknown")"
    today="$(date +%Y-%m-%d)"
    # Refresh loom_source_remote (#6780 AC3) from the SOURCE_ROOT this resync
    # actually resolved to, so it tracks a repointed sidecar rather than
    # freezing whatever was recorded at install time. Best-effort: empty when
    # SOURCE_ROOT isn't a git checkout or has no `origin` configured.
    remote="$(git -C "$SOURCE_ROOT" remote get-url origin 2>/dev/null || true)"
    tmp="${meta}.tmp.$$"

    if command -v jq >/dev/null 2>&1; then
        if jq --arg v "$version" --arg c "$commit" --arg r "$today" --arg src "$remote" \
              '.loom_version=$v | .loom_commit=$c | .last_resync=$r | .loom_source_remote=$src | del(.loom_source)' \
              "$meta" > "$tmp" 2>/dev/null && [[ -s "$tmp" ]]; then
            mv "$tmp" "$meta"
            note "  ${GREEN}re-stamped${NC} install-metadata.json (loom_version=$version, loom_commit=$commit, last_resync=$today)"
            return 0
        fi
        rm -f "$tmp"
    fi

    if command -v python3 >/dev/null 2>&1; then
        if META="$meta" VERSION="$version" COMMIT="$commit" TODAY="$today" REMOTE="$remote" \
           python3 - "$tmp" <<'PY' 2>/dev/null && [[ -s "$tmp" ]]; then
import json, os, sys
with open(os.environ["META"]) as f:
    data = json.load(f)
data["loom_version"] = os.environ["VERSION"]
data["loom_commit"] = os.environ["COMMIT"]
data["last_resync"] = os.environ["TODAY"]
data["loom_source_remote"] = os.environ["REMOTE"]
data.pop("loom_source", None)
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
    local ga="$WRITE_ROOT/.gitattributes"
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
    current="$(git -C "$WRITE_ROOT" config --get merge.ours.driver 2>/dev/null || true)"
    if [[ "$current" != "true" ]]; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            note "  ${BOLD}would set${NC} local git config merge.ours.driver=true"
        else
            git -C "$WRITE_ROOT" config merge.ours.driver true 2>/dev/null || true
            changed=1
        fi
    fi

    if [[ "$changed" -eq 1 ]]; then
        note "  ${GREEN}configured${NC} install-metadata.json merge=ours driver (.gitattributes + local git config)"
    fi
}
ensure_install_metadata_merge_driver

# #5294: EPHEMERAL_PATTERNS is single-sourced in
# loom-daemon/src/init/post_init.rs. Extract the pattern list directly from
# that source file (not from a compiled binary) so refresh_gitignore_block can
# verify the freshly-regenerated .gitignore against ground truth, independent
# of which loom-daemon binary happened to write it. Best-effort: echoes
# nothing (not a failure) if $SOURCE_ROOT isn't a full checkout with the daemon
# crate present (e.g. a stripped-down install source).
_gitignore_source_ephemeral_patterns() {
    local post_init="$SOURCE_ROOT/loom-daemon/src/init/post_init.rs"
    [[ -f "$post_init" ]] || return 0
    awk '/pub const EPHEMERAL_PATTERNS/ { flag=1; next } flag && /^\];/ { exit } flag' "$post_init" \
        | sed -n -E 's/^[[:space:]]*"([^"]*)",?[[:space:]]*$/\1/p'
}

# #5991: rewrite the Loom-managed `.gitignore` block in place from the given
# SOURCE pattern list (ground truth, independent of whichever loom-daemon
# binary wrote the block), preserving everything outside the block untouched.
# $1 is the block as previously extracted by the caller (used only to locate
# the exact begin/end marker lines and the header comment already present in
# the file -- NOT its pattern lines, which are fully replaced); the remaining
# args are the correct pattern list, in source declaration order. Returns 1
# (no write performed) if the markers can't be located in $1 or no patterns
# were given, so the caller can fall back to a coarser recovery.
_gitignore_restore_managed_block() {
    local block="$1"; shift
    local gitignore="$WRITE_ROOT/.gitignore"
    local begin_marker end_marker header
    begin_marker="$(head -n1 <<<"$block")"
    end_marker="$(tail -n1 <<<"$block")"
    header="$(sed -n '2p' <<<"$block")"
    [[ -n "$begin_marker" && -n "$end_marker" && -n "$header" ]] || return 1
    [[ "$#" -gt 0 ]] || return 1
    [[ -f "$gitignore" ]] || return 1

    local tmp
    tmp="$(mktemp "${gitignore}.XXXXXX")" || return 1
    local in_block=0 replaced=0 line pattern
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$in_block" -eq 0 && "$line" == "$begin_marker" ]]; then
            in_block=1
            replaced=1
            printf '%s\n' "$begin_marker" >>"$tmp"
            printf '%s\n' "$header" >>"$tmp"
            for pattern in "$@"; do
                printf '%s\n' "$pattern" >>"$tmp"
            done
            continue
        fi
        if [[ "$in_block" -eq 1 ]]; then
            if [[ "$line" == "$end_marker" ]]; then
                in_block=0
                printf '%s\n' "$end_marker" >>"$tmp"
            fi
            continue
        fi
        printf '%s\n' "$line" >>"$tmp"
    done <"$gitignore"

    if [[ "$replaced" -eq 1 ]]; then
        mv "$tmp" "$gitignore"
        return 0
    fi
    rm -f "$tmp"
    return 1
}

# #5294 / #5991: verify the managed block `$bin update-gitignore` just wrote
# actually contains every pattern the CURRENT source declares. A `loom-daemon`
# binary older than the just-pulled source has EPHEMERAL_PATTERNS compiled in
# from whenever it was built — if that predates a pattern addition (e.g.
# #5280's `.claude/worktrees/`), `update-gitignore` exits 0 having *silently*
# dropped that pattern from the regenerated block. That is precisely how
# 05cf67e8 reintroduced #5267's gitlink hazard 34 minutes after #5280 fixed
# it, and how 94fa30f2 reintroduced it a THIRD time (#5985) even after this
# function existed — because it only warned, never fixed. Detection without
# enforcement has now demonstrably failed once; never trust the exit code
# alone, and never leave the regression for a human to notice in the warning
# scroll: restore the dropped pattern(s) directly from source, or (if that
# isn't possible) revert the whole file, so the regression cannot land.
_gitignore_warn_if_stale() {
    local bin="$1" post_init="$SOURCE_ROOT/loom-daemon/src/init/post_init.rs"
    local -a missing=() source_patterns=()
    local pattern block

    [[ -f "$post_init" ]] || return 0
    [[ -f "$WRITE_ROOT/.gitignore" ]] || return 0

    block="$(sed -n '/# >>> loom-managed/,/# <<< loom-managed/p' "$WRITE_ROOT/.gitignore")"
    [[ -n "$block" ]] || return 0

    while IFS= read -r pattern; do
        [[ -n "$pattern" ]] || continue
        source_patterns+=("$pattern")
        grep -qxF -- "$pattern" <<<"$block" || missing+=("$pattern")
    done < <(_gitignore_source_ephemeral_patterns)

    [[ "${#missing[@]}" -gt 0 ]] || return 0

    warn "The resolved loom-daemon binary ($bin) regenerated .gitignore WITHOUT ${#missing[@]} pattern(s) that $post_init currently declares:"
    for pattern in "${missing[@]}"; do
        warn "    $pattern"
    done
    warn "  '$bin' is likely older than the source just synced (#5294) — rebuild loom-daemon"
    warn "  (cargo build --release -p loom-daemon under $SOURCE_ROOT) and re-run resync-installed.sh."

    if _gitignore_restore_managed_block "$block" "${source_patterns[@]}"; then
        warn "  ${GREEN}restored${NC} the missing pattern(s) directly from $post_init so the regression cannot land (#5991)."
    elif git -C "$WRITE_ROOT" checkout -- .gitignore 2>/dev/null; then
        warn "  Could not rewrite the block in place — ${GREEN}reverted${NC} .gitignore to its last-committed state instead (#5991)."
    else
        warn "  ${RED}Could not restore or revert .gitignore${NC} — the regressed rewrite is still in place; fix manually before committing (#5991)."
    fi
}

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
    # #5294: this resync runs specifically because source just changed, so for
    # THIS call only, hoist the resolver's normally-opt-in $LOOM_PREFER_REPO_BUILD=1
    # precedence (repo build ahead of PATH / the machine-level install) -- a
    # stale PATH/machine-level binary can predate a just-merged
    # EPHEMERAL_PATTERNS entry and silently drop it from the regenerated block
    # (exactly what happened in 05cf67e8, 34 minutes after #5280 added
    # `.claude/worktrees/`). An explicit $LOOM_DAEMON_BIN still wins regardless
    # -- loom_locate_daemon_bin checks it before this precedence tier. Scoped to
    # this one call via a subshell env override; the library's own default
    # (off) is unchanged for loom-daemon-start.sh and other production callers.
    bin="$(LOOM_PREFER_REPO_BUILD=1 loom_locate_daemon_bin "$SOURCE_ROOT")"
    if [[ -z "$bin" ]]; then
        warn "Could not refresh the Loom-managed .gitignore block: no loom-daemon binary resolved"
        warn "  (\$LOOM_DAEMON_BIN -> repo build under $SOURCE_ROOT -> 'loom-daemon' on PATH -> machine-level install)."
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
    if "$bin" update-gitignore "$WRITE_ROOT" >/dev/null 2>&1; then
        note "  ${GREEN}refreshed${NC} .gitignore (loom-managed block, via $bin)"
    else
        warn ".gitignore refresh failed: '$bin update-gitignore' errored"
        warn "  (a pre-#4280 daemon lacks this subcommand — rebuild loom-daemon)."
        return 0
    fi
    # #5294: defense in depth -- verify the write actually landed every pattern
    # the source declares, even after preferring a repo build above (that repo
    # build itself might be stale, or absent so resolution fell through to
    # PATH/machine-level anyway).
    _gitignore_warn_if_stale "$bin"
}
refresh_gitignore_block

# ---------- shared: pure-copy-surface path classifier (#5983, #6173) ----------
#
# Both audit_untracked_loom_paths() (below) and suggest_commit_if_resync_only_dirt()
# (further down) need to tell shipped-payload paths -- pure copies of
# defaults/{hooks,scripts,roles,docs,runtimes,bin}/, plus the individual
# single-file payloads synced verbatim by the "single-file nested Biome
# configs (#6031)" step above -- apart from genuine runtime state living
# elsewhere under .loom/. Single-source the list here so the two call sites
# can never drift out of sync with each other.
#
# `.loom/biome.jsonc` is shipped payload (a verbatim copy of
# defaults/.loom/biome.jsonc, applied by sync_one above), not Loom runtime
# state -- on a consumer's first resync to a version that ships it, the file
# lands on disk untracked-and-unignored and would otherwise trip
# audit_untracked_loom_paths()'s "add this to EPHEMERAL_PATTERNS" warning
# even though it is a tracked-payload file the consumer should simply commit
# (#6173). `.claude/biome.jsonc` is the same kind of payload but lives
# outside `.loom/`, so it never reaches audit_untracked_loom_paths() (which
# only scans paths under `.loom/`) -- it is classified here anyway so
# suggest_commit_if_resync_only_dirt() (which scans the whole tree) also
# recognizes it as resync-only dirt safe to suggest committing.
_is_loom_pure_copy_surface_path() {
    case "$1" in
        .loom/hooks/*|.loom/scripts/*|.loom/roles/*|.loom/docs/*|.loom/runtimes/*|.loom/bin/*|.loom/biome.jsonc|.claude/biome.jsonc)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# Maps a target-relative path that has already matched
# _is_loom_pure_copy_surface_path() to the defaults/ source path resync would
# have copied it from, and echoes it. Callers only invoke this after that
# match, so the default case below is defensive, not a normal return path
# (#6613 -- audit_untracked_loom_paths() needs this to tell "still-shipped
# payload" apart from "matches the pure-copy-surface path pattern but was
# removed from defaults/ without ever being added to defaults/.loom-retired.list").
_loom_pure_copy_surface_source_path() {
    case "$1" in
        .loom/hooks/*)
            printf '%s\n' "$DEFAULTS_DIR/hooks/${1#.loom/hooks/}"
            ;;
        .loom/scripts/*)
            printf '%s\n' "$DEFAULTS_DIR/scripts/${1#.loom/scripts/}"
            ;;
        .loom/roles/*)
            printf '%s\n' "$DEFAULTS_DIR/roles/${1#.loom/roles/}"
            ;;
        .loom/docs/*)
            printf '%s\n' "$DEFAULTS_DIR/docs/${1#.loom/docs/}"
            ;;
        .loom/runtimes/*)
            printf '%s\n' "$DEFAULTS_DIR/runtimes/${1#.loom/runtimes/}"
            ;;
        .loom/bin/*)
            printf '%s\n' "$DEFAULTS_DIR/.loom/bin/${1#.loom/bin/}"
            ;;
        .loom/biome.jsonc)
            printf '%s\n' "$DEFAULTS_DIR/.loom/biome.jsonc"
            ;;
        .claude/biome.jsonc)
            printf '%s\n' "$DEFAULTS_DIR/.claude/biome.jsonc"
            ;;
        *)
            return 1
            ;;
    esac
}

# ---------- audit: untracked-and-unignored paths under .loom/ (#4280, #5983) ----------
#
# After the block refresh, anything STILL surfacing as untracked-and-unignored
# under .loom/ needs a remedy -- but which remedy depends on what the path IS.
# A path under a pure-copy surface (.loom/hooks|scripts|roles|docs|runtimes|bin/)
# that still exists under the corresponding defaults/ subdirectory today is
# shipped payload: it arrived via resync from defaults/, so the fix is simply
# to commit it. A path matching that same pure-copy-surface *pattern* but with
# no defaults/ counterpart today is presumed retired -- removed from defaults/
# without ever being added to defaults/.loom-retired.list (#5981), so
# remove_retired_files() never got a chance to clean it up before this audit
# ran -- and committing it would permanently ship dead code into the consumer's
# tree (#6613). Anything outside the pure-copy-surface pattern entirely is
# presumed genuine Loom runtime state the EPHEMERAL_PATTERNS list does not yet
# cover (an enumerated list always trails reality) -- surface it as a warning
# so it can be added there, instead of silently dirtying the consumer's `git
# status` (or being swept into a commit by `git add -A`). `git status
# --porcelain` already excludes ignored files, so every `??` entry here is by
# definition untracked-and-unignored; tracked install-owned files never
# appear, and a path already shadowed by an overbroad gitignore pattern (the
# installer's separate OVERBROAD_LOOM_PATTERNS hard-fail,
# loom-daemon/src/init/post_init.rs) is ignored rather than untracked, so
# `git status` excludes it here too -- it is never double-reported.

audit_untracked_loom_paths() {
    [[ -d "$WRITE_ROOT/.loom" ]] || return 0
    local out
    out="$(git -C "$WRITE_ROOT" status --porcelain -- .loom/ 2>/dev/null | sed -n 's/^?? //p')"
    [[ -z "$out" ]] && return 0

    # Classify every path up front -- each one lands in exactly one bucket --
    # before printing anything, so the remedy sections below never overlap.
    local p src
    local -a payload_paths=()
    local -a retired_paths=()
    local -a runtime_paths=()
    while IFS= read -r p; do
        [[ -z "$p" ]] && continue
        if _is_loom_pure_copy_surface_path "$p"; then
            src="$(_loom_pure_copy_surface_source_path "$p" 2>/dev/null)"
            if [[ -n "$src" && -e "$src" ]]; then
                payload_paths+=("$p")
            else
                retired_paths+=("$p")
            fi
        else
            runtime_paths+=("$p")
        fi
    done <<< "$out"

    if [[ "${#payload_paths[@]}" -gt 0 ]]; then
        warn "Untracked-and-unignored shipped Loom file(s) under .loom/ (these are payload, not runtime state -- commit them):"
        for p in "${payload_paths[@]}"; do
            printf '%b\n' "${YELLOW}    $p${NC}" >&2
        done
    fi

    if [[ "${#retired_paths[@]}" -gt 0 ]]; then
        warn "Untracked-and-unignored Loom file(s) under .loom/ matching a pure-copy surface, but with no defaults/ counterpart today (likely retired, not committed payload):"
        for p in "${retired_paths[@]}"; do
            printf '%b\n' "${YELLOW}    $p${NC}" >&2
        done
        warn "These look retired from defaults/ without a defaults/.loom-retired.list entry -- add one there (or delete the file directly if you are working from the source repo). Do NOT commit them."
    fi

    if [[ "${#runtime_paths[@]}" -gt 0 ]]; then
        warn "Untracked-and-unignored path(s) under .loom/ (not covered by the managed .gitignore block):"
        for p in "${runtime_paths[@]}"; do
            printf '%b\n' "${YELLOW}    $p${NC}" >&2
        done
        warn "If these are Loom runtime state, add them to EPHEMERAL_PATTERNS (loom-daemon/src/init/post_init.rs)."
    fi
}
audit_untracked_loom_paths

# ---------- forge label drift check + safe auto-create (#6716) ----------
#
# .github/labels.yml is kept current by the scripts resync above, but nothing
# previously re-checked whether the TARGET FORGE REPO's LIVE label set still
# matched it after install -- a repo can drift silently forever (kicad-tools
# was missing 3 loom:operator* labels, corrupting a downstream operator
# census tool's bucketing, #6716). sync-labels.sh's --check mode (added
# alongside this) is read-only: it reports every declared label that is
# MISSING or STALE (present, wrong color/description), plus any UNKNOWN
# EXTRA -- a live loom:-prefixed label absent from labels.yml, reported but
# NEVER deleted. When drift is found on a real (non---dry-run) resync, this
# invokes the ALREADY-EXISTING mutating sync-labels.sh run to fix it --
# additive-only (no --prune-defaults passed), so nothing is ever deleted or
# renamed; it only creates what's missing and refreshes what's stale.
#
# Dispatches onto whichever sync-labels.sh ended up installed at
# .loom/scripts/sync-labels.sh by the "walk scripts" resync earlier in this
# run (not a hard-coded defaults/ path), so a repo that pins a customized
# copy via .loom/resync-ignore is checked against ITS OWN sync-labels.sh, not
# a bypassed one.
#
# Best-effort, like the .gitignore refresh above: any forge/lookup failure
# (no git remote, gh missing/unauthenticated, misconfigured Gitea) is a loud
# warning, never a script-aborting failure -- every other surface has already
# fully synced by the time this runs. A repo with no .github/labels.yml (or
# no sync-labels.sh to check with, even after the fallback below) is
# silently skipped -- nothing to check.
#
# Falls back to the SOURCE copy ($DEFAULTS_DIR/scripts/sync-labels.sh) when
# no installed copy exists yet (or it isn't executable) -- e.g. the very
# first --dry-run preview after upgrading past #6716 only PREVIEWS installing
# .loom/scripts/sync-labels.sh (see the "walk scripts" resync above), so it
# is not actually present on disk to invoke yet. WORKTREE_PATH is still
# pinned to $WRITE_ROOT either way (sync-labels.sh cd's into it before doing
# anything), so this fallback never points the check at the wrong repo's
# labels.yml -- only at a different (but functionally current) copy of the
# checking script itself.
LABELS_SYNC_SCRIPT="$WRITE_ROOT/.loom/scripts/sync-labels.sh"
if [[ ! -x "$LABELS_SYNC_SCRIPT" && -x "$DEFAULTS_DIR/scripts/sync-labels.sh" ]]; then
    LABELS_SYNC_SCRIPT="$DEFAULTS_DIR/scripts/sync-labels.sh"
fi
if [[ -f "$WRITE_ROOT/.github/labels.yml" && -x "$LABELS_SYNC_SCRIPT" ]]; then
    check_output=""
    check_rc=0
    check_output="$("$LABELS_SYNC_SCRIPT" --check -- "$WRITE_ROOT" 2>&1)" || check_rc=$?

    case "$check_rc" in
        0)
            note "  ${GREEN}unchanged${NC} forge labels (live label set matches .github/labels.yml)"
            ;;
        3)
            printf '%b\n' "${YELLOW}[resync] Forge label drift detected (.github/labels.yml vs live):${NC}"
            printf '%b\n' "$check_output" | sed 's/^/    /'
            if [[ "$DRY_RUN" -eq 1 ]]; then
                printf '%b\n' "  ${BOLD}would run${NC} sync-labels.sh to create the missing/refresh the stale labels (additive only, never deletes)"
                N_UPDATED=$((N_UPDATED + 1))
            else
                sync_output=""
                sync_rc=0
                sync_output="$("$LABELS_SYNC_SCRIPT" -- "$WRITE_ROOT" 2>&1)" || sync_rc=$?
                if [[ "$sync_rc" -eq 0 ]]; then
                    printf '%b\n' "  ${GREEN}updated${NC}   forge labels (created missing / refreshed stale labels via sync-labels.sh)"
                    N_UPDATED=$((N_UPDATED + 1))
                else
                    warn "sync-labels.sh could not fully apply the label fix (exit $sync_rc) -- live labels may still be missing/stale"
                    printf '%b\n' "$sync_output" | sed 's/^/    /' >&2
                fi
            fi
            ;;
        *)
            warn "Skipped forge label drift check (sync-labels.sh --check exited $check_rc). Surface sync still applied."
            printf '%b\n' "$check_output" | sed 's/^/    /' >&2
            ;;
    esac
fi

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
    status="$(git -C "$WRITE_ROOT" status --porcelain 2>/dev/null)"
    [[ -z "$status" ]] && return 0

    local line path src
    local -a resync_paths=()
    local -a retired_paths=()
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        path="${line:3}"
        [[ "$path" == *" -> "* ]] && path="${path##* -> }"
        path="${path%\"}"
        path="${path#\"}"
        if _is_loom_pure_copy_surface_path "$path"; then
            # #6613 (mirrored from audit_untracked_loom_paths() above): a path
            # matching the pure-copy-surface *pattern* with no defaults/
            # counterpart today is presumed retired-but-unlisted, not shipped
            # payload -- committing it would permanently ship dead code, so it
            # is excluded from the commit suggestion below instead.
            src="$(_loom_pure_copy_surface_source_path "$path" 2>/dev/null)"
            if [[ -n "$src" && -e "$src" ]]; then
                resync_paths+=("$path")
            else
                retired_paths+=("$path")
            fi
            continue
        fi
        case "$path" in
            .claude/commands/loom/*|.claude/README.md|.github/CONFIGURATION.md|.loom/install-metadata.json|.loom/CLAUDE.md|.gitattributes)
                resync_paths+=("$path")
                ;;
            *)
                # Non-resync dirt present — do not suggest a commit that would
                # also stage an unrelated (possibly operator) change.
                return 0
                ;;
        esac
    done <<< "$status"

    if [[ "${#retired_paths[@]}" -gt 0 ]]; then
        warn "Untracked-and-unignored file(s) matching a pure-copy surface, but with no defaults/ counterpart today (likely retired, not committed payload) -- excluded from the commit suggestion below:"
        for path in "${retired_paths[@]}"; do
            printf '%b\n' "${YELLOW}    $path${NC}" >&2
        done
        warn "These look retired from defaults/ without a defaults/.loom-retired.list entry -- add one there (or delete the file directly if you are working from the source repo). Do NOT commit them."
    fi

    [[ "${#resync_paths[@]}" -eq 0 ]] && return 0

    echo ""
    if [[ -n "$OUTPUT_DIR" ]]; then
        note "${BLUE}[resync] The staging worktree is dirty with only resync output above — stage and commit it there:${NC}"
        printf '%b\n' "    ${BOLD}cd $OUTPUT_DIR && git add ${resync_paths[*]} && git commit -m 'chore: resync installed Loom surfaces'${NC}"
    else
        note "${BLUE}[resync] The tree is dirty with only resync output above — stage and commit it so the main-health gate doesn't skip on it:${NC}"
        printf '%b\n' "    ${BOLD}git add ${resync_paths[*]} && git commit -m 'chore: resync installed Loom surfaces'${NC}"
    fi
}
[[ "$DRY_RUN" -eq 1 || "$N_FAILED" -gt 0 ]] || suggest_commit_if_resync_only_dirt

# ---------- next steps for output-dir staging mode (#6106) ----------
#
# Always fires (independent of the loom-source-repo-only dirty-tree hint
# above, which suggest_commit_if_resync_only_dirt gates on DEFAULTS_DIR ==
# $REPO_ROOT/defaults — a condition a general consumer repo's --output run
# would never satisfy) whenever --output produced a real, successful,
# non-dry-run staging worktree, so the operator always gets a concrete "what
# do I do with this now" regardless of repo layout.
print_output_mode_next_steps() {
    [[ -n "$OUTPUT_DIR" && "$STAGING_WORKTREE_CREATED" -eq 1 ]] || return 0
    [[ "$DRY_RUN" -eq 1 ]] && return 0
    [[ "$N_FAILED" -gt 0 ]] && return 0

    echo ""
    note "${GREEN}${BOLD}[resync] Complete resync staged — the primary checkout at $REPO_ROOT was never touched.${NC}"
    note "Review it, then turn it into a commit (and PR) from the staging worktree:"
    printf '%b\n' "    ${BOLD}cd $OUTPUT_DIR${NC}"
    printf '%b\n' "    ${BOLD}git status${NC}   # confirm only expected resync output is dirty"
    printf '%b\n' "    ${BOLD}git checkout -b chore/resync-installed-$(date +%Y%m%d)${NC}"
    printf '%b\n' "    ${BOLD}git add -A && git commit -m 'chore: resync installed Loom surfaces'${NC}"
    printf '%b\n' "    ${BOLD}git push -u origin HEAD${NC}   # then open a PR"
    note "When finished, remove the disposable staging worktree (from the primary checkout, not from inside it):"
    printf '%b\n' "    ${BOLD}git -C $REPO_ROOT worktree remove $OUTPUT_DIR${NC}"
}
print_output_mode_next_steps

# #6515: report dead resync-ignore pins before the summary, in both --dry-run
# and a real apply — every is_ignored() call site above has already run by
# this point, so SEEN_RELS/PIN_HIT are complete for this walk.
report_dead_pins

# ---------- summary ----------

echo ""
if [[ "$DRY_RUN" -eq 1 ]]; then
    # #6106: a preview must leave no residue — remove the staging worktree
    # (created only as this preview's target) before either exit path below.
    remove_staging_worktree
    if [[ "$N_UPDATED" -gt 0 || "$N_REMOVED" -gt 0 ]]; then
        printf '%b\n' "${YELLOW}${BOLD}[resync] DRY RUN: ${N_UPDATED} file(s) would be updated, ${N_REMOVED} would be removed, ${N_UNCHANGED} unchanged, ${N_SKIPPED} skipped.${NC}"
        printf '%b\n' "${YELLOW}Run without --dry-run to apply.${NC}"
        exit 2
    fi
    printf '%b\n' "${GREEN}[resync] DRY RUN: already in sync (${N_UNCHANGED} unchanged, ${N_SKIPPED} skipped).${NC}"
    exit 0
fi

# #6138: past this point both remaining outcomes (a partial refresh below, or
# a clean success at the bottom of the script) intentionally leave a
# completed staging worktree in place for the operator to inspect/commit
# from — so the EXIT-trap cleanup installed above must stand down here rather
# than remove it out from under them.
[[ -n "$OUTPUT_DIR" ]] && KEEP_STAGING_WORKTREE=1

# A failed file makes the refresh PARTIAL — say so explicitly and exit non-zero
# rather than folding it into a success summary (#4669). Nothing is ever left
# half-written (every copy is staged off to the side and renamed), so the
# recovery is simply "fix the cause, re-run".
if [[ "$N_FAILED" -gt 0 ]]; then
    printf '%b\n' "${RED}${BOLD}[resync] PARTIAL REFRESH: ${N_FAILED} file(s) could NOT be synced (${N_UPDATED} updated, ${N_REMOVED} removed, ${N_UNCHANGED} unchanged, ${N_SKIPPED} skipped).${NC}"
    printf '%b\n' "${RED}Failed to sync:${NC}"
    for failed_rel in "${FAILED_RELS[@]}"; do
        printf '%b\n' "${RED}    $failed_rel${NC}"
    done
    printf '%b\n' "${YELLOW}This install is now MIXED: the ${N_UPDATED} file(s) reported above are current, the failed ones are still stale.${NC}"
    printf '%b\n' "${YELLOW}No file was left half-written (each copy is staged beside its destination and renamed atomically),${NC}"
    printf '%b\n' "${YELLOW}so fixing the cause (permissions, disk space, read-only mount) and re-running completes the refresh.${NC}"
    if [[ -n "$OUTPUT_DIR" && "$STAGING_WORKTREE_CREATED" -eq 1 ]]; then
        printf '%b\n' "${YELLOW}The staging worktree at $OUTPUT_DIR was left in place (not removed) so you can inspect it.${NC}"
    fi
    exit 1
fi

# (the #5980 crash-detection marker was already cleared, right after N_FAILED
# was finalized, above — this is just a defensive no-op re-assertion in case a
# future refactor adds another early-return path between the two)
clear_resync_marker

if [[ "$N_UPDATED" -gt 0 || "$N_REMOVED" -gt 0 ]]; then
    printf '%b\n' "${GREEN}${BOLD}[resync] ${N_UPDATED} file(s) updated, ${N_REMOVED} removed, ${N_UNCHANGED} unchanged, ${N_SKIPPED} skipped.${NC}"
else
    printf '%b\n' "${GREEN}[resync] Already in sync (${N_UNCHANGED} unchanged, ${N_SKIPPED} skipped).${NC}"
fi
exit 0
