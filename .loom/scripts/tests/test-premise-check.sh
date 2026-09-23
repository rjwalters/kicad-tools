#!/usr/bin/env bash
# test-premise-check.sh — the premise gate's stub + exit-code contract (#8396).
#
# Covers `defaults/scripts/premise-check.sh`, a thin stub over
# `loom-daemon premise-check` (Rust, `loom-daemon/src/premise_check/`). The
# decision logic is unit-tested in Rust next to the code it tests; what this
# suite pins is the part the Rust tests structurally cannot see — the **exit
# codes and stdout keys role prompts branch on**.
#
# That distinction matters because those codes are the whole coupling:
# `curator.md`'s "Before Starting Curation" and
# `sweep-wave-lifecycle.md`'s Curator phase each read this script's exit
# status and act on it. A refactor that renamed a verdict or renumbered a code
# would leave every Rust test green and silently change what a Curator does.
#
# What this suite pins:
#   - 0 for an ordinary issue (the "routine bug fixes are unaffected"
#     acceptance criterion, driven through the real entry point);
#   - 10 when a gated issue has no record — the #7855 reconstruction;
#   - 11 for a consistent deliberate+reversal record;
#   - 12 when a record says deliberate+reversal+clear, the one combination
#     that must be unwritable;
#   - 13 for a premise-false record;
#   - 1 (never 0) on a usage error — the gate fails CLOSED;
#   - the `SCOPE=` / `TRIGGER=` / `VERDICT=` stdout keys exist and are stable;
#   - #8499: citations resolve against the INVOKING working tree, driven
#     through a real linked worktree whose primary clone lacks the cited path.
#     A Rust unit test cannot see this — it needs the `.git` pointer file and
#     a process cwd, which is precisely what this entry point supplies.
#
# Needs a BUILT `loom-daemon` (the subject is a stub over a Rust subcommand) —
# same shape as test-skip-labels.sh, wired in the "Native Port Suites" CI job,
# not shell-suite-tests.
#
# Usage:
#   cargo build --package loom-daemon
#   bash defaults/scripts/tests/test-premise-check.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "premise-check"

resolve_shipped_script() {
    local rel="$1"
    if [[ -f "$REPO_ROOT/.loom/scripts/$rel" ]]; then
        printf '%s\n' "$REPO_ROOT/.loom/scripts/$rel"
    else
        printf '%s\n' "$REPO_ROOT/defaults/scripts/$rel"
    fi
}

SUBJECT="$(resolve_shipped_script "premise-check.sh")"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

passed=0
failed=0
pass() { echo -e "${GREEN}\xe2\x9c\x93${NC} $1"; passed=$((passed + 1)); }
fail() { echo -e "${RED}\xe2\x9c\x97${NC} $1"; failed=$((failed + 1)); }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/premise-check-test-XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# A miniature tree the citations resolve against, so no assertion depends on a
# path in the real repo that a later commit may move (#8310's whole defect).
FIXTURE="$WORK/tree"
mkdir -p "$FIXTURE/src" "$FIXTURE/docs/adr"
printf '// No automatic kill/restart is attempted (deliberate).\n' >"$FIXTURE/src/watchdog.rs"
printf '# ADR 9\n' >"$FIXTURE/docs/adr/0009-example.md"

OUT="$WORK/out.txt"

# run <label> <expected-rc> -- <args...>
run() {
    local label="$1" want="$2"
    shift 3
    bash "$SUBJECT" "$@" >"$OUT" 2>&1
    local rc=$?
    if [[ "$rc" -eq "$want" ]]; then
        pass "$label (exit $rc)"
    else
        fail "$label: expected exit $want, got $rc"
        sed 's/^/    /' "$OUT"
    fi
}

# run_in <cwd> <label> <expected-rc> -- <args...>
# Same as `run`, from a chosen working directory and WITHOUT --repo-root, so
# the default root resolution is what is under test (#8499).
run_in() {
    local cwd="$1" label="$2" want="$3"
    shift 4
    (cd "$cwd" && bash "$SUBJECT" "$@") >"$OUT" 2>&1
    local rc=$?
    if [[ "$rc" -eq "$want" ]]; then
        pass "$label (exit $rc)"
    else
        fail "$label: expected exit $want, got $rc"
        sed 's/^/    /' "$OUT"
    fi
}

