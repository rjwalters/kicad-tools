#!/usr/bin/env bash
# test-version-bump-workflow-identity.sh - Regression guard for HOW
# .github/workflows/version-bump-on-merge.yml publishes its bump commit
# (issues #7743, #7829, #12063).
#
# Background: that workflow is the single post-merge owner of VERSION and the
# other version-bearing files (#7743). `main`'s ruleset requires a pull
# request, so the bump commit needs an identity the ruleset lets through. It
# used to be the `loom-fleet-dispatch` App, minted in the workflow and pushed
# with directly (#7829), which needed a ruleset bypass that could also skip
# required checks. Since #12063 the workflow never pushes: it POSTs the bump
# to the fleet publisher's /v1/publish_version_bump endpoint, authenticated
# by a GitHub Actions OIDC token, and the publisher commits it
# as `loom-fleet-publisher` under a fixed policy (the version files only, one
# patch step, compare-and-swap on the parent we name).
#
# Why a test and not just review: every property below is invisible at a
# glance and silently reopens a hole if dropped -- an App-token step put back
# "to be safe", a `git push` fallback after a refusal, `id-token: write` moved
# to the job that runs the toolchain actions (every step of such a job can
# mint the publisher's token), or a bearer value passed to curl in argv.
#
# Comment-blindness (PR #7891 review): every assertion about what the workflow
# DOES runs against a comment-stripped view of the file (`strip_comments`):
# full-line `#` comments are blanked and trailing ` # ...` comments outside
# quotes are cut, with line numbers preserved so ordering diagnostics still
# point at the real file. Without that, a commented-out line still satisfies
# `grep -F` over the raw file. This is a comment stripper, not a YAML parser:
# it does not understand block scalars or multi-line strings, and it is only
# as good as the mutation controls in Case 8 that pin it.
#
# What this asserts:
#   1. Publisher wiring: the publish step calls an https
#      /v1/publish_version_bump URL; the OIDC token is requested from
#      ACTIONS_ID_TOKEN_REQUEST_URL with that URL's origin as its audience
#      (the publisher refuses any other); no App token is minted and no
#      `secrets.` value is read anywhere. The host itself is not named here:
#      this file ships under defaults/ (check-vendored-private-refs.sh).
#   2. Least privilege: the workflow-level default token is read-only, no
#      `permissions:` block grants `contents: write`, and `id-token: write`
#      appears exactly once, in the `publish` job.
#   3. No direct push path remains: no `git push`, no `secrets.GITHUB_TOKEN` /
#      `github.token`, no `github-actions[bot]` -- the publisher is the only
#      way a bump reaches main, with no fallback when it refuses.
#   4. The minting job is minimal: `publish` runs no action (no `uses:`), needs
#      `bump-version`, and runs only when the gate said bump; the toolchain
#      actions are in `bump-version`, which has no id-token.
#   5. Failure behaviour and credentials: every run: script starts with
#      `set -euo pipefail`; both bearer values reach curl on stdin
#      (`--config -`), never as an argv `-H` header; the minted token is
#      masked; a refusal fails red.
#   6. The no-self-trigger invariant (#7743): the push trigger is exactly
#      `push` -> `main` -> `defaults/**`, and the version-file sets the bump is
#      computed and sent from hold the version files and nothing under
#      `defaults/` (nor CLAUDE.md, #8147). The publisher's App token DOES
#      trigger workflows, which makes the path filter the real loop guard.
#   7. Serialized-not-cancelled concurrency and the bounded retry survive, and
#      every attempt re-reads main.
#   9. The release-cadence gate (#11174): hourly `schedule` + `workflow_dispatch`
#      (`force`/`reason`) triggers exist; checkout has full history (blob:none)
#      for the last-bump lookup; the gate step runs scripts/version-bump-gate.sh
#      before the toolchain steps, which are gated on its verdict; the gate is
#      re-checked inside the publish loop before each POST; `inputs.reason`
#      only reaches a shell through `env:`; RELEASE_MIN_INTERVAL comes only
#      from `vars.`; a forced bump's message records actor + reason; and the
#      workflow makes no `gh` call at all.
#  10. The publisher's response contract: 200 is success, a 409 and a 503
#      upstream_ambiguous re-read main and re-run the gate, and anything not
#      named fails red.
#   8. Mutation controls (durable, not ad-hoc): the suite re-runs itself
#      against mutated copies of the workflow and requires each one to FAIL on
#      the specific assertion that guards it. The unmodified workflow is the
#      positive control. This is what stops the comment-blindness hole from
#      reopening.
#
# Any of these that changes deliberately should be updated here in the same
# PR -- a failure means "confirm this was intended", not "revert blindly".
#
# Source-tree-only by design (#6194): .github/workflows/ lives at the repo
# root, not under defaults/, so it is never shipped into an installed consumer
# repo. This suite SKIPs (exit 0) rather than errors when run outside Loom's
# own checkout.
#
# Usage:
#   bash .loom/scripts/tests/test-version-bump-workflow-identity.sh
#
# Internal (Case 8 re-entry): LOOM_VBW_WORKFLOW=<path> points the suite at an
# alternate workflow file and disables Case 8 so a child cannot recurse.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SELF="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"

if [[ -n "${LOOM_VBW_WORKFLOW:-}" ]]; then
    WORKFLOW="$LOOM_VBW_WORKFLOW"
    RUN_MUTATION_CONTROLS=0
