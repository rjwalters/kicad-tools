#!/usr/bin/env bash
# test-sweep-lease-fence.sh - Unit tests for sweep-lease-fence.sh (#6309,
# Epic #6165 Phase 3: sweep-side fencing before push / PR-open).
#
# Black-box tests: sweep-lease-fence.sh is a full CLI script (no functions to
# source), so `gh` is stubbed on PATH (real `jq` is used unstubbed to apply
# the script's own `--jq` filter against a canned comments fixture, exactly
# the way real `gh api --jq` would) and the real script is invoked as a
# subprocess, asserting on stdout/stderr/exit code. Mirrors the stubbing
# pattern in test-sweep-lease-renew.sh (#6180).
#
# Covers:
#   (a) fresh lease owned by this sweep's own host -> PASS (exit 0)
#   (b) expired lease, owned by THIS sweep's own host -> ABORT EXPIRED (exit 3)
#   (c) fresh lease owned by a DIFFERENT host -> ABORT SUPERSEDED (exit 4)
#   (r) expired lease owned by a DIFFERENT host (an abandoned, never-yielded
#       peer lease, #6783) -> PASS, not ABORT (exit 0) -- distinguishes this
#       from (b): only an EXPIRED lease that is also THIS sweep's OWN aborts
#   (d) no lease comment at all -> PASS, fail-open (exit 0)
#   (e) a lease comment whose marker fails to parse -> PASS, fail-open
#   (f) a `gh api` fetch failure -> PASS, fail-open
#   (g) TTL boundary: age exactly == ttl -> PASS (not-yet-expired, matches
#       lease_is_fresh's `age_minutes < ttl_minutes` strict-inequality
#       contract in claim_reconciliation.rs)
#   (h) picks the FRESHEST among multiple lease comments, not merely the
#       first or last in fixture order
#   (i) --host defaults to LOOM_HOST_ID's raw value when
#       LOOM_LEASE_PUBLISH_HOSTNAME opts into raw publishing (#6322)
#   (j) --ttl-minutes overrides the default TTL
#   (k) usage errors: bad issue number, unknown flag, non-numeric
#       --ttl-minutes, unknown command
#   (l) --help / bogus-command contract checks
#   (m) default (no opt-in) --host resolution is the OPAQUE id of
#       LOOM_HOST_ID, not the raw value (#6322)
#   (n) a lease comment carrying the raw hostname no longer matches the
#       opaque default resolution -> ABORT SUPERSEDED (#6322)
#   (o) a yielded (host, sweep)'s lease is excluded from "freshest" and the
#       check falls back to the next-freshest, non-yielded lease (#6485)
#   (p) every lease comment on the issue has yielded -> PASS, fail-open
#   (q) a yield record for a DIFFERENT sweep on the SAME host does not
#       exclude a later, legitimately-reclaimed lease from that host --
#       matched by exact (host, sweep), not by host alone (#6485)
#
# Usage:
#   ./.loom/scripts/tests/test-sweep-lease-fence.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SCRIPT="$SCRIPTS_DIR/sweep-lease-fence.sh"

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
    if printf '%s' "$haystack" | grep -F -- "$needle" >/dev/null; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

assert_true() {
    local cond="$1" msg="$2"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$cond" == "true" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
    fi
}

if [[ ! -x "$SCRIPT" ]]; then
    echo -e "${RED}FATAL${NC}: $SCRIPT not found or not executable" >&2
    exit 2
fi

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

# --- Stub gh on PATH ---------------------------------------------------
#   gh api [-R repo] repos/{owner}/{repo}/issues/<N>/comments --paginate --jq FILTER
#       -> apply the REAL `jq` (compact, one value per line -- exactly what
#          `gh api --jq` emits) to $STUB_DIR/comments.json (or "[]"), so the
#          script's own filter string is genuinely exercised, not a
#          hand-picked stand-in. Fails if $STUB_DIR/comments-fail exists.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
D="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
if [[ "$1" == "api" ]]; then
  shift
  path=""
  filter=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --jq) filter="$2"; shift 2 ;;
      -R) shift 2 ;;
      --paginate) shift ;;
      *)
        if [[ -z "$path" ]]; then path="$1"; fi
        shift
        ;;
    esac
  done
  if [[ "$path" == repos/*/issues/*/comments ]]; then
    if [[ -f "$D/comments-fail" ]]; then
      echo "stub gh: comments fetch failed" >&2
      exit 1
    fi
    canned="$D/comments.json"
    if [[ ! -f "$canned" ]]; then canned="$D/empty.json"; echo "[]" > "$canned"; fi
    jq -c "$filter" "$canned"
    exit 0
  fi
  echo "stub gh: unhandled api args: path=$path" >&2
  exit 3
