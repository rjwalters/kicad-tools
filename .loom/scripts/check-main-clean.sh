#!/usr/bin/env bash
# check-main-clean.sh - Backstop guard: fail if the MAIN worktree is dirty.
#
# Detects the #2802 / #3513 failure mode where a Builder agent's cwd resets
# between tool calls and a repo-relative Write/Edit/Bash file operation lands
# in the MAIN repository working tree instead of the issue worktree under
# .loom/worktrees/issue-N. The builder guidance (builder.md / builder-worktree.md)
# is the primary defense (capture the absolute worktree path once, use absolute
# paths everywhere). This script is the BACKSTOP: run it after the builder phase
# and before opening a PR. If the main worktree has uncommitted changes, the
# builder has contaminated main and the sweep should abort.
#
# It resolves the MAIN worktree from anywhere — including from inside an issue
# worktree — via `git rev-parse --git-common-dir`, so it works whether invoked
# from the repo root or from a worktree.
#
# Usage:
#   ./.loom/scripts/check-main-clean.sh                  # check main worktree, exit 3 if dirty
#   ./.loom/scripts/check-main-clean.sh --snapshot FILE  # record main's porcelain state to FILE, exit 0
#   ./.loom/scripts/check-main-clean.sh --baseline FILE  # check main, ignoring dirt recorded in FILE
#   ./.loom/scripts/check-main-clean.sh --baseline FILE --quarantine [--label TEXT] [--log FILE]
#                                                        # ...and atomically stash any new dirt away
#   ./.loom/scripts/check-main-clean.sh --list-quarantined [--json]
#                                                        # report outstanding quarantine stashes
#   ./.loom/scripts/check-main-clean.sh --help           # show usage
#
# Baseline mechanism (#3648):
#   The plain (no-arg) check treats ANY uncommitted change on main as builder
#   contamination. That false-positives when the main worktree carried
#   pre-existing, unrelated dirt (a regenerated lockfile, a scratch edit) before
#   the sweep even started. The baseline mechanism lets a caller (e.g. /loom:sweep)
#   snapshot main's porcelain state ONCE at sweep start, then pass that snapshot
#   as a --baseline on each post-wave check. Only changes that appeared AFTER the
#   snapshot (set difference on exact porcelain lines) are treated as
#   contamination; pre-existing dirt is ignored.
#
#     ./.loom/scripts/check-main-clean.sh --snapshot .loom/main-clean-baseline.txt   # once, before wave 1
#     ./.loom/scripts/check-main-clean.sh --baseline .loom/main-clean-baseline.txt   # after each builder
#
#   Fail-safe: if the --baseline FILE is missing or unreadable, the check warns
#   to stderr and falls back to the plain whole-status behavior (never a silent
#   pass). With NO --baseline/--snapshot argument, behavior is byte-for-byte
#   identical to the pre-#3648 whole-status hard-fail — the builder.md /
#   builder-worktree.md manual call sites depend on that contract.
#
# Quarantine mode (#4380):
#   Detection alone leaves remediation to whoever reads the failure — historically
#   an ad-hoc, per-file `git checkout --` restore that can (and did) stop half
#   way, leaving main in a state that is neither the pre-sweep baseline nor the
#   builder's intended change. `--quarantine` makes remediation ATOMIC and
#   LOGGED: on detecting new dirt, every offending path (tracked AND untracked)
#   is moved to a stash rescue ref in ONE `git stash push --include-untracked`
#   operation, and exactly one structured JSON line is emitted describing what
#   was quarantined and where it went.
#
#     ./.loom/scripts/check-main-clean.sh --baseline "$MAIN_CLEAN_BASELINE" \
#         --quarantine --label "run=$RUN_ID issue=$N"
#
#   - It is a RESCUE, never a discard: the full diff survives in the stash
#     (`git stash list` / `git stash show -p <ref>`), so the work can be replayed
#     into the correct worktree. Nothing is ever deleted.
#   - Only paths the check flagged as NEW are stashed. Pre-existing (baselined)
#     dirt is passed to `git stash push` as an explicit pathspec exclusion — an
#     operator's unrelated working-tree edits are never swept up.
#   - `--label TEXT` keys the stash message (e.g. the sweep RUN_ID + issue
#     number) so a quarantined stash is attributable to the builder that caused
#     it. Defaults to `$LOOM_QUARANTINE_LABEL`, else "unattributed".
#   - `--log FILE` appends the same one-line JSON entry to FILE (best effort;
#     never fails the check). Defaults to `<main>/.loom/logs/main-quarantine.log`
#     when that directory exists.
#   - Exit 4 (not 3) on a SUCCESSFUL quarantine: main is provably back at the
#     baseline, so the caller can log and continue rather than hard-blocking. If
#     the quarantine could not be completed, the exit stays 3 (hard-block).
#   - `--quarantine` is intended with `--baseline`. It is accepted without one,
#     in which case ALL non-Loom-owned dirt in main is quarantined (still a
#     rescue ref, but with no notion of "pre-existing" to protect). It is a
#     usage error with `--snapshot`.
#   - Immediately before the `git stash push`, the offending path set is
#     RE-DERIVED from a fresh `git status` (#5185). The first snapshot is taken
#     earlier in the run, and on a busy host the dirt it describes can be
#     resolved in between (a concurrent sweep, the builder's own commit, a
#     cleanup pass). Pushing the stale pathspec anyway produced either a
#     misattributed no-op — `git stash push` prints "No local changes to save",
#     exits 0, creates nothing, and the old success path then reported the
#     PREVIOUS entry's sha as the rescue ref — or, where git allowed it, an
#     EMPTY stash entry carrying no information. Since `git stash` shares one
#     reflog across every linked worktree, each such entry buries the real ones
#     further down a stack nobody can safely prune. When the re-derivation finds
#     nothing left to quarantine, NO stash is pushed and a distinct
#     `"result":"no_op"` event is logged (exit 0 — main is provably at the
#     baseline). A stash that is somehow still created empty is logged as
#     `"result":"quarantined_empty"`, so the race stays visible instead of
#     masquerading as an ordinary quarantine.
#
# Listing outstanding quarantine stashes (#5185):
#   A quarantine is recorded in the structured log, but the rescue stash itself
#   had no operator-facing surface — entries accumulated unreconciled (29 on one
#   host, oldest 7 days) and were only ever noticed by accident. `--list-quarantined`
#   is that surface: a read-only report of every Loom-produced stash entry
#   outstanding in the MAIN worktree.
#
#     ./.loom/scripts/check-main-clean.sh --list-quarantined          # human-readable
#     ./.loom/scripts/check-main-clean.sh --list-quarantined --json   # machine-readable
#
#   - Matches by stash MESSAGE, so it covers every Loom producer, not just this
#     script (see LOOM_STASH_PATTERNS below — the Auditor's drift shelf uses its
#     own prefix).
#   - Reports each entry's commit sha (indices shift as the shared stack grows),
#     relative age, producer, the `issue=`/`run=` attribution parsed out of the
#     message, and whether the entry is EMPTY (nothing was actually captured).
#   - Read-only: it never pops, drops, or otherwise mutates the shared stash
#     stack. Exit 0 whether or not anything is outstanding — it is a report, not
#     a gate, so `loom status` can surface the count without risking a failure.
#   - `./.loom/bin/loom status` calls it to show a "Quarantined work" section, so
#     an outstanding quarantine is discoverable without knowing one happened.
#
# Exit codes:
#   0 - Main worktree is clean (or, in --baseline mode, only pre-existing dirt
#       remains); or a --snapshot completed successfully; or a --quarantine
#       found nothing left to quarantine and created no stash (the `no_op`
#       result, #5185); or a --list-quarantined report completed (always 0 —
#       it is a report, not a gate).
#   2 - Usage error or could not resolve the main worktree (not a git repo).
#   3 - Main worktree is DIRTY: uncommitted changes detected (the contamination
#       this guard exists to catch). In --baseline mode, only NEW changes count.
#       Also returned when a requested --quarantine could NOT be completed.
#   4 - Main worktree WAS dirty and the new dirt was successfully quarantined to
#       a stash rescue ref (--quarantine only). Main is back at the baseline.
#
# Notes:
#   - "Dirty" means `git status --porcelain` on the MAIN worktree returns any
#     output: staged, unstaged, or untracked files all count. A builder working
#     correctly in its own worktree leaves the main worktree pristine.
#   - Issue worktrees live under <main>/.loom/worktrees/ which is gitignored, so
#     their existence does NOT make the main worktree dirty.
#   - Does NOT adopt the installed-surface byte-match ignore class added to
#     `main_health_gate.rs`'s `is_ignorable_dirt` (#4332) — a deliberate
#     divergence, not an oversight. That class exists to protect a *different*
#     property: "this dirt is provably `resync-installed.sh` output, not an
#     operator hand-edit worth halting the dispatch gate over." This script's
#     job is the opposite kind of check — catching a *builder* accidentally
#     writing into the MAIN worktree instead of its issue worktree (#2802 /
#     #3513) — and a builder-written file that happens to byte-match
#     `defaults/` is still a real out-of-worktree write bug the backstop must
#     not mask. It would also be a no-op in the common case anyway: this
#     script is the manual/consumer-repo backstop and most consumer installs
#     have no local `defaults/` tree to byte-match against.

