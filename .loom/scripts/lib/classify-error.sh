#!/usr/bin/env bash
# classify-error.sh — Classify a (output, exit_code, [provider]) triple into
# an error category.
#
# Source this file (do not exec). Defines two public functions:
#
#   classification_is_transient <category> -> exit 0 when a caller's retry loop
#       should retry, 1 when the category is terminal. THE single source of
#       truth for every retry decision (issue #4501) — see the function's own
#       comment for the deny-list policy and why no caller may keep a private
#       pattern list alongside it.
#
#   classify_error <output> <exit_code> [provider] -> echoes one of:
#       SUCCESS         — exit 0 (regardless of output content)
#       TIMEOUT         — exit 124/137 (productive cycle, not a failure)
#       CWD_DELETED     — working directory was removed
#       TOKEN_EXPIRED   — 401 / OAuth token expired (skip this token)
#       TOKEN_EXHAUSTED — quota/weekly/per-model limit hit (rotate). Covers
#                         both the "hit your … limit" family (#3738) and the
#                         per-model "reached your <model> limit" ceiling the
#                         CLI emits today (#4501) — the latter is a safe
#                         over-approximation (the account may still have
#                         headroom on a cheaper model) chosen over a new
#                         model-dimensioned category so no downstream consumer
#                         has to learn a new enum value.
#       SESSION_LIMIT   — concurrent-session-limit fault (issue #3947): the
#                         account is NOT out of quota, it just cannot start
#                         another *simultaneous* session right now (a capacity
#                         fault from per-token session stacking). Callers must
#                         re-select a different account and retry WITHOUT
#                         marking the token bad — poisoning .bad_tokens for a
#                         transient concurrency limit would wrongly shrink the
#                         healthy pool. Classified BEFORE TOKEN_EXHAUSTED so the
#                         "session limit" wording is not swallowed by the
#                         weekly/usage-limit regex.
#       MODEL_REFUSAL   — model safety classifier refused the turn
#                         (stop_reason "refusal" on a non-zero-exit run);
#                         a routing error, not a quality signal — the sweep
#                         orchestrator drops one ladder rung (e.g. fable→opus)
#                         WITHOUT consuming a Doctor cycle (see sweep.md).
#       RECOVERABLE     — transient (rate limit, 5xx, network, etc.)
#       FATAL           — non-recoverable: retrying the same invocation cannot
#                         succeed because the fault is in the CONFIGURATION,
#                         not the transport or the account. Never returned for
#                         provider `claude` (its table has no FATAL patterns),
#                         so the pre-#4468 claim "currently never returned"
#                         still holds for every Claude caller. The `codex`
#                         table (issue #4468) is the first to use it — e.g.
#                         Codex's trusted-directory refusal and an
#                         unknown-config-key rejection, both of which loop
#                         forever under a RECOVERABLE retry.
#
# Design — engine vs. provider pattern tables (issue #4190):
#   classify_error() is a two-layer design: a shared CLASSIFICATION ENGINE
#   (exit-code-first ordering, the category enum, and the generic-transient
#   fallthrough) plus provider-scoped PATTERN TABLES that hold the actual
#   failure-signature regexes. This keeps a future non-Claude runtime adapter
#   (Codex, Amp, ... — see #4167) additive: register a new provider table
#   instead of editing the shared engine. Both the `claude` and `codex` tables
#   are populated today (the latter filled in by epic #4167 Phase 2, issue
#   #4468, alongside `defaults/scripts/spawn-codex.sh`).
#
#   Provider selector precedence (highest first): optional 3rd positional
#   arg > `$LOOM_RUNTIME` (the same env var `spawn-worker.sh` uses for
#   runtime dispatch, see `.loom/docs/runtime-adapters.md`) > default
#   `"claude"`. Two-arg calls (e.g. `claude-wrapper.sh`, which sources this
#   file and calls `classify_error "$1" "$2"`) are therefore unaffected and
#   classify bit-identically to before this split — this file intentionally
#   does NOT introduce a `LOOM_WORKER` selector (that name belongs to an
#   older fork design; upstream's convention is `LOOM_RUNTIME`).
#
#   An unrecognized provider never errors — it simply has no provider-specific
#   matches and falls straight through to the shared generic-transient table
#   below. Provider tables are ordered case dispatch
#   (sequential pattern checks), NOT associative-array iteration, so match
#   order is always deterministic — bash does not guarantee iteration order
#   for associative arrays.
#
#   Within the `claude` table, match order is significant and preserved from
#   the pre-split implementation: CWD_DELETED -> MODEL_REFUSAL ->
#   TOKEN_EXPIRED -> SESSION_LIMIT -> TOKEN_EXHAUSTED -> the "No messages
#   returned" RECOVERABLE case. SESSION_LIMIT must precede TOKEN_EXHAUSTED
#   because "concurrent session limit" contains the substring "session limit"
#   that the exhaustion regex also matches (#3947) — and, since #4501 widened
#   the exhaustion regex to "reached your <model> limit", it now also contains
#   the "reached your … limit" shape, making that ordering load-bearing for a
#   second reason. MODEL_REFUSAL is matched
#   only when exit_code != 0, preserving the #3233 exit-code-first guarantee
#   (a clean exit whose output merely mentions "refusal" stays SUCCESS).
#
#   Generic transients — rate-limit/429, 5xx, network errors, and the
#   catch-all RECOVERABLE — are provider-INDEPENDENT and apply to every
#   provider, including unknown ones. An unknown provider never produces an
#   error; it always resolves through the generic table.
#
# Design — exit-code-first ordering (issue #3233, preserved by the split
# above):
#   The original lean-genius implementation grepped output BEFORE checking
#   the exit code, which caused false positives on clean exits whose stdout
#   legitimately contained substrings like "500" or "rate limit" (issue
#   #3233). This rewrite checks the exit code first and only inspects output
#   for genuine failures (exit_code != 0). This ordering lives in the shared
#   engine and applies identically regardless of provider.
#
# Design source: this provider-table split is a re-implementation, not a
# cherry-pick, of the engine/table split pioneered in gpeyton/loom@fd5d1da8
# ("feat(errors): per-provider pattern tables in classify-error.sh"). That
# commit predates upstream's SESSION_LIMIT (#3947) and MODEL_REFUSAL
# categories, so it is used here only as a design reference for the
# selector/table shape — all six Claude-specific categories, in their
# current documented order, are carried into the `claude` table below.
#
# Test vectors: the `claude` table's live in
# `defaults/scripts/tests/test-spawn-claude.sh`; the `codex` table's (plus the
# cross-provider isolation checks that prove the claude vectors are unchanged)
# live in `defaults/scripts/tests/test-spawn-codex.sh`.