fi
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

reset_state() {
    rm -f "$STUB_DIR"/comments.json "$STUB_DIR"/comments-fail
    unset LOOM_LEASE_FENCE_NOW LOOM_HOST_ID LOOM_LEASE_TTL_MINUTES HOSTNAME \
        LOOM_LEASE_PUBLISH_HOSTNAME 2> /dev/null || true
}

run_script() {
    OUT="$("$SCRIPT" "$@" 2>"$STUB_DIR/stderr.log")"
    RC=$?
    ERR="$(cat "$STUB_DIR/stderr.log" 2>/dev/null || true)"
}

# A fixed "now" -- 2026-08-15T15:52:00Z -- used across every scenario so age
# math is deterministic without depending on the real wall clock.
NOW_EPOCH="$(date -u -d "2026-08-15T15:52:00Z" +%s 2>/dev/null || date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "2026-08-15T15:52:00Z" +%s)"

echo "Testing sweep-lease-fence.sh..."

# --- (a) fresh lease owned by this sweep's own host -> PASS ---------------
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 1, "updated_at": "2026-08-15T15:50:00Z", "body": "<!-- loom:lease host=studio-host sweep=sweep-a -->\nprose"}
]
JSON
LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309 --host studio-host
assert_eq "0" "$RC" "(a) fresh lease owned by this host -> exit 0 (PASS)"
assert_eq "" "$OUT" "(a) check prints nothing to stdout (all diagnostics go to stderr)"
assert_contains "$ERR" "lease fence OK" "(a) stderr explains the pass"
assert_contains "$ERR" "host=studio-host" "(a) stderr names the matching host"

# --- (b) expired lease -> ABORT EXPIRED (exit 3) ---------------------------
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 1, "updated_at": "2026-08-15T15:30:00Z", "body": "<!-- loom:lease host=studio-host sweep=sweep-a -->\nprose"}
]
JSON
LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309 --host studio-host
assert_eq "3" "$RC" "(b) expired lease, owned by THIS sweep's own host -> exit 3 (ABORT EXPIRED)"
assert_contains "$ERR" "ABORT: EXPIRED" "(b) stderr distinguishes EXPIRED from SUPERSEDED"
assert_contains "$ERR" "2026-08-15T15:30:00Z" "(b) stderr includes the observed lease timestamp"

# --- (r) expired lease owned by a DIFFERENT, now-abandoned host -> PASS,
# NOT ABORT (#6783). Reproduces the #6694/PR #6773 incident shape: the
# freshest (and only) lease record belongs to a different, dead host and is
# well past the TTL -- with no live competitor, this must no longer
# permanently fence out this (successor) sweep's push/PR-open.
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 1, "updated_at": "2026-08-15T15:30:00Z", "body": "<!-- loom:lease host=dead-host sweep=sweep-issue-6694-1787458405 -->\nprose"}
]
JSON
LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309 --host studio-host
assert_eq "0" "$RC" "(r) expired lease owned by a DIFFERENT host -> exit 0 (PASS, not ABORT -- #6783)"
assert_contains "$ERR" "PASS" "(r) stderr reports a PASS, not an ABORT"
assert_contains "$ERR" "EXPIRED" "(r) stderr still names the EXPIRED condition it is passing through"
assert_contains "$ERR" "dead-host" "(r) stderr names the abandoned lease's owning host"
assert_contains "$ERR" "DIFFERENT host" "(r) stderr explains why this expired lease is not fenced against"

# --- (c) fresh lease owned by a DIFFERENT host -> ABORT SUPERSEDED --------
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 1, "updated_at": "2026-08-15T15:51:00Z", "body": "<!-- loom:lease host=other-host sweep=sweep-b -->\nprose"}
]
JSON
LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309 --host studio-host
assert_eq "4" "$RC" "(c) fresh lease held by a different host -> exit 4 (ABORT SUPERSEDED)"
assert_contains "$ERR" "ABORT: SUPERSEDED" "(c) stderr distinguishes SUPERSEDED from EXPIRED"
assert_contains "$ERR" "other-host" "(c) stderr names the superseding host"
assert_contains "$ERR" "studio-host" "(c) stderr names this sweep's own host too"

# --- (d) no lease comment at all -> PASS, fail-open ------------------------
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[{"id": 1, "updated_at": "2026-08-15T15:51:00Z", "body": "unrelated comment, no marker here"}]
JSON
LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309 --host studio-host
assert_eq "0" "$RC" "(d) no lease comment found -> exit 0 (fail-open)"
assert_contains "$ERR" "no lease comment found" "(d) stderr explains the fail-open reason"

