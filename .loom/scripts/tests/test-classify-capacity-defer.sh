#!/usr/bin/env bash
# test-classify-capacity-defer.sh - Tests for classify-capacity-defer.sh
# (issue #6729), the no-comment-stream idempotency for Champion's tier-
# capacity deferral path.
#
# Champion's tier-based rate limiting can hold back a proposal that passes
# all 8 promotion criteria purely on capacity (Tier 3: 1 promotion per
# iteration, only if fewer than 5 already in the backlog; Tier 2: up to 2 per
# iteration). Without a guard, every later pass re-derives the same
# conclusion for an unrevised proposal and posts an equivalent "Tier N
# backlog cap reached" comment again -- observed live as 10 near-identical
# comments on #6628 over ~22 hours, and 2-3 more on each of #6647/#6649, all
# citing the SAME five occupant tier:maintenance issues.
#
# This suite is hybrid, mirroring test-classify-dependency-block.sh:
#
#   1. normalize_occupants / capacity_fingerprint / capacity_marker /
#      extract_fingerprint_from_marker are real sourceable functions (the
#      script guards its main on `BASH_SOURCE == $0`), tested directly.
#   2. The decision (SKIP_COMMENT vs POST_COMMENT) and its exit codes are
#      exercised black-box by stubbing `gh` on PATH and running the real
#      script.
#   3. The Champion prose call sites are pinned with literal
#      assert_doc_contains checks, so wiring the gate out of the role files
#      fails here.
#
# Hermetic: no network, no live forge, no tokens. Every read goes to the stub.
#
# Usage:
#   ./.loom/scripts/tests/test-classify-capacity-defer.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/write-scope-fixture.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/write-scope-fixture.sh"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
CCD="$SCRIPTS_DIR/classify-capacity-defer.sh"

# The script under test filters its comment reads through
# `loom-daemon forge trusted-comments` (#9548), so this suite pins a real
# daemon: FATAL, not SKIP — a missing binary would silently turn every
# fixture marker into "absent" and the trust scenarios could not tell trust
# from absence. See the helper.
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "forge trusted-comments"

# Two `..` reaches repo-root/.claude/commands/loom for an INSTALLED copy
# (SCRIPTS_DIR is .loom/scripts there); one `..` reaches defaults/.claude/
# commands/loom when running inside this source repo (SCRIPTS_DIR is
# defaults/scripts) -- the two layouts differ in depth, so probe both rather
# than hard-coding one (#6725).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
CHAMPION_PROMO_MD="$PROMPT_DIR/champion-issue-promo.md"
CHAMPION_REF_MD="$PROMPT_DIR/champion-reference.md"

# Source for the pure helpers BEFORE defining our own colors (the sourced
# chain defines RED/YELLOW/NC itself).
# shellcheck source=/dev/null
source "$CCD"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected to contain: '$needle'"
        echo "    Actual: '$haystack'"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected NOT to contain: '$needle'"
        echo "    Actual: '$haystack'"
    fi
}

assert_doc_contains() {
    local file="$1" needle="$2" msg="$3"
    if grep -qF -- "$needle" "$file"; then
        pass "$msg"
    else
        fail "$msg (missing literal in $file: $needle)"
    fi
}

# =====================================================================
# Unit tests: the real sourced helpers
# =====================================================================

echo "--- normalize_occupants: order/duplicate/format independent ---"

assert_eq "4136 5512 6068 6076 6612" \
    "$(normalize_occupants '#6612,#6076,#6068,#5512,#4136')" \
    "comma-separated with # prefixes normalizes to sorted, unprefixed, space-joined"

assert_eq "4136 5512 6068 6076 6612" \
    "$(normalize_occupants '6076 6612   6068,5512,4136')" \
    "mixed whitespace/comma separators normalize the same way"

assert_eq "$(normalize_occupants '#3 #5 #9')" "$(normalize_occupants '#9,#3,#5')" \
    "order of the input list does not affect the normalized form"

