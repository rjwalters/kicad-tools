#!/usr/bin/env bash
# test-loom-daemon-update.sh — Tests for loom-daemon-update.sh (Issue #3968).
#
# Focus: (1) staleness detection (--check, built-commit vs. source-HEAD
# comparison), (2) flag preservation across a rebuild+restart (the persisted
# .loom/.daemon.flags file must be replayed verbatim, never widened), and
# (3) the "was not running -> do not start" / --no-restart / --dry-run
# guardrails that keep the FLAGS-OFF/opt-in contract intact.
#
# Style matches test-loom-daemon-start.sh — plain bash, hand-rolled
# assertions, no bats.
#
# Strategy: build a throwaway repo root (temp dir) with a real `.git`, a
# stub `loom-daemon/Cargo.toml`, real copies of loom-daemon-start.sh /
# loom-daemon-stop.sh (so the restart path exercises the actual scripts, not
# a mock), a FAKE `cargo` on PATH that "builds" by copying a prepared fake
# daemon binary into target/release/ instead of actually compiling Rust, and
# FAKE daemon binaries whose `--version` output embeds a controllable commit
# and whose normal-run mode records the autonomy env vars it inherited to a
# marker file before looping (staying "alive" for PID-liveness checks).
#
# Usage:
#   ./defaults/scripts/tests/test-loom-daemon-update.sh

set -uo pipefail

# Production-binary checksum guard (#4381 incident): 2026-07-29 ~06:03Z, the
# REAL machine-level `~/.local/bin/loom-daemon` was overwritten with a fake,
# infinitely-looping test-fixture stub for ~9 hours (an operator `status` poll
# hung; a crash at any point would have had launchd relaunch the stub). Root
# cause: three tests below (37/39/43, the ff-sync/staleness-detection cases)
# deliberately invoke loom-daemon-update.sh with NEITHER LOOM_DAEMON_BIN NOR a
# stubbed scripts/install/provision-daemon.sh, so the update script's
# provision_machine_daemon() call fell through to its real, un-sandboxed
# default destination (`${LOOM_DAEMON_BIN_DIR:-$HOME/.local/bin}`) — the
# operator's own real $HOME. The LOOM_DAEMON_BIN_DIR export below (threaded to
# every sub-invocation) is the actual fix; THIS checksum snapshot is the
# regression backstop that fails the suite outright if any current or future
# test call site regresses into writing the real production binary, even if
# the sandboxing above is bypassed or a new call site is added without it.
# Recorded before ANYTHING else runs, using the checksum tool most likely
# present (sha256sum on Linux, shasum on macOS); skipped (empty) only when
# neither is on PATH or no binary exists yet at that path (nothing to compare
# against — a fresh-machine first run).
_PROD_DAEMON_BIN="$HOME/.local/bin/loom-daemon"
_prod_daemon_checksum() {
    [[ -e "$_PROD_DAEMON_BIN" ]] || { echo "<absent>"; return 0; }
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$_PROD_DAEMON_BIN" 2>/dev/null | awk '{print $1}'
    elif command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$_PROD_DAEMON_BIN" 2>/dev/null | awk '{print $1}'
    else
        echo "<no-checksum-tool>"
    fi
}
_PROD_DAEMON_CHECKSUM_BEFORE="$(_prod_daemon_checksum)"

# Force the legacy nohup path everywhere in this suite (#3972): the restart
# flow below exercises the REAL loom-daemon-start.sh / loom-daemon-stop.sh
# (not a mock), and this test must NEVER touch the real machine's
# ~/Library/LaunchAgents/ -- which may hold an actual production
# com.rjwalters.loom-daemon LaunchAgent under the exact same default label.
export LOOM_DAEMON_LAUNCHD=0

# Force the legacy nohup path on LINUX too (#4799 CI hang). The launchd knob
# above only sandboxes Darwin; on a Linux runner `MINIMAL_PATH` below still
# reaches the REAL /usr/bin/systemctl and `is_linux_systemd` (lib/systemd-user.sh)
# returns true whenever the runner has a reachable `systemctl --user` manager.
# That made the suite non-hermetic AND non-terminating in CI:
#
#   1. scenario 5's real loom-daemon-start.sh took the systemd branch and ran
#      `systemctl --user enable --now loom-daemon.service` -- the DEFAULT unit
#      name, i.e. the operator's/runner's real user unit, plus a real
#      `loom-daemon-watchdog.timer` (provision_watchdog_job_systemd derives its
#      name from the daemon unit, NOT from the sandboxed LOOM_WATCHDOG_LABEL).
#   2. every LATER scenario then saw `systemd_unit_loaded` == true, so
#      loom-daemon-update.sh resolved DAEMON_MANAGER=systemd and took the
#      supervised-restart branch: `"$PROVISION_TARGET" restart`.
#   3. PROVISION_TARGET is the fake-daemon fixture, which has no `restart`
#      handler -- so it fell straight into its `while true; do sleep 1; done`
#      daemon body IN THE FOREGROUND and never returned. Scenario 5b wedged
#      there for the full 1200s per-suite CI budget (exit 124).
#
# A macOS dev run never exercises any of this (no systemctl), which is exactly
# why the suite passed locally in ~4m and hung in CI. The systemd-specific
# scenarios (26-34) opt back in explicitly with LOOM_DAEMON_SYSTEMD=1 alongside
# their LOOM_SYSTEMD_FORCE=1 seam and their own stub `systemctl` on PATH.
export LOOM_DAEMON_SYSTEMD=0
# Belt-and-braces on top of the knob, mirroring LOOM_LAUNCHD_LABEL below: even
# if a future regression re-opens a systemd path, the unit it would resolve is a
# per-run scratch name, never the real `loom-daemon.service`. The scenarios that
# drive systemd deliberately pin their own LOOM_SYSTEMD_UNIT per invocation.
export LOOM_SYSTEMD_UNIT="loom-daemon-update-test-$$.service"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI_DIR="$(cd "$SCRIPT_DIR/../cli" && pwd)"
UPDATE_SCRIPT="$CLI_DIR/loom-daemon-update.sh"

# Shared launchd sandbox (#4078). Belt-and-braces on top of LOOM_DAEMON_LAUNCHD=0:
#   - a scratch LOOM_LAUNCHD_LABEL so any launchd lookup that DID fire could not
#     resolve the operator's real com.rjwalters.loom-daemon job, and
#   - stub launchctl/pgrep installed onto the test PATH (below), so the real
#     tools are unreachable even if a future regression re-opens a launchd/pgrep
#     path in the stop script the restart flow drives.
# This closes the exact hole that booted out the operator's live daemon: the
# update suite exercises the REAL start/stop scripts, and launchd is
# machine-global.
# shellcheck source=lib/launchd-sandbox.sh
source "$SCRIPT_DIR/lib/launchd-sandbox.sh"
export LOOM_LAUNCHD_LABEL="$(launchd_sandbox_new_label)"

# Background-PID bookkeeping (#4773): every fake-daemon/decoy process this
# suite backgrounds is tracked here so the EXIT/INT/TERM trap below can reap
# it even if the parent test process is interrupted before its own inline
# cleanup runs.
# shellcheck source=lib/bg-proc-trap.sh
source "$SCRIPT_DIR/lib/bg-proc-trap.sh"

# Shared live-state sandbox (#5179). The snapshot MUST run here — before
# live_state_sandbox_init below rewrites the LOOM_* state vars, and before any
# sub-invocation can write anything — because it discovers WHICH paths are the
# live ones by reading the ambient environment (a Loom agent session exports
# LOOM_PID_FILE / LOOM_WORKSPACE / LOOM_DAEMON_BIN at the REAL production
# paths, so this suite inherits them unless it says otherwise). The matching
# live_state_sandbox_assert_untouched runs as the suite's final guard.
# shellcheck source=lib/live-state-sandbox.sh
source "$SCRIPT_DIR/lib/live-state-sandbox.sh"
live_state_sandbox_snapshot

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} $msg"
        echo -e "  expected: [$expected]"
        echo -e "  actual:   [$actual]"
    fi
}

assert_true() {
    local cond="$1" msg="$2"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$cond" == "true" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} $msg"
    fi
}

# Repo root of the actual Loom checkout this test suite lives in, so fixtures
# can pull in the real scripts/install/provision-daemon.sh (which defines the
# #4016 sign_daemon_binary helper loom-daemon-update.sh sources at
# $REPO_ROOT/scripts/install/provision-daemon.sh).
LOOM_REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# ---------- fixture builder ----------
# Sets up a fresh throwaway repo root at $1 with a real git HEAD, a stub
# loom-daemon crate, real start/stop scripts, a real copy of
# provision-daemon.sh (so the #4016 signing step is exercised, not silently
# skipped as "not found/sourceable"), and a minimal, machine-agnostic PATH
# (excludes ~/.local/bin and similar, so a real loom-daemon possibly
# installed on the dev machine can never leak into a test).
new_fixture() {
    local root="$1"
    mkdir -p "$root/.loom/logs" "$root/.loom/scripts/cli" "$root/.loom/scripts/lib" "$root/loom-daemon" "$root/scripts/install"
    cp "$CLI_DIR/loom-daemon-start.sh" "$root/.loom/scripts/cli/loom-daemon-start.sh"
    cp "$CLI_DIR/loom-daemon-stop.sh" "$root/.loom/scripts/cli/loom-daemon-stop.sh"
    chmod +x "$root/.loom/scripts/cli/"*.sh
    # The fixture start/stop scripts source ../lib/launchd-domain.sh for the
    # shared gui/<uid> ↦ user/<uid> resolver (#4130), so it must exist alongside
    # them in the throwaway tree — else a launchd-mode restart path would find no
    # resolve_launchd_domain. Mirrors the real defaults/scripts/lib layout.
    cp "$CLI_DIR/../lib/launchd-domain.sh" "$root/.loom/scripts/lib/launchd-domain.sh"
    # Same for lib/systemd-user.sh (#4268): the fixture start script's systemd
    # --user path (invoked via perform_systemd_relaunch's call to $START_SCRIPT,
    # #4260 sub-issue C) sources it relative to ITS OWN location, so it must exist
    # alongside the fixture copy too, not just in the real repo tree.
    cp "$CLI_DIR/../lib/systemd-user.sh" "$root/.loom/scripts/lib/systemd-user.sh"
    # Same for lib/bounded-run.sh (#4799): the fixture start script's
    # print_calibrate_hint() sources it relative to ITS OWN location to bound
    # its `calibrate` command substitution, so it must exist alongside the
    # fixture copy too.
    cp "$CLI_DIR/../lib/bounded-run.sh" "$root/.loom/scripts/lib/bounded-run.sh"
    # Same for lib/locate-daemon-bin.sh (#4875): the fixture start script
    # sources it relative to ITS OWN location to resolve the daemon binary
    # under a minimal PATH, so every fixture flow that execs the copied
    # loom-daemon-start.sh (restart, --relaunch, the full update run) needs it
    # in the throwaway tree. Without it those flows abort with
    # "locate-daemon-bin.sh not found at <fixture>/.loom/scripts/lib" before
    # reaching the behaviour under test.
    cp "$CLI_DIR/../lib/locate-daemon-bin.sh" "$root/.loom/scripts/lib/locate-daemon-bin.sh"
    cp "$LOOM_REPO_ROOT/scripts/install/provision-daemon.sh" "$root/scripts/install/provision-daemon.sh"
    cat > "$root/loom-daemon/Cargo.toml" <<'EOF'
[package]
name = "loom-daemon"
version = "0.0.0"
EOF
    ( cd "$root" && git init -q && git -c user.email=test@test -c user.name=test commit -q --allow-empty -m init )
}

# install_update_script_into <root> (#5140) — drops a real copy of
# loom-daemon-update.sh (plus every lib/ it sources, resolved relative to its
# own $SCRIPT_DIR) into a fixture's .loom/scripts/, so the self-location
# fallback can be exercised on a THROWAWAY checkout. Every other scenario
# invokes $UPDATE_SCRIPT from the real repo, which is fine while $PWD decides
# the repo root — but the whole point of the #5140 scenarios is that the
# script's OWN location decides it, so they must not run the real repo's copy.
install_update_script_into() {
    local root="$1"
    mkdir -p "$root/.loom/scripts/cli" "$root/.loom/scripts/lib"
    cp "$UPDATE_SCRIPT" "$root/.loom/scripts/cli/loom-daemon-update.sh"
    chmod +x "$root/.loom/scripts/cli/loom-daemon-update.sh"
    cp "$CLI_DIR/../lib/"*.sh "$root/.loom/scripts/lib/"
}

# new_fixture_with_origin <root> <bare_dir> (#4330) — builds on new_fixture(),
# adding a local BARE repo as `origin` so the ff-first sync path (which
# resolves the default branch via refs/remotes/origin/HEAD, then fetches and
# compares against origin/<branch>) has a real remote to talk to — entirely
# offline (a plain filesystem path, no network). Forces the branch name to
# `main` (deterministic regardless of the test host's init.defaultBranch) and
# sets refs/remotes/origin/HEAD via `git remote set-head origin -a` so
# loom_default_branch() resolves it the same way a real clone would.
new_fixture_with_origin() {
    local root="$1" bare="$2"
    new_fixture "$root"
    ( cd "$root" && git branch -q -M main )
    git init -q --bare "$bare"
    ( cd "$root" && git remote add origin "$bare" && git push -q origin HEAD:refs/heads/main )
    git -C "$bare" symbolic-ref HEAD refs/heads/main
    ( cd "$root" && git remote set-head origin -a >/dev/null 2>&1 )
}

# push_extra_commits_to_origin <bare_dir> <n> — advances the bare `origin`
# repo `n` commits ahead of whatever a fixture repo's `main` currently is, by
# cloning it into a throwaway dir, committing there, and pushing back. Echoes
# the new origin tip's short commit on stdout. The fixture repo passed to
# new_fixture_with_origin is left untouched (still behind by exactly <n>).
push_extra_commits_to_origin() {
    local bare="$1" n="$2" tmpclone
    tmpclone="$(mktemp -d)"
    git clone -q "$bare" "$tmpclone"
    local i
    for i in $(seq 1 "$n"); do
        ( cd "$tmpclone" && git -c user.email=test@test -c user.name=test commit -q --allow-empty -m "extra ${i}" )
    done
    ( cd "$tmpclone" && git push -q origin HEAD:refs/heads/main )
    local tip
    tip="$(cd "$tmpclone" && git rev-parse --short HEAD)"
    rm -rf "$tmpclone"
    echo "$tip"
}

# Writes a fake daemon binary at $1 that reports commit $2 on --version and,
# on a normal run, appends its inherited LOOM_WORK_FINDER / LOOM_MAIN_HEALTH_GATE
# to marker file $3 before looping forever (so it stays alive for kill -0).
#
# `calibrate` is handled explicitly (#4799) and exits immediately with no
# output: this fixture has no real calibrate implementation, and every
# successful loom-daemon-start.sh run (nohup/launchd/systemd, all three
# reached by this suite's restart scenarios) calls `$DAEMON_BIN calibrate
# --workspace ... --json` via print_calibrate_hint(). Before this fix, that
# call fell through to the `while true` loop below and hung forever inside
# print_calibrate_hint()'s blocking `$(...)` -- the exact hang
# ci-excluded.txt documented. print_calibrate_hint() is bounded independently
# now (lib/bounded-run.sh), but this fixture also short-circuits so the suite
# stays fast rather than eating that timeout on every restart.
#
# GENERALIZED (#4799 CI hang): `calibrate` was only one instance of a whole
# CLASS of wedge. ANY *subcommand* the lifecycle scripts dispatch that this
# fixture does not recognize used to fall through to the `while true` daemon
# body IN THE FOREGROUND and block its caller forever -- which is precisely how
# the CI run hung: with a real `systemctl --user` reachable, the update script
# resolved DAEMON_MANAGER=systemd and ran `"$PROVISION_TARGET" restart`, and
# this fixture (no `restart` handler) looped instead of answering. So the
# catch-all below exits non-zero for any unrecognized NON-FLAG first argument
# (a real daemon rejects an unknown subcommand; it does not daemonize). A
# leading `-`/`--` still falls through to the daemon body, because the
# supervisors DO launch the daemon proper with flags.
write_fake_daemon() {
    local path="$1" commit="$2" marker="$3"
    cat > "$path" <<EOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "--version" ]]; then
    echo "loom-daemon 0.15.0 (commit ${commit}, built 2026-07-26T00:00:00Z)"
    exit 0
fi
if [[ "\${1:-}" == "calibrate" ]]; then
    exit 1
fi
if [[ -n "\${1:-}" && "\${1:-}" != -* ]]; then
    echo "fake loom-daemon: unsupported subcommand: \$*" >&2
    exit 1
fi
echo "FAKE_DAEMON WF=[\${LOOM_WORK_FINDER:-}] HG=[\${LOOM_MAIN_HEALTH_GATE:-}]" > "${marker}"
while true; do sleep 1; done
EOF
    chmod +x "$path"
}

# Writes a fake daemon binary at $1 that reports commit $2 on --version, and on a
# `restart` subcommand (the #4077 supervised primitive, #4042) appends a line to
# marker file $3 and exits with code $4 — so a launchd-managed update test can
# assert the update drove `restart` (NOT stop+start) and control whether the
# request "succeeds" (0, supervised) or is "refused" (non-zero, pre-#4077 binary).
# On a normal run it loops forever (kept alive for any liveness check).
write_fake_daemon_restart() {
    local path="$1" commit="$2" restart_marker="$3" restart_rc="$4"
    cat > "$path" <<EOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "--version" ]]; then
    echo "loom-daemon 0.15.0 (commit ${commit}, built 2026-07-26T00:00:00Z)"
    exit 0
fi
if [[ "\${1:-}" == "restart" ]]; then
    echo "restart" >> "${restart_marker}"
    exit ${restart_rc}
fi
# See write_fake_daemon's comment (#4799): no real calibrate implementation,
# exit fast instead of falling into the daemon-body loop below.
if [[ "\${1:-}" == "calibrate" ]]; then
    exit 1
fi
# Same catch-all as write_fake_daemon (#4799): an unrecognized NON-FLAG
# subcommand must never fall into the foreground daemon loop and wedge its
# caller. Flags still reach the daemon body.
if [[ -n "\${1:-}" && "\${1:-}" != -* ]]; then
    echo "fake loom-daemon: unsupported subcommand: \$*" >&2
    exit 1
fi
while true; do sleep 1; done
EOF
    chmod +x "$path"
}

# Writes a fake daemon binary at $1 that reports commit $2 on --version, and on
# a `restart` subcommand appends the FULL argv (e.g. "restart --drain --timeout
# 5 --force-after-timeout") to marker file $3 and exits with code $4 — the
# drain-mode analog of write_fake_daemon_restart above (Issue #5138), which
# only ever logs the literal word "restart" and cannot distinguish a plain
# restart from a drain-mode one. Lets a test assert exactly which flags the
# update script threaded through to `loom-daemon restart`.
write_fake_daemon_restart_argv() {
    local path="$1" commit="$2" restart_marker="$3" restart_rc="$4"
    cat > "$path" <<EOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "--version" ]]; then
    echo "loom-daemon 0.15.0 (commit ${commit}, built 2026-07-26T00:00:00Z)"
    exit 0
fi
if [[ "\${1:-}" == "restart" ]]; then
    echo "\$*" >> "${restart_marker}"
    exit ${restart_rc}
fi
if [[ "\${1:-}" == "calibrate" ]]; then
    exit 1
fi
if [[ -n "\${1:-}" && "\${1:-}" != -* ]]; then
    echo "fake loom-daemon: unsupported subcommand: \$*" >&2
    exit 1
fi
while true; do sleep 1; done
EOF
    chmod +x "$path"
}

# Writes a fake `launchctl` at $1 that logs invocations to $2 and, on
# `launchctl print`, reports a LOADED job with a live-looking pid (exit 0) —
# simulating a launchd-managed loom-daemon for the #4042 ownership-detection
# tests. Paired with a fake `uname`->Darwin so the update script's Darwin-gated
# launchd path fires deterministically on any host.
#
# Optional $3/$4 (#4232): when a <post_restart_marker> file path is given,
# `print` reports pid 4242 until that file EXISTS, at which point it reports
# <post_restart_pid> instead — simulating "launchd relaunched the job onto a
# new (real, live) pid the instant the restart request was accepted". Tests
# that don't pass $3/$4 keep the old static-4242-forever behavior (they never
# exercise the #4232 pid-verification poll).
write_fake_launchd_loaded_bin() {
    local bin_dir="$1" log="$2" post_restart_marker="${3:-}" post_restart_pid="${4:-}"
    mkdir -p "$bin_dir"
    : > "$log"
    cat > "$bin_dir/launchctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "${log}"
case "\${1:-}" in
  print)
    if [[ -n "${post_restart_marker}" && -e "${post_restart_marker}" ]]; then
      echo "	pid = ${post_restart_pid}"
    else
      echo "	pid = 4242"
    fi
    exit 0 ;;
  *)     exit 0 ;;
esac
EOF
    chmod +x "$bin_dir/launchctl"
    cat > "$bin_dir/uname" <<'EOF'
#!/usr/bin/env bash
echo "Darwin"
EOF
    chmod +x "$bin_dir/uname"
}

# Writes a fake `launchctl` at $1 for the #4232 restart-VERIFICATION tests: the
# reported pid lives in a state file ($3) rather than being hardcoded, so a
# test can hold it "stuck" on the pre-restart pid to simulate launchd failing
# to relaunch on its own. `print` reports whatever pid (if any) is currently in
# the state file plus a "last exit status" line (diagnostic breadcrumb,
# #4232). `kickstart` is recorded VERBATIM to $2 (so a test can assert it was
# never invoked with `-k`) and, only when $4 (<kickstart_new_pid>) is given,
# overwrites the state file with it — simulating a kickstart-triggered
# relaunch; when $4 is omitted, `kickstart` is a no-op (a kickstart that also
# fails to bring the job up).
write_fake_launchd_pid_bin() {
    local bin_dir="$1" log="$2" state_file="$3" kickstart_new_pid="${4:-}"
    mkdir -p "$bin_dir"
    : > "$log"
    cat > "$bin_dir/launchctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "${log}"
case "\${1:-}" in
  print)
    pid="\$(cat "${state_file}" 2>/dev/null)"
    if [[ -n "\$pid" ]]; then
      echo "	state = running"
      echo "	pid = \$pid"
    else
      echo "	state = not running"
    fi
    echo "	last exit status = 0"
    exit 0 ;;
  kickstart)
    if [[ -n "${kickstart_new_pid}" ]]; then
      echo "${kickstart_new_pid}" > "${state_file}"
    fi
    exit 0 ;;
  *) exit 0 ;;
esac
EOF
    chmod +x "$bin_dir/launchctl"
    cat > "$bin_dir/uname" <<'EOF'
#!/usr/bin/env bash
echo "Darwin"
EOF
    chmod +x "$bin_dir/uname"
}

# Writes a stale, PRE-#4077 LaunchAgent plist at $1 (the exact state that causes
# the refused-restart exit-6 fallback): KeepAlive=false, NO LOOM_DAEMON_SUPERVISOR
# key, and four autonomy env keys plus a SENTINEL PATH. The re-render on the
# --relaunch path (#4118) must (a) install KeepAlive:{SuccessfulExit:true} +
# LOOM_DAEMON_SUPERVISOR=launchd, (b) carry all four autonomy keys through
# unchanged, and (c) NOT round-trip the sentinel PATH (start.sh rebuilds PATH).
# Args: <plist_path> <label> <bin> <homedir>.
write_fixture_plist_pre4077() {
    local plist="$1" label="$2" bin="$3" homedir="$4"
    mkdir -p "$(dirname "$plist")"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${bin}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/sentinel-oldpath-4118/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>${homedir}</string>
        <key>LOOM_WORK_FINDER</key>
        <string>1</string>
        <key>LOOM_MAIN_HEALTH_GATE</key>
        <string>1</string>
        <key>LOOM_WORK_FINDER_MAX_CONCURRENT</key>
        <string>10</string>
        <key>LOOM_PER_TOKEN_CONCURRENCY</key>
        <string>5</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>${homedir}/daemon.log</string>
    <key>StandardErrorPath</key>
    <string>${homedir}/daemon.log</string>
</dict>
</plist>
EOF
}

# Writes a stub `systemctl` at $1/systemctl that logs invocations to $2 and
# reports an ACTIVE unit with MainPID $3 — the systemd analog of
# write_fake_launchd_loaded_bin, used with the LOOM_SYSTEMD_FORCE=1 test seam
# (lib/systemd-user.sh) since this suite runs on Darwin runners. Structurally
# unable to touch a real systemd --user manager.
write_fake_systemd_active_bin() {
    local bin_dir="$1" log="$2" mainpid="$3"
    mkdir -p "$bin_dir"
    : > "$log"
    cat > "$bin_dir/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "${log}"
if [[ "\${1:-}" == "--user" ]]; then shift; fi
case "\${1:-}" in
  is-active)  exit 0 ;;
  is-enabled) exit 0 ;;
  show)       echo "${mainpid}" ;;
  *)          exit 0 ;;
esac
EOF
    chmod +x "$bin_dir/systemctl"
}

# Writes a stub `systemctl` at $1/systemctl for the #4950 restart-VERIFICATION
# tests: `MainPID`/`ActiveState`/`Result` all live in a single state file ($3,
# colon-delimited "pid:activestate:result") rather than being hardcoded, so a
# test can drive the exact transitions the update script's #4950 poll +
# self-heal logic reacts to. Structurally unable to touch a real systemd
# --user manager — this suite runs on Darwin runners (LOOM_SYSTEMD_FORCE=1).
#
# Args: <bin_dir> <log> <state_file> <pre_state> [post_restart_marker]
#       [post_state] [recovery_state]
#
#   <pre_state>            the initial "pid:activestate:result" written to
#                           <state_file> on first invocation if it does not
#                           already exist (e.g. "4242:active:success").
#   <post_restart_marker>  (optional) when this file EXISTS, the state auto-
#                           advances from <pre_state> to <post_state> exactly
#                           once — simulating "the instant the restart request
#                           was accepted, systemd's own transition took
#                           effect" (paired with write_fake_daemon_restart,
#                           which creates the marker as a side effect of its
#                           `restart` subcommand).
#   <post_state>            the state to advance to once the marker exists
#                           (e.g. "<new-live-pid>:active:success" to simulate
#                           an immediate spontaneous relaunch, or
#                           "0:failed:timeout" to simulate the #4950 incident
#                           shape — a stop-timeout escalation that
#                           Restart=on-success never fires for).
#   <recovery_state>        (optional) the state `start` (as invoked by the
#                           update script's `systemctl --user reset-failed
#                           <unit> && systemctl --user start <unit>` self-heal)
#                           advances to. Omit to simulate a self-heal attempt
#                           that ALSO fails to bring the unit up (the state
#                           stays whatever `reset-failed` left it at:
#                           "<pid>:inactive:success").
#   <settle_state>          (optional, #5119) the state a TRANSITIONAL
#                           ActiveState (deactivating/activating/reloading)
#                           SETTLES into after being observed twice — simulating
#                           systemd finishing a slow stop transition. On the 1st
#                           `show ActiveState` read the transitional value is
#                           returned unchanged (so the update script sees the
#                           mid-teardown snapshot the #4950 poll saw); on the 2nd
#                           the state file is rewritten to <settle_state> and it
#                           is returned. Lets a test drive the exact 2026-08-03
#                           incident: post_state="0:deactivating:timeout" ->
#                           settle_state="0:failed:timeout" -> reset-failed+start
#                           recovery. A settle counter lives in
#                           "<state_file>.actcount".
write_fake_systemd_pid_bin() {
    local bin_dir="$1" log="$2" state_file="$3" pre_state="$4"
    local post_restart_marker="${5:-}" post_state="${6:-}" recovery_state="${7:-}"
    local settle_state="${8:-}"
    mkdir -p "$bin_dir"
    : > "$log"
    echo "${pre_state}" > "${state_file}"
    rm -f "${state_file}.actcount"
    cat > "$bin_dir/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "${log}"
if [[ "\${1:-}" == "--user" ]]; then shift; fi

state_file="${state_file}"
cur="\$(cat "\$state_file" 2>/dev/null)"
if [[ -n "${post_restart_marker}" && -e "${post_restart_marker}" && "\$cur" == "${pre_state}" ]]; then
    echo "${post_state}" > "\$state_file"
    cur="${post_state}"
fi
IFS=: read -r cur_pid cur_active cur_result <<< "\$cur"

case "\${1:-}" in
  is-active)
    [[ "\$cur_active" == "active" ]] && exit 0 || exit 1 ;;
  is-enabled)
    exit 0 ;;
  show)
    prop=""
    for a in "\$@"; do
      case "\$a" in
        MainPID) prop=MainPID ;;
        ActiveState) prop=ActiveState ;;
        Result) prop=Result ;;
      esac
    done
    case "\$prop" in
      MainPID)     echo "\$cur_pid" ;;
      ActiveState)
        # #5119 settle simulation: a transitional state advances to
        # <settle_state> on its SECOND ActiveState observation, so the update
        # script first sees the mid-teardown snapshot (deactivating) and then
        # the settled terminal state (failed/inactive) once it waits.
        if [[ -n "${settle_state}" ]]; then
          case "\$cur_active" in
            deactivating|activating|reloading|deactivating-sigterm|deactivating-sigkill)
              actcount=\$(( \$(cat "\${state_file}.actcount" 2>/dev/null || echo 0) + 1 ))
              echo "\$actcount" > "\${state_file}.actcount"
              if (( actcount >= 2 )); then
                echo "${settle_state}" > "\$state_file"
                IFS=: read -r _sp _sa _sr <<< "${settle_state}"
                echo "\$_sa"
              else
                echo "\$cur_active"
              fi
              ;;
            *) echo "\$cur_active" ;;
          esac
        else
          echo "\$cur_active"
        fi
        ;;
      Result)      echo "\$cur_result" ;;
      *)           echo "" ;;
    esac
    ;;
  status)
    echo "● loom-daemon.service - Loom autonomous daemon (loom-daemon)"
    echo "   Loaded: loaded"
    echo "   Active: \$cur_active (Result: \$cur_result) since Mon 2026-08-02 03:17:36 UTC"
    echo "Main PID: \$cur_pid"
    ;;
  reset-failed)
    echo "\$cur_pid:inactive:success" > "\$state_file"
    ;;
  start)
    if [[ -n "${recovery_state}" ]]; then
      echo "${recovery_state}" > "\$state_file"
    fi
    ;;
  *) exit 0 ;;
esac
EOF
    chmod +x "$bin_dir/systemctl"
}

