#!/usr/bin/env bash
# test-check-duplicate.sh - Unit tests for check-duplicate.sh's --issue
# cross-reference probe (issue #4162) and its keyword-similarity scoring
# (issue #4409).
#
# check-duplicate.sh's existing keyword-similarity check answers "has this
# been reported before?" -- it is structurally blind to an OPEN issue that
# *critiques* the target's spec (a cross-reference in its body) without being
# textually similar. This mirrors a real incident (rjwalters/repo #32/#33):
# #33 named #32 three times in its body, arguing #32's acceptance criteria
# were wrong, but was never surfaced because it wasn't a *duplicate* -- it
# was a *related, spec-changing* piece of open work.
#
# `check-duplicate.sh --issue N` closes this gap by querying GitHub's
# timeline API for open issues/PRs that cross-reference N, emitting a
# RELATED_OPEN_WORK block distinct from DUPLICATE_FOUND. This is a
# black-box test: check-duplicate.sh is a full CLI script (main "$@" at
# EOF, not sourced functions), so we stub `gh` and `loom-daemon` on PATH and
# invoke the real script as a subprocess, asserting on stdout/exit code.
#
# Issue #4409 fixed two compounding bugs in calculate_similarity(): (1) `read
# -ra arr <<< "$multiline_str"` only ever consumes ONE line, so a multi-keyword
# (i.e. any realistically-worded) title/body collapsed to a single-element
# array -- comparisons became a coin flip on one token instead of a real set
# comparison; (2) even with arrays populated correctly, normalizing by the
# SMALLER set saturated near 100% whenever a large query keyword set was
# compared against a small candidate set. The tests below use small,
# hand-built keyword sets (plain English words, no stopwords) run through the
# real CLI against a stubbed candidate pool, so the resulting percentages are
# exact and checkable by hand (see comments at each candidate).
#
# Usage:
#   ./.loom/scripts/tests/test-check-duplicate.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
CDS="$SCRIPTS_DIR/check-duplicate.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
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

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if ! printf '%s' "$haystack" | grep -qF -- "$needle"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

if [[ ! -x "$CDS" ]]; then
    echo -e "${RED}FATAL${NC}: $CDS not found or not executable" >&2
    exit 2
fi

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

# --- Stub loom-daemon: present on PATH but deliberately non-functional, so
# check-duplicate.sh's `loom-daemon --version` probe fails and it falls back
# to the `gh` stub below (byte-for-byte the documented fallback path,
# defaults/scripts/check-duplicate.sh). Keeps this test independent of
# whether a real loom-daemon happens to be installed on the host.
#
# NOT renamed per #5548's "test fixtures should not be named `loom-daemon`"
# fix, unlike the fixtures in test-loom-status.sh / test-gh-cached.sh /
# test-loom-daemon-watchdog.sh: check-duplicate.sh resolves this binary via a
# bare `command -v loom-daemon` PATH lookup with no LOOM_DAEMON_BIN-style
# override, so the literal name is load-bearing here -- renaming it would
# stop check-duplicate.sh from finding it at all, defeating the test. This
# poses no #5548-shaped liveness-check risk in practice: the stub is invoked
# synchronously (`exit 1`, no backgrounding, no loop), so even a leaked copy
# left on disk after a failed trap can never appear as a running process for
# a `pgrep`-style liveness check to match. ---
cat > "$STUB_DIR/loom-daemon" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
chmod +x "$STUB_DIR/loom-daemon"

# --- Stub git on PATH ---
#   git remote get-url origin -> "https://github.com/owner/repo.git" (or fail
#                                 if $STUB_DIR/git-remote-fail exists). check-
#                                 duplicate.sh's get_repo_nwo() (#4659) resolves
#                                 "owner/repo" from this LOCAL read -- no `gh
#                                 repo view` (GraphQL) round-trip -- so this stub
#                                 never needs to simulate a rate limit for it.
#   anything else              -> delegated to the real `git` binary (unused by
#                                 check-duplicate.sh today; a safety net only).
REAL_GIT_BIN="$(command -v git)"
export REAL_GIT_BIN
cat > "$STUB_DIR/git" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub git: LOOM_TEST_STUB_DIR not set}"
if [[ "$1" == "remote" && "$2" == "get-url" && "$3" == "origin" ]]; then
  if [[ -f "$STUB_DIR_FROM_ENV/git-remote-fail" ]]; then
    echo "stub git: No such remote 'origin'" >&2
    exit 1
  fi
  echo "https://github.com/owner/repo.git"
  exit 0
fi
exec "$REAL_GIT_BIN" "$@"
STUB
chmod +x "$STUB_DIR/git"

# --- Stub gh on PATH ---
#   gh auth status                                     -> exit 0 (authenticated)
#   gh issue list --state=open|closed ...               -> cat $STUB_DIR/issues-<state>.json (or [];
#                                                           simulates a GraphQL rate-limit failure if
#                                                           $STUB_DIR/issue-list-rate-limit-<state> exists)
#   gh pr list --state=merged ...                        -> cat $STUB_DIR/prs-merged.json (or [];
#                                                           simulates a GraphQL rate-limit failure if
#                                                           $STUB_DIR/pr-list-rate-limit exists)
#   gh api repos/{owner}/{repo}/issues/N/timeline --paginate -> cat $STUB_DIR/timeline-N.json (or [];
#                                                           fails if $STUB_DIR/timeline-fail exists)
#   gh api repos/{owner}/{repo}/issues?state=<state>&... -> REST fallback (#4526): cat
#                                                           $STUB_DIR/rest-issues-<state>.json (or [];
#                                                           fails if $STUB_DIR/rest-issues-fail exists)
#   gh api repos/{owner}/{repo}/pulls?state=closed&...   -> REST fallback (#4526): cat
#                                                           $STUB_DIR/rest-prs.json (or [];
#                                                           fails if $STUB_DIR/rest-prs-fail exists)
#
# Note (#4659): check-duplicate.sh no longer calls `gh repo view` anywhere --
# repo resolution (get_repo_nwo(), used only by search_cross_references())
# reads the git remote directly (see the `git` stub above), and the REST
# fallback call sites hit `gh api "repos/{owner}/{repo}/..."` verbatim,
# letting `gh` resolve the placeholder locally. So this stub deliberately has
# no "repo" case left: a resurrected `gh repo view` call in production code
# would fail this test suite with "unhandled args" rather than silently
# passing, which is the point.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"

