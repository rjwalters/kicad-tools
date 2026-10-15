#!/usr/bin/env bash
# test-judge-fallback-cap.sh - Unit tests for judge-fallback-guard.sh (#5455).
#
# judge-fallback-guard.sh moves the Judge's fallback-queue skip/evaluate
# decision out of prompt-embedded bash into a real, testable script. It was
# added after PR #4972 (rjwalters/loom) accumulated 199 fallback-mode Judge
# comments over 37 hours — the existing SHA-based dedup (#5058) is correct in
# spec but expressed only as prose an LLM must faithfully re-derive on every
# pass, and a fleet propagation-lag window let ~21h of comments through even
# after #5058 had merged.
#
# This is a black-box test: judge-fallback-guard.sh is a full CLI script (no
# functions to source), so we stub `gh`/`jq` availability via a real `jq` (jq
# itself is not stubbed — its logic is exactly what's under test) and stub
# `gh` on PATH, then invoke the real script as a subprocess, asserting on
# stdout/exit code. Mirrors the stubbing pattern in test-check-duplicate.sh.
#
# Usage:
#   ./.loom/scripts/tests/test-judge-fallback-cap.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
GUARD="$SCRIPTS_DIR/judge-fallback-guard.sh"

# shellcheck source=lib/assert.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

if [[ ! -x "$GUARD" ]]; then
    echo -e "${RED}FATAL${NC}: $GUARD not found or not executable" >&2
    exit 2
fi

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

# --- Stub gh on PATH ---------------------------------------------------
#   gh api repos/{owner}/{repo}/pulls/<N>   -> cat $STUB_DIR/pr-<N>.json
#                                               (fails if pr-view-fail-<N> exists)
#   gh pr view / gh api graphql             -> ALWAYS fail with the GraphQL
#                                               rate-limit text (#9340: Step 1 is REST-only)
#   gh api repos/{owner}/{repo}/issues/<N>/comments --paginate
#                                            -> cat $STUB_DIR/comments-<N>.json
#                                               (or "[]"; fails if comments-fail-<N> exists)
# Real `jq` is used unstubbed — it is exactly the logic under test.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
if [[ "$1" == "pr" || "$1 $2" == "api graphql" ]]; then
  echo "GraphQL: API rate limit already exceeded for installation ID 1." >&2
  exit 1
fi
if [[ "$1" == "api" && "$2" == repos/*/pulls/* ]]; then
  pr_num="${2##*/}"
  if [[ -f "$STUB_DIR_FROM_ENV/pr-view-fail-$pr_num" ]]; then
    echo "stub gh: pulls fetch failed" >&2
    exit 1
  fi
  # Simulate `gh` emitting incidental content to stderr on a SUCCESSFUL
  # call (update-notifier banner, rate-limit hint, proxy/TLS warning) —
  # the guard must parse only stdout, never merge this into the JSON.
  if [[ -f "$STUB_DIR_FROM_ENV/pr-view-stderr-$pr_num" ]]; then
    echo "gh: A new release of gh is available: 2.0.0 -> 2.1.0" >&2
  fi
  canned="$STUB_DIR_FROM_ENV/pr-$pr_num.json"
  if [[ -f "$canned" ]]; then cat "$canned"; else echo '{"user":{"type":"User"},"head":{"sha":"0000000000000000000000000000000000000000"}}'; fi
  exit 0
fi
case "$1" in
  api)
    path="$2"
    if [[ "$path" == repos/*/issues/*/comments ]]; then
      num="${path#repos/*/issues/}"
      num="${num%/comments}"
      if [[ -f "$STUB_DIR_FROM_ENV/comments-fail-$num" ]]; then
        echo "stub gh: comments fetch failed" >&2
        exit 1
      fi
      # Same as above: benign stderr chatter on a SUCCESSFUL comments fetch.
      if [[ -f "$STUB_DIR_FROM_ENV/comments-stderr-$num" ]]; then
        echo "gh: request took longer than expected" >&2
      fi
      canned="$STUB_DIR_FROM_ENV/comments-$num.json"
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