# --- (e) a lease comment whose marker fails to parse -> PASS, fail-open ---
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[{"id": 1, "updated_at": "2026-08-15T15:51:00Z", "body": "<!-- loom:lease host=studio-host missing-sweep-field -->\nprose"}]
JSON
LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309 --host studio-host
assert_eq "0" "$RC" "(e) malformed lease marker -> exit 0 (fail-open)"
assert_contains "$ERR" "malformed marker" "(e) stderr explains the malformed-marker fail-open reason"

# --- (f) a `gh api` fetch failure -> PASS, fail-open -----------------------
reset_state
touch "$STUB_DIR/comments-fail"
LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309 --host studio-host
assert_eq "0" "$RC" "(f) gh fetch failure -> exit 0 (fail-open)"
assert_contains "$ERR" "could not fetch comments" "(f) stderr explains the fetch-failure fail-open reason"

# --- (g) TTL boundary: age exactly == ttl -> PASS (strict '>' only aborts) -
reset_state
# 15 minutes before NOW_EPOCH, exactly at the default 15-minute TTL boundary.
BOUNDARY_TS="2026-08-15T15:37:00Z"
cat > "$STUB_DIR/comments.json" <<JSON
[{"id": 1, "updated_at": "$BOUNDARY_TS", "body": "<!-- loom:lease host=studio-host sweep=sweep-a -->\nprose"}]
JSON
LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309 --host studio-host --ttl-minutes 15
assert_eq "0" "$RC" "(g) age exactly at the TTL boundary -> exit 0 (not yet expired)"

# --- (h) picks the FRESHEST among multiple lease comments ------------------
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 1, "updated_at": "2026-08-15T15:00:00Z", "body": "<!-- loom:lease host=old-host sweep=sweep-old -->\nprose"},
  {"id": 2, "updated_at": "2026-08-15T15:51:30Z", "body": "<!-- loom:lease host=studio-host sweep=sweep-new -->\nprose"}
]
JSON
LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309 --host studio-host
assert_eq "0" "$RC" "(h) freshest (not first/last-in-fixture-order) lease decides the outcome -> exit 0"
assert_contains "$ERR" "sweep-new" "(h) stderr names the freshest lease's sweep id, not the stale one"

# --- (i) --host defaults to LOOM_HOST_ID's raw value under the raw opt-in -
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[{"id": 1, "updated_at": "2026-08-15T15:51:00Z", "body": "<!-- loom:lease host=env-default-host sweep=sweep-a -->\nprose"}]
JSON
LOOM_HOST_ID="env-default-host" LOOM_LEASE_PUBLISH_HOSTNAME="1" LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309
assert_eq "0" "$RC" "(i) --host omitted resolves from LOOM_HOST_ID raw, under the opt-in, and matches -> exit 0"

# --- (m) default (no opt-in) --host resolution is the OPAQUE id of
# LOOM_HOST_ID, not the raw value (Issue #6322). `host-60a4fb97` is the
# pinned first-8-hex-chars-of-sha256("loom-lease-host-id-v1:opaque-test-host")
# result -- mirrors `sweep_registry::opaque_host_id`'s exact algorithm
# (Rust-side unit coverage: `opaque_host_id_has_the_expected_shape` et al in
# `loom-daemon/src/sweep_registry/mod.rs`).
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[{"id": 1, "updated_at": "2026-08-15T15:51:00Z", "body": "<!-- loom:lease host=host-60a4fb97 sweep=sweep-a -->\nprose"}]
JSON
LOOM_HOST_ID="opaque-test-host" LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309
assert_eq "0" "$RC" "(m) --host omitted defaults to the opaque id and matches the opaque lease -> exit 0"

# --- (n) a lease comment carrying the RAW hostname no longer matches the
# opaque default resolution -- the pre-#6322 shape is now treated as a
# different (peer) host, not a match.
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[{"id": 1, "updated_at": "2026-08-15T15:51:00Z", "body": "<!-- loom:lease host=opaque-test-host sweep=sweep-a -->\nprose"}]
JSON
LOOM_HOST_ID="opaque-test-host" LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309
assert_eq "4" "$RC" "(n) a raw-hostname lease comment does not match the opaque default -> ABORT SUPERSEDED"

