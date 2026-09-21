#!/usr/bin/env bash
# test-locate-daemon-bin.sh — Tests for lib/locate-daemon-bin.sh's shared
# loom_locate_daemon_bin() / loom_daemon_bin_search_paths() (Issue #4875).
#
# Focus: a non-interactive `ssh host 'cmd'` invocation does not source the
# login profile, so `~/.local/bin` (the epic #3835 Phase 3a machine-level
# install location) is NOT on $PATH. Before this fix, `loom-daemon-start.sh`
# and its sibling scripts (loom-daemon-watchdog.sh, loom-daemon-update.sh,
# loom-status.sh, `.loom/bin/loom health`) gave up in that exact scenario even
# though a current binary sat at `~/.local/bin/loom-daemon`. This suite drives
# the shared resolver directly (all five call sites now delegate to it) with
# $PATH reduced to a minimal, non-interactive default and no $LOOM_DAEMON_BIN,
# asserting the binary is still found.
#
# Since #8134 it also covers the OTHER resolution this library owns —
# loom_daemon_self_bin_override / $LOOM_DAEMON_SELF_BIN, "the binary that
# IMPLEMENTS this caller" — including end-to-end through a real Shape-A stub,
# because the two resolutions answer different questions and the bug was one
# silently answering for the other (cases 17-21).
#
# Style matches the other lib-focused suites — plain bash, hand-rolled
# assertions, no Bats.
#
# Usage:
#   ./defaults/scripts/tests/test-locate-daemon-bin.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$SCRIPT_DIR/../lib/locate-daemon-bin.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "${GREEN}✓${NC} $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "${RED}✗${NC} $1"; }

assert_eq() { # <expected> <actual> <msg>
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$1', got '$2')"; fi
}

assert_contains() { # <needle> <haystack> <msg>
    if [[ "$2" == *"$1"* ]]; then pass "$3"; else fail "$3 (expected to find '$1' in [$2])"; fi
}

if [[ ! -r "$LIB" ]]; then
    echo -e "${RED}FATAL${NC}: $LIB not found" >&2
    exit 1
fi

# A minimal, non-interactive-shell-like PATH: no ~/.local/bin, no repo-local
# toolchain dirs, just the base system directories a non-login `ssh host
# 'cmd'` session would inherit.
MINIMAL_PATH="/usr/bin:/bin"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-locate-daemon-bin.XXXXXX")"
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

make_fake_bin() { # <path>
    mkdir -p "$(dirname "$1")"
    cat > "$1" <<'EOF'
#!/usr/bin/env bash
echo "fake-loom-daemon"
EOF
    chmod +x "$1"
}