# The rate-limit error text the stubbed GraphQL calls emit. Defaults to the
# REAL phrasing GitHub's GraphQL API returns -- "API rate limit ALREADY
# exceeded" -- which is what loom-daemon/src/rate_limit_breaker.rs documents
# and unit-tests (RATE_LIMIT_SIGNATURES). This matters: "already exceeded"
# does not contain "API rate limit exceeded" as a contiguous substring, so a
# stub using the REST phrasing would let a check that only matches the REST
# form pass every test while never firing in production (#4526). Tests can
# override via $STUB_DIR/rate-limit-message to exercise other phrasings.
rate_limit_message() {
  if [[ -f "$STUB_DIR_FROM_ENV/rate-limit-message" ]]; then
    cat "$STUB_DIR_FROM_ENV/rate-limit-message"
  else
    echo "GraphQL: API rate limit already exceeded for user ID 12345667."
  fi
}

case "$1" in
  auth)
    exit 0
    ;;
  issue)
    if [[ "$2" == "list" ]]; then
      state="open"
      shift 2
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --state=*) state="${1#--state=}" ;;
        esac
        shift
      done
      if [[ -f "$STUB_DIR_FROM_ENV/issue-list-rate-limit-$state" ]]; then
        echo "$(rate_limit_message) (issue)" >&2
        exit 1
      fi
      canned="$STUB_DIR_FROM_ENV/issues-$state.json"
      if [[ -f "$canned" ]]; then cat "$canned"; else echo "[]"; fi
      exit 0
    fi
    echo "stub gh: unhandled issue args: $*" >&2
    exit 3
    ;;
  pr)
    if [[ "$2" == "list" ]]; then
      if [[ -f "$STUB_DIR_FROM_ENV/pr-list-rate-limit" ]]; then
        echo "$(rate_limit_message) (pr)" >&2
        exit 1
      fi
      canned="$STUB_DIR_FROM_ENV/prs-merged.json"
      if [[ -f "$canned" ]]; then cat "$canned"; else echo "[]"; fi
      exit 0
    fi
    echo "stub gh: unhandled pr args: $*" >&2
    exit 3
    ;;
  api)
    path="$2"
    if [[ "$path" == *"/timeline" ]]; then
      if [[ -f "$STUB_DIR_FROM_ENV/timeline-fail" ]]; then
        echo "stub gh: api call failed" >&2
        exit 1
      fi
      num="${path%/timeline}"
      num="${num##*/}"
      canned="$STUB_DIR_FROM_ENV/timeline-$num.json"
      if [[ -f "$canned" ]]; then cat "$canned"; else echo "[]"; fi
      exit 0
    elif [[ "$path" == *"/issues?"* ]]; then
      if [[ -f "$STUB_DIR_FROM_ENV/rest-issues-fail" ]]; then
        echo "stub gh: rest issues api call failed" >&2
        exit 1
      fi
      state="open"
      [[ "$path" == *"state=closed"* ]] && state="closed"
      canned="$STUB_DIR_FROM_ENV/rest-issues-$state.json"
      if [[ -f "$canned" ]]; then cat "$canned"; else echo "[]"; fi
      exit 0
    elif [[ "$path" == *"/pulls?"* ]]; then
      if [[ -f "$STUB_DIR_FROM_ENV/rest-prs-fail" ]]; then
        echo "stub gh: rest pulls api call failed" >&2
        exit 1
      fi
      canned="$STUB_DIR_FROM_ENV/rest-prs.json"
      if [[ -f "$canned" ]]; then cat "$canned"; else echo "[]"; fi
      exit 0
    fi
    echo "stub gh: unhandled api args: $*" >&2
    exit 3
    ;;
  *)
    echo "stub gh: unhandled args: $*" >&2
    exit 3
    ;;
esac
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

reset_state() {
    rm -f "$STUB_DIR"/issues-*.json "$STUB_DIR"/prs-merged.json "$STUB_DIR"/timeline-*.json
    rm -f "$STUB_DIR"/rest-issues-*.json "$STUB_DIR/rest-prs.json"
    rm -f "$STUB_DIR/timeline-fail" "$STUB_DIR/git-remote-fail"
    rm -f "$STUB_DIR"/issue-list-rate-limit-* "$STUB_DIR/pr-list-rate-limit"
    rm -f "$STUB_DIR/rest-issues-fail" "$STUB_DIR/rest-prs-fail"
    rm -f "$STUB_DIR/rate-limit-message"
}

run_cds() {
    # Runs check-duplicate.sh, capturing stdout to $OUT, stderr to $ERR, exit to $RC.
    OUT="$("$CDS" "$@" 2>"$STUB_DIR/stderr.log")"
    RC=$?
    ERR="$(cat "$STUB_DIR/stderr.log" 2>/dev/null || true)"
}

echo "Testing check-duplicate.sh --issue cross-reference probe..."

# (a) Open cross-referencing issue -> RELATED_OPEN_WORK block, exit 1.
# Mirrors the rjwalters/repo #32/#33 incident: querying #32 surfaces open #33.
reset_state
cat > "$STUB_DIR/timeline-32.json" <<'EOF'
[
  {"event": "labeled", "source": {}},
  {"event": "cross-referenced", "source": {"type": "issue", "issue": {
      "number": 33, "title": "Rework #32's acceptance criteria", "state": "open",
      "repository": {"full_name": "owner/repo"}}}}
]
EOF
run_cds --issue 32 --title "Some issue title" --body "Some issue body"
assert_eq "1" "$RC" "(a) Open cross-reference present -> exit 1"
assert_contains "$OUT" "RELATED_OPEN_WORK" "(a) RELATED_OPEN_WORK header present"
assert_contains "$OUT" "#33: Rework #32's acceptance criteria (open issue, cross-references #32)" \
  "(a) Cross-referencing issue #33 listed with correct format"
assert_not_contains "$OUT" "DUPLICATE_FOUND" "(a) No keyword-similarity duplicates found separately"

# (a2) Same fixture, but the cross-reference source is itself a PR.
reset_state
cat > "$STUB_DIR/timeline-32.json" <<'EOF'
[
  {"event": "cross-referenced", "source": {"type": "issue", "issue": {
      "number": 40, "title": "Implement alternate approach", "state": "open",
      "pull_request": {"url": "https://example.invalid"},
      "repository": {"full_name": "owner/repo"}}}}
]
EOF
run_cds --issue 32 --title "Some issue title"
assert_eq "1" "$RC" "(a2) Open cross-referencing PR -> exit 1"
assert_contains "$OUT" "PR #40: Implement alternate approach (open PR, cross-references #32)" \
  "(a2) Cross-referencing PR #40 listed with PR-specific format"

