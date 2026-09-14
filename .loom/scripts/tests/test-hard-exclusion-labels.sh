#!/usr/bin/env bash
# test-hard-exclusion-labels.sh — regression test for #7528's shared
# hard-exclusion label source.
#
# Covers `defaults/scripts/hard-exclusion-labels.sh`, the shell-side accessor
# for the one list of labels that take an issue out of the automated pipeline
# entirely (`external` today). Before #7528 that list existed only as a
# hardcoded `jq` literal in curator.md and builder.md; the Rust work finder
# knew nothing about it, so a `loom:issue` + `external` row was dispatched
# every tick, declined ~90s later, and had its claim released straight back
# into the candidate pool (23 dispatches in ~1h on kicad-tools#5197).
#
# What this suite pins:
#   - the list itself is non-empty and contains `external` (the fleet-wide
#     floor every Loom role enforces);
#   - each rendering mode (`--lines`, `--json`, `--jq-not`, `--search`) emits
#     every label in the list, in a form the consuming surface can actually
#     use — the `--jq-not` fragment in particular must be a valid jq
#     expression, since the role prompts paste it straight into
#     `gh issue list --jq`;
#   - the `--jq-not` fragment really does filter a hard-excluded issue out of
#     a realistic `gh issue list --json number,labels` payload (the end-to-end
#     property the prompts depend on);
#   - an unknown option is a usage error, not a silently-empty list — an
#     empty list would silently DISABLE every exclusion, which is the failure
#     mode this whole mechanism exists to prevent.
#
# The Rust half of the lockstep (const vs. this script) is asserted by
# `loom-daemon/src/hard_exclusion.rs`'s
# `rust_const_matches_shipped_shell_script` unit test, not here.
#
# Hermetic: no forge, no network, no daemon. Requires `jq` only for the
# fragment-validity checks, which are skipped (with a note) when jq is absent.
#
# Usage:
#   bash defaults/scripts/tests/test-hard-exclusion-labels.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Prefer the installed copy over the source-tree one, so this suite is
# meaningful in an installed consumer repo too (same precedent as
# test-record-noop-release.sh).
resolve_shipped_script() {
    local rel="$1"
    if [[ -f "$REPO_ROOT/.loom/scripts/$rel" ]]; then
        printf '%s\n' "$REPO_ROOT/.loom/scripts/$rel"
    else
        printf '%s\n' "$REPO_ROOT/defaults/scripts/$rel"
    fi
}

SUBJECT="$(resolve_shipped_script "hard-exclusion-labels.sh")"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

passed=0
failed=0
pass() { echo -e "${GREEN}\xe2\x9c\x93${NC} $1"; passed=$((passed + 1)); }
fail() { echo -e "${RED}\xe2\x9c\x97${NC} $1"; failed=$((failed + 1)); }

if [[ ! -f "$SUBJECT" ]]; then
    echo "ERROR: hard-exclusion-labels.sh not found at $REPO_ROOT/.loom/scripts/ or $REPO_ROOT/defaults/scripts/" >&2
    exit 1
fi

echo "=== test-hard-exclusion-labels.sh (#7528) ==="
echo "subject: $SUBJECT"
echo ""

# --- 1. The list --------------------------------------------------------
lines_out="$(bash "$SUBJECT" 2>&1)"
lines_rc=$?

if [[ "$lines_rc" -eq 0 && -n "$lines_out" ]]; then
    pass "default (--lines) mode prints a non-empty list"
else
    fail "expected a non-empty list on exit 0, got rc=$lines_rc: $lines_out"
fi

if printf '%s\n' "$lines_out" | grep -qx 'external'; then
    pass "the list contains \`external\` (the fleet-wide floor)"
else
    fail "the list does not contain \`external\`: $lines_out"
fi

explicit_out="$(bash "$SUBJECT" --lines 2>&1)"
if [[ "$explicit_out" == "$lines_out" ]]; then
    pass "--lines is the default mode"
else
    fail "--lines ($explicit_out) differs from the default ($lines_out)"
fi

# --- 2. Every rendering covers every label -------------------------------
json_out="$(bash "$SUBJECT" --json 2>&1)"
jqnot_out="$(bash "$SUBJECT" --jq-not 2>&1)"
search_out="$(bash "$SUBJECT" --search 2>&1)"

missing=""
while IFS= read -r label; do
    [[ -z "$label" ]] && continue
    [[ "$json_out" == *"\"$label\""* ]] || missing+=" json:$label"
    [[ "$jqnot_out" == *"contains([\"$label\"])"* ]] || missing+=" jq-not:$label"
    [[ "$search_out" == *"-label:\"$label\""* ]] || missing+=" search:$label"
done <<< "$lines_out"

if [[ -z "$missing" ]]; then
    pass "every label appears in --json, --jq-not and --search"
else
    fail "renderings omit:$missing"
fi

if [[ "$json_out" == \[*\] ]]; then
    pass "--json emits a JSON array"
else
    fail "--json did not emit an array: $json_out"
fi

# --- 3. The fragments are actually usable --------------------------------
if command -v jq >/dev/null 2>&1; then
    if printf '%s' "$json_out" | jq -e 'type == "array" and length > 0' >/dev/null 2>&1; then
        pass "--json parses as a non-empty JSON array"
    else
        fail "--json is not valid non-empty JSON: $json_out"
    fi

    # A realistic `gh issue list --json number,labels` payload: #1 is an
    # ordinary ready issue, #5197 is the incident shape (loom:issue + external).
    payload='[
      {"number":1,"labels":[{"name":"loom:issue"},{"name":"loom:curated"}]},
      {"number":5197,"labels":[{"name":"loom:issue"},{"name":"external"}]}
    ]'
    kept="$(printf '%s' "$payload" | jq -r ".[] | select($jqnot_out) | .number" 2>&1)"
    jq_rc=$?
    if [[ "$jq_rc" -ne 0 ]]; then
        fail "--jq-not is not a valid jq expression: $jqnot_out ($kept)"
    elif [[ "$kept" == "1" ]]; then
        pass "--jq-not keeps the ordinary issue and drops the hard-excluded one"
    else
        fail "expected only #1 to survive the --jq-not filter, got: $kept"
    fi
else
    echo "note: jq not installed — skipping the fragment-validity checks" >&2
fi

# --- 4. An unknown option must NOT yield a silently-empty list -----------
bad_out="$(bash "$SUBJECT" --nope 2>&1)"
bad_rc=$?
if [[ "$bad_rc" -ne 0 ]]; then
    pass "an unknown option is a usage error (rc=$bad_rc), never an empty list"
else
    fail "expected a non-zero exit for an unknown option, got 0: $bad_out"
fi

help_out="$(bash "$SUBJECT" --help 2>&1)"
help_rc=$?
if [[ "$help_rc" -eq 0 ]]; then
    pass "--help exits 0"
else
    fail "expected --help to exit 0, got $help_rc: $help_out"
fi

echo ""
echo "=== Results: $passed passed, $failed failed ==="
if [[ "$failed" -gt 0 ]]; then
    exit 1
fi
exit 0
