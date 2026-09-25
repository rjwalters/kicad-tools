#!/usr/bin/env bash
# worktree-removal-log.sh — the bash half of the worktree-removal ledger (#5950)
#
# Loom deletes worktrees from eight independent code paths (`loom-daemon
# clean`, `clean --aggressive`, the daemon's periodic reaper, the daemon's
# terminal-destroy path, `loom-recover-orphans`, `merge-pr.sh`'s post-merge
# cleanup, `worktree.sh remove`, `agent-destroy.sh`), each reporting to its own
# sink. When a Builder's live worktree vanished mid-session (#5950, during
# issue #5919) there was no single place to look for "what removed it?".
#
# Every Loom-owned removal now appends one JSON line to
# `<repo_root>/.loom/logs/worktree-removals.log`. That makes the answer
# symmetric: an entry names the mechanism, and NO entry is itself evidence that
# no Loom path did it (so the search moves to host-level/manual removal).
#
# The line format is byte-identical to the Rust writer
# (`loom-daemon/src/worktree_ops/removal_log.rs`) so one grep/jq reads both.
#
# Usage:
#   source "$SCRIPT_DIR/lib/worktree-removal-log.sh"
#   loom_record_worktree_removal <repo_root> <mechanism> <worktree_path> [branch] [reason]
#
# Strictly best-effort: never fails, never writes to stdout/stderr, and always
# returns 0 — a ledger problem must never break a removal or a caller's `set -e`.

# Guard against double-sourcing.
if [[ -n "${LOOM_WORKTREE_REMOVAL_LOG_SOURCED:-}" ]]; then
  return 0 2>/dev/null || true
fi
LOOM_WORKTREE_REMOVAL_LOG_SOURCED=1

# Escape a value for embedding in a JSON string (the same characters the Rust
# writer escapes: quote, backslash, and the three whitespace controls).
_loom_removal_log_json_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '%s' "$value"
}

# loom_record_worktree_removal <repo_root> <mechanism> <worktree_path> [branch] [reason]
loom_record_worktree_removal() {
  local repo_root="${1:-}" mechanism="${2:-}" worktree_path="${3:-}"
  local branch="${4:-}" reason="${5:-removed}"
  [[ -n "$repo_root" && -n "$mechanism" && -n "$worktree_path" ]] || return 0

  local log_dir="$repo_root/.loom/logs"
  mkdir -p "$log_dir" 2>/dev/null || return 0

  local ts branch_field
  # Issue #8504: trailing `Z`, matching the Rust writer
  # (`removal_log.rs`) this line format must stay byte-identical with.
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)" || return 0
  if [[ -n "$branch" ]]; then
    branch_field="\"$(_loom_removal_log_json_escape "$branch")\""
  else
    branch_field="null"
  fi

  # One printf of one line: O_APPEND makes a sub-PIPE_BUF write atomic, so
  # concurrent removers cannot interleave halves of a record.
  printf '{"ts":"%s","mechanism":"%s","pid":%s,"worktree":"%s","branch":%s,"reason":"%s"}\n' \
    "$(_loom_removal_log_json_escape "$ts")" \
    "$(_loom_removal_log_json_escape "$mechanism")" \
    "$$" \
    "$(_loom_removal_log_json_escape "$worktree_path")" \
    "$branch_field" \
    "$(_loom_removal_log_json_escape "$reason")" \
    >>"$log_dir/worktree-removals.log" 2>/dev/null || return 0

  return 0
}
