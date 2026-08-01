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
# =============================================================================

# Age thresholds (hours). STALE_HOURS is the ">7d" line handoff.md warns about.
RECENT_HOURS=48
STALE_HOURS=168

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

# Log a diagnostic error message (best-effort, never fails the script).
log_hook_error() {
    local msg="$1"
    mkdir -p "$(dirname "$HOOK_ERROR_LOG")" 2>/dev/null || true
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [session-start-handoff] $msg" >> "$HOOK_ERROR_LOG" 2>/dev/null || true
}

# Top-level error trap: on ANY unexpected error, emit nothing and exit 0. Note
# this exits BEFORE any stdout is produced (the single printf below is the last
# statement in the script), so the trap can never truncate a half-written JSON
# object into malformed output.
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

# Resolve the repo root from cwd (handles worktrees). Fall back to cwd itself
# when it is not a git repo, mirroring the original proposal's
# `root=$(git rev-parse --show-toplevel) || root=$PWD`.
[[ -n "$CWD" && -d "$CWD" ]] || exit 0
REPO_ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null) || REPO_ROOT=""
[[ -n "$REPO_ROOT" ]] || REPO_ROOT="$CWD"

NOTE="$REPO_ROOT/.claude/handoff.md"
[[ -f "$NOTE" && -r "$NOTE" ]] || exit 0

# Portable mtime: BSD/macOS `stat -f %m` vs GNU `stat -c %Y`. If neither works
# we cannot compute age, so skip the banner entirely rather than emit a partial
# one or error out.
MTIME=$(stat -f %m "$NOTE" 2>/dev/null) || MTIME=""
[[ -n "$MTIME" ]] || MTIME=$(stat -c %Y "$NOTE" 2>/dev/null) || MTIME=""
[[ "$MTIME" =~ ^[0-9]+$ ]] || { log_hook_error "could not stat mtime of $NOTE - emitting no context"; exit 0; }

NOW=$(date +%s 2>/dev/null) || NOW=""
[[ "$NOW" =~ ^[0-9]+$ ]] || exit 0

AGE_H=$(( (NOW - MTIME) / 3600 ))
# Clock skew / a future mtime would otherwise produce a negative age. Written as
# an `if` rather than `(( )) &&` so the arithmetic test's non-zero status in the
# common case can never reach the ERR trap.
if (( AGE_H < 0 )); then AGE_H=0; fi

if   (( AGE_H < 1 ));             then AGE_LABEL="just now"
elif (( AGE_H < RECENT_HOURS ));  then AGE_LABEL="${AGE_H}h old"
else                                   AGE_LABEL="$(( AGE_H / 24 ))d old"
fi

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

Read the note in full before doing anything else. Per the /repo:handoff one-shot
contract, once you have absorbed it, delete both the note and its pointer line
in the memory index - promote anything durable into real memory files or issues."

OUTPUT=$(jq -cn --arg ctx "$CONTEXT" \
    '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}' 2>/dev/null) || OUTPUT=""

# Never emit anything if jq could not build well-formed JSON.
[[ -n "$OUTPUT" ]] || { log_hook_error "jq failed to build output JSON - emitting no context"; exit 0; }

printf '%s\n' "$OUTPUT"
exit 0