has_line() {
    local label="$1" pattern="$2"
    if grep -qE "$pattern" "$OUT"; then
        pass "$label"
    else
        fail "$label: no line matching /$pattern/ in:"
        sed 's/^/    /' "$OUT"
    fi
}

marker() { printf '<!-- loom:premise-check %s -->\n' "$1"; }

echo "=== premise-check.sh: the gate's exit-code contract ==="

# ---------------------------------------------------------------------------
# 1. An ordinary issue is untouched. This is #8396's "routine bug fixes with a
#    verifiable acceptance criterion are demonstrably unaffected" criterion,
#    exercised through the real entry point rather than asserted in prose.
# ---------------------------------------------------------------------------
cat >"$WORK/ordinary.md" <<'EOF'
## Problem Statement

`spawn-codex.sh` forwards a `model@effort` suffix verbatim to the Codex CLI's
`-m` flag, which rejects it.

## Steps to Reproduce

1. Configure `gpt-5.2-codex@high`.
2. Spawn a codex worker.

## Expected

The suffix is split off before `-m`.

## Acceptance Criteria

- [ ] `spawn-codex.sh` passes only the bare model id to `-m`.
EOF
run "ordinary bug report proceeds" 0 -- --body-file "$WORK/ordinary.md" \
    --title "spawn-codex.sh forwards a model@effort suffix verbatim" \
    --repo-root "$FIXTURE" --no-scan
has_line "  reports SCOPE=out" '^SCOPE=out$'
has_line "  reports VERDICT=out-of-scope" '^VERDICT=out-of-scope$'

# ---------------------------------------------------------------------------
# 2. A proposal-labelled issue with no record: the gate is CLOSED (10).
# ---------------------------------------------------------------------------
printf 'Simplify the retry ladder.\n' >"$WORK/proposal.md"
run "proposal with no record is blocked" 10 -- --body-file "$WORK/proposal.md" \
    --labels "loom:hermit,loom:triage" --repo-root "$FIXTURE" --no-scan
has_line "  names the label that gated it" '^TRIGGER=label:loom:hermit$'
has_line "  reports VERDICT=record-required" '^VERDICT=record-required$'

# ---------------------------------------------------------------------------
# 3. #7855, reconstructed: an incident report, no record, no proposal label.
# ---------------------------------------------------------------------------
cat >"$WORK/incident.md" <<'EOF'
## What happened (2026-09-16, robb-pro, launchd)

- `~/.loom/logs/daemon-watchdog.log` logged `[DIVERGENCE] daemon IPC
  UNRESPONSIVE (CONFIRMED)` twice — and took no action.

## Acceptance criteria

- [ ] The watchdog acts on a CONFIRMED IPC-unresponsive daemon after a bounded
      number of confirmations, instead of only recording DIVERGENCE lines.
EOF
run "#7855-shaped incident report is gated" 10 -- --body-file "$WORK/incident.md" \
    --title "the watchdog CONFIRMED the wedge twice and never restarted it" \
    --repo-root "$FIXTURE" --no-scan
has_line "  names the incident heading" '^TRIGGER=incident-report:What happened'

# ---------------------------------------------------------------------------
# 4. The load-bearing rule: deliberate + reversal cannot be cleared (12).
#    This is #7855's actual reasoning, and it must not parse.
# ---------------------------------------------------------------------------
{
    marker "exists=yes deliberate=yes reversal=yes verdict=clear"
    printf 'premise-evidence: src/watchdog.rs:1 — no automatic kill/restart\n'
} >"$WORK/record-selfapproved.md"
run "deliberate+reversal+clear is malformed" 12 -- --body-file "$WORK/incident.md" \
    --record-file "$WORK/record-selfapproved.md" --repo-root "$FIXTURE" --no-scan
has_line "  the reason names the required verdict" 'verdict=operator-decision'

