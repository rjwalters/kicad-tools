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
#       TOKEN_EXPIRED   — 401 / OAuth token expired (skip this token). Also
#                         covers two account-level death phrasings folded in
#                         by issue #6424 rather than a new category (identical
#                         remedy: mark bad, rotate, needs human
#                         re-authorization): "organization has disabled
#                         [Claude subscription access]" and "Failed to
#                         authenticate ... socket connection was closed
#                         unexpectedly". Issue #11205 adds Claude Code's own
#                         "Failed to authenticate. API Error: 401 …" opening
#                         (any 401 message), "Failed to authenticate: OAuth
#                         session expired …" and "API Error: 401 Invalid API
#                         key".
#       TOKEN_EXHAUSTED — quota/weekly/per-model limit hit (rotate). Covers
#                         both the "hit your … limit" family (#3738) and the
#                         per-model "reached your <model> limit" ceiling the
#                         CLI emits today (#4501) — the latter is a safe
#                         over-approximation (the account may still have
#                         headroom on a cheaper model) chosen over a new
#                         model-dimensioned category so no downstream consumer
#                         has to learn a new enum value. Issue #11205 adds
#                         "You've hit your team's shared budget", "Your org is
#                         out of usage", the legacy "Claude AI usage limit
#                         reached|<epoch>".
#       MODEL_CREDITS_EXHAUSTED
#                       — per-model-TIER usage credits ran out (issue #5687):
#                         "You're out of usage credits". Distinct from
#                         TOKEN_EXHAUSTED because the remedy differs in KIND,
#                         not just degree: credits are scoped to a model tier,
#                         so re-dispatching the SAME work on a CHEAPER model
#                         under the SAME account is a valid (often the only
#                         available) remedy — account rotation is not the only
#                         option, and on a single-account host it is not an
#                         option at all. Every account-pool consumer treats it
#                         exactly like TOKEN_EXHAUSTED (rotate, same cooldown,
#                         same retryability), so the daemon path is unchanged;
#                         the distinct NAME exists so the in-session
#                         `/loom:sweep` orchestrator — which dispatches wave
#                         builders through the Task tool and never runs this
#                         classifier as a subprocess — has a precise signature
#                         to key its one-rung-down model fallback on (see
#                         sweep.md, "Credit-exhaustion fallback"), and so
#                         sweep forensics can name the failure class.
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
#   TOKEN_EXPIRED -> SESSION_LIMIT -> TOKEN_EXHAUSTED ->
#   MODEL_CREDITS_EXHAUSTED -> the "No messages returned" RECOVERABLE case.
#   MODEL_CREDITS_EXHAUSTED is checked LAST among the account-side categories
#   (#5687) so every pre-existing vector keeps its pre-#5687 category: the
#   #4501 per-model ceiling ("You've reached your Fable 5 limit. Run
#   /usage-credits to continue …") mentions credits too, and matching the new
#   branch first would have re-classified it. SESSION_LIMIT must precede
#   TOKEN_EXHAUSTED
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
# current documented order, are carried into the native `claude` table.
#
# Test vectors: `defaults/scripts/tests/test-classify-error.sh`, `test-spawn-claude.sh`,
# `test-spawn-codex.sh` and the shared fixtures under
# `defaults/scripts/tests/fixtures/classify_error/`.
#
# Native implementation (issues #11208, #11219): the engine, both provider
# tables and the deny-list live ONLY in `loom-daemon classify-error`
# (loom-daemon/src/classify_error.rs); this file is a thin wrapper. The design
# notes above describe the native classifier's behavior. The two public
# functions delegate to it whenever a daemon binary resolves — see
# `classify_error` below for the exact delegation contract. Nothing here
# carries a pattern table any more (the shell copy was deleted by #11219 to
# end the drift surface); the shared fixtures
# `defaults/scripts/tests/fixtures/classify_error/{vectors,conformance}.tsv`
# drive the Rust tests.
#
# Degraded answer with NO capable daemon (none on PATH, or one predating the
# subcommand), fail-safe by design:
#   classify_error                 -> SUCCESS for exit 0 and TIMEOUT for exit
#                                     124/137 (exit-code short-circuits, still
#                                     in shell); RECOVERABLE for any other
#                                     non-zero exit.
#   classification_is_transient    -> exit 0 (retry, bounded by the caller's
#                                     cap) for every category, matching the
#                                     deny-list's retry-by-default policy
#                                     (#4501).
# External consumers that vendor only this file (miniset's E2B shim) must
# ship a `loom-daemon` with `classify-error` to keep real classification.