# shellcheck disable=SC2120  # OK that callers pass the args; we don't default.

# --- Provider pattern tables -------------------------------------------

# _classify_error_claude <output> -> echoes a category, or nothing if no
# Claude-specific pattern matched (caller falls through to the generic
# table). Only called when exit_code != 0.
_classify_error_claude() {
    local output="$1"

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
    # failure (exit_code != 0, guaranteed by the caller) so the exit-code-first
    # #3233 guarantee holds — a clean exit whose output merely mentions
    # "refusal" stays SUCCESS in the shared engine above.
    if echo "$output" | grep -qiE '"?stop_reason"?[[:space:]]*[:=][[:space:]]*"?refusal'; then
        echo "MODEL_REFUSAL"
        return
    fi

    # Token expired (401 auth error) — this specific token is bad
    if echo "$output" | grep -qiE "401[^a-z]*authentication_error|OAuth token has expired|token has expired"; then
        echo "TOKEN_EXPIRED"
        return
    fi

    # Concurrent-session-limit fault (issue #3947) — the account is healthy but
    # cannot start another SIMULTANEOUS session right now. This is a capacity
    # signal from per-token session stacking, NOT quota exhaustion, so it is
    # classified distinctly and callers must NOT mark the token bad. Checked
    # BEFORE TOKEN_EXHAUSTED because "concurrent session limit" contains the
    # substring "session limit" that the exhaustion regex below also matches;
    # the concurrency-specific wording ("concurrent", "simultaneous", "already
    # running") disambiguates a capacity fault from a weekly/usage limit.
    if echo "$output" | grep -qiE "concurrent (session|sessions|request)|maximum number of concurrent|too many concurrent|simultaneous session|another session is (already )?(active|running)"; then
        echo "SESSION_LIMIT"
        return
    fi

    # Token exhausted (quota / session / weekly / usage limit) — rotate to a
    # different token. The phrase set is widened (issue #3738) to cover the
    # multi-word-gap variants the Claude CLI actually emits — "hit your
    # session limit", "hit your weekly limit", an org's "monthly usage limit",
    # and "out of extra usage". A naive `hit.your.limit` pattern misses the
    # "session"/multi-word forms (there is filler between "your" and "limit").
    # This regex is kept in lockstep with claude-wrapper.sh, which sources this
    # file rather than duplicating the pattern (issue #3738) — moving the
    # pattern into this table preserves that single-source property; the
    # wrapper still reaches it only via `classify_error`, never a copy.
    #
    # Issue #4501 adds the "reached your <model> limit" family: the CLI now
    # emits a PER-MODEL ceiling — "You've reached your Fable 5 limit. Run
    # /usage-credits to continue or switch models with /model." — where the
    # model name sits between "your" and "limit", so none of the "hit your …"
    # phrasings above fired and every daemon-dispatched child died permanently
    # at CLI start instead of rotating. `([^[:space:]]+[[:space:]]+){0,3}`
    # bounds the filler to at most three words so the pattern cannot swallow a
    # whole unrelated sentence that merely ends in "limit". Ordered AFTER the
    # SESSION_LIMIT branch above so "reached your concurrent session limit"
    # keeps its distinct capacity classification (#3947).
    #
    # Deliberately reuses TOKEN_EXHAUSTED rather than introducing a
    # model-dimensioned MODEL_LIMIT category: a Fable-only ceiling does not
    # exhaust `sonnet` on the same account, so marking the whole account
    # exhausted is a *safe over-approximation* (correct rotation, slightly
    # pessimistic pool) that needs no change in any downstream consumer
    # (`spawn-codex.sh`'s terminal-result allowlist,
    # `loom-daemon/src/tokens_pool/health.rs`'s `TerminalClassification`,
    # `bad_tokens.rs`/`failure_counts.rs`). Per-model account state is tracked
    # as an explicit follow-up, not smuggled in here.
    if echo "$output" | grep -qiE "hit your (limit|session limit|weekly limit)|hit\.your\.limit|monthly usage limit|out of extra usage|reached your ([^[:space:]]+[[:space:]]+){0,3}limit"; then
        echo "TOKEN_EXHAUSTED"
        return
    fi

    # "No messages returned" — transient API issue specific to the Claude CLI's
    # own wording (only reached when exit_code != 0, per the caller contract)
    if echo "$output" | grep -q "No messages returned"; then
        echo "RECOVERABLE"
        return
    fi

    # No Claude-specific pattern matched — fall through to the generic table.
}

