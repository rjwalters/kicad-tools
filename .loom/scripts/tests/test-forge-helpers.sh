#!/usr/bin/env bash
# test-forge-helpers.sh - Unit tests for forge-helpers.sh dispatch logic
#
# Tests forge detection, host extraction, and verifies that forge dispatch
# functions route to the correct backend based on FORGE_TYPE.
#
# Usage:
#   ./.loom/scripts/tests/test-forge-helpers.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1"
    local actual="$2"
    local msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

# --- Test _extract_host ---
echo "Testing _extract_host..."

# Need to source the library
source "$HELPERS_DIR/lib/forge-helpers.sh"
# #9548: the write wrappers vet their repo through the write scope first. The
# suite runs from a checkout registered as owner/repo (origin, .loom/, push
# reported to the permission probe), so the real decision admits it.
WS_FIXTURE_DIR="$(mktemp -d)"
# shellcheck source=lib/write-scope-fixture.sh
source "$SCRIPT_DIR/lib/write-scope-fixture.sh"
write_scope_register "$WS_FIXTURE_DIR" owner/repo
cd "$WS_FIXTURE_DIR"

# Reset state for testing
FORGE_TYPE=""

result=$(_extract_host "git@github.com:owner/repo.git")
assert_eq "github.com" "$result" "SSH GitHub URL"

result=$(_extract_host "https://github.com/owner/repo.git")
assert_eq "github.com" "$result" "HTTPS GitHub URL"

result=$(_extract_host "git@gitea.example.com:owner/repo.git")
assert_eq "gitea.example.com" "$result" "SSH Gitea URL"

result=$(_extract_host "https://gitea.example.com/owner/repo")
assert_eq "gitea.example.com" "$result" "HTTPS Gitea URL (no .git)"

result=$(_extract_host "not-a-url")
assert_eq "" "$result" "Invalid URL returns empty"

# --- Test forge_detect with env var ---
echo ""
echo "Testing forge_detect with LOOM_FORGE_TYPE env var..."

FORGE_TYPE=""
LOOM_FORGE_TYPE="github" forge_detect
assert_eq "github" "$FORGE_TYPE" "LOOM_FORGE_TYPE=github"

FORGE_TYPE=""
LOOM_FORGE_TYPE="gitea" forge_detect 2>/dev/null || true
# Note: this may fail if no Gitea config, but FORGE_TYPE should still be set
assert_eq "gitea" "$FORGE_TYPE" "LOOM_FORGE_TYPE=gitea"

# --- Test forge_split_nwo ---
echo ""
echo "Testing forge_split_nwo..."

forge_split_nwo "myowner/myrepo"
assert_eq "myowner" "$FORGE_OWNER" "Split NWO owner"
assert_eq "myrepo" "$FORGE_REPO" "Split NWO repo"

forge_split_nwo "org/complex-repo-name"
assert_eq "org" "$FORGE_OWNER" "Split NWO org owner"
assert_eq "complex-repo-name" "$FORGE_REPO" "Split NWO complex repo"

# --- Test forge detection defaults to github ---
echo ""
echo "Testing forge_detect defaults..."

FORGE_TYPE=""
# Unset LOOM_FORGE_TYPE to test auto-detection
unset LOOM_FORGE_TYPE 2>/dev/null || true
export LOOM_FORGE_TYPE=""
forge_detect
# In this repo (github.com remote), should detect as github
assert_eq "github" "$FORGE_TYPE" "Auto-detect defaults to github for github.com remote"

# --- Test forge_get_repo_nwo for github ---
echo ""
echo "Testing forge_get_repo_nwo..."

FORGE_TYPE="github"
result=$(forge_get_repo_nwo "gh" 2>/dev/null || echo "")
# Should return non-empty for this repo
if [[ -n "$result" ]]; then
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_get_repo_nwo returns non-empty for GitHub ($result)"
else
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_get_repo_nwo returned empty"
fi

# --- Test forge_pr_close_targets (Gitea fallback regex path) ---
# These tests exercise the regex fallback that is used for Gitea (and that
# serves as the safety net behavior we want to guarantee even without the
# GitHub GraphQL path). We test the regex directly to avoid needing a live
# forge or stubbing `gh pr view`.
echo ""
echo "Testing forge_pr_close_targets regex (Gitea fallback semantics)..."

# Helper: run the same regex used inside forge_pr_close_targets's Gitea branch.
# Note: `|| true` neutralizes grep's exit code 1 (no match) under `set -e`.
_close_targets_regex() {
    local body="$1"
    { echo "$body" \
        | grep -Eoi '\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b[[:space:]]+#[0-9]+' \
        | grep -Eo '[0-9]+' \
        | sort -un \
        | tr '\n' ' ' \
        | sed 's/ $//'; } || true
}

result=$(_close_targets_regex "Closes #42")
assert_eq "42" "$result" "Closes #N matches"

result=$(_close_targets_regex "Fixes #42")
assert_eq "42" "$result" "Fixes #N matches"

result=$(_close_targets_regex "Resolves #42")
assert_eq "42" "$result" "Resolves #N matches"

result=$(_close_targets_regex "closes #42")
assert_eq "42" "$result" "lowercase closes #N matches (case-insensitive)"

result=$(_close_targets_regex "Closed #42")
assert_eq "42" "$result" "tense variant 'Closed #N' matches"

result=$(_close_targets_regex "Updates #42")
assert_eq "" "$result" "Updates #N is correctly ignored (the bug from #3267)"

result=$(_close_targets_regex "See #42")
assert_eq "" "$result" "See #N is correctly ignored"

result=$(_close_targets_regex "References #42")
assert_eq "" "$result" "References #N is correctly ignored"

result=$(_close_targets_regex "Discloses #42")
assert_eq "" "$result" "substring trap 'Discloses #N' is correctly ignored"

