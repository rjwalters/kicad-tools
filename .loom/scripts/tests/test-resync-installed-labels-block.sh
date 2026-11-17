#!/usr/bin/env bash
# test-resync-installed-labels-block.sh - resync-installed.sh refreshes the
# Loom-managed block of .github/labels.yml from defaults/.github/labels.yml
# (#11033), hermetic with a fake LOOM_DAEMON_BIN.
#
# A label retired upstream stayed declared in every consumer's managed block,
# so the forge drift check re-created it after `labels migrate --apply` deleted
# it. This pins:
#   1. A retired label (loom:urgent) in the managed block is gone after one
#      resync, a new upstream label arrives, and every byte outside the markers
#      (custom labels above and below) is unchanged. A second run is a no-op.
#   2. No markers: the block is appended, and the old content is kept as is.
#   3. No file: the shipped file is written.
#   4. Malformed markers (a second BEGIN, END before BEGIN, a lone BEGIN, a
#      near-marker line): nothing is written, a warning names the file, exit 1.
#   5. A pin of .github/labels.yml in .loom/resync-ignore skips the step.
#   6. --dry-run reports the update and writes nothing.
#   7. An END marker on the last line with no newline keeps that shape.
#
# Usage: ./.loom/scripts/tests/test-resync-installed-labels-block.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESYNC="$(cd "$SCRIPT_DIR/.." && pwd)/resync-installed.sh"

TESTS_RUN=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo "  PASS: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo "  FAIL: $1"; }

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync-labels.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# Every daemon call (update-gitignore, labels migrate, ...) succeeds silently.
FAKE="$WORKDIR/fake-loom-daemon"
printf '#!/usr/bin/env bash\nexit 0\n' > "$FAKE"
chmod +x "$FAKE"

UPSTREAM='# BEGIN LOOM LABELS
# LOOM-MANAGED BLOCK
- name: loom:issue
  color: "3B82F6"
- name: loom:needs-revision
  color: "D97706"
# END LOOM LABELS
'

STALE_BLOCK='# BEGIN LOOM LABELS
# LOOM-MANAGED BLOCK
- name: loom:issue
  color: "3B82F6"
- name: loom:urgent
  color: "DC2626"
# END LOOM LABELS'

ABOVE='# consumer labels
- name: team:frontend
  color: "00ff00"

'
BELOW='

- name: team:backend   # trailing comment
  color: "0000ff"
'

REPO="$WORKDIR/repo"
LABELS="$REPO/.github/labels.yml"

# make_fixture <labels-content|__none__>
make_fixture() {
    rm -rf "$REPO"
    mkdir -p "$REPO/defaults/scripts" "$REPO/defaults/.github" "$REPO/.loom/scripts" "$REPO/.github"
    git -C "$REPO" init -q
    printf 'S\n' > "$REPO/defaults/scripts/foo.sh"
    chmod +x "$REPO/defaults/scripts/foo.sh"
    printf 'S\n' > "$REPO/.loom/scripts/foo.sh"
    printf '%s' "$UPSTREAM" > "$REPO/defaults/.github/labels.yml"
    [[ "$1" == "__none__" ]] || printf '%s' "$1" > "$LABELS"
    printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
        "$REPO" > "$REPO/.loom/install-metadata.json"
    git -C "$REPO" add -A >/dev/null 2>&1
    git -C "$REPO" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1
}

run() { # run <args...>; sets OUT, RC
    OUT="$(cd "$REPO" && LOOM_DAEMON_BIN="$FAKE" bash "$RESYNC" "$@" 2>&1)"; RC=$?
}

# Exact file bytes (a trailing newline survives the command substitution).
bytes() { local s; s="$(cat "$1"; printf x)"; printf '%s' "${s%x}"; }

echo "Case 1: a retired label leaves the managed block; custom labels are kept"
make_fixture "$ABOVE$STALE_BLOCK$BELOW"
run
want="$ABOVE${UPSTREAM%$'\n'}$BELOW"
got="$(bytes "$LABELS"; printf x)"; got="${got%x}"
if [[ $RC -eq 0 && "$got" == "$want" ]]; then
    pass "block replaced from upstream, bytes outside the markers unchanged"
else fail "rc=$RC out=$OUT
--- got ---
$got"; fi
if ! grep -q 'loom:urgent' "$LABELS" && grep -q 'loom:needs-revision' "$LABELS"; then
    pass "loom:urgent removed, loom:needs-revision added"