set -euo pipefail

EXIT_OK=0
EXIT_USAGE=2
EXIT_MAIN_DIRTY=3
EXIT_QUARANTINED=4

# ---- Loom-owned transient state paths (#3778) ----------------------------
# Runtime bookkeeping the sweep orchestrator itself writes under .loom/ during a
# run (checkpoints, the token pool, stats, locks, exit-codes, …). These are
# gitignored in Loom's *source* .gitignore, but a consumer repo's installed
# loom-managed .gitignore block can drift out of sync and omit newer entries —
# so the same transient surfaces as an untracked path and false-positives this
# backstop as "builder contamination" (observed in rjwalters/anvil, #3778).
# Rather than depend on the consumer's .gitignore being current, exclude the
# known Loom-owned transient paths internally, unconditionally. None of these is
# source a builder should ever be writing to main, so filtering them can never
# mask real contamination. Prefixes ending in "/" match a directory subtree; the
# others match an exact path. Keep roughly in sync with the loom-managed block in
# Loom's source .gitignore.
LOOM_OWNED_PREFIXES=(
    ".loom/sweep-checkpoint/"
    ".loom/sweep-run/"
    ".loom/tokens/"
    ".loom/accounts.env"
    ".loom/exit-codes/"
    ".loom/stats/"
    ".loom/CANARY"
    ".loom/spawn-loop.pid"
    ".loom/spawn-loop-state.json"
    ".loom/stop-spawn-loop"
    ".loom/locks/"
    ".loom/logs/"
    ".loom/worktrees/"
    ".loom-managed"
)