# (b) Closed cross-reference and self-reference are excluded.
reset_state
cat > "$STUB_DIR/timeline-50.json" <<'EOF'
[
  {"event": "cross-referenced", "source": {"type": "issue", "issue": {
      "number": 51, "title": "Closed related work", "state": "closed",
      "repository": {"full_name": "owner/repo"}}}},
  {"event": "cross-referenced", "source": {"type": "issue", "issue": {
      "number": 50, "title": "Self reference", "state": "open",
      "repository": {"full_name": "owner/repo"}}}}
]
EOF
run_cds --issue 50 --title "Some issue title"
assert_eq "0" "$RC" "(b) Only closed/self cross-references -> exit 0 (nothing to surface)"
assert_not_contains "$OUT" "RELATED_OPEN_WORK" "(b) No RELATED_OPEN_WORK block emitted"
assert_not_contains "$OUT" "#51" "(b) Closed cross-reference #51 excluded"
assert_not_contains "$OUT" "#50" "(b) Self-reference #50 excluded"

# (c) No cross-references at all -> behavior identical to a plain run.
reset_state
run_cds --issue 60 --title "Some issue title"
assert_eq "0" "$RC" "(c) No cross-references -> exit 0"
assert_not_contains "$OUT" "RELATED_OPEN_WORK" "(c) No RELATED_OPEN_WORK block when timeline is empty"

# (d) Invocation WITHOUT --issue is byte-identical to a --issue run with no
# cross-references (Architect/Hermit/Auditor non-regression: they never pass
# --issue, so their behavior must be untouched by this feature).
reset_state
run_cds --title "Some issue title"
OUT_NO_ISSUE="$OUT"
RC_NO_ISSUE="$RC"
run_cds --issue 60 --title "Some issue title"
assert_eq "$OUT_NO_ISSUE" "$OUT" "(d) --issue with no cross-refs -> output byte-identical to no --issue at all"
assert_eq "$RC_NO_ISSUE" "$RC" "(d) --issue with no cross-refs -> exit code identical to no --issue at all"

# (e) Timeline API failure -> probe skipped with a stderr warning, base
# similarity check result (and its exit code) is unaffected by the failure.
reset_state
: > "$STUB_DIR/timeline-fail"
run_cds --issue 70 --title "Some issue title"
assert_eq "0" "$RC" "(e) Timeline API failure -> exit code driven by similarity check alone"
assert_not_contains "$OUT" "RELATED_OPEN_WORK" "(e) Timeline API failure -> probe result skipped, not surfaced"
assert_contains "$ERR" "Failed to fetch timeline" "(e) Timeline API failure -> stderr warning emitted"

# (f) --json output includes a "cross_reference" typed entry.
reset_state
cat > "$STUB_DIR/timeline-32.json" <<'EOF'
[
  {"event": "cross-referenced", "source": {"type": "issue", "issue": {
      "number": 33, "title": "Rework #32's acceptance criteria", "state": "open",
      "repository": {"full_name": "owner/repo"}}}}
]
EOF
run_cds --issue 32 --title "Some issue title" --json
assert_eq "1" "$RC" "(f) --json with a cross-reference -> exit 1"
assert_contains "$OUT" '"type": "cross_reference"' "(f) --json output tags the entry as type cross_reference"
duplicate_found="$(echo "$OUT" | jq -r '.duplicate_found')"
assert_eq "true" "$duplicate_found" "(f) --json duplicate_found is true when only related work is present"
cross_num="$(echo "$OUT" | jq -r '.matches[] | select(.type == "cross_reference") | .number')"
assert_eq "33" "$cross_num" "(f) --json cross_reference entry carries the correct issue number"

# (g) Repo can't be resolved from the git remote (#4659: get_repo_nwo() reads
# `git remote get-url origin`, not `gh repo view`) -> degrades gracefully:
# probe skipped, similarity check unaffected, no hard failure.
reset_state
: > "$STUB_DIR/git-remote-fail"
run_cds --issue 80 --title "Some issue title"
assert_eq "0" "$RC" "(g) git remote resolution failure -> exit code driven by similarity check alone"
assert_not_contains "$OUT" "RELATED_OPEN_WORK" "(g) git remote resolution failure -> probe result skipped"
assert_contains "$ERR" "Failed to resolve repository" "(g) git remote resolution failure -> stderr warning emitted"

echo ""
echo "Testing check-duplicate.sh keyword-similarity scoring (issue #4409)..."

# All fixtures below use NATO-alphabet words (alpha, bravo, ...) as keywords:
# real English words, none of them in extract_keywords' stopword list, all
# >= 3 chars, so every keyword survives extraction and the resulting Jaccard
# percentages (matches * 100 / (|A| + |B| - matches)) are exact and easy to
# verify by hand.

# (h) Identical keyword sets -> 100% similarity (union == intersection).
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[{"number": 501, "title": "Alpha Bravo Charlie Delta", "body": ""}]
EOF
run_cds --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "1" "$RC" "(h) Identical keyword sets -> exit 1 (duplicate found)"
assert_contains "$OUT" "#501: Alpha Bravo Charlie Delta (similarity: 100%)" \
  "(h) Identical keyword sets score exactly 100%"

# (i) Disjoint keyword sets -> 0% similarity, never flagged even at a
# near-zero threshold.
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[{"number": 502, "title": "Yankee Zulu Xray Whiskey", "body": ""}]
EOF
run_cds --threshold 1 --title "Alpha Bravo Charlie Delta"
assert_eq "0" "$RC" "(i) Disjoint keyword sets -> exit 0 (no duplicate)"
assert_not_contains "$OUT" "DUPLICATE_FOUND" "(i) Disjoint keyword sets never flagged"

# (j) Partial overlap -> exact Jaccard percentage, not the old
# matches/min(|A|,|B|) normalization. Query {alpha,bravo,charlie,delta} (4)
# vs candidate {alpha,bravo,echo,foxtrot,golf} (5): matches=2 (alpha,bravo),
# union=4+5-2=7, percent=2*100/7=28 (integer division). The pre-#4409
# min-set normalization would have scored this matches/min(4,5)=2/4=50% --
# a materially different (and saturating) number.
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[{"number": 503, "title": "Alpha Bravo Echo Foxtrot Golf", "body": ""}]
EOF
run_cds --threshold 28 --title "Alpha Bravo Charlie Delta"
assert_eq "1" "$RC" "(j) Partial overlap at threshold==percent -> exit 1"
assert_contains "$OUT" "#503: Alpha Bravo Echo Foxtrot Golf (similarity: 28%)" \
  "(j) Partial overlap scores exact true-Jaccard percentage (28%, not 50%)"
run_cds --threshold 29 --title "Alpha Bravo Charlie Delta"
assert_eq "0" "$RC" "(j) Partial overlap just below threshold -> exit 0"
assert_not_contains "$OUT" "DUPLICATE_FOUND" "(j) 28% does not clear a 29% threshold"