# _classify_error_codex <output> -> echoes a category, or nothing if no
# Codex-specific pattern matched (caller falls through to the generic table).
# Only called when exit_code != 0.
#
# Provider table for the OpenAI Codex CLI adapter (`spawn-codex.sh`, epic #4167
# Phase 2 / issue #4468). Ported from the gpeyton/loom fork's codex table by
# Graham Peyton, then re-grounded against the real CLI: every phrase below was
# either OBSERVED in `codex exec` output on codex-cli 0.146.0 (2026-07-29) or
# extracted verbatim from that binary's own message strings. Nothing here is
# guessed — where a category has no defensible Codex signature it is left
# unimplemented and documented as such rather than filled with Claude wording.
#
# Match order and why:
#   1. FATAL config faults    unambiguous, and they must not be retried.
#   2. TOKEN_EXHAUSTED        BEFORE TOKEN_EXPIRED: OpenAI has served
#                             `insufficient_quota` with a 401 status as well as
#                             429, and mis-classifying a quota fault as an auth
#                             fault is the expensive direction — TOKEN_EXPIRED
#                             marks an account bad with reason `auth` (persists
#                             until a manual `loom-daemon tokens unblock`), whereas
#                             TOKEN_EXHAUSTED uses reason `exhausted` (TTL-
#                             expires on its own). Quota wording is also the
#                             more specific signal, so letting it win is both
#                             safer and more precise.
#   3. TOKEN_EXPIRED          401 / not-signed-in / refresh-token failures.
#
# Deliberately NOT implemented for codex (documented, not forgotten):
#   CWD_DELETED    No Codex-specific signature for "the workspace vanished
#                  mid-run" is known. Claude's "current working directory was
#                  deleted" phrasing is Claude-CLI-specific and is NOT reused.
#   SESSION_LIMIT  Codex exposes no concurrent-session-cap error wording. A
#                  bare 429 stays RECOVERABLE via the generic table, which is
#                  the correct conservative behavior (retry, don't mark bad).
#   MODEL_REFUSAL  The Responses API can emit a refusal content part, but the
#                  Codex CLI has no stable surfaced wording for it, and a
#                  refusal does not reliably produce a non-zero exit.
#
# Sandbox denials are deliberately absent — and that is a FINDING, not an
# omission. A Codex sandbox denial is an in-session tool failure, not a process
# failure: verified on 0.146.0 with `-s read-only`, a blocked `touch` returns
# `Operation not permitted` to the model and the `codex exec` process still
# exits **0**. Exit-code-first classification therefore never reaches this
# table for a denial, and adding an "Operation not permitted" pattern here
# would be dead code that could only ever fire as a false positive on some
# unrelated non-zero exit. What IS classified below is a sandbox/trust fault
# that genuinely aborts the run: the trusted-directory refusal, and Codex
# failing to construct its sandbox at all. See
# `defaults/docs/guardrail-parity-codex.md` for the full trust-boundary map.
_classify_error_codex() {
    local output="$1"

    # --- FATAL: configuration faults that no retry can fix -----------------
    # Observed verbatim on 0.146.0 when `codex exec` runs outside a git work
    # tree: "Not inside a trusted directory and --skip-git-repo-check was not
    # specified." (exit 1, on stderr). `spawn-codex.sh` injects
    # --skip-git-repo-check when the cwd is genuinely not a work tree, so
    # reaching this means something upstream mis-set the cwd — a retry loops.
    #
    # "unknown configuration field" is the CLI's own rejection of a bad
    # `-c key=value` override (observed with --strict-config); also
    # non-retryable.
    #
    # `landlock_sandbox_executable_not_provided` is Codex's internal error code
    # (string present in the 0.146.0 binary) for being unable to construct its
    # Linux sandbox — a host/config problem, not a transient one. Classifying
    # it FATAL is the safe direction: a worker that cannot establish its
    # sandbox must NOT be silently retried into running unsandboxed.
    if echo "$output" | grep -qiE "not inside a trusted directory|unknown configuration field|landlock_sandbox_executable_not_provided"; then
        echo "FATAL"
        return
    fi

    # --- TOKEN_EXHAUSTED: plan/quota exhaustion — rotate, mark `exhausted` ---
    # Strings taken verbatim from the 0.146.0 binary: "You've hit your usage
    # limit.", "You've reached your usage limit.", "You've reached your
    # workspace credit limit", "Your workspace is out of credits". Plus the
    # documented OpenAI API error codes the CLI relays (`rate_limit_exceeded`,
    # `insufficient_quota`, "exceeded your current quota").
    # A BARE 429 with no quota wording is intentionally NOT matched here — it
    # is a transient throttle and belongs to the generic RECOVERABLE table.
    if echo "$output" | grep -qiE "hit your usage limit|reached your usage limit|usage limit reached|workspace credit limit|out of credits|insufficient_quota|rate_limit_exceeded|exceeded your current quota|quota exceeded"; then
        echo "TOKEN_EXHAUSTED"
        return
    fi

    # --- TOKEN_EXPIRED: this credential is bad — skip/re-auth this profile ---
    # `401 Unauthorized` was OBSERVED end-to-end: pointing CODEX_HOME at an
    # unauthenticated profile makes 0.146.0 emit
    #   "failed to connect to websocket: HTTP error: 401 Unauthorized"
    # and exit 1. The remaining phrases are verbatim binary strings for the
    # ChatGPT-login/refresh-token failure modes a rotated `CODEX_HOME` profile
    # actually hits: "Not signed in. Please run 'codex login'", "...refresh
    # token has expired. Please log out and sign in again", "Your session has
    # expired. Please reauthenticate.", "Failed to refresh token", "ChatGPT
    # auth is missing a refresh token". `invalid_api_key` / "incorrect api key"
    # cover the API-key (OPENAI_API_KEY) side.
    if echo "$output" | grep -qiE "401[^a-z]*unauthorized|invalid_api_key|invalid api key|incorrect api key|not signed in|please run .?codex login|refresh token has expired|refresh token was (already used|revoked)|missing a refresh token|no refresh token available|failed to refresh token|session has expired|please reauthenticate"; then
        echo "TOKEN_EXPIRED"
        return
    fi

    # No Codex-specific pattern matched — fall through to the generic table
    # (rate-limit/429, 5xx, network, catch-all RECOVERABLE).
}

