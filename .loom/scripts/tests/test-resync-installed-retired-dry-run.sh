#!/usr/bin/env bash
# test-resync-installed-retired-dry-run.sh - `--dry-run` previews
# remove_retired_files()'s removals, and the two guards that suppress a removal
# (a .loom/resync-ignore pin, a symlinked destination) suppress the PREVIEW too
# (#8675).
#
# Split out of test-resync-installed.sh (2835 lines, frozen by the file-size
# ratchet — .loom/docs/file-size-policy.md) rather than grown in place. That
# file's group 22 already covers the apply path and the happy-path dry-run
# preview; this file is the dry-run-specific half its group 22 does not have:
# the pin and symlink guards are asserted there only on the APPLY path, so a
# regression that made --dry-run announce `would remove` for a pinned file
# would pass every existing assertion.
#
# Why that half matters, and why it is a contract rather than cosmetics:
# A downstream fleet-management repo's resync self-heal
# (`dirty_is_safe_resync_output`, example-org/fleet-repo#1009) reconciles every
# dirty path in a consumer repo against `resync-installed.sh --dry-run`'s plan
# and SKIPs the repo when a dirty path is unaccounted for. So the dry-run plan
# is a consumed API: a retired file missing from it stalls 23 repos, and a
# pinned file wrongly IN it makes a consumer expect a removal that will never
# come. #8675 suspected the preview was missing entirely; it is not
# (remove_retired_files() has its own `$DRY_RUN` guard), so this suite is the
# regression lock that keeps it that way.
#
# Usage:
#   ./.loom/scripts/tests/test-resync-installed-retired-dry-run.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/resync-installed.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

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
}

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync-retired-dryrun.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# --- fixture builder ---------------------------------------------------------
#
# The live-incident shape: an installed .loom/scripts/retired-tool.sh with NO
# defaults/scripts/retired-tool.sh counterpart at all, named by
# defaults/.loom-retired.list. Nothing else drifts, so every reported line and
# the exit code belong to the retirement and to nothing else.
#
# The commit subject is a routine install subject, not "fixture":
# resync-installed.sh's local-fix guard (#7864) refuses to run over a
# non-routine subject.
make_fixture() {
    local repo="$1"
    rm -rf "$repo"
    mkdir -p "$repo/defaults/scripts" "$repo/.loom/scripts"
    git -C "$repo" init -q
    printf 'scripts/retired-tool.sh   # #8675 test fixture\n' > "$repo/defaults/.loom-retired.list"
    printf '#!/usr/bin/env bash\necho retired\n' > "$repo/.loom/scripts/retired-tool.sh"
    printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
        "$repo" > "$repo/.loom/install-metadata.json"
    git -C "$repo" add -A >/dev/null 2>&1
    git -C "$repo" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1
}

# --- (1) the preview itself --------------------------------------------------
echo "Test group 1: --dry-run previews a retired-file removal without performing it (#8675)"
REPO="$WORKDIR/preview"
make_fixture "$REPO"

OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if [[ $RC -eq 2 ]]; then
    pass "(#8675) a pending retirement is reported as drift (exit 2), not as 'already in sync'"
else
    fail "(#8675) --dry-run with only a retirement pending exited $RC, expected 2 (out=$OUT)"
fi
if grep -q "would remove.*scripts/retired-tool\.sh" <<<"$OUT"; then
    pass "(#8675) the plan names the retired file with the 'would remove' verb"
else
    fail "(#8675) the plan does not mention the retired file at all — a consumer reconciling dirt against it sees an unexplained orphan (out=$OUT)"
fi
if grep -q "retired" <<<"$OUT"; then
    pass "(#8675) the preview line says WHY the file is going away (retired)"
else
    fail "(#8675) the preview line gives no retirement reason (out=$OUT)"
fi
# The count matters as much as the line: the downstream self-heal reads the
# summary to decide whether the plan is empty.
if grep -qE "1 would be removed" <<<"$OUT"; then
    pass "(#8675) the dry-run summary counts the pending removal"
else
    fail "(#8675) the dry-run summary does not count the pending removal (out=$OUT)"
fi
if [[ -f "$REPO/.loom/scripts/retired-tool.sh" ]]; then
    pass "(#8675) --dry-run left the file on disk (preview only)"
else
    fail "(#8675) --dry-run DELETED the retired file — a dry run must not write"
fi
# A preview makes no claim to have run, so it must not clear/leave state either.
if [[ ! -e "$REPO/.loom/.resync-in-progress" ]]; then
    pass "(#8675) --dry-run left no crash-detection marker behind"
else
    fail "(#8675) --dry-run wrote .loom/.resync-in-progress"
fi

# --- (2) a .loom/resync-ignore pin suppresses the PREVIEW, not just the rm ---
echo ""
echo "Test group 2: a .loom/resync-ignore pin suppresses the preview too (#8675)"
PINNED="$WORKDIR/pinned"
make_fixture "$PINNED"
printf 'KEEP-MY-FORK\n' > "$PINNED/.loom/scripts/retired-tool.sh"
printf 'scripts/retired-tool.sh  # I still use this locally\n' > "$PINNED/.loom/resync-ignore"
git -C "$PINNED" add -A >/dev/null 2>&1
git -C "$PINNED" commit -qm "chore: install Loom v0.0.0 (pin)" >/dev/null 2>&1

