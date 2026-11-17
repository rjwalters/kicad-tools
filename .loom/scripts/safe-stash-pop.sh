#!/usr/bin/env bash
# safe-stash-pop.sh — pop a stash entry that can NEVER leave conflict markers behind.
#
# WHY THIS EXISTS (#6501, follow-up to #6499/#6502)
#
#   `git stash pop` is not atomic. When the 3-way merge conflicts it:
#     - writes `<<<<<<< Updated upstream` / `=======` / `>>>>>>> Stashed changes`
#       markers into the affected TRACKED files,
#     - leaves unmerged entries in the index,
#     - exits non-zero but KEEPS the stash entry,
#   and then walks away. Nothing verifies the outcome. If the caller does not
#   read the exit status (a script that silences it, an agent that moves on, an
#   operator who gets distracted), the primary checkout is left in an
#   "abandoned conflict state" that looks like ordinary dirt. The next
#   `git add -A && git commit` ships the markers.
#
#   That is exactly what happened in the incident behind #6499/#6502: commit
#   `7d169a06` landed a `.loom/config.json` containing a live conflict-marker
#   block, so the daemon's legacy config tier silently failed to parse
#   fleet-wide. #6499 added periodic *detection* (report-only). This script is
#   the *prevention* half.
#
# CONTRACT
#
#   Either the pop applied cleanly and was VERIFIED clean (exit 0), or the
#   pre-pop working tree is restored byte-for-byte and the stash entry is left
#   intact (exit 3), or nothing was silently swallowed and the failure is
#   reported loudly (exit 4/5). There is no path through this script that ends
#   with unresolved conflict markers sitting quietly in a tracked file.
#
#   NOTHING IS EVER DISCARDED. The rollback only ever runs when the stash entry
#   is confirmed to still exist, and the pre-pop tree is captured first — as a
#   `git stash create` commit anchored under `refs/loom/safe-stash-pop/<stamp>`
#   (never `refs/stash`, so it cannot collide with another worktree's stack, the
#   #4821 hazard). If either of those two preconditions cannot be met, the
#   script refuses to roll back and reports instead: markers in a tracked file
#   are recoverable, destroyed WIP is not.
#
# USAGE
#
#   ./.loom/scripts/safe-stash-pop.sh [OPTIONS] [<stash-ref>]
#
#     <stash-ref>       Entry to pop. Default: stash@{0}.
#
#   Options:
#     --repo <path>     Repository (or any path inside it) to operate on.
#                       Default: the git toplevel of the current directory.
#     --index           Pass `--index` to `git stash pop`, reinstating the
#                       staged/unstaged split the entry recorded. Stricter than
#                       a plain pop: it fails rather than silently flattening a
#                       partial staging (the #3611 rationale in install.sh).
#     --no-restore      On conflict, do NOT roll back — leave the conflicted
#                       tree in place for manual resolution and report (exit 5).
#                       For an operator who wants to resolve the merge by hand.
#     --dry-run         Run every precondition and report what WOULD be popped,
#                       then exit without mutating anything.
#     --json            Emit one structured JSON line on stdout (stderr keeps
#                       the human-readable report).
#     --quiet           Suppress the human-readable report on stderr.
#     --help            Show usage.
#
# EXIT CODES
#
#   0  Pop applied and VERIFIED clean: no unmerged index entries, no newly
#      introduced conflict markers. The stash entry is consumed, as with a
#      normal successful `git stash pop`.
#   1  Precondition failure — not a git repo, unborn HEAD, a merge/rebase/
#      cherry-pick already in progress, an index that ALREADY has unmerged
#      entries, or a dirty tree that could not be snapshotted. Nothing ran.
#   2  Nothing to pop: no stash entry at <stash-ref>.
#   3  Pop conflicted; the pre-pop working tree was RESTORED and verified, and
#      the stash entry is preserved. Safe, expected failure mode.
#   4  Pop conflicted and the pre-pop state could NOT be safely restored (the
#      stash entry vanished, or the rollback itself failed). The tree is left
#      exactly as git left it and the report names every recovery handle. This
#      is the "loudly report rather than risk destroying work" branch.
#   5  Pop conflicted and `--no-restore` was given: the conflicted tree is left
#      in place deliberately, with the stash preserved.
#
# RELATIONSHIP TO THE OTHER STASH TOOLING
#
#   - `worktree.sh stash-push/stash-pop <N>` is the right tool for a Builder's
#     OWN WIP inside an issue worktree: it anchors to a per-issue ref and never
#     touches `refs/stash` at all. Prefer it there; this script is for the
#     primary checkout, where no per-issue equivalent exists.
#   - `check-main-clean.sh --quarantine` moves contamination ONTO the stash
#     stack. This script is the safe way back OFF it.
#   - `guard-destructive-generic.sh`'s `stash-scope:main-checkout` ask names
#     this script as the recommended path (#6501).