result=$(_close_targets_regex "")
assert_eq "" "$result" "empty body returns nothing"

result=$(_close_targets_regex "Closes #1, Fixes #2, Resolves #3")
assert_eq "1 2 3" "$result" "multiple closing keywords match all targets"

result=$(_close_targets_regex "Closes #5. Updates #6.")
assert_eq "5" "$result" "mixed Closes/Updates closes only Closes target"

result=$(_close_targets_regex "Closes #7 and Fixes #7")
assert_eq "7" "$result" "duplicate references are de-duplicated"

# --- Test forge_pr_close_targets dispatches to GitHub path ---
echo ""
echo "Testing forge_pr_close_targets GitHub dispatch (using stub gh)..."

# Create a stub `gh` that captures the closingIssuesReferences invocation
# and returns canned output. Place it on PATH ahead of the real gh.
STUB_DIR=$(mktemp -d)
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Stub gh that only handles the close-targets query.
# Usage: gh pr view <N> --json closingIssuesReferences --jq '.closingIssuesReferences[].number'
if [[ "$1" == "pr" && "$2" == "view" && "$*" == *"closingIssuesReferences"* ]]; then
  printf '123\n456\n'
  exit 0
fi
exit 1
STUB
chmod +x "$STUB_DIR/gh"

FORGE_TYPE="github"
result=$(forge_pr_close_targets "999" "$STUB_DIR/gh" | tr '\n' ' ' | sed 's/ $//')
assert_eq "123 456" "$result" "GitHub path delegates to gh pr view --json closingIssuesReferences"

rm -rf "$STUB_DIR"

# --- Test forge_get_pr_nocache does not pass --no-cache to plain gh (issue #3547) ---
# Regression: `--no-cache` is a gh-cached WRAPPER flag, not a real `gh` flag.
# When gh-cached is absent and the plain `gh` fallback is used, passing
# --no-cache made `gh api` fail on the unknown flag; the error was swallowed by
# 2>/dev/null and callers substituted '{}', silently breaking merge verification
# and race-condition rechecks. forge_get_pr_nocache must therefore NOT pass
# --no-cache when the command basename is plain `gh` (plain `gh api` is already
# uncached), while still passing it for the gh-cached wrapper.
echo ""
echo "Testing forge_get_pr_nocache --no-cache handling (issue #3547)..."

NC_STUB_DIR=$(mktemp -d)

# Stub named exactly `gh`: emits valid PR JSON for `api ...`, but exits non-zero
# (as real gh does) if the unknown `--no-cache` flag is present. This proves the
# helper reaches the api call cleanly only when --no-cache is omitted.
cat > "$NC_STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
for a in "$@"; do
  if [[ "$a" == "--no-cache" ]]; then
    echo "unknown flag: --no-cache" >&2
    exit 1
  fi
done
if [[ "$1" == "api" ]]; then
  printf '{"merged":true,"state":"closed"}\n'
  exit 0
fi
exit 1
STUB
chmod +x "$NC_STUB_DIR/gh"

# Stub named `gh-cached`: REQUIRES --no-cache to be present (proves the wrapper
# still receives the cache-bypass flag). Emits valid JSON when it is.
cat > "$NC_STUB_DIR/gh-cached" <<'STUB'
#!/usr/bin/env bash
has_no_cache=0
for a in "$@"; do
  [[ "$a" == "--no-cache" ]] && has_no_cache=1
done
if [[ "$has_no_cache" -ne 1 ]]; then
  echo "expected --no-cache" >&2
  exit 1
fi
printf '{"merged":true,"state":"closed"}\n'
exit 0
STUB
chmod +x "$NC_STUB_DIR/gh-cached"

FORGE_TYPE="github"

# Plain-gh path: helper must omit --no-cache and return real JSON.
nc_result=$(forge_get_pr_nocache "owner/repo" "123" "$NC_STUB_DIR/gh" 2>/dev/null | jq -r '.merged' 2>/dev/null || echo "")
assert_eq "true" "$nc_result" "forge_get_pr_nocache with plain gh omits --no-cache and returns real JSON"

# gh-cached path: helper must still pass --no-cache to the wrapper.
nc_cached_result=$(forge_get_pr_nocache "owner/repo" "123" "$NC_STUB_DIR/gh-cached" 2>/dev/null | jq -r '.merged' 2>/dev/null || echo "")
assert_eq "true" "$nc_cached_result" "forge_get_pr_nocache with gh-cached wrapper still passes --no-cache"

rm -rf "$NC_STUB_DIR"

# --- Test CI-status helpers stay UNCACHED (issue #4667) ---
# The sweep/judge/champion skills now route their hot discovery reads through
# gh-cached, but CI status is explicitly carved out: it is the read a verdict
# and a merge are gated on, so a cached green could predate the push that broke
# the build. forge_get_check_runs / forge_get_commit_status must therefore keep
# invoking plain `gh` from PATH — never a cache wrapper, and never with the
# wrapper-only `--no-cache` flag (the #3547 failure shape). This locks the
# carve-out in at the library level so a future "cache everything" pass has to
# delete a failing test rather than silently weaken a merge gate.
echo ""
echo "Testing CI-status helpers are uncached (issue #4667)..."

CI_STUB_DIR=$(mktemp -d)
CI_ARGS_FILE="$CI_STUB_DIR/argv.txt"

cat > "$CI_STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_CI_ARGS_FILE"
for a in "$@"; do
  if [[ "$a" == "--no-cache" ]]; then
    echo "unknown flag: --no-cache" >&2
    exit 1
  fi