OUT="$(cd "$PINNED" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if grep -q "skipped.*scripts/retired-tool\.sh" <<<"$OUT"; then
    pass "(#8675) the pinned retired file is reported as skipped under --dry-run"
else
    fail "(#8675) the pinned retired file was not reported as skipped under --dry-run (out=$OUT)"
fi
if grep -q "would remove.*scripts/retired-tool\.sh" <<<"$OUT"; then
    fail "(#8675) --dry-run promised to remove a PINNED file — a consumer would expect a removal that never comes"
else
    pass "(#8675) --dry-run does not promise to remove a pinned file"
fi
if [[ $RC -eq 0 ]]; then
    pass "(#8675) a pin-only run is not reported as drift (exit 0)"
else
    fail "(#8675) a pin-only --dry-run exited $RC, expected 0 (out=$OUT)"
fi
if [[ "$(cat "$PINNED/.loom/scripts/retired-tool.sh" 2>/dev/null)" == "KEEP-MY-FORK" ]]; then
    pass "(#8675) the pinned local fork is untouched"
else
    fail "(#8675) the pinned local fork was modified or removed"
fi

# --- (3) a symlinked destination suppresses the preview too ------------------
#
# The dogfood install shape: .loom/scripts is a symlink into the source tree,
# so removing the destination would unlink a file out from under its own source
# of truth. sync_one refuses that on the update path; remove_retired_files()
# refuses it on the delete path, and --dry-run must not advertise otherwise.
echo ""
echo "Test group 3: a symlinked destination suppresses the preview too (#8675)"
SYMREPO="$WORKDIR/symlinked"
make_fixture "$SYMREPO"
rm -f "$SYMREPO/.loom/scripts/retired-tool.sh"
printf 'SOURCE-OF-TRUTH\n' > "$SYMREPO/source-of-truth.sh"
ln -s "../../source-of-truth.sh" "$SYMREPO/.loom/scripts/retired-tool.sh"
git -C "$SYMREPO" add -A >/dev/null 2>&1
git -C "$SYMREPO" commit -qm "chore: install Loom v0.0.0 (symlink)" >/dev/null 2>&1

OUT="$(cd "$SYMREPO" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if grep -q "skipped.*scripts/retired-tool\.sh" <<<"$OUT"; then
    pass "(#8675) a symlinked retired destination is reported as skipped under --dry-run"
else
    fail "(#8675) a symlinked retired destination was not reported as skipped (out=$OUT)"
fi
if grep -q "would remove.*scripts/retired-tool\.sh" <<<"$OUT"; then
    fail "(#8675) --dry-run promised to unlink a symlinked destination"
else
    pass "(#8675) --dry-run does not promise to unlink a symlinked destination"
fi
if [[ -L "$SYMREPO/.loom/scripts/retired-tool.sh" ]] && \
   [[ "$(cat "$SYMREPO/source-of-truth.sh" 2>/dev/null)" == "SOURCE-OF-TRUTH" ]]; then
    pass "(#8675) the symlink and its target both survive"
else
    fail "(#8675) the symlink or its target was disturbed"
fi

# --- (4) the preview is exactly what the apply then does ---------------------
#
# The plan is only useful to a consumer if it MATCHES. Run the same fixture
# --dry-run and then for real, and assert the apply removes exactly the path
# the preview named, with the same report-relative spelling.
echo ""
echo "Test group 4: the apply removes exactly what the preview named (#8675)"
MATCH="$WORKDIR/match"
make_fixture "$MATCH"
# Anchor on the indented per-file report lines only; the run summary also
# contains the words "removed"/"would be removed" with a count, not a path.
# Take the first match via parameter expansion rather than `| head -1`: piping
# into an early-exit consumer under `pipefail` can SIGPIPE the producer
# (scripts/check-pipefail-early-exit.sh, #7790).
PREVIEW_ALL="$(cd "$MATCH" && bash "$SCRIPT" --dry-run 2>&1 | sed -n 's/^  .*would remove[^ ]*  *\([^ ]*\).*/\1/p')"
APPLIED_ALL="$(cd "$MATCH" && bash "$SCRIPT" 2>&1 | sed -n 's/^  .*removed[^ ]*  *\([^ ]*\).*/\1/p')"
PREVIEW="${PREVIEW_ALL%%$'\n'*}"
APPLIED="${APPLIED_ALL%%$'\n'*}"
if [[ -n "$PREVIEW" && "$PREVIEW" == "$APPLIED" ]]; then
    pass "(#8675) preview and apply name the same path ('$PREVIEW')"
else
    fail "(#8675) preview named '$PREVIEW' but apply removed '$APPLIED'"
fi
if [[ ! -e "$MATCH/.loom/scripts/retired-tool.sh" ]]; then
    pass "(#8675) the apply did remove the file the preview promised"
else
    fail "(#8675) the apply did not remove the previewed file"
fi

# --- summary -----------------------------------------------------------------
echo ""
echo "========================================"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
echo "========================================"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
