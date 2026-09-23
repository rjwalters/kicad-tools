#!/usr/bin/env bash
# test-version-bump-workflow-identity.sh - Regression guard for the pushing
# identity of .github/workflows/version-bump-on-merge.yml (issues #7743, #7829).
#
# Background: that workflow is the single post-merge owner of VERSION and the
# other version-bearing files (#7743). Its final step pushes straight to `main`,
# and this repo's `main` ruleset (8809610) requires a pull request -- so the
# push only lands when the pushing identity is a bypass actor. The default
# `github-actions[bot]` GITHUB_TOKEN can NEVER be one here: adding the GitHub
# Actions integration (app id 15368) to `bypass_actors` is rejected outright on
# a user-owned repo with
#
#   422 Validation Failed: Actor GitHub Actions integration must be part of
#   the ruleset source or owner organization
#
# The only viable identity is the repo's own `loom-fleet-dispatch` App (id
# 4486636), which IS listed in the ruleset's `bypass_actors` (#7829). The
# workflow therefore mints a short-lived, repository-scoped App installation
# token and uses that -- and only that -- for checkout, push, and commit
# identity.
#
# Why a test and not just review: every property below is invisible at a
# glance and silently reverts the workflow to a permanently-403ing state if
# dropped (a plausible "simplification" is to delete the token step and let
# checkout use its default token -- which reads fine and fails only at merge
# time, in a workflow nobody watches). The four ad-hoc shell controls run
# while implementing #7871 were never committed, so nothing pinned these
# invariants until now.
#
# Comment-blindness (PR #7891 review): every assertion about what the workflow
# DOES runs against a comment-stripped view of the file (`strip_comments`):
# full-line `#` comments are blanked and trailing ` # ...` comments outside
# quotes are cut, with line numbers preserved so ordering diagnostics still
# point at the real file. Without that, commenting out checkout's
# `token: ${{ steps.app-token.outputs.token }}` -- which silently hands the
# later `git push` back to the default GITHUB_TOKEN -- still passed every
# assertion, because `grep -F` over the raw file is satisfied by a comment.
# This is a comment stripper, not a YAML parser: it does not understand block
# scalars or multi-line strings, and it is only as good as the mutation
# controls in Case 8 that pin it.
#
# What this asserts:
#   1. Token minting: the App-token action is pinned by 40-hex commit SHA (not
#      a mutable tag), carries a human-readable version comment, and reads the
#      App id / PEM from the two named repository secrets -- never a literal.
#   2. Least privilege: the workflow-level default token is read-only, the
#      installation token is scoped to this owner + this repository with only
#      `contents: write`, and no `permissions:` block grants `contents: write`.
#   3. No GITHUB_TOKEN push path remains (#7829 acceptance criterion 2): no
#      executable line references `secrets.GITHUB_TOKEN` / `github.token`, and
#      the `github-actions[bot]` identity appears nowhere -- there is exactly
#      one pushing identity and no fallback to a second one.
#   4. Wiring + ordering: the token is minted BEFORE checkout, checkout
#      consumes it, the push step gets it via `GH_TOKEN`, and the commit
#      identity is derived from the App slug the mint step returned.
#   5. Failure behavior: the bot-identity lookup runs under `set -euo pipefail`
#      and BEFORE the first git mutation, so absent/failed credentials abort
#      the job rather than committing with a wrong identity.
#   6. The no-self-trigger invariant (#7743): the trigger is exactly
#      `push` -> `main` -> `defaults/**`, and the bump commit stages only
#      version-bearing files -- never anything under `defaults/` -- so the
#      workflow's own push can never re-trigger it. An App installation token
#      DOES trigger workflows (unlike GITHUB_TOKEN), which makes this the load-
#      bearing loop guard rather than a belt-and-braces one.
#   7. Serialized-not-cancelled concurrency and the bounded retry survive.
#   8. Mutation controls (durable, not ad-hoc): the suite re-runs itself
#      against mutated copies of the workflow and requires each one to FAIL on
#      the specific assertion that guards it -- every positive anchor commented
#      out, checkout's token line deleted / commented / hidden in a trailing
#      comment, a GITHUB_TOKEN revert, a widened path filter, a `contents:
#      write` grant, a staged `defaults/` path, and the identity lookup moved
#      after the first mutation. The unmodified workflow is the positive
#      control. This is what stops the comment-blindness hole from reopening.
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