# _classify_error_provider <output> <provider> -> dispatches to the matching
# provider table via ordered case dispatch (deterministic; no associative-
# array iteration). Unknown providers intentionally match nothing here and
# fall through to the generic table in classify_error.
_classify_error_provider() {
    local output="$1"
    local provider="$2"

    case "$provider" in
        claude)
            _classify_error_claude "$output"
            ;;
        codex)
            _classify_error_codex "$output"
            ;;
        *)
            # Unknown provider: no provider-specific table, no error — the
            # generic fallback in classify_error covers it.
            ;;
    esac
}

# --- Generic (provider-independent) transient table ---------------------

# _classify_error_generic <output> -> always echoes a category (never empty).
# Applies to every provider, including unknown ones, so an unrecognized
# `provider` value never fails to classify.
_classify_error_generic() {
    local output="$1"

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

    # Catch-all: unknown non-zero exit, treat as recoverable in daemon mode
    echo "RECOVERABLE"
}

# --- Shared classification engine ----------------------------------------

classify_error() {
    local output="$1"
    local exit_code="$2"
    # Provider selector precedence: explicit 3rd arg > $LOOM_RUNTIME > "claude".
    # Parameter-expansion defaults (not `${3:-}` followed by a separate check)
    # so a 2-arg call (e.g. claude-wrapper.sh) never trips `set -u`.
    local provider="${3:-${LOOM_RUNTIME:-claude}}"

    # 1. Timeout from the `timeout(1)` command — productive cycle, not error.
    #    Provider-independent: a timeout is a timeout regardless of runtime.
    if [[ "$exit_code" -eq 124 || "$exit_code" -eq 137 ]]; then
        echo "TIMEOUT"
        return
    fi

    # 2. Exit-code-first: a clean exit is SUCCESS regardless of output content
    #    or provider. This is the critical fix for #3233 — the previous
    #    implementation returned RECOVERABLE for clean exits whose stdout
    #    contained "500", "rate limit", or "No messages returned".
    if [[ "$exit_code" -eq 0 ]]; then
        echo "SUCCESS"
        return
    fi

    # --- Below here, exit_code != 0 (genuine failure). Inspect output. ---

    # 3. Provider-specific table first (e.g. the six Claude categories).
    local provider_result
    provider_result="$(_classify_error_provider "$output" "$provider")"
    if [[ -n "$provider_result" ]]; then
        echo "$provider_result"
        return
    fi

    # 4. Generic, provider-independent transients (always resolves).
    _classify_error_generic "$output"
}