# shellcheck disable=SC2120  # OK that callers pass the args; we don't default.

# --- Native delegation (issue #11208) -------------------------------------
#
# requires-daemon: classify-error optional   #11208 — a missing binary, one predating the subcommand, or any "could not ask" answer (non-0/1 exit, empty stdout) degrades to the fail-safe answer documented in the header (RECOVERABLE / retry); never a wrong terminal verdict.
#
# The resolved `loom-daemon` that IMPLEMENTS classify-error, in
# $_CLASSIFY_DAEMON_BIN — resolved ONCE, when this file is sourced
# (classify_error runs in retry loops; a fork per call is fine, a resolution
# walk — which may itself fork a `--version` probe — per call is not). At
# source time, not lazily, because callers almost always invoke
# `classification=$(classify_error …)`: a cache filled inside that command
# substitution would die with the subshell. Empty means "no capable daemon": the degraded answer.
#
# This is `loom_resolve_self_daemon_bin`'s chain — the binary that IMPLEMENTS
# ported logic, NOT $LOOM_DAEMON_BIN (the install this wrapper DRIVES, which
# may predate the subcommand — #7977's category error). locate-daemon-bin.sh
# sits beside this file in every tree that ships the pair, so it is sourced
# when present; a consumer that vendored ONLY this file (miniset's E2B shim)
# gets the two-tier inline chain instead: the explicit $LOOM_DAEMON_SELF_BIN
# seam, then `loom-daemon` on PATH. The variable is assigned straight from
# the resolver so scripts/check-daemon-subcommand-versions.sh sees the calls
# below as daemon calls and holds them to the marker above.
#
# Then ONE capability probe: RECOVERABLE is retryable, so a binary that
# implements the subcommand exits 0. An older daemon (clap's exit 2) or
# anything else that cannot answer is dropped here, once — so it neither
# costs a doomed fork per call nor has an exit 1 misread as `--transient`'s
# "terminal" verdict. Nothing here can turn a version skew into a wrong answer.
_CLASSIFY_ERROR_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${_CLASSIFY_ERROR_LIB_DIR}/locate-daemon-bin.sh" ]]; then
    # shellcheck source=lib/locate-daemon-bin.sh
    # shellcheck disable=SC1091
    source "${_CLASSIFY_ERROR_LIB_DIR}/locate-daemon-bin.sh"
fi
if declare -F loom_resolve_self_daemon_bin >/dev/null 2>&1; then
    _CLASSIFY_DAEMON_BIN="$(loom_resolve_self_daemon_bin)"
elif [[ -x "${LOOM_DAEMON_SELF_BIN:-}" ]]; then
    _CLASSIFY_DAEMON_BIN="${LOOM_DAEMON_SELF_BIN}"
else
    _CLASSIFY_DAEMON_BIN="$(command -v loom-daemon 2>/dev/null || true)"
fi
if [[ ! -x "${_CLASSIFY_DAEMON_BIN}" ]] \
    || ! "${_CLASSIFY_DAEMON_BIN}" classify-error --transient RECOVERABLE </dev/null >/dev/null 2>&1; then
    _CLASSIFY_DAEMON_BIN=""
fi