# Writes a stale, PRE-#4267 systemd --user unit at $1 (the exact state that
# causes the refused-restart exit-6 fallback): NO Restart=on-success, NO
# LOOM_DAEMON_SUPERVISOR key, four autonomy Environment= lines, and a SENTINEL
# PATH. The re-render on the --relaunch path must (a) install Restart=on-success
# + LOOM_DAEMON_SUPERVISOR=systemd, (b) carry all four autonomy keys through
# unchanged, and (c) NOT round-trip the sentinel PATH (start.sh rebuilds PATH).
# Args: <unit_path> <bin>.
write_fixture_unit_pre4267() {
    local unit_path="$1" bin="$2"
    mkdir -p "$(dirname "$unit_path")"
    cat > "$unit_path" <<EOF
[Unit]
Description=Loom autonomous daemon (loom-daemon)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/tmp
ExecStart=${bin}
Environment=PATH=/opt/sentinel-oldpath-4267/bin:/usr/bin:/bin
Environment=HOME=/tmp
Environment=LOOM_WORK_FINDER=1
Environment=LOOM_MAIN_HEALTH_GATE=1
Environment=LOOM_WORK_FINDER_MAX_CONCURRENT=10
Environment=LOOM_PER_TOKEN_CONCURRENCY=5
StandardOutput=append:/tmp/daemon.log
StandardError=append:/tmp/daemon.log

[Install]
WantedBy=default.target
EOF
}

# Writes a fake `cargo` that, on `cargo build --release` (cwd = loom-daemon/),
# copies $NEW_FAKE_BIN_SRC into target/release/loom-daemon instead of
# compiling. Tests export NEW_FAKE_BIN_SRC before invoking loom-daemon-update.sh.
write_fake_cargo() {
    local path="$1"
    cat > "$path" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "build" ]]; then
    mkdir -p target/release
    cp "$NEW_FAKE_BIN_SRC" target/release/loom-daemon
    chmod +x target/release/loom-daemon
    echo "[fake cargo] build ok"
    exit 0
fi
echo "[fake cargo] unsupported subcommand: $*" >&2
exit 1
EOF
    chmod +x "$path"
}

# Fake `crontab` (#4697): the update script's idle-shutdown-notice check runs
# `crontab -l` unconditionally on every invocation, so without this stub every
# test in this suite would shell out to the REAL system `crontab` for
# whichever account runs the suite — reading (never writing, but still a real
# information leak/hang risk on a host with no cron daemon configured) actual
# operator state, exactly the class of hazard the launchd/systemd sandboxing
# above exists to prevent. `-l` echoes the contents of
# $FAKE_CRONTAB_CONTENTS_FILE when set+readable, else exits 1 with no output
# (mirrors the common "no crontab for this user" case — silence, not an
# error the caller need alarm on). Any other invocation is a no-op success.
write_fake_crontab() {
    local path="$1"
    cat > "$path" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "-l" ]]; then
    if [[ -n "${FAKE_CRONTAB_CONTENTS_FILE:-}" && -r "${FAKE_CRONTAB_CONTENTS_FILE:-}" ]]; then
        cat "$FAKE_CRONTAB_CONTENTS_FILE"
        exit 0
    fi
    exit 1
fi
exit 0
EOF
    chmod +x "$path"
}

# Writes a fake `gh` at $1 for the artifact-fetch tests (Epic #4990 Phase 3,
# #5020). Understands exactly the invocations loom-daemon-update.sh's
# fetch_resolve_latest() / fetch_and_verify_artifact() make:
#   gh release view --json tagName  -R <slug> --jq '.tagName'         -> $2
#   gh release view --json assets   -R <slug> --jq '.assets[].name'   -> `ls $3`
#   gh release download <tag> -R <slug> -p <name> [-p <name> ...] -D <dir> --clobber
#       -> copies each matching file from $3 into <dir>; exits 1 if NONE of
#          the -p patterns matched anything under $3 (mirrors real gh's
#          "no assets match" failure for a required download).
write_fake_gh() {
    local path="$1" tag="$2" assets_dir="$3"
    cat > "$path" <<FAKEGH
#!/usr/bin/env bash
ASSETS_DIR="$assets_dir"
TAG_VAL="$tag"
FAKEGH
    cat >> "$path" <<'FAKEGH'
if [[ "${1:-}" == "release" && "${2:-}" == "view" ]]; then
    shift 2
    fields=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) fields="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    case "$fields" in
        tagName) echo "$TAG_VAL"; exit 0 ;;
        assets)  ls "$ASSETS_DIR" 2>/dev/null; exit 0 ;;
        *) exit 1 ;;
    esac
fi
if [[ "${1:-}" == "release" && "${2:-}" == "download" ]]; then
    shift 2
    shift # drop the <tag> positional arg
    dest="."
    patterns=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -p) patterns+=("$2"); shift 2 ;;
            -D) dest="$2"; shift 2 ;;
            -R) shift 2 ;;
            --clobber) shift ;;
            *) shift ;;
        esac
    done
    mkdir -p "$dest"
    copied=0
    for pat in "${patterns[@]}"; do
        for f in "$ASSETS_DIR"/$pat; do
            [[ -e "$f" ]] || continue
            cp "$f" "$dest/"
            copied=1
        done
    done
    [[ "$copied" -eq 1 ]] && exit 0 || exit 1
fi
echo "fake gh: unsupported invocation: $*" >&2
exit 1
FAKEGH
    chmod +x "$path"
}

# Writes a fake "release artifact" binary at $1 reporting version $2 / commit
# $3 on --version, otherwise behaving like write_fake_daemon (rejects unknown
# subcommands, loops forever on a normal run) — standing in for a downloaded
# `loom-daemon-<target>` asset. A parameterized-version sibling of
# write_fake_daemon (which hardcodes 0.15.0), needed so a fetched artifact can
# report a version NEWER than the installed daemon's.
write_fake_artifact_daemon() {
    local path="$1" version="$2" commit="$3"
    cat > "$path" <<EOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "--version" ]]; then
    echo "loom-daemon ${version} (commit ${commit}, built 2026-08-03T00:00:00Z)"
    exit 0
fi
if [[ "\${1:-}" == "calibrate" ]]; then
    exit 1
fi
if [[ -n "\${1:-}" && "\${1:-}" != -* ]]; then
    echo "fake loom-daemon: unsupported subcommand: \$*" >&2
    exit 1
fi
while true; do sleep 1; done
EOF
    chmod +x "$path"
}

# sha256_of <path> — portable checksum in `.sha256`-file format
# (`<hex>  <basename>`), matching the release workflow's own
# `shasum -a 256`/`sha256sum` output.
sha256_of() {
    local path="$1" base
    base="$(basename "$path")"
    if command -v shasum >/dev/null 2>&1; then
        (cd "$(dirname "$path")" && shasum -a 256 "$base")
    else
        (cd "$(dirname "$path")" && sha256sum "$base")
    fi
}

# Writes a fake `gh` at $1 whose every `release view` fails — standing in for
# the "GitHub API unreachable / rate-limited / unauthenticated" case that must
# SOFTLY fall back to the local source build (AC4), never hard-fail.
write_fake_gh_unreachable() {
    local path="$1"
    cat > "$path" <<'FAKEGH'
#!/usr/bin/env bash
echo "gh: failed to fetch release: dial tcp: lookup api.github.com: no such host" >&2
exit 1
FAKEGH
    chmod +x "$path"
}

# Writes a fake `codesign` at $1 emulating one of three macOS states, so the
# darwin signature branch of verify_artifact_signature() is testable on ANY
# host (including a Linux CI runner, which has no codesign at all):
#   unsigned    -- `-dv` reports "code object is not signed at all" (expected
#                  for a release built with no Developer ID secrets: soft-skip)
#   signed-ok   -- `-dv` reports an Authority, `--verify` succeeds
#   signed-bad  -- `-dv` reports an Authority, `--verify` FAILS (tamper
#                  evidence: must abort, NOT be confused with "unsigned")
write_fake_codesign() {
    local path="$1" mode="$2"
    cat > "$path" <<FAKECS
#!/usr/bin/env bash
MODE="$mode"
FAKECS
    cat >> "$path" <<'FAKECS'
target="${!#}"
if [[ "${1:-}" == "-dv" || "${1:-}" == "-dvvv" ]]; then
    if [[ "$MODE" == "unsigned" ]]; then
        echo "$target: code object is not signed at all" >&2
        exit 1
    fi
    {
        echo "Executable=$target"
        echo "Identifier=com.rjwalters.loom-daemon"
        echo "Authority=Developer ID Application: Test Authority (TESTTEAM)"
    } >&2
    exit 0
fi
if [[ "${1:-}" == "--verify" ]]; then
    [[ "$MODE" == "signed-ok" ]] && exit 0
    echo "$target: invalid signature (code or signature have been modified)" >&2
    exit 1
fi
exit 0
FAKECS
    chmod +x "$path"
}

# Writes a fake `cosign` at $1 whose `verify-blob` exits $2 — the Linux
# detached-signature branch of verify_artifact_signature().
write_fake_cosign() {
    local path="$1" rc="$2"
    cat > "$path" <<FAKECOSIGN
#!/usr/bin/env bash
if [[ "\${1:-}" == "verify-blob" ]]; then
    if [[ "$rc" -eq 0 ]]; then
        echo "Verified OK" >&2
        exit 0
    fi
    echo "Error: failed to verify signature" >&2
    exit 1
fi
exit 0
FAKECOSIGN
    chmod +x "$path"
}

# Writes a fake `cosign` at $1 whose `verify-blob` exits $2 AND appends its full
# argv to the log file at $3 (#5054). Recording the argv is the point: the
# keyless cases below assert not just "verification ran" but that it ran with
# the DERIVED signer identity + OIDC issuer, which is the whole security
# property — a fake cosign that always exits 0 would otherwise "pass" even if
# the script silently verified against nothing.
write_fake_cosign_recording() {
    local path="$1" rc="$2" argslog="$3"
    cat > "$path" <<FAKECOSIGN
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$argslog"
if [[ "\${1:-}" == "verify-blob" ]]; then
    if [[ "$rc" -eq 0 ]]; then
        echo "Verified OK" >&2
        exit 0
    fi
    echo "Error: failed to verify signature" >&2
    exit 1
fi
exit 0
FAKECOSIGN
    chmod +x "$path"
}

MINIMAL_PATH="/usr/bin:/bin:/usr/sbin:/sbin"

BASE_WORKDIR="$(mktemp -d)"

# ---------- live daemon state sandbox (#5179) ----------
# ONE helper owns EVERY live-state path this suite could otherwise reach, so a
# state file added to the daemon later is isolated by construction rather than
# after it leaks in production. It supersedes the per-variable overrides that
# used to live here, and covers (see lib/live-state-sandbox.sh for the full
# rationale per variable):
#
#   LOOM_PID_FILE          the #5179 leak itself. The ambient value a Loom
#                          agent session exports names the REAL .daemon.pid,
#                          and it is tier 1 of daemon_pidfile.rs's resolver —
#                          ahead of every other tier — so an un-overridden
#                          suite does not get a neutral default, it inherits
#                          the live path and rewrites it under a running
#                          daemon (observed: a false `degraded` liveness
#                          verdict on a healthy host, plus a poisoned input
#                          for the #5118/#5126 watchdog).
#   LOOM_AUTONOMY_MARKER   #4011/#5131: a restart path that reaches the real
#                          loom-daemon-start.sh must never write the
#                          operator's ~/.loom/autonomy-desired.
#   LOOM_SOCKET_PATH       its parent is the daemon's `loom_dir`, so the
#                          heartbeat + machine-level logs follow it into the
#                          sandbox (start.sh documents this seam explicitly).
#   LOOM_DAEMON_BIN_DIR    #4381 incident (see the checksum-guard writeup at
#                          the top of this file): update.sh resolves its
#                          machine-level install destination as
#                          `${LOOM_DAEMON_BIN_DIR:-$HOME/.local/bin}` whenever
#                          LOOM_DAEMON_BIN is unset, and tests 37/39/43
#                          deliberately exercise that no-LOOM_DAEMON_BIN path.
#   LOOM_DAEMON_BIN        #4902: unset, because the ambient agent-session
#                          value names the real production binary and would
#                          win over LOOM_DAEMON_BIN_DIR.
#   LOOM_WORKSPACE /       unset, so each sub-invocation resolves its own
#   LOOM_MACHINE_CHECKOUT  fixture instead of the live checkout / machine-mode
#                          state home. Tests that need either pin it inline.
#
# Tests below that need a pinned value still set it on their own invocation
# (e.g. `LOOM_DAEMON_BIN="$W1/..." bash ...`), which applies regardless.
live_state_sandbox_init "$BASE_WORKDIR/live-state"

# Supervisor identity, not a state path — the watchdog LaunchAgent must be
# provisioned under the scratch label so a restart path can never touch the
# real com.rjwalters.loom-daemon-watchdog job (#4011/#4078).
export LOOM_WATCHDOG_LABEL="${LOOM_LAUNCHD_LABEL}-watchdog"

# Binary-format sanity gate bypass (#4397, deferred from #4381's incident
# review): provision_machine_daemon now refuses to install anything that
# isn't a real compiled binary (Mach-O/ELF — see _pmd_is_real_binary in
# scripts/install/provision-daemon.sh). EVERY fake daemon binary this suite
# writes (write_fake_daemon et al.) is a bash script standing in for the real
# compiled binary, so THIS SUITE — and only this suite — sets the explicit,
# auditable bypass suite-wide. Production callers (scripts/install-loom.sh,
# defaults/scripts/cli/loom-daemon-update.sh) never set it; the gate itself is
# exercised directly (both the reject-a-script and accept-a-real-binary
# cases) by tests/install/test-provision-daemon.sh.
export LOOM_PROVISION_ALLOW_SCRIPT=1

# Suite-level decoy (#4078): a process whose argv ends in `/loom-daemon`, which
# the stop script's label-blind `pgrep -f '(^|/)loom-daemon$'` fallback would
# match. The whole update suite runs under a scratch LOOM_LAUNCHD_LABEL and
# LOOM_DAEMON_LAUNCHD=0, so no real launchd lookup or by-name kill may fire; if
# any test regresses into one, this decoy dies and the final assertion fails.
# Spawned OUTSIDE $BASE_WORKDIR's path so the trap's `pkill -f "$BASE_WORKDIR"`
# does not sweep it; killed explicitly below.
#
# Deliberately NOT renamed per #5548's "test fixtures should not be named
# `loom-daemon`" fix: the whole point of this fixture is to BE a process
# named `loom-daemon` so the label-blind pgrep tier it decoys has something
# to (not) match -- renaming it would defeat the test. It is tracked via
# bg_proc_track/bg_proc_reap (EXIT/INT/TERM traps) like every other
# background fixture in this suite, so it does not additionally widen
# #5548's "orphaned past all cleanup" risk beyond what SIGKILL of the test
# runner already allows for any tracked fixture (a hard, documented
# bash/POSIX limit -- see lib/bg-proc-trap.sh).
DECOY_DIR="$(mktemp -d)"
cat > "$DECOY_DIR/loom-daemon" <<'EOF'
#!/usr/bin/env bash
while true; do sleep 1; done
EOF
chmod +x "$DECOY_DIR/loom-daemon"
# Redirect stdio to /dev/null so the never-exiting decoy cannot hold open a
# captured stdout pipe of the suite (which would block a caller capturing its
# output — the same command-substitution gotcha the sandbox spawner avoids).
"$DECOY_DIR/loom-daemon" >/dev/null 2>&1 &
DECOY_PID=$!
bg_proc_track "$DECOY_PID"

# Best-effort cleanup of any fake-daemon processes left running (matched by
# their script path under $BASE_WORKDIR, which appears in `ps`'s command
# line) — individual tests also kill their own PIDs explicitly, this is a
# backstop for anything a failed assertion left behind. bg_proc_reap kills
# every PID tracked via bg_proc_track (the W5/W15/W26-style sandbox fixtures
# below track their own spawned PIDs directly rather than relying solely on
# the pkill pattern-match); EXIT/INT/TERM (not just EXIT, #4773) so a hard
# interruption of this suite still reaps every tracked/backstopped process.
# NOTE: a bare `trap CMD EXIT INT TERM` runs CMD on INT/TERM but does NOT stop
# the script (only an EXIT-trap firing auto-exits) -- the explicit `exit`
# below is required, else a SIGTERM'd suite would clean up once and then keep
# running every remaining (numbered W*) test case, re-populating
# $BASE_WORKDIR as it goes (observed directly while verifying this fix: an
# untrapped-exit version of this trap let a SIGTERM'd run limp all the way to
# a later scenario, which then hit a real, un-stubbed `cargo build --release`
# once its own fixture dir had already been rm -rf'd out from under it).
trap 'bg_proc_reap; [ -n "$BASE_WORKDIR" ] && pkill -f "$BASE_WORKDIR" >/dev/null 2>&1; rm -rf "$BASE_WORKDIR" "$DECOY_DIR"' EXIT
trap 'bg_proc_reap; [ -n "$BASE_WORKDIR" ] && pkill -f "$BASE_WORKDIR" >/dev/null 2>&1; rm -rf "$BASE_WORKDIR" "$DECOY_DIR"; exit 1' INT TERM

FAKE_BIN_DIR="$BASE_WORKDIR/fakebin"
mkdir -p "$FAKE_BIN_DIR"
write_fake_cargo "$FAKE_BIN_DIR/cargo"
write_fake_crontab "$FAKE_BIN_DIR/crontab"
# Stub launchctl/pgrep onto the front of every test PATH (FAKE_BIN_DIR is the
# first entry of TEST_PATH and TEST_PATH_NO_CODESIGN), recording invocations to
# $SANDBOX_LOG_DIR so the suite can assert no production label was ever named.
SANDBOX_LOG_DIR="$BASE_WORKDIR/sandbox-log"
launchd_sandbox_install_stubs "$FAKE_BIN_DIR" "$SANDBOX_LOG_DIR"
TEST_PATH="$FAKE_BIN_DIR:$MINIMAL_PATH"

