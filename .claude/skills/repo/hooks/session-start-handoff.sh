#!/usr/bin/env bash
# session-start-handoff.sh - SessionStart hook that surfaces a pending
# /repo:handoff note at the start of a Claude Code session.
#
# Part of Repo Skills (https://github.com/rjwalters/repo), installed by
# install.sh into .claude/skills/repo/hooks/ and wired into the consumer repo's
# .claude/settings.json under hooks.SessionStart (matchers "startup" and
# "resume").
#
# WHY: /repo:handoff writes .claude/handoff.md and adds a pointer line to the
# agent's memory index. That makes the note findable, but nothing announces it,
# and the command's one-shot contract ("read it, then delete it") means a note
# that never gets absorbed lingers silently. handoff.md calls a stale note "a
# trap of its own". This hook makes both facts visible at session start.
#
# =============================================================================
# STABLE INTERFACE (the contract install.sh/uninstall.sh and the tests rely on)
# =============================================================================
#
# Input:  JSON on stdin (Claude Code SessionStart payload). Fields consumed:
#           .cwd    - the session's working directory
#           .source - "startup" | "resume" | "clear" | "compact" | "fork"
#         Anything else in the payload is ignored.
#
# Output: silence + exit 0  => nothing to surface;  otherwise EXACTLY one JSON
#         object on stdout:
#           { "hookSpecificOutput": { "hookEventName": "SessionStart",
#               "additionalContext": "..." } }
#         The "hookEventName" field is REQUIRED by Claude Code's schema -
#         without it the additionalContext is silently discarded.
#
# Exit:   ALWAYS 0, on every path including internal error. SessionStart has no
#         decision control (a hook cannot block session start), so the only
#         thing a failure here can accomplish is noise in the transcript.
#
# Errors: this script MUST never exit non-zero and never emit invalid output.
#         Any internal error is caught by the ERR trap, logged to
#         <script-dir>/../logs/hook-errors.log, and resolves to silence (fail
#         open) - mirroring guard-destructive.sh's convention.
#
# READ-ONLY: this hook never writes, deletes, or modifies .claude/handoff.md or
#         any other file in the repo. Deleting the note after it is absorbed is
#         the /repo:handoff one-shot contract's job, not this hook's. The only
#         file it may ever write is its own diagnostic error log.
#
# SOURCE GATING: only "startup" and "resume" are acted on. install.sh wires
#         exactly those two matchers, and this script re-checks .source anyway
#         so a hand-edited settings.json that routes "clear"/"compact"/"fork"
#         here still no-ops. A /clear is not a process relaunch and the note has
#         not been touched, so repeating the banner there is pure noise.
#
# AUDIENCE GATING (issue #389): the /repo:handoff note is written for the
#         operator's next interactive session, not for autonomous Loom role
#         agents that also start via "startup"/"resume". Two layers guard
#         against a role agent consuming (and, per the one-shot contract,
#         deleting) a note that was never meant for it:
#           Layer 2 - if ${LOOM_ROLE:-} is non-empty (set on daemon-spawned
#             role sessions - see .loom/hooks/methodology-inject.sh), this
#             hook exits silently before reading the note at all.
#           Layer 1 - LOOM_ROLE only covers daemon-spawned sessions, not an
#             interactive `/loom:<role>` invocation, so the injected
#             additionalContext itself conditions the one-shot deletion
#             directive on the reader being the operator's interactive
#             session; an autonomous/role reader is told to leave the note and
#             its MEMORY.md pointer untouched.
#
# SIBLING VISIBILITY (issue #257), opt-in via the environment:
#   REPO_HANDOFF_SIBLING_ROOT - unset/empty (the default) => byte-identical to
#         the behavior above; the hook never looks outside $REPO_ROOT. Set it to
#         a directory that holds your checkouts (e.g. ~/GitHub) and, ONLY when
#         this repo has no pending note of its own, the hook additionally reports
#         which sibling repos directly under that root do have one - PATH AND AGE
#         ONLY, never the body. That closes the gap where "no note here" and "no
#         note anywhere" were indistinguishable at session start: a note written
#         in repo A left no trace readable from repo B.
#         The scan is deliberately bounded and inert: a single level
#         ("$ROOT"/*/.claude/handoff.md), at most MAX_SIBLING_DIRS entries
#         examined, no recursion, no `cd` into a sibling repo, no git
#         invocation, and no write of any kind. A root that is missing,
#         unreadable, or empty of notes resolves to silence, like every other
#         failure path here.
# =============================================================================

# Age thresholds (hours). STALE_HOURS is the ">7d" line handoff.md warns about.
RECENT_HOURS=48
STALE_HOURS=168

