#!/usr/bin/env bash
# test-guide-docs-pr-race.sh - Regression suite: does the existing
# single-writer discipline for Guide's Document Maintenance phase (Step 1's
# open-docs-PR check + Step 5's uncached OPEN_DOCS_PR_RECHECK, #5573/#5615)
# actually bound a multi-host race on the SAME debounce-eligible delta to
# exactly one `gh pr create` (Issue #6327), AND does it survive GitHub
# search-index propagation lag (Issue #7354 -- see Scenario 5 below).
#
# ## Why this suite exists
#
# Issue #6327 was filed observing N-host duplicate `docs: Guide document
# maintenance update` PRs and initially asked for a generic lease/claim
# primitive for this phase -- "the way sweeps now have it" (#6165). Live
# forge-history verification (see the issue's "Verified corrections" +
# "Curator Enhancement" sections) found that premise incomplete: the
# lock+recheck combination this suite exercises was ALREADY implemented
# (`docs-guide-lock.sh`, #5573, same-host mkdir lock; the Step 5 uncached
# recheck, #5615, cross-host TOCTOU narrowing) before this issue was ever
# filed. What was missing was a REGRESSION TEST that actually simulates the
# interleaving and proves the combination holds, rather than asserting it by
# code-inspection/comments alone -- exactly the gap
# `test-sweep-lease-fence-race.sh` (#6315) closed for the analogous
# sweep-side lease fencing check. This suite mirrors that test's shape:
# extract the REAL command lines from the prompt, run them against a shared
# stubbed forge state across a sequence of simulated host attempts, and
# assert the aggregate `gh pr create` count.
#
# ## Why this is a separate suite from test-docs-guide-lock.sh
#
# `test-docs-guide-lock.sh` already unit-tests `docs-guide-lock.sh` itself
# (acquire/release/staleness reaping) in isolation. That is necessary but not
# sufficient: `docs-guide-lock.sh` is explicitly SAME-HOST ONLY (see its own
# header comment and guide.md's Step 1 "#5615 GAP" note) -- it can never, by
# itself, prove anything about a CROSS-HOST race, which is exactly the shape
# #6327 was filed against. This suite does not re-test the lock; it tests
# the Step 1 check + Step 5 recheck PAIR that guide.md documents as the
# cross-host mitigation, using a harness that models two-or-more INDEPENDENT
# hosts (no shared lock file) contending for the same forge state.
#
# ## Harness shape
#
# A fake shared "forge" is a JSON array of open docs-maintenance PRs in a
# temp file. `simulate_tick <host>` runs the EXACT Step 1 line (extracted
# verbatim from guide.md, using `$GH_READ`) and, if it finds no open PR, the
# EXACT Step 5 recheck line (also extracted verbatim, using bare `gh` per
# the #5615 "deliberately uncached" requirement) against that shared store.
# Only if BOTH checks come back empty does the simulated tick "create" a PR
# -- appending an entry to the shared store and to `created.log`. Ticks are
# invoked in an explicit, documented order that models a specific timeline
# of forge-visible events (identical technique to
# `test-sweep-lease-fence-race.sh`'s `simulate_sweep_attempt` sequencing --
# this is not real OS-level concurrency, it is a deterministic reconstruction
# of the sequence of forge reads/writes that a genuine race produces).
#
# ## Scope (read before assuming this "proves no race is possible")
#
# This suite proves the REALISTIC race shape guide.md's own Step 1 comment
# describes as what the recheck narrows the window DOWN TO: two-or-more hosts
# that both pass Step 1 near-simultaneously (both see "no open PR"), but
# whose Step 5 rechecks are NOT the same forge read -- i.e. the winner's
# `gh pr create` has already landed on the forge by the time a loser's
# recheck runs. Every Guide tick does substantial work (rendering doc bodies,
# git operations) between Step 1 and Step 5, so in practice two hosts'
# Step-5 moments are separated by real wall-clock time even when their
# Step-1 checks were nearly simultaneous -- this is the scenario the #5615
# fix actually targets, and the one the issue's own "Verified corrections"
# analysis found no counter-evidence against in live forge history.
# It deliberately does NOT claim to prove atomicity for the case where two
# hosts' Step 5 rechecks both read the SAME pre-create forge state (an
# exact simultaneous double-recheck) -- guide.md's own Step 1 comment
# already documents that residual gap ("narrowing... not a hard guarantee").
# Closing that residual sliver, if it is ever observed live, is exactly the
# "the new regression test finds the existing lock+recheck insufficient"
# trigger condition #6327 names for building a real lease primitive --
# which this suite does not attempt, per the issue's explicit instruction
# not to build one preemptively.
#
# ## Scenario 5 (#7354): search-index propagation lag
#
# #7352/#7353 recurred the exact race Scenarios 2-4 prove is closed --
# despite the lock+recheck combination being present and correct by every
# check above. Root cause: the pre-#7354 Step 1/Step 5 lines queried GitHub's
# search/issues index (`--search "head:docs/guide-update"`), a SEPARATE,
# eventually-consistent store from the primary Pulls List API `gh pr create`
# writes to. "Uncached" (bare `gh`, no `gh-cached` TTL) only guarantees a
# fresh read of the local HTTP cache -- it says nothing about how fresh
# GitHub's own search index is relative to the Pulls List API, and that index
# can lag by minutes. Scenario 5 models this with a second, independently-
# synced "search index" store and shows: (a) the OLD `--search`-based line
# reproduces the #7352/#7353 duplicate-create when the search index hasn't
# caught up, (b) the FIX -- a plain `pr list --state open` read filtered
# client-side on `headRefName`, which never touches the search index -- does
# not, and (c) the OLD line was not wrong in general, only late (it does find
# the PR once the search index is synced), confirming this is a timing bug in
# the query shape rather than a logic bug in the recheck itself.
#
# Usage:
#   ./.loom/scripts/tests/test-guide-docs-pr-race.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# guide.md is shipped (installed at .claude/commands/loom/guide.md), so
# resolve it the way each layout actually lays it out: the installed path
# first (consumer repos, and Loom's own dogfooded checkout), falling back
# to the defaults/ source-tree path (a bare source checkout with no
# .claude/commands/loom/ copy yet). See issue #6194 / #6241.
if [[ -f "$REPO_ROOT/.claude/commands/loom/guide.md" ]]; then
    GUIDE_MD="$REPO_ROOT/.claude/commands/loom/guide.md"