set -uo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

REPO_ARG=""
USE_INDEX=false
NO_RESTORE=false
DRY_RUN=false
EMIT_JSON=false
QUIET=false
STASH_REF=""

# Populated as the run progresses; consumed by emit_json/report.
REPO=""
STASH_SHA=""
SNAPSHOT_REF=""
SNAPSHOT_SHA=""
POP_STATUS=""
CONFLICT_FILES=""

usage() {
    sed -n '2,90p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

say() { [[ "$QUIET" == true ]] || echo -e "$@" >&2; }
say_err() { echo -e "${RED}ERROR${NC}: $*" >&2; }
say_warn() { echo -e "${YELLOW}WARNING${NC}: $*" >&2; }
say_ok() { [[ "$QUIET" == true ]] || echo -e "${GREEN}OK${NC}: $*" >&2; }

# One structured line on stdout, for callers that parse rather than read.
# $1 = result keyword, $2 = exit code.
emit_json() {
    [[ "$EMIT_JSON" == true ]] || return 0
    local files_json="" f f_escaped
    while IFS= read -r f; do
        [[ -n "$f" ]] || continue
        # Escape backslash FIRST, then quote — otherwise a quote-escape's own
        # backslash would itself need escaping. This matters for any literal
        # `\` in a path, and for core.quotepath's C-quoted (`\NNN`-octal)
        # rendering of non-ASCII/control-char paths reaching here.
        f_escaped="${f//\\/\\\\}"
        f_escaped="${f_escaped//\"/\\\"}"
        files_json+="${files_json:+,}\"${f_escaped}\""
    done <<< "$CONFLICT_FILES"
    printf '{"event":"safe-stash-pop","result":"%s","repo":"%s","stashRef":"%s","stashSha":"%s","popStatus":%s,"conflictFiles":[%s],"snapshotRef":"%s","exitCode":%s}\n' \
        "$1" "$REPO" "$STASH_REF" "$STASH_SHA" "${POP_STATUS:-null}" "$files_json" "$SNAPSHOT_REF" "$2"
}

# Terminate with a result keyword + exit code, emitting JSON exactly once.
finish() {
    emit_json "$1" "$2"
    exit "$2"
}

# --- argument parsing --------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)
            [[ $# -ge 2 ]] || { say_err "--repo requires a path"; exit 1; }
            REPO_ARG="$2"; shift 2 ;;
        --repo=*) REPO_ARG="${1#--repo=}"; shift ;;
        --index) USE_INDEX=true; shift ;;
        --no-restore) NO_RESTORE=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        --json) EMIT_JSON=true; shift ;;
        --quiet|-q) QUIET=true; shift ;;
        --help|-h) usage; exit 0 ;;
        --) shift ;;
        -*)
            say_err "Unknown option: $1 (see --help)"
            exit 1 ;;
        *)
            if [[ -n "$STASH_REF" ]]; then
                say_err "Only one stash ref may be given (got '$STASH_REF' and '$1')"
                exit 1
            fi
            STASH_REF="$1"; shift ;;
    esac
done

[[ -n "$STASH_REF" ]] || STASH_REF="stash@{0}"

# --- preconditions -----------------------------------------------------------
resolve_repo() {
    local start="${1:-$PWD}" top
    [[ -d "$start" ]] || return 1
    top="$(cd "$start" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null)" || return 1
    [[ -n "$top" && -d "$top" ]] || return 1
    (cd "$top" 2>/dev/null && pwd -P)
}

if ! REPO="$(resolve_repo "${REPO_ARG:-$PWD}")" || [[ -z "$REPO" ]]; then
    say_err "Not a git repository: ${REPO_ARG:-$PWD}"
    finish "precondition" 1
fi

GIT_DIR_ABS="$(git -C "$REPO" rev-parse --absolute-git-dir 2>/dev/null)" || GIT_DIR_ABS=""
if [[ -z "$GIT_DIR_ABS" ]]; then
    say_err "Could not resolve the git directory for $REPO"
    finish "precondition" 1