# ---- Loom-owned stash producers (#5185) ----------------------------------
# Stash MESSAGE fragments that identify an entry created by Loom automation
# rather than by a human. `git stash list` always prefixes an entry with
# "On <branch>: " regardless of the -m message, so these are matched as
# substrings, not anchors. Keep in sync with the producers documented above:
# any new automation that pushes to the shared stash stack must add its prefix
# here, or its entries stay invisible to `--list-quarantined` (and therefore to
# `loom status`) exactly like quarantine stashes were before this surface.
LOOM_STASH_PATTERNS=(
    "loom-quarantine:|quarantine"              # this script's --quarantine rescue ref
    "auditor-tmp-drift-stash-|auditor-drift"   # Auditor role's temporary drift shelf
)

# stash_producer <stash-subject> -> the producer name for a Loom-produced stash
# entry, or empty if the subject is not one of ours.
stash_producer() {
    local subject="$1" entry pattern name
    for entry in "${LOOM_STASH_PATTERNS[@]}"; do
        pattern="${entry%%|*}"
        name="${entry##*|}"
        if [[ "$subject" == *"$pattern"* ]]; then
            printf '%s' "$name"
            return 0
        fi
    done
    return 1
}

# is_loom_owned <repo-relative-path> -> 0 if the path is a Loom-owned transient.
is_loom_owned() {
    local path="$1" prefix
    for prefix in "${LOOM_OWNED_PREFIXES[@]}"; do
        if [[ "$prefix" == */ ]]; then
            [[ "$path" == "$prefix"* ]] && return 0
        else
            [[ "$path" == "$prefix" ]] && return 0
        fi
    done
    return 1
}

# filter_loom_owned <porcelain-text> -> the same text with Loom-owned transient
# lines removed. Parses each `git status --porcelain` v1 line (2 status chars +
# space + path; rename lines carry "old -> new" and are keyed on the new path).
filter_loom_owned() {
    local in="$1" line path out=""
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        path="${line:3}"
        if [[ "$path" == *" -> "* ]]; then
            path="${path##* -> }"
        fi
        path="${path%\"}"
        path="${path#\"}"
        if is_loom_owned "$path"; then
            continue
        fi
        out+="$line"$'\n'
    done <<< "$in"
    printf '%s' "${out%$'\n'}"
}

# Print the leading comment block (everything from line 2 up to the first
# non-comment line) with the "# " prefix stripped. Derived from the file itself
# so it can never drift out of sync with a hardcoded line range.
usage() {
    awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
}