else
    GUIDE_MD="$REPO_ROOT/defaults/.claude/commands/loom/guide.md"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    local actual="$1" expected="$2" msg="$3"
    if [[ "$actual" == "$expected" ]]; then pass "$msg"; else fail "$msg (got '$actual', expected '$expected')"; fi
}

assert_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then pass "$msg"; else fail "$msg (missing pattern: $pattern)"; fi
}

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    echo -e "${RED}FATAL${NC}: jq is required for this suite"
    exit 2
fi

# ---------------------------------------------------------------------------
# Test 1: the two guard lines exist as documented (Step 1 cached-OK read,
# Step 5 deliberately-uncached read) -- sanity check before we extract and
# execute them below.
# ---------------------------------------------------------------------------
echo "Test 1: guide.md defines the Step 1 check and Step 5 uncached recheck"

assert_grep 'OPEN_DOCS_PR=\$\("\$GH_READ" pr list --state open --limit 100 --json number,headRefName' "$GUIDE_MD" \
    "Step 1's open-docs-PR check uses \$GH_READ (may be cached) against the plain Pulls List API"
assert_grep 'OPEN_DOCS_PR_RECHECK=\$\(gh pr list --state open --limit 100 --json number,headRefName' "$GUIDE_MD" \
    "Step 5's recheck uses bare gh (deliberately uncached, #5615) against the plain Pulls List API"
assert_grep '#6327 CORRECTED UNDERSTANDING' "$GUIDE_MD" \
    "guide.md documents the #6327 corrected understanding near the Step 1 lock/recheck block"
# #7354: neither guard line may use `--search` any more -- that routes through
# GitHub's eventually-consistent search index, which is what actually caused
# #7352/#7353 (see Scenario 5 below), not a cache/staleness bug in gh itself.
if grep -qF 'pr list --state open --search "head:docs/guide-update"' "$GUIDE_MD"; then
    fail "(1) neither Step 1 nor Step 5 guard line uses --search (#7354 -- search index lag, not staleness)"