fi

# A pop layered on top of an in-progress merge/rebase/cherry-pick/revert is
# ambiguous: the markers that show up afterwards cannot be attributed, and a
# rollback would clobber the other operation's state. Refuse — fail closed,
# matching primary_checkout_reaper's in_special_git_state() marker set (#6499).
for _special in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG rebase-merge rebase-apply; do
    if [[ -e "$GIT_DIR_ABS/$_special" ]]; then
        say_err "$REPO is mid-operation ($_special present) — resolve or abort that first; refusing to pop into an ambiguous state"
        finish "precondition" 1
    fi
done

if ! git -C "$REPO" rev-parse --verify --quiet HEAD >/dev/null 2>&1; then
    say_err "$REPO has an unborn HEAD — nothing to merge a stash against"
    finish "precondition" 1
fi

# An index that ALREADY carries unmerged entries is the abandoned-conflict
# state this script exists to prevent. Popping on top of it would bury the
# evidence, and `git stash create` cannot snapshot it either.
if [[ -n "$(git -C "$REPO" ls-files --unmerged 2>/dev/null)" ]]; then
    say_err "$REPO already has unmerged index entries (an abandoned conflict state)."
    say "  Resolve it FIRST — this script will not pop on top of it."
    say "  Inspect:  ${BOLD}git -C $REPO status${NC}"
    say "  Abandon:  ${BOLD}git -C $REPO merge --abort${NC}  (or 'git -C $REPO reset --merge')"
    say "  See also: ${BOLD}./.loom/scripts/check-main-clean.sh${NC}"
    finish "precondition" 1
fi

# The stash entry must exist. `stash@{N}` against an empty stack fails to
# resolve, which is exit 2 ("nothing to do"), not an error.
if ! git -C "$REPO" rev-parse --verify --quiet refs/stash >/dev/null 2>&1; then
    say_warn "No stash stack in $REPO — nothing to pop"
    finish "no_stash" 2
fi
STASH_SHA="$(git -C "$REPO" rev-parse --verify --quiet "${STASH_REF}^{commit}" 2>/dev/null)" || STASH_SHA=""
if [[ -z "$STASH_SHA" ]]; then
    say_warn "No stash entry at '$STASH_REF' in $REPO — nothing to pop"
    finish "no_stash" 2
fi

# --- conflict-marker scanning ------------------------------------------------
# The definition of "has conflict markers" is deliberately narrow: a file must
# carry BOTH an opening `<<<<<<< ` and a closing `>>>>>>> ` at line start. A
# lone `=======` is a markdown heading underline, and a lone `<<<<<<<` shows up
# in this repo's own guard fixtures — neither alone means a broken merge.
# `-I` makes binary files non-matching so a binary blob never trips the scan.
file_has_markers() {
    grep -Iq '^<<<<<<< ' "$1" 2>/dev/null && grep -Iq '^>>>>>>> ' "$1" 2>/dev/null
}

# Whether <path>'s CURRENT content is byte-identical to a blob that already
# existed somewhere — the pre-pop snapshot, the stash entry's tracked tree, the
# stash entry's untracked payload (its third parent), or HEAD. If it is, the
# marker-shaped lines in it are DATA that was copied in verbatim, not the
# residue of a failed 3-way merge: this repo's own guard fixtures and
# documentation legitimately contain `<<<<<<<` / `>>>>>>>` lines, and stashing
# one of those files must not be mistaken for a broken pop. A genuine merge
# conflict interleaves both sides and therefore matches none of the four.
#
# Compared by blob hash rather than text so binary content and exact trailing
# whitespace are handled without special-casing.
content_is_preexisting_blob() {
    local path="$1" current base blob
    current="$(git -C "$REPO" hash-object -- "$REPO/$path" 2>/dev/null)" || return 1
    [[ -n "$current" ]] || return 1
    for base in "$SNAPSHOT_SHA" "$STASH_SHA" "${STASH_SHA:+${STASH_SHA}^3}" HEAD; do
        [[ -n "$base" ]] || continue
        blob="$(git -C "$REPO" rev-parse --verify --quiet "$base:$path" 2>/dev/null)" || continue
        [[ "$blob" == "$current" ]] && return 0
    done
    return 1
}