if [[ ! -f "$WORKFLOW" ]]; then
    if [[ "$RUN_MUTATION_CONTROLS" -eq 1 ]]; then
        echo "SKIP: source-tree-only test, $WORKFLOW not found (not shipped into an installed repo)" >&2
        exit 0
    fi
    echo "ERROR: LOOM_VBW_WORKFLOW=$WORKFLOW does not exist" >&2
    exit 2
fi

echo "Testing version-bump-on-merge.yml pushing identity ($WORKFLOW)"
echo ""

# ---------------------------------------------------------------------------
# Case 1: the App installation token is minted from SHA-pinned action + secrets
# ---------------------------------------------------------------------------
echo "Case 1: App-token minting step"

assert_matches \
    '^ +uses: actions/create-github-app-token@[0-9a-f]{40}$' \
    "Case 1: create-github-app-token is pinned by 40-hex commit SHA (not a mutable tag)"

assert_matches_raw \
    '^ +uses: actions/create-github-app-token@[0-9a-f]{40} +# +v[0-9]' \
    "Case 1: the SHA pin carries a '# vX.Y.Z' comment so the version is readable"

assert_present 'id: app-token' \
    "Case 1: the mint step is addressable as steps.app-token"

assert_present 'app-id: ${{ secrets.LOOM_FLEET_DISPATCH_APP_ID }}' \
    "Case 1: app id comes from the LOOM_FLEET_DISPATCH_APP_ID secret"

assert_present 'private-key: ${{ secrets.LOOM_FLEET_DISPATCH_APP_PRIVATE_KEY }}' \
    "Case 1: PEM comes from the LOOM_FLEET_DISPATCH_APP_PRIVATE_KEY secret"

# The App id is public, but hardcoding it (or, far worse, any PEM material)
# defeats the secret indirection the operator provisions.
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

# `permission-contents: write` (an input to the mint step) is a different key
# and must not be confused with a `permissions:` grant -- hence the anchor.
assert_no_match '^ +contents: write$' \
    "Case 2: no permissions: block grants contents: write to the default token"

assert_present 'owner: ${{ github.repository_owner }}' \
    "Case 2: installation token is scoped to this repository owner"

assert_present 'repositories: ${{ github.event.repository.name }}' \
    "Case 2: installation token is scoped to this repository only"

assert_present 'permission-contents: write' \
    "Case 2: installation token requests exactly contents: write"

echo ""

# ---------------------------------------------------------------------------
# Case 3: no GITHUB_TOKEN-based push path remains (#7829 acceptance #2)
# ---------------------------------------------------------------------------
echo "Case 3: no GITHUB_TOKEN push path"

assert_absent_from_code 'secrets.GITHUB_TOKEN' \
    "Case 3: no executable reference to secrets.GITHUB_TOKEN"

assert_absent_from_code 'github.token' \
    "Case 3: no executable reference to github.token"

assert_absent_from_code 'github-actions[bot]' \
    "Case 3: the github-actions[bot] identity is gone entirely"

echo ""

# ---------------------------------------------------------------------------
# Case 4: token wiring and step ordering
# ---------------------------------------------------------------------------
echo "Case 4: token wiring and ordering"

assert_present 'token: ${{ steps.app-token.outputs.token }}' \
    "Case 4: checkout persists the App token in the remote URL"

assert_present 'GH_TOKEN: ${{ steps.app-token.outputs.token }}' \
    "Case 4: the bump/push step gets the App token via GH_TOKEN"

assert_present 'APP_SLUG: ${{ steps.app-token.outputs.app-slug }}' \
    "Case 4: the commit identity is derived from the minted App's slug"

assert_present 'git config user.name "$bot_login"' \
    "Case 4: git identity is the App bot login, not a hardcoded name"

assert_order 'uses: actions/create-github-app-token@' 'uses: actions/checkout@' \
    "Case 4: the token is minted before checkout"