# (k) Empty keyword set on the CANDIDATE side (all stopwords) -> the existing
# early-return in calculate_similarity (arr2 has 0 elements) must still yield
# 0%, not a crash or division by zero.
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[{"number": 504, "title": "The Is Are", "body": ""}]
EOF
run_cds --threshold 1 --title "Alpha Bravo Charlie Delta"
assert_eq "0" "$RC" "(k) All-stopword candidate -> empty keyword set -> exit 0, no crash"
assert_not_contains "$OUT" "DUPLICATE_FOUND" "(k) Empty candidate keyword set never matches"

# (k2) Empty keyword set on the QUERY side (title is entirely stopwords) --
# search_similar_issues' own early return (before ever calling
# calculate_similarity), with a warning on stderr and no forge calls.
reset_state
run_cds --title "Is Are Was"
assert_eq "0" "$RC" "(k2) All-stopword query title -> exit 0, no crash"
assert_contains "$ERR" "No significant keywords" "(k2) All-stopword query title warns on stderr"

# (l) One-word title on both sides: identical single-keyword sets -> 100%,
# disjoint single-keyword sets -> 0%.
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[
  {"number": 505, "title": "Alpha", "body": ""},
  {"number": 506, "title": "Bravo", "body": ""}
]
EOF
run_cds --threshold 50 --title "Alpha"
assert_eq "1" "$RC" "(l) One-word query matches its identical one-word candidate"
assert_contains "$OUT" "#505: Alpha (similarity: 100%)" "(l) Identical one-word sets score 100%"
assert_not_contains "$OUT" "#506" "(l) Disjoint one-word candidate (Bravo) not matched"

# (m) Degenerate-result self-detection: when more than half of the scanned
# candidates score >= threshold, the search must self-announce
# NON_DISCRIMINATIVE instead of dumping a DUPLICATE_FOUND wall. Query has 7
# keywords; of 4 open-issue candidates, 3 share enough keywords to clear the
# default --threshold (18) and 1 is unrelated:
#   #601 "Quantum flux widget"              -> matches=2/union=8  -> 25%
#   #602 "Capacitor reactor system"         -> matches=2/union=8  -> 25%
#   #603 "Completely unrelated banana fruit basket" -> matches=0  ->  0%
#   #604 "Core module driver suite"         -> matches=3/union=8  -> 37%
# matched=3 of scanned=4 -> 3*2=6 > 4 -> degenerate.
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[
  {"number": 601, "title": "Quantum flux widget", "body": ""},
  {"number": 602, "title": "Capacitor reactor system", "body": ""},
  {"number": 603, "title": "Completely unrelated banana fruit basket", "body": ""},
  {"number": 604, "title": "Core module driver suite", "body": ""}
]
EOF
run_cds --title "Quantum Flux Capacitor Reactor Core Module Driver"
assert_eq "1" "$RC" "(m) Degenerate result set -> still exit 1 (caller should not treat as safe)"
assert_contains "$OUT" "NON_DISCRIMINATIVE" "(m) Degenerate result set -> NON_DISCRIMINATIVE warning emitted"
assert_contains "$OUT" "3 of 4 candidates" "(m) NON_DISCRIMINATIVE reports matched/scanned counts"
assert_not_contains "$OUT" "DUPLICATE_FOUND" "(m) Degenerate result set -> no DUPLICATE_FOUND wall"

# (m2) Same fixture, --json: degenerate flag set, duplicate_found false,
# empty matches (a degenerate result is noise, not a real match list).
run_cds --title "Quantum Flux Capacitor Reactor Core Module Driver" --json
assert_eq "1" "$RC" "(m2) --json degenerate result -> exit 1"
degenerate_flag="$(echo "$OUT" | jq -r '.degenerate')"
assert_eq "true" "$degenerate_flag" "(m2) --json degenerate flag is true"
duplicate_found_flag="$(echo "$OUT" | jq -r '.duplicate_found')"
assert_eq "false" "$duplicate_found_flag" "(m2) --json duplicate_found is false for a degenerate (noise) result"
matches_len="$(echo "$OUT" | jq -r '.matches | length')"
assert_eq "0" "$matches_len" "(m2) --json matches is empty for a degenerate result"

# (n) Mixed real-match + degenerate-result header regression test: when
# --include-merged-prs turns up a REAL match in one pool (merged PRs) and a
# DEGENERATE result in the other (closed issues), the real match must still
# get a "DUPLICATE_FOUND" header -- an earlier draft of this fix required
# BOTH pools to be non-degenerate before adding the header, which silently
# swallowed a real duplicate whenever the other pool happened to be noisy.
reset_state
echo "[]" > "$STUB_DIR/issues-open.json"
cat > "$STUB_DIR/prs-merged.json" <<'EOF'
[
  {"number": 701, "title": "Quantum flux widget", "body": ""},
  {"number": 702, "title": "Zulu Yankee Xray", "body": ""},
  {"number": 703, "title": "Whiskey Tango Foxtrot", "body": ""}
]
EOF
cat > "$STUB_DIR/issues-closed.json" <<'EOF'
[
  {"number": 601, "title": "Quantum flux widget", "body": ""},
  {"number": 602, "title": "Capacitor reactor system", "body": ""},
  {"number": 603, "title": "Completely unrelated banana fruit basket", "body": ""},
  {"number": 604, "title": "Core module driver suite", "body": ""}
]
EOF
run_cds --include-merged-prs --title "Quantum Flux Capacitor Reactor Core Module Driver"
assert_eq "1" "$RC" "(n) Mixed real+degenerate -> exit 1"
assert_contains "$OUT" "DUPLICATE_FOUND" \
  "(n) A real match in one pool still gets a DUPLICATE_FOUND header even when the other pool is degenerate"
assert_contains "$OUT" "PR #701: Quantum flux widget (similarity: 25%)" \
  "(n) Real merged-PR match is listed under the header"
assert_contains "$OUT" "NON_DISCRIMINATIVE (closed issues)" \
  "(n) Degenerate closed-issues pool still self-announces alongside the real match"