assert_eq "3 5" "$(normalize_occupants '#3,#3,#5,5')" \
    "duplicates (with or without #) collapse to one entry"

assert_eq "" "$(normalize_occupants '')" \
    "an empty occupant list normalizes to an empty string"

echo
echo "--- capacity_fingerprint: identity of the (tier, occupant-set) PAIR ---"

occ_a="$(normalize_occupants '#3,#5,#9')"
occ_a_reordered="$(normalize_occupants '#9,#3,#5')"
occ_b="$(normalize_occupants '#3,#5,#8')"

assert_eq "$(capacity_fingerprint 'tier:maintenance' "$occ_a")" \
    "$(capacity_fingerprint 'tier:maintenance' "$occ_a_reordered")" \
    "same set in either order fingerprints identically"

if [[ "$(capacity_fingerprint 'tier:maintenance' "$occ_a")" \
      == "$(capacity_fingerprint 'tier:maintenance' "$occ_b")" ]]; then
    fail "a different occupant set fingerprints differently"
else
    pass "a different occupant set fingerprints differently"
fi

if [[ "$(capacity_fingerprint 'tier:maintenance' "$occ_a")" \
      == "$(capacity_fingerprint 'tier:goal-supporting' "$occ_a")" ]]; then
    fail "the SAME occupant set under a different tier fingerprints differently"
else
    pass "the SAME occupant set under a different tier fingerprints differently"
fi

echo
echo "--- capacity_marker / capacity_marker_prefix / extract_fingerprint_from_marker ---"

fp="$(capacity_fingerprint 'tier:maintenance' "$occ_a")"
marker="$(capacity_marker 'tier:maintenance' "$fp")"
assert_eq "<!-- champion:capacity-defer:tier:maintenance:$fp -->" "$marker" \
    "marker shape is the tag, tier, and fingerprint"

extracted="$(extract_fingerprint_from_marker "some prose $marker more prose" 'tier:maintenance')"
assert_eq "$fp" "$extracted" "the fingerprint round-trips out of a comment body containing the marker"

assert_eq "" "$(extract_fingerprint_from_marker 'no marker in this comment at all' 'tier:maintenance')" \
    "a comment with no marker at all yields no fingerprint"

other_marker="$(capacity_marker 'tier:goal-supporting' "$fp")"
assert_eq "" "$(extract_fingerprint_from_marker "$other_marker" 'tier:maintenance')" \
    "a marker for a DIFFERENT tier is not read as this tier's marker"

# A comment carrying two markers (composition changed, so a fresh marker was
# posted alongside the record of the prior one in a hypothetical body) -- the
# LAST one wins, mirroring classify-dependency-block.sh's own "last" convention.
fp2="$(capacity_fingerprint 'tier:maintenance' "$occ_b")"
marker2="$(capacity_marker 'tier:maintenance' "$fp2")"
two_marker_body="$marker
$marker2"
assert_eq "$fp2" "$(extract_fingerprint_from_marker "$two_marker_body" 'tier:maintenance')" \
    "when a comment body carries two markers for this tier, the LAST one is read"

# =====================================================================
# Black-box: stub `gh` on PATH, run the real script
# =====================================================================

STUB_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ccd-test.XXXXXX")"
cleanup() { rm -rf "$STUB_DIR"; }
trap cleanup EXIT

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Minimal `gh` stub: serves fixtures from issue-<owner>_<repo>_<N>.json
# ({"comments": [{"id":.., "body":..}, ...]}). Records PATCH calls to a log.
STUB_DIR="$(cd "$(dirname "$0")" && pwd)"
printf '%s\n' "$*" >> "$STUB_DIR/calls.log"

[[ "${1:-}" == "--version" ]] && { echo "gh version 0.0.0 (stub)"; exit 0; }