# A copy of /usr/bin with every entry EXCEPT `codesign` symlinked in, used by
# the #4016 "codesign absent from PATH" test below. Built once (symlinking is
# effectively instant) rather than per-test.
NO_CODESIGN_DIR="$BASE_WORKDIR/no-codesign-usr-bin"
mkdir -p "$NO_CODESIGN_DIR"
for f in /usr/bin/*; do
    name="$(basename "$f")"
    [[ "$name" == "codesign" ]] && continue
    ln -sf "$f" "$NO_CODESIGN_DIR/$name" 2>/dev/null
done
TEST_PATH_NO_CODESIGN="$FAKE_BIN_DIR:$NO_CODESIGN_DIR:/bin:/usr/sbin:/sbin"

# ============================================================
# 1. --check reports "up to date" (exit 0) when the installed
#    binary's baked-in commit matches the source tree's HEAD.
# ============================================================
W1="$BASE_WORKDIR/w1"
new_fixture "$W1"
HEAD1="$(cd "$W1" && git rev-parse --short HEAD)"
write_fake_daemon "$W1/installed-loom-daemon" "$HEAD1" "$W1/marker"
out=$( cd "$W1" && PATH="$TEST_PATH" LOOM_DAEMON_BIN="$W1/installed-loom-daemon" \
    bash "$UPDATE_SCRIPT" --check; echo "EXIT=$?" )
rc=$(echo "$out" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rc" "--check exits 0 when installed commit matches source HEAD"

# ============================================================
# 2. --check reports "update available" (exit 3) when the
#    installed binary's baked-in commit differs from source HEAD.
# ============================================================
W2="$BASE_WORKDIR/w2"
new_fixture "$W2"
write_fake_daemon "$W2/installed-loom-daemon" "deadbee" "$W2/marker"
out=$( cd "$W2" && PATH="$TEST_PATH" LOOM_DAEMON_BIN="$W2/installed-loom-daemon" \
    bash "$UPDATE_SCRIPT" --check; echo "EXIT=$?" )
rc=$(echo "$out" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "3" "$rc" "--check exits 3 when installed commit differs from source HEAD"

# ============================================================
# 3. --check makes NO writes (no rebuild, no provisioning).
# ============================================================
W3="$BASE_WORKDIR/w3"
new_fixture "$W3"
write_fake_daemon "$W3/installed-loom-daemon" "deadbee" "$W3/marker"
( cd "$W3" && PATH="$TEST_PATH" LOOM_DAEMON_BIN="$W3/installed-loom-daemon" \
    bash "$UPDATE_SCRIPT" --check >/dev/null 2>&1 )
if [[ -e "$W3/loom-daemon/target/release/loom-daemon" ]]; then
    assert_eq "no-build-dir" "build-dir-present" "--check performs no rebuild"
else
    assert_eq "true" "true" "--check performs no rebuild"
fi

# ============================================================
# 4. --dry-run reports the plan but makes no writes either.
# ============================================================
W4="$BASE_WORKDIR/w4"
new_fixture "$W4"
write_fake_daemon "$W4/installed-loom-daemon" "deadbee" "$W4/marker"
dryrun_out=$( cd "$W4" && PATH="$TEST_PATH" LOOM_DAEMON_BIN="$W4/installed-loom-daemon" \
    bash "$UPDATE_SCRIPT" --dry-run 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -e "$W4/loom-daemon/target/release/loom-daemon" ]]; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --dry-run performs no rebuild"
elif echo "$dryrun_out" | grep -q 'dry-run.*cargo build'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --dry-run performs no rebuild and prints the plan"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --dry-run performs no rebuild and prints the plan"
    echo "  output: $dryrun_out"
fi

# ============================================================
# 5. Full flow: stale binary + a daemon that WAS running with
#    persisted flags (--work-finder) -> rebuild, provision,
#    restart with EXACTLY the persisted flags (never widened).
# ============================================================
W5="$BASE_WORKDIR/w5"
new_fixture "$W5"
HEAD5="$(cd "$W5" && git rev-parse --short HEAD)"
INSTALLED5="$W5/installed/loom-daemon"
mkdir -p "$W5/installed"
write_fake_daemon "$INSTALLED5" "deadbee" "$W5/old-marker"
NEW_FAKE5="$W5/new-fake-daemon"
write_fake_daemon "$NEW_FAKE5" "$HEAD5" "$W5/new-marker"

# Persisted flags from a prior `loom-daemon-start.sh --work-finder` (Issue #3968).
echo "--work-finder" > "$W5/.loom/.daemon.flags"

# Simulate an already-running old daemon: background the OLD fake binary and
# record its PID, exactly like loom-daemon-start.sh would have.
"$INSTALLED5" >/dev/null 2>&1 &
old_pid=$!
bg_proc_track "$old_pid"
sleep 0.3
echo "$old_pid" > "$W5/.loom/.daemon.pid"
TESTS_RUN=$((TESTS_RUN + 1))
if kill -0 "$old_pid" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} fixture: old daemon process is alive before update"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} fixture: old daemon process is alive before update"
fi

( cd "$W5" && PATH="$TEST_PATH" LOOM_DAEMON_BIN="$INSTALLED5" NEW_FAKE_BIN_SRC="$NEW_FAKE5" \
    env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    bash "$UPDATE_SCRIPT" >"$W5/update.log" 2>&1 )
update_rc=$?
assert_eq "0" "$update_rc" "full update run (rebuild+provision+restart) exits 0"

TESTS_RUN=$((TESTS_RUN + 1))
if kill -0 "$old_pid" 2>/dev/null; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} old daemon process was stopped by the restart"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} old daemon process was stopped by the restart"
fi

# Wait briefly for the new (restarted) daemon to write its marker.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -s "$W5/new-marker" ]] && break
    sleep 0.3
done
new_marker_content="$(cat "$W5/new-marker" 2>/dev/null || echo "<missing>")"
assert_eq "FAKE_DAEMON WF=[1] HG=[0]" "$new_marker_content" \
    "restarted daemon inherited EXACTLY the persisted --work-finder flag (never widened to health-gate too)"

TESTS_RUN=$((TESTS_RUN + 1))
if [[ -x "$INSTALLED5" ]] && "$INSTALLED5" --version 2>/dev/null | grep -q "$HEAD5"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} LOOM_DAEMON_BIN path was provisioned with the freshly-built binary"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} LOOM_DAEMON_BIN path was provisioned with the freshly-built binary"
fi

# Happy-path roll must self-verify both the built binary and the destination
# (#4053): the short-circuit can no longer produce a silent no-op on a real roll.
TESTS_RUN=$((TESTS_RUN + 1))
if grep -qi 'Build verification' "$W5/update.log" \
    && grep -qi 'Post-provision verification' "$W5/update.log"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} happy-path roll self-verifies built commit AND destination binary"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} happy-path roll self-verifies built commit AND destination binary"
    echo "  update.log: $(cat "$W5/update.log" 2>/dev/null)"
fi

# Clean up the restarted daemon (find it via the PID file this run wrote).
if [[ -f "$W5/.loom/.daemon.pid" ]]; then
    kill "$(cat "$W5/.loom/.daemon.pid" 2>/dev/null)" 2>/dev/null || true
fi

# ============================================================
# 5b. Regression: an EMPTY persisted-flags file (a prior bare/FLAGS-OFF
#     start) must restart cleanly with zero flags — no unbound-variable
#     crash from `"${RESTART_ARGS[@]}"` on a zero-element array under
#     `set -u` (bash < 4.4, still /bin/bash on stock macOS).
# ============================================================
W5B="$BASE_WORKDIR/w5b"
new_fixture "$W5B"
HEAD5B="$(cd "$W5B" && git rev-parse --short HEAD)"
INSTALLED5B="$W5B/installed/loom-daemon"
mkdir -p "$W5B/installed"
write_fake_daemon "$INSTALLED5B" "deadbee" "$W5B/old-marker"
NEW_FAKE5B="$W5B/new-fake-daemon"
write_fake_daemon "$NEW_FAKE5B" "$HEAD5B" "$W5B/new-marker"
: > "$W5B/.loom/.daemon.flags" # empty, mirroring a bare prior start

"$INSTALLED5B" >/dev/null 2>&1 &
old_pid5b=$!
bg_proc_track "$old_pid5b"
sleep 0.3
echo "$old_pid5b" > "$W5B/.loom/.daemon.pid"

# Capture to a log rather than /dev/null (#4799): when this scenario wedged in
# CI the tail of the suite output was undiagnostic precisely because its update
# run wrote nowhere, so the failure surfaced as silence. Mirror scenario 5.
( cd "$W5B" && PATH="$TEST_PATH" LOOM_DAEMON_BIN="$INSTALLED5B" NEW_FAKE_BIN_SRC="$NEW_FAKE5B" \
    env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    bash "$UPDATE_SCRIPT" >"$W5B/update.log" 2>&1 )
update5b_rc=$?
if [[ "$update5b_rc" != "0" ]]; then
    echo "  update.log (5b): $(tail -n 20 "$W5B/update.log" 2>/dev/null)"
fi
assert_eq "0" "$update5b_rc" "restart with an EMPTY persisted-flags file exits 0 (#3968 regression)"

for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -s "$W5B/new-marker" ]] && break
    sleep 0.3
done
new5b_marker_content="$(cat "$W5B/new-marker" 2>/dev/null || echo "<missing>")"
# A plain (zero-flag) restart forwards no CLI args, so loom-daemon-start.sh's
# own FLAGS-OFF default applies: LOOM_WORK_FINDER=0, LOOM_MAIN_HEALTH_GATE=0.
assert_eq "FAKE_DAEMON WF=[0] HG=[0]" "$new5b_marker_content" \
    "restarted daemon with empty persisted flags runs plain FLAGS-OFF (no args forwarded)"

if [[ -f "$W5B/.loom/.daemon.pid" ]]; then
    kill "$(cat "$W5B/.loom/.daemon.pid" 2>/dev/null)" 2>/dev/null || true
fi

# ============================================================
# 5c. Non-interactive-SSH cargo fallback (#4695): cargo is absent from PATH
#     entirely (the exact non-login-shell-SSH symptom), but present at
#     rustup's default $HOME/.cargo/bin -- the update must still find it via
#     the fallback and rebuild successfully.
# ============================================================
W5C="$BASE_WORKDIR/w5c"
new_fixture "$W5C"
HEAD5C="$(cd "$W5C" && git rev-parse --short HEAD)"
INSTALLED5C="$W5C/installed/loom-daemon"
mkdir -p "$W5C/installed"
write_fake_daemon "$INSTALLED5C" "deadbee" "$W5C/old-marker"
NEW_FAKE5C="$W5C/new-fake-daemon"
write_fake_daemon "$NEW_FAKE5C" "$HEAD5C" "$W5C/new-marker"
# No .loom/.daemon.pid -> WAS_RUNNING resolves false, so this test exercises
# only the rebuild+provision path (the cargo-fallback code under test)
# without needing a background process to restart.

# A PATH carrying the same launchd-sandbox stubs (launchctl/pgrep) as every
# other test, but deliberately WITHOUT cargo anywhere on it.
NO_CARGO_BIN_DIR5C="$BASE_WORKDIR/w5c-no-cargo-bin"
NO_CARGO_LOG_DIR5C="$BASE_WORKDIR/w5c-no-cargo-log"
launchd_sandbox_install_stubs "$NO_CARGO_BIN_DIR5C" "$NO_CARGO_LOG_DIR5C"
TEST_PATH_NO_CARGO5C="$NO_CARGO_BIN_DIR5C:$MINIMAL_PATH"

# The fake cargo lives ONLY under a scratch $HOME's rustup-default location,
# simulating a non-interactive SSH shell that never sourced the profile line
# rustup's installer adds.
HOME5C="$BASE_WORKDIR/w5c-home"
mkdir -p "$HOME5C/.cargo/bin"
write_fake_cargo "$HOME5C/.cargo/bin/cargo"

out5c=$( cd "$W5C" && PATH="$TEST_PATH_NO_CARGO5C" HOME="$HOME5C" \
    LOOM_DAEMON_BIN="$INSTALLED5C" NEW_FAKE_BIN_SRC="$NEW_FAKE5C" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc5c=$?
assert_eq "0" "$rc5c" "update succeeds when cargo is absent from PATH but present at \$HOME/.cargo/bin (#4695)"

TESTS_RUN=$((TESTS_RUN + 1))
if [[ -x "$INSTALLED5C" ]] && "$INSTALLED5C" --version 2>/dev/null | grep -q "$HEAD5C"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} \$HOME/.cargo/bin fallback cargo actually rebuilt+provisioned the fresh binary"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} \$HOME/.cargo/bin fallback cargo actually rebuilt+provisioned the fresh binary"
    echo "  output: $out5c"
fi

# ============================================================
# 5d. cargo genuinely absent (not on PATH, not under $HOME/.cargo/bin either)
#     -> exit 1 with a failure message that suggests installing via rustup,
#     not just "not found" (#4695 AC).
# ============================================================
W5D="$BASE_WORKDIR/w5d"
new_fixture "$W5D"
HOME5D="$BASE_WORKDIR/w5d-home"
mkdir -p "$HOME5D" # deliberately no .cargo/bin at all
NO_CARGO_BIN_DIR5D="$BASE_WORKDIR/w5d-no-cargo-bin"
NO_CARGO_LOG_DIR5D="$BASE_WORKDIR/w5d-no-cargo-log"
launchd_sandbox_install_stubs "$NO_CARGO_BIN_DIR5D" "$NO_CARGO_LOG_DIR5D"
TEST_PATH_NO_CARGO5D="$NO_CARGO_BIN_DIR5D:$MINIMAL_PATH"

out5d=$( cd "$W5D" && PATH="$TEST_PATH_NO_CARGO5D" HOME="$HOME5D" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc5d=$?
assert_eq "1" "$rc5d" "update exits 1 when cargo is genuinely absent (no PATH, no \$HOME/.cargo/bin)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out5d" | grep -qi 'rustup'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} genuinely-absent-cargo error message suggests installing via rustup"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} genuinely-absent-cargo error message suggests installing via rustup"
    echo "  output: $out5d"
fi

# ============================================================
# 6. Stale binary but daemon was NOT running -> rebuild+provision
#    happen, but the daemon is NOT started (never widen FLAGS-OFF
#    by starting autonomy — or anything — that wasn't already up).
# ============================================================
W6="$BASE_WORKDIR/w6"
new_fixture "$W6"
HEAD6="$(cd "$W6" && git rev-parse --short HEAD)"
INSTALLED6="$W6/installed/loom-daemon"
mkdir -p "$W6/installed"
write_fake_daemon "$INSTALLED6" "deadbee" "$W6/old-marker"
NEW_FAKE6="$W6/new-fake-daemon"
write_fake_daemon "$NEW_FAKE6" "$HEAD6" "$W6/new-marker"
# No .loom/.daemon.pid -> WAS_RUNNING resolves false.

out6=$( cd "$W6" && PATH="$TEST_PATH" LOOM_DAEMON_BIN="$INSTALLED6" NEW_FAKE_BIN_SRC="$NEW_FAKE6" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc6=$?
assert_eq "0" "$rc6" "update with no prior PID file exits 0 (rebuild+provision only)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out6" | grep -qi 'not running'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} update reports the daemon was not running (nothing started)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} update reports the daemon was not running (nothing started)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$W6/.loom/.daemon.pid" ]]; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} update never starts a daemon that wasn't already running"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} update never starts a daemon that wasn't already running"
fi

# ============================================================
# 7. --no-restart rebuilds + provisions but leaves a running
#    daemon untouched (old process stays alive on the old binary).
# ============================================================
W7="$BASE_WORKDIR/w7"
new_fixture "$W7"
HEAD7="$(cd "$W7" && git rev-parse --short HEAD)"
INSTALLED7="$W7/installed/loom-daemon"
mkdir -p "$W7/installed"
write_fake_daemon "$INSTALLED7" "deadbee" "$W7/old-marker"
NEW_FAKE7="$W7/new-fake-daemon"
write_fake_daemon "$NEW_FAKE7" "$HEAD7" "$W7/new-marker"

"$INSTALLED7" >/dev/null 2>&1 &
old_pid7=$!
bg_proc_track "$old_pid7"
sleep 0.3
echo "$old_pid7" > "$W7/.loom/.daemon.pid"

( cd "$W7" && PATH="$TEST_PATH" LOOM_DAEMON_BIN="$INSTALLED7" NEW_FAKE_BIN_SRC="$NEW_FAKE7" \
    bash "$UPDATE_SCRIPT" --no-restart >/dev/null 2>&1 )

TESTS_RUN=$((TESTS_RUN + 1))
if kill -0 "$old_pid7" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --no-restart leaves the running daemon process untouched"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --no-restart leaves the running daemon process untouched"
fi
kill "$old_pid7" 2>/dev/null || true

# ============================================================
# 8. --help documents --check / --dry-run / --force / --no-restart.
# ============================================================
help_out=$(bash "$UPDATE_SCRIPT" --help 2>/dev/null)
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$help_out" | grep -q -- '--check' && echo "$help_out" | grep -q -- '--dry-run' \
    && echo "$help_out" | grep -q -- '--no-restart'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --help documents --check / --dry-run / --no-restart"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --help documents --check / --dry-run / --no-restart"
fi

# ============================================================
# 9. Refuses to run outside a loom-daemon source checkout.
# ============================================================
W9="$BASE_WORKDIR/w9"
mkdir -p "$W9/.loom"
( cd "$W9" && git init -q )
( cd "$W9" && PATH="$TEST_PATH" bash "$UPDATE_SCRIPT" --check >/dev/null 2>&1 )
rc9=$?
assert_eq "1" "$rc9" "refuses to run when loom-daemon/Cargo.toml is absent"

# ============================================================
# 10. Build verification: a freshly-built binary whose embedded
#     commit does NOT match source HEAD (a stale baked-in commit,
#     e.g. from a build.rs watch-set bug) -> exit 4 and NO provision.
#     This is the self-verifying rebuild the whole issue is about:
#     a successful compile that ships the wrong commit is refused,
#     distinguishably from a compile failure (#4053).
# ============================================================
W10="$BASE_WORKDIR/w10"
new_fixture "$W10"
INSTALLED10="$W10/installed/loom-daemon"
mkdir -p "$W10/installed"
write_fake_daemon "$INSTALLED10" "deadbee" "$W10/old-marker"   # stale installed
# The freshly-"built" binary reports a WRONG commit (NOT source HEAD): simulates
# a build.rs watch-set defect that bakes a stale commit into a successful build.
NEW_FAKE10="$W10/new-fake-daemon"
write_fake_daemon "$NEW_FAKE10" "badc0de" "$W10/new-marker"
out10=$( cd "$W10" && PATH="$TEST_PATH" LOOM_DAEMON_BIN="$INSTALLED10" NEW_FAKE_BIN_SRC="$NEW_FAKE10" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc10=$?
assert_eq "4" "$rc10" "stale baked-in commit (build-system defect) exits 4"
TESTS_RUN=$((TESTS_RUN + 1))
if "$INSTALLED10" --version 2>/dev/null | grep -q "deadbee"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} build-commit mismatch does NOT provision (destination left untouched)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} build-commit mismatch does NOT provision (destination left untouched)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out10" | grep -qi 'Build verification FAILED'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} build-commit mismatch is reported distinguishably from a compile failure"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} build-commit mismatch is reported distinguishably from a compile failure"
fi

# ============================================================
# 11. Post-provision verification: a provision step that reports
#     SUCCESS but leaves the destination stale (the exact "reports
#     success while shipping nothing" hazard) -> exit 5. Exercises
#     the provision_machine_daemon branch via a fake provisioner
#     that returns 0 and points PROVISIONED_DAEMON_BIN at a stale
#     binary — proving the short-circuit can no longer silently
#     no-op a real roll, and that the failure is distinguishable
#     from the pre-existing soft warn (#4053, findings 3 & 4).
# ============================================================
W11="$BASE_WORKDIR/w11"
new_fixture "$W11"
HEAD11="$(cd "$W11" && git rev-parse --short HEAD)"
# The freshly-"built" binary reports the CORRECT source HEAD (passes build
# verification), so the failure below is unambiguously post-provision.
NEW_FAKE11="$W11/new-fake-daemon"
write_fake_daemon "$NEW_FAKE11" "$HEAD11" "$W11/new-marker"
# A STALE destination binary the fake provisioner will dishonestly point at.
STALE_DEST11="$W11/staledest/loom-daemon"
mkdir -p "$W11/staledest"
write_fake_daemon "$STALE_DEST11" "abc0123" "$W11/stale-marker"
# Fake provision-daemon.sh: reports success but leaves the destination stale.
mkdir -p "$W11/scripts/install"
cat > "$W11/scripts/install/provision-daemon.sh" <<EOF
provision_machine_daemon() {
    PROVISIONED_DAEMON_BIN="$STALE_DEST11"
    return 0
}
EOF
# No LOOM_DAEMON_BIN -> the provision_machine_daemon branch. No resolvable
# installed binary -> UPDATE_NEEDED is true -> the fake cargo "builds" NEW_FAKE11.
out11=$( cd "$W11" && PATH="$TEST_PATH" NEW_FAKE_BIN_SRC="$NEW_FAKE11" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc11=$?
assert_eq "5" "$rc11" "provision reports success but ships a stale destination -> exit 5"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out11" | grep -qi 'Post-provision verification FAILED'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} silent no-op roll is caught and reported distinguishably"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} silent no-op roll is caught and reported distinguishably"
    echo "  output: $out11"
fi

# ============================================================
# 12. Signing helper (#4016) — a codesign FAILURE during the update is
#     non-fatal: the run still exits 0 and the binary is still provisioned,
#     with a warning surfaced. Fakes `uname` as Darwin so this is
#     deterministic regardless of the host actually running this suite.
# ============================================================
W12="$BASE_WORKDIR/w12"
new_fixture "$W12"
HEAD12="$(cd "$W12" && git rev-parse --short HEAD)"
INSTALLED12="$W12/installed/loom-daemon"
mkdir -p "$W12/installed"
write_fake_daemon "$INSTALLED12" "deadbee" "$W12/old-marker"
NEW_FAKE12="$W12/new-fake-daemon"
write_fake_daemon "$NEW_FAKE12" "$HEAD12" "$W12/new-marker"

FAKE_SIGN_FAIL_DIR="$W12/fake-codesign-fail-bin"
mkdir -p "$FAKE_SIGN_FAIL_DIR"
cat > "$FAKE_SIGN_FAIL_DIR/uname" <<'EOF'
#!/usr/bin/env bash
echo "Darwin"
EOF
chmod +x "$FAKE_SIGN_FAIL_DIR/uname"
cat > "$FAKE_SIGN_FAIL_DIR/codesign" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$FAKE_SIGN_FAIL_DIR/codesign"

out12=$( cd "$W12" && PATH="$FAKE_SIGN_FAIL_DIR:$TEST_PATH" LOOM_DAEMON_BIN="$INSTALLED12" NEW_FAKE_BIN_SRC="$NEW_FAKE12" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc12=$?
assert_eq "0" "$rc12" "codesign failure during update is non-fatal — exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out12" | grep -qi 'codesign failed'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} codesign failure surfaces a non-fatal warning"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} codesign failure surfaces a non-fatal warning"
    echo "  output: $out12"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -x "$INSTALLED12" ]] && "$INSTALLED12" --version 2>/dev/null | grep -q "$HEAD12"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} codesign failure: binary is still provisioned"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} codesign failure: binary is still provisioned"
fi

# ============================================================
# 13. Signing helper (#4016) — non-Darwin skips codesign entirely: the
#     update still succeeds and codesign is NEVER invoked. Fakes `uname`
#     as Linux and a `codesign` that leaves a marker file if ever called.
# ============================================================
W13="$BASE_WORKDIR/w13"
new_fixture "$W13"
HEAD13="$(cd "$W13" && git rev-parse --short HEAD)"
INSTALLED13="$W13/installed/loom-daemon"
mkdir -p "$W13/installed"
write_fake_daemon "$INSTALLED13" "deadbee" "$W13/old-marker"
NEW_FAKE13="$W13/new-fake-daemon"
write_fake_daemon "$NEW_FAKE13" "$HEAD13" "$W13/new-marker"

FAKE_LINUX_DIR13="$W13/fake-linux-bin"
mkdir -p "$FAKE_LINUX_DIR13"
cat > "$FAKE_LINUX_DIR13/uname" <<'EOF'
#!/usr/bin/env bash
echo "Linux"
EOF
chmod +x "$FAKE_LINUX_DIR13/uname"
CODESIGN_MARKER13="$W13/codesign-invoked-marker"
cat > "$FAKE_LINUX_DIR13/codesign" <<EOF
#!/usr/bin/env bash
touch "$CODESIGN_MARKER13"
exit 0
EOF
chmod +x "$FAKE_LINUX_DIR13/codesign"

# shellcheck disable=SC2034  # captured for ad-hoc debugging, not asserted on
out13=$( cd "$W13" && PATH="$FAKE_LINUX_DIR13:$TEST_PATH" LOOM_DAEMON_BIN="$INSTALLED13" NEW_FAKE_BIN_SRC="$NEW_FAKE13" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc13=$?
assert_eq "0" "$rc13" "non-Darwin: update still exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -x "$INSTALLED13" ]] && "$INSTALLED13" --version 2>/dev/null | grep -q "$HEAD13"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} non-Darwin: binary is still provisioned"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} non-Darwin: binary is still provisioned"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -e "$CODESIGN_MARKER13" ]]; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} non-Darwin: codesign is never invoked"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} non-Darwin: codesign is never invoked"
fi

# ============================================================
# 14. Signing helper (#4016) — codesign absent from PATH entirely: the
#     update still succeeds and provisions the binary (no codesign to
#     invoke at all). Uses a curated PATH built from /usr/bin minus
#     codesign, so this is a genuine absence rather than a stub.
# ============================================================
W14="$BASE_WORKDIR/w14"
new_fixture "$W14"
HEAD14="$(cd "$W14" && git rev-parse --short HEAD)"
INSTALLED14="$W14/installed/loom-daemon"
mkdir -p "$W14/installed"
write_fake_daemon "$INSTALLED14" "deadbee" "$W14/old-marker"
NEW_FAKE14="$W14/new-fake-daemon"
write_fake_daemon "$NEW_FAKE14" "$HEAD14" "$W14/new-marker"

out14=$( cd "$W14" && PATH="$TEST_PATH_NO_CODESIGN" LOOM_DAEMON_BIN="$INSTALLED14" NEW_FAKE_BIN_SRC="$NEW_FAKE14" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc14=$?
assert_eq "0" "$rc14" "codesign absent from PATH: update still exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -x "$INSTALLED14" ]] && "$INSTALLED14" --version 2>/dev/null | grep -q "$HEAD14"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} codesign absent from PATH: binary is still provisioned"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} codesign absent from PATH: binary is still provisioned"
    echo "  output: $out14"
fi

# ============================================================
# 15. Launchd ownership + restart (#4042/#4232): a launchd-managed daemon (stub
#     launchctl reports a LOADED job + pid) with NO .loom/.daemon.pid file ->
#     the updater plans/executes a RESTART (not "was not running"), drives it
#     through the `restart` subcommand (stub records the invocation), does NOT
#     consult .daemon.flags, and exits 0. The stub relaunches onto a NEW, real,
#     live pid the instant the restart is accepted (#4232 case (a): relaunch
#     observed -> success, no kickstart fallback ever invoked).
# ============================================================
W15="$BASE_WORKDIR/w15"
new_fixture "$W15"
HEAD15="$(cd "$W15" && git rev-parse --short HEAD)"
INSTALLED15="$W15/installed/loom-daemon"
mkdir -p "$W15/installed"
# The provisioned (fresh) binary — invoked as `restart` after provisioning —
# reports source HEAD and accepts the restart request (rc 0). No .daemon.pid.
RESTART_MARKER15="$W15/restart-invoked"
write_fake_daemon_restart "$INSTALLED15" "deadbee" "$RESTART_MARKER15" 0
NEW_FAKE15="$W15/new-fake-daemon"
write_fake_daemon_restart "$NEW_FAKE15" "$HEAD15" "$RESTART_MARKER15" 0
# A .daemon.flags that MUST NOT be consulted in launchd mode (would otherwise
# add --work-finder to a stop+start path).
echo "--work-finder" > "$W15/.loom/.daemon.flags"
# A real, live process standing in for "the relaunched job's new pid" — the
# #4232 poll requires a live pid (kill -0), not just a differing number.
sleep 60 >/dev/null 2>&1 &
RELAUNCHED_PID15=$!
bg_proc_track "$RELAUNCHED_PID15"
LD_BIN15="$W15/launchd-bin"
write_fake_launchd_loaded_bin "$LD_BIN15" "$W15/launchctl.log" "$RESTART_MARKER15" "$RELAUNCHED_PID15"

out15=$( cd "$W15" && PATH="$LD_BIN15:$TEST_PATH" LOOM_DAEMON_LAUNCHD=1 \
    LOOM_DAEMON_BIN="$INSTALLED15" NEW_FAKE_BIN_SRC="$NEW_FAKE15" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc15=$?
kill "$RELAUNCHED_PID15" 2>/dev/null || true
assert_eq "0" "$rc15" "launchd-managed update (no pid file) exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -s "$RESTART_MARKER15" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} launchd restart driven through the 'restart' subcommand (not stop+start)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} launchd restart driven through the 'restart' subcommand (not stop+start)"
    echo "  output: $out15"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out15" | grep -qi 'not running'; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} launchd-loaded job is NOT mistaken for 'was not running'"
    echo "  output: $out15"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} launchd-loaded job is NOT mistaken for 'was not running'"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q "^${RELAUNCHED_PID15}$\|new pid ${RELAUNCHED_PID15}" <<< "$out15" || echo "$out15" | grep -q "new pid ${RELAUNCHED_PID15}"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} success message reports the VERIFIED new pid (#4232)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} success message reports the VERIFIED new pid (#4232)"
    echo "  output: $out15"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q '^kickstart ' "$W15/launchctl.log" 2>/dev/null; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} relaunch observed immediately -> kickstart fallback is NEVER invoked (#4232 case a)"
    echo "  launchctl.log: $(cat "$W15/launchctl.log" 2>/dev/null)"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} relaunch observed immediately -> kickstart fallback is NEVER invoked (#4232 case a)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out15" | grep -qi 'FLAGS-OFF'; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} no 'restarting FLAGS-OFF' warning fires for a launchd restart"
    echo "  output: $out15"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} no 'restarting FLAGS-OFF' warning fires for a launchd restart"
fi

# ============================================================
# 16. Launchd restart REFUSED (#4042/#4118): a launchd job is loaded but the
#     running (old) binary rejects the restart request (pre-#4077 binary / dead
#     socket). Without --relaunch the updater must exit NON-ZERO (6) and print
#     the CORRECTED fallback: it names the `--relaunch` re-render path (NOT a
#     bare `launchctl bootstrap` of the stale plist, which was the #4118 bug),
#     mentions `launchctl bootout` and a graceful `kill -TERM` (AC4) — as of
#     #5081, bootout no longer terminates in-flight sweeps on a current build
#     (every sweep holds its own process group, #3800), so the warning is now
#     about the async bootout/bootstrap race leaving the daemon down, not
#     about sweep safety; `kill -TERM` is still named as the graceful,
#     settled/retried/verified path.
# ============================================================
W16="$BASE_WORKDIR/w16"
new_fixture "$W16"
HEAD16="$(cd "$W16" && git rev-parse --short HEAD)"
INSTALLED16="$W16/installed/loom-daemon"
mkdir -p "$W16/installed"
RESTART_MARKER16="$W16/restart-invoked"
write_fake_daemon_restart "$INSTALLED16" "deadbee" "$RESTART_MARKER16" 1
NEW_FAKE16="$W16/new-fake-daemon"
# Fresh binary's `restart` returns non-zero (request refused by the running daemon).
write_fake_daemon_restart "$NEW_FAKE16" "$HEAD16" "$RESTART_MARKER16" 1
LD_BIN16="$W16/launchd-bin"
write_fake_launchd_loaded_bin "$LD_BIN16" "$W16/launchctl.log"

out16=$( cd "$W16" && PATH="$LD_BIN16:$TEST_PATH" LOOM_DAEMON_LAUNCHD=1 \
    LOOM_DAEMON_BIN="$INSTALLED16" NEW_FAKE_BIN_SRC="$NEW_FAKE16" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc16=$?
assert_eq "6" "$rc16" "launchd restart refused -> exit 6 (never a silent half-update)"
# (a) Names the --relaunch re-render path and does NOT recommend a bare
#     `launchctl bootstrap` of the stale plist (the #4118 self-perpetuating bug).
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out16" | grep -q -- '--relaunch' && ! echo "$out16" | grep -qi 'launchctl bootstrap'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} refused restart names --relaunch, never a bare bootstrap of the stale plist"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} refused restart names --relaunch, never a bare bootstrap of the stale plist"
    echo "  output: $out16"
fi
# (b) AC4: mentions bootout + sweeps (the #5081-corrected framing: bootout no
#     longer kills in-flight sweeps, but hand-running it can still race the
#     async teardown) and still prefers a graceful `kill -TERM` fallback.
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out16" | grep -qi 'bootout' && echo "$out16" | grep -qi 'sweep' \
    && echo "$out16" | grep -q 'kill -TERM'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} refused restart mentions bootout+sweeps (#5081-corrected) + prefers kill -TERM (AC4)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} refused restart mentions bootout+sweeps (#5081-corrected) + prefers kill -TERM (AC4)"
    echo "  output: $out16"
fi

# ============================================================
# 17. --check names the owning manager (#4042): launchd (with label) when a
#     job is loaded; PID-file/nohup when a live pid file exists; not running
#     otherwise. All three are read-only (exit 3 here since the commit differs).
# ============================================================
W17="$BASE_WORKDIR/w17"
new_fixture "$W17"
INSTALLED17="$W17/installed/loom-daemon"
mkdir -p "$W17/installed"
write_fake_daemon "$INSTALLED17" "deadbee" "$W17/marker"
LD_BIN17="$W17/launchd-bin"
write_fake_launchd_loaded_bin "$LD_BIN17" "$W17/launchctl.log"
# (a) launchd loaded -> manager: launchd (names the scratch label).
check_ld_out=$( cd "$W17" && PATH="$LD_BIN17:$TEST_PATH" LOOM_DAEMON_LAUNCHD=1 \
    LOOM_LAUNCHD_LABEL="com.example.scratch-4042" LOOM_DAEMON_BIN="$INSTALLED17" \
    bash "$UPDATE_SCRIPT" --check 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$check_ld_out" | grep -qi 'manager: launchd' && echo "$check_ld_out" | grep -q 'com.example.scratch-4042'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --check names launchd (with label) as the owning manager"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --check names launchd (with label) as the owning manager"
    echo "  output: $check_ld_out"
fi
# (b) LOOM_DAEMON_LAUNCHD=0 + live pid file -> manager: PID-file/nohup.
"$INSTALLED17" >/dev/null 2>&1 &
pid17=$!
bg_proc_track "$pid17"
sleep 0.3
echo "$pid17" > "$W17/.loom/.daemon.pid"
check_pid_out=$( cd "$W17" && PATH="$TEST_PATH" LOOM_DAEMON_LAUNCHD=0 \
    LOOM_DAEMON_BIN="$INSTALLED17" bash "$UPDATE_SCRIPT" --check 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$check_pid_out" | grep -qi 'manager: PID-file'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --check names PID-file/nohup as the owning manager"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --check names PID-file/nohup as the owning manager"
    echo "  output: $check_pid_out"
fi
kill "$pid17" 2>/dev/null || true
rm -f "$W17/.loom/.daemon.pid"
# (c) nothing running -> manager: not running.
check_none_out=$( cd "$W17" && PATH="$TEST_PATH" LOOM_DAEMON_LAUNCHD=0 \
    LOOM_DAEMON_BIN="$INSTALLED17" bash "$UPDATE_SCRIPT" --check 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$check_none_out" | grep -qi 'manager: not running'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --check names 'not running' when no daemon is up"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --check names 'not running' when no daemon is up"
    echo "  output: $check_none_out"
fi

# ============================================================
# 18. --dry-run in launchd mode reports the launchd restart plan and makes no
#     writes (no rebuild, no restart invocation).
# ============================================================
W18="$BASE_WORKDIR/w18"
new_fixture "$W18"
INSTALLED18="$W18/installed/loom-daemon"
mkdir -p "$W18/installed"
RESTART_MARKER18="$W18/restart-invoked"
write_fake_daemon_restart "$INSTALLED18" "deadbee" "$RESTART_MARKER18" 0
LD_BIN18="$W18/launchd-bin"
write_fake_launchd_loaded_bin "$LD_BIN18" "$W18/launchctl.log"
dry18_out=$( cd "$W18" && PATH="$LD_BIN18:$TEST_PATH" LOOM_DAEMON_LAUNCHD=1 \
    LOOM_DAEMON_BIN="$INSTALLED18" bash "$UPDATE_SCRIPT" --dry-run 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$dry18_out" | grep -qi 'launchd-managed' && echo "$dry18_out" | grep -qi 'restart' \
    && [[ ! -s "$RESTART_MARKER18" ]] && [[ ! -e "$W18/loom-daemon/target/release/loom-daemon" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --dry-run reports the launchd restart plan and makes no writes"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --dry-run reports the launchd restart plan and makes no writes"
    echo "  output: $dry18_out"
fi

# ============================================================
# 19. LOOM_DAEMON_LAUNCHD=0 skips all launchd tiers even on a (faked) Darwin
#     host with a launchd job loaded: the daemon is treated as NOT launchd-
#     managed, so with no pid file it is "not running" (no restart attempted).
#     Guards the sandbox invariant that --no-launchd never reaches launchd.
# ============================================================
W19="$BASE_WORKDIR/w19"
new_fixture "$W19"
HEAD19="$(cd "$W19" && git rev-parse --short HEAD)"
INSTALLED19="$W19/installed/loom-daemon"
mkdir -p "$W19/installed"
RESTART_MARKER19="$W19/restart-invoked"
write_fake_daemon_restart "$INSTALLED19" "deadbee" "$RESTART_MARKER19" 0
NEW_FAKE19="$W19/new-fake-daemon"
write_fake_daemon_restart "$NEW_FAKE19" "$HEAD19" "$RESTART_MARKER19" 0
LD_BIN19="$W19/launchd-bin"
write_fake_launchd_loaded_bin "$LD_BIN19" "$W19/launchctl.log"
out19=$( cd "$W19" && PATH="$LD_BIN19:$TEST_PATH" LOOM_DAEMON_LAUNCHD=0 \
    LOOM_DAEMON_BIN="$INSTALLED19" NEW_FAKE_BIN_SRC="$NEW_FAKE19" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc19=$?
assert_eq "0" "$rc19" "LOOM_DAEMON_LAUNCHD=0 update exits 0 (rebuild+provision, no launchd)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out19" | grep -qi 'not running' && [[ ! -s "$RESTART_MARKER19" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} LOOM_DAEMON_LAUNCHD=0 skips launchd tiers (no restart driven)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} LOOM_DAEMON_LAUNCHD=0 skips launchd tiers (no restart driven)"
    echo "  output: $out19"
fi

# ============================================================
# Scenarios 21+22 are the only two in this suite that need a REAL `plutil`
# (#4799). They drive loom-daemon-update.sh's `--relaunch` path, whose first
# step is `harvest_plist_env` (lib/daemon-env-harvest.sh) -- which requires
# plutil + jq by contract ("required on the macOS launchd path") and returns 2
# when either is missing, so perform_relaunch aborts with 6 and never
# re-renders the plist. Scenario 22 then reads the result back with
# `plutil -extract` directly. Everything else launchd-flavored in this suite
# runs off the stub `launchctl`/`uname` pair and IS portable; these two are
# not, and plutil is macOS-only (no ubuntu package stands in -- an XML-plist
# parser stub would be testing the stub, not the production path). So skip
# them where plutil is absent rather than fail a Linux CI runner on a genuinely
# Darwin-only code path. They still run on every macOS dev/CI host.
if ! command -v plutil >/dev/null 2>&1; then
    echo -e "${YELLOW}⊘${NC} SKIP scenarios 21-22 (--relaunch plist re-render + env preservation): plutil not available — harvest_plist_env is a macOS-only production path"
else
# ============================================================
# 21. --relaunch re-renders the plist with the SUPERVISED keys (#4118 AC1):
#     after a refused restart, `--relaunch` re-renders via loom-daemon-start.sh,
#     installing KeepAlive:{SuccessfulExit:true} + LOOM_DAEMON_SUPERVISOR=launchd
#     into the plist — the two keys the stale pre-#4077 fixture plist LACKS, so a
#     passing assertion proves the re-render actually happened (not a leftover).
#     HOME is sandboxed so LAUNCHD_PLIST resolves inside the test tree, never the
#     operator's real ~/Library/LaunchAgents (#4078).
# ============================================================
W21="$BASE_WORKDIR/w21"
new_fixture "$W21"
INSTALLED21="$W21/installed/loom-daemon"
mkdir -p "$W21/installed"
RESTART_MARKER21="$W21/restart-invoked"
# Running (old) + fresh binaries both REFUSE restart (pre-#4077) -> exit-6 path.
write_fake_daemon_restart "$INSTALLED21" "deadbee" "$RESTART_MARKER21" 1
NEW_FAKE21="$W21/new-fake-daemon"
HEAD21="$(cd "$W21" && git rev-parse --short HEAD)"
write_fake_daemon_restart "$NEW_FAKE21" "$HEAD21" "$RESTART_MARKER21" 1
LD_BIN21="$W21/launchd-bin"
write_fake_launchd_loaded_bin "$LD_BIN21" "$W21/launchctl.log"
HOME21="$W21/home"
PLIST21="$HOME21/Library/LaunchAgents/${LOOM_LAUNCHD_LABEL}.plist"
write_fixture_plist_pre4077 "$PLIST21" "$LOOM_LAUNCHD_LABEL" "$INSTALLED21" "$HOME21"
( cd "$W21" && PATH="$LD_BIN21:$TEST_PATH" HOME="$HOME21" LOOM_DAEMON_LAUNCHD=1 \
    LOOM_DAEMON_BIN="$INSTALLED21" NEW_FAKE_BIN_SRC="$NEW_FAKE21" \
    bash "$UPDATE_SCRIPT" --relaunch >/dev/null 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'LOOM_DAEMON_SUPERVISOR' "$PLIST21" 2>/dev/null \
    && grep -q 'SuccessfulExit' "$PLIST21" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --relaunch re-renders the plist with KeepAlive:SuccessfulExit + LOOM_DAEMON_SUPERVISOR (AC1)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --relaunch re-renders the plist with KeepAlive:SuccessfulExit + LOOM_DAEMON_SUPERVISOR (AC1)"
    echo "  plist: $(cat "$PLIST21" 2>/dev/null)"
fi

# ============================================================
# 22. --relaunch PRESERVES the live plist's autonomy env across the re-render
#     (#4118 AC3): the four autonomy keys carry through byte-for-byte, and the
#     stale PATH is NOT round-tripped (start.sh rebuilds PATH; harvesting it would
#     grow the string every roll — Curator trap #1).
# ============================================================
W22="$BASE_WORKDIR/w22"
new_fixture "$W22"
INSTALLED22="$W22/installed/loom-daemon"
mkdir -p "$W22/installed"
RESTART_MARKER22="$W22/restart-invoked"
write_fake_daemon_restart "$INSTALLED22" "deadbee" "$RESTART_MARKER22" 1
NEW_FAKE22="$W22/new-fake-daemon"
HEAD22="$(cd "$W22" && git rev-parse --short HEAD)"
write_fake_daemon_restart "$NEW_FAKE22" "$HEAD22" "$RESTART_MARKER22" 1
LD_BIN22="$W22/launchd-bin"
write_fake_launchd_loaded_bin "$LD_BIN22" "$W22/launchctl.log"
HOME22="$W22/home"
PLIST22="$HOME22/Library/LaunchAgents/${LOOM_LAUNCHD_LABEL}.plist"
write_fixture_plist_pre4077 "$PLIST22" "$LOOM_LAUNCHD_LABEL" "$INSTALLED22" "$HOME22"
( cd "$W22" && PATH="$LD_BIN22:$TEST_PATH" HOME="$HOME22" LOOM_DAEMON_LAUNCHD=1 \
    LOOM_DAEMON_BIN="$INSTALLED22" NEW_FAKE_BIN_SRC="$NEW_FAKE22" \
    bash "$UPDATE_SCRIPT" --relaunch >/dev/null 2>&1 )
wf22=$(plutil -extract EnvironmentVariables.LOOM_WORK_FINDER raw -o - "$PLIST22" 2>/dev/null)
hg22=$(plutil -extract EnvironmentVariables.LOOM_MAIN_HEALTH_GATE raw -o - "$PLIST22" 2>/dev/null)
mc22=$(plutil -extract EnvironmentVariables.LOOM_WORK_FINDER_MAX_CONCURRENT raw -o - "$PLIST22" 2>/dev/null)
pt22=$(plutil -extract EnvironmentVariables.LOOM_PER_TOKEN_CONCURRENCY raw -o - "$PLIST22" 2>/dev/null)
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$wf22" == "1" && "$hg22" == "1" && "$mc22" == "10" && "$pt22" == "5" ]] \
    && ! grep -q 'sentinel-oldpath-4118' "$PLIST22" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --relaunch preserves all 4 autonomy env keys and does NOT round-trip the stale PATH (AC3)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --relaunch preserves all 4 autonomy env keys and does NOT round-trip the stale PATH (AC3)"
    echo "  WF=[$wf22] HG=[$hg22] MAXC=[$mc22] PERTOK=[$pt22]"
    echo "  plist: $(cat "$PLIST22" 2>/dev/null)"
fi
fi # end plutil-availability guard for scenarios 21-22

# ============================================================
# 23. --no-restart on a launchd host does NOT print a bare `launchctl bootstrap`
#     of the stale plist (the second #4118 stale-advice site): it names the
#     --relaunch re-render path and mentions bootout + sweeps (#5081-corrected
#     framing: bootout no longer kills in-flight sweeps on a current build,
#     but the async bootout/bootstrap race is still a reason to prefer
#     --relaunch's settled/retried/verified sequence over a hand-run one).
# ============================================================
W23="$BASE_WORKDIR/w23"
new_fixture "$W23"
INSTALLED23="$W23/installed/loom-daemon"
mkdir -p "$W23/installed"
RESTART_MARKER23="$W23/restart-invoked"
write_fake_daemon_restart "$INSTALLED23" "deadbee" "$RESTART_MARKER23" 0
NEW_FAKE23="$W23/new-fake-daemon"
HEAD23="$(cd "$W23" && git rev-parse --short HEAD)"
write_fake_daemon_restart "$NEW_FAKE23" "$HEAD23" "$RESTART_MARKER23" 0
LD_BIN23="$W23/launchd-bin"
write_fake_launchd_loaded_bin "$LD_BIN23" "$W23/launchctl.log"
out23=$( cd "$W23" && PATH="$LD_BIN23:$TEST_PATH" LOOM_DAEMON_LAUNCHD=1 \
    LOOM_DAEMON_BIN="$INSTALLED23" NEW_FAKE_BIN_SRC="$NEW_FAKE23" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1 )
rc23=$?
assert_eq "0" "$rc23" "--no-restart on a launchd host exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out23" | grep -q -- '--relaunch' \
    && ! echo "$out23" | grep -qi 'launchctl bootstrap' \
    && echo "$out23" | grep -qi 'bootout' && echo "$out23" | grep -qi 'sweep'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --no-restart names --relaunch, no bare bootstrap, mentions bootout+sweeps (#5081-corrected)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --no-restart names --relaunch, no bare bootstrap, mentions bootout+sweeps (#5081-corrected)"
    echo "  output: $out23"
fi

# ============================================================
# 24. --relaunch with the live plist ABSENT fails loudly (names the missing path)
#     and refuses to relaunch — it must NEVER silently render FLAGS-OFF defaults
#     (the #4011 class this whole line of work closes). No plist is written.
# ============================================================
W24="$BASE_WORKDIR/w24"
new_fixture "$W24"
INSTALLED24="$W24/installed/loom-daemon"
mkdir -p "$W24/installed"
RESTART_MARKER24="$W24/restart-invoked"
write_fake_daemon_restart "$INSTALLED24" "deadbee" "$RESTART_MARKER24" 1
NEW_FAKE24="$W24/new-fake-daemon"
HEAD24="$(cd "$W24" && git rev-parse --short HEAD)"
write_fake_daemon_restart "$NEW_FAKE24" "$HEAD24" "$RESTART_MARKER24" 1
LD_BIN24="$W24/launchd-bin"
write_fake_launchd_loaded_bin "$LD_BIN24" "$W24/launchctl.log"
HOME24="$W24/home"
mkdir -p "$HOME24/Library/LaunchAgents"   # dir exists, plist deliberately absent
PLIST24="$HOME24/Library/LaunchAgents/${LOOM_LAUNCHD_LABEL}.plist"
out24=$( cd "$W24" && PATH="$LD_BIN24:$TEST_PATH" HOME="$HOME24" LOOM_DAEMON_LAUNCHD=1 \
    LOOM_DAEMON_BIN="$INSTALLED24" NEW_FAKE_BIN_SRC="$NEW_FAKE24" \
    bash "$UPDATE_SCRIPT" --relaunch 2>&1 )
rc24=$?
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$rc24" -ne 0 ]] && echo "$out24" | grep -qi 'not found' \
    && echo "$out24" | grep -qF "$PLIST24" && [[ ! -e "$PLIST24" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --relaunch with an absent plist fails loudly (names the path) and renders nothing"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --relaunch with an absent plist fails loudly (names the path) and renders nothing"
    echo "  rc=$rc24 plist-exists=$([[ -e "$PLIST24" ]] && echo yes || echo no)"
    echo "  output: $out24"
fi

# ============================================================
# M1. Machine mode (Epic #3835 Phase 3b, #4229): LOOM_MACHINE_CHECKOUT lets
#     `--check` rebuild-detect against the CHECKOUT's source tree even when
#     $PWD is a totally unrelated non-Loom directory -- the concrete Gap 1
#     regression this issue closes ("only works inside a Loom source
#     checkout"). HOME is overridden to a scratch dir so DAEMON_STATE_HOME
#     ($HOME/.loom in machine mode) never touches the real operator ~/.loom,
#     matching the rest of this suite's isolation discipline.
# ============================================================
WM1="$BASE_WORKDIR/wm1-checkout"
new_fixture "$WM1"
HEADM1="$(cd "$WM1" && git rev-parse --short HEAD)"
write_fake_daemon "$WM1/installed-loom-daemon" "$HEADM1" "$WM1/marker"
NON_LOOM_DIR="$BASE_WORKDIR/wm1-non-loom-cwd"
mkdir -p "$NON_LOOM_DIR"
HOME_WM1="$BASE_WORKDIR/wm1-home"
mkdir -p "$HOME_WM1"
out=$( cd "$NON_LOOM_DIR" && PATH="$TEST_PATH" HOME="$HOME_WM1" LOOM_MACHINE_CHECKOUT="$WM1" \
    LOOM_DAEMON_BIN="$WM1/installed-loom-daemon" \
    bash "$UPDATE_SCRIPT" --check; echo "EXIT=$?" )
rc=$(echo "$out" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rc" "machine mode: --check from an unrelated non-Loom \$PWD resolves the checkout as source tree (Gap 1)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out" | grep -q "only works inside a Loom source checkout"; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} machine mode: never refuses with the dev-mode 'source checkout' error"
    echo "  output: $out"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} machine mode: never refuses with the dev-mode 'source checkout' error"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out" | grep -qF "$WM1"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} machine mode: reports the machine checkout as the resolved source tree"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} machine mode: reports the machine checkout as the resolved source tree"
    echo "  output: $out"
fi

# ============================================================
# M2 (#5140). CWD-independent source-tree resolution, direct invocation (no
#     dispatcher, no LOOM_MACHINE_CHECKOUT). The reported failure: the script
#     was invoked BY ABSOLUTE PATH from $HOME on a fleet host where
#     `~/.loom/tokens` exists (the token pool `loom-daemon tokens bootstrap`
#     provisions), so the old `.loom`-only upward walk matched $HOME on its
#     first iteration and refused with "No loom-daemon/Cargo.toml found at
#     $HOME/loom-daemon" -- a path the operator never named. Two fixes are
#     asserted here: the walk now requires .git ALONGSIDE .loom/ (so a bare
#     ~/.loom is never mistaken for a checkout), and the script falls back to
#     the checkout it physically lives in, which is unambiguous when it is
#     invoked by absolute path.
# ============================================================
WM2="$BASE_WORKDIR/wm2-checkout"
new_fixture "$WM2"
install_update_script_into "$WM2"
HEADM2="$(cd "$WM2" && git rev-parse --short HEAD)"
write_fake_daemon "$WM2/installed-loom-daemon" "$HEADM2" "$WM2/marker"
HOME_LIKE_M2="$BASE_WORKDIR/wm2-home"
mkdir -p "$HOME_LIKE_M2/.loom/tokens"   # machine-level daemon state, NOT a repo
out_m2=$( cd "$HOME_LIKE_M2" && PATH="$TEST_PATH" HOME="$HOME_LIKE_M2" \
    LOOM_DAEMON_BIN="$WM2/installed-loom-daemon" \
    bash "$WM2/.loom/scripts/cli/loom-daemon-update.sh" --check 2>&1; echo "EXIT=$?" )
rc_m2=$(echo "$out_m2" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rc_m2" "#5140: --check from a \$HOME-like dir holding a bare .loom/ resolves the script's own checkout"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out_m2" | grep -qF "$HOME_LIKE_M2/loom-daemon"; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} #5140: never resolves a bare ~/.loom directory as the repo root"
    echo "  output: $out_m2"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} #5140: never resolves a bare ~/.loom directory as the repo root"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out_m2" | grep -qF "using this script's own checkout: $WM2"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} #5140: announces the self-location fallback (never a silent switch)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} #5140: announces the self-location fallback (never a silent switch)"
    echo "  output: $out_m2"
fi

# ============================================================
# M3 (#5140, scope guard -- supersedes the pre-#5140 "dev-mode fallback"
#     assertion that used the REAL repo's script copy). When NEITHER $PWD NOR
#     the script's own location is inside a Loom checkout, the refusal is
#     preserved: exit 1, naming the CWD it searched and what a checkout
#     requires. A bare `.loom/` in the CWD must not change that. Machine mode
#     (M1) remains the additive escape hatch.
# ============================================================
WM3_TOOLS="$BASE_WORKDIR/wm3-tools"   # a script tree that is NOT a Loom checkout
mkdir -p "$WM3_TOOLS/cli" "$WM3_TOOLS/lib"
cp "$UPDATE_SCRIPT" "$WM3_TOOLS/cli/loom-daemon-update.sh"
cp "$CLI_DIR/../lib/"*.sh "$WM3_TOOLS/lib/"
NON_REPO_M3="$BASE_WORKDIR/wm3-cwd"
mkdir -p "$NON_REPO_M3/.loom"
HOME_M3="$BASE_WORKDIR/wm3-home"
mkdir -p "$HOME_M3"
out_m3=$( cd "$NON_REPO_M3" && PATH="$TEST_PATH" HOME="$HOME_M3" \
    bash "$WM3_TOOLS/cli/loom-daemon-update.sh" --check 2>&1; echo "EXIT=$?" )
rc_m3=$(echo "$out_m3" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "1" "$rc_m3" "#5140: refuses (exit 1) when neither \$PWD nor the script's own tree is a Loom checkout"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out_m3" | grep -qi "Not in a Loom workspace" && echo "$out_m3" | grep -qF "$NON_REPO_M3"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} #5140: the refusal names the CWD it searched"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} #5140: the refusal names the CWD it searched"
    echo "  output: $out_m3"
fi

# ============================================================
# 26. Systemd ownership + restart (#4260 sub-issue C, mirrors test 15): a
#     systemd-managed daemon (stub systemctl reports an ACTIVE unit + MainPID)
#     with NO .loom/.daemon.pid file -> the updater plans/executes a RESTART
#     (not "was not running"), drives it through the `restart` subcommand
#     (stub records the invocation), does NOT consult .daemon.flags, and exits
#     0. The stub relaunches onto a NEW, real, live pid the instant the restart
#     is accepted (#4950 case (a): relaunch observed -> success, no
#     reset-failed+start self-heal ever invoked). Forced via the
#     LOOM_SYSTEMD_FORCE=1 test seam (lib/systemd-user.sh) since this suite
#     runs on Darwin; LOOM_DAEMON_LAUNCHD=0 is already the suite-wide default
#     so launchd never competes for this tier.
# ============================================================
W26="$BASE_WORKDIR/w26"
new_fixture "$W26"
HEAD26="$(cd "$W26" && git rev-parse --short HEAD)"
INSTALLED26="$W26/installed/loom-daemon"
mkdir -p "$W26/installed"
RESTART_MARKER26="$W26/restart-invoked"
write_fake_daemon_restart "$INSTALLED26" "deadbee" "$RESTART_MARKER26" 0
NEW_FAKE26="$W26/new-fake-daemon"
write_fake_daemon_restart "$NEW_FAKE26" "$HEAD26" "$RESTART_MARKER26" 0
# A .daemon.flags that MUST NOT be consulted in systemd mode (would otherwise
# add --work-finder to a stop+start path).
echo "--work-finder" > "$W26/.loom/.daemon.flags"
# A real, live process standing in for "the relaunched unit's new MainPID" —
# the #4950 poll requires a live pid (kill -0), not just a differing number.
sleep 60 >/dev/null 2>&1 &
RELAUNCHED_PID26=$!
bg_proc_track "$RELAUNCHED_PID26"
SD_BIN26="$W26/systemd-bin"
SD_LOG26="$W26/systemctl.log"
SD_STATE26="$W26/systemd-pid-state"
write_fake_systemd_pid_bin "$SD_BIN26" "$SD_LOG26" "$SD_STATE26" "4242:active:success" \
    "$RESTART_MARKER26" "${RELAUNCHED_PID26}:active:success"

out26=$( cd "$W26" && PATH="$SD_BIN26:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd26.service" \
    LOOM_DAEMON_BIN="$INSTALLED26" NEW_FAKE_BIN_SRC="$NEW_FAKE26" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc26=$?
kill "$RELAUNCHED_PID26" 2>/dev/null || true
assert_eq "0" "$rc26" "systemd-managed update (no pid file) exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -s "$RESTART_MARKER26" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd restart driven through the 'restart' subcommand (not stop+start)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd restart driven through the 'restart' subcommand (not stop+start)"
    echo "  output: $out26"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out26" | grep -qi 'not running'; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd-loaded unit is NOT mistaken for 'was not running'"
    echo "  output: $out26"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd-loaded unit is NOT mistaken for 'was not running'"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out26" | grep -qi 'FLAGS-OFF'; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} no 'restarting FLAGS-OFF' warning fires for a systemd restart"
    echo "  output: $out26"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} no 'restarting FLAGS-OFF' warning fires for a systemd restart"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q "new pid ${RELAUNCHED_PID26}" <<< "$out26"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} success message reports the VERIFIED new MainPID (#4950)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} success message reports the VERIFIED new MainPID (#4950)"
    echo "  output: $out26"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'reset-failed' "$SD_LOG26" 2>/dev/null; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} relaunch observed immediately -> reset-failed+start self-heal is NEVER invoked (#4950 case a)"
    echo "  systemctl.log: $(cat "$SD_LOG26" 2>/dev/null)"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} relaunch observed immediately -> reset-failed+start self-heal is NEVER invoked (#4950 case a)"
fi

# ============================================================
# 27. Systemd restart REFUSED (mirrors test 16): a systemd unit is active but
#     the running (old) binary rejects the restart request (pre-#4267 binary /
#     dead socket). Without --relaunch the updater must exit NON-ZERO (6) and
#     print the systemd fallback: names --relaunch (NOT a bare `systemctl
#     --user stop`, which tears down the cgroup and kills in-flight sweeps),
#     and prefers a graceful `kill -TERM`.
# ============================================================
W27="$BASE_WORKDIR/w27"
new_fixture "$W27"
HEAD27="$(cd "$W27" && git rev-parse --short HEAD)"
INSTALLED27="$W27/installed/loom-daemon"
mkdir -p "$W27/installed"
RESTART_MARKER27="$W27/restart-invoked"
write_fake_daemon_restart "$INSTALLED27" "deadbee" "$RESTART_MARKER27" 1
NEW_FAKE27="$W27/new-fake-daemon"
write_fake_daemon_restart "$NEW_FAKE27" "$HEAD27" "$RESTART_MARKER27" 1
SD_BIN27="$W27/systemd-bin"
SD_LOG27="$W27/systemctl.log"
write_fake_systemd_active_bin "$SD_BIN27" "$SD_LOG27" "4242"

out27=$( cd "$W27" && PATH="$SD_BIN27:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd27.service" \
    LOOM_DAEMON_BIN="$INSTALLED27" NEW_FAKE_BIN_SRC="$NEW_FAKE27" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc27=$?
assert_eq "6" "$rc27" "systemd restart refused -> exit 6 (never a silent half-update)"
# Names --relaunch as the fix and NEVER recommends a bare `systemctl --user
# restart`/`enable` by hand as a shortcut (that would skip the re-render and
# refuse identically forever, the systemd analog of the #4118 bootstrap bug) —
# the ONLY manual systemctl-adjacent mention allowed is the "do NOT ... stop"
# warning itself.
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out27" | grep -q -- '--relaunch' \
    && ! echo "$out27" | grep -qi 'systemctl --user restart' \
    && ! echo "$out27" | grep -qi 'systemctl --user enable'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} refused restart names --relaunch, never a bare manual systemctl restart/enable"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} refused restart names --relaunch, never a bare manual systemctl restart/enable"
    echo "  output: $out27"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out27" | grep -qi 'cgroup' && echo "$out27" | grep -qi 'sweep' \
    && echo "$out27" | grep -q 'kill -TERM'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} refused restart warns stop tears down the cgroup + kills in-flight sweeps, prefers kill -TERM"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} refused restart warns stop tears down the cgroup + kills in-flight sweeps, prefers kill -TERM"
    echo "  output: $out27"
fi

# ============================================================
# 28. --check names systemd (with unit) as the owning manager when a unit is
#     active (mirrors test 17a).
# ============================================================
W28="$BASE_WORKDIR/w28"
new_fixture "$W28"
INSTALLED28="$W28/installed/loom-daemon"
mkdir -p "$W28/installed"
write_fake_daemon "$INSTALLED28" "deadbee" "$W28/marker"
SD_BIN28="$W28/systemd-bin"
SD_LOG28="$W28/systemctl.log"
write_fake_systemd_active_bin "$SD_BIN28" "$SD_LOG28" "4242"
check_sd_out=$( cd "$W28" && PATH="$SD_BIN28:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd28.service" LOOM_DAEMON_BIN="$INSTALLED28" \
    bash "$UPDATE_SCRIPT" --check 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$check_sd_out" | grep -qi 'manager: systemd' && echo "$check_sd_out" | grep -q 'loom-daemon-test-sd28.service'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --check names systemd (with unit) as the owning manager"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --check names systemd (with unit) as the owning manager"
    echo "  output: $check_sd_out"
fi

# ============================================================
# 29. --dry-run in systemd mode reports the systemd restart plan and makes no
#     writes (no rebuild, no restart invocation) (mirrors test 18).
# ============================================================
W29="$BASE_WORKDIR/w29"
new_fixture "$W29"
INSTALLED29="$W29/installed/loom-daemon"
mkdir -p "$W29/installed"
RESTART_MARKER29="$W29/restart-invoked"
write_fake_daemon_restart "$INSTALLED29" "deadbee" "$RESTART_MARKER29" 0
SD_BIN29="$W29/systemd-bin"
SD_LOG29="$W29/systemctl.log"
write_fake_systemd_active_bin "$SD_BIN29" "$SD_LOG29" "4242"
dry29_out=$( cd "$W29" && PATH="$SD_BIN29:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd29.service" LOOM_DAEMON_BIN="$INSTALLED29" \
    bash "$UPDATE_SCRIPT" --dry-run 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$dry29_out" | grep -qi 'systemd-managed' && echo "$dry29_out" | grep -qi 'restart' \
    && [[ ! -s "$RESTART_MARKER29" ]] && [[ ! -e "$W29/loom-daemon/target/release/loom-daemon" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --dry-run reports the systemd restart plan and makes no writes"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --dry-run reports the systemd restart plan and makes no writes"
    echo "  output: $dry29_out"
fi

# ============================================================
# 30. LOOM_DAEMON_SYSTEMD=0 skips all systemd tiers even with the unit detected
#     as active (LOOM_SYSTEMD_FORCE=1 + stub systemctl on PATH): the daemon is
#     treated as NOT systemd-managed, so with no pid file it is "not running"
#     (no restart attempted). Guards the --no-systemd symmetry contract (#4260
#     sub-issue C AC3, mirrors test 19).
# ============================================================
W30="$BASE_WORKDIR/w30"
new_fixture "$W30"
HEAD30="$(cd "$W30" && git rev-parse --short HEAD)"
INSTALLED30="$W30/installed/loom-daemon"
mkdir -p "$W30/installed"
RESTART_MARKER30="$W30/restart-invoked"
write_fake_daemon_restart "$INSTALLED30" "deadbee" "$RESTART_MARKER30" 0
NEW_FAKE30="$W30/new-fake-daemon"
write_fake_daemon_restart "$NEW_FAKE30" "$HEAD30" "$RESTART_MARKER30" 0
SD_BIN30="$W30/systemd-bin"
SD_LOG30="$W30/systemctl.log"
write_fake_systemd_active_bin "$SD_BIN30" "$SD_LOG30" "4242"
out30=$( cd "$W30" && PATH="$SD_BIN30:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=0 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd30.service" \
    LOOM_DAEMON_BIN="$INSTALLED30" NEW_FAKE_BIN_SRC="$NEW_FAKE30" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc30=$?
assert_eq "0" "$rc30" "LOOM_DAEMON_SYSTEMD=0 update exits 0 (rebuild+provision, no systemd)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out30" | grep -qi 'not running' && [[ ! -s "$RESTART_MARKER30" ]] && [[ ! -s "$SD_LOG30" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} LOOM_DAEMON_SYSTEMD=0 skips systemd tiers (no restart driven, zero systemctl calls)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} LOOM_DAEMON_SYSTEMD=0 skips systemd tiers (no restart driven, zero systemctl calls)"
    echo "  output: $out30"
    echo "  systemctl calls: $(cat "$SD_LOG30" 2>/dev/null)"
fi

# ============================================================
# 31. --relaunch re-renders the unit with the SUPERVISED keys (mirrors test
#     21): after a refused restart, `--relaunch` re-renders via
#     loom-daemon-start.sh, installing Restart=on-success +
#     LOOM_DAEMON_SUPERVISOR=systemd into the unit — the two keys the stale
#     pre-#4267 fixture unit LACKS, so a passing assertion proves the
#     re-render actually happened (not a leftover). HOME is sandboxed so
#     resolve_systemd_unit_path() resolves inside the test tree.
# ============================================================
W31="$BASE_WORKDIR/w31"
new_fixture "$W31"
INSTALLED31="$W31/installed/loom-daemon"
mkdir -p "$W31/installed"
RESTART_MARKER31="$W31/restart-invoked"
# Running (old) + fresh binaries both REFUSE restart (pre-#4267) -> exit-6 path.
write_fake_daemon_restart "$INSTALLED31" "deadbee" "$RESTART_MARKER31" 1
NEW_FAKE31="$W31/new-fake-daemon"
HEAD31="$(cd "$W31" && git rev-parse --short HEAD)"
write_fake_daemon_restart "$NEW_FAKE31" "$HEAD31" "$RESTART_MARKER31" 1
UNIT31="loom-daemon-test-sd31.service"
SD_BIN31="$W31/systemd-bin"
SD_LOG31="$W31/systemctl.log"
write_fake_systemd_active_bin "$SD_BIN31" "$SD_LOG31" "4242"
HOME31="$W31/home"
UNIT_PATH31="$HOME31/.config/systemd/user/${UNIT31}"
write_fixture_unit_pre4267 "$UNIT_PATH31" "$INSTALLED31"
( cd "$W31" && PATH="$SD_BIN31:$TEST_PATH" HOME="$HOME31" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="$UNIT31" \
    LOOM_DAEMON_BIN="$INSTALLED31" NEW_FAKE_BIN_SRC="$NEW_FAKE31" \
    bash "$UPDATE_SCRIPT" --relaunch >/dev/null 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'LOOM_DAEMON_SUPERVISOR=systemd' "$UNIT_PATH31" 2>/dev/null \
    && grep -qx 'Restart=on-success' "$UNIT_PATH31" 2>/dev/null \
    && grep -qx 'KillMode=mixed' "$UNIT_PATH31" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --relaunch re-renders the unit with Restart=on-success + KillMode=mixed (#4862) + LOOM_DAEMON_SUPERVISOR=systemd"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --relaunch re-renders the unit with Restart=on-success + KillMode=mixed + LOOM_DAEMON_SUPERVISOR=systemd"
    echo "  unit: $(cat "$UNIT_PATH31" 2>/dev/null)"
fi

# ============================================================
# 32. --relaunch PRESERVES the live unit's autonomy env across the re-render
#     (mirrors test 22): the four autonomy keys carry through byte-for-byte,
#     and the stale sentinel PATH is NOT round-tripped (start.sh rebuilds
#     PATH).
# ============================================================
W32="$BASE_WORKDIR/w32"
new_fixture "$W32"
INSTALLED32="$W32/installed/loom-daemon"
mkdir -p "$W32/installed"
RESTART_MARKER32="$W32/restart-invoked"
write_fake_daemon_restart "$INSTALLED32" "deadbee" "$RESTART_MARKER32" 1
NEW_FAKE32="$W32/new-fake-daemon"
HEAD32="$(cd "$W32" && git rev-parse --short HEAD)"
write_fake_daemon_restart "$NEW_FAKE32" "$HEAD32" "$RESTART_MARKER32" 1
UNIT32="loom-daemon-test-sd32.service"
SD_BIN32="$W32/systemd-bin"
SD_LOG32="$W32/systemctl.log"
write_fake_systemd_active_bin "$SD_BIN32" "$SD_LOG32" "4242"
HOME32="$W32/home"
UNIT_PATH32="$HOME32/.config/systemd/user/${UNIT32}"
write_fixture_unit_pre4267 "$UNIT_PATH32" "$INSTALLED32"
( cd "$W32" && PATH="$SD_BIN32:$TEST_PATH" HOME="$HOME32" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="$UNIT32" \
    LOOM_DAEMON_BIN="$INSTALLED32" NEW_FAKE_BIN_SRC="$NEW_FAKE32" \
    bash "$UPDATE_SCRIPT" --relaunch >/dev/null 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if grep -qx 'Environment=LOOM_WORK_FINDER=1' "$UNIT_PATH32" 2>/dev/null \
    && grep -qx 'Environment=LOOM_MAIN_HEALTH_GATE=1' "$UNIT_PATH32" 2>/dev/null \
    && grep -qx 'Environment=LOOM_WORK_FINDER_MAX_CONCURRENT=10' "$UNIT_PATH32" 2>/dev/null \
    && grep -qx 'Environment=LOOM_PER_TOKEN_CONCURRENCY=5' "$UNIT_PATH32" 2>/dev/null \
    && ! grep -q 'sentinel-oldpath-4267' "$UNIT_PATH32" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --relaunch preserves all 4 autonomy env keys and does NOT round-trip the stale PATH"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --relaunch preserves all 4 autonomy env keys and does NOT round-trip the stale PATH"
    echo "  unit: $(cat "$UNIT_PATH32" 2>/dev/null)"
fi

# ============================================================
# 33. --no-restart on a systemd host does NOT print a bare 'systemctl --user
#     stop' of the active unit: it names the --relaunch re-render path and
#     warns that stop kills in-flight sweeps (mirrors test 23).
# ============================================================
W33="$BASE_WORKDIR/w33"
new_fixture "$W33"
INSTALLED33="$W33/installed/loom-daemon"
mkdir -p "$W33/installed"
RESTART_MARKER33="$W33/restart-invoked"
write_fake_daemon_restart "$INSTALLED33" "deadbee" "$RESTART_MARKER33" 0
NEW_FAKE33="$W33/new-fake-daemon"
HEAD33="$(cd "$W33" && git rev-parse --short HEAD)"
write_fake_daemon_restart "$NEW_FAKE33" "$HEAD33" "$RESTART_MARKER33" 0
SD_BIN33="$W33/systemd-bin"
SD_LOG33="$W33/systemctl.log"
write_fake_systemd_active_bin "$SD_BIN33" "$SD_LOG33" "4242"
out33=$( cd "$W33" && PATH="$SD_BIN33:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd33.service" \
    LOOM_DAEMON_BIN="$INSTALLED33" NEW_FAKE_BIN_SRC="$NEW_FAKE33" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1 )
rc33=$?
assert_eq "0" "$rc33" "--no-restart on a systemd host exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out33" | grep -q -- '--relaunch' \
    && ! echo "$out33" | grep -qi 'systemctl --user restart' \
    && ! echo "$out33" | grep -qi 'systemctl --user enable' \
    && echo "$out33" | grep -qi 'cgroup' && echo "$out33" | grep -qi 'sweep'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --no-restart names --relaunch, no bare manual systemctl restart/enable, warns cgroup teardown kills sweeps"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --no-restart names --relaunch, no bare manual systemctl restart/enable, warns cgroup teardown kills sweeps"
    echo "  output: $out33"
fi

# ============================================================
# 34. --relaunch with the live unit file ABSENT fails loudly (names the
#     missing path) and refuses to relaunch — it must NEVER silently render
#     FLAGS-OFF defaults (the #4011 class this whole line of work closes). No
#     unit is written (mirrors test 24).
# ============================================================
W34="$BASE_WORKDIR/w34"
new_fixture "$W34"
INSTALLED34="$W34/installed/loom-daemon"
mkdir -p "$W34/installed"
RESTART_MARKER34="$W34/restart-invoked"
write_fake_daemon_restart "$INSTALLED34" "deadbee" "$RESTART_MARKER34" 1
NEW_FAKE34="$W34/new-fake-daemon"
HEAD34="$(cd "$W34" && git rev-parse --short HEAD)"
write_fake_daemon_restart "$NEW_FAKE34" "$HEAD34" "$RESTART_MARKER34" 1
UNIT34="loom-daemon-test-sd34.service"
SD_BIN34="$W34/systemd-bin"
SD_LOG34="$W34/systemctl.log"
write_fake_systemd_active_bin "$SD_BIN34" "$SD_LOG34" "4242"
HOME34="$W34/home"
mkdir -p "$HOME34/.config/systemd/user"   # dir exists, unit deliberately absent
UNIT_PATH34="$HOME34/.config/systemd/user/${UNIT34}"
out34=$( cd "$W34" && PATH="$SD_BIN34:$TEST_PATH" HOME="$HOME34" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="$UNIT34" \
    LOOM_DAEMON_BIN="$INSTALLED34" NEW_FAKE_BIN_SRC="$NEW_FAKE34" \
    bash "$UPDATE_SCRIPT" --relaunch 2>&1 )
rc34=$?
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$rc34" -ne 0 ]] && echo "$out34" | grep -qi 'not found' \
    && echo "$out34" | grep -qF "$UNIT_PATH34" && [[ ! -e "$UNIT_PATH34" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --relaunch with an absent unit fails loudly (names the path) and renders nothing"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --relaunch with an absent unit fails loudly (names the path) and renders nothing"
    echo "  rc=$rc34 unit-exists=$([[ -e "$UNIT_PATH34" ]] && echo yes || echo no)"
    echo "  output: $out34"
fi

# ============================================================
# 35. Restart ack'd but launchd never relaunches -> kickstart fallback DOES
#     relaunch it (#4232 case (b)): the restart request is accepted (exit 0)
#     but the reported pid never moves off the pre-restart pid on its own;
#     the updater falls back to a PLAIN 'launchctl kickstart' (never -k),
#     which (per the stub) IS what finally moves the pid — success, with a
#     remediation note in the output. Poll windows are shrunk via env so the
#     test runs fast without changing the production defaults.
# ============================================================
W35="$BASE_WORKDIR/w35"
new_fixture "$W35"
INSTALLED35="$W35/installed/loom-daemon"
mkdir -p "$W35/installed"
RESTART_MARKER35="$W35/restart-invoked"
write_fake_daemon_restart "$INSTALLED35" "deadbee" "$RESTART_MARKER35" 0
NEW_FAKE35="$W35/new-fake-daemon"
HEAD35="$(cd "$W35" && git rev-parse --short HEAD)"
write_fake_daemon_restart "$NEW_FAKE35" "$HEAD35" "$RESTART_MARKER35" 0

sleep 60 >/dev/null 2>&1 &
OLD_PID26=$!
bg_proc_track "$OLD_PID26"
sleep 60 >/dev/null 2>&1 &
KICKSTART_PID35=$!
bg_proc_track "$KICKSTART_PID35"
STATE35="$W35/launchd-pid-state"
echo "$OLD_PID26" > "$STATE35"
LD_BIN35="$W35/launchd-bin"
write_fake_launchd_pid_bin "$LD_BIN35" "$W35/launchctl.log" "$STATE35" "$KICKSTART_PID35"

out35=$( cd "$W35" && PATH="$LD_BIN35:$TEST_PATH" LOOM_DAEMON_LAUNCHD=1 \
    LOOM_DAEMON_BIN="$INSTALLED35" NEW_FAKE_BIN_SRC="$NEW_FAKE35" \
    LOOM_DAEMON_RESTART_POLL_SECS=1 LOOM_DAEMON_RESTART_POLL_INTERVAL=0.2 \
    LOOM_DAEMON_RESTART_KICKSTART_POLL_SECS=3 \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc26=$?
kill "$OLD_PID26" "$KICKSTART_PID35" 2>/dev/null || true
assert_eq "0" "$rc26" "no spontaneous relaunch -> kickstart fallback relaunches it -> exit 0 (#4232 case b)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out35" | grep -qi 'kickstart' && echo "$out35" | grep -qi 'remediation'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} success output names the kickstart fallback + a remediation note"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} success output names the kickstart fallback + a remediation note"
    echo "  output: $out35"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -qE '^kickstart [^ ]+$' "$W35/launchctl.log" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} kickstart is invoked PLAIN — never with -k"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} kickstart is invoked PLAIN — never with -k"
    echo "  launchctl.log: $(cat "$W35/launchctl.log" 2>/dev/null)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q -- '-k' "$W35/launchctl.log" 2>/dev/null; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} kickstart is NEVER invoked with -k (would risk killing a daemon that relaunched during the race window)"
    echo "  launchctl.log: $(cat "$W35/launchctl.log" 2>/dev/null)"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} kickstart is NEVER invoked with -k (would risk killing a daemon that relaunched during the race window)"
fi

# ============================================================
# 36. Restart ack'd, launchd never relaunches, AND the kickstart fallback also
#     never brings it up -> exit NON-ZERO (7), loudly, rather than a silent
#     half-update success (#4232 case (c)).
# ============================================================
W36="$BASE_WORKDIR/w36"
new_fixture "$W36"
INSTALLED36="$W36/installed/loom-daemon"
mkdir -p "$W36/installed"
RESTART_MARKER36="$W36/restart-invoked"
write_fake_daemon_restart "$INSTALLED36" "deadbee" "$RESTART_MARKER36" 0
NEW_FAKE36="$W36/new-fake-daemon"
HEAD36="$(cd "$W36" && git rev-parse --short HEAD)"
write_fake_daemon_restart "$NEW_FAKE36" "$HEAD36" "$RESTART_MARKER36" 0

sleep 60 >/dev/null 2>&1 &
OLD_PID27=$!
bg_proc_track "$OLD_PID27"
STATE36="$W36/launchd-pid-state"
echo "$OLD_PID27" > "$STATE36"
LD_BIN36="$W36/launchd-bin"
# No kickstart_new_pid given -> kickstart is a no-op; the pid never moves.
write_fake_launchd_pid_bin "$LD_BIN36" "$W36/launchctl.log" "$STATE36"

out36=$( cd "$W36" && PATH="$LD_BIN36:$TEST_PATH" LOOM_DAEMON_LAUNCHD=1 \
    LOOM_DAEMON_BIN="$INSTALLED36" NEW_FAKE_BIN_SRC="$NEW_FAKE36" \
    LOOM_DAEMON_RESTART_POLL_SECS=1 LOOM_DAEMON_RESTART_POLL_INTERVAL=0.2 \
    LOOM_DAEMON_RESTART_KICKSTART_POLL_SECS=1 \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc27=$?
kill "$OLD_PID27" 2>/dev/null || true
assert_eq "7" "$rc27" "no relaunch even after kickstart -> exit 7 (never a silent half-update, #4232 case c)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out36" | grep -qi 'FAILED' && echo "$out36" | grep -qi 'kickstart'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} failure output loudly reports the exhausted kickstart fallback"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} failure output loudly reports the exhausted kickstart fallback"
    echo "  output: $out36"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out36" | grep -qi 'last exit status'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} failure output includes launchctl print diagnostics (state/last exit status)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} failure output includes launchctl print diagnostics (state/last exit status)"
    echo "  output: $out36"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q -- '-k' "$W36/launchctl.log" 2>/dev/null; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} exhausted kickstart fallback was still never invoked with -k"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} exhausted kickstart fallback was still never invoked with -k"
fi

# ============================================================
# 37. ff-first default (#4330): local main is behind origin/main with a
#     CLEAN tree -> the ff-merge applies, and the rebuild below builds the
#     freshly-synced (post-merge) HEAD, not the stale pre-merge one.
# ============================================================
W37="$BASE_WORKDIR/w37"
BARE37="$BASE_WORKDIR/w37-origin.git"
new_fixture_with_origin "$W37" "$BARE37"
ORIGIN_TIP37="$(push_extra_commits_to_origin "$BARE37" 2)"
NEW_FAKE37="$W37/new-fake-daemon"
write_fake_daemon "$NEW_FAKE37" "$ORIGIN_TIP37" "$W37/new-marker"
out37=$( cd "$W37" && PATH="$TEST_PATH" NEW_FAKE_BIN_SRC="$NEW_FAKE37" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc37=$?
assert_eq "0" "$rc37" "ff-first: behind origin + clean tree -> update succeeds (ff-merge applied)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out37" | grep -qi 'Fast-forwarded'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} ff-first: reports the fast-forward sync"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} ff-first: reports the fast-forward sync"
    echo "  output: $out37"
fi
HEAD_AFTER37="$(cd "$W37" && git rev-parse --short HEAD)"
assert_eq "$ORIGIN_TIP37" "$HEAD_AFTER37" "ff-first: local HEAD now equals origin tip after the sync"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out37" | grep -qi "Build verification: freshly-built binary embeds source HEAD commit (${ORIGIN_TIP37})"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} ff-first: the rebuild is verified against the POST-merge HEAD, not the stale pre-merge one"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} ff-first: the rebuild is verified against the POST-merge HEAD, not the stale pre-merge one"
    echo "  output: $out37"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out37" | grep -qE "Installed: ${ORIGIN_TIP37} \(matches origin/main\)"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} final installed line states the built commit AND that it matches origin/main (AC4)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} final installed line states the built commit AND that it matches origin/main (AC4)"
    echo "  output: $out37"
fi

# ============================================================
# 38. ff-first default: local main is behind origin/main AND has a DIVERGED
#     local commit whose CONTENT genuinely differs from origin's (not merely
#     its commit history) -> the ff-merge cannot apply -> abort exit 1, no
#     merge, no build, working tree untouched. Never guesses or hard-resets.
#     This is the #4951 regression case: real (non-empty) content divergence
#     must NOT be reclassified as the content-identical case covered by tests
#     57/58 below — both sides here add a real, DIFFERENT file, so
#     `git diff origin/main...main` is genuinely non-empty.
# ============================================================
W38="$BASE_WORKDIR/w38"
BARE38="$BASE_WORKDIR/w38-origin.git"
new_fixture_with_origin "$W38" "$BARE38"
TMPCLONE38="$(mktemp -d)"
git clone -q "$BARE38" "$TMPCLONE38"
echo "origin-value" > "$TMPCLONE38/origin-only-file.txt"
( cd "$TMPCLONE38" && git add origin-only-file.txt && git -c user.email=test@test -c user.name=test commit -q -m "origin real change" && git push -q origin HEAD:refs/heads/main )
rm -rf "$TMPCLONE38"
# Diverge: a local commit that is NOT on origin, adding DIFFERENT real
# content (so the merge is not a plain fast-forward, AND the resulting
# content diff vs origin is genuinely non-empty).
echo "local-value" > "$W38/local-only-file.txt"
( cd "$W38" && git add local-only-file.txt && git -c user.email=test@test -c user.name=test commit -q -m "local diverged commit (real content)" )
HEAD_BEFORE38="$(cd "$W38" && git rev-parse --short HEAD)"
out38=$( cd "$W38" && PATH="$TEST_PATH" bash "$UPDATE_SCRIPT" 2>&1 )
rc38=$?
assert_eq "1" "$rc38" "ff-first: diverged local commit -> abort exit 1 (never guess or hard-reset)"
HEAD_AFTER38="$(cd "$W38" && git rev-parse --short HEAD)"
assert_eq "$HEAD_BEFORE38" "$HEAD_AFTER38" "ff-first: diverged case leaves local HEAD completely untouched"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out38" | grep -qi 'Refusing to guess or hard-reset'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} ff-first: diverged case reports the abort rationale"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} ff-first: diverged case reports the abort rationale"
    echo "  output: $out38"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -e "$W38/loom-daemon/target/release/loom-daemon" ]]; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} ff-first: diverged case performs no build"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} ff-first: diverged case performs no build"
fi

# ============================================================
# 39. --allow-stale restores the pre-#4330 build-what's-here behavior: the
#     ff-merge is skipped entirely, the current (stale) checkout is built,
#     and the behind-origin advisory warning is still printed.
# ============================================================
W39="$BASE_WORKDIR/w39"
BARE39="$BASE_WORKDIR/w39-origin.git"
new_fixture_with_origin "$W39" "$BARE39"
push_extra_commits_to_origin "$BARE39" 3 >/dev/null
HEAD_BEFORE39="$(cd "$W39" && git rev-parse --short HEAD)"
NEW_FAKE39="$W39/new-fake-daemon"
write_fake_daemon "$NEW_FAKE39" "$HEAD_BEFORE39" "$W39/new-marker"
out39=$( cd "$W39" && PATH="$TEST_PATH" NEW_FAKE_BIN_SRC="$NEW_FAKE39" \
    bash "$UPDATE_SCRIPT" --allow-stale 2>&1 )
rc39=$?
assert_eq "0" "$rc39" "--allow-stale: builds the current (stale) checkout, exits 0"
HEAD_AFTER39="$(cd "$W39" && git rev-parse --short HEAD)"
assert_eq "$HEAD_BEFORE39" "$HEAD_AFTER39" "--allow-stale: local HEAD is never merged/advanced"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out39" | grep -qi 'behind origin/main.*--allow-stale'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --allow-stale: the behind-origin advisory warning is still printed"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --allow-stale: the behind-origin advisory warning is still printed"
    echo "  output: $out39"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out39" | grep -qE "Installed: ${HEAD_BEFORE39} \(origin/main is at .* does NOT match"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --allow-stale: final installed line reports the built commit does NOT match origin/main (AC4)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --allow-stale: final installed line reports the built commit does NOT match origin/main (AC4)"
    echo "  output: $out39"
fi

# ============================================================
# 40. --check / --dry-run stay read-only even when behind origin: no fetch
#     result may write to the tree — HEAD is unchanged after either mode, and
#     --check reports the behind-origin status informationally while
#     --dry-run's printed plan mentions the ff-sync.
# ============================================================
W40="$BASE_WORKDIR/w40"
BARE40="$BASE_WORKDIR/w40-origin.git"
new_fixture_with_origin "$W40" "$BARE40"
push_extra_commits_to_origin "$BARE40" 1 >/dev/null
HEAD_BEFORE40="$(cd "$W40" && git rev-parse --short HEAD)"
out40_check=$( cd "$W40" && PATH="$TEST_PATH" bash "$UPDATE_SCRIPT" --check 2>&1 )
HEAD_AFTER40_CHECK="$(cd "$W40" && git rev-parse --short HEAD)"
assert_eq "$HEAD_BEFORE40" "$HEAD_AFTER40_CHECK" "--check while behind origin: HEAD unchanged (no writes contract holds)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out40_check" | grep -qi 'behind origin/main'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --check reports the behind-origin status informationally"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --check reports the behind-origin status informationally"
    echo "  output: $out40_check"
fi
out40_dry=$( cd "$W40" && PATH="$TEST_PATH" bash "$UPDATE_SCRIPT" --dry-run 2>&1 )
HEAD_AFTER40_DRY="$(cd "$W40" && git rev-parse --short HEAD)"
assert_eq "$HEAD_BEFORE40" "$HEAD_AFTER40_DRY" "--dry-run while behind origin: HEAD unchanged (no writes contract holds)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out40_dry" | grep -qi 'fast-forward.*origin/main'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --dry-run's printed plan mentions the ff-sync"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --dry-run's printed plan mentions the ff-sync"
    echo "  output: $out40_dry"
fi

# ============================================================
# 41. HEAD on a non-default branch while behind origin -> abort, naming
#     --allow-stale (an operator deliberately elsewhere — e.g. bisecting — is
#     exactly the --allow-stale use case, never a guess-which-branch merge).
# ============================================================
W41="$BASE_WORKDIR/w41"
BARE41="$BASE_WORKDIR/w41-origin.git"
new_fixture_with_origin "$W41" "$BARE41"
push_extra_commits_to_origin "$BARE41" 1 >/dev/null
( cd "$W41" && git checkout -q -b feature-branch )
HEAD_BEFORE41="$(cd "$W41" && git rev-parse --short HEAD)"
out41=$( cd "$W41" && PATH="$TEST_PATH" bash "$UPDATE_SCRIPT" 2>&1 )
rc41=$?
assert_eq "1" "$rc41" "non-default branch while behind -> abort exit 1"
HEAD_AFTER41="$(cd "$W41" && git rev-parse --short HEAD)"
assert_eq "$HEAD_BEFORE41" "$HEAD_AFTER41" "non-default branch while behind: HEAD untouched"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out41" | grep -q -- '--allow-stale'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} abort message names --allow-stale as the deliberate escape hatch"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} abort message names --allow-stale as the deliberate escape hatch"
    echo "  output: $out41"
fi

# ============================================================
# 42. Fetch failure (origin unreachable) must NOT make the script
#     network-dependent: warn and proceed with local HEAD as-is, exactly like
#     today's behavior — never abort, never hang past the bounded fetch.
# ============================================================
W42="$BASE_WORKDIR/w42"
BARE42="$BASE_WORKDIR/w42-origin.git"
new_fixture_with_origin "$W42" "$BARE42"
# refs/remotes/origin/HEAD is now cached locally (set by new_fixture_with_origin
# above), so loom_default_branch() can still name "main" via its offline,
# no-network tier even though the URL below makes the ACTUAL fetch fail —
# isolating "fetch failed" from "branch unresolvable" (test 43 covers the latter).
( cd "$W42" && git remote set-url origin "$BASE_WORKDIR/does-not-exist-w42.git" )
HEAD42="$(cd "$W42" && git rev-parse --short HEAD)"
INSTALLED42="$W42/installed/loom-daemon"
mkdir -p "$W42/installed"
write_fake_daemon "$INSTALLED42" "$HEAD42" "$W42/old-marker"
out42=$( cd "$W42" && PATH="$TEST_PATH" LOOM_DAEMON_BIN="$INSTALLED42" \
    bash "$UPDATE_SCRIPT" --check 2>&1 )
rc42=$?
assert_eq "0" "$rc42" "fetch failure: proceeds as today (installed already matches local HEAD) -> exit 0"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out42" | grep -qi 'could not reach origin'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} fetch failure surfaces a warning instead of aborting"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} fetch failure surfaces a warning instead of aborting"
    echo "  output: $out42"
fi

# ============================================================
# 43. Signaling "unknown currency" honestly: a fixture with NO origin remote
#     at all (loom_default_branch cannot resolve a default branch) still
#     completes normally, and the final installed line says so explicitly
#     rather than silently omitting the currency claim.
# ============================================================
W43="$BASE_WORKDIR/w43"
new_fixture "$W43"
HEAD43="$(cd "$W43" && git rev-parse --short HEAD)"
NEW_FAKE43="$W43/new-fake-daemon"
write_fake_daemon "$NEW_FAKE43" "$HEAD43" "$W43/new-marker"
out43=$( cd "$W43" && PATH="$TEST_PATH" NEW_FAKE_BIN_SRC="$NEW_FAKE43" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc43=$?
assert_eq "0" "$rc43" "no origin remote at all: update still succeeds"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out43" | grep -qi 'currency vs origin/<default-branch> unknown'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} final installed line honestly reports unknown currency when origin is unresolvable"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} final installed line honestly reports unknown currency when origin is unresolvable"
    echo "  output: $out43"
fi

# ============================================================
# 45-48. Stale `loom-*` entry-point advisory (#4079 hardening, epic #4081
#     Phase 4 / #4557). The update script scans PATH for `loom-*` executables
#     that do not resolve to the loom-daemon binary it resolved, and warns.
#
#     Fixture PATH holds, in one directory:
#       - loom-daemon           : the resolved binary itself (never flagged)
#       - loom-clean            : a legit auto-generated shim exec'ing the
#                                 sibling loom-daemon (never flagged)
#       - loom-tokens           : a STALE pip console script (MUST be flagged)
#       - loom-agent-spawn      : a second stale pip console script (flagged)
#       - loom-search           : a third stale console script (flagged) — the
#                                 `loom-search` carve-out was itself retired in
#                                 #4970, so it is no longer allowlisted and a
#                                 leftover binary is exactly the #4079 shape.
#
#     LOOM_SKIP_STALE_ENTRY_POINT_CHECK=1 must silence all of it, and the
#     advisory must never change the exit code.
# ============================================================
W45="$BASE_WORKDIR/w45"
new_fixture "$W45"
HEAD45="$(cd "$W45" && git rev-parse --short HEAD)"
STALE_BIN_DIR="$W45/stale-bin"
mkdir -p "$STALE_BIN_DIR"

# The resolved daemon binary, on PATH.
write_fake_daemon "$STALE_BIN_DIR/loom-daemon" "$HEAD45" "$W45/marker45"

# A legitimate auto-generated PATH shim (provision-daemon.sh's template).
cat > "$STALE_BIN_DIR/loom-clean" <<'SHIM'
#!/usr/bin/env bash
# Auto-generated PATH shim (issue #4272) — do not edit by hand.
exec "$(dirname "$0")/loom-daemon" clean "$@"
SHIM
chmod +x "$STALE_BIN_DIR/loom-clean"

# Three stale pip/PATH console scripts of the #4079 shape (including
# `loom-search`, no longer allowlisted post-#4970).
for _stale in loom-tokens loom-agent-spawn loom-search; do
    cat > "$STALE_BIN_DIR/$_stale" <<'STALEPY'
#!/usr/bin/python3
# -*- coding: utf-8 -*-
import sys
sys.exit(0)
STALEPY
    chmod +x "$STALE_BIN_DIR/$_stale"
done

out45=$( cd "$W45" && PATH="$STALE_BIN_DIR:$TEST_PATH" \
    LOOM_DAEMON_BIN="$STALE_BIN_DIR/loom-daemon" \
    bash "$UPDATE_SCRIPT" --check 2>&1; echo "EXIT=$?" )
rc45=$(echo "$out45" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)

# 45. The three stale console scripts are named.
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out45" | grep -q "Stale 'loom-\*' entry points found on PATH" \
   && echo "$out45" | grep -q "$STALE_BIN_DIR/loom-tokens" \
   && echo "$out45" | grep -q "$STALE_BIN_DIR/loom-agent-spawn" \
   && echo "$out45" | grep -q "$STALE_BIN_DIR/loom-search"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} stale loom-* PATH entry points are warned about by path (#4079/#4557/#4970)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} stale loom-* PATH entry points are warned about by path (#4079/#4557/#4970)"
    echo "  output: $out45"
fi

# 46. The legit shim and the resolved binary are NOT flagged (a false positive
#     here would train operators to ignore the check).
TESTS_RUN=$((TESTS_RUN + 1))
_stale_block="$(echo "$out45" | sed -n "/Stale 'loom-\*' entry points/,/Suppress this check/p")"
if ! echo "$_stale_block" | grep -q 'loom-clean' \
   && ! echo "$_stale_block" | grep -qE '(^|/)loom-daemon —'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} the current shim and the resolved binary are not flagged as stale"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} the current shim and the resolved binary are not flagged as stale"
    echo "  stale block: $_stale_block"
fi

# 47. The advisory does NOT change the exit code (--check with a matching commit
#     is still a clean exit 0).
assert_eq "0" "$rc45" "the stale-entry-point advisory never changes the exit code"

# 48. LOOM_SKIP_STALE_ENTRY_POINT_CHECK=1 silences it entirely.
out48=$( cd "$W45" && PATH="$STALE_BIN_DIR:$TEST_PATH" \
    LOOM_DAEMON_BIN="$STALE_BIN_DIR/loom-daemon" \
    LOOM_SKIP_STALE_ENTRY_POINT_CHECK=1 \
    bash "$UPDATE_SCRIPT" --check 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if ! echo "$out48" | grep -q "Stale 'loom-\*' entry points"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} LOOM_SKIP_STALE_ENTRY_POINT_CHECK=1 suppresses the advisory"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} LOOM_SKIP_STALE_ENTRY_POINT_CHECK=1 suppresses the advisory"
    echo "  output: $out48"
fi

# ============================================================
# 49-53. Idle-shutdown cron-guard post-update notice (#4697): the update
#     script should warn, post-update, that this host will power itself off
#     after N idle minutes when the stage-2 `fleet add-worker
#     --idle-shutdown-minutes` cron guard is installed — and stay silent
#     when it is not. Uses --check (fast, no rebuild) exactly like the
#     stale-entry-point block above; a sandboxed $HOME49 keeps the
#     `$HOME/.local/bin/loom-idle-shutdown.sh` lookup off the real operator
#     account (belt-and-braces alongside the fake `crontab` stub above).
# ============================================================
W49="$BASE_WORKDIR/w49"
new_fixture "$W49"
HEAD49="$(cd "$W49" && git rev-parse --short HEAD)"
write_fake_daemon "$W49/resolved-daemon" "$HEAD49" "$W49/marker49"

HOME49="$BASE_WORKDIR/home49"
mkdir -p "$HOME49/.local/bin"
cat > "$HOME49/.local/bin/loom-idle-shutdown.sh" <<'GUARD'
#!/usr/bin/env bash
set -euo pipefail
LIMIT=45
GUARD
chmod +x "$HOME49/.local/bin/loom-idle-shutdown.sh"

CRONFIX49="$BASE_WORKDIR/cron49-with-guard"
echo "*/5 * * * * $HOME49/.local/bin/loom-idle-shutdown.sh >/dev/null 2>&1" > "$CRONFIX49"