# #9537: the guard asks `loom-daemon forge is-fleet`. Pin the daemon to stubs
# so the result never depends on whichever binary this machine has installed.
# Default: an OLD daemon without the is-fleet verb (clap-style exit 2), which
# makes the guard use its built-in default-family fallback — but it DOES
# answer `forge trusted-comments` (#9548/#9716), filtering stdin the same way
# the real predicate does for these fixtures' shapes: trusted iff
# author_association/authorAssociation is an insider one, or the author is
# App-spelled (`x[bot]` / `app/x`) and names the default fleet family. This
# mirrors test-classify-ac-verification.sh's stub daemon.
TRUSTED_COMMENTS_FILTER='
  def who: (.user // .author // {});
  def assoc: ((.author_association // .authorAssociation // "") | ascii_upcase);
  def lg: (who | .login // "");
  def norm: (lg | ascii_downcase | ltrimstr("app/") | rtrimstr("[bot]"));
  def app: ((lg | test("\\[bot\\]$")) or (lg | test("^app/")));
  [.[] | select((assoc | IN("OWNER","MEMBER","COLLABORATOR"))
                or (app and (norm == "loom-fleet-dispatch")))]'
cat > "$STUB_DIR/daemon-old" <<EOS
#!/usr/bin/env bash
if [[ "\$1 \$2" == "forge trusted-comments" ]]; then
  if [[ -f "\${LOOM_TEST_STUB_DIR:-}/trust-verb-missing" ]]; then
    echo "error: unrecognized subcommand 'trusted-comments'" >&2
    exit 2
  fi
  exec jq -c '$TRUSTED_COMMENTS_FILTER'
fi
echo "error: unrecognized subcommand '\$2'" >&2
exit 2
EOS
# A NEW daemon whose roster is: writer loom-fleet-dispatch, reader loom-fleet-reader-1.
# Also answers `forge trusted-comments` the same way as daemon-old above.
cat > "$STUB_DIR/daemon-new" <<EOS
#!/usr/bin/env bash
if [[ "\$1 \$2" == "forge trusted-comments" ]]; then
  if [[ -f "\${LOOM_TEST_STUB_DIR:-}/trust-verb-missing" ]]; then
    echo "error: unrecognized subcommand 'trusted-comments'" >&2
    exit 2
  fi
  exec jq -c '$TRUSTED_COMMENTS_FILTER'
fi
[[ "\$1 \$2" == "forge is-fleet" ]] || exit 2
case "\$3" in
  app/loom-fleet-dispatch|loom-fleet-dispatch\[bot\]) echo writer; exit 0 ;;
  app/loom-fleet-reader-1|loom-fleet-reader-1\[bot\]) echo reader; exit 0 ;;
  *) exit 1 ;;
esac
EOS
chmod +x "$STUB_DIR/daemon-old" "$STUB_DIR/daemon-new"
export LOOM_DAEMON_BIN="$STUB_DIR/daemon-old"

# ISO-8601 timestamp N hours before now-ish (macOS `date -v` first, GNU
# `date -d` fallback — mirrors judge-fallback-guard.sh's own dual-path idiom,
# using the REAL `date` binary, which is intentionally not stubbed here).
hours_ago() {
    date -u -v-"$1"H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "-$1 hours" +%Y-%m-%dT%H:%M:%SZ
}

marker_comment() {
    # marker_comment <created_at> <sha> [login] [author_association]
    #
    # Default author is this fleet's own writer App, App-spelled
    # (`loom-fleet-dispatch[bot]`) — Judge is the one that actually posts this
    # marker, so every PRE-#9716 test case keeps counting its markers exactly
    # as before once the guard's trust filter is in place. Tests that exercise
    # the filter itself (#9548/#9716) pass an explicit untrusted author.
    local login="${3:-loom-fleet-dispatch[bot]}" assoc="${4:-NONE}"
    printf '{"created_at":"%s","body":"Looks good.\\n\\n<!-- loom:fallback-evaluated sha=%s -->","user":{"login":"%s"},"author_association":"%s"}' \
        "$1" "$2" "$login" "$assoc"
}

reset_state() {
    rm -f "$STUB_DIR"/pr-*.json "$STUB_DIR"/comments-*.json
    rm -f "$STUB_DIR"/pr-view-fail-* "$STUB_DIR"/comments-fail-*
    rm -f "$STUB_DIR"/pr-view-stderr-* "$STUB_DIR"/comments-stderr-*
    rm -f "$STUB_DIR/trust-verb-missing"
}

run_guard() {
    OUT="$("$GUARD" "$@" 2>"$STUB_DIR/stderr.log")"
    RC=$?
    ERR="$(cat "$STUB_DIR/stderr.log" 2>/dev/null || true)"
}

get_field() {
    # get_field <output> <KEY>
    printf '%s\n' "$1" | grep "^$2=" | head -n1 | cut -d= -f2-
}

echo "Testing judge-fallback-guard.sh..."

# (a) Bot author -> SKIP, exit 10, independent of empty comment history.
reset_state
cat > "$STUB_DIR/pr-100.json" <<'EOF'
{"user":{"type":"Bot","login":"dependabot[bot]"},"head":{"sha":"aaaa000000000000000000000000000000000a"}}
EOF
run_guard 100
assert_eq "10" "$RC" "(a) Bot author -> exit 10"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(a) DECISION=SKIP"
assert_contains "$OUT" "bot-author" "(a) REASON mentions bot-author"

# (b) No comments at all, non-bot author -> EVALUATE, exit 0.
reset_state
cat > "$STUB_DIR/pr-101.json" <<'EOF'
{"user":{"type":"User","login":"someuser"},"head":{"sha":"bbbb000000000000000000000000000000000b"}}
EOF
run_guard 101
assert_eq "0" "$RC" "(b) No prior evaluations -> exit 0"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(b) DECISION=EVALUATE"
assert_eq "0" "$(get_field "$OUT" MARKER_COUNT)" "(b) MARKER_COUNT=0"
assert_eq "0" "$(get_field "$OUT" VELOCITY_ALERT)" "(b) VELOCITY_ALERT=0"

# (c) Lifetime cap reached via markers spread across DIFFERENT head SHAs
#     (simulating repeated trivial force-pushes) — the cap must fire even
#     though the most recent marker's SHA does NOT match the current head
#     SHA (i.e. this is NOT the SHA-dedup path; it is specifically the
#     per-PR-lifetime cap, the edge case the Curator flagged: a per-SHA-only
#     cap would not bound this PR because every force-push resets it).
reset_state
cat > "$STUB_DIR/pr-102.json" <<EOF
{"user":{"type":"User"},"head":{"sha":"cccc000000000000000000000000000000000c"}}
EOF
{
  echo "["
  marker_comment "$(hours_ago 40)" "1111111111111111111111111111111111111a"
  echo ","
  marker_comment "$(hours_ago 30)" "2222222222222222222222222222222222222b"
  echo ","
  marker_comment "$(hours_ago 20)" "3333333333333333333333333333333333333c"
  echo "]"
} > "$STUB_DIR/comments-102.json"
run_guard 102 --cap 3
assert_eq "11" "$RC" "(c) Lifetime cap reached across changing SHAs -> exit 11"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(c) DECISION=SKIP"
assert_contains "$OUT" "lifetime fallback-evaluation cap reached" "(c) REASON names the lifetime cap"
assert_eq "3" "$(get_field "$OUT" MARKER_COUNT)" "(c) MARKER_COUNT=3"

# (c2) Same marker history, but a HIGHER cap that hasn't been reached yet ->
#      falls through to SHA dedup / evaluate instead of the cap.
reset_state
cat > "$STUB_DIR/pr-103.json" <<EOF
{"user":{"type":"User"},"head":{"sha":"dddd000000000000000000000000000000000d"}}
EOF
{
  echo "["
  marker_comment "$(hours_ago 10)" "4444444444444444444444444444444444444d"
  echo "]"
} > "$STUB_DIR/comments-103.json"
run_guard 103 --cap 20
assert_eq "0" "$RC" "(c2) One prior marker, cap not reached, SHA changed -> exit 0"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(c2) DECISION=EVALUATE"
assert_eq "1" "$(get_field "$OUT" MARKER_COUNT)" "(c2) MARKER_COUNT=1"

# (d) SHA dedup (#5058): most recent marker's SHA matches the CURRENT head
#     SHA, cap not reached -> SKIP via dedup, exit 12.
reset_state
HEAD_SHA_D="eeee000000000000000000000000000000000e"
cat > "$STUB_DIR/pr-104.json" <<EOF
{"user":{"type":"User"},"head":{"sha":"$HEAD_SHA_D"}}
EOF
{
  echo "["
  marker_comment "$(hours_ago 5)" "$HEAD_SHA_D"
  echo "]"
} > "$STUB_DIR/comments-104.json"
run_guard 104 --cap 20
assert_eq "12" "$RC" "(d) Marker SHA matches current head SHA -> exit 12"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(d) DECISION=SKIP"
assert_contains "$OUT" "already evaluated in fallback mode at current head SHA" "(d) REASON names SHA dedup"

# (e) Velocity alert: several markers within the trailing window, cap not
#     reached, SHA changed (so decision is still EVALUATE) -> VELOCITY_ALERT=1
#     fires independently of the cap/dedup decision.
reset_state
cat > "$STUB_DIR/pr-105.json" <<EOF
{"user":{"type":"User"},"head":{"sha":"ffff000000000000000000000000000000000f"}}
EOF
{
  echo "["
  marker_comment "$(hours_ago 3)" "1111111111111111111111111111111111111a"
  echo ","
  marker_comment "$(hours_ago 2)" "2222222222222222222222222222222222222b"
  echo ","
  marker_comment "$(hours_ago 1)" "3333333333333333333333333333333333333c"
  echo "]"
} > "$STUB_DIR/comments-105.json"
run_guard 105 --cap 20 --velocity-threshold 3 --velocity-window-hours 4
assert_eq "0" "$RC" "(e) Velocity alert does not itself block evaluation -> exit 0"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(e) DECISION=EVALUATE despite velocity alert"
assert_eq "1" "$(get_field "$OUT" VELOCITY_ALERT)" "(e) VELOCITY_ALERT=1"
assert_eq "3" "$(get_field "$OUT" VELOCITY_COUNT)" "(e) VELOCITY_COUNT=3"

# (e2) Same markers, but a narrower window that excludes the oldest one, and
#      a threshold the remaining 2 don't meet -> VELOCITY_ALERT=0.
run_guard 105 --cap 20 --velocity-threshold 3 --velocity-window-hours 1
assert_eq "0" "$(get_field "$OUT" VELOCITY_ALERT)" "(e2) Narrower window drops below threshold -> VELOCITY_ALERT=0"

# (f) A Dependabot-style PR that reaches the cap must fall out of the
#     fallback queue via the SAME cap path as any other PR (structural
#     exclusion is via is_bot, independent of and prior to the cap) — confirm
#     it does not error or loop, i.e. still a clean SKIP with a stable exit
#     code, not e.g. exit 1.
reset_state
cat > "$STUB_DIR/pr-106.json" <<'EOF'
{"user":{"type":"Bot","login":"dependabot[bot]"},"head":{"sha":"1010101010101010101010101010101010101a"}}
EOF
{
  echo "["
  marker_comment "$(hours_ago 1)" "1010101010101010101010101010101010101a"
  echo "]"
} > "$STUB_DIR/comments-106.json"
run_guard 106 --cap 1
assert_eq "10" "$RC" "(f) Dependabot PR skipped via bot-author path (checked before cap) -> exit 10"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(f) DECISION=SKIP for Dependabot PR"

# (g) Bad args: non-numeric PR number -> usage error, exit 1.
reset_state
run_guard not-a-number
assert_eq "1" "$RC" "(g) Non-numeric PR number -> exit 1"
assert_contains "$ERR" "numeric PR number is required" "(g) stderr explains the usage error"

# (h) gh pr view failure -> exit 1 (environment error, not silently EVALUATE
#     or SKIP — the caller must treat this like any other gh failure).
reset_state
touch "$STUB_DIR/pr-view-fail-107"
run_guard 107
assert_eq "1" "$RC" "(h) REST pulls fetch failure -> exit 1"
assert_contains "$ERR" "/pulls/107" "(h) stderr names the failing gh call"

# (i) `gh pr view` writes a benign line to STDERR alongside a valid JSON
#     response on STDOUT (exit 0) — e.g. an update-notifier banner. The guard
#     must parse only stdout: the bot-check must resolve correctly and the
#     script must proceed past step 1, NOT spuriously exit 1 on corrupted JSON.
#     Regression guard for the #5455 Judge finding (merged 2>&1 broke parsing).
reset_state
cat > "$STUB_DIR/pr-108.json" <<'EOF'
{"user":{"type":"User","login":"someuser"},"head":{"sha":"8080808080808080808080808080808080808a"}}
EOF
touch "$STUB_DIR/pr-view-stderr-108"
run_guard 108
assert_eq "0" "$RC" "(i) benign stderr on gh pr view success -> still exit 0 (not spurious exit 1)"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(i) DECISION=EVALUATE despite pr-view stderr chatter"
assert_eq "8080808080808080808080808080808080808a" "$(get_field "$OUT" HEAD_SHA)" "(i) HEAD_SHA parsed cleanly from stdout only"

# (i2) Same stderr-chatter scenario, but the author IS a bot — the bot-check
#      must still fire (SKIP exit 10), proving stdout parsed cleanly for the
#      is_bot field too, not just headRefOid.
reset_state
cat > "$STUB_DIR/pr-109.json" <<'EOF'
{"user":{"type":"Bot","login":"dependabot[bot]"},"head":{"sha":"9090909090909090909090909090909090909a"}}
EOF
touch "$STUB_DIR/pr-view-stderr-109"
run_guard 109
assert_eq "10" "$RC" "(i2) benign stderr + bot author -> still exit 10 (bot-check unaffected)"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(i2) DECISION=SKIP for bot author despite stderr chatter"

# (j) `gh api .../comments` writes a benign line to STDERR alongside a valid
#     JSON array on STDOUT (exit 0). The guard must count markers from stdout
#     only — merging stderr in silently zeroed MARKER_COUNT and defeated the
#     lifetime cap (the exact #4972 199-comment livelock this PR prevents).
#     Here 3 real markers with --cap 3 MUST still fire the cap (exit 11),
#     not silently return MARKER_COUNT=0 / EVALUATE.
reset_state
cat > "$STUB_DIR/pr-110.json" <<'EOF'
{"user":{"type":"User"},"head":{"sha":"a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a"}}
EOF
{
  echo "["
  marker_comment "$(hours_ago 40)" "1111111111111111111111111111111111111a"
  echo ","
  marker_comment "$(hours_ago 30)" "2222222222222222222222222222222222222b"
  echo ","
  marker_comment "$(hours_ago 20)" "3333333333333333333333333333333333333c"
  echo "]"
} > "$STUB_DIR/comments-110.json"
touch "$STUB_DIR/comments-stderr-110"
run_guard 110 --cap 3
assert_eq "11" "$RC" "(j) benign stderr on gh api comments success -> cap still fires (exit 11)"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(j) DECISION=SKIP despite comments stderr chatter"
assert_eq "3" "$(get_field "$OUT" MARKER_COUNT)" "(j) MARKER_COUNT=3 parsed from stdout only (not silently 0)"

# (j2) Both call sites emit stderr chatter simultaneously, markers present but
#      cap not yet reached -> still counts correctly and proceeds to EVALUATE
#      with the true MARKER_COUNT, proving neither stream corrupts the other.
reset_state
cat > "$STUB_DIR/pr-111.json" <<'EOF'
{"user":{"type":"User"},"head":{"sha":"b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b"}}
EOF
{
  echo "["
  marker_comment "$(hours_ago 10)" "4444444444444444444444444444444444444d"
  echo "]"
} > "$STUB_DIR/comments-111.json"
touch "$STUB_DIR/pr-view-stderr-111"
touch "$STUB_DIR/comments-stderr-111"
run_guard 111 --cap 20
assert_eq "0" "$RC" "(j2) stderr on BOTH calls, cap not reached -> exit 0"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(j2) DECISION=EVALUATE with stderr on both calls"
assert_eq "1" "$(get_field "$OUT" MARKER_COUNT)" "(j2) MARKER_COUNT=1 counted correctly despite dual stderr"

# (k) Loom's own App dispatch identity (app/loom-fleet-dispatch) is reported
#     by GitHub as is_bot:true, same as Dependabot -- but MUST NOT be skipped
#     via the bot-author path (#6982): it is Loom's own PR creation identity,
#     not an external bot outside the Loom label workflow. No prior markers
#     -> proceeds all the way to EVALUATE, exit 0, proving it reaches cap/dedup
#     logic rather than exiting 10.
reset_state
cat > "$STUB_DIR/pr-112.json" <<'EOF'
{"user":{"type":"Bot","login":"loom-fleet-dispatch[bot]"},"head":{"sha":"c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c"}}
EOF
run_guard 112
assert_eq "0" "$RC" "(k) loom-fleet-dispatch[bot] type:Bot -> NOT skipped, exit 0"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(k) DECISION=EVALUATE for loom-fleet-dispatch[bot] despite type:Bot"

# (k2) Same allowlisted identity, but with enough prior markers to reach the
#      lifetime cap -> proves it reaches Step 2 (cap logic), not that it is
#      unconditionally waved through -- SKIP now comes from the cap, not the
#      bot-author path (exit 11, not exit 10).
reset_state
cat > "$STUB_DIR/pr-113.json" <<EOF
{"user":{"type":"Bot","login":"loom-fleet-dispatch[bot]"},"head":{"sha":"d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d"}}
EOF
{
  echo "["
  marker_comment "$(hours_ago 40)" "1111111111111111111111111111111111111a"
  echo ","
  marker_comment "$(hours_ago 30)" "2222222222222222222222222222222222222b"
  echo ","
  marker_comment "$(hours_ago 20)" "3333333333333333333333333333333333333c"
  echo "]"
} > "$STUB_DIR/comments-113.json"
run_guard 113 --cap 3
assert_eq "11" "$RC" "(k2) loom-fleet-dispatch[bot] past bot-check, cap reached -> exit 11 (not 10)"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(k2) DECISION=SKIP via lifetime cap, not bot-author path"

# (k3) #9537: with a daemon that knows the roster, a READER identity's PR (its
#      history re-attributed by an App rename) is Loom's own -> EVALUATE.
reset_state
cat > "$STUB_DIR/pr-114.json" <<'EOF'
{"user":{"type":"Bot","login":"loom-fleet-reader-1[bot]"},"head":{"sha":"e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e"}}
EOF
LOOM_DAEMON_BIN="$STUB_DIR/daemon-new" run_guard 114
assert_eq "0" "$RC" "(k3) roster reader loom-fleet-reader-1[bot] -> NOT skipped"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(k3) DECISION=EVALUATE for a roster reader"

# (k4) A daemon that knows the verb answers not-ours for an unrelated bot ->
#      bot-author SKIP, and its exit 1 is final (the fallback pattern is NOT
#      consulted after a definite answer).
reset_state
cat > "$STUB_DIR/pr-115.json" <<'EOF'
{"user":{"type":"Bot","login":"renovate[bot]"},"head":{"sha":"f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"}}
EOF
LOOM_DAEMON_BIN="$STUB_DIR/daemon-new" run_guard 115
assert_eq "10" "$RC" "(k4) daemon answers not-fleet for renovate[bot] -> exit 10"

# (k5) An old daemon (no is-fleet verb): the fallback accepts a numbered pool
#      App -- the #6982 regression the old exact match reintroduced for -1/-2.
reset_state
cat > "$STUB_DIR/pr-116.json" <<'EOF'
{"user":{"type":"Bot","login":"loom-fleet-dispatch-2[bot]"},"head":{"sha":"a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a"}}
EOF
run_guard 116
assert_eq "0" "$RC" "(k5) old daemon, loom-fleet-dispatch-2[bot] -> fallback accepts it"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(k5) DECISION=EVALUATE via the default-family fallback"

# (k6) ...but the fallback is exact, never a prefix.
reset_state
cat > "$STUB_DIR/pr-117.json" <<'EOF'
{"user":{"type":"Bot","login":"loom-fleet-dispatch-evil[bot]"},"head":{"sha":"b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b"}}
EOF
run_guard 117
assert_eq "10" "$RC" "(k6) loom-fleet-dispatch-evil[bot] -> bot-author SKIP, exit 10"

# (k7) #9340: Step 1 is REST, which spells an App `<slug>[bot]` (GraphQL said
#      `app/<slug>`). The no-daemon fallback must accept the REST spelling of a
#      numbered member too, or #6982 comes back for every fleet-authored PR.
reset_state
cat > "$STUB_DIR/pr-118.json" <<'EOF'
{"user":{"type":"Bot","login":"loom-fleet-dispatch-3[bot]"},"head":{"sha":"c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c"}}
EOF
run_guard 118
assert_eq "0" "$RC" "(k7) old daemon, loom-fleet-dispatch-3[bot] (REST spelling) -> NOT skipped"

# (k8) #9340: a PR whose REST payload lacks head.sha is still an environment
#      error (exit 1), never a silent EVALUATE.
reset_state
echo '{"user":{"type":"User","login":"someuser"},"head":{}}' > "$STUB_DIR/pr-119.json"
run_guard 119
assert_eq "1" "$RC" "(k8) missing head.sha -> exit 1"
assert_contains "$ERR" "could not resolve head SHA" "(k8) stderr names the missing head SHA"

# ============================================================================
# #9548 / #9716: `loom:fallback-evaluated` counts only from a trusted author.
# The marker SUPPRESSES review (feeds the lifetime cap and SHA dedup), so an
# outsider able to comment on a public PR must not be able to post one and
# silence the Judge's own safety net.
# ============================================================================
OUTSIDER_LOGIN="drive-by"
OUTSIDER_ASSOC="CONTRIBUTOR"

# (l) An outsider spoofing the lifetime cap: 3 well-formed markers, --cap 3,
#     but none from a trusted author -> MARKER_COUNT=0, no cap, EVALUATE.
reset_state
cat > "$STUB_DIR/pr-118.json" <<EOF
{"user":{"type":"User"},"head":{"sha":"c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c"}}
EOF
{
  echo "["
  marker_comment "$(hours_ago 40)" "1111111111111111111111111111111111111a" "$OUTSIDER_LOGIN" "$OUTSIDER_ASSOC"
  echo ","
  marker_comment "$(hours_ago 30)" "2222222222222222222222222222222222222b" "$OUTSIDER_LOGIN" "$OUTSIDER_ASSOC"
  echo ","
  marker_comment "$(hours_ago 20)" "3333333333333333333333333333333333333c" "$OUTSIDER_LOGIN" "$OUTSIDER_ASSOC"
  echo "]"
} > "$STUB_DIR/comments-118.json"
run_guard 118 --cap 3
assert_eq "0" "$RC" "(l) 3 outsider markers cannot spoof the cap -> exit 0, not 11"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(l) DECISION=EVALUATE despite 3 well-formed outsider markers"
assert_eq "0" "$(get_field "$OUT" MARKER_COUNT)" "(l) MARKER_COUNT=0 -- an outsider's marker counts for nothing"

# (l2) An outsider spoofing SHA dedup: a single marker naming the CURRENT head
#      SHA, from an untrusted author -> must NOT skip via dedup.
reset_state
HEAD_SHA_L2="d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d"
cat > "$STUB_DIR/pr-119.json" <<EOF
{"user":{"type":"User"},"head":{"sha":"$HEAD_SHA_L2"}}
EOF
{
  echo "["
  marker_comment "$(hours_ago 1)" "$HEAD_SHA_L2" "$OUTSIDER_LOGIN" "$OUTSIDER_ASSOC"
  echo "]"
} > "$STUB_DIR/comments-119.json"
run_guard 119 --cap 20
assert_eq "0" "$RC" "(l2) outsider's current-head marker cannot spoof SHA dedup -> exit 0, not 12"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(l2) DECISION=EVALUATE -- the outsider's marker is prose, not state"

# (l3) An outsider spoofing the velocity alert: several untrusted markers
#      inside the window -> VELOCITY_ALERT stays 0, VELOCITY_COUNT=0.
reset_state
cat > "$STUB_DIR/pr-120.json" <<EOF
{"user":{"type":"User"},"head":{"sha":"e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e"}}
EOF
{
  echo "["
  marker_comment "$(hours_ago 3)" "1111111111111111111111111111111111111a" "$OUTSIDER_LOGIN" "$OUTSIDER_ASSOC"
  echo ","
  marker_comment "$(hours_ago 2)" "2222222222222222222222222222222222222b" "$OUTSIDER_LOGIN" "$OUTSIDER_ASSOC"
  echo ","
  marker_comment "$(hours_ago 1)" "3333333333333333333333333333333333333c" "$OUTSIDER_LOGIN" "$OUTSIDER_ASSOC"
  echo "]"
} > "$STUB_DIR/comments-120.json"
run_guard 120 --cap 20 --velocity-threshold 3 --velocity-window-hours 4
assert_eq "0" "$(get_field "$OUT" VELOCITY_ALERT)" "(l3) 3 outsider markers in-window do not trip VELOCITY_ALERT"
assert_eq "0" "$(get_field "$OUT" VELOCITY_COUNT)" "(l3) VELOCITY_COUNT=0 -- outsider markers are not counted at all"

# (l4) Mixed authorship: an outsider's marker and a trusted (fleet-App)
#      marker on the same PR -- only the trusted one counts toward the cap,
#      proving this is per-comment filtering, not an all-or-nothing toggle.
reset_state
cat > "$STUB_DIR/pr-121.json" <<EOF
{"user":{"type":"User"},"head":{"sha":"f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f"}}
EOF
{
  echo "["
  marker_comment "$(hours_ago 10)" "1111111111111111111111111111111111111a" "$OUTSIDER_LOGIN" "$OUTSIDER_ASSOC"
  echo ","
  marker_comment "$(hours_ago 5)" "2222222222222222222222222222222222222b"
  echo "]"
} > "$STUB_DIR/comments-121.json"
run_guard 121 --cap 1
assert_eq "11" "$RC" "(l4) the one TRUSTED marker alone reaches --cap 1 -> exit 11"
assert_eq "1" "$(get_field "$OUT" MARKER_COUNT)" "(l4) MARKER_COUNT=1 -- the outsider's marker is dropped, the fleet App's counts"

# (m) Fail-safe: no `forge trusted-comments` verb (old or absent binary) ->
#     EVERY marker counts as absent, even a well-formed, correctly-attributed
#     one -- never fall back to the unfiltered listing. Direction check: this
#     can only make the guard evaluate more (never let an unauthenticatable
#     marker suppress a real review).
reset_state
touch "$STUB_DIR/trust-verb-missing"
cat > "$STUB_DIR/pr-122.json" <<EOF
{"user":{"type":"User"},"head":{"sha":"a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a"}}
EOF
{
  echo "["
  marker_comment "$(hours_ago 40)" "1111111111111111111111111111111111111a"
  echo ","
  marker_comment "$(hours_ago 30)" "2222222222222222222222222222222222222b"
  echo ","
  marker_comment "$(hours_ago 20)" "3333333333333333333333333333333333333c"
  echo "]"
} > "$STUB_DIR/comments-122.json"
run_guard 122 --cap 3
assert_eq "0" "$RC" "(m) no trusted-comments verb -> markers read as absent, exit 0 not 11"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(m) DECISION=EVALUATE when the filter cannot run"
assert_eq "0" "$(get_field "$OUT" MARKER_COUNT)" "(m) MARKER_COUNT=0 when authorship cannot be authenticated"
assert_contains "$ERR" "could not authenticate comment authors" "(m) stderr names the authentication failure (#9548/#9716)"
rm -f "$STUB_DIR/trust-verb-missing"

# --- Summary -------------------------------------------------------------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