case "${1:-}" in
  issue)
    action="$2"; num="$3"; shift 3
    repo=""; jqexpr=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --repo) repo="$2"; shift 2 ;;
        --json) shift 2 ;;
        --jq)   jqexpr="$2"; shift 2 ;;
        *)      shift ;;
      esac
    done
    [[ "$action" == "view" ]] || { echo "stub gh: unhandled issue action: $action" >&2; exit 3; }
    key="$(printf '%s' "$repo#$num" | tr '/#' '__')"
    f="$STUB_DIR/issue-$key.json"
    [[ -f "$f" ]] || { echo "gh: not found" >&2; exit 1; }
    # Render what `gh issue view --json comments` really returns: node ids,
    # author.login with any App `[bot]` suffix stripped, no account type,
    # authorAssociation. Serving the REST shape here hid the #9657 bug.
    gql='{comments:[.comments[] | {id:("IC_" + (.id|tostring)), author:{login:(.user.login | sub("\\[bot\\]$"; ""))}, authorAssociation:.author_association, body}]}'
    if [[ -n "$jqexpr" ]]; then jq "$gql" < "$f" | jq -c "$jqexpr"; else jq "$gql" < "$f"; fi
    ;;
  api)
    shift
    method="GET"; path=""; fbody=""; paginate=0
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --method)   method="$2"; shift 2 ;;
        --paginate) paginate=1; shift ;;
        -f)         fval="$2"; [[ "$fval" == body=* ]] && fbody="${fval#body=}"; shift 2 ;;
        -X)         method="$2"; shift 2 ;;
        *)          [[ -z "$path" ]] && path="$1"; shift ;;
      esac
    done
    if [[ "$method" == "PATCH" ]]; then
      # path like: repos/{owner}/{repo}/issues/comments/<id>
      cid="${path##*/}"
      match=0
      for f in "$STUB_DIR"/issue-*.json; do
        [[ -f "$f" ]] || continue
        if jq -e --arg id "$cid" '.comments[]? | select((.id|tostring) == $id)' "$f" >/dev/null 2>&1; then
          tmp="$(mktemp)"
          jq --arg id "$cid" --arg b "$fbody" \
            '.comments = [.comments[] | if ((.id|tostring) == $id) then (.body = $b) else . end]' \
            "$f" > "$tmp" && mv "$tmp" "$f"
          printf 'PATCH %s\n' "$cid" >> "$STUB_DIR/patches.log"
          match=1
          break
        fi
      done
      [[ "$match" -eq 1 ]] || { echo "stub gh: no comment with id $cid" >&2; exit 1; }
      exit 0
    fi
    # GET .../issues/<N>/comments --paginate -> print the comments array
    if [[ "$path" == *"/issues/"*"/comments" ]]; then
      num="$(printf '%s' "$path" | sed -E 's#.*/issues/([0-9]+)/comments#\1#')"
      f=""
      for cand in "$STUB_DIR"/issue-*_"$num".json; do
        [[ -f "$cand" ]] && f="$cand" && break
      done
      [[ -n "$f" ]] || { echo "gh: not found" >&2; exit 1; }
      jq -c '.comments' "$f"
      exit 0
    fi
    echo "stub gh: unhandled api path: $path" >&2
    exit 3
    ;;
  *)
    echo "stub gh: unhandled args: $*" >&2
    exit 3
    ;;
esac
STUB
chmod +x "$STUB_DIR/gh"

export PATH="$STUB_DIR:$PATH"

# #9548: The script (with --apply) vets its write target through the write scope before it
# writes. It runs from a checkout registered as o/r (origin, .loom/, push
# reported to the permission probe), so the real decision admits it.
write_scope_register "$STUB_DIR/checkout" o/r
cd "$STUB_DIR/checkout" || exit 1

# issue_fixture_by <login> <type> <association> <owner/repo#N> [comment-body...]
# Writes the issue's comment listing in the REST shape (`gh api
# .../issues/N/comments`: user.login, user.type, author_association) -- the
# shape a real fleet App author has its `[bot]` suffix in. Each comment is
# auto-assigned a numeric id (1, 2, 3, ...) in call order.
issue_fixture_by() {
    local login="$1" type="$2" assoc="$3" node="$4"; shift 4
    local key; key="$(printf '%s' "$node" | tr '/#' '__')"
    jq -n '{comments:[]}' > "$STUB_DIR/issue-$key.json"
    local c
    for c in "$@"; do append_comment "$node" "$login" "$type" "$assoc" "$c"; done
}