assert_order 'token: ${{ steps.app-token.outputs.token }}' 'git push origin HEAD:main' \
    "Case 4: checkout consumes the token before the push runs"

echo ""

# ---------------------------------------------------------------------------
# Case 5: failure behavior -- abort before mutating anything
# ---------------------------------------------------------------------------
echo "Case 5: failure behavior"

assert_present 'set -euo pipefail' \
    "Case 5: the bump script runs under set -euo pipefail (a failed lookup aborts)"

assert_order 'bot_id="$(gh api' 'for attempt in' \
    "Case 5: bot identity is resolved before the first git mutation"

assert_order 'bot_id="$(gh api' './scripts/version.sh bump' \
    "Case 5: a failed identity lookup cannot reach the version bump"

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
        "App-token pushes DO trigger workflows, so this is the real loop guard."
fi

# Everything the bump commit stages, from the first `git add` through the
# `git commit` that seals it. None of it may live under defaults/.
STAGED_BLOCK="$(workflow_code | awk '/git add /{found=1} found {print} /git commit -m/{exit}')"
if [[ -z "$STAGED_BLOCK" ]]; then
    fail "Case 6: bump commit stages no defaults/ path" \
        "Could not locate the git add ... git commit block"
elif grep -q 'defaults/' <<<"$STAGED_BLOCK"; then
    fail "Case 6: bump commit stages no defaults/ path" \
        "A staged path under defaults/ would make the workflow re-trigger itself:" \
        "$STAGED_BLOCK"
else
    pass "Case 6: bump commit stages no defaults/ path (cannot re-trigger itself)"
fi

for managed in VERSION package.json mcp-loom/package.json Cargo.toml \
    .loom/install-metadata.json; do
    if grep -qF -- "$managed" <<<"$STAGED_BLOCK"; then
        pass "Case 6: version-bearing file still staged: $managed"
    else
        fail "Case 6: version-bearing file still staged: $managed" \
            "scripts/version.sh rewrites it, so an unstaged one desyncs main"
    fi
done

# The mirror image (#8147): CLAUDE.md must NOT be staged. version.sh stopped
# rewriting it (it is injected into every agent session's prompt prefix, so a
# per-bump token in it invalidates every warm prefix in the fleet), and a
# `git add CLAUDE.md` left behind here would silently sweep an unrelated dirty
# CLAUDE.md into the automated bump commit.
if grep -qF -- 'CLAUDE.md' <<<"$STAGED_BLOCK"; then
    fail "Case 6: CLAUDE.md is NOT staged by the bump commit (#8147)" \
        "version.sh no longer rewrites CLAUDE.md; staging it can only pick up" \
        "unrelated working-tree changes:" \
        "$STAGED_BLOCK"
else
    pass "Case 6: CLAUDE.md is NOT staged by the bump commit (#8147)"
fi

echo ""

# ---------------------------------------------------------------------------
# Case 7: concurrency and retry bound survive
# ---------------------------------------------------------------------------
echo "Case 7: concurrency and retry bound"

assert_present 'group: version-bump-on-merge' \
    "Case 7: runs are serialized in one concurrency group"

assert_present 'cancel-in-progress: false' \
    "Case 7: queued runs are never cancelled (each merge needs its own bump)"

assert_present 'attempts=5' \
    "Case 7: the bounded push retry is still 5 attempts"