# 49. Guard installed + configured window readable -> notice names "45".
out49=$( cd "$W49" && PATH="$TEST_PATH" HOME="$HOME49" \
    LOOM_DAEMON_BIN="$W49/resolved-daemon" \
    FAKE_CRONTAB_CONTENTS_FILE="$CRONFIX49" \
    bash "$UPDATE_SCRIPT" --check 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out49" | grep -q 'idle-shutdown cron guard installed' \
   && echo "$out49" | grep -q -- '--idle-shutdown-minutes 45)'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} idle-shutdown guard installed -> post-update notice names the configured minutes (#4697)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} idle-shutdown guard installed -> post-update notice names the configured minutes (#4697)"
    echo "  output: $out49"
fi

# 50. No guard installed (empty crontab fixture) -> completely silent.
CRONFIX50="$BASE_WORKDIR/cron50-empty"
: > "$CRONFIX50"
out50=$( cd "$W49" && PATH="$TEST_PATH" HOME="$HOME49" \
    LOOM_DAEMON_BIN="$W49/resolved-daemon" \
    FAKE_CRONTAB_CONTENTS_FILE="$CRONFIX50" \
    bash "$UPDATE_SCRIPT" --check 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if ! echo "$out50" | grep -qi 'idle-shutdown'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} no idle-shutdown guard installed -> no notice at all (#4697)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} no idle-shutdown guard installed -> no notice at all (#4697)"
    echo "  output: $out50"
fi

# 51. LOOM_SKIP_IDLE_SHUTDOWN_NOTICE=1 suppresses it even with the guard present.
out51=$( cd "$W49" && PATH="$TEST_PATH" HOME="$HOME49" \
    LOOM_DAEMON_BIN="$W49/resolved-daemon" \
    FAKE_CRONTAB_CONTENTS_FILE="$CRONFIX49" \
    LOOM_SKIP_IDLE_SHUTDOWN_NOTICE=1 \
    bash "$UPDATE_SCRIPT" --check 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if ! echo "$out51" | grep -qi 'idle-shutdown'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} LOOM_SKIP_IDLE_SHUTDOWN_NOTICE=1 suppresses the notice"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} LOOM_SKIP_IDLE_SHUTDOWN_NOTICE=1 suppresses the notice"
    echo "  output: $out51"
fi

# 52. The notice never changes the exit code (still exit 0 -- up to date).
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out49" | grep -q . && ( cd "$W49" && PATH="$TEST_PATH" HOME="$HOME49" \
    LOOM_DAEMON_BIN="$W49/resolved-daemon" \
    FAKE_CRONTAB_CONTENTS_FILE="$CRONFIX49" \
    bash "$UPDATE_SCRIPT" --check >/dev/null 2>&1 ); then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} the idle-shutdown notice never changes the exit code"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} the idle-shutdown notice never changes the exit code"
fi

# 53. Guard entry present in cron but the guard SCRIPT (and thus its LIMIT=)
#     unreadable -- e.g. a hand-installed or relocated guard. The notice must
#     still fire (the host still powers itself off) with the honest
#     "window unknown" wording rather than silently degrading to nothing or
#     inventing a minutes value. $HOME53 has no .local/bin/loom-idle-shutdown.sh.
HOME53="$BASE_WORKDIR/home53"
mkdir -p "$HOME53/.local/bin"
out53=$( cd "$W49" && PATH="$TEST_PATH" HOME="$HOME53" \
    LOOM_DAEMON_BIN="$W49/resolved-daemon" \
    FAKE_CRONTAB_CONTENTS_FILE="$CRONFIX49" \
    bash "$UPDATE_SCRIPT" --check 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out53" | grep -q 'idle-shutdown cron guard installed' \
   && echo "$out53" | grep -q 'could not be read from'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} unreadable guard script -> notice still fires with an honest unknown window (#4697)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} unreadable guard script -> notice still fires with an honest unknown window (#4697)"
    echo "  output: $out53"
fi

# ============================================================
# 54. Systemd restart ack'd but the unit never relaunches on its own AND lands
#     in 'failed (Result: timeout)' -> the #4950 self-heal ('systemctl --user
#     reset-failed <unit> && systemctl --user start <unit>') DOES recover it:
#     the restart request is accepted (exit 0) but the reported MainPID never
#     moves off the pre-restart pid until AFTER reset-failed+start are invoked
#     -- success, with a remediation note in the output. This is the exact
#     2026-08-02 incident shape this issue closes. Poll windows are shrunk via
#     env so the test runs fast without changing the production defaults.
# ============================================================
W54="$BASE_WORKDIR/w54"
new_fixture "$W54"
INSTALLED54="$W54/installed/loom-daemon"
mkdir -p "$W54/installed"
RESTART_MARKER54="$W54/restart-invoked"
write_fake_daemon_restart "$INSTALLED54" "deadbee" "$RESTART_MARKER54" 0
NEW_FAKE54="$W54/new-fake-daemon"
HEAD54="$(cd "$W54" && git rev-parse --short HEAD)"
write_fake_daemon_restart "$NEW_FAKE54" "$HEAD54" "$RESTART_MARKER54" 0

sleep 60 >/dev/null 2>&1 &
RECOVERED_PID54=$!
bg_proc_track "$RECOVERED_PID54"
SD_BIN54="$W54/systemd-bin"
SD_LOG54="$W54/systemctl.log"
SD_STATE54="$W54/systemd-pid-state"
# pre_state: OLD pid, active/success. post_state (once restart is requested):
# 0:failed:timeout -- Restart=on-success never fires for a failed unit.
# recovery_state: only reached via reset-failed + start -- a NEW, live pid.
write_fake_systemd_pid_bin "$SD_BIN54" "$SD_LOG54" "$SD_STATE54" "9999:active:success" \
    "$RESTART_MARKER54" "0:failed:timeout" "${RECOVERED_PID54}:active:success"

out54=$( cd "$W54" && PATH="$SD_BIN54:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd54.service" \
    LOOM_DAEMON_BIN="$INSTALLED54" NEW_FAKE_BIN_SRC="$NEW_FAKE54" \
    LOOM_DAEMON_RESTART_POLL_SECS=1 LOOM_DAEMON_RESTART_POLL_INTERVAL=0.2 \
    LOOM_DAEMON_RESTART_KICKSTART_POLL_SECS=3 \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc54=$?
kill "$RECOVERED_PID54" 2>/dev/null || true
assert_eq "0" "$rc54" "no spontaneous relaunch, unit failed -> reset-failed+start recovers it -> exit 0 (#4950)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out54" | grep -qi 'reset-failed' && echo "$out54" | grep -qi 'remediation'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} success output names the reset-failed+start self-heal + a remediation note"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} success output names the reset-failed+start self-heal + a remediation note"
    echo "  output: $out54"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q -- 'reset-failed' "$SD_LOG54" 2>/dev/null \
    && grep -qE -- '(^|[[:space:]])start([[:space:]]|$)' "$SD_LOG54" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} self-heal invokes the EXACT documented recovery: reset-failed then start"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} self-heal invokes the EXACT documented recovery: reset-failed then start"
    echo "  systemctl.log: $(cat "$SD_LOG54" 2>/dev/null)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q "new pid ${RECOVERED_PID54}" <<< "$out54"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} success message reports the pid recovered by the self-heal"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} success message reports the pid recovered by the self-heal"
    echo "  output: $out54"