else
    WORKFLOW="$REPO_ROOT/.github/workflows/version-bump-on-merge.yml"
    RUN_MUTATION_CONTROLS=1
fi

# Colors (off in Case 8 child mode so the parent can grep plain `FAIL: ...`)
if [[ "$RUN_MUTATION_CONTROLS" -eq 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    NC=''
fi

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
    shift
    local detail
    for detail in "$@"; do
        echo "    $detail"
    done
}

# stdin -> stdout: the executable view of a YAML(+embedded shell) file.
#   * a `#` outside single/double quotes that starts the line or follows
#     whitespace begins a comment; the rest of the line is dropped
#   * `#` inside quotes, or glued to a preceding non-space char (`a#b`), stays
#   * backslash-escapes inside double quotes are skipped, so `\"` cannot
#     toggle quote state
#   * lines are never removed, only emptied, so line numbers match the source
strip_comments() {
    awk '{
        line = $0; out = ""; in_s = 0; in_d = 0; n = length(line)
        for (i = 1; i <= n; i++) {
            c = substr(line, i, 1)
            if (in_d && c == "\\") { out = out c substr(line, i + 1, 1); i++; continue }
            if (c == "\"" && !in_s) { in_d = !in_d }
            else if (c == "\x27" && !in_d) { in_s = !in_s }
            else if (c == "#" && !in_s && !in_d) {
                if (i == 1 || substr(line, i - 1, 1) ~ /[ \t]/) break
            }
            out = out c
        }
        sub(/[ \t]+$/, "", out)
        print out
    }'
}

# The comment-stripped workflow. Every assertion about behaviour reads this,
# never the raw file -- see the comment-blindness note in the header.
workflow_code() {
    strip_comments < "$WORKFLOW"
}

# Literal substring must be present on an executable (non-comment) line.
assert_present() {
    local needle="$1" msg="$2"
    if workflow_code | grep -F -- "$needle" >/dev/null; then
        pass "$msg"
    else
        fail "$msg" "Expected to find literally (outside comments): $needle"
    fi
}

# Literal substring must be absent from every executable line. Comments are
# stripped first so the workflow's own prose (which necessarily NAMES
# GITHUB_TOKEN to explain why it is unusable here) does not trip a check about
# what the workflow actually DOES.
assert_absent_from_code() {
    local needle="$1" msg="$2" hits
    hits="$(workflow_code | grep -nF -- "$needle" || true)"
    if [[ -z "$hits" ]]; then
        pass "$msg"
    else
        fail "$msg" "Unexpected executable reference to: $needle" "$hits"
    fi
}

# Regex must match an executable line.
assert_matches() {
    local pattern="$1" msg="$2"
    if workflow_code | grep -E -- "$pattern" >/dev/null; then
        pass "$msg"
    else
        fail "$msg" "No executable line matched: $pattern"
    fi
}

# Regex must match a RAW line. Only for assertions ABOUT comments (the
# human-readable `# vX.Y.Z` beside a SHA pin); never for behaviour.
assert_matches_raw() {
    local pattern="$1" msg="$2"
    if grep -qE -- "$pattern" "$WORKFLOW"; then
        pass "$msg"
    else
        fail "$msg" "No line matched: $pattern"
    fi
}

assert_no_match() {
    local pattern="$1" msg="$2" hits
    hits="$(workflow_code | grep -nE -- "$pattern" || true)"
    if [[ -z "$hits" ]]; then
        pass "$msg"
    else
        fail "$msg" "Unexpected executable match for: $pattern" "$hits"
    fi
}

# First line number whose executable text contains a literal ("" when absent).
line_of() {
    workflow_code | grep -nF -- "$1" | sed -n '1p' | cut -d: -f1
}

# Assert one literal appears strictly before another in (executable) file order.
assert_order() {
    local first="$1" second="$2" msg="$3"
    local a b
    a="$(line_of "$first")"
    b="$(line_of "$second")"
    if [[ -z "$a" || -z "$b" ]]; then
        fail "$msg" "Could not locate both anchors outside comments (first='$a' second='$b')" \
            "  first : $first" "  second: $second"
    elif [[ "$a" -lt "$b" ]]; then
        pass "$msg (line $a before line $b)"
    else
        fail "$msg" "Expected line $a ('$first') to come before line $b ('$second')"
    fi
}


# The executable lines of one job: from `  <name>:` under `jobs:` to the next
# job (or the end of the file), comment-stripped.
job_block() {
    workflow_code | awk -v job="  $1:" '
        /^jobs:$/ { in_jobs = 1; next }
        in_jobs && $0 == job { f = 1; next }
        in_jobs && f && /^  [A-Za-z0-9_-]+:$/ { exit }
        f { print }
    '
}

if [[ ! -f "$WORKFLOW" ]]; then
    if [[ "$RUN_MUTATION_CONTROLS" -eq 1 ]]; then
        echo "SKIP: source-tree-only test, $WORKFLOW not found (not shipped into an installed repo)" >&2
        exit 0
    fi
    echo "ERROR: LOOM_VBW_WORKFLOW=$WORKFLOW does not exist" >&2
    exit 2
fi

echo "Testing version-bump-on-merge.yml publishing path ($WORKFLOW)"
echo ""

PUBLISH_JOB="$(job_block publish)"
BUMP_JOB="$(job_block bump-version)"

# ---------------------------------------------------------------------------
# Case 1: publisher wiring
# ---------------------------------------------------------------------------
echo "Case 1: publisher wiring"