else
    pass "(1) neither Step 1 nor Step 5 guard line uses --search (#7354 -- search index lag, not staleness)"
fi

# ---------------------------------------------------------------------------
# Extract the two guard lines VERBATIM so this suite can never silently drift
# from the actual prompt text (mirrors test-guide-work-log-debounce.sh's
# JQ_EXPR extraction style).
# ---------------------------------------------------------------------------
STEP1_LINE="$(grep -m1 '^OPEN_DOCS_PR=\$("\$GH_READ" pr list' "$GUIDE_MD")"
STEP5_LINE="$(grep -m1 '^  OPEN_DOCS_PR_RECHECK=\$(gh pr list' "$GUIDE_MD")"

if [[ -z "$STEP1_LINE" || -z "$STEP5_LINE" ]]; then
    echo -e "${RED}FATAL${NC}: could not extract Step 1 / Step 5 guard lines from guide.md"
    exit 2
fi

# The PRE-#7354 Step 5 line, hardcoded (guide.md no longer contains it) so
# Scenario 5 can demonstrate the bug it caused and confirm the fix actually
# closes that specific gap rather than just asserting the new line exists.
OLD_STEP5_LINE='OPEN_DOCS_PR_RECHECK=$(gh pr list --state open --search "head:docs/guide-update" --json number --jq '\''.[0].number // empty'\'')'

# ---------------------------------------------------------------------------
# Harness: a shared fake-forge store of open docs-maintenance PRs, and a stub
# `gh` that answers exactly the query shape both guard lines issue.
# ---------------------------------------------------------------------------
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

STORE="$STUB_DIR/open-prs.json"
CREATED_LOG="$STUB_DIR/created.log"
EVENT_LOG="$STUB_DIR/events.log"

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Stub handles two query shapes against two independent fake-forge JSON
# arrays of open docs PRs (each {"number": N, "headRefName": "docs/..."}):
#   1. `pr list --state open [--limit N] --json number,headRefName --jq
#      FILTER` (the #7354-fixed Step 1/Step 5 lines) -- reads $LOOM_TEST_STORE,
#      which every `simulate_tick`/scenario updates the instant a PR is
#      "created". This is the real, non-search Pulls List API: always fresh.
#   2. `pr list --state open --search "..." --json number --jq FILTER` (the
#      PRE-#7354 line, kept only for Scenario 5's bug-reproduction) -- reads
#      $LOOM_TEST_SEARCH_STORE if set, else falls back to $LOOM_TEST_STORE.
#      Scenario 5 updates this copy on its own schedule to model GitHub's
#      search/issues index lagging behind the primary Pulls List API.
STORE="${LOOM_TEST_STORE:?stub gh: LOOM_TEST_STORE not set}"
SEARCH_STORE="${LOOM_TEST_SEARCH_STORE:-$STORE}"
if [[ "$1" == "pr" && "$2" == "list" ]]; then
  shift 2
  filter=""
  is_search=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --search) is_search=1; shift 2 ;;
      --jq) filter="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  if [[ "$is_search" == "1" ]]; then
    jq -c "$filter" "$SEARCH_STORE"
  else
    jq -c "$filter" "$STORE"
  fi
  exit 0
fi
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_STORE="$STORE"
export PATH="$STUB_DIR:$PATH"
export GH_READ="gh"   # matches guide.md's fallback when gh-cached is absent

reset_state() {
    echo '[]' > "$STORE"
    : > "$CREATED_LOG"
    : > "$EVENT_LOG"
    unset LOOM_TEST_SEARCH_STORE
}

next_pr_number() {
    # 1000 + however many PRs (open or historically created) already exist,
    # so numbers never collide across a scenario's whole timeline.
    local n
    n=$(( 1000 + $(wc -l < "$CREATED_LOG" | tr -d '[:space:]') ))
    echo "$n"
}