fi

# ============================================================
# 55. Systemd restart ack'd, unit never relaunches, lands in 'failed
#     (Result: timeout)', AND the reset-failed+start self-heal ALSO fails to
#     bring it up -> exit NON-ZERO (7), loudly, rather than a silent
#     half-update success (#4950 case (c), mirrors test 36).
# ============================================================
W55="$BASE_WORKDIR/w55"
new_fixture "$W55"
INSTALLED55="$W55/installed/loom-daemon"
mkdir -p "$W55/installed"
RESTART_MARKER55="$W55/restart-invoked"
write_fake_daemon_restart "$INSTALLED55" "deadbee" "$RESTART_MARKER55" 0
NEW_FAKE55="$W55/new-fake-daemon"
HEAD55="$(cd "$W55" && git rev-parse --short HEAD)"
write_fake_daemon_restart "$NEW_FAKE55" "$HEAD55" "$RESTART_MARKER55" 0

SD_BIN55="$W55/systemd-bin"
SD_LOG55="$W55/systemctl.log"
SD_STATE55="$W55/systemd-pid-state"
# No recovery_state given -> reset-failed+start leaves the unit inactive; the
# pid never moves.
write_fake_systemd_pid_bin "$SD_BIN55" "$SD_LOG55" "$SD_STATE55" "9999:active:success" \
    "$RESTART_MARKER55" "0:failed:timeout"

