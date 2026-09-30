#!/usr/bin/env bash
# test-session-mode.sh - Install-time session mode, shell-side contracts (#8884).
#
# Session mode is one persisted marker -- `"mode": "session"` in
# .loom/config.json -- plus the key set derived from it (`terminals: []`,
# `autonomous.roleRunner.enabled: false`, `autonomous.workFinder.enabled:
# false`). The config WRITER is Rust and is covered there
# (loom-daemon/src/init/session_mode{,_init_tests}.rs). This suite covers the
# three shell-side halves that Rust cannot see:
#
#   Group 1 (always runs; subject is SHIPPED): resync-installed.sh leaves a
#     session-mode .loom/config.json byte-for-byte untouched. That is the
#     mechanism by which the marker survives `loom update` -- resync has no
#     "restore the default terminals array" step because it never visits the
#     file at all -- so it needs a regression lock rather than only a comment.
#     A future change that started resyncing config.json would silently re-arm
#     the tmux pool in a repo installed specifically to never have one.
#
#   Group 2 (always runs; subject is SHIPPED): loom-start.sh's existing
#     check_config() still refuses to start on `terminals: []` (the guard #8884
#     deliberately did NOT duplicate), and its hint names session mode as one of
#     the two causes so a session-mode operator is not sent into a reinstall.
#
#   Group 3 (source-tree only): ./install.sh and scripts/install-loom.sh accept
#     `--mode session|default`, reject anything else, and advertise the flag in
#     --help. SKIPped in an installed consumer repo, where those files do not
#     exist (see check-ci-suite-manifest.sh's note for suite authors).
#
# Usage:
#   ./.loom/scripts/tests/test-session-mode.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Resolves to .loom/scripts/ in an installed repo and defaults/scripts/ in the
# source tree, so both shipped subjects below are found in either layout.
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RESYNC="$HELPERS_DIR/resync-installed.sh"
LOOM_START="$HELPERS_DIR/cli/loom-start.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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

skip() { echo -e "  ${YELLOW}SKIP${NC}: $1"; }

for required in "$RESYNC" "$LOOM_START"; do
    if [[ ! -f "$required" ]]; then
        echo "FATAL: subject not found: $required" >&2
        exit 1
    fi
done
if ! command -v jq >/dev/null 2>&1; then
    echo "FATAL: jq is required by this suite's subjects (loom-start.sh check_config)" >&2
    exit 1
fi

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-session-mode.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# A session-mode .loom/config.json exactly as `loom-daemon init --mode session`
# writes it (pretty-printed, trailing newline).
SESSION_CONFIG='{
  "version": "2",
  "terminals": [],
  "mode": "session",
  "autonomous": {
    "roleRunner": {
      "enabled": false
    },
    "workFinder": {
      "enabled": false
    }
  }
}'

# The stock four-terminal shape, reduced to two entries: what a resync must NOT
# restore over the file above.
DEFAULTS_CONFIG='{
  "version": "2",
  "terminals": [
    {"id": "terminal-1", "name": "Judge"},
    {"id": "terminal-2", "name": "Curator"}
  ]
}'

# ============================================================================
# Group 1: resync-installed.sh never restores the default terminals array
# ============================================================================
#
# The fixture deliberately carries REAL drift in a surface resync does own
# (.loom/scripts/drifted.sh), so the run does actual work and reports success.
# A test whose resync was a no-op would pass even if config.json were resynced.
#
# The commit subject is a routine install subject: resync-installed.sh's
# local-fix guard (#7864) refuses to run over a non-routine one.
echo "Test group 1: resync leaves a session-mode .loom/config.json untouched (#8884)"
REPO="$WORKDIR/resync"
mkdir -p "$REPO/defaults/scripts" "$REPO/.loom/scripts"
git -C "$REPO" init -q
printf '#!/usr/bin/env bash\necho NEW\n' > "$REPO/defaults/scripts/drifted.sh"
printf '#!/usr/bin/env bash\necho OLD\n' > "$REPO/.loom/scripts/drifted.sh"
printf '%s\n' "$DEFAULTS_CONFIG" > "$REPO/defaults/config.json"
printf '%s\n' "$SESSION_CONFIG" > "$REPO/.loom/config.json"
printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
    "$REPO" > "$REPO/.loom/install-metadata.json"
git -C "$REPO" add -A >/dev/null 2>&1
git -C "$REPO" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1

BEFORE_SHA="$(git -C "$REPO" hash-object "$REPO/.loom/config.json")"
OUT="$(cd "$REPO" && bash "$RESYNC" 2>&1)"
AFTER_SHA="$(git -C "$REPO" hash-object "$REPO/.loom/config.json")"