classify_error() {
    local output="$1"
    local exit_code="$2"
    # Provider selector precedence: explicit 3rd arg > $LOOM_RUNTIME > "claude".
    # Parameter-expansion defaults (not `${3:-}` followed by a separate check)
    # so a 2-arg call (e.g. claude-wrapper.sh) never trips `set -u`.
    local provider="${3:-${LOOM_RUNTIME:-claude}}"

    # 1. Timeout from the `timeout(1)` command — productive cycle, not error.
    #    Provider-independent: a timeout is a timeout regardless of runtime.
    #    Kept in shell (not delegated): no fork needed for an exit-code answer,
    #    and these two short-circuits are engine-level, not table-level.
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

    # 3. Delegate to `loom-daemon classify-error` (#11208; optional — see the
    #    requires-daemon marker above): a missing or
    #    older binary, a failed pipe, or any non-0 exit degrades to the
    #    fail-safe RECOVERABLE below, never to a wrong answer. stdout must be one
    #    uppercase category token: classify mode always prints exactly one,
    #    so anything else (empty included) means "could not ask".
    if [[ -n "$_CLASSIFY_DAEMON_BIN" ]]; then
        local native_result=""
        if native_result="$(printf '%s' "$output" \
                | "$_CLASSIFY_DAEMON_BIN" classify-error --exit-code "$exit_code" --provider "$provider" 2>/dev/null)" \
           && [[ "$native_result" =~ ^[A-Z_]+$ ]]; then
            printf '%s\n' "$native_result"
            return
        fi
    fi

    # 4. Degraded answer (no capable daemon, #11219): the pattern tables live
    #    only in `loom-daemon classify-error`, so with nothing to ask, report
    #    the fail-safe RECOVERABLE for a genuine failure (retried, bounded by
    #    the caller's cap) rather than guess a terminal category.
    echo "RECOVERABLE"
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
# matches the documented contract of the native classifier's catch-all ("unknown non-zero exit, treat as recoverable
# in daemon mode").
#
# Delegates to `loom-daemon classify-error --transient` (#11208) when a
# binary resolves — the SAME deny-list the native classifier enforces, so
# there is one policy with two pinned renderings, not two policies. The exit
# code IS the answer (0 transient, 1 terminal); only a binary that passed
# the capability probe above is asked at all, and any
# other code (2 = "could not ask") must NOT travel — it falls through to the
# degraded answer below (retry).
classification_is_transient() {
    if [[ -n "$_CLASSIFY_DAEMON_BIN" ]]; then
        # `loom-daemon classify-error --transient`: 0 retry, 1 terminal.
        local rc=0
        "$_CLASSIFY_DAEMON_BIN" classify-error --transient "$1" </dev/null 2>/dev/null || rc=$?
        [[ "$rc" -gt 1 ]] || return "$rc"
    fi
    # Degraded answer (no capable daemon, #11219): retry. Retry-by-default is
    # the deny-list's own fail-safe direction (#4501); the caller's retry cap
    # bounds it. The terminal categories are enumerated only in the native
    # classifier, so a daemon-less host cannot tell them apart.
    return 0
}

# `loom_model_class_marker` -- how NARROW a `.bad_tokens` mark may be (#8058) --
# used to live here. It is now `loom-daemon retry-classify model-class`
# (loom_daemon::retry_classify::model_class_marker), ported out of shell by
# #8138 under epic #7810.
#
# It moved rather than shrank because the boundary was already drawn: #8037 put
# every one of claude-wrapper.sh's OTHER rotation predicates behind
# `retry-classify`, and this is the remedy-shaping half of one of them
# (is_account_exhaustion decides to rotate and mark; model-class decides how
# much of the account the mark takes out). The classifier it consults --
# `classify_error` / `classification_is_transient`, above -- is now a
# thin wrapper over `loom-daemon classify-error` (#11208, #11219), which was
# the one half of the #4501 boundary still living only in this file. The #4501
# invariant survives the move in a stronger form: there is exactly ONE policy,
# and it lives in the native classifier.