# append_comment <owner/repo#N> <login> <type> <association> <body>
# Appends one REST-shaped comment (next numeric id) to an existing fixture,
# so a single issue can mix authors.
append_comment() {
    local node="$1" login="$2" type="$3" assoc="$4" body="$5"
    local key; key="$(printf '%s' "$node" | tr '/#' '__')"
    local f="$STUB_DIR/issue-$key.json" tmp; tmp="$(mktemp)"
    jq --arg l "$login" --arg t "$type" --arg a "$assoc" --arg b "$body" \
        '.comments += [{"id":((.comments|length)+1),"body":$b,"user":{"login":$l,"type":$t},"author_association":$a}]' \
        "$f" > "$tmp" && mv "$tmp" "$f"
}

# issue_fixture <owner/repo#N> [comment-body...]
# Comments authored by the repo OWNER (an author_association
# `comment_trust::TRUSTED_ASSOCIATIONS` believes with no roster -- #9548).
issue_fixture() { issue_fixture_by o User OWNER "$@"; }

# issue_fixture_untrusted <owner/repo#N> [comment-body...]
# Authored by an OUTSIDER (foreign login, author_association NONE): `forge
# trusted-comments` drops every comment, so the decision must behave exactly
# as if the issue had no comments (#9548).
issue_fixture_untrusted() { issue_fixture_by stranger User NONE "$@"; }

# issue_fixture_fleet_app <owner/repo#N> [comment-body...]
# Authored by one of THIS fleet's Apps, as the forge reports it: an App login
# (`[bot]`, type Bot) whose association is CONTRIBUTOR, never an insider one.
# `loom-fleet-dispatch` is the fleet's default App family, trusted by every
# roster, so the fixture needs no configured roster. Only the REST listing
# spells it as an App; `gh issue view --json comments` reduces it to the bare
# slug, which comment_trust must treat as a user -- which is why the live read
# has to be REST (#9657 review, live evidence on #9126: 1 of 7 markers kept
# via `--json comments`, 7 of 7 via REST).
issue_fixture_fleet_app() { issue_fixture_by 'loom-fleet-dispatch[bot]' Bot CONTRIBUTOR "$@"; }

reset_state() {
    rm -f "$STUB_DIR"/issue-*.json "$STUB_DIR"/patches.log "$STUB_DIR/calls.log"
}

# run_ccd <args...> -> sets OUT / RC
run_ccd() {
    OUT="$("$CCD" --no-cache "$@" 2>"$STUB_DIR/stderr.log")"
    RC=$?
}

patches_log() { cat "$STUB_DIR/patches.log" 2>/dev/null; }

OCCUPANTS_5="#6612,#6076,#6068,#5512,#4136"
OCCUPANTS_5_REORDERED="#4136,#5512,#6068,#6076,#6612"
OCCUPANTS_6="#6612,#6076,#6068,#5512,#4136,#9999"

echo
echo "--- --check-capacity-defer (default mode): no prior comment -> POST_COMMENT, first-deferral ---"
reset_state
issue_fixture 'o/r#5'
run_ccd --issue 5 --repo o/r --tier tier:maintenance --occupants "$OCCUPANTS_5"
assert_eq "1" "$RC" "exit 1 - post a comment (nothing to defer against yet)"
assert_contains "$OUT" "POST_COMMENT" "POST_COMMENT marker present"
assert_contains "$OUT" "REASON: first-deferral" "reason is first-deferral"
assert_contains "$OUT" "FINGERPRINT: " "a fingerprint is emitted for the caller to seed into the new comment"