# Newline-separated list of tracked files that carry conflict markers NOW whose
# content is not explained by any pre-existing blob (see above) — i.e. markers
# this pop actually produced. Scans everything differing from HEAD (staged or
# unstaged, including staged adds) plus every unmerged path.
scan_new_marker_files() {
    local path
    local -a seen=()
    local already s
    # `-z`, read via NUL-delimited `read -r -d ''`, NOT `-c core.quotepath=false`:
    # quotepath only suppresses quoting of non-ASCII/control-byte paths — git
    # ALWAYS C-quotes a path containing a literal `\` or `"` regardless of that
    # setting, so quotepath alone still fails the `[[ -f "$REPO/$path" ]]` test
    # below for such names. `-z` disables quoting unconditionally and emits the
    # verbatim path, which handles non-ASCII, control chars, `"`, AND `\` in one
    # move (also incidentally embedded-newline-safe) — see #6517.
    while IFS= read -r -d '' path; do
        [[ -n "$path" ]] || continue
        already=false
        for s in "${seen[@]:-}"; do
            [[ "$s" == "$path" ]] && { already=true; break; }
        done
        [[ "$already" == true ]] && continue
        seen+=("$path")
        [[ -f "$REPO/$path" ]] || continue
        file_has_markers "$REPO/$path" || continue
        content_is_preexisting_blob "$path" && continue
        printf '%s\n' "$path"
    done < <(
        {
            git -C "$REPO" diff --name-only -z HEAD 2>/dev/null
            git -C "$REPO" diff --name-only -z --diff-filter=U 2>/dev/null
        }
    )
}

# --- pre-pop snapshot --------------------------------------------------------
DIRTY_PRE="$(git -C "$REPO" status --porcelain --untracked-files=no 2>/dev/null)"
# NUL-delimited (`-z` + `read -r -d ''`), not a newline-joined string: this is
# compared byte-for-byte against the untracked-payload tree below (also `-z`)
# to decide whether a path pre-dates the pop. A quoted/unquoted mismatch
# between the two sides would make a pre-existing non-ASCII/`\`/`"`-containing
# untracked path fail to match and be misidentified as "new" (i.e. part of the
# stash payload) — which would make the cleanup loop delete an operator's own
# untracked file (#6517). Collecting into an array (rather than grep -qxF on a
# newline-joined string) also sidesteps any embedded-newline path.
UNTRACKED_PRE_ARR=()
while IFS= read -r -d '' _pre; do
    [[ -n "$_pre" ]] && UNTRACKED_PRE_ARR+=("$_pre")
done < <(git -C "$REPO" ls-files --others --exclude-standard -z 2>/dev/null)

if [[ "$DRY_RUN" == true ]]; then
    say_ok "Dry run: would pop ${BOLD}$STASH_REF${NC} ($STASH_SHA) in $REPO"
    say "  index restore: $USE_INDEX | rollback on conflict: $([[ "$NO_RESTORE" == true ]] && echo false || echo true)"
    say "  pre-pop tracked dirt: $([[ -n "$DIRTY_PRE" ]] && echo "yes" || echo "none")"
    finish "dry_run" 0
fi

# `git stash create` builds a stash-format commit WITHOUT touching refs/stash
# (the #5217 / worktree.sh stash-push precedent). Empty output means the tree
# had no tracked changes to capture, i.e. "clean HEAD" is itself the restore
# target.
SNAPSHOT_SHA="$(git -C "$REPO" stash create 2>/dev/null | tr -d '[:space:]')" || SNAPSHOT_SHA=""

if [[ -n "$DIRTY_PRE" && -z "$SNAPSHOT_SHA" ]]; then
    # The tree carries tracked changes we could not capture. A conflicting pop
    # could then not be rolled back without destroying them, so do not pop at
    # all. Refusing costs the caller a retry; guessing costs the operator work.
    say_err "$REPO has uncommitted tracked changes but 'git stash create' captured nothing."
    say "  Refusing to pop: a conflicting pop could not be rolled back without risking those changes."
    say "  Commit or set them aside first, then re-run."
    finish "precondition" 1
fi

if [[ -n "$SNAPSHOT_SHA" ]]; then
    SNAPSHOT_REF="refs/loom/safe-stash-pop/$(date -u +%Y%m%dT%H%M%SZ)-$$"
    if ! git -C "$REPO" update-ref "$SNAPSHOT_REF" "$SNAPSHOT_SHA" 2>/dev/null; then
        say_err "Could not anchor the pre-pop snapshot at $SNAPSHOT_REF — refusing to pop without a rollback path"
        SNAPSHOT_REF=""
        finish "precondition" 1
    fi