# unquote_path <porcelain-path> -> the real filesystem path.
# `git status --porcelain` v1 wraps paths containing special characters in
# double quotes and C-escapes them (\", \\, \ooo octal). Passing that literal
# text to `git stash push` as a pathspec would miss the file, so decode it.
unquote_path() {
    local p="$1"
    if [[ "$p" == \"*\" ]]; then
        p="${p%\"}"
        p="${p#\"}"
        p=$(printf '%b' "$p")
    fi
    printf '%s' "$p"
}

# json_escape <text> -> the text, escaped for embedding in a JSON string.
json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\t'/\\t}"
    s="${s//$'\r'/\\r}"
    s="${s//$'\n'/\\n}"
    printf '%s' "$s"
}

# ---- Argument parsing ----------------------------------------------------
# Modes: plain (no args, legacy back-compat), snapshot, baseline.
# Flags:  --quarantine [--label TEXT] [--log FILE]  (#4380)
MODE="plain"
MODE_FILE=""
QUARANTINE=0
LIST_QUARANTINED=0
LIST_JSON=0
QUARANTINE_LABEL="${LOOM_QUARANTINE_LABEL:-}"
QUARANTINE_LOG="${LOOM_QUARANTINE_LOG:-}"

require_value() {
    # require_value <flag> <value-or-empty>
    if [[ -z "$2" ]]; then
        echo "check-main-clean.sh: $1 requires a file argument" >&2
        echo "Run with --help for usage." >&2
        exit "$EXIT_USAGE"
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit "$EXIT_OK"
            ;;
        --snapshot)
            require_value "--snapshot" "${2:-}"
            MODE="snapshot"
            MODE_FILE="$2"
            shift 2
            ;;
        --baseline)
            require_value "--baseline" "${2:-}"
            MODE="baseline"
            MODE_FILE="$2"
            shift 2
            ;;
        --quarantine)
            QUARANTINE=1
            shift
            ;;
        --list-quarantined)
            LIST_QUARANTINED=1
            shift
            ;;
        --json)
            LIST_JSON=1
            shift
            ;;
        --label)
            require_value "--label" "${2:-}"
            QUARANTINE_LABEL="$2"
            shift 2
            ;;
        --log)
            require_value "--log" "${2:-}"
            QUARANTINE_LOG="$2"
            shift 2
            ;;
        *)
            echo "check-main-clean.sh: unknown argument: $1" >&2
            echo "Run with --help for usage." >&2
            exit "$EXIT_USAGE"
            ;;
    esac
done

if [[ "$QUARANTINE" -eq 1 && "$MODE" == "snapshot" ]]; then
    echo "check-main-clean.sh: --quarantine is not valid with --snapshot (nothing is checked in snapshot mode)" >&2
    echo "Run with --help for usage." >&2
    exit "$EXIT_USAGE"
fi

# --list-quarantined is a standalone read-only report: it inspects the stash
# stack, never the working tree, so combining it with a check/snapshot mode
# would silently discard one of the two requests.
if [[ "$LIST_QUARANTINED" -eq 1 ]] && { [[ "$MODE" != "plain" ]] || [[ "$QUARANTINE" -eq 1 ]]; }; then
    echo "check-main-clean.sh: --list-quarantined is a standalone report; it cannot be combined with --snapshot/--baseline/--quarantine" >&2
    echo "Run with --help for usage." >&2
    exit "$EXIT_USAGE"
fi

if [[ "$LIST_JSON" -eq 1 && "$LIST_QUARANTINED" -eq 0 ]]; then
    echo "check-main-clean.sh: --json is only valid with --list-quarantined" >&2
    echo "Run with --help for usage." >&2
    exit "$EXIT_USAGE"
fi

[[ -n "$QUARANTINE_LABEL" ]] || QUARANTINE_LABEL="unattributed"

# ---- Resolve the main worktree root --------------------------------------
# Resolve the canonical git common dir, then the main worktree root (its parent).
# This works from the repo root AND from inside any worktree.
common_dir=$(git rev-parse --git-common-dir 2>/dev/null || true)
if [[ -z "$common_dir" ]]; then
    echo "check-main-clean.sh: not inside a git repository" >&2
    exit "$EXIT_USAGE"
fi

# git-common-dir may be relative; resolve to an absolute path.
abs_common=$(cd "$common_dir" 2>/dev/null && pwd) || abs_common="$common_dir"
main_root=$(dirname "$abs_common")

if [[ ! -d "$main_root" ]]; then
    echo "check-main-clean.sh: could not resolve main worktree root from '$common_dir'" >&2
    exit "$EXIT_USAGE"
fi

# ---- List mode: report outstanding Loom stash entries (#5185) ------------
# Read-only: inspects the stash stack of the MAIN worktree and never touches
# the working tree, so it runs before any status/baseline logic and exits 0
# regardless of what it finds.

# stash_is_empty <stash-ref-or-sha> -> 0 when the entry captured nothing.
# `git stash show --name-only` alone reports nothing for an untracked-ONLY
# entry (those files live in the stash's third parent), which would misreport a
# perfectly good rescue ref as empty — so the --include-untracked form is tried
# first, falling back for git versions that predate it (< 2.32).
stash_is_empty() {
    local ref="$1" files=""
    files=$(git -C "$main_root" stash show --include-untracked --name-only "$ref" 2>/dev/null) \
        || files=$(git -C "$main_root" stash show --name-only "$ref" 2>/dev/null) \
        || files=""
    [[ -z "${files//[[:space:]]/}" ]]
}

