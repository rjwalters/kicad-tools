#!/usr/bin/env bash
# classify-error.sh — Classify a (output, exit_code) pair into an error category.
#
# Source this file (do not exec). Defines a single function:
#
#   classify_error <output> <exit_code> -> echoes one of:
#       SUCCESS         — exit 0 (regardless of output content)
#       TIMEOUT         — exit 124/137 (productive cycle, not a failure)
#       CWD_DELETED     — working directory was removed
#       TOKEN_EXPIRED   — 401 / OAuth token expired (skip this token)
#       TOKEN_EXHAUSTED — quota/weekly limit hit (rotate)
#       MODEL_REFUSAL   — model safety classifier refused the turn
#                         (stop_reason "refusal" on a non-zero-exit run);
#                         a routing error, not a quality signal — the sweep
#                         orchestrator drops one ladder rung (e.g. fable→opus)
#                         WITHOUT consuming a Doctor cycle (see sweep.md).
#       RECOVERABLE     — transient (rate limit, 5xx, network, etc.)
#       FATAL           — non-recoverable (currently never returned;
#                         reserved for future explicit FATAL signals)
#
# Design — exit-code-first ordering:
#   The original lean-genius implementation grepped output BEFORE checking
#   the exit code, which caused false positives on clean exits whose stdout
#   legitimately contained substrings like "500" or "rate limit" (issue
#   #3233). This rewrite checks the exit code first and only inspects output
#   for genuine failures (exit_code != 0).
#
# Test vectors live in `.loom/scripts/tests/test-spawn-claude.sh`.

# shellcheck disable=SC2120  # OK that callers pass the args; we don't default.

classify_error() {
    local output="$1"
    local exit_code="$2"

    # 1. Timeout from the `timeout(1)` command — productive cycle, not error
    if [[ "$exit_code" -eq 124 || "$exit_code" -eq 137 ]]; then
        echo "TIMEOUT"
        return
    fi

    # 2. Exit-code-first: a clean exit is SUCCESS regardless of output content.
    #    This is the critical fix for #3233 — the previous implementation
    #    returned RECOVERABLE for clean exits whose stdout contained "500",
    #    "rate limit", or "No messages returned".
    if [[ "$exit_code" -eq 0 ]]; then
        echo "SUCCESS"
        return
    fi

    # --- Below here, exit_code != 0 (genuine failure). Inspect output. ---

    # Working directory deleted (worktree cleaned up while CLI ran)
    if echo "$output" | grep -qi "current working directory was deleted"; then
        echo "CWD_DELETED"
        return
    fi

    # Model refusal (safety classifier declined the turn) — a `stop_reason`
    # of "refusal" on a non-zero-exit run. This is a routing error, not a
    # transport failure or a quality signal: the sweep orchestrator responds
    # by dropping one ladder rung (e.g. fable→opus) WITHOUT consuming a Doctor
    # cycle (see sweep.md, "Refusal-aware fallback"). Matched only on a genuine
    # failure (exit_code != 0) so the exit-code-first #3233 guarantee holds — a
    # clean exit whose output merely mentions "refusal" stays SUCCESS above.
    if echo "$output" | grep -qiE '"?stop_reason"?[[:space:]]*[:=][[:space:]]*"?refusal'; then
        echo "MODEL_REFUSAL"
        return
    fi

    # Token expired (401 auth error) — this specific token is bad
    if echo "$output" | grep -qiE "401[^a-z]*authentication_error|OAuth token has expired|token has expired"; then
        echo "TOKEN_EXPIRED"
        return
    fi

    # Token exhausted (quota used up) — rotate to a different token
    if echo "$output" | grep -qiE "hit your (limit|weekly limit)|hit.your.limit"; then
        echo "TOKEN_EXHAUSTED"
        return
    fi

    # Rate limit (429) — transient, retry with backoff
    if echo "$output" | grep -qiE "rate.limit|too.many.requests|429"; then
        echo "RECOVERABLE"
        return
    fi

    # Server errors (5xx) — transient
    if echo "$output" | grep -qiE "500|502|503|504|internal.server.error|service.unavailable"; then
        echo "RECOVERABLE"
        return
    fi

    # Network errors — transient
    if echo "$output" | grep -qiE "ECONNREFUSED|ETIMEDOUT|network.error"; then
        echo "RECOVERABLE"
        return
    fi

    # "No messages returned" — transient API issue (only if exit_code != 0)
    if echo "$output" | grep -q "No messages returned"; then
        echo "RECOVERABLE"
        return
    fi

    # Catch-all: unknown non-zero exit, treat as recoverable in daemon mode
    echo "RECOVERABLE"
}

# Convenience predicate matching legacy callers in claude-wrapper.sh.
is_recoverable_error() {
    local classification
    classification=$(classify_error "$1" "$2")
    [[ "$classification" != "FATAL" && "$classification" != "SUCCESS" ]]
}