echo
echo "--- same tier + same occupant set as the last deferral comment -> SKIP_COMMENT ---"
reset_state
fp="$(capacity_fingerprint 'tier:maintenance' "$(normalize_occupants "$OCCUPANTS_5")")"
issue_fixture 'o/r#5' "<!-- champion:capacity-defer:tier:maintenance:$fp -->
<!-- champion:capacity-defer-seen:$fp:1 -->
**Champion Review: Tier 3 backlog cap reached — deferring promotion**"
run_ccd --issue 5 --repo o/r --tier tier:maintenance --occupants "$OCCUPANTS_5_REORDERED"
assert_eq "0" "$RC" "exit 0 - skip, the occupant set (reordered) is the SAME set"
assert_contains "$OUT" "SKIP_COMMENT" "SKIP_COMMENT marker present"
assert_contains "$OUT" "FINGERPRINT: $fp" "the matching fingerprint is echoed"

echo
echo "--- --apply on a SKIP bumps the existing comment's seen-counter, posts NOTHING new ---"
reset_state
fp="$(capacity_fingerprint 'tier:maintenance' "$(normalize_occupants "$OCCUPANTS_5")")"
issue_fixture 'o/r#5' "<!-- champion:capacity-defer:tier:maintenance:$fp -->
<!-- champion:capacity-defer-seen:$fp:1 -->
**Champion Review: Tier 3 backlog cap reached — deferring promotion**"
run_ccd --issue 5 --repo o/r --tier tier:maintenance --occupants "$OCCUPANTS_5" --apply
assert_eq "0" "$RC" "exit 0 - skip applies"
assert_contains "$OUT" "PATCHED: o/r#5" "--apply reports the patch"
assert_contains "$(patches_log)" "PATCH 1" "the existing comment (id 1) was PATCHed, not a new one created"
new_body="$(jq -r '.comments[0].body' "$STUB_DIR/issue-o_r_5.json")"
assert_contains "$new_body" "champion:capacity-defer-seen:$fp:2" "the seen-counter advanced from 1 to 2"
assert_eq "1" "$(jq '.comments | length' "$STUB_DIR/issue-o_r_5.json")" "still exactly one comment -- nothing new was posted"

echo
echo "--- --apply, called AGAIN on the same (now bumped) state, advances the counter further ---"
run_ccd --issue 5 --repo o/r --tier tier:maintenance --occupants "$OCCUPANTS_5" --apply
assert_eq "0" "$RC" "exit 0 - still skip"
new_body="$(jq -r '.comments[0].body' "$STUB_DIR/issue-o_r_5.json")"
assert_contains "$new_body" "champion:capacity-defer-seen:$fp:3" "the seen-counter advances again on a second unchanged pass"
assert_eq "1" "$(jq '.comments | length' "$STUB_DIR/issue-o_r_5.json")" "still exactly one comment after two skip passes"

echo
echo "--- occupant set CHANGED since the last deferral comment -> POST_COMMENT, composition-changed ---"
reset_state
fp_old="$(capacity_fingerprint 'tier:maintenance' "$(normalize_occupants "$OCCUPANTS_5")")"
issue_fixture 'o/r#5' "<!-- champion:capacity-defer:tier:maintenance:$fp_old -->
<!-- champion:capacity-defer-seen:$fp_old:1 -->
**Champion Review: Tier 3 backlog cap reached — deferring promotion**"
run_ccd --issue 5 --repo o/r --tier tier:maintenance --occupants "$OCCUPANTS_6"
assert_eq "1" "$RC" "exit 1 - post again, the backlog gained a 6th occupant"
assert_contains "$OUT" "POST_COMMENT" "POST_COMMENT marker present"
assert_contains "$OUT" "REASON: composition-changed" "reason is composition-changed"
assert_contains "$OUT" "PRIOR_FINGERPRINT: $fp_old" "the prior fingerprint is named for context"
fp_new="$(capacity_fingerprint 'tier:maintenance' "$(normalize_occupants "$OCCUPANTS_6")")"
assert_contains "$OUT" "FINGERPRINT: $fp_new" "the NEW fingerprint (for the fresh comment) is emitted"