# stash_field <message> <key> -> the value of a `key=value` token in a stash
# message (e.g. `issue=5137`), or empty when absent. Used to surface the
# quarantine's own attribution so an operator can check liveness (is that sweep
# still running? is that issue still open?) without decoding the message by eye.
stash_field() {
    local msg="$1" key="$2" rest
    [[ "$msg" == *"$key="* ]] || return 0
    rest="${msg##*"$key="}"
    rest="${rest%% *}"
    printf '%s' "$rest"
}

if [[ "$LIST_QUARANTINED" -eq 1 ]]; then
    entries=$(git -C "$main_root" stash list --format='%gd%x09%H%x09%cr%x09%gs' 2>/dev/null || true)

    count=0
    json_items=""
    human_rows=""
    empty_count=0
    while IFS=$'\t' read -r ref sha age subject; do
        [[ -z "$ref" ]] && continue
        producer=$(stash_producer "$subject") || continue
        empty="false"
        if stash_is_empty "$sha"; then
            empty="true"
            empty_count=$((empty_count + 1))
        fi
        issue=$(stash_field "$subject" "issue")
        run=$(stash_field "$subject" "run")
        count=$((count + 1))
        human_rows+="$ref"$'\t'"$sha"$'\t'"$age"$'\t'"$producer"$'\t'"$empty"$'\t'"$subject"$'\n'
        [[ -n "$json_items" ]] && json_items+=","
        json_items+="{\"ref\":\"$(json_escape "$ref")\",\"commit\":\"$(json_escape "$sha")\",\"age\":\"$(json_escape "$age")\",\"producer\":\"$(json_escape "$producer")\",\"empty\":$empty,\"issue\":$( [[ -n "$issue" ]] && printf '"%s"' "$(json_escape "$issue")" || printf 'null' ),\"run\":$( [[ -n "$run" ]] && printf '"%s"' "$(json_escape "$run")" || printf 'null' ),\"message\":\"$(json_escape "$subject")\"}"
    done <<< "$entries"

    if [[ "$LIST_JSON" -eq 1 ]]; then
        printf '{"main":"%s","count":%d,"empty_count":%d,"stashes":[%s]}\n' \
            "$(json_escape "$main_root")" "$count" "$empty_count" "$json_items"
        exit "$EXIT_OK"
    fi

    if [[ "$count" -eq 0 ]]; then
        echo "check-main-clean.sh: no outstanding Loom stash entries in $main_root"
        exit "$EXIT_OK"
    fi

    echo "check-main-clean.sh: $count outstanding Loom stash entr$( [[ "$count" -eq 1 ]] && echo "y" || echo "ies" ) in $main_root"
    echo ""
    while IFS=$'\t' read -r ref sha age producer empty subject; do
        [[ -z "$ref" ]] && continue
        marker=""
        [[ "$empty" == "true" ]] && marker="  [EMPTY — nothing was captured]"
        echo "  $ref  ${sha:0:12}  $age  ($producer)$marker"
        echo "      $subject"
    done <<< "$human_rows"
    echo ""
    echo "  Inspect one (resolve by COMMIT, not by index — stash@{N} shifts as the"
    echo "  shared stack grows, and every linked worktree pushes onto the same stack):"
    echo "    git -C \"$main_root\" stash show -p --include-untracked <commit>"
    echo ""
    echo "  Reconcile before dropping: an entry naming a LIVE sweep run or an OPEN"
    echo "  issue may still be the only copy of that work."
    exit "$EXIT_OK"
fi

status=$(git -C "$main_root" status --porcelain 2>/dev/null || true)

# Exclude Loom-owned transient state paths regardless of the consumer's
# .gitignore currency (#3778) — applied before snapshot/baseline/plain logic so
# every mode sees a consistently-filtered view.
status=$(filter_loom_owned "$status")

# ---- Snapshot mode: record and exit --------------------------------------
if [[ "$MODE" == "snapshot" ]]; then
    # Ensure the parent directory exists so callers can point at .loom/ transients.
    snap_dir=$(dirname "$MODE_FILE")
    if [[ -n "$snap_dir" && ! -d "$snap_dir" ]]; then
        mkdir -p "$snap_dir" 2>/dev/null || true
    fi
    if ! printf '%s\n' "$status" > "$MODE_FILE" 2>/dev/null; then
        echo "check-main-clean.sh: could not write snapshot to '$MODE_FILE'" >&2
        exit "$EXIT_USAGE"
    fi
    echo "check-main-clean.sh: snapshot of main worktree written to $MODE_FILE ($main_root)"
    exit "$EXIT_OK"