done
# `gh api ... --jq` filters server-side; the helpers ask for a reshaped object,
# so just emit the already-shaped result the helper would have produced.
case "$*" in
  *check-runs*) printf '{"total_count":1,"check_runs":[{"name":"build","status":"completed","conclusion":"success","html_url":"u"}]}\n' ;;
  *status*)     printf '{"state":"success","statuses":[]}\n' ;;
  *)            exit 1 ;;
esac
exit 0
STUB
chmod +x "$CI_STUB_DIR/gh"

# A `gh-cached` that must never be reached by these helpers.
cat > "$CI_STUB_DIR/gh-cached" <<'STUB'
#!/usr/bin/env bash
echo "gh-cached must NOT be used for CI-status reads" >&2
exit 1
STUB
chmod +x "$CI_STUB_DIR/gh-cached"

FORGE_TYPE="github"
: > "$CI_ARGS_FILE"

cr_state=$(GH_CI_ARGS_FILE="$CI_ARGS_FILE" PATH="$CI_STUB_DIR:$PATH" \
  forge_get_check_runs "owner/repo" "deadbeef" 2>/dev/null | jq -r '.check_runs[0].conclusion' 2>/dev/null || echo "")
assert_eq "success" "$cr_state" "forge_get_check_runs reaches plain gh (no --no-cache, no wrapper)"

cs_state=$(GH_CI_ARGS_FILE="$CI_ARGS_FILE" PATH="$CI_STUB_DIR:$PATH" \
  forge_get_commit_status "owner/repo" "deadbeef" 2>/dev/null | jq -r '.state' 2>/dev/null || echo "")
assert_eq "success" "$cs_state" "forge_get_commit_status reaches plain gh (no --no-cache, no wrapper)"

nocache_hits=$(grep -c -- '--no-cache' "$CI_ARGS_FILE" || true)
assert_eq "0" "$nocache_hits" "CI-status helpers never pass the wrapper-only --no-cache flag"

rm -rf "$CI_STUB_DIR"

# --- Test forge_get_pr_reviews pagination + fail-closed contract (#7647) ---
# The pre-#7647 helper made ONE unpaginated request, emitted `.[].body` only,
# and swallowed every error as empty output. All three are now failures by
# contract: reviews paginate, records are complete (id/state/commit/timestamp),
# and a read error exits non-zero instead of looking like "no reviews".
echo ""
echo "Testing forge_get_pr_reviews pagination and fail-closed behavior (#7647)..."

REV_STUB_DIR=$(mktemp -d)
cat > "$REV_STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Emulates `gh api <endpoint> --paginate --jq FILTER`: applies the caller's own
# jq filter to each page in turn, exactly as gh streams them.
if [[ -n "${REV_STUB_FAIL:-}" ]]; then
  echo "stub gh: simulated API failure" >&2
  exit 1
fi
shift  # drop "api"
shift  # drop the endpoint
jq_filter=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --jq) jq_filter="$2"; shift 2 ;;
    *) shift ;;
  esac
done
for page in "$REV_STUB_DIR_ENV"/page-*.json; do
  [[ -f "$page" ]] || continue
  jq -r "$jq_filter" "$page" || exit 1
done
exit 0
STUB
chmod +x "$REV_STUB_DIR/gh"

cat > "$REV_STUB_DIR/page-1.json" <<'JSON'
[{"id":11,"state":"COMMENTED","commit_id":"aaa","submitted_at":"2026-09-14T20:00:00Z","user":{"login":"carol"},"body":"note"}]
JSON
cat > "$REV_STUB_DIR/page-2.json" <<'JSON'
[{"id":12,"state":"CHANGES_REQUESTED","commit_id":"bbb","submitted_at":"2026-09-14T21:10:12Z","user":{"login":"alice"},"body":"needs coverage"}]
JSON

FORGE_TYPE="github"
rev_out=$(REV_STUB_DIR_ENV="$REV_STUB_DIR" forge_get_pr_reviews "owner/repo" "5369" "$REV_STUB_DIR/gh" 2>/dev/null)
assert_eq "2" "$(printf '%s\n' "$rev_out" | grep -c '^{')" "forge_get_pr_reviews returns reviews from BOTH pages"
assert_eq "CHANGES_REQUESTED" "$(printf '%s\n' "$rev_out" | jq -r 'select(.id == 12) | .state')" "review state is retained (not just the body)"
assert_eq "bbb" "$(printf '%s\n' "$rev_out" | jq -r 'select(.id == 12) | .commit_id')" "review commit/head association is retained"
assert_eq "2026-09-14T21:10:12Z" "$(printf '%s\n' "$rev_out" | jq -r 'select(.id == 12) | .submitted_at')" "review submission timestamp is retained"

set +e
REV_STUB_FAIL=1 REV_STUB_DIR_ENV="$REV_STUB_DIR" \
  forge_get_pr_reviews "owner/repo" "5369" "$REV_STUB_DIR/gh" > /dev/null 2>&1
rev_fail_rc=$?
set -e
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$rev_fail_rc" -ne 0 ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: a failed review read exits non-zero (fail closed, never silent empty output)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: a failed review read exited 0 — the #7647 'errors look like no reviews' bug is back"
fi

rm -rf "$REV_STUB_DIR"

# --- Test gitea_api auth-mode selection (issue #3297) ---
# Use a `curl` shim on PATH that records its argv and returns a fake 200.
echo ""
echo "Testing gitea_api auth mode selection (Basic vs token)..."

SHIM_DIR=$(mktemp -d)
CURL_ARGS_FILE=$(mktemp)
export CURL_ARGS_FILE
cat > "$SHIM_DIR/curl" <<'SHIM'
#!/usr/bin/env bash
# Record argv (one per line) and emit a fake 200 OK response.
: > "$CURL_ARGS_FILE"
for a in "$@"; do
  printf '%s\n' "$a" >> "$CURL_ARGS_FILE"