if [[ "$BEFORE_SHA" == "$AFTER_SHA" ]]; then
    pass "(#8884) .loom/config.json is byte-for-byte unchanged by a resync"
else
    fail "(#8884) resync REWROTE .loom/config.json — the session-mode marker is not durable (out=$OUT)"
fi
if [[ "$(jq -c '.terminals' "$REPO/.loom/config.json")" == "[]" ]]; then
    pass "(#8884) terminals is still [] — the default array was not restored"
else
    fail "(#8884) resync restored a terminals array: $(jq -c '.terminals' "$REPO/.loom/config.json")"
fi
if [[ "$(jq -r '.mode' "$REPO/.loom/config.json")" == "session" ]]; then
    pass "(#8884) the \"mode\": \"session\" marker survived the resync"
else
    fail "(#8884) the mode marker did not survive the resync"
fi
if [[ "$(jq -r '.autonomous.workFinder.enabled' "$REPO/.loom/config.json")" == "false" ]]; then
    pass "(#8884) the daemon-tier work-generator flags survived the resync"
else
    fail "(#8884) the autonomous.* flags did not survive the resync"
fi
# Proof the run was not a no-op: the drifted surface really was refreshed.
if grep -q NEW "$REPO/.loom/scripts/drifted.sh"; then
    pass "(#8884) the resync actually did work (a drifted surface was refreshed)"
else
    fail "(#8884) the resync refreshed nothing, so the assertions above prove nothing (out=$OUT)"
fi
if ! grep -q "config\.json" <<<"$OUT"; then
    pass "(#8884) the resync report does not mention config.json at all"
else
    fail "(#8884) the resync report mentions config.json (out=$OUT)"
fi

# ============================================================================
# Group 2: loom-start.sh's EXISTING refusal still fires, and its hint names
#          session mode as one of the two causes
# ============================================================================
#
# #8884 deliberately added no new "refuse to start" guard: check_config() has
# refused on an empty `terminals` array since the config-tiering work. This
# group is the regression lock on that (the feature depends on it) plus the one
# thing #8884 did change there — the hint, because "have you initialized Loom?"
# ALONE would send a session-mode operator into a needless reinstall.
echo ""
echo "Test group 2: loom-start.sh refuses to start, and says WHY (#8884)"

# Drive check_config() directly: sourcing loom-start.sh with no arguments would
# run its main dispatch. This keeps the test on the function under test.
run_check_config() {
    local repo="$1"
    (
        cd "$repo" || exit 99
        # shellcheck disable=SC2030
        export LOOM_START_TEST_REPO_ROOT="$repo"
        bash -c '
            set -uo pipefail
            REPO_ROOT="$LOOM_START_TEST_REPO_ROOT"
            RED=""; NC=""
            # shellcheck source=/dev/null
            source "'"$HELPERS_DIR"'/lib/config-resolver.sh"
            # Re-declare only the two helpers check_config() depends on that
            # loom-start.sh defines above it, then the function itself, by
            # extracting them from the real script — so this test exercises the
            # shipped implementation, not a copy.
            eval "$(sed -n "/^_loom_start_config_tiers()/,/^}/p" "'"$LOOM_START"'")"
            eval "$(sed -n "/^check_config()/,/^}/p" "'"$LOOM_START"'")"
            check_config
        ' 2>&1
    )
}

SESSION_REPO="$WORKDIR/session-start"
mkdir -p "$SESSION_REPO/.loom"
printf '%s\n' "$SESSION_CONFIG" > "$SESSION_REPO/.loom/config.json"
OUT="$(run_check_config "$SESSION_REPO")"
RC=$?
if [[ $RC -ne 0 ]]; then
    pass "(#8884) check_config() still refuses to start on terminals: [] (exit $RC)"
else
    fail "(#8884) check_config() ACCEPTED an empty terminals array — session mode has no guard left"
fi
if grep -q 'No Loom config with a non-empty' <<<"$OUT"; then
    pass "(#8884) the pre-existing refusal message is unchanged (regression, not new behavior)"
else
    fail "(#8884) the pre-existing refusal message changed (out=$OUT)"
fi
if grep -q 'session-mode.md' <<<"$OUT"; then
    pass "(#8884) the refusal points a session-mode operator at the session-mode doc"
else
    fail "(#8884) the refusal does not mention session mode at all (out=$OUT)"
fi
if grep -q 'install-loom.sh' <<<"$OUT"; then
    pass "(#8884) the refusal still names the not-installed cause too (both causes stated)"