fi

# ---- Baseline mode: subtract pre-existing dirt ---------------------------
# Only lines that appear in the current status but NOT in the baseline are
# treated as new (builder) contamination. A missing/unreadable baseline falls
# back to the plain whole-status behavior (fail-safe, never a silent pass).
effective_status="$status"
ignored=0
if [[ "$MODE" == "baseline" ]]; then
    if [[ -r "$MODE_FILE" ]]; then
        # Set difference: current porcelain lines minus baseline lines.
        # comm -13 prints lines unique to the second (current) input.
        effective_status=$(comm -13 \
            <(sort "$MODE_FILE") \
            <(printf '%s\n' "$status" | sort) \
            | sed '/^$/d')
        pre_existing=$(printf '%s\n' "$status" | sed '/^$/d' | grep -c '' || true)
        new_count=$(printf '%s\n' "$effective_status" | sed '/^$/d' | grep -c '' || true)
        ignored=$(( pre_existing - new_count ))
        if [[ "$ignored" -lt 0 ]]; then ignored=0; fi
    else
        echo "WARNING: check-main-clean.sh: baseline file '$MODE_FILE' is missing or unreadable." >&2
        echo "         Falling back to whole-status check (fail-safe)." >&2
        effective_status="$status"
    fi
fi

# ---- Quarantine mode: atomically rescue new dirt to a stash ref (#4380) --
# Runs only when contamination was actually detected. Everything below is a
# no-op for callers that did not pass --quarantine.

# emit_quarantine_log <json-line>
# Writes EXACTLY ONE structured entry: to stderr always, and appended to the
# quarantine log file when one is configured/derivable (best effort — a failed
# append never changes the check's verdict).
emit_quarantine_log() {
    local line="$1"
    printf '%s\n' "$line" >&2
    local target="$QUARANTINE_LOG"
    if [[ -z "$target" && -d "$main_root/.loom/logs" ]]; then
        target="$main_root/.loom/logs/main-quarantine.log"
    fi
    if [[ -n "$target" ]]; then
        local dir
        dir=$(dirname "$target")
        [[ -d "$dir" ]] || mkdir -p "$dir" 2>/dev/null || true
        printf '%s\n' "$line" >> "$target" 2>/dev/null || true
    fi
}

# Collect the offending (NEW) paths from the porcelain lines. Rename lines
# ("old -> new") contribute BOTH sides so the rename is undone as a whole.
collect_offending_paths() {
    local line path
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        path="${line:3}"
        if [[ "$path" == *" -> "* ]]; then
            OFFENDING_PATHS+=("$(unquote_path "${path%% -> *}")")
            path="${path##* -> }"
        fi
        OFFENDING_PATHS+=("$(unquote_path "$path")")
    done <<< "$effective_status"
}