assert_present 'git checkout -B main origin/main' \
    "Case 7: each retry re-syncs to the current tip before re-bumping"

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
    assert_strip '      # token: ${{ steps.app-token.outputs.token }}' '' \
        "blanks a full-line comment"
    assert_strip '          fetch-depth: 1 # token: ${{ x }}' '          fetch-depth: 1' \
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
uses: actions/create-github-app-token@|Case 1: create-github-app-token is pinned by 40-hex commit SHA
id: app-token|Case 1: the mint step is addressable as steps.app-token
app-id: ${{ secrets.LOOM_FLEET_DISPATCH_APP_ID }}|Case 1: app id comes from the LOOM_FLEET_DISPATCH_APP_ID secret
private-key: ${{ secrets.LOOM_FLEET_DISPATCH_APP_PRIVATE_KEY }}|Case 1: PEM comes from the LOOM_FLEET_DISPATCH_APP_PRIVATE_KEY secret
owner: ${{ github.repository_owner }}|Case 2: installation token is scoped to this repository owner
repositories: ${{ github.event.repository.name }}|Case 2: installation token is scoped to this repository only
permission-contents: write|Case 2: installation token requests exactly contents: write
token: ${{ steps.app-token.outputs.token }}|Case 4: checkout persists the App token in the remote URL
GH_TOKEN: ${{ steps.app-token.outputs.token }}|Case 4: the bump/push step gets the App token via GH_TOKEN
APP_SLUG: ${{ steps.app-token.outputs.app-slug }}|Case 4: the commit identity is derived from the minted App's slug
git config user.name "$bot_login"|Case 4: git identity is the App bot login, not a hardcoded name
uses: actions/checkout@|Case 4: the token is minted before checkout
set -euo pipefail|Case 5: the bump script runs under set -euo pipefail
bot_id="$(gh api|Case 5: bot identity is resolved before the first git mutation
branches: [main]|Case 6: trigger is restricted to pushes on main
group: version-bump-on-merge|Case 7: runs are serialized in one concurrency group
cancel-in-progress: false|Case 7: queued runs are never cancelled
attempts=5|Case 7: the bounded push retry is still 5 attempts
git checkout -B main origin/main|Case 7: each retry re-syncs to the current tip before re-bumping
ANCHORS

    # --- 8d: structural regressions (the ad-hoc controls from PR #7891,
    #         now permanent) ---

    # checkout's token line deleted outright -> checkout falls back to the
    # default GITHUB_TOKEN and persists THAT for the later push. (`grep -F` is
    # case-sensitive, so the `GH_TOKEN:` env line is untouched: only the
    # checkout token goes missing -- precisely the regression the review
    # reproduced.)
    grep -vF 'token: ${{ steps.app-token.outputs.token }}' "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 4: checkout persists the App token in the remote URL" \
        "Case 8: checkout token line DELETED is rejected"

    # Same, but the literal survives inside a trailing comment on a neighbouring
    # executable line -- proves trailing-comment stripping, not just full-line.
    awk '
        /^ +token: \$\{\{ steps.app-token.outputs.token \}\}$/ { next }
        /^ +fetch-depth: 1$/ { print $0 " # token: ${{ steps.app-token.outputs.token }}"; next }
        { print }
    ' "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 4: checkout persists the App token in the remote URL" \
        "Case 8: checkout token literal hidden in a TRAILING comment is rejected"

    # Revert checkout to the default token explicitly.
    sed 's|token: ${{ steps.app-token.outputs.token }}|token: ${{ secrets.GITHUB_TOKEN }}|' \
        "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 3: no executable reference to secrets.GITHUB_TOKEN" \
        "Case 8: checkout reverted to secrets.GITHUB_TOKEN is rejected"

    # Widen the trigger path filter (would let the bump commit re-trigger).
    awk '/- "defaults\/\*\*"/ { print; sub(/"defaults\/\*\*"/, "\"docs/**\""); print; next } { print }' \
        "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 6: path filter is exactly defaults/** (one entry)" \
        "Case 8: widened paths: filter is rejected"

    # Grant contents: write to the default token at workflow level.
    sed 's|^  contents: read$|  contents: write|' "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 2: no permissions: block grants contents: write to the default token" \
        "Case 8: workflow-level contents: write grant is rejected"

    # Stage a defaults/ path in the bump commit (self-trigger loop).
    sed 's|git add package.json|git add defaults/VERSION package.json|' "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 6: bump commit stages no defaults/ path" \
        "Case 8: a staged defaults/ path is rejected"

    # Move the identity lookup after the first git mutation.
    awk '
        /bot_id="\$\(gh api/ { held = $0; next }
        { print }
        /for attempt in/ && held != "" { print held; held = "" }
    ' "$WORKFLOW" >"$MUTANT"
    expect_child_fails "Case 5: bot identity is resolved before the first git mutation" \
        "Case 8: identity lookup moved after the retry loop starts is rejected"

    echo ""
fi

# --- Summary ---
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