echo
echo "--- a DIFFERENT tier's cap on the same issue never matches this tier's marker -> POST_COMMENT ---"
reset_state
fp_maint="$(capacity_fingerprint 'tier:maintenance' "$(normalize_occupants "$OCCUPANTS_5")")"
issue_fixture 'o/r#5' "<!-- champion:capacity-defer:tier:maintenance:$fp_maint -->
<!-- champion:capacity-defer-seen:$fp_maint:1 -->
**Champion Review: Tier 3 backlog cap reached — deferring promotion**"
run_ccd --issue 5 --repo o/r --tier tier:goal-supporting --occupants "#100,#101"
assert_eq "1" "$RC" "exit 1 - a tier:maintenance marker is not read as a tier:goal-supporting deferral"
assert_contains "$OUT" "REASON: first-deferral" "from tier:goal-supporting's perspective this is a first deferral"

echo
echo "--- only the LATEST capacity-deferral comment is consulted, not an older superseded one ---"
reset_state
fp_a="$(capacity_fingerprint 'tier:maintenance' "$(normalize_occupants '#1,#2,#3')")"
fp_b="$(capacity_fingerprint 'tier:maintenance' "$(normalize_occupants '#1,#2,#4')")"
issue_fixture 'o/r#5' \
    "<!-- champion:capacity-defer:tier:maintenance:$fp_a -->
<!-- champion:capacity-defer-seen:$fp_a:1 -->
first deferral" \
    "<!-- champion:capacity-defer:tier:maintenance:$fp_b -->
<!-- champion:capacity-defer-seen:$fp_b:1 -->
second deferral, composition changed"
run_ccd --issue 5 --repo o/r --tier tier:maintenance --occupants "#1,#2,#4"
assert_eq "0" "$RC" "exit 0 - matches the SECOND (latest) comment's occupant set, not the first"
assert_contains "$OUT" "FINGERPRINT: $fp_b" "the latest comment's fingerprint is the one compared against"

echo
echo "--- an empty occupant set is handled without erroring (a cap not tied to enumerable occupants) ---"
reset_state
issue_fixture 'o/r#5'
run_ccd --issue 5 --repo o/r --tier tier:maintenance --occupants ""
assert_eq "1" "$RC" "exit 1 - first deferral, even with no occupants named"
assert_contains "$OUT" "OCCUPANTS: " "the (empty) occupants line is still printed"

# =====================================================================
# Argument validation
# =====================================================================

echo
echo "--- argument validation ---"
reset_state
run_ccd --repo o/r --tier tier:maintenance --occupants "#1"
assert_eq "2" "$RC" "missing --issue exits 2"
run_ccd --issue notanumber --repo o/r --tier tier:maintenance --occupants "#1"
assert_eq "2" "$RC" "non-numeric --issue exits 2"
run_ccd --issue 5 --repo o/r --occupants "#1"
assert_eq "2" "$RC" "missing --tier exits 2"
run_ccd --issue 404 --repo o/r --tier tier:maintenance --occupants "#1"
assert_eq "2" "$RC" "unreadable root issue exits 2"

# =====================================================================
# Doc pins: the Champion prose actually calls the gate
# =====================================================================

echo
echo "--- #9548: an OUTSIDER's capacity marker does not defer ---"
reset_state
issue_fixture_untrusted 'o/r#5' "<!-- champion:capacity-defer:tier:maintenance:$fp_maint -->
champion:capacity-defer-seen: 9"
run_ccd --issue 5 --repo o/r --tier tier:maintenance --occupants "$OCCUPANTS_5"
assert_eq "1" "$RC" "exit 1 - an untrusted author's deferral marker counts as absent (#9548)"
assert_contains "$OUT" "POST_COMMENT" "the untrusted marker does not produce SKIP_COMMENT"
if grep -q "capacity-defer" <<<"$OUT"; then
    fail "the untrusted marker text leaked into the posted comment"
else
    pass "the posted comment quotes no untrusted marker"
fi