# create_pr_entry <n> -- appends {"number": n, "headRefName":
# "docs/guide-update-<n>"} to $STORE. Centralized so every scenario's "a PR
# just got created" step produces an entry shaped the way BOTH the #7354 fix
# (needs .headRefName) and the pre-#7354 line (needs only .number, since its
# store was implicitly pre-filtered to docs PRs) can read.
create_pr_entry() {
    local n="$1"
    jq --argjson n "$n" --arg h "docs/guide-update-$n" \
        '. + [{"number": $n, "headRefName": $h}]' "$STORE" > "$STORE.tmp" && mv "$STORE.tmp" "$STORE"
}

# simulate_tick <host>
#
# Runs the REAL Step 1 line, then (only if it found nothing) the REAL Step 5
# line, both eval'd verbatim against the shared stub. On a genuine pass of
# BOTH checks, "creates" a PR: appends it to the shared store (making it
# visible to every subsequent simulate_tick call) and to created.log (the
# side effect this suite counts). Logs which guard (if any) caused a skip.
simulate_tick() {
    local host="$1"
    local OPEN_DOCS_PR="" OPEN_DOCS_PR_RECHECK=""

    eval "$STEP1_LINE"
    if [[ -n "$OPEN_DOCS_PR" ]]; then
        echo "$host: Step 1 found open PR #$OPEN_DOCS_PR -- skip" >> "$EVENT_LOG"
        return
    fi

    eval "$STEP5_LINE"
    if [[ -n "$OPEN_DOCS_PR_RECHECK" ]]; then
        echo "$host: Step 5 recheck found open PR #$OPEN_DOCS_PR_RECHECK -- discard local commit, skip" >> "$EVENT_LOG"
        return
    fi

    local n
    n="$(next_pr_number)"
    create_pr_entry "$n"
    echo "$host" >> "$CREATED_LOG"
    echo "$host: Step 1 and Step 5 both empty -- created PR #$n" >> "$EVENT_LOG"
}

created_count() { wc -l < "$CREATED_LOG" | tr -d '[:space:]'; }
created_contents() { cat "$CREATED_LOG"; }

# ============================================================================
# Scenario 1: sequential (non-racing) ticks -- host B's tick starts only
# after host A's whole tick (including its Step 1 check through PR creation)
# has already landed on the forge. This is the common case (staggered role-
# runner cadence) and the cheapest one for Step 1 alone to handle -- Step 5
# never even needs to fire for host B.
# ============================================================================
echo ""
echo "--- Scenario 1: sequential ticks -- Step 1 alone is enough ---"
reset_state
simulate_tick host-A
simulate_tick host-B
assert_eq "1" "$(created_count)" "(1) exactly one PR created across two sequential (non-racing) ticks"
assert_eq "host-A" "$(created_contents)" "(1) the first host's tick is the one that created the PR"
assert_grep "host-B: Step 1 found open PR" "$EVENT_LOG" \
    "(1) the second host's Step 1 check alone caught the already-open PR (Step 5 never needed to fire)"

# ============================================================================
# Scenario 2: genuine two-host race -- both hosts pass Step 1 while the
# store is still empty (interleaved starts), but host A reaches Step 5 (and
# creates) BEFORE host B's own Step 5 recheck runs -- the realistic shape
# the #5615 fix targets (near-simultaneous starts, staggered finishes, see
# "Scope" above). Modeled by NOT resetting the store between host A's full
# tick and host B's Step-1-already-passed continuation.
# ============================================================================
echo ""
echo "--- Scenario 2: interleaved starts, staggered finishes -- host A wins ---"
reset_state

# Both hosts' Step 1 checks run back-to-back while the store is still empty
# (this models the near-simultaneous start -- store genuinely has nothing
# open yet for EITHER read).
OPEN_DOCS_PR=""; eval "$STEP1_LINE"; STEP1_A="$OPEN_DOCS_PR"
OPEN_DOCS_PR=""; eval "$STEP1_LINE"; STEP1_B="$OPEN_DOCS_PR"
assert_eq "" "$STEP1_A" "(2) host A's Step 1 check finds nothing (store still empty at start)"
assert_eq "" "$STEP1_B" "(2) host B's Step 1 check ALSO finds nothing (genuine interleaved start, not sequential)"