# (o) Real historical duplicate pair, threshold calibration (#4409): #3551
# ("guard-destructive.sh emits PreToolUse decisions without hookEventName ->
# guard is a silent no-op") was closed with "Closing as already fixed. This
# is a duplicate of #3550" -- an actual confirmed duplicate from this repo's
# history, not a synthetic fixture. Titles only (both are real, verbatim):
# keyword sets {decisions,destructive,emits,guard,hookeventname,pretooluse,
# silent,without} (8) vs {ask,decisions,deny,destructive,discarded,guard,
# hookeventname,hookspecificoutput,missing,required,silently} (11); 4 shared
# words (decisions, destructive, guard, hookeventname); union=8+11-4=15;
# true Jaccard = 4*100/15 = 26% -- clears the calibrated default threshold
# (18, no --threshold flag passed here) even though the two titles share no
# common phrasing beyond individual keywords.
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[{"number": 3550, "title": "guard-destructive.sh: hookSpecificOutput missing required hookEventName -- deny/ask decisions silently discarded", "body": ""}]
EOF
run_cds --title "guard-destructive.sh emits PreToolUse decisions without hookEventName -> guard is a silent no-op"
assert_eq "1" "$RC" "(o) Real historical duplicate pair (#3550/#3551) -> exit 1 at the calibrated default threshold"
assert_contains "$OUT" "#3550" "(o) Real historical duplicate pair (#3550/#3551) -> flagged as a candidate"
assert_contains "$OUT" "(similarity: 26%)" "(o) Real historical duplicate pair scores the expected 26% true-Jaccard"

echo ""
echo "Testing check-duplicate.sh self-match exclusion via --issue (issue #4662)..."

# check-duplicate.sh never excluded the issue being curated from its own
# similarity candidate pool: the curator workflow fetches the existing
# issue's own title/body and passes them in, so the issue is in the
# open-issues pool it's being checked against and matches itself at ~100%,
# forcing a guaranteed false DUPLICATE_FOUND on every curation pass of an
# already-filed issue. Threading the --issue number into the three
# similarity-search functions and skipping that number in each candidate
# loop fixes it; passing NO --issue (pre-filing checks, where the issue
# doesn't exist yet) must remain byte-for-byte unchanged.

# (aa) Candidate pool contains only the probed issue itself (same number,
# identical title/body) -- the exact self-noise from #4662. With --issue <n>
# passed, the self-candidate is excluded from the pool entirely (not just
# scored below threshold): with nothing left to scan, the search reports "no
# duplicates", not a self-match.
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[{"number": 4659, "title": "check-duplicate.sh: REST fallback still dies", "body": "REST fallback dies under load"}]
EOF
run_cds --issue 4659 --title "check-duplicate.sh: REST fallback still dies" --body "REST fallback dies under load"
assert_eq "0" "$RC" "(aa) Self-only candidate pool + --issue <n> -> exit 0, no self-match false positive"
assert_not_contains "$OUT" "DUPLICATE_FOUND" "(aa) Self-match excluded, no DUPLICATE_FOUND"

# (aa2) Same fixture WITHOUT --issue -> the self-match still surfaces. This is
# the documented pre-existing behavior for pre-filing checks (Architect/Hermit/
# Auditor calls, which never pass --issue because the issue doesn't exist yet)
# -- there is no self number to exclude, so nothing changes for them.
run_cds --title "check-duplicate.sh: REST fallback still dies" --body "REST fallback dies under load"
assert_eq "1" "$RC" "(aa2) Same fixture without --issue -> self-match still surfaces (no self number to exclude)"
assert_contains "$OUT" "DUPLICATE_FOUND" "(aa2) Without --issue, the match is reported (unchanged pre-existing behavior)"
assert_contains "$OUT" "#4659" "(aa2) Without --issue, the (self) match is listed"

# (ab) Self-candidate PLUS a genuinely different candidate in the same pool:
# exclusion removes only the self entry -- a real duplicate elsewhere in the
# pool is still found and reported.
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[
  {"number": 4659, "title": "Alpha Bravo Charlie Delta", "body": ""},
  {"number": 4700, "title": "Alpha Bravo Charlie Delta", "body": ""}
]
EOF
run_cds --issue 4659 --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "1" "$RC" "(ab) Self plus a genuine duplicate candidate -> genuine duplicate still found"
assert_contains "$OUT" "#4700: Alpha Bravo Charlie Delta (similarity: 100%)" "(ab) Genuine (non-self) duplicate reported"
assert_not_contains "$OUT" "#4659:" "(ab) Self-candidate excluded from the match list"

# (ac) Self-exclusion also threads through the closed-issues pool
# (--include-merged-prs): a probed issue that shows up in the closed-issues
# pool (e.g. it was briefly closed/reopened) must not match itself there.
reset_state
echo "[]" > "$STUB_DIR/issues-open.json"
cat > "$STUB_DIR/issues-closed.json" <<'EOF'
[{"number": 4659, "title": "Alpha Bravo Charlie Delta", "body": ""}]
EOF
run_cds --include-merged-prs --issue 4659 --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "0" "$RC" "(ac) Self-match in the closed-issues pool excluded via --issue -> exit 0"
assert_not_contains "$OUT" "DUPLICATE_FOUND" "(ac) No DUPLICATE_FOUND from the closed-issues self-match"

# (ad) Self-exclusion threads through the merged-PRs pool identically. GitHub
# issue/PR numbers share one sequence, so a genuine self-match can't occur
# here in production -- this is a defensive-consistency check that the same
# skip parameter doesn't misbehave (e.g. off-by-one, wrong variable) if a
# candidate ever carries the probed issue's number.
reset_state
echo "[]" > "$STUB_DIR/issues-open.json"
cat > "$STUB_DIR/prs-merged.json" <<'EOF'
[{"number": 4659, "title": "Alpha Bravo Charlie Delta", "body": ""}]
EOF
run_cds --include-merged-prs --issue 4659 --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "0" "$RC" "(ad) Self-numbered candidate in the merged-PRs pool excluded via --issue -> exit 0"
assert_not_contains "$OUT" "DUPLICATE_FOUND" "(ad) No DUPLICATE_FOUND from the merged-PRs self-numbered match"

echo ""
echo "Testing check-duplicate.sh REST fallback on GraphQL rate limit (issue #4526)..."

# (p) Open-issues GraphQL rate-limited -> REST fallback succeeds and is used.
# REST's /issues endpoint also returns PRs, so the fixture includes one
# (#802) to confirm it's filtered out (pull_request != null).
reset_state
: > "$STUB_DIR/issue-list-rate-limit-open"
cat > "$STUB_DIR/rest-issues-open.json" <<'EOF'
[
  {"number": 801, "title": "Alpha Bravo Charlie Delta", "body": "", "pull_request": null},
  {"number": 802, "title": "Alpha Bravo Charlie Delta", "body": "", "pull_request": {"url": "x"}}
]
EOF
run_cds --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "1" "$RC" "(p) Rate-limited GraphQL open-issues search falls back to REST -> exit 1"
assert_contains "$OUT" "DUPLICATE_FOUND (REST fallback" "(p) REST fallback output is labeled distinctly from a normal GraphQL match"
assert_contains "$OUT" "#801: Alpha Bravo Charlie Delta (similarity: 100%)" "(p) REST fallback finds the matching issue"
assert_not_contains "$OUT" "#802" "(p) REST fallback excludes PR entries returned by the /issues endpoint"