else
    fail "(#8884) the refusal dropped the 'have you installed Loom?' cause (out=$OUT)"
fi

# The hint names BOTH causes unconditionally rather than branching on
# `.mode == "session"`: loom-start.sh is `contract` shell in epic #7810's
# portable pool, which may not grow, so a static line that costs zero code lines
# is preferred over a 7-line conditional (see the comment at the refusal). The
# consequence under test: a repo with no session marker at all still gets the
# original advice, and the session-mode pointer is present but not a claim ABOUT
# this repo -- session mode is never INFERRED from an empty terminals array,
# because nothing here reads the marker.
PLAIN_REPO="$WORKDIR/plain-start"
mkdir -p "$PLAIN_REPO/.loom"
printf '{"version": "2", "terminals": []}\n' > "$PLAIN_REPO/.loom/config.json"
OUT="$(run_check_config "$PLAIN_REPO")"
if grep -q 'Have you initialized Loom' <<<"$OUT"; then
    pass "(#8884) a non-session repo keeps the original 'have you initialized Loom?' advice"
else
    fail "(#8884) the original advice was lost for a non-session repo (out=$OUT)"
fi
if ! grep -q 'jq -r .*\.mode' "$LOOM_START"; then
    pass "(#8884) loom-start.sh adds no \`.mode\` branch (epic #7810: portable pool may not grow)"
else
    fail "(#8884) loom-start.sh grew a session-mode conditional — that is portable-pool growth"
fi

# ============================================================================
# Group 3: the installers' --mode flag (source-tree only)
# ============================================================================
echo ""
echo "Test group 3: ./install.sh and scripts/install-loom.sh accept --mode (#8884)"
REPO_ROOT_CANDIDATE="$(cd "$HELPERS_DIR/../.." && pwd)"
INSTALL_SH="$REPO_ROOT_CANDIDATE/install.sh"
INSTALL_LOOM_SH="$REPO_ROOT_CANDIDATE/scripts/install-loom.sh"

if [[ ! -f "$INSTALL_SH" || ! -f "$INSTALL_LOOM_SH" ]]; then
    skip "source-tree-only test, install.sh / scripts/install-loom.sh not found (not shipped into an installed repo)"
else
    for subject in "$INSTALL_SH" "$INSTALL_LOOM_SH"; do
        name="$(basename "$subject")"
        HELP="$(bash "$subject" --help 2>&1)"
        if grep -q -- '--mode session|default' <<<"$HELP"; then
            pass "(#8884) $name --help advertises --mode session|default"
        else
            fail "(#8884) $name --help does not advertise the flag (a flag nobody can discover)"
        fi
        if grep -q 'session-mode.md' <<<"$HELP"; then
            pass "(#8884) $name --help points at the session-mode doc"
        else
            fail "(#8884) $name --help does not point at defaults/docs/session-mode.md"
        fi

        # An invalid value must be rejected during argument parsing — BEFORE any
        # build, uninstall, or write — so a typo costs a second, not a
        # half-finished install.
        OUT="$(bash "$subject" --mode sesion "$WORKDIR" 2>&1)"
        RC=$?
        if [[ $RC -ne 0 ]] && grep -qi 'mode' <<<"$OUT"; then
            pass "(#8884) $name rejects an invalid --mode value (exit $RC)"
        else
            fail "(#8884) $name accepted --mode sesion (rc=$RC, out=$OUT)"
        fi
        OUT="$(bash "$subject" --mode 2>&1)"
        RC=$?
        if [[ $RC -ne 0 ]]; then
            pass "(#8884) $name rejects a bare --mode with no value (exit $RC)"
        else
            fail "(#8884) $name accepted a bare --mode with no value"
        fi
    done

    # The flag has to reach `loom-daemon init` to mean anything. Assert the argv
    # splice at every init call site rather than only at the parse site: the
    # #8884 failure mode is a flag that parses fine and is then dropped.
    for subject in "$INSTALL_SH" "$INSTALL_LOOM_SH"; do
        name="$(basename "$subject")"
        INIT_CALLS="$(grep -c 'loom-daemon" init' "$subject" || true)"
        SPLICED="$(grep -c 'loom-daemon" init.*MODE_FLAGS' "$subject" || true)"
        if [[ "$INIT_CALLS" -gt 0 && "$INIT_CALLS" -eq "$SPLICED" ]]; then
            pass "(#8884) $name forwards --mode at all $INIT_CALLS 'loom-daemon init' call site(s)"
        else
            fail "(#8884) $name has $INIT_CALLS 'loom-daemon init' call site(s) but only $SPLICED forward MODE_FLAGS"
        fi
    done
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