out55=$( cd "$W55" && PATH="$SD_BIN55:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd55.service" \
    LOOM_DAEMON_BIN="$INSTALLED55" NEW_FAKE_BIN_SRC="$NEW_FAKE55" \
    LOOM_DAEMON_RESTART_POLL_SECS=1 LOOM_DAEMON_RESTART_POLL_INTERVAL=0.2 \
    LOOM_DAEMON_RESTART_KICKSTART_POLL_SECS=1 \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc55=$?
assert_eq "7" "$rc55" "no relaunch even after reset-failed+start -> exit 7 (never a silent half-update, #4950 case c)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out55" | grep -qi 'FAILED' && echo "$out55" | grep -qi 'reset-failed'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} failure output loudly reports the exhausted self-heal fallback"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} failure output loudly reports the exhausted self-heal fallback"
    echo "  output: $out55"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out55" | grep -qi 'Active:' && echo "$out55" | grep -qi 'Result:'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} failure output includes systemctl status diagnostics (Active:/Result:)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} failure output includes systemctl status diagnostics (Active:/Result:)"
    echo "  output: $out55"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q -- 'reset-failed' "$SD_LOG55" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} exhausted self-heal still attempted the documented reset-failed recovery"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} exhausted self-heal still attempted the documented reset-failed recovery"
    echo "  systemctl.log: $(cat "$SD_LOG55" 2>/dev/null)"
fi

# ============================================================
# 56. Systemd restart ack'd but never relaunches, and the unit is STUCK in a
#     transitional state that never settles (e.g. `activating` forever) -> under
#     #5119 the updater WAITS the bounded settle window and then STILL attempts
#     the documented reset-failed+start recovery (rather than the pre-#5119
#     "refusing to guess" — which left the daemon down). The recovery here does
#     not bring it up, so it exits 7 loudly, but reset-failed WAS invoked. This
#     supersedes the old #4950 "self-heal gated on a confirmed failed state"
#     behavior: a transitional stall is exactly the 2026-08-03 incident shape.
# ============================================================
W56="$BASE_WORKDIR/w56"
new_fixture "$W56"
INSTALLED56="$W56/installed/loom-daemon"
mkdir -p "$W56/installed"
RESTART_MARKER56="$W56/restart-invoked"
write_fake_daemon_restart "$INSTALLED56" "deadbee" "$RESTART_MARKER56" 0
NEW_FAKE56="$W56/new-fake-daemon"
HEAD56="$(cd "$W56" && git rev-parse --short HEAD)"
write_fake_daemon_restart "$NEW_FAKE56" "$HEAD56" "$RESTART_MARKER56" 0

SD_BIN56="$W56/systemd-bin"
SD_LOG56="$W56/systemctl.log"
SD_STATE56="$W56/systemd-pid-state"
# post_state is 'activating' and NO settle_state is given -> the stub stays
# transitional forever, so the settle wait times out and the recovery is still
# attempted against the (never-recovering) unit.
write_fake_systemd_pid_bin "$SD_BIN56" "$SD_LOG56" "$SD_STATE56" "9999:active:success" \
    "$RESTART_MARKER56" "0:activating:success"

out56=$( cd "$W56" && PATH="$SD_BIN56:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd56.service" \
    LOOM_DAEMON_BIN="$INSTALLED56" NEW_FAKE_BIN_SRC="$NEW_FAKE56" \
    LOOM_DAEMON_RESTART_POLL_SECS=1 LOOM_DAEMON_RESTART_POLL_INTERVAL=0.2 \
    LOOM_DAEMON_STOP_SETTLE_SECS=1 LOOM_DAEMON_RESTART_KICKSTART_POLL_SECS=1 \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc56=$?
assert_eq "7" "$rc56" "transitional stall never recovers -> exit 7 after attempted self-heal (#5119)"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q -- 'reset-failed' "$SD_LOG56" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} a transitional stall now ATTEMPTS reset-failed+start recovery, not 'refusing to guess' (#5119)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} a transitional stall now ATTEMPTS reset-failed+start recovery, not 'refusing to guess' (#5119)"
    echo "  systemctl.log: $(cat "$SD_LOG56" 2>/dev/null)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out56" | grep -qi 'FAILED'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} failure output loudly reports the unconfirmed relaunch"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} failure output loudly reports the unconfirmed relaunch"
    echo "  output: $out56"
fi

# ============================================================
# 56b. THE #5119 INCIDENT (2026-08-03 loom-worker-1): systemd restart ack'd, the
#      daemon exited 0, but on a busy host the unit sat in
#      `deactivating (stop-sigterm)` while systemd reaped the sweep/role children
#      still in the cgroup — long past the #4950 pid poll on a stale unit's 90s
#      TimeoutStopSec. The pre-#5119 code read ActiveState once, saw
#      `deactivating` (not yet `failed`), and "refused to guess" -> exit 7, daemon
#      left DOWN. Under #5119 the updater WAITS for the stop to settle (here it
#      lands in `failed`, Result=timeout — the classic escalation) and then
#      reset-failed+start RECOVERS it -> exit 0, no manual intervention. This is
#      the acceptance-criteria scenario for the issue.
# ============================================================
W56B="$BASE_WORKDIR/w56b"
new_fixture "$W56B"
INSTALLED56B="$W56B/installed/loom-daemon"
mkdir -p "$W56B/installed"
RESTART_MARKER56B="$W56B/restart-invoked"
write_fake_daemon_restart "$INSTALLED56B" "deadbee" "$RESTART_MARKER56B" 0
NEW_FAKE56B="$W56B/new-fake-daemon"
HEAD56B="$(cd "$W56B" && git rev-parse --short HEAD)"
write_fake_daemon_restart "$NEW_FAKE56B" "$HEAD56B" "$RESTART_MARKER56B" 0

sleep 60 >/dev/null 2>&1 &
RECOVERED_PID56B=$!
bg_proc_track "$RECOVERED_PID56B"
SD_BIN56B="$W56B/systemd-bin"
SD_LOG56B="$W56B/systemctl.log"
SD_STATE56B="$W56B/systemd-pid-state"
# post_state: the mid-teardown snapshot the pid poll observes (deactivating,
# Result=timeout). settle_state: what it lands in after the stop completes
# (failed). recovery_state: only reset-failed+start reaches a NEW, live pid.
write_fake_systemd_pid_bin "$SD_BIN56B" "$SD_LOG56B" "$SD_STATE56B" "9999:active:success" \
    "$RESTART_MARKER56B" "0:deactivating:timeout" "${RECOVERED_PID56B}:active:success" \
    "0:failed:timeout"

out56b=$( cd "$W56B" && PATH="$SD_BIN56B:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd56b.service" \
    LOOM_DAEMON_BIN="$INSTALLED56B" NEW_FAKE_BIN_SRC="$NEW_FAKE56B" \
    LOOM_DAEMON_RESTART_POLL_SECS=1 LOOM_DAEMON_RESTART_POLL_INTERVAL=0.2 \
    LOOM_DAEMON_STOP_SETTLE_SECS=3 LOOM_DAEMON_RESTART_KICKSTART_POLL_SECS=3 \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc56b=$?
kill "$RECOVERED_PID56B" 2>/dev/null || true
assert_eq "0" "$rc56b" "deactivating stall settles to failed -> settle-wait + reset-failed+start recovers -> exit 0 (#5119)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out56b" | grep -qi 'settle' || echo "$out56b" | grep -qi 'transitioning'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} output names the settle-wait for the still-transitioning stop (#5119)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} output names the settle-wait for the still-transitioning stop (#5119)"
    echo "  output: $out56b"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q -- 'reset-failed' "$SD_LOG56B" 2>/dev/null \
    && grep -qE -- '(^|[[:space:]])start([[:space:]]|$)' "$SD_LOG56B" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} the incident recovery invokes the documented reset-failed then start"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} the incident recovery invokes the documented reset-failed then start"
    echo "  systemctl.log: $(cat "$SD_LOG56B" 2>/dev/null)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q "new pid ${RECOVERED_PID56B}" <<< "$out56b"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} success message reports the pid recovered after the settle-wait (#5119)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} success message reports the pid recovered after the settle-wait (#5119)"
    echo "  output: $out56b"
fi

# ============================================================
# 57. ff-abort classification (#4951): local <default> has DIVERGED from
#     origin/<default> in commit history, but the two are content-IDENTICAL
#     (`git diff origin/main...main` is empty — e.g. a local resync commit
#     and its own revert that net to no change, the robb-STUDIO 2026-08-02
#     incident shape). Default (no --auto-resolve-safe-abort): still hard
#     aborts (exit 1, HEAD untouched) but now names the exact safe command
#     instead of the old bare "resolve manually" message.
# ============================================================
W57="$BASE_WORKDIR/w57"
BARE57="$BASE_WORKDIR/w57-origin.git"
new_fixture_with_origin "$W57" "$BARE57"
push_extra_commits_to_origin "$BARE57" 2 >/dev/null
# Diverge locally with an equally content-free commit -- both sides' extra
# commits are --allow-empty, so the FINAL tree state matches even though the
# commit graphs have diverged (git merge --ff-only still refuses).
( cd "$W57" && git -c user.email=test@test -c user.name=test commit -q --allow-empty -m "local commit that nets to no change" )
HEAD_BEFORE57="$(cd "$W57" && git rev-parse --short HEAD)"
out57=$( cd "$W57" && PATH="$TEST_PATH" bash "$UPDATE_SCRIPT" 2>&1 )
rc57=$?
assert_eq "1" "$rc57" "content-identical divergence (no flag): still exits 1 by default"
HEAD_AFTER57="$(cd "$W57" && git rev-parse --short HEAD)"
assert_eq "$HEAD_BEFORE57" "$HEAD_AFTER57" "content-identical divergence (no flag): HEAD completely untouched"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out57" | grep -qi 'content-IDENTICAL'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} content-identical divergence is classified and named explicitly"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} content-identical divergence is classified and named explicitly"
    echo "  output: $out57"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out57" | grep -q -- 'reset --hard origin/main'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} abort message names the exact safe reset command"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} abort message names the exact safe reset command"
    echo "  output: $out57"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out57" | grep -q -- '--auto-resolve-safe-abort'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} abort message names --auto-resolve-safe-abort as the automatic path"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} abort message names --auto-resolve-safe-abort as the automatic path"
    echo "  output: $out57"
fi

# ============================================================
# 58. Same content-identical-divergence shape as test 57, but WITH
#     --auto-resolve-safe-abort: the script performs `git reset --hard
#     origin/main` itself, then proceeds to rebuild the now-current HEAD ->
#     exit 0, HEAD now equals origin's tip.
# ============================================================
W58="$BASE_WORKDIR/w58"
BARE58="$BASE_WORKDIR/w58-origin.git"
new_fixture_with_origin "$W58" "$BARE58"
ORIGIN_TIP58="$(push_extra_commits_to_origin "$BARE58" 2)"
( cd "$W58" && git -c user.email=test@test -c user.name=test commit -q --allow-empty -m "local commit that nets to no change" )
NEW_FAKE58="$W58/new-fake-daemon"
write_fake_daemon "$NEW_FAKE58" "$ORIGIN_TIP58" "$W58/new-marker"
out58=$( cd "$W58" && PATH="$TEST_PATH" NEW_FAKE_BIN_SRC="$NEW_FAKE58" \
    bash "$UPDATE_SCRIPT" --auto-resolve-safe-abort 2>&1 )
rc58=$?
assert_eq "0" "$rc58" "content-identical divergence + --auto-resolve-safe-abort: auto-resolves and succeeds"
HEAD_AFTER58="$(cd "$W58" && git rev-parse --short HEAD)"
assert_eq "$ORIGIN_TIP58" "$HEAD_AFTER58" "content-identical divergence + --auto-resolve-safe-abort: HEAD now equals origin/main's tip"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out58" | grep -qi 'Auto-resolved'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} auto-resolve reports what it did"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} auto-resolve reports what it did"
    echo "  output: $out58"
fi

# ============================================================
# 59. ff-abort classification (#4951): the ff-merge is blocked by a dirty
#     TRACKED file, but it is a Loom-managed installed copy (under
#     .loom/docs/, regenerated from defaults/ by resync-installed.sh) whose
#     incoming origin change conflicts with the local edit -- not real local
#     work (the loom-worker-1 2026-08-02 incident shape). Default (no
#     --auto-resolve-safe-abort): still hard aborts (exit 1, dirty file left
#     untouched) but now names the exact safe commands instead of the old
#     bare "resolve manually" message.
# ============================================================
W59="$BASE_WORKDIR/w59"
BARE59="$BASE_WORKDIR/w59-origin.git"
new_fixture_with_origin "$W59" "$BARE59"
mkdir -p "$W59/.loom/docs"
echo "docv1" > "$W59/.loom/docs/example.md"
( cd "$W59" && git add .loom/docs/example.md && git -c user.email=test@test -c user.name=test commit -q -m "add managed doc" && git push -q origin HEAD:refs/heads/main )
TMPCLONE59="$(mktemp -d)"
git clone -q "$BARE59" "$TMPCLONE59"
echo "docv2-from-origin" > "$TMPCLONE59/.loom/docs/example.md"
( cd "$TMPCLONE59" && git commit -aq -m "origin updates the managed doc" && git push -q origin HEAD:refs/heads/main )
rm -rf "$TMPCLONE59"
# Dirty (uncommitted) that SAME managed file with a conflicting edit, so
# `git merge --ff-only` genuinely refuses (an unrelated dirty file would not
# block a fast-forward).
echo "docv1-local-edit" > "$W59/.loom/docs/example.md"
HEAD_BEFORE59="$(cd "$W59" && git rev-parse --short HEAD)"
out59=$( cd "$W59" && PATH="$TEST_PATH" bash "$UPDATE_SCRIPT" 2>&1 )
rc59=$?
assert_eq "1" "$rc59" "managed-only dirty file (no flag): still exits 1 by default"
HEAD_AFTER59="$(cd "$W59" && git rev-parse --short HEAD)"
assert_eq "$HEAD_BEFORE59" "$HEAD_AFTER59" "managed-only dirty file (no flag): HEAD untouched"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$(cat "$W59/.loom/docs/example.md")" == "docv1-local-edit" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} dirty managed file is left untouched by default (no checkout performed)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dirty managed file is left untouched by default (no checkout performed)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out59" | grep -qi 'Loom-managed installed copies'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} managed-only dirty state is classified and named explicitly"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} managed-only dirty state is classified and named explicitly"
    echo "  output: $out59"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out59" | grep -q -- 'checkout -- .loom/docs/example.md' \
    && echo "$out59" | grep -q -- 'resync-installed.sh'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} abort message names the exact safe checkout + resync commands"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} abort message names the exact safe checkout + resync commands"
    echo "  output: $out59"
fi

# ============================================================
# 60. Same managed-only-dirty shape as test 59, but WITH
#     --auto-resolve-safe-abort: the script discards the local edit
#     (`git checkout --`), fast-forwards, and invokes resync-installed.sh
#     post-roll -> exit 0, the managed file now matches origin's content, and
#     the (stubbed) resync-installed.sh was actually invoked.
# ============================================================
W60="$BASE_WORKDIR/w60"
BARE60="$BASE_WORKDIR/w60-origin.git"
new_fixture_with_origin "$W60" "$BARE60"
mkdir -p "$W60/.loom/docs"
echo "docv1" > "$W60/.loom/docs/example.md"
( cd "$W60" && git add .loom/docs/example.md && git -c user.email=test@test -c user.name=test commit -q -m "add managed doc" && git push -q origin HEAD:refs/heads/main )
TMPCLONE60="$(mktemp -d)"
git clone -q "$BARE60" "$TMPCLONE60"
echo "docv2-from-origin" > "$TMPCLONE60/.loom/docs/example.md"
( cd "$TMPCLONE60" && git commit -aq -m "origin updates the managed doc" && git push -q origin HEAD:refs/heads/main )
ORIGIN_TIP60="$(cd "$TMPCLONE60" && git rev-parse --short HEAD)"
rm -rf "$TMPCLONE60"
echo "docv1-local-edit" > "$W60/.loom/docs/example.md"
# Stub resync-installed.sh at the installed-copy resolution path so we can
# prove it was actually invoked post-roll, without exercising the real
# script's defaults/-tree assumptions inside this minimal fixture.
mkdir -p "$W60/.loom/scripts"
RESYNC_MARKER60="$W60/resync-invoked-marker"
cat > "$W60/.loom/scripts/resync-installed.sh" <<RESYNCEOF
#!/usr/bin/env bash
echo invoked > "$RESYNC_MARKER60"
RESYNCEOF
chmod +x "$W60/.loom/scripts/resync-installed.sh"
NEW_FAKE60="$W60/new-fake-daemon"
write_fake_daemon "$NEW_FAKE60" "$ORIGIN_TIP60" "$W60/new-marker"
out60=$( cd "$W60" && PATH="$TEST_PATH" NEW_FAKE_BIN_SRC="$NEW_FAKE60" \
    bash "$UPDATE_SCRIPT" --auto-resolve-safe-abort 2>&1 )
rc60=$?
assert_eq "0" "$rc60" "managed-only dirty file + --auto-resolve-safe-abort: auto-resolves and succeeds"
HEAD_AFTER60="$(cd "$W60" && git rev-parse --short HEAD)"
assert_eq "$ORIGIN_TIP60" "$HEAD_AFTER60" "managed-only dirty file + --auto-resolve-safe-abort: HEAD now equals origin/main's tip"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$(cat "$W60/.loom/docs/example.md" 2>/dev/null)" == "docv2-from-origin" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} local edit was discarded; managed file now matches origin's post-merge content"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} local edit was discarded; managed file now matches origin's post-merge content"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$RESYNC_MARKER60" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} post-roll resync-installed.sh was actually invoked"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} post-roll resync-installed.sh was actually invoked"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out60" | grep -qi 'Auto-resolved'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} auto-resolve reports what it did"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} auto-resolve reports what it did"
    echo "  output: $out60"
fi

# ============================================================
# 61. Regression (#4951 safety floor): a dirty tracked file OUTSIDE the
#     Loom-managed set (a top-level repo file, conflicting with origin's
#     change) alongside ANOTHER dirty file that IS managed -- the "every
#     blocking file" wording is conjunctive, not "any": one unmanaged dirty
#     file must still force the generic hard abort, with NO auto-resolve
#     attempted, even when --auto-resolve-safe-abort is passed.
# ============================================================
W61="$BASE_WORKDIR/w61"
BARE61="$BASE_WORKDIR/w61-origin.git"
new_fixture_with_origin "$W61" "$BARE61"
mkdir -p "$W61/.loom/docs"
echo "docv1" > "$W61/.loom/docs/example.md"
echo "unmanagedv1" > "$W61/unmanaged-file.txt"
( cd "$W61" && git add .loom/docs/example.md unmanaged-file.txt && git -c user.email=test@test -c user.name=test commit -q -m "add managed doc + unmanaged file" && git push -q origin HEAD:refs/heads/main )
TMPCLONE61="$(mktemp -d)"
git clone -q "$BARE61" "$TMPCLONE61"
echo "unmanagedv2-from-origin" > "$TMPCLONE61/unmanaged-file.txt"
( cd "$TMPCLONE61" && git commit -aq -m "origin updates the unmanaged file" && git push -q origin HEAD:refs/heads/main )
rm -rf "$TMPCLONE61"
# Dirty the UNMANAGED tracked file with a conflicting edit (blocks the
# ff-merge) alongside a dirty MANAGED file (does not itself conflict, but
# must not let the unmanaged one slip through).
echo "unmanagedv1-local-edit" > "$W61/unmanaged-file.txt"
echo "docv1-local-edit" > "$W61/.loom/docs/example.md"
HEAD_BEFORE61="$(cd "$W61" && git rev-parse --short HEAD)"
out61=$( cd "$W61" && PATH="$TEST_PATH" bash "$UPDATE_SCRIPT" --auto-resolve-safe-abort 2>&1 )
rc61=$?
assert_eq "1" "$rc61" "unmanaged dirty file alongside a managed one, EVEN with --auto-resolve-safe-abort: still hard-aborts"
HEAD_AFTER61="$(cd "$W61" && git rev-parse --short HEAD)"
assert_eq "$HEAD_BEFORE61" "$HEAD_AFTER61" "unmanaged dirty file case: HEAD untouched"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$(cat "$W61/unmanaged-file.txt")" == "unmanagedv1-local-edit" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} unmanaged dirty file is left untouched -- no auto-resolve attempted"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} unmanaged dirty file is left untouched -- no auto-resolve attempted"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out61" | grep -qi 'Refusing to guess or hard-reset'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} mixed managed+unmanaged dirty: falls through to the generic hard-abort message"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} mixed managed+unmanaged dirty: falls through to the generic hard-abort message"
    echo "  output: $out61"
fi

# ============================================================
# 62. Regression (#4951 safety floor, Judge finding on PR #4965): the
#     content-identical classifier compares only COMMITTED refs, so a host can
#     simultaneously have (a) diverged local commits that net to zero content
#     diff vs. origin (the test-57 shape) AND (b) an entirely unrelated dirty
#     tracked file OUTSIDE the Loom-managed set. `git merge --ff-only` fails on
#     the history divergence alone, so this branch is reached — and before the
#     fix, _ff_abort_content_identical returned true regardless of the working
#     tree, letting --auto-resolve-safe-abort `git reset --hard` silently
#     destroy that file's uncommitted changes. It must hard-abort instead, with
#     the dirty file byte-for-byte intact.
# ============================================================
W62="$BASE_WORKDIR/w62"
BARE62="$BASE_WORKDIR/w62-origin.git"
new_fixture_with_origin "$W62" "$BARE62"
echo "scratchv1" > "$W62/unmanaged-scratch.txt"
( cd "$W62" && git add unmanaged-scratch.txt && git -c user.email=test@test -c user.name=test commit -q -m "add unmanaged scratch file" && git push -q origin HEAD:refs/heads/main )
# Origin advances with content-free commits, so the two sides' TREES stay
# identical -- exactly the content-identical divergence of tests 57/58.
push_extra_commits_to_origin "$BARE62" 2 >/dev/null
( cd "$W62" && git -c user.email=test@test -c user.name=test commit -q --allow-empty -m "local commit that nets to no change" )
# ... but now an unrelated, UNMANAGED tracked file is also dirty in the
# working tree (an operator's manual scratch edit).
echo "scratchv1-local-edit" > "$W62/unmanaged-scratch.txt"
HEAD_BEFORE62="$(cd "$W62" && git rev-parse --short HEAD)"
out62=$( cd "$W62" && PATH="$TEST_PATH" bash "$UPDATE_SCRIPT" --auto-resolve-safe-abort 2>&1 )
rc62=$?
assert_eq "1" "$rc62" "content-identical divergence + dirty UNMANAGED file, EVEN with --auto-resolve-safe-abort: still hard-aborts"
HEAD_AFTER62="$(cd "$W62" && git rev-parse --short HEAD)"
assert_eq "$HEAD_BEFORE62" "$HEAD_AFTER62" "content-identical divergence + dirty unmanaged file: HEAD untouched (no reset --hard)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$(cat "$W62/unmanaged-scratch.txt")" == "scratchv1-local-edit" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} uncommitted edit to an unmanaged file survives a content-identical divergence"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} uncommitted edit to an unmanaged file survives a content-identical divergence"
    echo "  file now: $(cat "$W62/unmanaged-scratch.txt" 2>/dev/null)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out62" | grep -qi 'Refusing to guess or hard-reset'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} content-identical + dirty unmanaged file falls through to the generic hard-abort message"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} content-identical + dirty unmanaged file falls through to the generic hard-abort message"
    echo "  output: $out62"
fi

# ============================================================
# 63. Same combined shape as test 62, but the co-existing dirty tracked file IS
#     Loom-managed. The content-identical classifier must STILL decline (its
#     `git reset --hard` is only vouched-for on a clean tree), handing the case
#     to the managed-dirty classifier instead. That one discards the managed
#     edit it is authorized to discard and retries the fast-forward, which
#     still cannot apply over the history divergence -- so the run hard-aborts
#     (exit 1) with local commits intact, rather than silently hard-resetting
#     from the content-identical branch. Deliberately NOT resolved end-to-end:
#     widening either classifier to cover the combination is exactly the kind
#     of "when genuinely unsure" widening the #4381 safety note forbids.
# ============================================================
W63="$BASE_WORKDIR/w63"
BARE63="$BASE_WORKDIR/w63-origin.git"
new_fixture_with_origin "$W63" "$BARE63"
mkdir -p "$W63/.loom/docs"
echo "docv1" > "$W63/.loom/docs/example.md"
( cd "$W63" && git add .loom/docs/example.md && git -c user.email=test@test -c user.name=test commit -q -m "add managed doc" && git push -q origin HEAD:refs/heads/main )
push_extra_commits_to_origin "$BARE63" 2 >/dev/null
( cd "$W63" && git -c user.email=test@test -c user.name=test commit -q --allow-empty -m "local commit that nets to no change" )
echo "docv1-local-edit" > "$W63/.loom/docs/example.md"
HEAD_BEFORE63="$(cd "$W63" && git rev-parse --short HEAD)"
out63=$( cd "$W63" && PATH="$TEST_PATH" bash "$UPDATE_SCRIPT" --auto-resolve-safe-abort 2>&1 )
rc63=$?
assert_eq "1" "$rc63" "content-identical divergence + dirty MANAGED file, EVEN with --auto-resolve-safe-abort: still hard-aborts"
HEAD_AFTER63="$(cd "$W63" && git rev-parse --short HEAD)"
assert_eq "$HEAD_BEFORE63" "$HEAD_AFTER63" "content-identical divergence + dirty managed file: HEAD untouched (no reset --hard)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out63" | grep -qi 'reset local main to origin/main'; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} content-identical branch never claims a reset --hard while the tree is dirty"
    echo "  output: $out63"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} content-identical branch never claims a reset --hard while the tree is dirty"
fi

# ============================================================
# Artifact-fetch mode (Epic #4990 Phase 3, #5020): resolve the latest GitHub
# Release, download + verify its artifact for this host's platform, and
# provision it INSTEAD of a local `cargo build --release`. Every test below
# pins LOOM_DAEMON_UPDATE_GH_REPO (bypassing git-remote parsing) and
# LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" (a Linux target, so no
# codesign/cosign tooling is needed to exercise the core resolve/download/
# verify/provision flow — signature verification for that target is a
# soft-skip whenever no `.sig` asset is published, exactly test A's fixture).
# ============================================================

# ------------------------------------------------------------
# A. Successful artifact update: a newer release with a matching-platform
#    artifact is fetched, checksum-verified, and provisioned — WITHOUT ever
#    invoking `cargo build` (no fake cargo is placed on PATH for this test at
#    all, so a fallback to the source-build path would fail loudly rather
#    than silently succeed).
# ------------------------------------------------------------
WA="$BASE_WORKDIR/w-fetch-a"
new_fixture "$WA"
write_fake_daemon "$WA/installed-loom-daemon" "oldc0mm" "$WA/marker"

WA_ASSETS="$WA/gh-assets"
mkdir -p "$WA_ASSETS"
WA_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WA_ASSETS/$WA_BIN_NAME" "0.16.0" "artifac1"
sha256_of "$WA_ASSETS/$WA_BIN_NAME" > "$WA_ASSETS/$WA_BIN_NAME.sha256"

WA_FAKEBIN="$WA/fakebin"
mkdir -p "$WA_FAKEBIN"
write_fake_gh "$WA_FAKEBIN/gh" "v0.16.0" "$WA_ASSETS"