# Sibling-scan bound (issue #257). A configured root is a directory of
# checkouts, not a filesystem to walk: at most this many immediate
# subdirectories are examined, in glob (alphabetical) order, so a root that
# unexpectedly holds thousands of entries cannot make session start pay for it.
MAX_SIBLING_DIRS=64

# Payload sizing (issue #33). When the note is small enough (<= MAX_BODY_BYTES)
# it is cheap to inline verbatim on every launch, and the full body is what
# carries the actionable content - a headers-only outline conveys shape but
# nothing to act on. Above that cap we fall back to a headers-only outline
# (capped at MAX_HEADERS) plus a loud oversize warning, bounding the pathological
# case. The 10 KB cap comfortably covers the ~9 KB real-world note that motivated
# the hook, and /repo:handoff's exclusion discipline is designed to keep notes
# short, so the common case inlines.
MAX_BODY_BYTES=10240
MAX_HEADERS=9

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null || echo ".")"
HOOK_ERROR_LOG="${SCRIPT_DIR}/../logs/hook-errors.log"

# ensure_log_dir <log-file> — create the log file's directory and make that
# directory ignore its own contents (repo#482).
#
# Byte-for-byte the same helper guard-destructive.sh carries, and for the same
# reason: both hooks write into the SAME directory
# (.claude/skills/repo/logs/ in a real install), whichever one happens to run
# first creates it, and install.sh never creates it at all. A `*`-only
# .gitignore dropped in at creation time makes the directory ignore its own
# contents — that .gitignore included — so a consumer needs no .gitignore rule
# of their own for hook runtime output, and an untracked hook-errors.log can no
# longer keep `git status --porcelain` permanently dirty (which is what stalls
# an installed-surface resync that gates on a clean tree).
#
# The two copies are duplicated rather than shared because each hook is
# installed as a standalone script — there is no shared library on the consumer
# side to source. An existing .gitignore is never overwritten.
#
# Best-effort like everything else on this hook's failure path: a failed mkdir
# or write never changes behaviour and never produces a non-zero exit.
ensure_log_dir() {  # <log-file-path>
    local dir
    dir="$(dirname "$1" 2>/dev/null)" || return 0
    [[ -n "$dir" ]] || return 0
    mkdir -p "$dir" 2>/dev/null || return 0
    [[ -e "$dir/.gitignore" ]] && return 0
    # Grouped so a FAILED redirection-open (unwritable dir) has its bash-level
    # error suppressed by the group's stderr redirect too.
    { printf '%s\n' \
        "# Runtime output from the installed Repo Skills hooks (repo#482)." \
        "# Machine-local: these logs routinely carry absolute filesystem paths," \
        "# so they must never be committed. This file ignores the whole" \
        "# directory, itself included, so no consumer .gitignore rule is needed." \
        "*" >"$dir/.gitignore"; } 2>/dev/null || true
    return 0
}

# Log a diagnostic error message (best-effort, never fails the script).
log_hook_error() {
    local msg="$1"
    ensure_log_dir "$HOOK_ERROR_LOG"
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [session-start-handoff] $msg" >> "$HOOK_ERROR_LOG" 2>/dev/null || true
}

# note_age_hours <file> -> echoes whole hours since mtime, or returns 1.
#
# Portable mtime: BSD/macOS `stat -f %m` vs GNU `stat -c %Y`. GNU's `-f` means
# "filesystem status" (not "-f FORMAT"), so it does not fail cleanly - it can
# print a filesystem report. Validate against ^[0-9]+$ before trusting either
# form rather than relying on exit status alone.
note_age_hours() {  # <file>
    local f="$1" mtime now age
    mtime=$(stat -f %m "$f" 2>/dev/null) || mtime=""
    [[ "$mtime" =~ ^[0-9]+$ ]] || { mtime=$(stat -c %Y "$f" 2>/dev/null) || mtime=""; }
    [[ "$mtime" =~ ^[0-9]+$ ]] || return 1
    now=$(date +%s 2>/dev/null) || now=""
    [[ "$now" =~ ^[0-9]+$ ]] || return 1
    age=$(( (now - mtime) / 3600 ))
    # Clock skew / a future mtime would otherwise produce a negative age.
    if (( age < 0 )); then age=0; fi
    printf '%s' "$age"
}

# age_label <hours> -> "just now" | "Nh old" | "Nd old"
age_label() {  # <hours>
    local h="$1"
    if   (( h < 1 ));            then printf 'just now'
    elif (( h < RECENT_HOURS )); then printf '%sh old' "$h"
    else                              printf '%sd old' "$(( h / 24 ))"
    fi
}