echo
echo "--- #9548: a TRUSTED author's capacity marker still defers ---"
reset_state
issue_fixture 'o/r#5' "<!-- champion:capacity-defer:tier:maintenance:$fp_maint -->
champion:capacity-defer-seen: 9"
run_ccd --issue 5 --repo o/r --tier tier:maintenance --occupants "$OCCUPANTS_5"
assert_eq "0" "$RC" "exit 0 - a trusted (OWNER-authored) marker still defers (#9548)"
assert_contains "$OUT" "SKIP_COMMENT" "the trusted marker produces SKIP_COMMENT"

echo
echo "--- #9657: a FLEET APP's capacity marker (live read, REST shape) still dedupes ---"
reset_state
issue_fixture_fleet_app 'o/r#5' "<!-- champion:capacity-defer:tier:maintenance:$fp_maint -->
<!-- champion:capacity-defer-seen:$fp_maint:1 -->"
run_ccd --issue 5 --repo o/r --tier tier:maintenance --occupants "$OCCUPANTS_5"
assert_eq "0" "$RC" "exit 0 - a fleet-App-authored marker (CONTRIBUTOR, [bot]) is believed on the live path"
assert_contains "$OUT" "SKIP_COMMENT" "the fleet App's marker produces SKIP_COMMENT, not a duplicate deferral comment"
run_ccd --issue 5 --repo o/r --tier tier:maintenance --occupants "$OCCUPANTS_5" --apply
assert_eq "0" "$RC" "exit 0 - --apply on the fleet App's marker still skips"
assert_contains "$(patches_log)" "PATCH 1" "--apply bumps the fleet App's own comment"

echo
echo "--- #9657: a FOREIGN App's capacity marker counts as absent ---"
reset_state
issue_fixture_by 'someone-elses-loom[bot]' Bot CONTRIBUTOR 'o/r#5' "<!-- champion:capacity-defer:tier:maintenance:$fp_maint -->"
run_ccd --issue 5 --repo o/r --tier tier:maintenance --occupants "$OCCUPANTS_5"
assert_eq "1" "$RC" "exit 1 - another installation's App is not this fleet's"

echo
echo "--- #9657: --apply PATCHes the trusted comment, never an outsider's later copy of the marker ---"
reset_state
issue_fixture 'o/r#5' "<!-- champion:capacity-defer:tier:maintenance:$fp_maint -->
<!-- champion:capacity-defer-seen:$fp_maint:1 -->"
append_comment 'o/r#5' stranger User NONE "<!-- champion:capacity-defer:tier:maintenance:$fp_maint -->
<!-- champion:capacity-defer-seen:$fp_maint:1 -->"
run_ccd --issue 5 --repo o/r --tier tier:maintenance --occupants "$OCCUPANTS_5" --apply
assert_eq "0" "$RC" "exit 0 - the trusted marker still dedupes"
assert_eq "PATCH 1" "$(patches_log)" "only the trusted comment (id 1) is PATCHed; the outsider's (id 2) is left alone"

echo
echo "--- Doc pins: the Champion prose actually calls the gate ---"

{
    assert_doc_contains "$CHAMPION_PROMO_MD" "classify-capacity-defer.sh" \
        "champion-issue-promo.md invokes the capacity-deferral classifier"
    assert_doc_contains "$CHAMPION_PROMO_MD" "Step 3c: Capacity Deferral" \
        "the capacity-deferral gate has its own named step, alongside Step 3a/3b"
    assert_doc_contains "$CHAMPION_PROMO_MD" "SKIP_COMMENT" \
        "the role prose reads the SKIP_COMMENT outcome"
    assert_doc_contains "$CHAMPION_PROMO_MD" "POST_COMMENT" \
        "the role prose reads the POST_COMMENT outcome"
    assert_doc_contains "$CHAMPION_PROMO_MD" 'champion:capacity-defer-seen' \
        "the seeded seen-counter marker is documented in the posted-comment template"
    assert_doc_contains "$CHAMPION_REF_MD" "classify-capacity-defer.sh" \
        "champion-reference.md's edge case documents the capacity-deferral gate"
    assert_doc_contains "$CHAMPION_REF_MD" "Edge Case 5d" \
        "the capacity-deferral gate has its own named edge case"
}

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