# (p2) GLOBAL GraphQL exhaustion (issue #4659): not just the `issue list`
# GraphQL call is rate-limited, but repo resolution is ALSO unavailable (here,
# `git remote get-url origin` fails outright) -- modeling the real production
# incident this issue is about, where get_repo_nwo() used to be `gh repo
# view` (itself GraphQL-backed) and its failure short-circuited the REST
# fallback via `&&` BEFORE `gh api` was ever attempted, reporting exit 2
# ("could not check") and blaming REST for a failure REST never had. With the
# fix, the REST-fallback call sites resolve "{owner}/{repo}" via `gh api`'s
# own local placeholder substitution and never call get_repo_nwo()/git at
# all, so this must still succeed with exit 1, not degrade to exit 2.
reset_state
: > "$STUB_DIR/issue-list-rate-limit-open"
: > "$STUB_DIR/git-remote-fail"
cat > "$STUB_DIR/rest-issues-open.json" <<'EOF'
[{"number": 801, "title": "Alpha Bravo Charlie Delta", "body": "", "pull_request": null}]
EOF
run_cds --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "1" "$RC" "(p2) Global GraphQL exhaustion (list AND repo resolution both down) -> REST fallback still completes, exit 1"
assert_contains "$OUT" "DUPLICATE_FOUND (REST fallback" "(p2) REST fallback output labeled distinctly even under global exhaustion"
assert_contains "$OUT" "#801: Alpha Bravo Charlie Delta (similarity: 100%)" "(p2) REST fallback still finds the matching issue"
assert_not_contains "$ERR" "REST fallback also failed" "(p2) REST never actually failed -- must not be blamed for the repo-resolution outage"

# (q) Open-issues GraphQL rate-limited AND the REST fallback also fails ->
# exit 2 ("could not run at all"), not silently treated as no duplicates.
reset_state
: > "$STUB_DIR/issue-list-rate-limit-open"
: > "$STUB_DIR/rest-issues-fail"
run_cds --title "Alpha Bravo Charlie Delta"
assert_eq "2" "$RC" "(q) Rate-limited GraphQL AND failed REST fallback (open issues) -> exit 2"
assert_contains "$ERR" "REST fallback also failed" "(q) stderr explains both GraphQL and REST failed"

# (r) Merged-PRs GraphQL rate-limited -> REST fallback succeeds. REST has no
# state=merged filter, so the fetch is `pulls?state=closed` filtered locally
# to merged_at != null; the fixture includes a closed-but-unmerged PR (#902)
# to confirm it's excluded.
reset_state
: > "$STUB_DIR/pr-list-rate-limit"
cat > "$STUB_DIR/rest-prs.json" <<'EOF'
[
  {"number": 901, "title": "Alpha Bravo Charlie Delta", "body": "", "merged_at": "2026-01-01T00:00:00Z"},
  {"number": 902, "title": "Alpha Bravo Charlie Delta", "body": "", "merged_at": null}
]
EOF
run_cds --include-merged-prs --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "1" "$RC" "(r) Rate-limited GraphQL merged-PR search falls back to REST -> exit 1"
assert_contains "$OUT" "DUPLICATE_FOUND (REST fallback" "(r) REST fallback header labeled for the merged-PR pool"
assert_contains "$OUT" "PR #901: Alpha Bravo Charlie Delta (similarity: 100%)" "(r) REST fallback finds the merged PR"
assert_not_contains "$OUT" "PR #902" "(r) REST fallback excludes the closed-but-unmerged PR (merged_at null)"

# (s) Merged-PRs GraphQL rate-limited AND the REST fallback also fails ->
# exit 2, matching the open-issues both-failed case.
reset_state
: > "$STUB_DIR/pr-list-rate-limit"
: > "$STUB_DIR/rest-prs-fail"
run_cds --include-merged-prs --title "Alpha Bravo Charlie Delta"
assert_eq "2" "$RC" "(s) Rate-limited GraphQL AND failed REST fallback (merged PRs) -> exit 2"
assert_contains "$ERR" "REST fallback also failed" "(s) stderr explains both GraphQL and REST failed (merged PRs)"

# (t) Closed-issues GraphQL rate-limited -> REST fallback succeeds.
reset_state
: > "$STUB_DIR/issue-list-rate-limit-closed"
cat > "$STUB_DIR/rest-issues-closed.json" <<'EOF'
[{"number": 1001, "title": "Alpha Bravo Charlie Delta", "body": "", "pull_request": null}]
EOF
run_cds --include-merged-prs --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "1" "$RC" "(t) Rate-limited GraphQL closed-issues search falls back to REST -> exit 1"
assert_contains "$OUT" "DUPLICATE_FOUND (REST fallback" "(t) REST fallback header labeled for the closed-issues pool"
assert_contains "$OUT" "Closed #1001: Alpha Bravo Charlie Delta (similarity: 100%)" "(t) REST fallback finds the closed issue"

# (u) Closed-issues GraphQL rate-limited AND the REST fallback also fails ->
# exit 2, matching the other two both-failed cases.
reset_state
: > "$STUB_DIR/issue-list-rate-limit-closed"
: > "$STUB_DIR/rest-issues-fail"
run_cds --include-merged-prs --title "Alpha Bravo Charlie Delta"
assert_eq "2" "$RC" "(u) Rate-limited GraphQL AND failed REST fallback (closed issues) -> exit 2"
assert_contains "$ERR" "REST fallback also failed" "(u) stderr explains both GraphQL and REST failed (closed issues)"

# (v) Regression: an ordinary successful (non-rate-limited) GraphQL call must
# NOT trigger the REST fallback path or its labeling.
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[{"number": 1101, "title": "Alpha Bravo Charlie Delta", "body": ""}]
EOF
run_cds --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "1" "$RC" "(v) Ordinary GraphQL success -> exit 1"
assert_contains "$OUT" "DUPLICATE_FOUND" "(v) Ordinary GraphQL success still emits a DUPLICATE_FOUND header"
assert_not_contains "$OUT" "REST fallback" "(v) Ordinary GraphQL success never mentions the REST fallback"

echo ""
echo "Testing rate-limit signature coverage (both GraphQL and REST phrasings)..."