# Host A now completes the rest of its tick first: Step 5 recheck (still
# empty) + create.
OPEN_DOCS_PR_RECHECK=""; eval "$STEP5_LINE"
assert_eq "" "$OPEN_DOCS_PR_RECHECK" "(2) host A's Step 5 recheck also finds nothing -- proceeds to create"
n="$(next_pr_number)"; create_pr_entry "$n"
echo "host-A" >> "$CREATED_LOG"
echo "host-A: Step 1 and Step 5 both empty -- created PR #$n" >> "$EVENT_LOG"

# Host B's Step 5 recheck runs AFTER host A's create has landed on the
# (shared) store -- this is the #5615 mitigation actually firing.
OPEN_DOCS_PR_RECHECK=""; eval "$STEP5_LINE"
assert_eq "$n" "$OPEN_DOCS_PR_RECHECK" \
    "(2) host B's Step 5 recheck now sees host A's just-created PR (the uncached #5615 recheck catching the race)"

assert_eq "1" "$(created_count)" "(2) exactly one PR created across the whole race (not zero, not two)"
assert_eq "host-A" "$(created_contents)" "(2) the surviving create belongs to the winner (host A) only"

# ============================================================================
# Scenario 3: same race, reversed winner -- proves the outcome depends on
# forge-visible ordering, not any host-identity bias baked into the checks.
# ============================================================================
echo ""
echo "--- Scenario 3: interleaved starts, staggered finishes -- host B wins ---"
reset_state

OPEN_DOCS_PR=""; eval "$STEP1_LINE"; STEP1_A="$OPEN_DOCS_PR"
OPEN_DOCS_PR=""; eval "$STEP1_LINE"; STEP1_B="$OPEN_DOCS_PR"
assert_eq "" "$STEP1_A" "(3) host A's Step 1 check finds nothing"
assert_eq "" "$STEP1_B" "(3) host B's Step 1 check also finds nothing"

# This time host B finishes first.
OPEN_DOCS_PR_RECHECK=""; eval "$STEP5_LINE"
n="$(next_pr_number)"; create_pr_entry "$n"
echo "host-B" >> "$CREATED_LOG"
echo "host-B: Step 1 and Step 5 both empty -- created PR #$n" >> "$EVENT_LOG"

OPEN_DOCS_PR_RECHECK=""; eval "$STEP5_LINE"
assert_eq "$n" "$OPEN_DOCS_PR_RECHECK" "(3) host A's Step 5 recheck now sees host B's just-created PR"

assert_eq "1" "$(created_count)" "(3) exactly one PR created (not zero, not two) -- winner determined by finish order, not identity"
assert_eq "host-B" "$(created_contents)" "(3) the surviving create belongs to the winner (host B) only"

# ============================================================================
# Scenario 4: three-host interleave on the same debounce-eligible delta --
# the acceptance criteria's "≥2 dispatchers" case, exercised at N=3. All
# three pass Step 1 while the store is empty; only the first to reach Step 5
# succeeds, the other two's rechecks both catch the winner's PR.
# ============================================================================
echo ""
echo "--- Scenario 4: three-host interleave -- still exactly one winner ---"
reset_state

for h in host-A host-B host-C; do
    OPEN_DOCS_PR=""; eval "$STEP1_LINE"
    if [[ -n "$OPEN_DOCS_PR" ]]; then
        fail "(4) $h's Step 1 check unexpectedly found an open PR before any host has created one"
    else
        pass "(4) $h's Step 1 check finds nothing (store still empty)"
    fi
done

# host-B reaches Step 5 first among the three (arbitrary finish order).
OPEN_DOCS_PR_RECHECK=""; eval "$STEP5_LINE"
n="$(next_pr_number)"; create_pr_entry "$n"
echo "host-B" >> "$CREATED_LOG"
echo "host-B: Step 1 and Step 5 both empty -- created PR #$n" >> "$EVENT_LOG"

# host-A and host-C's Step 5 rechecks both run after host-B's create landed.
for h in host-A host-C; do
    OPEN_DOCS_PR_RECHECK=""; eval "$STEP5_LINE"
    assert_eq "$n" "$OPEN_DOCS_PR_RECHECK" "(4) $h's Step 5 recheck sees host-B's already-created PR and would skip"