outA=$( cd "$WA" && PATH="$WA_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WA/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcA=$(echo "$outA" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcA" "artifact-fetch: successful update exits 0"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outA" | grep -q 'Rebuilding loom-daemon (cargo build'; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: successful update never invokes 'cargo build' (AC1)"
    echo "  output: $outA"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: successful update never invokes 'cargo build' (AC1)"
fi

installedA_version="$("$WA/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$installedA_version" | grep -q 'commit artifac1'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: the fetched+verified artifact was provisioned to the destination"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: the fetched+verified artifact was provisioned to the destination"
    echo "  --version: $installedA_version"
fi

# ------------------------------------------------------------
# B. Checksum-mismatch abort: a tampered/corrupted checksum aborts the WHOLE
#    update (exit 1) and leaves the running (destination) daemon untouched —
#    never a soft fallback to a source build (AC2).
# ------------------------------------------------------------
WB="$BASE_WORKDIR/w-fetch-b"
new_fixture "$WB"
write_fake_daemon "$WB/installed-loom-daemon" "oldc0mm" "$WB/marker"
installedB_before="$("$WB/installed-loom-daemon" --version 2>/dev/null)"

WB_ASSETS="$WB/gh-assets"
mkdir -p "$WB_ASSETS"
WB_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WB_ASSETS/$WB_BIN_NAME" "0.16.0" "artifac2"
# Deliberately WRONG checksum (does not match the binary written above).
echo "0000000000000000000000000000000000000000000000000000000000000000  ${WB_BIN_NAME}" > "$WB_ASSETS/$WB_BIN_NAME.sha256"

WB_FAKEBIN="$WB/fakebin"
mkdir -p "$WB_FAKEBIN"
write_fake_gh "$WB_FAKEBIN/gh" "v0.16.0" "$WB_ASSETS"

outB=$( cd "$WB" && PATH="$WB_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WB/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcB=$(echo "$outB" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "1" "$rcB" "artifact-fetch: checksum mismatch aborts the update (exit 1)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outB" | grep -q 'Checksum verification FAILED'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: checksum-mismatch failure is reported explicitly"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: checksum-mismatch failure is reported explicitly"
    echo "  output: $outB"
fi

installedB_after="$("$WB/installed-loom-daemon" --version 2>/dev/null)"
assert_eq "$installedB_before" "$installedB_after" "artifact-fetch: checksum mismatch leaves the destination binary untouched"

# ------------------------------------------------------------
# C. Missing-artifact fallback: the resolved release has no artifact
#    published for this host's platform (a real, but incomplete, release) —
#    softly falls back to the existing local source-build path and still
#    completes the update end-to-end (AC4).
# ------------------------------------------------------------
WC="$BASE_WORKDIR/w-fetch-c"
new_fixture "$WC"
HEADC="$(cd "$WC" && git rev-parse --short HEAD)"
write_fake_daemon "$WC/installed-loom-daemon" "deadbee" "$WC/marker"
NEW_FAKE_C="$WC/new-fake-daemon"
write_fake_daemon "$NEW_FAKE_C" "$HEADC" "$WC/new-marker"

# A release exists (newer than installed) but publishes NO assets at all for
# this target — fetch_resolve_latest must reject it (no matching bin/sha256)
# and the caller must fall back, not hard-fail.
WC_ASSETS="$WC/gh-assets"
mkdir -p "$WC_ASSETS"
echo "unrelated-file" > "$WC_ASSETS/README.txt"

WC_FAKEBIN="$WC/fakebin"
mkdir -p "$WC_FAKEBIN"
write_fake_gh "$WC_FAKEBIN/gh" "v0.20.0" "$WC_ASSETS"
write_fake_cargo "$WC_FAKEBIN/cargo"

outC=$( cd "$WC" && PATH="$WC_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WC/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    NEW_FAKE_BIN_SRC="$NEW_FAKE_C" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcC=$(echo "$outC" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcC" "artifact-fetch: missing-artifact fallback still completes the update (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outC" | grep -q 'Artifact-fetch:.*falling back to the local source-build path'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: a resolution failure is reported as a soft fallback"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: a resolution failure is reported as a soft fallback"
    echo "  output: $outC"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outC" | grep -q 'Rebuilding loom-daemon (cargo build'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: missing-artifact fallback actually rebuilds from source"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: missing-artifact fallback actually rebuilds from source"
    echo "  output: $outC"
fi

installedC_version="$("$WC/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$installedC_version" | grep -q "commit ${HEADC}"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: the source-build fallback provisioned the freshly-built binary"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: the source-build fallback provisioned the freshly-built binary"
    echo "  --version: $installedC_version"
fi

# ------------------------------------------------------------
# D. --no-fetch disables artifact-fetch mode entirely, even when a matching
#    release artifact IS available — restores the pre-#5020 always-build
#    behavior.
# ------------------------------------------------------------
WD="$BASE_WORKDIR/w-fetch-d"
new_fixture "$WD"
HEADD="$(cd "$WD" && git rev-parse --short HEAD)"
write_fake_daemon "$WD/installed-loom-daemon" "deadbee" "$WD/marker"
NEW_FAKE_D="$WD/new-fake-daemon"
write_fake_daemon "$NEW_FAKE_D" "$HEADD" "$WD/new-marker"

WD_ASSETS="$WD/gh-assets"
mkdir -p "$WD_ASSETS"
WD_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WD_ASSETS/$WD_BIN_NAME" "0.16.0" "artifac4"
sha256_of "$WD_ASSETS/$WD_BIN_NAME" > "$WD_ASSETS/$WD_BIN_NAME.sha256"

WD_FAKEBIN="$WD/fakebin"
mkdir -p "$WD_FAKEBIN"
write_fake_gh "$WD_FAKEBIN/gh" "v0.16.0" "$WD_ASSETS"
write_fake_cargo "$WD_FAKEBIN/cargo"

outD=$( cd "$WD" && PATH="$WD_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WD/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    NEW_FAKE_BIN_SRC="$NEW_FAKE_D" \
    bash "$UPDATE_SCRIPT" --no-restart --no-fetch 2>&1; echo "EXIT=$?" )
rcD=$(echo "$outD" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcD" "--no-fetch: update still completes (exit 0)"

installedD_version="$("$WD/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$installedD_version" | grep -q "commit ${HEADD}"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --no-fetch: rebuilds from source even though a release artifact was available"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --no-fetch: rebuilds from source even though a release artifact was available"
    echo "  --version: $installedD_version"
fi

# ------------------------------------------------------------
# E. --fetch (forced) hard-fails rather than silently falling back to a
#    source build when no matching artifact resolves.
# ------------------------------------------------------------
WE="$BASE_WORKDIR/w-fetch-e"
new_fixture "$WE"
write_fake_daemon "$WE/installed-loom-daemon" "deadbee" "$WE/marker"

WE_ASSETS="$WE/gh-assets"
mkdir -p "$WE_ASSETS"
echo "unrelated-file" > "$WE_ASSETS/README.txt"

WE_FAKEBIN="$WE/fakebin"
mkdir -p "$WE_FAKEBIN"
write_fake_gh "$WE_FAKEBIN/gh" "v0.20.0" "$WE_ASSETS"
write_fake_cargo "$WE_FAKEBIN/cargo"

outE=$( cd "$WE" && PATH="$WE_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WE/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart --fetch 2>&1; echo "EXIT=$?" )
rcE=$(echo "$outE" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "1" "$rcE" "--fetch: refuses to silently fall back to a source build (exit 1)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outE" | grep -q 'Rebuilding loom-daemon (cargo build'; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --fetch: never falls back to 'cargo build' on a forced-fetch failure"
    echo "  output: $outE"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --fetch: never falls back to 'cargo build' on a forced-fetch failure"
fi

# ------------------------------------------------------------
# F. GitHub API unreachable/rate-limited during release resolution: every
#    `gh release view` fails. This must be a SOFT fallback to the local
#    source build (AC4) — never a hard failure of the whole update.
# ------------------------------------------------------------
WF="$BASE_WORKDIR/w-fetch-f"
new_fixture "$WF"
HEADF="$(cd "$WF" && git rev-parse --short HEAD)"
write_fake_daemon "$WF/installed-loom-daemon" "deadbee" "$WF/marker"
NEW_FAKE_F="$WF/new-fake-daemon"
write_fake_daemon "$NEW_FAKE_F" "$HEADF" "$WF/new-marker"

WF_FAKEBIN="$WF/fakebin"
mkdir -p "$WF_FAKEBIN"
write_fake_gh_unreachable "$WF_FAKEBIN/gh"
write_fake_cargo "$WF_FAKEBIN/cargo"

outF=$( cd "$WF" && PATH="$WF_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WF/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    NEW_FAKE_BIN_SRC="$NEW_FAKE_F" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcF=$(echo "$outF" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcF" "artifact-fetch: an unreachable GitHub API falls back to the source build (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outF" | grep -q 'Artifact-fetch:.*falling back to the local source-build path'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: an unreachable GitHub API is reported as a soft fallback"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: an unreachable GitHub API is reported as a soft fallback"
    echo "  output: $outF"
fi

installedF_version="$("$WF/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$installedF_version" | grep -q "commit ${HEADF}"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: the API-failure fallback still provisioned a freshly-built binary"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: the API-failure fallback still provisioned a freshly-built binary"
    echo "  --version: $installedF_version"
fi

# ------------------------------------------------------------
# G. Already at the latest release: the resolved release's version equals the
#    installed daemon's AND the local source commit matches too — the
#    existing up-to-date no-op contract must hold (exit 0, no download, no
#    build, destination untouched).
# ------------------------------------------------------------
WG="$BASE_WORKDIR/w-fetch-g"
new_fixture "$WG"
HEADG="$(cd "$WG" && git rev-parse --short HEAD)"
write_fake_daemon "$WG/installed-loom-daemon" "$HEADG" "$WG/marker"

WG_ASSETS="$WG/gh-assets"
mkdir -p "$WG_ASSETS"
WG_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WG_ASSETS/$WG_BIN_NAME" "0.15.0" "shouldnt"
sha256_of "$WG_ASSETS/$WG_BIN_NAME" > "$WG_ASSETS/$WG_BIN_NAME.sha256"

WG_FAKEBIN="$WG/fakebin"
mkdir -p "$WG_FAKEBIN"
# write_fake_daemon reports version 0.15.0, so tag v0.15.0 is NOT newer.
write_fake_gh "$WG_FAKEBIN/gh" "v0.15.0" "$WG_ASSETS"

outG=$( cd "$WG" && PATH="$WG_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WG/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcG=$(echo "$outG" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcG" "artifact-fetch: already at the latest release is a no-op (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outG" | grep -q 'is not newer than the installed version'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: an equal-version release is reported as nothing to fetch"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: an equal-version release is reported as nothing to fetch"
    echo "  output: $outG"
fi

installedG_version="$("$WG/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$installedG_version" | grep -q "commit ${HEADG}" && ! echo "$outG" | grep -q 'Downloading '; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: the up-to-date no-op downloads nothing and leaves the binary untouched"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: the up-to-date no-op downloads nothing and leaves the binary untouched"
    echo "  output: $outG"
fi

# ------------------------------------------------------------
# H. --check regression: reports the available RELEASE artifact (exit 3) and
#    writes nothing.
# ------------------------------------------------------------
WH="$BASE_WORKDIR/w-fetch-h"
new_fixture "$WH"
write_fake_daemon "$WH/installed-loom-daemon" "oldc0mm" "$WH/marker"
installedH_before="$("$WH/installed-loom-daemon" --version 2>/dev/null)"

WH_ASSETS="$WH/gh-assets"
mkdir -p "$WH_ASSETS"
WH_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WH_ASSETS/$WH_BIN_NAME" "0.16.0" "artifac8"
sha256_of "$WH_ASSETS/$WH_BIN_NAME" > "$WH_ASSETS/$WH_BIN_NAME.sha256"

WH_FAKEBIN="$WH/fakebin"
mkdir -p "$WH_FAKEBIN"
write_fake_gh "$WH_FAKEBIN/gh" "v0.16.0" "$WH_ASSETS"

outH=$( cd "$WH" && PATH="$WH_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WH/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --check 2>&1; echo "EXIT=$?" )
rcH=$(echo "$outH" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "3" "$rcH" "--check: an available release artifact still reports 'update available' (exit 3)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outH" | grep -q 'Update available via release artifact v0.16.0'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --check: names the release artifact it would fetch"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --check: names the release artifact it would fetch"
    echo "  output: $outH"
fi

installedH_after="$("$WH/installed-loom-daemon" --version 2>/dev/null)"
assert_eq "$installedH_before" "$installedH_after" "--check: writes nothing even when an artifact is available"

# ------------------------------------------------------------
# I. --dry-run regression: describes the fetch it WOULD perform, never runs
#    cargo, and leaves the destination untouched.
# ------------------------------------------------------------
WI="$BASE_WORKDIR/w-fetch-i"
new_fixture "$WI"
write_fake_daemon "$WI/installed-loom-daemon" "oldc0mm" "$WI/marker"
installedI_before="$("$WI/installed-loom-daemon" --version 2>/dev/null)"

WI_ASSETS="$WI/gh-assets"
mkdir -p "$WI_ASSETS"
WI_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WI_ASSETS/$WI_BIN_NAME" "0.16.0" "artifac9"
sha256_of "$WI_ASSETS/$WI_BIN_NAME" > "$WI_ASSETS/$WI_BIN_NAME.sha256"

WI_FAKEBIN="$WI/fakebin"
mkdir -p "$WI_FAKEBIN"
write_fake_gh "$WI_FAKEBIN/gh" "v0.16.0" "$WI_ASSETS"

outI=$( cd "$WI" && PATH="$WI_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WI/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --dry-run 2>&1; echo "EXIT=$?" )
rcI=$(echo "$outI" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcI" "--dry-run: exits 0 with an artifact available"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outI" | grep -q '\[dry-run\] Would fetch + verify release artifact v0.16.0' \
    && ! echo "$outI" | grep -q '\[dry-run\] Would run: (cd .* cargo build'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --dry-run: describes the artifact fetch instead of a cargo build"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --dry-run: describes the artifact fetch instead of a cargo build"
    echo "  output: $outI"
fi

installedI_after="$("$WI/installed-loom-daemon" --version 2>/dev/null)"
assert_eq "$installedI_before" "$installedI_after" "--dry-run: leaves the destination binary untouched in artifact mode"

# ------------------------------------------------------------
# J. Linux detached signature PRESENT, cosign + a public key both resolvable,
#    verification SUCCEEDS -> the update proceeds and says so (AC3).
# ------------------------------------------------------------
WJ="$BASE_WORKDIR/w-fetch-j"
new_fixture "$WJ"
write_fake_daemon "$WJ/installed-loom-daemon" "oldc0mm" "$WJ/marker"

WJ_ASSETS="$WJ/gh-assets"
mkdir -p "$WJ_ASSETS"
WJ_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WJ_ASSETS/$WJ_BIN_NAME" "0.16.0" "sigokc0"
sha256_of "$WJ_ASSETS/$WJ_BIN_NAME" > "$WJ_ASSETS/$WJ_BIN_NAME.sha256"
echo "fake-detached-signature" > "$WJ_ASSETS/$WJ_BIN_NAME.sig"
echo "-----BEGIN PUBLIC KEY-----fake-----END PUBLIC KEY-----" > "$WJ/cosign.pub"

WJ_FAKEBIN="$WJ/fakebin"
mkdir -p "$WJ_FAKEBIN"
write_fake_gh "$WJ_FAKEBIN/gh" "v0.16.0" "$WJ_ASSETS"
write_fake_cosign "$WJ_FAKEBIN/cosign" 0

outJ=$( cd "$WJ" && PATH="$WJ_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WJ/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    LOOM_DAEMON_UPDATE_COSIGN_PUBKEY="$WJ/cosign.pub" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcJ=$(echo "$outJ" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcJ" "signature present + cosign verifies: update proceeds (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outJ" | grep -q 'cosign signature verification passed'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} signature present: cosign verification actually ran and passed"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} signature present: cosign verification actually ran and passed"
    echo "  output: $outJ"
fi

installedJ_version="$("$WJ/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$installedJ_version" | grep -q 'commit sigokc0'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} signature present + verified: the artifact was provisioned"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} signature present + verified: the artifact was provisioned"
    echo "  --version: $installedJ_version"
fi

# ------------------------------------------------------------
# K. Linux detached signature PRESENT but cosign verification FAILS -> abort
#    (exit 1), destination untouched. A present-but-invalid signature is
#    tamper evidence, never a soft skip.
# ------------------------------------------------------------
WK="$BASE_WORKDIR/w-fetch-k"
new_fixture "$WK"
write_fake_daemon "$WK/installed-loom-daemon" "oldc0mm" "$WK/marker"
installedK_before="$("$WK/installed-loom-daemon" --version 2>/dev/null)"

WK_ASSETS="$WK/gh-assets"
mkdir -p "$WK_ASSETS"
WK_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WK_ASSETS/$WK_BIN_NAME" "0.16.0" "sigbadc"
sha256_of "$WK_ASSETS/$WK_BIN_NAME" > "$WK_ASSETS/$WK_BIN_NAME.sha256"
echo "tampered-detached-signature" > "$WK_ASSETS/$WK_BIN_NAME.sig"
echo "-----BEGIN PUBLIC KEY-----fake-----END PUBLIC KEY-----" > "$WK/cosign.pub"

WK_FAKEBIN="$WK/fakebin"
mkdir -p "$WK_FAKEBIN"
write_fake_gh "$WK_FAKEBIN/gh" "v0.16.0" "$WK_ASSETS"
write_fake_cosign "$WK_FAKEBIN/cosign" 1
write_fake_cargo "$WK_FAKEBIN/cargo"

outK=$( cd "$WK" && PATH="$WK_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WK/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    LOOM_DAEMON_UPDATE_COSIGN_PUBKEY="$WK/cosign.pub" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcK=$(echo "$outK" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "1" "$rcK" "signature present but INVALID: aborts the update (exit 1)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outK" | grep -q 'cosign signature verification FAILED' \
    && ! echo "$outK" | grep -q 'Rebuilding loom-daemon (cargo build'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} signature present but INVALID: never degrades to a source-build fallback"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} signature present but INVALID: never degrades to a source-build fallback"
    echo "  output: $outK"
fi

installedK_after="$("$WK/installed-loom-daemon" --version 2>/dev/null)"
assert_eq "$installedK_before" "$installedK_after" "signature-verification failure leaves the destination binary untouched"

# ------------------------------------------------------------
# L. Linux detached signature PRESENT but no cosign public key is resolvable
#    -> LOUD SKIP, update still proceeds (AC3: an unverifiable-but-optional
#    signature never blocks; the checksum already passed).
# ------------------------------------------------------------
WL="$BASE_WORKDIR/w-fetch-l"
new_fixture "$WL"
write_fake_daemon "$WL/installed-loom-daemon" "oldc0mm" "$WL/marker"

WL_ASSETS="$WL/gh-assets"
mkdir -p "$WL_ASSETS"
WL_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WL_ASSETS/$WL_BIN_NAME" "0.16.0" "nokeyc0"
sha256_of "$WL_ASSETS/$WL_BIN_NAME" > "$WL_ASSETS/$WL_BIN_NAME.sha256"
echo "fake-detached-signature" > "$WL_ASSETS/$WL_BIN_NAME.sig"

WL_FAKEBIN="$WL/fakebin"
mkdir -p "$WL_FAKEBIN"
write_fake_gh "$WL_FAKEBIN/gh" "v0.16.0" "$WL_ASSETS"
write_fake_cosign "$WL_FAKEBIN/cosign" 0

outL=$( cd "$WL" && PATH="$WL_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WL/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcL=$(echo "$outL" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcL" "signature present, no public key: loud skip, update still proceeds (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outL" | grep -q 'no cosign public key is resolvable.*SKIPPING verification'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} signature present, no public key: the skip is LOUD, not silent"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} signature present, no public key: the skip is LOUD, not silent"
    echo "  output: $outL"
fi

installedL_version="$("$WL/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$installedL_version" | grep -q 'commit nokeyc0'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} signature present, no public key: the artifact was still provisioned"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} signature present, no public key: the artifact was still provisioned"
    echo "  --version: $installedL_version"
fi

# ------------------------------------------------------------
# M. macOS target, artifact UNSIGNED (no Developer ID secrets were configured
#    for that release) -> soft-skip, update proceeds. This is the case that
#    must NOT be confused with tamper evidence.
# ------------------------------------------------------------
WM="$BASE_WORKDIR/w-fetch-m"
new_fixture "$WM"
write_fake_daemon "$WM/installed-loom-daemon" "oldc0mm" "$WM/marker"

WM_ASSETS="$WM/gh-assets"
mkdir -p "$WM_ASSETS"
WM_BIN_NAME="loom-daemon-aarch64-apple-darwin"
write_fake_artifact_daemon "$WM_ASSETS/$WM_BIN_NAME" "0.16.0" "unsignd"
sha256_of "$WM_ASSETS/$WM_BIN_NAME" > "$WM_ASSETS/$WM_BIN_NAME.sha256"

WM_FAKEBIN="$WM/fakebin"
mkdir -p "$WM_FAKEBIN"
write_fake_gh "$WM_FAKEBIN/gh" "v0.16.0" "$WM_ASSETS"
write_fake_codesign "$WM_FAKEBIN/codesign" unsigned

outM=$( cd "$WM" && PATH="$WM_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WM/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="aarch64-apple-darwin" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcM=$(echo "$outM" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcM" "macOS artifact unsigned: soft-skip, update proceeds (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outM" | grep -q 'Downloaded artifact is unsigned'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} macOS artifact unsigned: reported as 'unsigned', not as a verification failure"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} macOS artifact unsigned: reported as 'unsigned', not as a verification failure"
    echo "  output: $outM"
fi

installedM_version="$("$WM/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$installedM_version" | grep -q 'commit unsignd'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} macOS artifact unsigned: still provisioned (absence never blocks)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} macOS artifact unsigned: still provisioned (absence never blocks)"
    echo "  --version: $installedM_version"
fi

# ------------------------------------------------------------
# N. macOS target, artifact SIGNED and codesign verification succeeds ->
#    proceeds, reporting the verification.
# ------------------------------------------------------------
WN="$BASE_WORKDIR/w-fetch-n"
new_fixture "$WN"
write_fake_daemon "$WN/installed-loom-daemon" "oldc0mm" "$WN/marker"

WN_ASSETS="$WN/gh-assets"
mkdir -p "$WN_ASSETS"
WN_BIN_NAME="loom-daemon-aarch64-apple-darwin"
write_fake_artifact_daemon "$WN_ASSETS/$WN_BIN_NAME" "0.16.0" "csignok"
sha256_of "$WN_ASSETS/$WN_BIN_NAME" > "$WN_ASSETS/$WN_BIN_NAME.sha256"

WN_FAKEBIN="$WN/fakebin"
mkdir -p "$WN_FAKEBIN"
write_fake_gh "$WN_FAKEBIN/gh" "v0.16.0" "$WN_ASSETS"
write_fake_codesign "$WN_FAKEBIN/codesign" signed-ok

outN=$( cd "$WN" && PATH="$WN_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WN/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="aarch64-apple-darwin" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcN=$(echo "$outN" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcN" "macOS artifact signed + verified: update proceeds (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outN" | grep -q 'macOS codesign verification passed'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} macOS artifact signed: codesign verification actually ran and passed"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} macOS artifact signed: codesign verification actually ran and passed"
    echo "  output: $outN"
fi

# ------------------------------------------------------------
# O. macOS target, artifact SIGNED but codesign verification FAILS -> abort
#    (exit 1), destination untouched. Distinct from the "unsigned" case in M.
# ------------------------------------------------------------
WO="$BASE_WORKDIR/w-fetch-o"
new_fixture "$WO"
write_fake_daemon "$WO/installed-loom-daemon" "oldc0mm" "$WO/marker"
installedO_before="$("$WO/installed-loom-daemon" --version 2>/dev/null)"

WO_ASSETS="$WO/gh-assets"
mkdir -p "$WO_ASSETS"
WO_BIN_NAME="loom-daemon-aarch64-apple-darwin"
write_fake_artifact_daemon "$WO_ASSETS/$WO_BIN_NAME" "0.16.0" "csignbad"
sha256_of "$WO_ASSETS/$WO_BIN_NAME" > "$WO_ASSETS/$WO_BIN_NAME.sha256"

WO_FAKEBIN="$WO/fakebin"
mkdir -p "$WO_FAKEBIN"
write_fake_gh "$WO_FAKEBIN/gh" "v0.16.0" "$WO_ASSETS"
write_fake_codesign "$WO_FAKEBIN/codesign" signed-bad
write_fake_cargo "$WO_FAKEBIN/cargo"

outO=$( cd "$WO" && PATH="$WO_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WO/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="aarch64-apple-darwin" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcO=$(echo "$outO" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "1" "$rcO" "macOS artifact signed but INVALID: aborts the update (exit 1)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outO" | grep -q 'codesign verification FAILED' \
    && ! echo "$outO" | grep -q 'Rebuilding loom-daemon (cargo build'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} macOS signed-but-invalid: treated as tamper evidence, no source-build fallback"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} macOS signed-but-invalid: treated as tamper evidence, no source-build fallback"
    echo "  output: $outO"
fi

installedO_after="$("$WO/installed-loom-daemon" --version 2>/dev/null)"
assert_eq "$installedO_before" "$installedO_after" "macOS signed-but-invalid leaves the destination binary untouched"

# ------------------------------------------------------------
# P. KEYLESS verification is the DEFAULT with NO operator configuration
#    (#5054, the core regression this issue exists for). The release publishes
#    `.sig` + its `.pem` signing certificate; NO LOOM_DAEMON_UPDATE_COSIGN_*
#    env var is set and NO cosign.pub is checked in. Cases J/K/L only ever
#    exercised the LOOM_DAEMON_UPDATE_COSIGN_PUBKEY override, so this is the
#    first coverage of the path a stock install actually takes.
#
#    Asserts the recorded cosign argv, not just the log line: the expected
#    signer identity must be DERIVED from the release slug + tag, and the
#    issuer must be GitHub Actions' OIDC provider. Without that, a verification
#    that "ran" could still be trusting anything.
# ------------------------------------------------------------
WP="$BASE_WORKDIR/w-fetch-p"
new_fixture "$WP"
write_fake_daemon "$WP/installed-loom-daemon" "oldc0mm" "$WP/marker"

WP_ASSETS="$WP/gh-assets"
mkdir -p "$WP_ASSETS"
WP_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WP_ASSETS/$WP_BIN_NAME" "0.16.0" "keyles0"
sha256_of "$WP_ASSETS/$WP_BIN_NAME" > "$WP_ASSETS/$WP_BIN_NAME.sha256"
echo "fake-detached-signature" > "$WP_ASSETS/$WP_BIN_NAME.sig"
echo "-----BEGIN CERTIFICATE-----fake-----END CERTIFICATE-----" > "$WP_ASSETS/$WP_BIN_NAME.pem"

WP_FAKEBIN="$WP/fakebin"
mkdir -p "$WP_FAKEBIN"
WP_COSIGN_ARGS="$WP/cosign-args.log"
write_fake_gh "$WP_FAKEBIN/gh" "v0.16.0" "$WP_ASSETS"
write_fake_cosign_recording "$WP_FAKEBIN/cosign" 0 "$WP_COSIGN_ARGS"

outP=$( cd "$WP" && PATH="$WP_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WP/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcP=$(echo "$outP" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcP" "keyless (sig + cert, no env override): update proceeds (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outP" | grep -q 'cosign keyless signature verification passed' \
    && ! echo "$outP" | grep -q 'SKIPPING verification'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} keyless: real verification runs on a STOCK install (no loud skip, no env override)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} keyless: real verification runs on a STOCK install (no loud skip, no env override)"
    echo "  output: $outP"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if grep -qF -- '--certificate ' "$WP_COSIGN_ARGS" 2>/dev/null \
    && grep -qF -- '--certificate-identity-regexp ^https://github\.com/test-owner/test-repo/\.github/workflows/[^@]+@refs/tags/v0\.16\.0$' "$WP_COSIGN_ARGS" \
    && grep -qF -- '--certificate-oidc-issuer https://token.actions.githubusercontent.com' "$WP_COSIGN_ARGS" \
    && ! grep -qF -- '--key ' "$WP_COSIGN_ARGS"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} keyless: cosign was invoked with the DERIVED signer identity (repo slug + release tag) and the GitHub Actions issuer"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} keyless: cosign was invoked with the DERIVED signer identity (repo slug + release tag) and the GitHub Actions issuer"
    echo "  cosign argv: $(cat "$WP_COSIGN_ARGS" 2>/dev/null)"
fi

installedP_version="$("$WP/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$installedP_version" | grep -q 'commit keyles0'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} keyless verified: the artifact was provisioned"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} keyless verified: the artifact was provisioned"
    echo "  --version: $installedP_version"
fi

# ------------------------------------------------------------
# Q. Keyless verification FAILS (wrong signer / tampered blob) -> abort
#    (exit 1), destination untouched, no source-build fallback. The keyless
#    twin of case K: enforcement must be real in BOTH directions, or "default
#    verification" is theatre.
# ------------------------------------------------------------
WQ="$BASE_WORKDIR/w-fetch-q"
new_fixture "$WQ"
write_fake_daemon "$WQ/installed-loom-daemon" "oldc0mm" "$WQ/marker"
installedQ_before="$("$WQ/installed-loom-daemon" --version 2>/dev/null)"

WQ_ASSETS="$WQ/gh-assets"
mkdir -p "$WQ_ASSETS"
WQ_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WQ_ASSETS/$WQ_BIN_NAME" "0.16.0" "keybad0"
sha256_of "$WQ_ASSETS/$WQ_BIN_NAME" > "$WQ_ASSETS/$WQ_BIN_NAME.sha256"
echo "tampered-detached-signature" > "$WQ_ASSETS/$WQ_BIN_NAME.sig"
echo "-----BEGIN CERTIFICATE-----wrong-signer-----END CERTIFICATE-----" > "$WQ_ASSETS/$WQ_BIN_NAME.pem"

WQ_FAKEBIN="$WQ/fakebin"
mkdir -p "$WQ_FAKEBIN"
write_fake_gh "$WQ_FAKEBIN/gh" "v0.16.0" "$WQ_ASSETS"
write_fake_cosign "$WQ_FAKEBIN/cosign" 1
write_fake_cargo "$WQ_FAKEBIN/cargo"

outQ=$( cd "$WQ" && PATH="$WQ_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WQ/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcQ=$(echo "$outQ" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "1" "$rcQ" "keyless verification failure: aborts the update (exit 1)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outQ" | grep -q 'cosign keyless signature verification FAILED' \
    && ! echo "$outQ" | grep -q 'Rebuilding loom-daemon (cargo build'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} keyless failure: treated as tamper evidence, never a source-build fallback"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} keyless failure: treated as tamper evidence, never a source-build fallback"
    echo "  output: $outQ"
fi

installedQ_after="$("$WQ/installed-loom-daemon" --version 2>/dev/null)"
assert_eq "$installedQ_before" "$installedQ_after" "keyless verification failure leaves the destination binary untouched"

# ------------------------------------------------------------
# R. KEY-mode default resolution with NO env override (#5054): a key-signed
#    release (bare `.sig`, no `.pem`) plus a checked-in `.loom/cosign.pub`
#    verifies for real -- proving resolve_cosign_pubkey()'s conventional-path
#    branch works, which no prior test covered (J/K set the env override, L
#    resolved nothing at all).
# ------------------------------------------------------------
WR="$BASE_WORKDIR/w-fetch-r"
new_fixture "$WR"
write_fake_daemon "$WR/installed-loom-daemon" "oldc0mm" "$WR/marker"

WR_ASSETS="$WR/gh-assets"
mkdir -p "$WR_ASSETS"
WR_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WR_ASSETS/$WR_BIN_NAME" "0.16.0" "convkey"
sha256_of "$WR_ASSETS/$WR_BIN_NAME" > "$WR_ASSETS/$WR_BIN_NAME.sha256"
echo "fake-detached-signature" > "$WR_ASSETS/$WR_BIN_NAME.sig"
echo "-----BEGIN PUBLIC KEY-----fake-----END PUBLIC KEY-----" > "$WR/.loom/cosign.pub"

WR_FAKEBIN="$WR/fakebin"
mkdir -p "$WR_FAKEBIN"
WR_COSIGN_ARGS="$WR/cosign-args.log"
write_fake_gh "$WR_FAKEBIN/gh" "v0.16.0" "$WR_ASSETS"
write_fake_cosign_recording "$WR_FAKEBIN/cosign" 0 "$WR_COSIGN_ARGS"

outR=$( cd "$WR" && PATH="$WR_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WR/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcR=$(echo "$outR" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcR" "key mode via checked-in .loom/cosign.pub (no env override): update proceeds (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outR" | grep -q 'cosign signature verification passed' \
    && grep -qF -- "--key $WR/.loom/cosign.pub" "$WR_COSIGN_ARGS" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} key mode: the checked-in .loom/cosign.pub resolves with no env override"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} key mode: the checked-in .loom/cosign.pub resolves with no env override"
    echo "  output: $outR"
    echo "  cosign argv: $(cat "$WR_COSIGN_ARGS" 2>/dev/null)"
fi

# ------------------------------------------------------------
# S. The ARTIFACT's shape selects the mode, never local config (#5054): a
#    keyless release (`.sig` + `.pem`) is verified keylessly EVEN WHEN a stale
#    LOOM_DAEMON_UPDATE_COSIGN_PUBKEY is set. The inverse (letting a leftover
#    key win) would turn every operator's stale env var into a fleet-wide false
#    "tamper" abort.
# ------------------------------------------------------------
WS="$BASE_WORKDIR/w-fetch-s"
new_fixture "$WS"
write_fake_daemon "$WS/installed-loom-daemon" "oldc0mm" "$WS/marker"

WS_ASSETS="$WS/gh-assets"
mkdir -p "$WS_ASSETS"
WS_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WS_ASSETS/$WS_BIN_NAME" "0.16.0" "shapes0"
sha256_of "$WS_ASSETS/$WS_BIN_NAME" > "$WS_ASSETS/$WS_BIN_NAME.sha256"
echo "fake-detached-signature" > "$WS_ASSETS/$WS_BIN_NAME.sig"
echo "-----BEGIN CERTIFICATE-----fake-----END CERTIFICATE-----" > "$WS_ASSETS/$WS_BIN_NAME.pem"
echo "-----BEGIN PUBLIC KEY-----stale-----END PUBLIC KEY-----" > "$WS/stale-cosign.pub"

WS_FAKEBIN="$WS/fakebin"
mkdir -p "$WS_FAKEBIN"
WS_COSIGN_ARGS="$WS/cosign-args.log"
write_fake_gh "$WS_FAKEBIN/gh" "v0.16.0" "$WS_ASSETS"
write_fake_cosign_recording "$WS_FAKEBIN/cosign" 0 "$WS_COSIGN_ARGS"

outS=$( cd "$WS" && PATH="$WS_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WS/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    LOOM_DAEMON_UPDATE_COSIGN_PUBKEY="$WS/stale-cosign.pub" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcS=$(echo "$outS" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcS" "keyless release + stale pubkey env: still verifies keylessly (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outS" | grep -q 'cosign keyless signature verification passed' \
    && ! grep -qF -- '--key ' "$WS_COSIGN_ARGS" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact shape (not local config) selects the verification mode"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact shape (not local config) selects the verification mode"
    echo "  output: $outS"
    echo "  cosign argv: $(cat "$WS_COSIGN_ARGS" 2>/dev/null)"
fi

# ============================================================
# P1-P8. --prune-stale-entry-points (#5139): the stale-entry-point advisory
#     (tests 45-48 above) detects but never removes anything. This flag
#     removes EXACTLY what the check classifies as a "Python console script
#     (stale pip/pipx editable install)" — never `loom-daemon` itself, never
#     a legitimate bash-wrapper shim (whether current or itself stale).
#
#     Fixture PATH holds, across two directories:
#       PSTALE_BIN_DIR (the resolved binary's own directory):
#         - loom-daemon      : the resolved binary (must survive)
#         - loom-clean       : a CURRENT auto-generated shim, sibling
#                               loom-daemon == resolved (must survive)
#         - loom-claim       : same shape, a second legitimate wrapper
#                               (must survive) — the AC's named example
#         - loom-tokens      : a stale pip console script (must be pruned)
#         - loom-agent-spawn : a second stale pip console script (pruned)
#         - loom-search      : a third stale console script (pruned)
#       POLD_SHIM_DIR (a second PATH entry, simulating a moved install):
#         - loom-recover-orphans : an auto-generated shim whose sibling
#                                   loom-daemon does NOT match the resolved
#                                   binary (a STALE shim) — reported by the
#                                   warning, but must NEVER be pruned (it is
#                                   a legitimate wrapper, not a frozen Python
#                                   script).
# ============================================================
WP="$BASE_WORKDIR/w-prune"
new_fixture "$WP"
HEADP="$(cd "$WP" && git rev-parse --short HEAD)"
PSTALE_BIN_DIR="$WP/stale-bin"
mkdir -p "$PSTALE_BIN_DIR"

# The resolved daemon binary, on PATH.
write_fake_daemon "$PSTALE_BIN_DIR/loom-daemon" "$HEADP" "$WP/markerP"

# Two legitimate, CURRENT auto-generated PATH shims.
for _shim in loom-clean loom-claim; do
    cat > "$PSTALE_BIN_DIR/$_shim" <<SHIM
#!/usr/bin/env bash
# Auto-generated PATH shim (issue #4272) — do not edit by hand.
exec "\$(dirname "\$0")/loom-daemon" ${_shim#loom-} "\$@"
SHIM
    chmod +x "$PSTALE_BIN_DIR/$_shim"
done

# Three stale pip/PATH console scripts of the #4079 shape.
for _stale in loom-tokens loom-agent-spawn loom-search; do
    cat > "$PSTALE_BIN_DIR/$_stale" <<STALEPY
#!/usr/bin/python3
# -*- coding: utf-8 -*-
import sys
sys.exit(0)
STALEPY
    chmod +x "$PSTALE_BIN_DIR/$_stale"
done

# A second PATH directory holding a STALE (moved-target) legitimate shim: its
# sibling loom-daemon exists but is NOT the resolved binary.
POLD_SHIM_DIR="$WP/old-install"
mkdir -p "$POLD_SHIM_DIR"
write_fake_daemon "$POLD_SHIM_DIR/loom-daemon" "oldc0mm" "$WP/markerP-old"
cat > "$POLD_SHIM_DIR/loom-recover-orphans" <<'SHIM'
#!/usr/bin/env bash
# Auto-generated PATH shim (issue #4272) — do not edit by hand.
exec "$(dirname "$0")/loom-daemon" recover-orphans "$@"
SHIM
chmod +x "$POLD_SHIM_DIR/loom-recover-orphans"

PRUNE_PATH="$PSTALE_BIN_DIR:$POLD_SHIM_DIR:$TEST_PATH"

# P1. First run: the 3 stale Python scripts are reported as removed.
outP1=$( cd "$WP" && PATH="$PRUNE_PATH" \
    LOOM_DAEMON_BIN="$PSTALE_BIN_DIR/loom-daemon" \
    bash "$UPDATE_SCRIPT" --prune-stale-entry-points 2>&1; echo "EXIT=$?" )
rcP1=$(echo "$outP1" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcP1" "prune: successful prune exits 0"

TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outP1" | grep -q "removed: $PSTALE_BIN_DIR/loom-tokens" \
   && echo "$outP1" | grep -q "removed: $PSTALE_BIN_DIR/loom-agent-spawn" \
   && echo "$outP1" | grep -q "removed: $PSTALE_BIN_DIR/loom-search"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} prune: all 3 stale Python console scripts are reported removed (#5139)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} prune: all 3 stale Python console scripts are reported removed (#5139)"
    echo "  output: $outP1"
fi

# P2. The 3 stale scripts are actually gone from disk.
TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -e "$PSTALE_BIN_DIR/loom-tokens" && ! -e "$PSTALE_BIN_DIR/loom-agent-spawn" \
      && ! -e "$PSTALE_BIN_DIR/loom-search" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} prune: the stale Python console scripts are actually deleted from disk"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} prune: the stale Python console scripts are actually deleted from disk"
fi

# P3. loom-daemon itself is never touched.
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -x "$PSTALE_BIN_DIR/loom-daemon" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} prune: loom-daemon itself is never removed"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} prune: loom-daemon itself is never removed"
fi

# P4. The two CURRENT legitimate bash-wrapper shims (loom-clean, loom-claim)
#     are never touched — the exact "never touch the legitimate bash wrapper"
#     guardrail named in the issue.
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -x "$PSTALE_BIN_DIR/loom-clean" && -x "$PSTALE_BIN_DIR/loom-claim" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} prune: current legitimate bash-wrapper shims (loom-clean, loom-claim) survive"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} prune: current legitimate bash-wrapper shims (loom-clean, loom-claim) survive"
fi

# P5. The STALE bash-wrapper shim (loom-recover-orphans, target moved) is
#     reported by the warning but survives the prune untouched — the whole
#     point of excluding ANY shim (current or stale) from pruning.
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -x "$POLD_SHIM_DIR/loom-recover-orphans" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} prune: a STALE bash-wrapper shim (moved target) is reported but never deleted"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} prune: a STALE bash-wrapper shim (moved target) is reported but never deleted"
fi

# P6. Idempotent: running it again finds nothing left to prune.
outP6=$( cd "$WP" && PATH="$PRUNE_PATH" \
    LOOM_DAEMON_BIN="$PSTALE_BIN_DIR/loom-daemon" \
    bash "$UPDATE_SCRIPT" --prune-stale-entry-points 2>&1; echo "EXIT=$?" )
rcP6=$(echo "$outP6" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcP6" "prune: second run (idempotent) exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$outP6" | grep -q 'nothing to prune'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} prune: second run is a no-op (idempotent, #5139)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} prune: second run is a no-op (idempotent, #5139)"
    echo "  output: $outP6"
fi

# P7. After pruning, re-running the ordinary update (--check) emits NO stale
#     Python-console-script warning any more — the stale shim (a DIFFERENT
#     hazard class, deliberately left in place by design) may still surface
#     its own warning, so this asserts on the Python-script paths specifically
#     rather than "no warning at all".
outP7=$( cd "$WP" && PATH="$PRUNE_PATH" \
    LOOM_DAEMON_BIN="$PSTALE_BIN_DIR/loom-daemon" \
    bash "$UPDATE_SCRIPT" --check 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if ! echo "$outP7" | grep -q "$PSTALE_BIN_DIR/loom-tokens" \
   && ! echo "$outP7" | grep -q "$PSTALE_BIN_DIR/loom-agent-spawn" \
   && ! echo "$outP7" | grep -q "$PSTALE_BIN_DIR/loom-search"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} prune: converges — a later --check no longer warns about the pruned paths (#5139)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} prune: converges — a later --check no longer warns about the pruned paths (#5139)"
    echo "  output: $outP7"
fi

# P8. LOOM_SKIP_STALE_ENTRY_POINT_CHECK=1 keeps --prune-stale-entry-points a
#     no-op too (mirrors test 48's guarantee for the warning itself).
WP8="$BASE_WORKDIR/w-prune-skip"
new_fixture "$WP8"
HEADP8="$(cd "$WP8" && git rev-parse --short HEAD)"
PSTALE_BIN_DIR8="$WP8/stale-bin"
mkdir -p "$PSTALE_BIN_DIR8"
write_fake_daemon "$PSTALE_BIN_DIR8/loom-daemon" "$HEADP8" "$WP8/markerP8"
cat > "$PSTALE_BIN_DIR8/loom-tokens" <<'STALEPY'
#!/usr/bin/python3
import sys
sys.exit(0)
STALEPY
chmod +x "$PSTALE_BIN_DIR8/loom-tokens"

outP8=$( cd "$WP8" && PATH="$PSTALE_BIN_DIR8:$TEST_PATH" \
    LOOM_DAEMON_BIN="$PSTALE_BIN_DIR8/loom-daemon" \
    LOOM_SKIP_STALE_ENTRY_POINT_CHECK=1 \
    bash "$UPDATE_SCRIPT" --prune-stale-entry-points 2>&1; echo "EXIT=$?" )
rcP8=$(echo "$outP8" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcP8" "prune: LOOM_SKIP_STALE_ENTRY_POINT_CHECK=1 still exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -e "$PSTALE_BIN_DIR8/loom-tokens" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} prune: LOOM_SKIP_STALE_ENTRY_POINT_CHECK=1 leaves stale entries untouched"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} prune: LOOM_SKIP_STALE_ENTRY_POINT_CHECK=1 leaves stale entries untouched"
fi

# ============================================================
# 64. --help documents the drain flags (Issue #5138).
# ============================================================
help64_out=$(bash "$UPDATE_SCRIPT" --help 2>/dev/null)
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$help64_out" | grep -q -- '--drain' && echo "$help64_out" | grep -q -- '--timeout' \
    && echo "$help64_out" | grep -q -- '--force-after-timeout' \
    && echo "$help64_out" | grep -q -- '--restart-now'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --help documents --drain / --timeout / --force-after-timeout / --restart-now"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --help documents --drain / --timeout / --force-after-timeout / --restart-now"
fi

# ============================================================
# 65. --drain and --restart-now are mutually exclusive -> exit 1 with a clear
#     message (Issue #5138).
# ============================================================
out65=$(bash "$UPDATE_SCRIPT" --drain --restart-now 2>&1)
rc65=$?
assert_eq "1" "$rc65" "--drain + --restart-now conflict -> exit 1"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out65" | grep -qi 'mutually exclusive'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --drain + --restart-now conflict names the conflict"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --drain + --restart-now conflict names the conflict"
    echo "  output: $out65"
fi

# ============================================================
# 66. --timeout requires a numeric argument -> exit 1 rather than silently
#     swallowing a bad value (Issue #5138).
# ============================================================
bash "$UPDATE_SCRIPT" --drain --timeout notanumber >/dev/null 2>&1
rc66=$?
assert_eq "1" "$rc66" "--timeout with a non-numeric argument -> exit 1"

# ============================================================
# 67. Systemd DEFAULT (no flags at all, Issue #5138): a plain
#     loom-daemon-update.sh invocation on a systemd-managed host drives
#     `restart --drain` (not a bare `restart`), and an immediate relaunch
#     still reports success -> exit 0. Confirms the systemd-default decision
#     is wired all the way through to the actual invocation, not just
#     documented.
# ============================================================
W67="$BASE_WORKDIR/w67"
new_fixture "$W67"
HEAD67="$(cd "$W67" && git rev-parse --short HEAD)"
INSTALLED67="$W67/installed/loom-daemon"
mkdir -p "$W67/installed"
RESTART_MARKER67="$W67/restart-invoked"
write_fake_daemon_restart_argv "$INSTALLED67" "deadbee" "$RESTART_MARKER67" 0
NEW_FAKE67="$W67/new-fake-daemon"
write_fake_daemon_restart_argv "$NEW_FAKE67" "$HEAD67" "$RESTART_MARKER67" 0
sleep 60 >/dev/null 2>&1 &
RELAUNCHED_PID67=$!
bg_proc_track "$RELAUNCHED_PID67"
SD_BIN67="$W67/systemd-bin"
SD_LOG67="$W67/systemctl.log"
SD_STATE67="$W67/systemd-pid-state"
write_fake_systemd_pid_bin "$SD_BIN67" "$SD_LOG67" "$SD_STATE67" "4242:active:success" \
    "$RESTART_MARKER67" "${RELAUNCHED_PID67}:active:success"

out67=$( cd "$W67" && PATH="$SD_BIN67:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd67.service" \
    LOOM_DAEMON_BIN="$INSTALLED67" NEW_FAKE_BIN_SRC="$NEW_FAKE67" \
    bash "$UPDATE_SCRIPT" 2>&1 )
rc67=$?
kill "$RELAUNCHED_PID67" 2>/dev/null || true
assert_eq "0" "$rc67" "systemd DEFAULT (no flags) drain-mode update exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q -- '--drain' "$RESTART_MARKER67" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd default (no flags) drives 'restart --drain', not a bare 'restart' (Issue #5138)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd default (no flags) drives 'restart --drain', not a bare 'restart' (Issue #5138)"
    echo "  restart marker: $(cat "$RESTART_MARKER67" 2>/dev/null)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out67" | grep -qi 'DEFAULT' && echo "$out67" | grep -q '5138'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} output names the systemd drain-by-default decision"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} output names the systemd drain-by-default decision"
    echo "  output: $out67"
fi

# ============================================================
# 68. --restart-now opts OUT of the systemd drain-by-default: drives a bare
#     `restart` (no --drain) (Issue #5138).
# ============================================================
W68="$BASE_WORKDIR/w68"
new_fixture "$W68"
HEAD68="$(cd "$W68" && git rev-parse --short HEAD)"
INSTALLED68="$W68/installed/loom-daemon"
mkdir -p "$W68/installed"
RESTART_MARKER68="$W68/restart-invoked"
write_fake_daemon_restart_argv "$INSTALLED68" "deadbee" "$RESTART_MARKER68" 0
NEW_FAKE68="$W68/new-fake-daemon"
write_fake_daemon_restart_argv "$NEW_FAKE68" "$HEAD68" "$RESTART_MARKER68" 0
sleep 60 >/dev/null 2>&1 &
RELAUNCHED_PID68=$!
bg_proc_track "$RELAUNCHED_PID68"
SD_BIN68="$W68/systemd-bin"
SD_LOG68="$W68/systemctl.log"
SD_STATE68="$W68/systemd-pid-state"
write_fake_systemd_pid_bin "$SD_BIN68" "$SD_LOG68" "$SD_STATE68" "4242:active:success" \
    "$RESTART_MARKER68" "${RELAUNCHED_PID68}:active:success"

( cd "$W68" && PATH="$SD_BIN68:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd68.service" \
    LOOM_DAEMON_BIN="$INSTALLED68" NEW_FAKE_BIN_SRC="$NEW_FAKE68" \
    bash "$UPDATE_SCRIPT" --restart-now >/dev/null 2>&1 )
rc68=$?
kill "$RELAUNCHED_PID68" 2>/dev/null || true
assert_eq "0" "$rc68" "--restart-now update exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$(cat "$RESTART_MARKER68" 2>/dev/null)" == "restart" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --restart-now drives a bare 'restart' (no --drain), opting out of the systemd default"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --restart-now drives a bare 'restart' (no --drain), opting out of the systemd default"
    echo "  restart marker: $(cat "$RESTART_MARKER68" 2>/dev/null)"
fi

# ============================================================
# 69. Explicit --drain on a launchd host end-to-end: threads --drain through
#     to the restart invocation, and an immediate relaunch still reports
#     success (Issue #5138 / #4090). Launchd's OWN default stays the plain
#     restart (mirrors test 15) — --drain here is an explicit opt-in.
# ============================================================
W69="$BASE_WORKDIR/w69"
new_fixture "$W69"
HEAD69="$(cd "$W69" && git rev-parse --short HEAD)"
INSTALLED69="$W69/installed/loom-daemon"
mkdir -p "$W69/installed"
RESTART_MARKER69="$W69/restart-invoked"
write_fake_daemon_restart_argv "$INSTALLED69" "deadbee" "$RESTART_MARKER69" 0
NEW_FAKE69="$W69/new-fake-daemon"
write_fake_daemon_restart_argv "$NEW_FAKE69" "$HEAD69" "$RESTART_MARKER69" 0
LD_BIN69="$W69/launchd-bin"
sleep 60 >/dev/null 2>&1 &
RELAUNCHED_PID69=$!
bg_proc_track "$RELAUNCHED_PID69"
write_fake_launchd_loaded_bin "$LD_BIN69" "$W69/launchctl.log" "$RESTART_MARKER69" "$RELAUNCHED_PID69"

out69=$( cd "$W69" && PATH="$LD_BIN69:$TEST_PATH" LOOM_DAEMON_LAUNCHD=1 \
    LOOM_DAEMON_BIN="$INSTALLED69" NEW_FAKE_BIN_SRC="$NEW_FAKE69" \
    bash "$UPDATE_SCRIPT" --drain 2>&1 )
rc69=$?
kill "$RELAUNCHED_PID69" 2>/dev/null || true
assert_eq "0" "$rc69" "explicit --drain on launchd exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q -- '--drain' "$RESTART_MARKER69" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} explicit --drain on launchd threads --drain through to the restart invocation"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} explicit --drain on launchd threads --drain through to the restart invocation"
    echo "  restart marker: $(cat "$RESTART_MARKER69" 2>/dev/null)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out69" | grep -q '4090' || echo "$out69" | grep -qi 'DRAIN'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} output names the drain restart primitive"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} output names the drain restart primitive"
    echo "  output: $out69"
fi

# ============================================================
# 70. Drain timeout WITHOUT --force-after-timeout preserves the fail-safe
#     (Issue #5138 / #4090): the daemon accepts the drain request but never
#     exits/relaunches (simulating "in-flight sweep never finished within the
#     timeout, dispatch resumed") -> exit 8, the pre-update pid is reported as
#     STILL RUNNING, and — critically — the reset-failed+start self-heal is
#     NEVER invoked (that would defeat the fail-safe by forcing exactly the
#     sweep-cancelling restart it exists to prevent).
# ============================================================
W70="$BASE_WORKDIR/w70"
new_fixture "$W70"
HEAD70="$(cd "$W70" && git rev-parse --short HEAD)"
INSTALLED70="$W70/installed/loom-daemon"
mkdir -p "$W70/installed"
RESTART_MARKER70="$W70/restart-invoked"
write_fake_daemon_restart_argv "$INSTALLED70" "deadbee" "$RESTART_MARKER70" 0
NEW_FAKE70="$W70/new-fake-daemon"
write_fake_daemon_restart_argv "$NEW_FAKE70" "$HEAD70" "$RESTART_MARKER70" 0
# A REAL, live process standing in for "the daemon that never exited" — the
# fail-safe detector requires a live pid (kill -0), not just an unchanged number.
sleep 60 >/dev/null 2>&1 &
STILL_RUNNING_PID70=$!
bg_proc_track "$STILL_RUNNING_PID70"
SD_BIN70="$W70/systemd-bin"
SD_LOG70="$W70/systemctl.log"
SD_STATE70="$W70/systemd-pid-state"
# No post_restart_marker/post_state given: the unit's reported pid/state never
# change, no matter how long the poll runs -- exactly "the drain accepted the
# request but the daemon is still there, unmoved" (the fail-safe holding).
write_fake_systemd_pid_bin "$SD_BIN70" "$SD_LOG70" "$SD_STATE70" "${STILL_RUNNING_PID70}:active:success"

out70=$( cd "$W70" && PATH="$SD_BIN70:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd70.service" \
    LOOM_DAEMON_BIN="$INSTALLED70" NEW_FAKE_BIN_SRC="$NEW_FAKE70" \
    LOOM_DAEMON_DRAIN_POLL_SECS=1 LOOM_DAEMON_RESTART_POLL_INTERVAL=0.2 \
    bash "$UPDATE_SCRIPT" --drain 2>&1 )
rc70=$?
assert_eq "8" "$rc70" "drain timeout without --force-after-timeout -> exit 8 (fail-safe, not a failure)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out70" | grep -qi 'FAIL-SAFE' && echo "$out70" | grep -q "pid ${STILL_RUNNING_PID70}"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} exit-8 output names the fail-safe and the still-running pre-update pid"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} exit-8 output names the fail-safe and the still-running pre-update pid"
    echo "  output: $out70"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if ! grep -qi 'reset-failed' "$SD_LOG70" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} fail-safe timeout NEVER triggers the reset-failed+start self-heal"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} fail-safe timeout NEVER triggers the reset-failed+start self-heal"
    echo "  systemctl.log: $(cat "$SD_LOG70" 2>/dev/null)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
# Checked BEFORE the cleanup kill below -- this asserts the process was never
# touched by the update script itself, not merely that it happens to be alive
# right now.
if kill -0 "$STILL_RUNNING_PID70" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} the pre-update daemon process was never touched (no sweep-cancelling restart forced)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} the pre-update daemon process was never touched (no sweep-cancelling restart forced)"
fi
kill "$STILL_RUNNING_PID70" 2>/dev/null || true

# ============================================================
# 71. Same drain timeout shape as test 70, but WITH --force-after-timeout:
#     the daemon eventually force-cancels and exits (landing the unit in
#     'failed', the #4950 shape), so the ordinary reset-failed+start self-heal
#     DOES run and recovers it -> exit 0 (Issue #5138 never suppresses the
#     self-heal when the operator explicitly asked to force the roll
#     through).
# ============================================================
W71="$BASE_WORKDIR/w71"
new_fixture "$W71"
HEAD71="$(cd "$W71" && git rev-parse --short HEAD)"
INSTALLED71="$W71/installed/loom-daemon"
mkdir -p "$W71/installed"
RESTART_MARKER71="$W71/restart-invoked"
write_fake_daemon_restart_argv "$INSTALLED71" "deadbee" "$RESTART_MARKER71" 0
NEW_FAKE71="$W71/new-fake-daemon"
write_fake_daemon_restart_argv "$NEW_FAKE71" "$HEAD71" "$RESTART_MARKER71" 0
sleep 60 >/dev/null 2>&1 &
RECOVERED_PID71=$!
bg_proc_track "$RECOVERED_PID71"
SD_BIN71="$W71/systemd-bin"
SD_LOG71="$W71/systemctl.log"
SD_STATE71="$W71/systemd-pid-state"
write_fake_systemd_pid_bin "$SD_BIN71" "$SD_LOG71" "$SD_STATE71" "9999:active:success" \
    "$RESTART_MARKER71" "0:failed:timeout" "${RECOVERED_PID71}:active:success"

( cd "$W71" && PATH="$SD_BIN71:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd71.service" \
    LOOM_DAEMON_BIN="$INSTALLED71" NEW_FAKE_BIN_SRC="$NEW_FAKE71" \
    LOOM_DAEMON_DRAIN_POLL_SECS=1 LOOM_DAEMON_RESTART_POLL_INTERVAL=0.2 \
    LOOM_DAEMON_RESTART_KICKSTART_POLL_SECS=3 \
    bash "$UPDATE_SCRIPT" --drain --force-after-timeout >/dev/null 2>&1 )
rc71=$?
kill "$RECOVERED_PID71" 2>/dev/null || true
assert_eq "0" "$rc71" "drain timeout WITH --force-after-timeout still self-heals a failed unit -> exit 0"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q -- '--force-after-timeout' "$RESTART_MARKER71" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --force-after-timeout is threaded through to the restart invocation"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --force-after-timeout is threaded through to the restart invocation"
    echo "  restart marker: $(cat "$RESTART_MARKER71" 2>/dev/null)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -qi 'reset-failed' "$SD_LOG71" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --force-after-timeout still allows the reset-failed+start self-heal to run"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --force-after-timeout still allows the reset-failed+start self-heal to run"
    echo "  systemctl.log: $(cat "$SD_LOG71" 2>/dev/null)"
fi

# ============================================================
# 72. --dry-run on a systemd host without --restart-now describes the
#     drain-by-default plan (names #5119 and --restart-now); with
#     --restart-now it describes the immediate/non-drained plan instead
#     (Issue #5138). Neither writes anything.
# ============================================================
W72="$BASE_WORKDIR/w72"
new_fixture "$W72"
INSTALLED72="$W72/installed/loom-daemon"
mkdir -p "$W72/installed"
RESTART_MARKER72="$W72/restart-invoked"
write_fake_daemon_restart_argv "$INSTALLED72" "deadbee" "$RESTART_MARKER72" 0
SD_BIN72="$W72/systemd-bin"
SD_LOG72="$W72/systemctl.log"
write_fake_systemd_active_bin "$SD_BIN72" "$SD_LOG72" "4242"

dry72_default=$( cd "$W72" && PATH="$SD_BIN72:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd72.service" LOOM_DAEMON_BIN="$INSTALLED72" \
    bash "$UPDATE_SCRIPT" --dry-run 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$dry72_default" | grep -q -- '--drain' && echo "$dry72_default" | grep -qi 'DEFAULT' \
    && echo "$dry72_default" | grep -q '5119' && [[ ! -s "$RESTART_MARKER72" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --dry-run on systemd (no flags) describes the drain-by-default plan, no writes"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --dry-run on systemd (no flags) describes the drain-by-default plan, no writes"
    echo "  output: $dry72_default"
fi

dry72_now=$( cd "$W72" && PATH="$SD_BIN72:$TEST_PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-test-sd72.service" LOOM_DAEMON_BIN="$INSTALLED72" \
    bash "$UPDATE_SCRIPT" --dry-run --restart-now 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$dry72_now" | grep -qi 'IMMEDIATE' && ! echo "$dry72_now" | grep -q -- '--drain' \
    && [[ ! -s "$RESTART_MARKER72" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --dry-run --restart-now on systemd describes the immediate (non-drained) plan"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --dry-run --restart-now on systemd describes the immediate (non-drained) plan"
    echo "  output: $dry72_now"
fi

# ============================================================
# 25. Launchd-sandbox guards (#4078): the whole suite exercises the REAL
#     start/stop scripts, so prove it never reached the operator's live daemon.
#     (a) The suite-level decoy loom-daemon is still alive — no by-name kill
#         fired anywhere in the suite.
#     (b) No recorded launchctl invocation ever named a com.rjwalters.* label.
# ============================================================
TESTS_RUN=$((TESTS_RUN + 1))
if kill -0 "$DECOY_PID" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} suite-level decoy loom-daemon survived the whole update suite"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} suite-level decoy loom-daemon survived the whole update suite"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if launchd_sandbox_assert_no_production_label "$SANDBOX_LOG_DIR/launchctl-invocations.log"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} no launchctl invocation named a com.rjwalters.* label"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} no launchctl invocation named a com.rjwalters.* label"
    echo "  launchctl invocations: $(cat "$SANDBOX_LOG_DIR/launchctl-invocations.log" 2>/dev/null)"
fi

# ============================================================
# 44. Production-binary checksum guard (#4381 incident): the REAL
#     ~/.local/bin/loom-daemon must be byte-for-byte unchanged after the whole
#     suite runs. This is the direct regression test for the 2026-07-29
#     incident (a test fixture stub overwrote the production binary for ~9
#     hours) — it fails LOUDLY if the LOOM_DAEMON_BIN_DIR sandbox above is ever
#     bypassed, removed, or a new call site is added that skips it.
# ============================================================
_PROD_DAEMON_CHECKSUM_AFTER="$(_prod_daemon_checksum)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$_PROD_DAEMON_CHECKSUM_BEFORE" == "$_PROD_DAEMON_CHECKSUM_AFTER" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} real ${_PROD_DAEMON_BIN} is byte-identical before/after the suite (#4381 regression guard)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} real ${_PROD_DAEMON_BIN} CHANGED during this test run (#4381 regression!)"
    echo "  before: $_PROD_DAEMON_CHECKSUM_BEFORE"
    echo "  after:  $_PROD_DAEMON_CHECKSUM_AFTER"
fi

# ============================================================
# 45. Live daemon state guard (#5179): every live `.loom` state path reachable
#     from the ambient environment (the real $HOME/.loom, the live checkout's
#     .loom, an ambient LOOM_PID_FILE / LOOM_WORKSPACE / LOOM_MACHINE_CHECKOUT)
#     must be byte-and-mtime identical to its pre-suite snapshot — and a path
#     that was ABSENT must still be absent. This is what converts the whole
#     "state file leaked into production" class (#4087, #5131, #5179) from
#     "discovered by an operator on a degraded host" into "caught by the suite":
#     isolation alone is unfalsifiable, since each of the previous three fixes
#     also LOOKED complete.
# ============================================================
TESTS_RUN=$((TESTS_RUN + 1))
if live_state_sandbox_assert_untouched; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} no live .loom daemon state path was written during the suite ($(live_state_sandbox_snapshot_size) paths guarded, #5179)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} a LIVE .loom daemon state path was written during this test run (#5179 regression!)"
    echo "  sandbox in effect during the run:"
    live_state_sandbox_describe | sed 's/^/    /'
fi

# ---------- summary ----------
echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