# Convenience predicate matching legacy callers in claude-wrapper.sh.
is_recoverable_error() {
    local classification
    classification=$(classify_error "$1" "$2")
    [[ "$classification" != "FATAL" && "$classification" != "SUCCESS" ]]
}

# --- Retry verdict: the single source of truth (issue #4501) --------------
#
# classification_is_transient <category> -> exit 0 when a caller's retry loop
# SHOULD retry the same invocation, 1 when the failure is terminal for that
# caller. The argument is a category emitted by `classify_error`.
#
# Why this lives here. Before #4501, `claude-wrapper.sh::is_transient_error`
# kept its OWN pattern array, disjoint from this file's tables, so the retry
# decision and the printed `classification=` were two independent verdict
# systems that could — and did — contradict each other in the same log block:
#
#   [ERROR] Non-transient error detected - not retrying
#   [ERROR] exit_code=1 classification=RECOVERABLE
#
# Deriving the verdict from the category makes that contradiction structurally
# impossible: there is now exactly one classifier, and the retry decision is a
# pure function of its output.
#
# Policy is a DENY-LIST. Only categories whose fault cannot be cleared by
# retrying the same invocation are terminal; everything else — including the
# generic RECOVERABLE catch-all for an *unrecognized* non-zero exit — is
# retried, bounded by the caller's own retry cap. Retry-by-default is the
# fail-safe direction for unattended daemon children: an allow-list of known
# transient phrasings has twice turned a new CLI wording into an instant
# permanent death (#4255's bare `Execution error`, #4501's "reached your
# <model> limit"), while an over-retry costs only a bounded backoff. It also
# matches the documented contract of the catch-all in
# `_classify_error_generic` ("unknown non-zero exit, treat as recoverable in
# daemon mode").
classification_is_transient() {
    case "$1" in
        # Terminal — retrying the same invocation cannot succeed:
        #   SUCCESS       nothing to retry.
        #   TIMEOUT       the wall-clock budget was already spent, and a 137 is
        #                 normally an external kill (daemon reaper / OOM) —
        #                 surface it rather than re-spending the budget.
        #   TOKEN_EXPIRED this token needs re-auth, not another attempt.
        #   CWD_DELETED   the working directory is gone.
        #   MODEL_REFUSAL the same model refuses the same turn again; the sweep
        #                 orchestrator drops a ladder rung instead (see
        #                 sweep.md, "Refusal-aware fallback").
        #   FATAL         a configuration fault (the `codex` table's category).
        SUCCESS|TIMEOUT|TOKEN_EXPIRED|CWD_DELETED|MODEL_REFUSAL|FATAL)
            return 1
            ;;
        # Retryable: RECOVERABLE (including the unknown-non-zero-exit
        # catch-all), plus TOKEN_EXHAUSTED and SESSION_LIMIT. The latter two are
        # normally consumed by a caller's account-rotation / re-selection path
        # BEFORE this predicate is reached; they land here only when that path
        # is capped or found no alternate account, where a bounded
        # backoff-and-retry (rather than a permanent death) is exactly what
        # claude-wrapper.sh's own comments already promise.
        *)
            return 0
            ;;
    esac
}