if [[ -n "$effective_status" && "$QUARANTINE" -eq 1 ]]; then
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")
    stash_msg="loom-quarantine: $QUARANTINE_LABEL"

    # Re-derive the offending set from a FRESH status immediately before the
    # push (#5185). `effective_status` was computed from a snapshot taken
    # earlier in this run; on a busy host the dirt it describes can be resolved
    # in between by a concurrent sweep, the builder's own commit, or a cleanup
    # pass. Quarantining a stale pathspec is never right: at best it is a no-op
    # `git stash push` that creates nothing (yet exits 0, so the old code below
    # then read the PREVIOUS entry's sha out of refs/stash and reported someone
    # else's stash as this run's rescue ref); at worst it is an EMPTY stash
    # entry that carries no information and pushes the real entries further
    # down a shared stack nobody can safely prune.
    recheck_status=$(filter_loom_owned "$(git -C "$main_root" status --porcelain 2>/dev/null || true)")
    if [[ "$MODE" == "baseline" && -r "$MODE_FILE" ]]; then
        recheck_status=$(comm -13 \
            <(sort "$MODE_FILE") \
            <(printf '%s\n' "$recheck_status" | sort) \
            | sed '/^$/d')
    fi
    effective_status="$recheck_status"

    OFFENDING_PATHS=()
    if [[ -n "$effective_status" ]]; then
        collect_offending_paths
    fi

    if [[ "${#OFFENDING_PATHS[@]}" -eq 0 ]]; then
        # Nothing left to rescue. Emit this as its OWN structured result rather
        # than a silent no-op or a misleading "quarantined" line — an empty
        # quarantine attempt means something else resolved the contamination
        # concurrently, which is exactly the race worth seeing in the log.
        emit_quarantine_log "{\"event\":\"main-clean.quarantine\",\"ts\":\"$ts\",\"result\":\"no_op\",\"label\":\"$(json_escape "$QUARANTINE_LABEL")\",\"main\":\"$(json_escape "$main_root")\",\"reason\":\"offending paths were resolved between detection and the stash push; no stash created\",\"paths\":[],\"count\":0}"
        echo "check-main-clean.sh: contamination resolved before it could be quarantined — no stash created." >&2
        echo "       Main worktree: $main_root (now matches the baseline)" >&2
        exit "$EXIT_OK"
    fi

    # Build the JSON path array once — reused by the success and failure entries.
    paths_json=""
    for p in "${OFFENDING_PATHS[@]}"; do
        [[ -n "$paths_json" ]] && paths_json+=","
        paths_json+="\"$(json_escape "$p")\""
    done

    quarantine_fail() {
        # quarantine_fail <reason>
        emit_quarantine_log "{\"event\":\"main-clean.quarantine\",\"ts\":\"$ts\",\"result\":\"failed\",\"label\":\"$(json_escape "$QUARANTINE_LABEL")\",\"main\":\"$(json_escape "$main_root")\",\"reason\":\"$(json_escape "$1")\",\"paths\":[$paths_json],\"count\":${#OFFENDING_PATHS[@]}}"
        echo "ERROR: check-main-clean.sh: could not quarantine main-worktree contamination." >&2
        echo "       Reason: $1" >&2
        echo "       Main worktree: $main_root" >&2
        echo "       The contamination is STILL PRESENT — do not advance the wave." >&2
        echo "       Resolve it manually with a single atomic operation, e.g.:" >&2
        echo "         git -C \"$main_root\" stash push --include-untracked -m \"$stash_msg\" -- <paths>" >&2
        exit "$EXIT_MAIN_DIRTY"
    }

    # ONE operation covering tracked + untracked dirt. Restricted to the
    # offending pathspecs so baselined (pre-existing) dirt is left untouched;
    # `:(literal,top)` anchors each at the repo root and disables glob magic so
    # a path containing `*`/`[` cannot over-match.
    stash_pathspecs=()
    for p in "${OFFENDING_PATHS[@]}"; do
        stash_pathspecs+=(":(literal,top)$p")
    done

    # Remember the stack top BEFORE pushing. `git stash push` exits 0 and
    # creates NOTHING when the pathspec resolves to no local changes ("No local
    # changes to save"), so `refs/stash` alone cannot distinguish "our rescue
    # ref" from "the previous entry, still on top" — comparing the two is what
    # makes the difference detectable (#5185).
    stash_sha_before=$(git -C "$main_root" rev-parse --verify --quiet refs/stash 2>/dev/null || true)

    stash_err=$(git -C "$main_root" stash push --include-untracked \
        -m "$stash_msg" -- "${stash_pathspecs[@]}" 2>&1) || \
        quarantine_fail "git stash push failed: $(printf '%s' "$stash_err" | tr '\n' ' ')"

    stash_sha=$(git -C "$main_root" rev-parse --verify --quiet refs/stash 2>/dev/null || true)
    stash_created=1
    if [[ -z "$stash_sha" || "$stash_sha" == "$stash_sha_before" ]]; then
        stash_created=0
    fi

    # Verify the rescue was COMPLETE: re-run the same detection and require that
    # no new dirt remains. A partial quarantine is exactly the failure mode this
    # mode exists to prevent, so it is reported as a hard failure, not a success.
    post_status=$(filter_loom_owned "$(git -C "$main_root" status --porcelain 2>/dev/null || true)")
    post_effective="$post_status"
    if [[ "$MODE" == "baseline" && -r "$MODE_FILE" ]]; then
        post_effective=$(comm -13 \
            <(sort "$MODE_FILE") \
            <(printf '%s\n' "$post_status" | sort) \
            | sed '/^$/d')
    fi
    if [[ -n "$post_effective" ]]; then
        quarantine_fail "residual dirt remains after stash: $(printf '%s' "$post_effective" | tr '\n' ';')"
    fi

    # No new entry was created, yet main came back clean: the contamination was
    # resolved concurrently in the window the re-derivation above narrowed but
    # cannot close. Report the same distinct no_op result (never a bogus
    # "quarantined" naming a stash this run did not create).
    if [[ "$stash_created" -eq 0 ]]; then
        emit_quarantine_log "{\"event\":\"main-clean.quarantine\",\"ts\":\"$ts\",\"result\":\"no_op\",\"label\":\"$(json_escape "$QUARANTINE_LABEL")\",\"main\":\"$(json_escape "$main_root")\",\"reason\":\"git stash push created no new entry ($(json_escape "$(printf '%s' "$stash_err" | tr '\n' ' ')")); main is already at the baseline\",\"paths\":[$paths_json],\"count\":${#OFFENDING_PATHS[@]}}"
        echo "check-main-clean.sh: nothing was left to quarantine — no stash created." >&2
        echo "       Main worktree: $main_root (matches the baseline)" >&2
        exit "$EXIT_OK"
    fi

    # A stash WAS created but captured nothing. This should be unreachable
    # after the re-derivation above (and modern git refuses to create an empty
    # stash at all), so log it as its own result rather than letting it pass as
    # an ordinary quarantine: an empty entry is pure noise on the shared stack
    # and its existence means the race is still live somewhere.
    if stash_is_empty "$stash_sha"; then
        emit_quarantine_log "{\"event\":\"main-clean.quarantine\",\"ts\":\"$ts\",\"result\":\"quarantined_empty\",\"label\":\"$(json_escape "$QUARANTINE_LABEL")\",\"main\":\"$(json_escape "$main_root")\",\"stash_ref\":\"stash@{0}\",\"stash_commit\":\"$stash_sha\",\"stash_message\":\"$(json_escape "$stash_msg")\",\"reason\":\"stash entry was created but captured nothing (race between detection and push)\",\"paths\":[$paths_json],\"count\":${#OFFENDING_PATHS[@]}}"
        echo "WARNING: check-main-clean.sh: the quarantine stash captured NOTHING (empty entry)." >&2
        echo "       Main worktree: $main_root (matches the baseline)" >&2
        echo "       Empty ref:     $stash_sha  (\"$stash_msg\")" >&2
        echo "       It is safe to drop, and is listed by:" >&2
        echo "         ./.loom/scripts/check-main-clean.sh --list-quarantined" >&2
        exit "$EXIT_QUARANTINED"
    fi

    emit_quarantine_log "{\"event\":\"main-clean.quarantine\",\"ts\":\"$ts\",\"result\":\"quarantined\",\"label\":\"$(json_escape "$QUARANTINE_LABEL")\",\"main\":\"$(json_escape "$main_root")\",\"stash_ref\":\"stash@{0}\",\"stash_commit\":\"$stash_sha\",\"stash_message\":\"$(json_escape "$stash_msg")\",\"paths\":[$paths_json],\"count\":${#OFFENDING_PATHS[@]}}"

    echo "check-main-clean.sh: QUARANTINED ${#OFFENDING_PATHS[@]} contaminating path(s) from the main worktree." >&2
    echo "       Main worktree: $main_root (now matches the baseline)" >&2
    echo "       Rescue ref:    stash@{0} = $stash_sha  (\"$stash_msg\")" >&2
    echo "       Nothing was discarded — recover the diff with:" >&2
    echo "         git -C \"$main_root\" stash show -p $stash_sha" >&2
    echo "       This entry stays outstanding until someone reconciles it; it is listed by" >&2
    echo "       './.loom/scripts/check-main-clean.sh --list-quarantined' and by 'loom status'." >&2
    exit "$EXIT_QUARANTINED"