# ---------------------------------------------------------------------------
# 5. The same finding, routed: 11.
# ---------------------------------------------------------------------------
{
    marker "exists=yes deliberate=yes reversal=yes verdict=operator-decision"
    printf 'premise-evidence: src/watchdog.rs:1 — no automatic kill/restart\n'
} >"$WORK/record-routed.md"
run "deliberate+reversal routed to the operator" 11 -- --body-file "$WORK/incident.md" \
    --record-file "$WORK/record-routed.md" --repo-root "$FIXTURE" --no-scan
has_line "  reports VERDICT=operator-decision" '^VERDICT=operator-decision$'
has_line "  echoes the record fields" '^SUMMARY=exists=yes deliberate=yes reversal=yes'

# ---------------------------------------------------------------------------
# 6. Nothing deliberate found, and the search is shown: 0.
# ---------------------------------------------------------------------------
{
    marker "exists=yes deliberate=no reversal=no verdict=clear"
    printf 'premise-searched: src/watchdog.rs\npremise-searched: docs/adr/0009-example.md\n'
} >"$WORK/record-clear.md"
run "a clear record lets curation proceed" 0 -- --body-file "$WORK/incident.md" \
    --record-file "$WORK/record-clear.md" --repo-root "$FIXTURE" --no-scan
has_line "  reports VERDICT=proceed" '^VERDICT=proceed$'

# ---------------------------------------------------------------------------
# 7. deliberate=no with nothing searched is malformed: absence is not evidence.
# ---------------------------------------------------------------------------
marker "exists=yes deliberate=no reversal=no verdict=clear" >"$WORK/record-bare.md"
run "deliberate=no must show what was read" 12 -- --body-file "$WORK/incident.md" \
    --record-file "$WORK/record-bare.md" --repo-root "$FIXTURE" --no-scan
has_line "  the reason names premise-searched" 'premise-searched'

# ---------------------------------------------------------------------------
# 8. A citation that does not resolve is malformed (#8310's defect, mechanised).
# ---------------------------------------------------------------------------
{
    marker "exists=yes deliberate=yes reversal=yes verdict=operator-decision"
    printf 'premise-evidence: defaults/scripts/cli/loom-daemon-watchdog.sh:91 — moved in #8086\n'
} >"$WORK/record-stale.md"
run "an unresolvable citation is malformed" 12 -- --body-file "$WORK/incident.md" \
    --record-file "$WORK/record-stale.md" --repo-root "$FIXTURE" --no-scan
has_line "  the reason says it does not resolve" 'does not resolve'

# ---------------------------------------------------------------------------
# 9. exists=no is its own outcome (13), not a route and not a proceed.
# ---------------------------------------------------------------------------
{
    marker "exists=no deliberate=no reversal=no verdict=clear"
    printf 'premise-searched: src/watchdog.rs\n'
} >"$WORK/record-false.md"
run "premise-false gets its own code" 13 -- --body-file "$WORK/incident.md" \
    --record-file "$WORK/record-false.md" --repo-root "$FIXTURE" --no-scan
has_line "  reports VERDICT=premise-false" '^VERDICT=premise-false$'

# ---------------------------------------------------------------------------
# 10. Fail-closed: a usage error is 1, never 0. A caller that only checks for a
#     zero exit must not be told "proceed" because the gate could not run.
# ---------------------------------------------------------------------------
run "no input at all is an error, not a pass" 1 -- --repo-root "$FIXTURE"
run "an unreadable body file is an error" 1 -- --body-file "$WORK/nope.md" --repo-root "$FIXTURE"

# ---------------------------------------------------------------------------
# 11. A prose mention of the marker is not a record — this very repo's docs and
#     role prompts quote the syntax, and none of them may gate anything.
# ---------------------------------------------------------------------------
printf 'Post a loom:premise-check record before curating.\n' >"$WORK/record-prose.md"
run "a prose mention is not a record" 10 -- --body-file "$WORK/incident.md" \
    --record-file "$WORK/record-prose.md" --repo-root "$FIXTURE" --no-scan
has_line "  still reports RECORD=absent" '^RECORD=absent$'