done
# gitea_api expects body lines followed by a final-line HTTP status code.
printf '{"ok":true}\n200\n'
SHIM
chmod +x "$SHIM_DIR/curl"

# --- Subtest 1: token mode sends "Authorization: token ..." and NOT -u ---
_GITEA_BASE_URL="https://gitea.example.com"
_GITEA_TOKEN="tok-abc"
_GITEA_USERNAME=""
PATH="$SHIM_DIR:$PATH" gitea_api GET "user" >/dev/null 2>&1 || true

if grep -q "^Authorization: token tok-abc$" "$CURL_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: token mode sends 'Authorization: token …' header"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: token mode missing 'Authorization: token …' header"
    echo "    curl argv:"; sed 's/^/      /' "$CURL_ARGS_FILE"
fi

if grep -qx -- "-u" "$CURL_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: token mode unexpectedly used '-u'"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: token mode does NOT use '-u'"
fi

# --- Subtest 2: Basic mode sends -u user:pass and NOT Authorization: token ---
_GITEA_USERNAME="alice"
_GITEA_BASE_URL="https://gitea.example.com"
PATH="$SHIM_DIR:$PATH" gitea_api GET "user" >/dev/null 2>&1 || true

if grep -qx -- "-u" "$CURL_ARGS_FILE" && grep -qx -- "alice:tok-abc" "$CURL_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Basic mode sends '-u user:pass'"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Basic mode missing '-u user:pass'"
    echo "    curl argv:"; sed 's/^/      /' "$CURL_ARGS_FILE"
fi

if grep -q "^Authorization: token" "$CURL_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Basic mode unexpectedly sent 'Authorization: token …'"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Basic mode does NOT send 'Authorization: token …'"
fi

# --- Subtest 3: HTTPS guard rejects http:// in Basic mode ---
_GITEA_USERNAME="alice"
_GITEA_TOKEN="tok-abc"
_GITEA_BASE_URL="http://insecure.example.com"
unset LOOM_ALLOW_INSECURE_BASIC_AUTH 2>/dev/null || true
# Capture rc and stderr separately. Use a subshell with set +e so the
# function's nonzero return code propagates without aborting the script.
guard_output=$(
  set +e
  PATH="$SHIM_DIR:$PATH" gitea_api GET "user" 2>&1 >/dev/null
  echo "RC=$?"
)
guard_rc=$(echo "$guard_output" | tail -1 | sed 's/^RC=//')
if [[ "$guard_rc" -ne 0 ]] && [[ "$guard_output" == *"Basic Auth requires HTTPS"* ]]; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: HTTPS guard rejects http:// in Basic mode"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: HTTPS guard did not fire (rc=$guard_rc, output=$guard_output)"
fi

# --- Subtest 4: HTTPS guard override via LOOM_ALLOW_INSECURE_BASIC_AUTH=1 ---
LOOM_ALLOW_INSECURE_BASIC_AUTH=1 PATH="$SHIM_DIR:$PATH" \
  gitea_api GET "user" >/dev/null 2>&1
override_rc=$?
if [[ "$override_rc" -eq 0 ]]; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: LOOM_ALLOW_INSECURE_BASIC_AUTH=1 permits http://"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: LOOM_ALLOW_INSECURE_BASIC_AUTH=1 did not unblock http:// (rc=$override_rc)"
fi

# --- Subtest 5: Username with ':' is rejected ---
_GITEA_USERNAME="alice:bob"
_GITEA_TOKEN="tok-abc"
_GITEA_BASE_URL="https://gitea.example.com"
colon_output=$(
  set +e
  PATH="$SHIM_DIR:$PATH" gitea_api GET "user" 2>&1 >/dev/null
  echo "RC=$?"
)
colon_rc=$(echo "$colon_output" | tail -1 | sed 's/^RC=//')
if [[ "$colon_rc" -ne 0 ]] && [[ "$colon_output" == *"may not contain ':'"* ]]; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: username with ':' rejected"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: username with ':' was NOT rejected (rc=$colon_rc, output=$colon_output)"
fi

rm -rf "$SHIM_DIR" "$CURL_ARGS_FILE"

# --- Test forge_detect_merge_method / forge_merge_pr respect a non-squash
# repo (#7754) -- the original #1258 bug was a hardcoded merge_method=squash
# / Do:"squash" breaking any repo that has squash-merge disabled.
echo ""
echo "Testing forge_detect_merge_method / forge_merge_pr honor the target repo's allowed strategies (#7754)..."

FORGE_TYPE="github"