fi

# --- the pop ------------------------------------------------------------------
POP_ARGS=(stash pop)
[[ "$USE_INDEX" == true ]] && POP_ARGS+=(--index)
POP_ARGS+=("$STASH_REF")

POP_OUTPUT="$(git -C "$REPO" "${POP_ARGS[@]}" 2>&1)"
POP_STATUS=$?

UNMERGED_AFTER="$(git -C "$REPO" ls-files --unmerged 2>/dev/null)"
CONFLICT_FILES="$(scan_new_marker_files)"

CONFLICTED=false
[[ "$POP_STATUS" -ne 0 ]] && CONFLICTED=true
[[ -n "$UNMERGED_AFTER" ]] && CONFLICTED=true
[[ -n "$CONFLICT_FILES" ]] && CONFLICTED=true

# --- clean path ---------------------------------------------------------------
if [[ "$CONFLICTED" == false ]]; then
    if [[ -n "$SNAPSHOT_REF" ]]; then
        git -C "$REPO" update-ref -d "$SNAPSHOT_REF" 2>/dev/null || true
        SNAPSHOT_REF=""
    fi
    say_ok "Popped $STASH_REF cleanly in $REPO (verified: no unmerged entries, no conflict markers)"
    finish "clean" 0
fi

# --- conflict path ------------------------------------------------------------
report_conflict_header() {
    say_warn "'git stash pop' conflicted in $REPO (status $POP_STATUS)"
    if [[ -n "$POP_OUTPUT" ]]; then
        say "  git reported:"
        printf '%s\n' "$POP_OUTPUT" | sed 's/^/    /' >&2
    fi
    if [[ -n "$CONFLICT_FILES" ]]; then
        say "  Files carrying NEW conflict markers:"
        printf '%s\n' "$CONFLICT_FILES" | sed 's/^/    /' >&2
    fi
}

report_conflict_header

if [[ "$NO_RESTORE" == true ]]; then
    say_warn "--no-restore given: leaving the conflicted tree in place for manual resolution."
    say "  The stash entry is PRESERVED ($STASH_SHA)."
    [[ -n "$SNAPSHOT_REF" ]] && say "  Pre-pop snapshot: ${BOLD}$SNAPSHOT_REF${NC} (git stash apply $SNAPSHOT_REF)"
    say "  Resolve the markers before ANY commit — see defaults/docs/troubleshooting.md."
    finish "skipped_restore" 5
fi

# Rollback precondition #1: the stash entry must still exist. A conflicting pop
# keeps it, but verify rather than assume — if it is gone for any reason, the
# conflicted working tree is the ONLY copy of that work and must not be reset.
STASH_STILL_PRESENT=false
if [[ "$(git -C "$REPO" rev-parse --verify --quiet "${STASH_REF}^{commit}" 2>/dev/null)" == "$STASH_SHA" ]]; then
    STASH_STILL_PRESENT=true
elif git -C "$REPO" stash list --format='%H' 2>/dev/null | grep -qxF "$STASH_SHA"; then
    STASH_STILL_PRESENT=true
fi

if [[ "$STASH_STILL_PRESENT" != true ]]; then
    say_err "The stash entry $STASH_SHA is no longer on the stack after the conflicting pop."
    say "  NOT rolling back: the conflicted working tree is now the only copy of that work."
    say "  Resolve the markers by hand, then commit or discard deliberately."
    [[ -n "$SNAPSHOT_REF" ]] && say "  Pre-pop snapshot preserved at ${BOLD}$SNAPSHOT_REF${NC}"
    finish "not_restored" 4
fi

# Rollback. `reset --hard` restores every TRACKED path to HEAD (it never
# touches untracked files), and the snapshot commit then replays exactly what
# the tree carried before the pop.
if ! git -C "$REPO" reset -q --hard HEAD >/dev/null 2>&1; then
    say_err "Rollback failed: could not reset $REPO to HEAD."
    say "  The stash entry is PRESERVED ($STASH_SHA); resolve the conflict by hand."
    [[ -n "$SNAPSHOT_REF" ]] && say "  Pre-pop snapshot preserved at ${BOLD}$SNAPSHOT_REF${NC}"
    finish "not_restored" 4
fi

