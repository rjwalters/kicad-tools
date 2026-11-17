#!/usr/bin/env bash
# launchd-sandbox.sh — shared test sandbox for the loom-daemon lifecycle tests
# (issue #4078).
#
# Why this exists: the daemon lifecycle tests deliberately execute the REAL
# loom-daemon-start.sh / loom-daemon-stop.sh (not a mock) so the restart path
# exercises the actual scripts. But launchd is machine-GLOBAL, and the stop
# script's PID-resolution ladder can reach outside any per-test sandbox two
# ways:
#
#   1. launchd label lookup — `launchctl print gui/<uid>/com.rjwalters.loom-daemon`
#      resolves against the operator's REAL production LaunchAgent when the
#      test does not scope LOOM_LAUNCHD_LABEL, so the stop script adopts and
#      SIGTERMs the live daemon (issue #4078, curator Correction 2).
#   2. label-blind pgrep — `pgrep -f '(^|/)loom-daemon$'` matches the production
#      daemon (or another repo's daemon) by binary NAME alone, so even a scoped
#      LOOM_LAUNCHD_LABEL does not close the hole (curator Correction 3).
#
# This helper closes BOTH axes for a sourcing test, and is deliberately narrow:
# it stubs only the syscall-level tools (`launchctl`, `pgrep`), NEVER the
# lifecycle scripts themselves — so the "exercise the actual scripts" intent of
# the update test's header survives.
#
# Usage (source it):
#   source "$SCRIPT_DIR/lib/launchd-sandbox.sh"
#   export LOOM_LAUNCHD_LABEL="$(launchd_sandbox_new_label)"
#   launchd_sandbox_install_stubs "$FAKE_BIN_DIR" "$LOG_DIR"   # optional
#   decoy_pid="$(launchd_sandbox_spawn_decoy "$WORKDIR")"
#   ... run the real scripts ...
#   launchd_sandbox_decoy_alive "$decoy_pid"                    # assert survival
#   launchd_sandbox_assert_no_production_label "$LOG_DIR/launchctl-invocations.log"

# Print a per-run, guaranteed-nonexistent scratch launchd label. A label built
# from $$ + $RANDOM cannot collide with the production `com.rjwalters.loom-daemon`
# label, so `launchctl print` against it always resolves false and the stop
# script can never observe or mutate a real loaded job under it.
launchd_sandbox_new_label() {
    echo "com.example.loom-sandbox-$$-${RANDOM}"
}

# Install stub `launchctl` and `pgrep` into <bin_dir> (which the caller must
# place on PATH ahead of the real tools). Both stubs:
#   - record every invocation to <log_dir>/{launchctl,pgrep}-invocations.log
#     (one line per call) so a test can assert what was attempted, and
#   - are STRUCTURALLY UNABLE to touch a real launchd job or process:
#       * launchctl: `print` always exits 1 (job "not loaded"); everything else
#         is a harmless no-op exit 0. This is domain-AGNOSTIC (#4130): `print`
#         fails for BOTH gui/<uid> and user/<uid>, so it correctly stubs a job
#         under whichever domain resolve_launchd_domain() picks, and the failing
#         gui/<uid> probe drives the resolver to the user/<uid> fallback — so a
#         sandboxed test never depends on the host having (or lacking) a GUI
#         login. launchd_sandbox_assert_no_production_label scopes on the LABEL
#         (com.rjwalters.*), independent of the domain, so it still holds.
#       * pgrep: always exits 1 with no output (never matches a real process),
#         so a label-blind `pgrep -f loom-daemon` in the stop script can never
#         return the production daemon's (or a decoy's) PID.
#
# These are belt-and-braces on top of the production-side fixes (stop.sh now
# honors LOOM_DAEMON_LAUNCHD and scopes its pgrep fallback to the default
# label): a test that sources this helper cannot reach the real tools even if a
# future regression re-opens a production path.
launchd_sandbox_install_stubs() {
    local bin_dir="$1" log_dir="$2"
    mkdir -p "$bin_dir" "$log_dir"
    local launchctl_log="$log_dir/launchctl-invocations.log"
    local pgrep_log="$log_dir/pgrep-invocations.log"
    : > "$launchctl_log"
    : > "$pgrep_log"

    cat > "$bin_dir/launchctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$launchctl_log"
# A scratch-labeled job never exists: 'print' reports not-loaded (exit 1);
# 'bootout' and everything else are harmless no-ops. NEVER touches a real job.
case "\${1:-}" in
  print) exit 1 ;;
  *)     exit 0 ;;
esac
EOF
    chmod +x "$bin_dir/launchctl"

    cat > "$bin_dir/pgrep" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$pgrep_log"
# Never match a real process — the whole point is that a label-blind pgrep must
# not be able to find the production daemon (or a decoy) during a test.
exit 1
EOF
    chmod +x "$bin_dir/pgrep"
}

# Spawn a decoy process whose argv ends in `/loom-daemon`, so it matches the
# stop script's label-blind fallback pattern `(^|/)loom-daemon$` exactly like a
# real production daemon would. Prints the decoy's PID on stdout; the caller
# must kill it during cleanup.
#
# The decoy is a tiny sleeper script literally named `loom-daemon` (not an
# `exec sleep`, which would rewrite argv to "sleep" and stop matching). It runs
# under <work_dir>/decoy-bin/loom-daemon.
launchd_sandbox_spawn_decoy() {
    local work_dir="$1"
    local decoy_dir="$work_dir/decoy-bin"
    mkdir -p "$decoy_dir"
    cat > "$decoy_dir/loom-daemon" <<'EOF'
#!/usr/bin/env bash
# Decoy: keep this process alive with `loom-daemon` in its argv.
while true; do sleep 1; done
EOF
    chmod +x "$decoy_dir/loom-daemon"
    # Redirect the decoy's stdio to /dev/null so it does not hold open the
    # stdout pipe of a `$(launchd_sandbox_spawn_decoy ...)` command substitution
    # (which would block the caller forever waiting on this never-exiting child).
    "$decoy_dir/loom-daemon" >/dev/null 2>&1 &
    echo $!
}

# Return 0 if the decoy PID is still alive, non-zero otherwise.
launchd_sandbox_decoy_alive() {
    kill -0 "$1" 2>/dev/null
}

# Assert that no recorded launchctl invocation named a production label
# (com.rjwalters.*). Returns 0 (safe) when the log is absent/empty or contains
# only scratch labels; returns 1 when a production label appears.
launchd_sandbox_assert_no_production_label() {
    local launchctl_log="$1"
    [[ -f "$launchctl_log" ]] || return 0
    ! grep -q 'com\.rjwalters\.' "$launchctl_log"
}