fi

if [[ -n "$effective_status" ]]; then
    echo "ERROR: MAIN worktree is dirty (uncommitted changes detected)." >&2
    echo "       Main worktree: $main_root" >&2
    echo "" >&2
    echo "       This usually means a builder wrote to the main repo instead of" >&2
    echo "       its issue worktree (cwd reset between tool calls — see #3513/#2802)." >&2
    echo "       Builders MUST capture the absolute worktree path once and use" >&2
    echo "       absolute paths for every Write/Edit/Bash file operation." >&2
    echo "" >&2
    if [[ "$ignored" -gt 0 ]]; then
        echo "       ($ignored pre-existing change(s) ignored via baseline $MODE_FILE)" >&2
        echo "" >&2
    fi
    echo "       Offending changes:" >&2
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        echo "         $line" >&2
    done <<< "$effective_status"
    echo "" >&2
    echo "       Remediation must be ALL-OR-NOTHING (#4380). Do NOT restore these" >&2
    echo "       paths piecemeal with per-file 'git checkout --' / 'rm' — a half-" >&2
    echo "       restored main is worse than either extreme. Re-run this check with" >&2
    echo "       --quarantine to move every offending path (tracked + untracked) to" >&2
    echo "       a stash rescue ref in one operation, then replay the diff into the" >&2
    echo "       owning issue worktree." >&2
    exit "$EXIT_MAIN_DIRTY"
fi

if [[ "$ignored" -gt 0 ]]; then
    echo "check-main-clean.sh: main worktree carries only pre-existing dirt, no new contamination ($main_root)"
    echo "check-main-clean.sh: $ignored pre-existing change(s) ignored via baseline $MODE_FILE"
else
    echo "check-main-clean.sh: main worktree is clean ($main_root)"
fi
exit "$EXIT_OK"