else fail "labels: $(cat "$LABELS")"; fi
if [[ "$OUT" == *"updated"*".github/labels.yml"* ]]; then
    pass "the update is reported"
else fail "out=$OUT"; fi
git -C "$REPO" add -A >/dev/null 2>&1; git -C "$REPO" commit -qm "chore: resync" >/dev/null 2>&1
run
got2="$(bytes "$LABELS"; printf x)"; got2="${got2%x}"
if [[ $RC -eq 0 && "$got2" == "$want" && "$OUT" != *"labels.yml"* ]]; then
    pass "a second resync changes nothing"
else fail "rc=$RC out=$OUT"; fi

echo "Case 2: no markers -> the block is appended"
old='- name: team:frontend
  color: "00ff00"'   # no trailing newline
make_fixture "$old"
run
got="$(bytes "$LABELS"; printf x)"; got="${got%x}"
if [[ $RC -eq 0 && "$got" == "$old"$'\n\n'"$UPSTREAM" ]]; then
    pass "appended after a blank line, old content kept as a prefix"
else fail "rc=$RC got=$got"; fi

echo "Case 3: no file -> the shipped file is written"
make_fixture "__none__"
run
got="$(bytes "$LABELS"; printf x)"; got="${got%x}"
if [[ $RC -eq 0 && "$got" == "$UPSTREAM" && "$OUT" == *"created"*".github/labels.yml"* ]]; then
    pass "created from defaults"
else fail "rc=$RC out=$OUT"; fi

check_malformed() { # check_malformed <label> <content>
    make_fixture "$2"
    run
    got="$(bytes "$LABELS"; printf x)"; got="${got%x}"
    if [[ $RC -ne 0 && "$got" == "$2" && "$OUT" == *"WARN"*".github/labels.yml"* ]]; then
        pass "$1: not rewritten, warned, non-zero exit"
    else fail "$1: rc=$RC out=$OUT"; fi
}

echo "Case 4: malformed markers fail safe"
check_malformed "duplicate BEGIN" "$ABOVE# BEGIN LOOM LABELS
$STALE_BLOCK$BELOW"
check_malformed "duplicate block" "$STALE_BLOCK
$STALE_BLOCK
"
check_malformed "END before BEGIN" "# END LOOM LABELS
- name: loom:urgent
# BEGIN LOOM LABELS
"
check_malformed "lone BEGIN" "$ABOVE# BEGIN LOOM LABELS
- name: loom:urgent
"
check_malformed "indented marker" "$ABOVE  # BEGIN LOOM LABELS
- name: loom:urgent
# END LOOM LABELS
"

echo "Case 5: a pinned labels.yml is skipped"
make_fixture "$ABOVE$STALE_BLOCK$BELOW"
printf '.github/labels.yml\n' > "$REPO/.loom/resync-ignore"
git -C "$REPO" add -A >/dev/null 2>&1; git -C "$REPO" commit -qm "pin" >/dev/null 2>&1
run
got="$(bytes "$LABELS"; printf x)"; got="${got%x}"
if [[ $RC -eq 0 && "$got" == "$ABOVE$STALE_BLOCK$BELOW" && "$OUT" == *"labels.yml"*"pinned"* ]]; then
    pass "left as is and reported as pinned"
else fail "rc=$RC out=$OUT"; fi

echo "Case 6: --dry-run writes nothing"
make_fixture "$ABOVE$STALE_BLOCK$BELOW"
run --dry-run
got="$(bytes "$LABELS"; printf x)"; got="${got%x}"
if [[ "$got" == "$ABOVE$STALE_BLOCK$BELOW" && "$OUT" == *"would update"*".github/labels.yml"* ]]; then
    pass "update reported, file untouched"
else fail "rc=$RC out=$OUT"; fi

echo "Case 7: END on the last line with no newline"
make_fixture "$ABOVE$STALE_BLOCK"
run
got="$(bytes "$LABELS"; printf x)"; got="${got%x}"
if [[ $RC -eq 0 && "$got" == "$ABOVE${UPSTREAM%$'\n'}" ]]; then
    pass "block replaced, no newline added after END"
else fail "rc=$RC got=$got"; fi

echo ""
echo "Tests run: $TESTS_RUN, failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