if [[ -n "$SNAPSHOT_SHA" ]]; then
    if ! git -C "$REPO" stash apply --index "$SNAPSHOT_SHA" >/dev/null 2>&1; then
        if ! git -C "$REPO" stash apply "$SNAPSHOT_SHA" >/dev/null 2>&1; then
            say_err "Rollback failed: could not replay the pre-pop snapshot."
            say "  Nothing was discarded — recover with: ${BOLD}git -C $REPO stash apply $SNAPSHOT_REF${NC}"
            say "  The stash entry is also PRESERVED ($STASH_SHA)."
            finish "not_restored" 4
        fi
    fi
fi

# `reset --hard` leaves untracked files alone, so a `-u` stash's untracked
# payload can survive the rollback as stray files. Remove ONLY paths that (a)
# were absent before the pop, (b) are recorded in the stash's own untracked
# tree (its third parent), and (c) are NOT tracked in HEAD — both copies of an
# untracked payload still live in the preserved stash entry, so removing one is
# a rollback, not a discard.
#
# Condition (c) is the important one and is NOT redundant with (a): a path can
# be untracked at stash time and tracked in HEAD by pop time (someone committed
# a file of that name meanwhile). `reset --hard HEAD` has just restored the
# COMMITTED copy at that path, so deleting it would manufacture a spurious
# deletion the caller never asked for — the rollback verification below would
# catch it (exit 4) but the tree would be left worse than it started. This is
# the only line in the script that removes a file; it stays maximally narrow.
STASH_UNTRACKED_TREE="$(git -C "$REPO" rev-parse --verify --quiet "${STASH_SHA}^3" 2>/dev/null)" || STASH_UNTRACKED_TREE=""
if [[ -n "$STASH_UNTRACKED_TREE" ]]; then
    while IFS= read -r -d '' _u; do
        [[ -n "$_u" ]] || continue
        _u_pre_existing=false
        for _pre in "${UNTRACKED_PRE_ARR[@]:-}"; do
            [[ "$_pre" == "$_u" ]] && { _u_pre_existing=true; break; }
        done
        [[ "$_u_pre_existing" == true ]] && continue
        git -C "$REPO" cat-file -e "HEAD:$_u" 2>/dev/null && continue
        [[ -f "$REPO/$_u" ]] || continue
        rm -f "$REPO/$_u" 2>/dev/null || true
    done < <(git -C "$REPO" ls-tree -r -z --name-only "$STASH_UNTRACKED_TREE" 2>/dev/null)
fi

# Verify the rollback actually landed: no unmerged entries, no leftover
# markers, and the tracked-dirt fingerprint back to what it was pre-pop.
ROLLBACK_OK=true
[[ -n "$(git -C "$REPO" ls-files --unmerged 2>/dev/null)" ]] && ROLLBACK_OK=false
[[ -n "$(scan_new_marker_files)" ]] && ROLLBACK_OK=false
if [[ "$(git -C "$REPO" status --porcelain --untracked-files=no 2>/dev/null)" != "$DIRTY_PRE" ]]; then
    ROLLBACK_OK=false
fi

if [[ "$ROLLBACK_OK" != true ]]; then
    say_err "Rollback could not be VERIFIED — $REPO does not match its pre-pop state."
    say "  Nothing was discarded. Recovery handles:"
    [[ -n "$SNAPSHOT_REF" ]] && say "    pre-pop snapshot: ${BOLD}git -C $REPO stash apply $SNAPSHOT_REF${NC}"
    say "    stash entry:      ${BOLD}git -C $REPO stash show -p $STASH_SHA${NC}"
    finish "not_restored" 4
fi

say_ok "Pre-pop state restored and verified — no conflict markers were left in $REPO"
say "  The stash entry is PRESERVED ($STASH_SHA) — nothing was discarded."
say "  Reconcile it by hand, e.g.:"
say "    ${BOLD}git -C $REPO stash show -p $STASH_REF${NC}                  # inspect"
say "    ${BOLD}git -C $REPO stash show -p $STASH_REF | git -C $REPO apply --3way${NC}"
say "    ${BOLD}git -C $REPO stash drop $STASH_REF${NC}                     # once reconciled"
if [[ -n "$SNAPSHOT_REF" ]]; then
    say "  Pre-pop snapshot kept as insurance at ${BOLD}$SNAPSHOT_REF${NC}"
    say "    delete when satisfied: ${BOLD}git -C $REPO update-ref -d $SNAPSHOT_REF${NC}"
fi
finish "restored" 3