# (w) The REST phrasing ("API rate limit exceeded", no "already") must ALSO
# trigger the fallback. Cases (p)-(v) above now run against the default stub
# message, which is the GraphQL phrasing ("API rate limit ALREADY exceeded"),
# so this case pins the other half of the signature table. A single-pattern
# check for either phrasing alone fails one of these two groups: "already
# exceeded" does not contain "API rate limit exceeded" contiguously, and vice
# versa. Ground truth: loom-daemon/src/rate_limit_breaker.rs
# RATE_LIMIT_SIGNATURES lists both as distinct, both-required entries.
reset_state
echo "HTTP 403: API rate limit exceeded for 1.2.3.4" > "$STUB_DIR/rate-limit-message"
: > "$STUB_DIR/issue-list-rate-limit-open"
cat > "$STUB_DIR/rest-issues-open.json" <<'EOF'
[{"number": 811, "title": "Alpha Bravo Charlie Delta", "body": "", "pull_request": null}]
EOF
run_cds --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "1" "$RC" "(w) REST phrasing 'API rate limit exceeded' also triggers the fallback -> exit 1"
assert_contains "$OUT" "DUPLICATE_FOUND (REST fallback" "(w) REST-phrasing rate limit is recognized, not treated as a hard error"
assert_contains "$OUT" "#811" "(w) REST fallback still finds the match under the REST phrasing"

# (w2) Case-insensitivity: the signature match lowercases first (mirroring
# rate_limit_breaker.rs's to_ascii_lowercase), so an all-caps deployment
# variant is still recognized.
reset_state
echo "GRAPHQL: API RATE LIMIT ALREADY EXCEEDED FOR USER ID 99" > "$STUB_DIR/rate-limit-message"
: > "$STUB_DIR/issue-list-rate-limit-open"
cat > "$STUB_DIR/rest-issues-open.json" <<'EOF'
[{"number": 812, "title": "Alpha Bravo Charlie Delta", "body": "", "pull_request": null}]
EOF
run_cds --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "1" "$RC" "(w2) Rate-limit signature match is case-insensitive -> exit 1"
assert_contains "$OUT" "#812" "(w2) Uppercase rate-limit text still routes to the REST fallback"

# (w3) Secondary rate limits are in the same signature table and also route
# to the REST fallback rather than the hard-error path.
reset_state
echo "You have exceeded a secondary rate limit. Please wait a few minutes." > "$STUB_DIR/rate-limit-message"
: > "$STUB_DIR/issue-list-rate-limit-open"
cat > "$STUB_DIR/rest-issues-open.json" <<'EOF'
[{"number": 813, "title": "Alpha Bravo Charlie Delta", "body": "", "pull_request": null}]
EOF
run_cds --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "1" "$RC" "(w3) Secondary rate limit routes to the REST fallback -> exit 1"
assert_contains "$OUT" "#813" "(w3) Secondary-rate-limit text still routes to the REST fallback"

# (w4) Regression guard on the narrow gate: a NON-rate-limit GraphQL failure
# must still take the original hard-error path (exit 2), never the fallback.
reset_state
echo "GraphQL: Could not resolve to a Repository with the name 'owner/repo'." > "$STUB_DIR/rate-limit-message"
: > "$STUB_DIR/issue-list-rate-limit-open"
cat > "$STUB_DIR/rest-issues-open.json" <<'EOF'
[{"number": 814, "title": "Alpha Bravo Charlie Delta", "body": "", "pull_request": null}]
EOF
run_cds --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "2" "$RC" "(w4) Non-rate-limit GraphQL failure still hard-errors -> exit 2"
assert_contains "$ERR" "Failed to fetch issues" "(w4) Non-rate-limit failure uses the original error path"
assert_not_contains "$OUT" "#814" "(w4) Non-rate-limit failure never consults the REST fallback fixture"

echo ""
echo "Testing partial-failure vs confirmed-duplicate precedence (issue #4526)..."

# (x) THE regression this section exists for: the open-issues search finds a
# real duplicate via ordinary GraphQL while the merged-PRs pool fails
# COMPLETELY (GraphQL rate-limited AND REST fallback failed). A confirmed
# duplicate must win: exit 1 with the match reported, plus a
# SEARCH_INCOMPLETE note naming the pool that could not be checked. The
# earlier draft unconditionally overwrote the exit code with 2 whenever any
# pool totally failed, silently discarding the real match.
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[{"number": 801, "title": "Alpha Bravo Charlie Delta", "body": ""}]
EOF
: > "$STUB_DIR/pr-list-rate-limit"
: > "$STUB_DIR/rest-prs-fail"
run_cds --include-merged-prs --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "1" "$RC" "(x) Confirmed open-issue duplicate outranks a total merged-PR failure -> exit 1, not 2"
assert_contains "$OUT" "DUPLICATE_FOUND" "(x) The confirmed duplicate is still reported"
assert_contains "$OUT" "#801: Alpha Bravo Charlie Delta (similarity: 100%)" "(x) The match itself is not discarded"
assert_contains "$OUT" "SEARCH_INCOMPLETE: merged PRs" "(x) The uncheckable pool is named in a SEARCH_INCOMPLETE note"
assert_contains "$ERR" "REST fallback also failed" "(x) stderr still explains the merged-PR pool failure"

# (x2) Same scenario in --json mode, where the old behavior was worse than a
# wrong exit code: the exit-2 branch emits a bare {"error": ...} and drops the
# `matches` array entirely, so a confirmed duplicate vanished from the output.
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[{"number": 801, "title": "Alpha Bravo Charlie Delta", "body": ""}]
EOF
: > "$STUB_DIR/pr-list-rate-limit"
: > "$STUB_DIR/rest-prs-fail"
run_cds --include-merged-prs --threshold 50 --title "Alpha Bravo Charlie Delta" --json
assert_eq "1" "$RC" "(x2) --json: confirmed duplicate + total pool failure -> exit 1"
assert_eq "true" "$(echo "$OUT" | jq -r '.duplicate_found')" "(x2) --json reports duplicate_found true"
assert_eq "801" "$(echo "$OUT" | jq -r '.matches[0].number')" "(x2) --json still carries the confirmed match"
assert_eq "1" "$(echo "$OUT" | jq -r '.matches | length')" "(x2) --json matches contains exactly the real match"
assert_eq "true" "$(echo "$OUT" | jq -r '.search_incomplete')" "(x2) --json flags the degraded coverage separately from the match"
assert_not_contains "$OUT" '"error"' "(x2) --json no longer collapses to a bare error object"