# --- GitHub: repo allows only merge-commit (squash and rebase disabled) ---
GH_MM_STUB_DIR=$(mktemp -d)
cat > "$GH_MM_STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
if [[ "$1" == "api" && "$2" == repos/* && "$*" != *"-X PUT"* ]]; then
  printf '{"allow_squash_merge":false,"allow_merge_commit":true,"allow_rebase_merge":false}\n'
  exit 0
fi
exit 1
STUB
chmod +x "$GH_MM_STUB_DIR/gh"

gh_mm_result=$(PATH="$GH_MM_STUB_DIR:$PATH" forge_detect_merge_method "owner/repo" "$GH_MM_STUB_DIR/gh")
assert_eq "merge" "$gh_mm_result" "forge_detect_merge_method (GitHub) selects 'merge' when only allow_merge_commit is true"

# --- GitHub: repo allows only rebase ---
cat > "$GH_MM_STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
if [[ "$1" == "api" && "$2" == repos/* ]]; then
  printf '{"allow_squash_merge":false,"allow_merge_commit":false,"allow_rebase_merge":true}\n'
  exit 0
fi
exit 1
STUB
chmod +x "$GH_MM_STUB_DIR/gh"

gh_mm_result=$(PATH="$GH_MM_STUB_DIR:$PATH" forge_detect_merge_method "owner/repo" "$GH_MM_STUB_DIR/gh")
assert_eq "rebase" "$gh_mm_result" "forge_detect_merge_method (GitHub) selects 'rebase' when only allow_rebase_merge is true"

# --- GitHub: probe failure fails open to "merge" (#9105 inverted the
# #7754 fail-open: a repo that truly disallows merge commits must fail the
# merge loudly at the forge, not silently squash history) ---
cat > "$GH_MM_STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
chmod +x "$GH_MM_STUB_DIR/gh"

gh_mm_result=$(PATH="$GH_MM_STUB_DIR:$PATH" forge_detect_merge_method "owner/repo" "$GH_MM_STUB_DIR/gh")
assert_eq "merge" "$gh_mm_result" "forge_detect_merge_method (GitHub) fails open to 'merge' on a probe failure (#9105)"

rm -rf "$GH_MM_STUB_DIR"

# --- GitHub: forge_merge_pr sends the CALLER-SUPPLIED method, not a
# hardcoded squash -- this is the actual fix for the original #1258
# "Squash merges are not allowed on this repository" failure.
GH_MERGE_STUB_DIR=$(mktemp -d)
GH_MERGE_ARGS_FILE=$(mktemp)
cat > "$GH_MERGE_STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_MERGE_ARGS_FILE"
echo '{"merged":true}'
exit 0
STUB
chmod +x "$GH_MERGE_STUB_DIR/gh"

: > "$GH_MERGE_ARGS_FILE"
GH_MERGE_ARGS_FILE="$GH_MERGE_ARGS_FILE" PATH="$GH_MERGE_STUB_DIR:$PATH" \
  forge_merge_pr "owner/repo" "42" "" "rebase" >/dev/null
if grep -q -- "-f merge_method=rebase" "$GH_MERGE_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_merge_pr (GitHub) sends merge_method=rebase when explicitly requested"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_merge_pr (GitHub) did not send merge_method=rebase (argv: $(cat "$GH_MERGE_ARGS_FILE"))"
fi

: > "$GH_MERGE_ARGS_FILE"
GH_MERGE_ARGS_FILE="$GH_MERGE_ARGS_FILE" PATH="$GH_MERGE_STUB_DIR:$PATH" \
  forge_merge_pr "owner/repo" "42" >/dev/null
if grep -q -- "-f merge_method=merge" "$GH_MERGE_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_merge_pr (GitHub) defaults to merge when no method is supplied (#9105: merge commits are the default)"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_merge_pr (GitHub) default-method behavior regressed (argv: $(cat "$GH_MERGE_ARGS_FILE"))"
fi

rm -rf "$GH_MERGE_STUB_DIR"; rm -f "$GH_MERGE_ARGS_FILE"

# --- Gitea: repo allows only rebase (squash disabled) ---
FORGE_TYPE="gitea"
GITEA_MM_SHIM_DIR=$(mktemp -d)
cat > "$GITEA_MM_SHIM_DIR/curl" <<'SHIM'
#!/usr/bin/env bash
printf '{"allow_squash_merge":false,"allow_merge_commits":false,"allow_rebase_merge":true}\n200\n'
SHIM
chmod +x "$GITEA_MM_SHIM_DIR/curl"

_GITEA_BASE_URL="https://gitea.example.com"
_GITEA_TOKEN="tok-abc"
_GITEA_USERNAME=""
gitea_mm_result=$(PATH="$GITEA_MM_SHIM_DIR:$PATH" forge_detect_merge_method "owner/repo")
assert_eq "rebase" "$gitea_mm_result" "forge_detect_merge_method (Gitea) selects 'rebase' when only allow_rebase_merge is true"

# --- Gitea: forge_merge_pr sends the CALLER-SUPPLIED "Do" value ---
GITEA_MERGE_CURL_ARGS=$(mktemp)
export GITEA_MERGE_CURL_ARGS
cat > "$GITEA_MM_SHIM_DIR/curl" <<'SHIM'
#!/usr/bin/env bash
: > "$GITEA_MERGE_CURL_ARGS"
for a in "$@"; do
  printf '%s\n' "$a" >> "$GITEA_MERGE_CURL_ARGS"
done
printf '{"merged":true}\n200\n'
SHIM
chmod +x "$GITEA_MM_SHIM_DIR/curl"

PATH="$GITEA_MM_SHIM_DIR:$PATH" forge_merge_pr "owner/repo" "42" "" "rebase" >/dev/null
if grep -q '"Do":"rebase"' "$GITEA_MERGE_CURL_ARGS"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_merge_pr (Gitea) sends Do:rebase when explicitly requested"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_merge_pr (Gitea) did not send Do:rebase (curl -d args: $(cat "$GITEA_MERGE_CURL_ARGS"))"
fi

rm -rf "$GITEA_MM_SHIM_DIR"; rm -f "$GITEA_MERGE_CURL_ARGS"

# --- #9109: forge values are URL-encoded before landing in an API path ---
#
# A branch name is forge-derived. git's ref-format forbids `..` so there is no
# path traversal here, but it PERMITS URL metacharacters — and
# `branches/$branch` interpolated raw turns `?`, `#` or `%` into query/fragment/
# escape syntax, silently addressing a different endpoint than the caller asked
# for. The separators must survive, though: both endpoints route on literal
# slashes, so `feature/issue-N` has to come out unchanged.
echo ""
echo "Testing url_encode_path_segment + forge_delete_branch path encoding (#9109)..."

assert_eq "feature/issue-9109" "$(url_encode_path_segment 'feature/issue-9109')" \
    "url_encode_path_segment leaves an ordinary branch name (and its slashes) alone"
assert_eq "a%20b%3Fc%23d%25e%26f" "$(url_encode_path_segment 'a b?c#d%e&f')" \
    "url_encode_path_segment percent-encodes URL metacharacters"
assert_eq "a%22b%5Cc%3Cd%3Ee" "$(url_encode_path_segment 'a"b\c<d>e')" \
    "url_encode_path_segment percent-encodes quote/backslash/angle brackets"
assert_eq "wip/h%C3%A9llo" "$(url_encode_path_segment 'wip/héllo')" \
    "url_encode_path_segment encodes multi-byte input as UTF-8 bytes"
assert_eq "" "$(url_encode_path_segment '')" \
    "url_encode_path_segment on empty input yields empty"

# Gitea arm: shim `curl` and read back the FULL request URL gitea_api built —
# a strictly stronger assertion than capturing its path argument alone.
# (Stubbing gitea_api itself is not an option: a redefinition here turns the
# library's own earlier calls into SC2218 forward references.)
GITEA_DEL_SHIM_DIR=$(mktemp -d)
GITEA_DEL_URL_FILE=$(mktemp)
export GITEA_DEL_URL_FILE
cat > "$GITEA_DEL_SHIM_DIR/curl" <<'SHIM'
#!/usr/bin/env bash
# gitea_api always passes the URL last.
for a in "$@"; do last="$a"; done
printf '%s\n' "$last" > "$GITEA_DEL_URL_FILE"
printf '{}\n200\n'
SHIM
chmod +x "$GITEA_DEL_SHIM_DIR/curl"
FORGE_TYPE="gitea"
_GITEA_BASE_URL="https://gitea.example.com"
_GITEA_TOKEN="tok-abc"
_GITEA_USERNAME=""
PATH="$GITEA_DEL_SHIM_DIR:$PATH" forge_delete_branch "owner/repo" 'feature/weird?x#y z%00' >/dev/null
assert_eq "https://gitea.example.com/api/v1/repos/owner/repo/branches/feature/weird%3Fx%23y%20z%2500" \
    "$(cat "$GITEA_DEL_URL_FILE")" \
    "forge_delete_branch (Gitea) percent-encodes metacharacters in the branch path"
PATH="$GITEA_DEL_SHIM_DIR:$PATH" forge_delete_branch "owner/repo" 'feature/issue-9109' >/dev/null
assert_eq "https://gitea.example.com/api/v1/repos/owner/repo/branches/feature/issue-9109" \
    "$(cat "$GITEA_DEL_URL_FILE")" \
    "forge_delete_branch (Gitea) leaves an ordinary branch path byte-identical"
rm -rf "$GITEA_DEL_SHIM_DIR"; rm -f "$GITEA_DEL_URL_FILE"; unset GITEA_DEL_URL_FILE

# GitHub arm: shim `gh` on PATH and read back the path it was invoked with.
GH_DEL_SHIM_DIR=$(mktemp -d)
GH_DEL_ARGS=$(mktemp)
export GH_DEL_ARGS
cat > "$GH_DEL_SHIM_DIR/gh" <<'SHIM'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$GH_DEL_ARGS"
SHIM
chmod +x "$GH_DEL_SHIM_DIR/gh"
FORGE_TYPE="github"
PATH="$GH_DEL_SHIM_DIR:$PATH" forge_delete_branch "owner/repo" 'feature/weird?x#y z'
assert_eq "repos/owner/repo/git/refs/heads/feature/weird%3Fx%23y%20z" "$(sed -n 2p "$GH_DEL_ARGS")" \
    "forge_delete_branch (GitHub) percent-encodes metacharacters in the ref path"
PATH="$GH_DEL_SHIM_DIR:$PATH" forge_delete_branch "owner/repo" 'feature/issue-9109'
assert_eq "repos/owner/repo/git/refs/heads/feature/issue-9109" "$(sed -n 2p "$GH_DEL_ARGS")" \
    "forge_delete_branch (GitHub) leaves an ordinary ref path byte-identical"
rm -rf "$GH_DEL_SHIM_DIR"; rm -f "$GH_DEL_ARGS"; unset GH_DEL_ARGS

# --- Test forge_get_workflow_runs Gitea pagination + fail-closed (#9879) ---
# curl shim that serves scripted /actions/tasks pages: parses the page=N arg,
# returns full 2-task pages forever unless a page-specific script is set.
echo ""
echo "Testing forge_get_workflow_runs Gitea pagination + fail-closed (#9879)..."

WF_SHIM_DIR=$(mktemp -d)
WF_PAGES_FILE=$(mktemp)
export WF_PAGES_FILE
export WF_SHIM_DIR
# Page bodies are PRE-COMPUTED files (jq runs here, at setup, with its exit
# codes checked) and the curl shim only cats them: a jq spawn inside the
# shim under hermetic-CI load (14 concurrent suites) once flaked an empty
# body -> paginate failed closed -> a coin-flip suite (judge, round 3 of
# #9880). cat is load-immune.
SHA_HEX="96c2b8246403c9c91d37c2c7d6eebf7558f790f4"
jq -nc '{workflow_runs: ([range(0; 49) | {head_sha: "other", display_title: "filler"}] + [{head_sha: $sha, display_title: "one"}])}' --arg sha "$SHA_HEX" > "$WF_SHIM_DIR/page-ok1.json" || exit 1
jq -nc '{workflow_runs: [{head_sha: $sha, display_title: "two"}]}' --arg sha "$SHA_HEX" > "$WF_SHIM_DIR/page-ok2.json" || exit 1
jq -nc '{workflow_runs: [range(0; 50) | {head_sha: "x", status: "queued"}]}' > "$WF_SHIM_DIR/page-cap.json" || exit 1
cat > "$WF_SHIM_DIR/curl" <<'SHIM'
#!/usr/bin/env bash
# Find the page= argument (gitea_api appends &limit=50&page=N).
page=1
for a in "$@"; do
  case "$a" in
    *page=*) page="${a##*page=}" ;;
  esac
done
# Page script: semicolon-separated per-page directives, e.g. "ok1;ok2" or
# "cap". Pages beyond the directives repeat the LAST one (a full page), so
# the cap script trips the 50-page cap rather than ending on a short page.
I=1
directive=""
for d in $(tr ';' ' ' < "$WF_PAGES_FILE"); do
  directive="$d"
  if [ "$I" -eq "$page" ]; then
    case "$d" in
      fail)
        printf 'server error\n500\n'
        exit 0
        ;;
      ok1|ok2|cap)
        cat "$WF_SHIM_DIR/page-$d.json"
        printf '200\n'
        ;;
    esac
    exit 0
  fi
  I=$((I + 1))
done
case "$directive" in
  ok1|ok2|cap)
    cat "$WF_SHIM_DIR/page-$directive.json"
    printf '200\n'
    ;;
  *)
    printf '{}\n200\n'
    ;;
esac
SHIM
chmod +x "$WF_SHIM_DIR/curl"

wf_run() {  # wf_run <commit>  -> "exit:<rc> out:<stdout>"
  local out rc
  FORGE_TYPE=""
  LOOM_FORGE_TYPE="gitea"
  forge_detect >/dev/null 2>&1 || true
  out=$(_GITEA_BASE_URL="https://gitea.example.com" _GITEA_TOKEN="tok" _GITEA_USERNAME="" \
      PATH="$WF_SHIM_DIR:$PATH" forge_get_workflow_runs "owner/repo" "$1" 2>/dev/null)
  rc=$?
  printf 'exit:%s out:%s' "$rc" "$out"
}

# Subtest 1: multi-page read assembles all matching runs across pages.
printf 'ok1;ok2;ok2\n' > "$WF_PAGES_FILE"
RESULT=$(wf_run "96c2b8246403c9c91d37c2c7d6eebf7558f790f4" 2>/dev/null) || RESULT="exit:$?"
# RESULT is "exit:<rc> out:<json>" — strip the prefix before parsing the JSON.
# Membership tests use the no-pipe idiom (grep -q RE <<<"$var"): this file
# runs pipefail, and the ratchet (scripts/check-pipefail-early-exit.sh,
# #7790) freezes new `printf | grep -q` occurrences — #7771's false answer.
OUT_JSON="${RESULT#*out:}"
RUNS="$(jq '[.workflow_runs[]] | length' <<<"$OUT_JSON" 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'exit:0' <<<"$RESULT" \
   && [ "$RUNS" = "2" ] \
   && grep -q '"one"' <<<"$OUT_JSON" && grep -q '"two"' <<<"$OUT_JSON" \
   && ! grep -q '"skip"' <<<"$OUT_JSON"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: workflow-runs Gitea reads across pages (2 runs, sha-filtered)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: workflow-runs multi-page read broken: $RESULT"
fi

# Subtest 2: a failing page is nonzero + no stdout JSON (never empty-success).
printf 'ok1;fail\n' > "$WF_PAGES_FILE"
rc=0
( FORGE_TYPE=""
  LOOM_FORGE_TYPE="gitea"
  forge_detect >/dev/null 2>&1 || true
  _GITEA_BASE_URL="https://gitea.example.com" _GITEA_TOKEN="tok" _GITEA_USERNAME="" \
  PATH="$WF_SHIM_DIR:$PATH" forge_get_workflow_runs "owner/repo" "96c2b8246403c9c91d37c2c7d6eebf7558f790f4" ) >/tmp/wf-fail.out 2>/dev/null || rc=$?
TESTS_RUN=$((TESTS_RUN + 1))
if [ "$rc" -ne 0 ] && [ ! -s /tmp/wf-fail.out ]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: workflow-runs failing page -> nonzero exit, no stdout JSON (fail closed)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: workflow-runs failing page did not fail closed (rc=$rc)"
fi

# Subtest 3: page-cap trip is nonzero, never a truncated-but-successful read.
printf 'cap\n' > "$WF_PAGES_FILE"
rc=0
( FORGE_TYPE=""
  LOOM_FORGE_TYPE="gitea"
  forge_detect >/dev/null 2>&1 || true
  echo "cap debug: FORGE_TYPE=[$FORGE_TYPE]" >&2
  _GITEA_BASE_URL="https://gitea.example.com" _GITEA_TOKEN="tok" _GITEA_USERNAME="" \
  PATH="$WF_SHIM_DIR:$PATH" forge_get_workflow_runs "owner/repo" "x" ) >/tmp/wf-cap.out 2>/tmp/wf-cap.err || rc=$?
TESTS_RUN=$((TESTS_RUN + 1))
if [ "$rc" -ne 0 ]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: workflow-runs page-cap trip -> nonzero (refuses truncated list)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: workflow-runs page-cap trip exited 0"
fi
rm -rf "$WF_SHIM_DIR" "$WF_PAGES_FILE" /tmp/wf-fail.out

# --- Test forge_pr_close_targets Gitea: fences + blockquotes excluded (#9879) ---
echo ""
echo "Testing forge_pr_close_targets Gitea fence/blockquote handling (#9879)..."

CT_DIR=$(mktemp -d)
CT_SHIM_DIR="$CT_DIR/shim"
mkdir -p "$CT_SHIM_DIR"
git -C "$CT_DIR" init -q
git -C "$CT_DIR" remote add origin https://gitea.example.com/owner/repo.git
git -C "$CT_DIR" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init

CT_BODY_FILE=$(mktemp)
export CT_BODY_FILE
cat > "$CT_SHIM_DIR/curl" <<'SHIM'
#!/usr/bin/env bash
# Serve the scripted PR body (a JSON object with a "body" field).
cat "$CT_BODY_FILE"
printf '200\n'
SHIM
chmod +x "$CT_SHIM_DIR/curl"

ct_run() {  # ct_run <body> -> close-target numbers, one per line
  printf '{"body": %s}\n' "$(jq -Rn --arg b "$1" '$b')" > "$CT_BODY_FILE"
  ( cd "$CT_DIR" && FORGE_TYPE="" LOOM_FORGE_TYPE="gitea"
      forge_detect >/dev/null 2>&1 || true
      _GITEA_BASE_URL="https://gitea.example.com" _GITEA_TOKEN="tok" _GITEA_USERNAME="" \
      PATH="$CT_SHIM_DIR:$PATH" forge_pr_close_targets 7 )
}

RESULT=$(ct_run 'Closes #42
Fixes #43

```bash
Closes #99
```

> Closes #88
	Resolves #44')

TESTS_RUN=$((TESTS_RUN + 1))
if [ "$RESULT" = "42
43
44" ]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: close-targets excludes fenced + blockquoted closers, keeps real ones"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: close-targets got: $(printf '%s' "$RESULT" | tr '\n' ' ')"
fi

# The GitHub branch must be byte-identical to before: still the plain
# closingIssuesReferences query (no fence handling added there).
GH_CT_SHIM="$CT_SHIM_DIR/gh"
cat > "$GH_CT_SHIM" <<'SHIM'
#!/usr/bin/env bash
printf '31\n' # the only thing the GitHub branch outputs
SHIM
chmod +x "$GH_CT_SHIM"
RESULT=$( ( cd "$CT_DIR" && FORGE_TYPE="" LOOM_FORGE_TYPE="github"
      forge_detect >/dev/null 2>&1 || true; PATH="$CT_SHIM_DIR:$PATH" forge_pr_close_targets 7 ) )
TESTS_RUN=$((TESTS_RUN + 1))
if [ "$RESULT" = "31" ]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: close-targets GitHub branch unchanged (closingIssuesReferences passthrough)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: close-targets GitHub branch changed: $RESULT"
fi
rm -rf "$CT_DIR" "$CT_BODY_FILE"

# --- forge_fetch_error_cause / forge_get_pr_nocache stderr (#9192) ---
echo ""
echo "Testing forge_fetch_error_cause (#9192)..."
fec() { forge_fetch_error_cause "$@" | cut -d: -f1; }
assert_eq "HTTP 401 auth failure -- the credential is invalid or expired; re-authenticate" \
  "$(fec 'gh: Bad credentials (HTTP 401)' '{"message":"Bad credentials","status":"401"}')" "401 -> auth failure"
assert_eq "HTTP 403 rate limit -- the credential DID authenticate (do not re-authenticate); wait for the reset" \
  "$(fec 'gh: API rate limit exceeded for user ID 4242. (HTTP 403)' '{"message":"API rate limit exceeded for user ID 4242.","status":"403"}')" \
  "rate-limit 403 -> rate limit, NOT auth"
assert_eq "HTTP 403 rate limit -- the credential DID authenticate (do not re-authenticate); wait for the reset" \
  "$(fec '' '{"message":"You have exceeded a secondary rate limit","status":"403"}')" "secondary rate limit from the body alone (status read from .status)"
assert_eq "HTTP 403 forbidden (not a rate limit) -- the credential lacks access to this repository" \
  "$(fec 'gh: Resource not accessible by integration (HTTP 403)' '')" "non-rate-limit 403 -> forbidden"
assert_eq "HTTP 404 not found -- the PR does not exist in this repository, or the credential cannot see the repository" \
  "$(fec 'gh: Not Found (HTTP 404)' '{"message":"Not Found","status":"404"}')" "404 -> not found"
assert_eq "transient forge/network failure (HTTP 502) -- retry" "$(fec 'gh: Server Error (HTTP 502)' '')" "5xx -> transient"
assert_eq "HTTP status unknown" "$(fec '' '')" "no signal at all -> says so, never empty"
_fec_full="$(forge_fetch_error_cause 'gh: API rate limit exceeded for user ID 4242. (HTTP 403)' '{"message":"API rate limit exceeded for user ID 4242."}')"
assert_eq "yes" "$([[ "$_fec_full" == *": API rate limit exceeded for user ID 4242." ]] && echo yes || echo no)" "the forge's own message (naming the user ID) is carried verbatim"
_fec_tok="$(forge_fetch_error_cause '{"message":"bad token ghp_SECRET123abc and github_pat_XYZ_9"}' '')"
assert_eq "no" "$([[ "$_fec_tok" == *SECRET123abc* || "$_fec_tok" == *XYZ_9* ]] && echo yes || echo no)" "token-shaped strings are redacted"
_FEC_SHIM="$(mktemp -d)"; printf '#!/usr/bin/env bash\necho "{\\"message\\":\\"Not Found\\"}"; echo "gh: Not Found (HTTP 404)" >&2; exit 1\n' > "$_FEC_SHIM/gh"; chmod +x "$_FEC_SHIM/gh"
_fec_err="$( (FORGE_TYPE=github; forge_get_pr_nocache o/r 1 "$_FEC_SHIM/gh" 2>&1 >/dev/null) || true)"
assert_eq "gh: Not Found (HTTP 404)" "$_fec_err" "forge_get_pr_nocache passes gh's stderr through instead of discarding it"
_fec_err="$( (FORGE_TYPE=github; forge_get_pr o/r 1 "$_FEC_SHIM/gh" 2>&1 >/dev/null) || true)"
assert_eq "gh: Not Found (HTTP 404)" "$_fec_err" "forge_get_pr passes gh's stderr through instead of discarding it"
rm -rf "$_FEC_SHIM"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