# --- (o) a lease excluded by a same (host, sweep) loom:lease-yield record
# (#6485) falls back to the next-freshest, non-yielded candidate -- here
# that candidate is this sweep's OWN, older lease, so the check still
# PASSES even though the freshest comment overall belongs to a peer.
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 1, "updated_at": "2026-08-15T15:50:00Z", "body": "<!-- loom:lease host=studio-host sweep=sweep-own -->\nprose"},
  {"id": 2, "updated_at": "2026-08-15T15:51:30Z", "body": "<!-- loom:lease host=peer-host sweep=sweep-peer -->\nprose"},
  {"id": 3, "updated_at": "2026-08-15T15:51:00Z", "body": "<!-- loom:lease-yield host=peer-host sweep=sweep-peer earliest_host=studio-host earliest_sweep=sweep-own -->\nprose"}
]
JSON
LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309 --host studio-host
assert_eq "0" "$RC" "(o) the freshest lease belongs to a yielded (host, sweep) -- excluded, falls back to this host's own older lease -> PASS"
assert_contains "$ERR" "host=studio-host" "(o) stderr confirms the match landed on studio-host's own lease"

# --- (p) EVERY lease comment on the issue has yielded -> PASS, fail-open
# (no non-yielded evidence to fence against at all).
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 1, "updated_at": "2026-08-15T15:51:00Z", "body": "<!-- loom:lease host=studio-host sweep=sweep-a -->\nprose"},
  {"id": 2, "updated_at": "2026-08-15T15:51:30Z", "body": "<!-- loom:lease-yield host=studio-host sweep=sweep-a earliest_host=peer-host earliest_sweep=sweep-b -->\nprose"}
]
JSON
LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309 --host studio-host
assert_eq "0" "$RC" "(p) every lease comment has yielded -> PASS, fail-open"
assert_contains "$ERR" "loom:lease-yield" "(p) stderr explains the all-yielded fail-open reason"

# --- (q) a yield record for a DIFFERENT sweep on the SAME host does NOT
# exclude a later, legitimately-reclaimed lease from that host -- matched
# by exact (host, sweep) pair, not by host alone.
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 1, "updated_at": "2026-08-15T15:40:00Z", "body": "<!-- loom:lease host=studio-host sweep=sweep-old -->\nprose"},
  {"id": 2, "updated_at": "2026-08-15T15:40:30Z", "body": "<!-- loom:lease-yield host=studio-host sweep=sweep-old earliest_host=peer-host earliest_sweep=sweep-peer-old -->\nprose"},
  {"id": 3, "updated_at": "2026-08-15T15:51:00Z", "body": "<!-- loom:lease host=studio-host sweep=sweep-new -->\nprose"}
]
JSON
LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309 --host studio-host
assert_eq "0" "$RC" "(q) a stale yield for a DIFFERENT (older) sweep on the same host does not block this host's brand new lease"
assert_contains "$ERR" "sweep=sweep-new" "(q) stderr confirms the match is the new sweep's lease, not the old yielded one"

# --- (j) --ttl-minutes overrides the default TTL ---------------------------
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[{"id": 1, "updated_at": "2026-08-15T15:30:00Z", "body": "<!-- loom:lease host=studio-host sweep=sweep-a -->\nprose"}]
JSON
# 22 minutes old: fresh under a 30-minute TTL, expired under the 15-minute default.
LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309 --host studio-host --ttl-minutes 30
assert_eq "0" "$RC" "(j) --ttl-minutes 30 keeps a 22-minute-old lease fresh -> exit 0"
LOOM_LEASE_FENCE_NOW="$NOW_EPOCH" run_script check 6309 --host studio-host --ttl-minutes 15
assert_eq "3" "$RC" "(j) the same lease is expired under the default 15-minute TTL -> exit 3"

# --- (k) usage errors -------------------------------------------------------
reset_state
run_script check not-a-number
assert_eq "1" "$RC" "(k) non-numeric issue number -> exit 1 (usage error)"
run_script check 6309 --bogus-flag
assert_eq "1" "$RC" "(k) unknown flag -> exit 1 (usage error)"
run_script check 6309 --ttl-minutes not-a-number
assert_eq "1" "$RC" "(k) non-numeric --ttl-minutes -> exit 1 (usage error)"
run_script bogus-command 6309
assert_true "$([[ "$RC" -ne 0 ]] && echo true || echo false)" "(k) unknown command exits non-zero"

# --- (l) --help / bogus-command contract checks (mirrors
#     test-sweep-lease-renew.sh's own contract-check style) ----------------
"$SCRIPT" --help > "$STUB_DIR/help.out" 2>&1
HELP_RC=$?
assert_true "$([[ -s "$STUB_DIR/help.out" ]] && echo true || echo false)" "(l) --help prints usage text"
assert_eq "1" "$HELP_RC" "(l) --help exits 1 (usage-exit convention, matches sweep-lease-renew.sh)"

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if ((TESTS_FAILED > 0)); then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}ALL PASSED${NC}"
exit 0