done

assert_eq "1" "$(created_count)" "(4) exactly one PR created across a 3-host interleave (not zero, not three)"
assert_eq "host-B" "$(created_contents)" "(4) the surviving create belongs to the single winner (host-B) only"

# ============================================================================
# Scenario 5 (#7354): search-index propagation lag -- the actual #7352/#7353
# shape. Host A creates a PR; the live Pulls List store reflects it
# immediately, but a separate "search index" store (modeling GitHub's
# eventually-consistent search/issues backend) has not been synced yet. The
# PRE-#7354 --search-based recheck (OLD_STEP5_LINE) reads the lagging store
# and comes back empty even though a PR already exists -- reproducing the
# duplicate-create bug. The FIXED recheck (STEP5_LINE) reads the live store
# directly and catches it immediately. Finally, once the search index is
# synced, the OLD line does find the PR too -- confirming this was a timing
# bug in the query shape, not a logic bug in the recheck.
# ============================================================================
echo ""
echo "--- Scenario 5: search-index lag reproduces #7352/#7353 with the OLD --search line; the #7354 fix closes it ---"
reset_state
LOOM_TEST_SEARCH_STORE="$STUB_DIR/search-index.json"
export LOOM_TEST_SEARCH_STORE
echo '[]' > "$LOOM_TEST_SEARCH_STORE"

# Host A: Step 1 empty, Step 5 (fixed line) empty -- proceeds to create. The
# live store is updated immediately; the search-index copy is deliberately
# NOT synced (models real propagation lag).
OPEN_DOCS_PR=""; eval "$STEP1_LINE"
assert_eq "" "$OPEN_DOCS_PR" "(5) host A's Step 1 check finds nothing"
OPEN_DOCS_PR_RECHECK=""; eval "$STEP5_LINE"
assert_eq "" "$OPEN_DOCS_PR_RECHECK" "(5) host A's Step 5 (fixed, non-search) recheck finds nothing -- proceeds to create"
n="$(next_pr_number)"; create_pr_entry "$n"
echo "host-A" >> "$CREATED_LOG"
echo "host-A: created PR #$n (search index deliberately NOT yet synced)" >> "$EVENT_LOG"

# Host B started before host A's create landed (models the ~2m28s
# #7352/#7353 interleave) and is now at its own Step 5 moment. Run the OLD
# (pre-#7354) --search-based recheck at this exact point: it still returns
# EMPTY, because the search index it reads has not caught up -- reproducing
# the bug that shipped #7352/#7353.
OPEN_DOCS_PR_RECHECK=""; eval "$OLD_STEP5_LINE"
assert_eq "" "$OPEN_DOCS_PR_RECHECK" \
    "(5) OLD --search-based recheck returns EMPTY even though host A already created (unsynced search index) -- reproduces #7352/#7353"

# Now run the FIXED (#7354) recheck at the identical point in the timeline:
# it reads the live store directly and finds host A's PR immediately.
OPEN_DOCS_PR_RECHECK=""; eval "$STEP5_LINE"
assert_eq "$n" "$OPEN_DOCS_PR_RECHECK" \
    "(5) FIXED recheck (plain pr list, client-side headRefName filter) finds host A's PR immediately -- #7354 closes the gap"

# Sync the search index (as GitHub eventually would) and confirm the OLD line
# WOULD have caught the PR once it caught up -- proving this is an
# index-propagation timing bug, not a query-logic bug.
cp "$STORE" "$LOOM_TEST_SEARCH_STORE"
OPEN_DOCS_PR_RECHECK=""; eval "$OLD_STEP5_LINE"
assert_eq "$n" "$OPEN_DOCS_PR_RECHECK" \
    "(5) OLD --search-based recheck DOES find the PR once the search index catches up (confirms lag, not a logic bug)"

unset LOOM_TEST_SEARCH_STORE

# ============================================================================
echo ""
echo "================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
if ((TESTS_FAILED > 0)); then
    echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
    exit 1
fi
echo "All tests passed"
exit 0