# ---------- 1. $LOOM_DAEMON_BIN wins over everything, even a bogus PATH ----------
BIN1="$WORKDIR/t1/explicit-loom-daemon"
make_fake_bin "$BIN1"
out=$( env -i PATH="$MINIMAL_PATH" HOME="$WORKDIR/t1-nohome" LOOM_DAEMON_BIN="$BIN1" \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$WORKDIR/t1-root'" )
assert_eq "$BIN1" "$out" "LOOM_DAEMON_BIN override wins"

# ---------- 2. LOOM_DAEMON_BIN set but NOT executable falls through (does not
#               hard-fail) to the remaining candidates ----------
BOGUS_BIN="$WORKDIR/t2/does-not-exist"
BIN2_HOME="$WORKDIR/t2-home"
BIN2="$BIN2_HOME/.local/bin/loom-daemon"
make_fake_bin "$BIN2"
out=$( env -i PATH="$MINIMAL_PATH" HOME="$BIN2_HOME" LOOM_DAEMON_BIN="$BOGUS_BIN" \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$WORKDIR/t2-root'" )
assert_eq "$BIN2" "$out" "non-executable LOOM_DAEMON_BIN falls through to machine-level install, not a hard failure"

# ---------- 3. `loom-daemon` on PATH is found (no LOOM_DAEMON_BIN needed) ----------
PATH_BIN_DIR="$WORKDIR/t3/on-path"
make_fake_bin "$PATH_BIN_DIR/loom-daemon"
out=$( env -i PATH="$PATH_BIN_DIR:$MINIMAL_PATH" HOME="$WORKDIR/t3-nohome" \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$WORKDIR/t3-root'" )
assert_eq "$PATH_BIN_DIR/loom-daemon" "$out" "loom-daemon on \$PATH is found"

# ---------- 4. THE CORE FIX (#4875): PATH reduced to a minimal, non-login
#               default (no ~/.local/bin, no LOOM_DAEMON_BIN) still finds the
#               machine-level install under $HOME/.local/bin -- exactly the
#               `ssh host 'loom-daemon-start.sh --from-config'` scenario. ----------
SSH_HOME="$WORKDIR/t4-ssh-home"
SSH_BIN="$SSH_HOME/.local/bin/loom-daemon"
make_fake_bin "$SSH_BIN"
out=$( env -i PATH="$MINIMAL_PATH" HOME="$SSH_HOME" \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$WORKDIR/t4-root'" )
assert_eq "$SSH_BIN" "$out" "non-interactive-SSH-like minimal \$PATH + no LOOM_DAEMON_BIN still finds \$HOME/.local/bin/loom-daemon (#4875)"

# ---------- 5. $LOOM_DAEMON_BIN_DIR overrides the default ~/.local/bin dir ----------
CUSTOM_DIR="$WORKDIR/t5-custom-install-dir"
CUSTOM_BIN="$CUSTOM_DIR/loom-daemon"
make_fake_bin "$CUSTOM_BIN"
DECOY_HOME="$WORKDIR/t5-decoy-home"
mkdir -p "$DECOY_HOME"
out=$( env -i PATH="$MINIMAL_PATH" HOME="$DECOY_HOME" LOOM_DAEMON_BIN_DIR="$CUSTOM_DIR" \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$WORKDIR/t5-root'" )
assert_eq "$CUSTOM_BIN" "$out" "\$LOOM_DAEMON_BIN_DIR overrides the default ~/.local/bin install dir"

# ---------- 6. in-repo build-output candidates are still the last resort ----------
REPO6="$WORKDIR/t6-repo"
make_fake_bin "$REPO6/target/release/loom-daemon"
out=$( env -i PATH="$MINIMAL_PATH" HOME="$WORKDIR/t6-nohome" \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$REPO6'" )
assert_eq "$REPO6/target/release/loom-daemon" "$out" "in-repo target/release/loom-daemon is still found when nothing else resolves"

# ---------- 7. nothing resolvable -> empty output, no error ----------
out=$( env -i PATH="$MINIMAL_PATH" HOME="$WORKDIR/t7-nohome" \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$WORKDIR/t7-root'" )
assert_eq "" "$out" "nothing resolvable -> empty output (caller reports not-found itself)"

# ---------- 8. loom_daemon_bin_search_paths() names the machine-level
#               install location so a "not found" error can list it ----------
paths_out=$( env -i PATH="$MINIMAL_PATH" HOME="$WORKDIR/t8-home" \
    bash -c "source '$LIB'; loom_daemon_bin_search_paths '$WORKDIR/t8-root'" )
assert_contains "$WORKDIR/t8-home/.local/bin/loom-daemon" "$paths_out" "search-paths summary names the machine-level ~/.local/bin install location"
assert_contains "$WORKDIR/t8-root/target/release/loom-daemon" "$paths_out" "search-paths summary still names the in-repo build-output candidates"

# ---------- 9. resolving a binary logs its path on stderr (#4997 AC1) ----------
# The diagnostic line must not contaminate stdout (callers capture stdout via
# command substitution to get the path itself).
BIN9_HOME="$WORKDIR/t9-home"
BIN9="$BIN9_HOME/.local/bin/loom-daemon"
make_fake_bin "$BIN9"
stdout_out=$( env -i PATH="$MINIMAL_PATH" HOME="$BIN9_HOME" \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$WORKDIR/t9-root'" 2>"$WORKDIR/t9-stderr" )
stderr_out="$(cat "$WORKDIR/t9-stderr")"
assert_eq "$BIN9" "$stdout_out" "stdout still carries only the resolved path (diagnostics do not contaminate it)"
assert_contains "$BIN9" "$stderr_out" "stderr names the resolved binary path (#4997 AC1)"
assert_contains "machine-level install" "$stderr_out" "stderr names which precedence tier the binary was resolved from"

# ---------- 9b. LOOM_LOCATE_DAEMON_BIN_QUIET=1 suppresses the resolution
#                trace for a single call, without affecting the resolved
#                path on stdout (#6392) ----------
BIN9B_HOME="$WORKDIR/t9b-home"
BIN9B="$BIN9B_HOME/.local/bin/loom-daemon"
make_fake_bin "$BIN9B"
stdout_out=$( env -i PATH="$MINIMAL_PATH" HOME="$BIN9B_HOME" LOOM_LOCATE_DAEMON_BIN_QUIET=1 \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$WORKDIR/t9b-root'" 2>"$WORKDIR/t9b-stderr" )
stderr_out="$(cat "$WORKDIR/t9b-stderr")"
assert_eq "$BIN9B" "$stdout_out" "LOOM_LOCATE_DAEMON_BIN_QUIET=1 still resolves the correct path on stdout"
assert_eq "" "$stderr_out" "LOOM_LOCATE_DAEMON_BIN_QUIET=1 suppresses the resolution-trace stderr line (#6392)"

# Default (unset) behavior is unchanged -- the trace still prints when the
# quiet opt-in is not requested, so existing callers see no behavior change.
BIN9C_HOME="$WORKDIR/t9c-home"
BIN9C="$BIN9C_HOME/.local/bin/loom-daemon"
make_fake_bin "$BIN9C"
stderr_out=$( env -i PATH="$MINIMAL_PATH" HOME="$BIN9C_HOME" \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$WORKDIR/t9c-root'" 2>&1 >/dev/null )
assert_contains "loom_locate_daemon_bin: resolved" "$stderr_out" "the resolution trace still prints by default (LOOM_LOCATE_DAEMON_BIN_QUIET unset -- no behavior change for existing callers)"

# ---------- 10. $LOOM_PREFER_REPO_BUILD=1 hoists the repo-local build above
#                the machine-level install and $PATH (#4997) ----------
REPO10="$WORKDIR/t10-repo"
make_fake_bin "$REPO10/target/release/loom-daemon"
PATH10_DIR="$WORKDIR/t10-on-path"
make_fake_bin "$PATH10_DIR/loom-daemon"
HOME10="$WORKDIR/t10-home"
make_fake_bin "$HOME10/.local/bin/loom-daemon"

# Without the opt-in, $PATH still wins (unchanged default precedence).
out=$( env -i PATH="$PATH10_DIR:$MINIMAL_PATH" HOME="$HOME10" \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$REPO10'" 2>/dev/null )
assert_eq "$PATH10_DIR/loom-daemon" "$out" "without LOOM_PREFER_REPO_BUILD, \$PATH still wins over the fresh repo build (default precedence unchanged)"

# With the opt-in, the repo-local build wins over both $PATH and the
# machine-level install, even though both of those also resolve.
out=$( env -i PATH="$PATH10_DIR:$MINIMAL_PATH" HOME="$HOME10" LOOM_PREFER_REPO_BUILD=1 \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$REPO10'" 2>"$WORKDIR/t10-stderr" )
stderr_out="$(cat "$WORKDIR/t10-stderr")"
assert_eq "$REPO10/target/release/loom-daemon" "$out" "LOOM_PREFER_REPO_BUILD=1 hoists the fresh repo-local build above \$PATH and the machine-level install"
assert_contains "LOOM_PREFER_REPO_BUILD" "$stderr_out" "stderr names LOOM_PREFER_REPO_BUILD as the provenance when the opt-in fires"

# ---------- 11. #4875 regression still holds under LOOM_PREFER_REPO_BUILD=1:
#                a non-login SSH session with no repo-local build present and
#                no $PATH entry still finds the machine-level install ----------
SSH11_HOME="$WORKDIR/t11-ssh-home"
SSH11_BIN="$SSH11_HOME/.local/bin/loom-daemon"
make_fake_bin "$SSH11_BIN"
out=$( env -i PATH="$MINIMAL_PATH" HOME="$SSH11_HOME" LOOM_PREFER_REPO_BUILD=1 \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$WORKDIR/t11-root'" 2>/dev/null )
assert_eq "$SSH11_BIN" "$out" "LOOM_PREFER_REPO_BUILD=1 does not break the #4875 non-login-\$PATH ssh case when no repo build exists"

# ---------- 12. LOCKSTEP: loom_locate_daemon_bin must always agree with the
#                first existing candidate in loom_daemon_bin_search_paths's
#                own ordering (#4997 AC2 -- fails if the two functions
#                disagree on precedence, in either default or
#                LOOM_PREFER_REPO_BUILD=1 mode). ----------
assert_lockstep() { # <label> <extra_env...=value...> -- reads globals REPO/HOME/PATH via env args
    local label="$1"; shift
    local resolved first_existing
    resolved=$(env -i "$@" LOCKSTEP_LIB="$LIB" bash -c 'source "$LOCKSTEP_LIB"; loom_locate_daemon_bin "$LOCKSTEP_ROOT"' 2>/dev/null)
    # Resolve loom_daemon_bin_search_paths's own ordering to real, checkable
    # filesystem paths in the SAME environment (so a "loom-daemon on \$PATH"
    # descriptive line resolves via that env's own $PATH, not the test
    # runner's), then walk it to find the first one that actually exists.
    first_existing=$(env -i "$@" LOCKSTEP_LIB="$LIB" bash -c '
        source "$LOCKSTEP_LIB"
        while IFS= read -r line; do
            path="${line%% (*}"
            path="${path#\$LOOM_DAEMON_BIN=}"
            if [[ "$path" == "loom-daemon on \$PATH" ]]; then
                path="$(command -v loom-daemon 2>/dev/null || true)"
            fi
            if [[ -n "$path" && -x "$path" ]]; then echo "$path"; break; fi
        done <<< "$(loom_daemon_bin_search_paths "$LOCKSTEP_ROOT")"
    ')
    assert_eq "$first_existing" "$resolved" "$label: loom_locate_daemon_bin agrees with loom_daemon_bin_search_paths's first existing candidate"
}

LOCK12_ROOT="$WORKDIR/t12-repo"
make_fake_bin "$LOCK12_ROOT/target/release/loom-daemon"
LOCK12_PATH_DIR="$WORKDIR/t12-on-path"
make_fake_bin "$LOCK12_PATH_DIR/loom-daemon"
LOCK12_HOME="$WORKDIR/t12-home"
make_fake_bin "$LOCK12_HOME/.local/bin/loom-daemon"

assert_lockstep "default precedence" \
    PATH="$LOCK12_PATH_DIR:$MINIMAL_PATH" HOME="$LOCK12_HOME" LOCKSTEP_ROOT="$LOCK12_ROOT"
assert_lockstep "LOOM_PREFER_REPO_BUILD=1 precedence" \
    PATH="$LOCK12_PATH_DIR:$MINIMAL_PATH" HOME="$LOCK12_HOME" LOCKSTEP_ROOT="$LOCK12_ROOT" LOOM_PREFER_REPO_BUILD=1
assert_lockstep "default precedence, nothing but the machine install resolves" \
    PATH="$MINIMAL_PATH" HOME="$LOCK12_HOME" LOCKSTEP_ROOT="$WORKDIR/t12-empty-root"
assert_lockstep "LOOM_PREFER_REPO_BUILD=1, no repo build present falls through to machine install" \
    PATH="$MINIMAL_PATH" HOME="$LOCK12_HOME" LOCKSTEP_ROOT="$WORKDIR/t12-empty-root" LOOM_PREFER_REPO_BUILD=1

# ---------- 13. REGRESSION (#6208): a repo-local build under a redirected
#                $CARGO_TARGET_DIR is still discovered when no other
#                candidate (LOOM_DAEMON_BIN, $PATH, machine-level install)
#                is present. Mirrors test 80 in test-loom-daemon-update.sh
#                (#6160/#6209), which fixed the same class of bug for the
#                build-verification path; this is the discovery-only
#                counterpart. Before this fix, only the four hardcoded
#                <repo>/loom-daemon/target and <repo>/target paths were
#                probed, so a build that landed entirely outside the repo
#                tree (as CARGO_TARGET_DIR redirects typically do) was never
#                found even though it existed and was executable. ----------
ROOT13="$WORKDIR/t13-repo"
REDIRECT13="$WORKDIR/t13-redirected-cargo-target"
make_fake_bin "$REDIRECT13/release/loom-daemon"
out=$( env -i PATH="$MINIMAL_PATH" HOME="$WORKDIR/t13-nohome" CARGO_TARGET_DIR="$REDIRECT13" \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$ROOT13'" 2>"$WORKDIR/t13-stderr" )
assert_eq "$REDIRECT13/release/loom-daemon" "$out" \
    "a repo-local build under a redirected \$CARGO_TARGET_DIR is discovered when nothing else resolves (#6208)"
stderr_out="$(cat "$WORKDIR/t13-stderr")"
assert_contains "repo-local build" "$stderr_out" "stderr still names the repo-local-build provenance tier for a \$CARGO_TARGET_DIR-redirected find"

# ---------- 14. REGRESSION (#6208): a build redirected via
#                ~/.cargo/config.toml's build.target-dir (NOT the
#                $CARGO_TARGET_DIR env var -- only `cargo metadata` itself
#                can see that redirect) is still discovered, keyed off a
#                loom-daemon/Cargo.toml manifest. ----------
ROOT14="$WORKDIR/t14-repo"
mkdir -p "$ROOT14/loom-daemon"
touch "$ROOT14/loom-daemon/Cargo.toml"
REDIRECT14="$WORKDIR/t14-redirected-config-target"
make_fake_bin "$REDIRECT14/release/loom-daemon"

# A fake `cargo` that only answers `metadata --format-version 1 --no-deps
# --manifest-path <root>/loom-daemon/Cargo.toml` with the redirected
# target_directory -- anything else is an error (proves the real subcommand
# invocation shape is exactly what's expected, not just "any cargo call").
FAKE_CARGO_DIR14="$WORKDIR/t14-fake-cargo-bin"
mkdir -p "$FAKE_CARGO_DIR14"
cat > "$FAKE_CARGO_DIR14/cargo" <<EOF
#!/usr/bin/env bash
if [[ "\$1" == "metadata" ]]; then
    echo '{"target_directory":"$REDIRECT14"}'
    exit 0
fi
echo "[fake cargo] unsupported: \$*" >&2
exit 1
EOF
chmod +x "$FAKE_CARGO_DIR14/cargo"

out=$( env -i PATH="$FAKE_CARGO_DIR14:$MINIMAL_PATH" HOME="$WORKDIR/t14-nohome" \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$ROOT14'" 2>"$WORKDIR/t14-stderr" )
assert_eq "$REDIRECT14/release/loom-daemon" "$out" \
    "a build.target-dir redirect (no \$CARGO_TARGET_DIR set) is discovered via 'cargo metadata', keyed off loom-daemon/Cargo.toml (#6208)"
stderr_out="$(cat "$WORKDIR/t14-stderr")"
assert_contains "repo-local build" "$stderr_out" "stderr still names the repo-local-build provenance tier for a cargo-metadata-resolved find"

# ---------- 15. $CARGO_TARGET_DIR (cheap, no subprocess) is checked BEFORE
#                falling back to 'cargo metadata' -- a poisoned fake cargo
#                that fails if invoked at all must never be called when
#                $CARGO_TARGET_DIR alone already resolves the binary. ----------
ROOT15="$WORKDIR/t15-repo"
mkdir -p "$ROOT15/loom-daemon"
touch "$ROOT15/loom-daemon/Cargo.toml"
REDIRECT15="$WORKDIR/t15-redirected-cargo-target"
make_fake_bin "$REDIRECT15/release/loom-daemon"
POISON_MARKER15="$WORKDIR/t15-cargo-was-called"
POISON_CARGO_DIR15="$WORKDIR/t15-poison-cargo-bin"
mkdir -p "$POISON_CARGO_DIR15"
cat > "$POISON_CARGO_DIR15/cargo" <<EOF
#!/usr/bin/env bash
touch "$POISON_MARKER15"
exit 1
EOF
chmod +x "$POISON_CARGO_DIR15/cargo"

out=$( env -i PATH="$POISON_CARGO_DIR15:$MINIMAL_PATH" HOME="$WORKDIR/t15-nohome" CARGO_TARGET_DIR="$REDIRECT15" \
    bash -c "source '$LIB'; loom_locate_daemon_bin '$ROOT15'" 2>/dev/null )
assert_eq "$REDIRECT15/release/loom-daemon" "$out" \
    "\$CARGO_TARGET_DIR resolves the binary even with a loom-daemon/Cargo.toml present and cargo on \$PATH"
if [[ -e "$POISON_MARKER15" ]]; then
    fail "cargo metadata is NOT invoked when \$CARGO_TARGET_DIR alone already resolves the binary (poison marker was created)"
else
    pass "cargo metadata is NOT invoked when \$CARGO_TARGET_DIR alone already resolves the binary"
fi

# ---------- 16. LOCKSTEP (#6208): loom_daemon_bin_search_paths must list the
#                same $CARGO_TARGET_DIR-redirected candidate
#                loom_locate_daemon_bin actually resolves. ----------
assert_lockstep "\$CARGO_TARGET_DIR-redirected repo-local build" \
    PATH="$MINIMAL_PATH" HOME="$WORKDIR/t16-nohome" CARGO_TARGET_DIR="$REDIRECT13" LOCKSTEP_ROOT="$ROOT13"

# ===========================================================================
# 17-21 (#8134): $LOOM_DAEMON_BIN and $LOOM_DAEMON_SELF_BIN mean DIFFERENT
# binaries, and a Shape-A stub must resolve the second one.
#
# $LOOM_DAEMON_BIN = the daemon a caller manages or PROBES (an installed
# release whose version is compared; the endpoint loom-daemon-watchdog.sh
# round-trips; a deliberately fake binary in a suite). $LOOM_DAEMON_SELF_BIN =
# the daemon that IMPLEMENTS the caller. The watchdog is the first script that
# is both a stub and a daemon-invoker, and its retained suite pins a HANGING
# mock through LOOM_DAEMON_BIN: before this split the stub exec'd that mock as
# the watchdog and the suite hung rather than failed.
# ===========================================================================

# ---------- 17. the override helper: hit, miss, and set-but-not-executable ----------
BIN17="$WORKDIR/t17/self-loom-daemon"
make_fake_bin "$BIN17"
out=$( env -i PATH="$MINIMAL_PATH" HOME="$WORKDIR/t17-nohome" LOOM_DAEMON_SELF_BIN="$BIN17" \
    bash -c "source '$LIB'; loom_daemon_self_bin_override" )
assert_eq "$BIN17" "$out" "loom_daemon_self_bin_override echoes an executable \$LOOM_DAEMON_SELF_BIN"

out=$( env -i PATH="$MINIMAL_PATH" HOME="$WORKDIR/t17-nohome" \
    bash -c "source '$LIB'; loom_daemon_self_bin_override; echo \"rc=\$?\"" )
assert_eq "rc=1" "$out" "loom_daemon_self_bin_override returns 1 (and prints nothing) when \$LOOM_DAEMON_SELF_BIN is unset"

out=$( env -i PATH="$MINIMAL_PATH" HOME="$WORKDIR/t17-nohome" LOOM_DAEMON_SELF_BIN="$WORKDIR/t17/not-a-binary" \
    bash -c "source '$LIB'; loom_daemon_self_bin_override; echo \"rc=\$?\"" )
assert_eq "rc=1" "$out" "loom_daemon_self_bin_override returns 1 for a set-but-not-executable \$LOOM_DAEMON_SELF_BIN"

# ---------- 18. loom_resolve_self_daemon_bin still honours the same tier 1
#                (it now delegates to the helper, so the two cannot drift) ----------
out=$( env -i PATH="$MINIMAL_PATH" HOME="$WORKDIR/t18-nohome" LOOM_DAEMON_SELF_BIN="$BIN17" \
    bash -c "source '$LIB'; loom_resolve_self_daemon_bin" )
assert_eq "$BIN17" "$out" "loom_resolve_self_daemon_bin still resolves \$LOOM_DAEMON_SELF_BIN first (#8037 behaviour preserved)"

# ---------- 19. THE #8134 FIX, end to end through a real Shape-A stub: with
#                LOOM_DAEMON_BIN pointing at a probe MOCK and
#                LOOM_DAEMON_SELF_BIN at the implementation, the stub execs the
#                IMPLEMENTATION. ----------
STUB="$(cd "$SCRIPT_DIR/.." && pwd)/strip-ansi.sh"
if [[ ! -x "$STUB" ]]; then
    echo -e "${RED}FATAL${NC}: stub $STUB not found" >&2
    exit 1
fi

SELF19="$WORKDIR/t19/impl/loom-daemon"
mkdir -p "$(dirname "$SELF19")"
cat > "$SELF19" <<'EOF'
#!/usr/bin/env bash
echo "IMPL invoked: $*"
EOF
chmod +x "$SELF19"

MOCK19="$WORKDIR/t19/mock/loom-daemon-mock"
mkdir -p "$(dirname "$MOCK19")"
cat > "$MOCK19" <<'EOF'
#!/usr/bin/env bash
echo "MOCK invoked: $*"
EOF
chmod +x "$MOCK19"

out=$( env -i PATH="$MINIMAL_PATH" HOME="$WORKDIR/t19-nohome" \
    LOOM_DAEMON_BIN="$MOCK19" LOOM_DAEMON_SELF_BIN="$SELF19" \
    bash "$STUB" </dev/null 2>/dev/null )
assert_eq "IMPL invoked: strip-ansi" "$out" \
    "a stub execs \$LOOM_DAEMON_SELF_BIN, NOT the \$LOOM_DAEMON_BIN a caller set to mean a probe mock (#8134)"

# ---------- 20. …and with LOOM_DAEMON_SELF_BIN unset, the stub falls back to
#                the normal resolution unchanged, so an operator can still pin
#                the implementation with LOOM_DAEMON_BIN exactly as before. ----------
out=$( env -i PATH="$MINIMAL_PATH" HOME="$WORKDIR/t20-nohome" LOOM_DAEMON_BIN="$MOCK19" \
    bash "$STUB" </dev/null 2>/dev/null )
assert_eq "MOCK invoked: strip-ansi" "$out" \
    "with no \$LOOM_DAEMON_SELF_BIN the stub still honours \$LOOM_DAEMON_BIN (operator pin preserved)"

# ---------- 21. a set-but-not-executable LOOM_DAEMON_SELF_BIN falls through
#                rather than hard-failing — the same contract case 2 pins for
#                $LOOM_DAEMON_BIN, so a typo'd pin degrades identically
#                whichever of the two variables carries it. ----------
stdout_out=$( env -i PATH="$MINIMAL_PATH" HOME="$WORKDIR/t21-nohome" \
    LOOM_DAEMON_BIN="$MOCK19" LOOM_DAEMON_SELF_BIN="$WORKDIR/t21/not-a-binary" \
    bash "$STUB" </dev/null 2>/dev/null )
assert_eq "MOCK invoked: strip-ansi" "$stdout_out" \
    "a non-executable \$LOOM_DAEMON_SELF_BIN falls through to the normal resolution, not a hard failure"

# ---------- 22. the stub's not-found error names BOTH knobs, so an operator
#                who lands there learns which one pins the implementation. ----------
stderr_out=$( env -i PATH="$MINIMAL_PATH" HOME="$WORKDIR/t22-nohome" \
    bash "$STUB" </dev/null 2>&1 >/dev/null )
assert_contains "LOOM_DAEMON_SELF_BIN" "$stderr_out" \
    "the 'loom-daemon not found' error names \$LOOM_DAEMON_SELF_BIN as the implementation knob"
assert_contains "LOOM_DAEMON_BIN" "$stderr_out" \
    "…and still names \$LOOM_DAEMON_BIN"

# ---------- summary ----------
echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