# ---------------------------------------------------------------------------
# 12. #8499: a citation resolves against the INVOKING WORKING TREE, not the
#     shared primary clone it was created from.
#
#     Loom's whole model is that the primary clone sits on `main` while N
#     worktrees run ahead of it, so a citation written inside a worktree
#     routinely names a file the primary clone does not have yet. Resolving it
#     against the primary clone returned RECORD-MALFORMED (12) — "does not
#     resolve in this checkout" — for a record that was entirely correct, and
#     exit 12 is what curator.md and the sweep orchestrator read as "do not
#     enrich". It failed the dangerous way too: a citation that resolves ONLY
#     in the stale primary clone was reported as resolving.
#
#     The fixture is a real linked worktree, because the defect lives in the
#     `.git` pointer-file dereference that only a real one has.
# ---------------------------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
    fail "git is required to exercise the linked-worktree citation root (#8499)"
else
    mkdir -p "$WORK/primary"
    # `pwd -P` so the fixture path matches the canonicalized root the gate
    # prints (on macOS $TMPDIR is a /var -> /private/var symlink).
    PRIMARY="$(cd "$WORK/primary" && pwd -P)"
    mkdir -p "$PRIMARY/.loom" "$PRIMARY/src" "$PRIMARY/docs"
    git init -q -b main "$PRIMARY"
    git -C "$PRIMARY" config user.email "premise-check-test@example.com"
    git -C "$PRIMARY" config user.name "premise-check test"
    printf '{}\n' >"$PRIMARY/.loom/config.json"
    printf '// No automatic kill/restart is attempted (deliberate).\n' \
        >"$PRIMARY/src/watchdog.rs"
    git -C "$PRIMARY" add -A >/dev/null
    git -C "$PRIMARY" commit -qm "base"

    # A branch that ADDS the cited file, then put the primary clone back on
    # `main` — exactly the "primary is behind" state the bug needs.
    git -C "$PRIMARY" checkout -q -b feature/issue-8499
    printf '# Landed on the branch, not yet on main.\n' >"$PRIMARY/docs/ahead.md"
    git -C "$PRIMARY" add -A >/dev/null
    git -C "$PRIMARY" commit -qm "add docs/ahead.md"
    git -C "$PRIMARY" checkout -q main

    LINKED="$PRIMARY/.loom/worktrees/issue-8499"
    git -C "$PRIMARY" worktree add -q "$LINKED" feature/issue-8499 2>/dev/null
    [[ -d "$LINKED" ]] && LINKED="$(cd "$LINKED" && pwd -P)"

    if [[ ! -f "$LINKED/docs/ahead.md" || -f "$PRIMARY/docs/ahead.md" ]]; then
        fail "fixture: expected docs/ahead.md only in the linked worktree"
    else
        {
            marker "exists=yes deliberate=no reversal=no verdict=clear"
            printf 'premise-searched: docs/ahead.md\n'
        } >"$WORK/record-ahead.md"

        run_in "$LINKED" "a worktree-only citation resolves from that worktree" 0 -- \
            --body-file "$WORK/incident.md" --record-file "$WORK/record-ahead.md" --no-scan
        has_line "  reports VERDICT=proceed" '^VERDICT=proceed$'

        # The control: the same record, run from the behind primary clone, is
        # still malformed. Without this the test above could pass vacuously.
        run_in "$PRIMARY" "the same citation is unresolvable in the behind clone" 12 -- \
            --body-file "$WORK/incident.md" --record-file "$WORK/record-ahead.md" --no-scan
        has_line "  the reason names the root it resolved against" "REASON=.*$PRIMARY"

        # ...and the inverse direction: a citation that exists ONLY in the
        # stale primary clone must NOT be reported as resolving from the
        # worktree.
        printf 'stale\n' >"$PRIMARY/untracked-in-primary-only.md"
        {
            marker "exists=yes deliberate=no reversal=no verdict=clear"
            printf 'premise-searched: untracked-in-primary-only.md\n'
        } >"$WORK/record-primary-only.md"
        run_in "$LINKED" "a primary-clone-only citation does not resolve from the worktree" 12 -- \
            --body-file "$WORK/incident.md" \
            --record-file "$WORK/record-primary-only.md" --no-scan
        has_line "  the reason names the worktree it resolved against" "REASON=.*$LINKED"
    fi

    git -C "$PRIMARY" worktree remove --force "$LINKED" >/dev/null 2>&1
fi

echo
echo "passed: $passed, failed: $failed"
[[ "$failed" -eq 0 ]]