# emit_context <text> -> the ONE JSON object this hook is allowed to print.
# Every emitting path funnels through here so the output contract (exactly one
# well-formed object, or silence) has a single implementation. Never emits
# anything when jq could not build valid JSON.
emit_context() {  # <additionalContext text>
    local out
    out=$(jq -cn --arg ctx "$1" \
        '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}' 2>/dev/null) || out=""
    [[ -n "$out" ]] || { log_hook_error "jq failed to build output JSON - emitting no context"; return 0; }
    printf '%s\n' "$out"
}

# sibling_context -> echoes the sibling-note context text, or nothing at all.
#
# Opt-in (issue #257): does nothing unless REPO_HANDOFF_SIBLING_ROOT names an
# existing, readable directory. Called ONLY when this repo has no pending note
# of its own, so a local note always wins and the common single-repo case pays
# nothing. Reports path + age per sibling note and NEVER the body: a handoff is
# one-shot for the repository it belongs to, and this hook is read-only by
# contract (see the READ-ONLY header note above).
#
# Bounded by construction: one glob level, at most MAX_SIBLING_DIRS entries
# examined, no recursion, no `cd`, no git invocation. Any failure resolves to
# silence rather than noise.
sibling_context() {
    local root="${REPO_HANDOFF_SIBLING_ROOT:-}"
    [[ -n "$root" ]] || return 0
    # Trailing slashes would double up in the reported paths ("/repos//foo/...").
    # Strip them, but never reduce "/" itself to the empty string.
    while [[ "$root" == */ && ${#root} -gt 1 ]]; do root="${root%/}"; done
    if [[ ! -d "$root" || ! -r "$root" || ! -x "$root" ]]; then
        log_hook_error "REPO_HANDOFF_SIBLING_ROOT=$root is not a readable directory - skipping sibling scan"
        return 0
    fi

    local dir note examined=0 capped=0 found=0 lines="" hours label
    for dir in "$root"/*/; do
        # An unmatched glob comes back as the literal pattern; -d rejects it.
        [[ -d "$dir" ]] || continue
        if (( examined >= MAX_SIBLING_DIRS )); then capped=1; break; fi
        examined=$(( examined + 1 ))

        # Never report the repo the session is already in.
        [[ "${dir%/}" != "$REPO_ROOT" ]] || continue

        note="${dir}.claude/handoff.md"
        [[ -f "$note" && -r "$note" ]] || continue

        hours=$(note_age_hours "$note") || hours=""
        [[ "$hours" =~ ^[0-9]+$ ]] || continue
        label=$(age_label "$hours")
        if (( hours >= STALE_HOURS )); then
            label="$label, STALE - older than 7 days"
        fi

        found=$(( found + 1 ))
        lines="$lines
  - $note ($label)"
    done

    # Silence is still the answer when there is nothing to say; "no note here"
    # only becomes worth reporting once a note exists somewhere else.
    (( found > 0 )) || return 0

    local noun="note"
    if (( found > 1 )); then noun="notes"; fi

    local ctx="No /repo:handoff note is pending in THIS repository.

A sibling scan found ${found} pending handoff ${noun} in other repositories
under REPO_HANDOFF_SIBLING_ROOT ($root):
$lines"

    if (( capped == 1 )); then
        ctx="$ctx

  (scan stopped after ${MAX_SIBLING_DIRS} directories - later siblings, in
   alphabetical order, were not examined.)"
    fi

    printf '%s' "$ctx

Path and age only - the body of another repository's note is deliberately not
shown, and this hook has not read it. A handoff note is one-shot for the
repository it belongs to: to absorb one of the above, start a session in that
repository, where the ordinary banner will surface it in full.

Do NOT open, act on, or delete another repository's note from this session."
}

# Top-level error trap: on ANY unexpected error, emit nothing and exit 0. Note
# this exits BEFORE any stdout is produced (the only printf to stdout is the
# single already-complete JSON object emit_context writes, immediately before
# the script ends), so the trap can never truncate a half-written JSON object
# into malformed output.
trap 'log_hook_error "Unexpected error on line ${LINENO}: ${BASH_COMMAND:-unknown} (exit=$?)"; exit 0' ERR

# Read stdin safely - if cat fails, the ERR trap fires and we stay silent.
INPUT=$(cat 2>/dev/null) || INPUT=""

# jq is how we both parse the payload and build well-formed output. Without it
# we cannot guarantee valid JSON, so stay silent rather than hand-roll one.
if ! command -v jq &>/dev/null; then
    log_hook_error "jq not found in PATH - emitting no context"
    exit 0
fi

SOURCE=$(printf '%s' "$INPUT" | jq -r '.source // empty' 2>/dev/null) || SOURCE=""
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null) || CWD=""

# Source gating. An absent/unparseable source means malformed input, which
# fails open to silence rather than guessing.
case "$SOURCE" in
    startup|resume) ;;
    *) exit 0 ;;
esac

# Layer 2 (issue #389): autonomous/role sessions are never the intended
# recipient of a handoff note - the one-shot deletion directive below is
# written for a human operator's interactive session and is destructive if a
# role agent obeys it. LOOM_ROLE is set on daemon-spawned role sessions (see
# .loom/hooks/methodology-inject.sh's identical check), so when present we
# skip emitting the banner entirely rather than rely on the reader correctly
# interpreting Layer 1's conditional wording.
if [[ -n "${LOOM_ROLE:-}" ]]; then
    exit 0
fi

# Resolve the repo root from cwd (handles worktrees). Fall back to cwd itself
# when it is not a git repo, mirroring the original proposal's
# `root=$(git rev-parse --show-toplevel) || root=$PWD`.
[[ -n "$CWD" && -d "$CWD" ]] || exit 0
REPO_ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null) || REPO_ROOT=""
[[ -n "$REPO_ROOT" ]] || REPO_ROOT="$CWD"

NOTE="$REPO_ROOT/.claude/handoff.md"
if [[ ! -f "$NOTE" || ! -r "$NOTE" ]]; then
    # No note HERE. Before going silent, the opt-in sibling scan (issue #257)
    # gets a chance to say whether there is one somewhere else - path and age
    # only. Unset root => sibling_context prints nothing => unchanged silence.
    SIB_CONTEXT=$(sibling_context) || SIB_CONTEXT=""
    [[ -n "$SIB_CONTEXT" ]] || exit 0
    emit_context "$SIB_CONTEXT"
    exit 0
fi

AGE_H=$(note_age_hours "$NOTE") || AGE_H=""
[[ "$AGE_H" =~ ^[0-9]+$ ]] || { log_hook_error "could not stat mtime of $NOTE - emitting no context"; exit 0; }
AGE_LABEL=$(age_label "$AGE_H")

CONTEXT="A /repo:handoff note is pending in this repository.

  Path: $NOTE
  Age:  $AGE_LABEL"

if (( AGE_H >= STALE_HOURS )); then
    CONTEXT="$CONTEXT
  STALE: this note is older than 7 days. A handoff describes one moment; verify
         every claim in it against the repo before acting on it."
fi

# Payload policy (issue #33). Under the size cap, inline the full note body -
# that is where the load-bearing, actionable content lives; a headers-only
# outline conveys shape but nothing to act on, which was the observed failure.
# Above the cap, fall back to that outline plus an explicit oversize warning so a
# pathological note cannot bloat every launch. The read-in-full / one-shot
# directive below is appended in BOTH branches.
NOTE_BYTES=$(wc -c < "$NOTE" 2>/dev/null | tr -d '[:space:]') || NOTE_BYTES=""
[[ "$NOTE_BYTES" =~ ^[0-9]+$ ]] || NOTE_BYTES=0

if (( NOTE_BYTES <= MAX_BODY_BYTES )); then
    # Full body inline. `|| BODY=""` keeps a read failure on the fail-open path.
    BODY=$(cat "$NOTE" 2>/dev/null) || BODY=""
    CONTEXT="$CONTEXT

The full note follows between the markers - read it now:

----- BEGIN HANDOFF NOTE -----
$BODY
----- END HANDOFF NOTE -----"
else
    # Oversize fallback: headers only. `|| true` because grep exits 1 when the
    # note has no headers at all (a valid, non-error state) and head closing the
    # pipe early can surface as a non-zero status.
    HEADERS=$(grep -E '^#{1,2} ' "$NOTE" 2>/dev/null | head -n "$MAX_HEADERS" | sed 's/^#* *//' || true)
    CONTEXT="$CONTEXT
  OVERSIZE: this note is ${NOTE_BYTES} bytes, above the ${MAX_BODY_BYTES}-byte
            inline cap, so only its section headers are shown below. Open the
            note and read it in full now - the actionable detail is in the body,
            not these headers."
    if [[ -n "$HEADERS" ]]; then
        CONTEXT="$CONTEXT

Sections:
$(printf '%s\n' "$HEADERS" | sed 's/^/  - /')"
    fi
fi

CONTEXT="$CONTEXT

Read the note in full before doing anything else.

If you are the operator's interactive session: per the /repo:handoff one-shot
contract, once you have absorbed it, delete both the note and its pointer line
in the memory index - promote anything durable into real memory files or issues.

If you are an autonomous or role agent (a Loom role, a headless/scripted run, a
subagent) rather than the operator's interactive session: do NOT act on this
note and do NOT delete it or its pointer - leave both in place for the
operator's next interactive session to absorb."

emit_context "$CONTEXT"
exit 0