PUBLISH_URL_VALUE="$(sed -n 's/^ *PUBLISH_URL: //p' <<<"$PUBLISH_JOB" | sed -n '1p')"
PUBLISH_ORIGIN="${PUBLISH_URL_VALUE%/v1/publish_version_bump}"
if [[ "$PUBLISH_URL_VALUE" =~ ^https://[a-z0-9.-]+/v1/publish_version_bump$ ]]; then
    pass "Case 1: the publish job targets an https /v1/publish_version_bump URL"
else
    fail "Case 1: the publish job targets an https /v1/publish_version_bump URL" \
        "PUBLISH_URL in the publish job was: '${PUBLISH_URL_VALUE:-<none>}'"
fi

if [[ -n "$PUBLISH_URL_VALUE" ]] \
    && grep -qF "\"\$ACTIONS_ID_TOKEN_REQUEST_URL&audience=${PUBLISH_ORIGIN}\"" <<<"$PUBLISH_JOB"; then
    pass "Case 1: the OIDC token is requested with the publisher's origin as audience"
else
    fail "Case 1: the OIDC token is requested with the publisher's origin as audience" \
        "no \"\$ACTIONS_ID_TOKEN_REQUEST_URL&audience=${PUBLISH_ORIGIN:-<origin>}\" request in the publish job"
fi

assert_present '"$PUBLISH_URL")"' \
    "Case 1: the publish request goes to PUBLISH_URL"

assert_absent_from_code 'create-github-app-token' \
    "Case 1: no App token is minted in the workflow"

assert_absent_from_code 'secrets.' \
    "Case 1: the workflow reads no secret (the publisher holds the only key)"

assert_no_match '(4486636|BEGIN [A-Z ]*PRIVATE KEY)' \
    "Case 1: no hardcoded App id or private-key material in the workflow"

echo ""

# ---------------------------------------------------------------------------
# Case 2: least privilege
# ---------------------------------------------------------------------------
echo "Case 2: least privilege"

DEFAULT_PERMS="$(workflow_code | awk '/^permissions:/{found=1; next} found && NF {print; exit}')"
if [[ "$DEFAULT_PERMS" == *"contents: read"* ]]; then
    pass "Case 2: workflow-level default token is read-only (contents: read)"
else
    fail "Case 2: workflow-level default token is read-only (contents: read)" \
        "First entry under 'permissions:' was: '${DEFAULT_PERMS:-<none>}'"
fi

assert_no_match '^ +contents: write$' \
    "Case 2: no permissions: block grants contents: write"

IDTOKEN_ALL="$(workflow_code | grep -c '^ *id-token: write$' || true)"
IDTOKEN_PUBLISH="$(grep -c '^ *id-token: write$' <<<"$PUBLISH_JOB" || true)"
if [[ "$IDTOKEN_ALL" -eq 1 && "$IDTOKEN_PUBLISH" -eq 1 ]]; then
    pass "Case 2: id-token: write is granted exactly once, to the publish job"
else
    fail "Case 2: id-token: write is granted exactly once, to the publish job" \
        "id-token: write lines: $IDTOKEN_ALL in the file, $IDTOKEN_PUBLISH in the publish job"
fi

echo ""

# ---------------------------------------------------------------------------
# Case 3: no direct push path remains
# ---------------------------------------------------------------------------
echo "Case 3: no direct push path"

assert_absent_from_code 'git push' \
    "Case 3: nothing pushes to the repository (no fallback to a direct push)"

assert_absent_from_code 'secrets.GITHUB_TOKEN' \
    "Case 3: no executable reference to secrets.GITHUB_TOKEN"

assert_absent_from_code 'github.token' \
    "Case 3: no executable reference to github.token"

assert_absent_from_code 'github-actions[bot]' \
    "Case 3: the github-actions[bot] identity is gone entirely"

assert_absent_from_code 'git commit' \
    "Case 3: the workflow commits nothing itself (the publisher builds the commit)"

echo ""

# ---------------------------------------------------------------------------
# Case 4: the job that can mint the OIDC token is minimal
# ---------------------------------------------------------------------------
echo "Case 4: minimal minting job"

if [[ -z "$PUBLISH_JOB" ]]; then
    fail "Case 4: the publish job runs no action (no uses:)" "no 'publish' job found under jobs:"
elif grep -qE '^ *(- )?uses:' <<<"$PUBLISH_JOB"; then
    fail "Case 4: the publish job runs no action (no uses:)" \
        "every step of a job holding id-token: write can mint the publisher's token:" \
        "$(grep -nE '^ *(- )?uses:' <<<"$PUBLISH_JOB")"
else
    pass "Case 4: the publish job runs no action (no uses:)"
fi

if grep -qxF '    needs: bump-version' <<<"$PUBLISH_JOB"; then
    pass "Case 4: the publish job needs bump-version"
else
    fail "Case 4: the publish job needs bump-version" "no 'needs: bump-version' in the publish job"
fi

if grep -qxF "    if: needs.bump-version.outputs.decision == 'bump'" <<<"$PUBLISH_JOB"; then
    pass "Case 4: the publish job runs only on a bump verdict"
else
    fail "Case 4: the publish job runs only on a bump verdict" \
        "no \"if: needs.bump-version.outputs.decision == 'bump'\" in the publish job"
fi

for tool in 'uses: dtolnay/rust-toolchain@' 'uses: actions/setup-node@' 'uses: actions/checkout@'; do
    if grep -qF -- "$tool" <<<"$BUMP_JOB"; then
        pass "Case 4: $tool is in bump-version (the job with no id-token)"
    else
        fail "Case 4: $tool is in bump-version (the job with no id-token)" "not found in bump-version"
    fi
done

assert_present 'persist-credentials: false' \
    "Case 4: checkout does not persist a token (nothing in the run pushes)"

echo ""

# ---------------------------------------------------------------------------
# Case 5: failure behaviour and credential handling
# ---------------------------------------------------------------------------
echo "Case 5: failure behaviour and credentials"

RUN_BLOCKS="$(workflow_code | grep -c '^ *run: |$' || true)"
STRICT_RUNS="$(workflow_code | awk '/^ *run: \|$/{getline; if ($0 ~ /^ +set -euo pipefail$/) n++} END {print n + 0}')"
if [[ "$RUN_BLOCKS" -gt 0 && "$STRICT_RUNS" -eq "$RUN_BLOCKS" ]]; then
    pass "Case 5: every run: script starts with set -euo pipefail"
else
    fail "Case 5: every run: script starts with set -euo pipefail" \
        "$STRICT_RUNS of $RUN_BLOCKS run: blocks start with set -euo pipefail"
fi

CONFIG_STDIN="$(grep -c 'curl .*--config -' <<<"$PUBLISH_JOB" || true)"
if [[ "$CONFIG_STDIN" -eq 2 ]]; then
    pass "Case 5: both curl calls read their Authorization header from stdin (--config -)"
else
    fail "Case 5: both curl calls read their Authorization header from stdin (--config -)" \
        "found $CONFIG_STDIN curl ... --config - calls in the publish job, expected 2"
fi

assert_no_match "(-H|--header) +[\"']?Authorization" \
    "Case 5: no Authorization header is passed to curl in argv"

for bearer in ACTIONS_ID_TOKEN_REQUEST_TOKEN oidc; do
    BEARER_LINES="$(grep -F -- "\"\$${bearer}\"" <<<"$PUBLISH_JOB" || true)"
    if [[ "$BEARER_LINES" == *"printf 'header = \"Authorization: "* ]]; then
        pass "Case 5: \$${bearer} reaches curl's stdin through printf, not argv"
    else
        fail "Case 5: \$${bearer} reaches curl's stdin through printf, not argv" \
            "no printf 'header = \"Authorization: ...' line passing \"\$${bearer}\" in the publish job"
    fi
done

assert_present 'echo "::add-mask::$oidc"' \
    "Case 5: the minted OIDC token is masked in the log"

echo ""

# ---------------------------------------------------------------------------
# Case 6: the no-self-trigger invariant (#7743)
# ---------------------------------------------------------------------------
echo "Case 6: no-self-trigger invariant"

assert_present 'branches: [main]' \
    "Case 6: trigger is restricted to pushes on main"

TRIGGER_PATHS="$(workflow_code | awk '/^ +paths:/{found=1; next} found {if ($1 == "-") print $2; else exit}')"
if [[ "$TRIGGER_PATHS" == '"defaults/**"' ]]; then
    pass "Case 6: path filter is exactly defaults/** (one entry)"
else
    fail "Case 6: path filter is exactly defaults/** (one entry)" \
        "Parsed path list was: '${TRIGGER_PATHS:-<none>}'" \
        "Widening this filter lets the bump commit re-trigger the workflow." \
        "Publisher App commits DO trigger workflows, so this is the real loop guard."
fi

# Every `files=(...)` set: what the bump is diffed over (bump-version) and
# what the request is built from (publish).
FILE_SETS="$(workflow_code | awk '/files\+?=\(/{f=1} f {print} f && /\)$/{f=0}')"
SET_COUNT="$(grep -c 'files=(' <<<"$FILE_SETS" || true)"
if [[ "$SET_COUNT" -ne 2 ]]; then
    fail "Case 6: version-file sets stage no defaults/ path" \
        "expected 2 files=(...) sets (bump-version, publish), found $SET_COUNT"
elif grep -q 'defaults/' <<<"$FILE_SETS"; then
    fail "Case 6: version-file sets stage no defaults/ path" \
        "A defaults/ path would make the bump commit re-trigger the workflow:" "$FILE_SETS"
else
    pass "Case 6: version-file sets stage no defaults/ path (cannot re-trigger itself)"
fi

for managed in VERSION package.json mcp-loom/package.json mcp-loom/package-lock.json \
    Cargo.toml Cargo.lock .loom/install-metadata.json; do
    if [[ "$(grep -cF -- "$managed" <<<"$FILE_SETS")" -ge 2 ]]; then
        pass "Case 6: version-bearing file in the version-file sets: $managed"
    else
        fail "Case 6: version-bearing file in the version-file sets: $managed" \
            "scripts/version.sh rewrites it, so leaving it out desyncs main"
    fi
done

if grep -qF -- 'CLAUDE.md' <<<"$FILE_SETS"; then
    fail "Case 6: CLAUDE.md is NOT in the bump (#8147)" "$FILE_SETS"
else
    pass "Case 6: CLAUDE.md is NOT in the bump (#8147)"
fi

echo ""

# ---------------------------------------------------------------------------
# Case 7: concurrency and retry bound survive
# ---------------------------------------------------------------------------
echo "Case 7: concurrency and retry bound"

assert_present 'group: version-bump-on-merge' \
    "Case 7: runs are serialized in one concurrency group"

assert_present 'cancel-in-progress: false' \
    "Case 7: queued runs are never cancelled (each re-checks the gate on the current tip)"

assert_present 'attempts=5' \
    "Case 7: the bounded publish retry is still 5 attempts"

assert_present 'git checkout --quiet -f -B main origin/main' \
    "Case 7: each attempt re-syncs to the current tip of main"

echo ""

# ---------------------------------------------------------------------------
# Case 9: the release-cadence gate (#11174)
# ---------------------------------------------------------------------------
echo "Case 9: release-cadence gate"

assert_matches '^  schedule:$' \
    "Case 9: a schedule trigger exists (catch-up for a quiet day)"
assert_matches '^    - cron: "[0-9]+ \* \* \* \*"$' \
    "Case 9: the catch-up cron is hourly (worst case = interval + ~1 h)"
assert_matches '^  workflow_dispatch:$' \
    "Case 9: a workflow_dispatch trigger exists (operator hotfix path)"
FORCE_TYPE="$(workflow_code | awk '/^      force:$/{f=1; next} f && /^      [a-z]/{exit} f && /type:/{print $2; exit}')"
REASON_TYPE="$(workflow_code | awk '/^      reason:$/{f=1; next} f && /^      [a-z]/{exit} f && /type:/{print $2; exit}')"
if [[ "$FORCE_TYPE" == "boolean" && "$REASON_TYPE" == "string" ]]; then
    pass "Case 9: dispatch inputs are force (boolean) and reason (string)"
else
    fail "Case 9: dispatch inputs are force (boolean) and reason (string)" \
        "force type='${FORCE_TYPE:-<none>}' reason type='${REASON_TYPE:-<none>}'"
fi

assert_present 'fetch-depth: 0' \
    "Case 9: checkout fetches full history (the last bump may be weeks back)"
assert_present 'filter: blob:none' \
    "Case 9: the full-history checkout is a blob:none partial clone"
assert_no_match 'fetch-depth: [1-9]' \
    "Case 9: no shallow checkout (depth 1 makes the last-bump lookup wrong)"
assert_no_match 'git clone .*--depth' \
    "Case 9: the publish job's clone is not shallow either"

GATE_STEP="$(workflow_code | awk '/^        id: gate$/{f=1; next} f && /^      - /{exit} f {print}')"
if grep -qF 'bash scripts/version-bump-gate.sh' <<<"$GATE_STEP"; then
    pass "Case 9: the gate step runs scripts/version-bump-gate.sh"
else
    fail "Case 9: the gate step runs scripts/version-bump-gate.sh" \
        "the step with id: gate does not invoke the gate script"
fi
assert_order 'id: gate' 'uses: dtolnay/rust-toolchain@' \
    "Case 9: the gate runs before the Rust toolchain setup"
assert_order 'id: gate' 'uses: actions/setup-node@' \
    "Case 9: the gate runs before the Node setup"
for tool in 'uses: dtolnay/rust-toolchain@' 'uses: actions/setup-node@'; do
    n="$(line_of "$tool")"
    prev=""
    [[ -n "$n" ]] && prev="$(workflow_code | sed -n "$((n - 1))p")"
    if [[ "$prev" == "      - if: steps.gate.outputs.decision == 'bump'" ]]; then
        pass "Case 9: toolchain step is gated on the gate verdict: $tool"
    else
        fail "Case 9: toolchain step is gated on the gate verdict: $tool" \
            "line before it was: '${prev:-<none>}'"
    fi
done
BUMP_STEP_IF="$(workflow_code | awk '/name: Compute the version bump \(no commit/{f=1; next} f && /^        id:/{next} f {print; exit}')"
if [[ "$BUMP_STEP_IF" == "        if: steps.gate.outputs.decision == 'bump'" ]]; then
    pass "Case 9: the bump step is gated on the gate verdict"
else
    fail "Case 9: the bump step is gated on the gate verdict" \
        "line after the bump step name (and id) was: '${BUMP_STEP_IF:-<none>}'"
fi

LOOP_START="$(line_of 'for attempt in')"
POST_LINE="$(line_of '"$PUBLISH_URL")"')"
IN_LOOP=""
if [[ -n "$LOOP_START" && -n "$POST_LINE" ]]; then
    IN_LOOP="$(workflow_code | sed -n "${LOOP_START},${POST_LINE}p" | grep -F 'bash scripts/version-bump-gate.sh' || true)"
fi
if [[ -n "$IN_LOOP" ]]; then
    pass "Case 9: the gate is re-checked inside the publish loop, before each POST"
else
    fail "Case 9: the gate is re-checked inside the publish loop, before each POST" \
        "no gate call between 'for attempt in' (line ${LOOP_START:-?}) and the POST (line ${POST_LINE:-?})"
fi

REASON_HITS="$(workflow_code | grep -nF 'inputs.reason' || true)"
REASON_BAD="$(grep -vE '^[0-9]+: +[A-Z_]+: \$\{\{ inputs\.reason \}\}$' <<<"$REASON_HITS" || true)"
if [[ -z "$REASON_HITS" ]]; then
    fail "Case 9: inputs.reason reaches a shell only through env:" "inputs.reason is never passed"
elif [[ -n "$REASON_BAD" ]]; then
    fail "Case 9: inputs.reason reaches a shell only through env:" \
        "template injection: inputs.reason used outside an env: entry" "$REASON_BAD"
else
    pass "Case 9: inputs.reason reaches a shell only through env:"
fi

INTERVAL_HITS="$(workflow_code | grep -nE 'RELEASE_MIN_INTERVAL:' || true)"
INTERVAL_BAD="$(grep -vE '^[0-9]+: +RELEASE_MIN_INTERVAL: \$\{\{ vars\.RELEASE_MIN_INTERVAL \}\}$' <<<"$INTERVAL_HITS" || true)"
if [[ -n "$INTERVAL_HITS" && -z "$INTERVAL_BAD" ]]; then
    pass "Case 9: RELEASE_MIN_INTERVAL comes only from vars.RELEASE_MIN_INTERVAL"
else
    fail "Case 9: RELEASE_MIN_INTERVAL comes only from vars.RELEASE_MIN_INTERVAL" \
        "${INTERVAL_BAD:-RELEASE_MIN_INTERVAL is never passed}"
fi

assert_present 'message="chore: bump version to $BUMP_VERSION"' \
    "Case 9: the message's first line is exactly the publisher's subject"
assert_present "message+=\$'\\n\\n'\"Forced-by: \$ACTOR\"\$'\\n\\n'\"Reason: \$FORCE_REASON\"" \
    "Case 9: a forced bump's message records the actor and the reason"
assert_present '>>"$GITHUB_STEP_SUMMARY"' \
    "Case 9: the gate verdict (deferred / nothing / bump) is written to the job summary"
assert_present '--ref origin/main)" || rc=$?' \
    "Case 9: a failing gate's reason is surfaced (exit code captured, not set -e)"
assert_order '} >>"$GITHUB_STEP_SUMMARY"' 'exit "$rc"' \
    "Case 9: the gate step exits with the gate's code only after writing the summary"
assert_present '--ref origin/main)" || gate_rc=$?' \
    "Case 9: a failing in-loop gate re-check surfaces its reason (exit code captured)"

GH_CALLS="$(workflow_code | grep -cE '(^|[^[:alnum:]_.-])gh (api|pr|issue|release|run|workflow|repo|label|search)( |$)' || true)"
if [[ "$GH_CALLS" -eq 0 ]]; then
    pass "Case 9: no gh call (the gate is git-only; publishing is one curl)"
else
    fail "Case 9: no gh call (the gate is git-only; publishing is one curl)" \
        "found $GH_CALLS gh invocations; this file is off defaults/forge/call-bypass-baseline.toml"
fi

echo ""

# ---------------------------------------------------------------------------
# Case 10: the publisher's response contract
# ---------------------------------------------------------------------------
echo "Case 10: publisher response handling"

case_arm_present() {
    local arm="$1" msg="$2"
    if grep -qE "^ +${arm}\)$" <<<"$PUBLISH_JOB"; then
        pass "$msg"
    else
        fail "$msg" "no case arm matching: ${arm})"
    fi
}
case_arm_present '200:\*' "Case 10: 200 is handled as success"
case_arm_present '409:\* \| 503:upstream_ambiguous \| 000:\*' \
    "Case 10: 409, 503 upstream_ambiguous and an unknown outcome re-read main"
case_arm_present '\*' "Case 10: every other answer has a catch-all arm"

DEFAULT_ARM="$(awk '/^ +\*\)$/{f=1; next} f {print; exit}' <<<"$PUBLISH_JOB")"
if [[ "$DEFAULT_ARM" == *'die "the publisher refused the bump'* ]]; then
    pass "Case 10: an answer the contract does not name fails red"
else
    fail "Case 10: an answer the contract does not name fails red" \
        "line after the catch-all arm was: '${DEFAULT_ARM:-<none>}'"
fi

echo ""

# ---------------------------------------------------------------------------
# Case 8: mutation controls -- the suite must FAIL on each injected regression
# ---------------------------------------------------------------------------
# Parent mode only. Each control writes a mutated copy of the workflow, re-runs
# this same script against it (LOOM_VBW_WORKFLOW disables Case 8 in the child),
# and requires exit 1 plus a FAIL line for the specific assertion that guards
# the mutated property. A control that "fails for some other reason" does not
# count -- that is how a vacuous guard would hide.
if [[ "$RUN_MUTATION_CONTROLS" -eq 1 ]]; then
    echo "Case 8: mutation controls"

    MUTANT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/vbw-identity.XXXXXX")"
    trap 'rm -rf "$MUTANT_DIR"' EXIT
    MUTANT="$MUTANT_DIR/version-bump-on-merge.yml"
    MUTANT_LOG="$MUTANT_DIR/child.log"

    # --- 8a: the comment stripper itself (a broken stripper would make every
    #         control below pass for the wrong reason) ---
    assert_strip() {
        local input="$1" expected="$2" msg="$3" got
        got="$(printf '%s\n' "$input" | strip_comments)"
        if [[ "$got" == "$expected" ]]; then
            pass "Case 8: strip_comments $msg"
        else
            fail "Case 8: strip_comments $msg" "input   : [$input]" \
                "expected: [$expected]" "got     : [$got]"
        fi
    }
    assert_strip '      # id-token: write' '' \
        "blanks a full-line comment"
    assert_strip '          fetch-depth: 1 # id-token: write' '          fetch-depth: 1' \
        "cuts a trailing comment"
    assert_strip '          run: echo "a # b" # c' '          run: echo "a # b"' \
        "keeps a # inside double quotes, cuts the one after"
    assert_strip "          run: echo 'x#y' \"q\\\"#z\" a#b # gone" \
        "          run: echo 'x#y' \"q\\\"#z\" a#b" \
        "keeps # in single quotes, after an escaped quote, and glued to a word"

    # --- 8b: positive control -- the real workflow passes in the child ---
    if LOOM_VBW_WORKFLOW="$WORKFLOW" bash "$SELF" >"$MUTANT_LOG" 2>&1 \
        && grep -q 'Failed: 0$' "$MUTANT_LOG"; then
        pass "Case 8: positive control -- unmodified workflow passes when re-run as a child"
    else
        fail "Case 8: positive control -- unmodified workflow passes when re-run as a child" \
            "$(tail -5 "$MUTANT_LOG")"
    fi

    # Run the child against $MUTANT and require exit 1 + the named FAIL line.
    expect_child_fails() {
        local expected_fail="$1" msg="$2" rc
        LOOM_VBW_WORKFLOW="$MUTANT" bash "$SELF" >"$MUTANT_LOG" 2>&1
        rc=$?
        if [[ "$rc" -ne 1 ]]; then
            fail "$msg" "expected child exit 1, got $rc" "$(tail -3 "$MUTANT_LOG")"
        elif ! grep -qF -- "FAIL: $expected_fail" "$MUTANT_LOG"; then
            fail "$msg" "child failed, but not on the guarding assertion" \
                "expected: FAIL: $expected_fail" \
                "$(grep 'FAIL' "$MUTANT_LOG" | sed -n '1,5p')"
        else
            pass "$msg"
        fi
    }

    # Comment out the first EXECUTABLE line containing $1 (a `# ` prefix at
    # column 0, so the mutant is exactly what a hand edit would produce).
    comment_out() {
        awk -v needle="$1" '
            !done && index($0, needle) && $0 !~ /^[[:space:]]*#/ { print "# " $0; done=1; next }
            { print }
            END { if (!done) exit 2 }
        ' "$WORKFLOW" >"$MUTANT"
    }

    # --- 8c: every positive anchor, commented out, must be caught by the
    #         assertion that names it (PR #7891 review: the original hole) ---
    while IFS='|' read -r anchor expected_fail; do
        [[ -z "$anchor" ]] && continue
        if ! comment_out "$anchor"; then
            fail "Case 8: commented anchor rejected: $anchor" \
                "no executable line contains this anchor -- the control is stale"
            continue
        fi
        expect_child_fails "$expected_fail" "Case 8: commented anchor rejected: $anchor"
    done <<'ANCHORS'
PUBLISH_URL: https://|Case 1: the publish job targets an https /v1/publish_version_bump URL
&audience=https://|Case 1: the OIDC token is requested with the publisher's origin as audience
id-token: write|Case 2: id-token: write is granted exactly once, to the publish job
needs: bump-version|Case 4: the publish job needs bump-version
if: needs.bump-version.outputs.decision == 'bump'|Case 4: the publish job runs only on a bump verdict
persist-credentials: false|Case 4: checkout does not persist a token
set -euo pipefail|Case 5: every run: script starts with set -euo pipefail
echo "::add-mask::$oidc"|Case 5: the minted OIDC token is masked in the log
--config - -X POST|Case 5: both curl calls read their Authorization header from stdin
branches: [main]|Case 6: trigger is restricted to pushes on main
group: version-bump-on-merge|Case 7: runs are serialized in one concurrency group
cancel-in-progress: false|Case 7: queued runs are never cancelled
attempts=5|Case 7: the bounded publish retry is still 5 attempts
git checkout --quiet -f -B main origin/main|Case 7: each attempt re-syncs to the current tip of main
schedule:|Case 9: a schedule trigger exists
workflow_dispatch:|Case 9: a workflow_dispatch trigger exists
fetch-depth: 0|Case 9: checkout fetches full history
filter: blob:none|Case 9: the full-history checkout is a blob:none partial clone
id: gate|Case 9: the gate step runs scripts/version-bump-gate.sh
message+=|Case 9: a forced bump's message records the actor and the reason
503:upstream_ambiguous|Case 10: 409, 503 upstream_ambiguous and an unknown outcome re-read main
ANCHORS

    # --- 8d: structural regressions ---

    # The App-token step put back.
    awk '/^    steps:$/ && !done { print; print "      - uses: actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1 # v3.2.0"; print "        with:"; print "          app-id: ${{ secrets.LOOM_FLEET_DISPATCH_APP_ID }}"; done=1; next } { print }' \
        "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 1: no App token is minted in the workflow" \
        "Case 8: a reinstated App-token step is rejected"

    # A direct-push fallback after a refusal.
    awk '/die "the publisher refused the bump/ { print "                git push origin HEAD:main" } { print }' \
        "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 3: nothing pushes to the repository" \
        "Case 8: a git push fallback is rejected"

    # Checkout persisting the default token explicitly.
    sed 's|persist-credentials: false|token: ${{ secrets.GITHUB_TOKEN }}|' "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 3: no executable reference to secrets.GITHUB_TOKEN" \
        "Case 8: checkout with secrets.GITHUB_TOKEN is rejected"

    # id-token: write moved to the workflow level (every job could mint).
    awk '/^  contents: read$/ && !done { print; print "  id-token: write"; done=1; next } { print }' \
        "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 2: id-token: write is granted exactly once, to the publish job" \
        "Case 8: a workflow-level id-token: write is rejected"

    # The audience no longer matches the publisher's origin.
    sed 's|&audience=https://|\&audience=https://other.|' "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 1: the OIDC token is requested with the publisher's origin as audience" \
        "Case 8: an audience other than the publisher's origin is rejected"

    # An action added to the publish job.
    awk '/name: Publish version bump/ && !done { print "      - uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0"; done=1 } { print }' \
        "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 4: the publish job runs no action (no uses:)" \
        "Case 8: an action in the publish job is rejected"

    # The bearer passed to curl in argv.
    sed 's|curl -sS --max-time 90 --config - -X POST|curl -sS --max-time 90 -H "Authorization: Bearer $oidc" -X POST|' \
        "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 5: no Authorization header is passed to curl in argv" \
        "Case 8: an argv Authorization header is rejected"

    # Widen the trigger path filter (would let the bump commit re-trigger).
    awk '/- "defaults\/\*\*"/ { print; sub(/"defaults\/\*\*"/, "\"docs/**\""); print; next } { print }' \
        "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 6: path filter is exactly defaults/** (one entry)" \
        "Case 8: widened paths: filter is rejected"

    # Grant contents: write at workflow level.
    sed 's|^  contents: read$|  contents: write|' "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 2: no permissions: block grants contents: write" \
        "Case 8: workflow-level contents: write grant is rejected"

    # A defaults/ path in the version-file set (self-trigger loop).
    sed 's|files=(VERSION package.json|files=(VERSION defaults/VERSION package.json|' "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 6: version-file sets stage no defaults/ path" \
        "Case 8: a defaults/ path in the version-file set is rejected"

    # An unnamed answer logged and retried instead of failing red.
    awk '/die "the publisher refused the bump: \$outcome"/ { sub(/die "the publisher refused the bump: \$outcome"/, "echo \"unexpected: $outcome\"") } { print }' \
        "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 10: an answer the contract does not name fails red" \
        "Case 8: a catch-all arm that does not fail red is rejected"

    # --- 8e: release-cadence gate regressions (#11174) ---

    # inputs.reason interpolated straight into a run: script (template injection).
    awk '/^ +set -euo pipefail$/ && !done { print; print "          echo \"${{ inputs.reason }}\""; done=1; next } { print }' \
        "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 9: inputs.reason reaches a shell only through env:" \
        "Case 8: \${{ inputs.reason }} inside run: is rejected"

    # The gate step stops running the gate script.
    awk '/bash scripts\/version-bump-gate.sh/ && !done { done=1; next } { print }' "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 9: the gate step runs scripts/version-bump-gate.sh" \
        "Case 8: gate step without the gate call is rejected"

    # The in-loop re-check dropped (a racing bump would be bumped over).
    awk '/gate="\$\(bash scripts\/version-bump-gate.sh/ { next } { print }' "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 9: the gate is re-checked inside the publish loop, before each POST" \
        "Case 8: publish loop without the gate re-check is rejected"

    # The in-loop re-check moved after the POST.
    awk '
        /gate="\$\(bash scripts\/version-bump-gate.sh/ { held = $0; next }
        { print }
        /"\$PUBLISH_URL"\)"/ && held != "" { print held; held = "" }
    ' "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 9: the gate is re-checked inside the publish loop, before each POST" \
        "Case 8: gate re-check moved after the POST is rejected"

    # Rust toolchain set up unconditionally (a deferred run pays for it).
    awk '
        /^ +- if: steps\.gate\.outputs\.decision == .bump.$/ && !done { pending = $0; next }
        pending != "" {
            if ($0 ~ /uses: dtolnay\/rust-toolchain@/) { sub(/^        uses:/, "      - uses:"); done = 1 }
            else { print pending }
            pending = ""
        }
        { print }
    ' "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 9: toolchain step is gated on the gate verdict: uses: dtolnay/rust-toolchain@" \
        "Case 8: ungated toolchain step is rejected"

    # The bump step loses its gate condition.
    awk '/^        if: steps\.gate\.outputs\.decision == .bump.$/ { next } { print }' \
        "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 9: the bump step is gated on the gate verdict" \
        "Case 8: ungated bump step is rejected"

    # A hardcoded interval instead of the repo variable.
    awk '/RELEASE_MIN_INTERVAL: \$\{\{ vars/ && !done { sub(/\$\{\{ vars\.RELEASE_MIN_INTERVAL \}\}/, "\"0\""); done=1 } { print }' \
        "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 9: RELEASE_MIN_INTERVAL comes only from vars.RELEASE_MIN_INTERVAL" \
        "Case 8: hardcoded RELEASE_MIN_INTERVAL is rejected"

    # The gate's exit code no longer captured (set -e swallows the reason).
    sed 's/--ref origin\/main)" || rc=\$?/--ref origin\/main)"/' "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 9: a failing gate's reason is surfaced (exit code captured, not set -e)" \
        "Case 8: bare out=\"\$(gate)\" under set -e is rejected"

    # Back to a shallow checkout.
    sed 's|fetch-depth: 0|fetch-depth: 1|' "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 9: no shallow checkout" \
        "Case 8: fetch-depth: 1 is rejected"

    # A forge call added (this file is off the bypass ledger).
    awk '/^ +set -euo pipefail$/ && !done { print; print "          gh release list --limit 1"; done=1; next } { print }' \
        "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 9: no gh call" \
        "Case 8: an added gh call is rejected"

    echo ""
fi

# --- Summary ---
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