# (x3) Mirror image: the OPEN-issues pool fails completely while the
# merged-PRs pool finds a real duplicate. Same principle, opposite direction
# -- the match must survive and get its DUPLICATE_FOUND header even though
# the pool searched first could not run.
reset_state
: > "$STUB_DIR/issue-list-rate-limit-open"
: > "$STUB_DIR/rest-issues-fail"
cat > "$STUB_DIR/prs-merged.json" <<'EOF'
[{"number": 901, "title": "Alpha Bravo Charlie Delta", "body": ""}]
EOF
run_cds --include-merged-prs --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "1" "$RC" "(x3) Merged-PR duplicate outranks a total open-issues failure -> exit 1"
assert_contains "$OUT" "DUPLICATE_FOUND" "(x3) The merged-PR match still gets an umbrella header"
assert_contains "$OUT" "PR #901: Alpha Bravo Charlie Delta (similarity: 100%)" "(x3) The merged-PR match is reported"
assert_contains "$OUT" "SEARCH_INCOMPLETE: open issues" "(x3) The uncheckable open-issues pool is named"

# (y) Total failure with NO duplicate anywhere still means exit 2 -- the
# precedence fix must not weaken the "could not run at all" signal that the
# rest of this section depends on.
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[{"number": 805, "title": "Completely unrelated banana fruit basket", "body": ""}]
EOF
: > "$STUB_DIR/pr-list-rate-limit"
: > "$STUB_DIR/rest-prs-fail"
run_cds --include-merged-prs --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "2" "$RC" "(y) Total pool failure with no duplicate found anywhere -> still exit 2"
assert_not_contains "$OUT" "SEARCH_INCOMPLETE" "(y) No SEARCH_INCOMPLETE note when there is no match to qualify"

# (y2) ...and its --json form still reports the bare error object, unchanged.
reset_state
: > "$STUB_DIR/pr-list-rate-limit"
: > "$STUB_DIR/rest-prs-fail"
run_cds --include-merged-prs --threshold 50 --title "Alpha Bravo Charlie Delta" --json
assert_eq "2" "$RC" "(y2) --json: total failure with no match -> exit 2"
assert_contains "$OUT" '"error"' "(y2) --json still reports the error object when nothing could be determined"

# (z) Non-blocking gap from review, fixed here: the open-issues search matches
# via ORDINARY GraphQL (so its plain DUPLICATE_FOUND header stands) while the
# merged-PRs pool separately succeeds through the REST fallback. Without a
# REST_FALLBACK note the REST-sourced subset would be reported with no hint
# that its similarity ranking basis differs.
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[{"number": 801, "title": "Alpha Bravo Charlie Delta", "body": ""}]
EOF
: > "$STUB_DIR/pr-list-rate-limit"
cat > "$STUB_DIR/rest-prs.json" <<'EOF'
[{"number": 901, "title": "Alpha Bravo Charlie Delta", "body": "", "merged_at": "2026-01-01T00:00:00Z"}]
EOF
run_cds --include-merged-prs --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "1" "$RC" "(z) Ordinary-GraphQL open match + REST-fallback merged match -> exit 1"
assert_contains "$OUT" "#801: Alpha Bravo Charlie Delta (similarity: 100%)" "(z) Open-issue match reported"
assert_contains "$OUT" "PR #901: Alpha Bravo Charlie Delta (similarity: 100%)" "(z) REST-fallback merged-PR match reported"
assert_contains "$OUT" "REST_FALLBACK: merged PRs" "(z) REST provenance is labeled even though the umbrella header came from the open-issues search"

# (z2) The new marker lines must never be parsed as matches by --json.
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[{"number": 801, "title": "Alpha Bravo Charlie Delta", "body": ""}]
EOF
: > "$STUB_DIR/pr-list-rate-limit"
cat > "$STUB_DIR/rest-prs.json" <<'EOF'
[{"number": 901, "title": "Alpha Bravo Charlie Delta", "body": "", "merged_at": "2026-01-01T00:00:00Z"}]
EOF
run_cds --include-merged-prs --threshold 50 --title "Alpha Bravo Charlie Delta" --json
assert_eq "2" "$(echo "$OUT" | jq -r '.matches | length')" "(z2) --json parses exactly the two real matches, not the REST_FALLBACK marker line"
assert_eq "false" "$(echo "$OUT" | jq -r '.search_incomplete')" "(z2) A successful REST fallback is NOT reported as incomplete coverage"

# (z3) Line-splicing regression, latent on main and first exposed by (z)/(z2):
# `result=$(search_similar_issues ...)` loses its trailing newline to command
# substitution, so appending the merged-PR list ran the two pools' lists
# together on ONE line ("#801: ... (similarity: 100%)PR #901: ..."). Text
# output was merely garbled; --json was worse, parsing the spliced pair as a
# single match whose title swallowed the second entry and whose number was
# lost. Exercised here with no rate-limiting at all, so it pins the plain
# multi-pool aggregation path independently of the #4526 fallback.
reset_state
cat > "$STUB_DIR/issues-open.json" <<'EOF'
[{"number": 801, "title": "Alpha Bravo Charlie Delta", "body": ""}]
EOF
cat > "$STUB_DIR/prs-merged.json" <<'EOF'
[{"number": 901, "title": "Alpha Bravo Charlie Delta", "body": ""}]
EOF
cat > "$STUB_DIR/issues-closed.json" <<'EOF'
[{"number": 1001, "title": "Alpha Bravo Charlie Delta", "body": ""}]
EOF
run_cds --include-merged-prs --threshold 50 --title "Alpha Bravo Charlie Delta"
assert_eq "1" "$RC" "(z3) Matches in all three pools (no rate limiting) -> exit 1"
assert_contains "$OUT" "#801: Alpha Bravo Charlie Delta (similarity: 100%)" "(z3) Open-issue match occupies its own line"
assert_contains "$OUT" "PR #901: Alpha Bravo Charlie Delta (similarity: 100%)" "(z3) Merged-PR match occupies its own line"
assert_contains "$OUT" "Closed #1001: Alpha Bravo Charlie Delta (similarity: 100%)" "(z3) Closed-issue match occupies its own line"
assert_not_contains "$OUT" "100%)PR #901" "(z3) Open-issue and merged-PR lines are not spliced onto one line"

run_cds --include-merged-prs --threshold 50 --title "Alpha Bravo Charlie Delta" --json
assert_eq "3" "$(echo "$OUT" | jq -r '.matches | length')" "(z3) --json parses all three pool matches separately"
assert_eq "801 901 1001" "$(echo "$OUT" | jq -r '[.matches[].number] | join(" ")')" "(z3) --json recovers every match number, none swallowed by a splice"
assert_eq "issue pr closed_issue" "$(echo "$OUT" | jq -r '[.matches[].type] | join(" ")')" "(z3) --json assigns each match its correct pool type"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
