#!/usr/bin/env bash
# Tests spawn-claude.sh's per-scope MemoryMax wiring (#11094): the call to
# `loom-daemon ram-scope-limit` and how each of its exit codes lands on the
# systemd-run argv. The sizing policy itself (history-gated, env-disable-able)
# is unit-tested in loom-daemon/src/cli/ram_scope_limit.rs; here the daemon is
# a stub so only the shell call-site is under test.
# Split from test-spawn-claude.sh, which is frozen by the file-size ratchet.
set -uo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); echo "  ok: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

echo "spawn-claude systemd scope MemoryMax (#11094)"
WS="$T/big"
mkdir -p "$WS/.loom" "$T/bin"
ln -s "$SCRIPTS_DIR" "$WS/.loom/scripts"
echo '{"runtimes": {"containment": {"enabled": false}}}' >"$WS/.loom/config.json"
printf '#!/usr/bin/env bash\necho "stub-claude ran"\n' >"$T/bin/claude"
printf '#!/usr/bin/env bash\necho 8\n' >"$T/bin/nproc"
printf '#!/usr/bin/env bash\nexit 0\n' >"$T/bin/systemctl"
SYSTEMD_RUN_LOG="$T/systemd-run.log"
cat >"$T/bin/systemd-run" <<STUB
#!/usr/bin/env bash
echo "\$*" >>"$SYSTEMD_RUN_LOG"
while [[ \$# -gt 0 && "\$1" != "--" ]]; do shift; done
shift
exec "\$@"
STUB
chmod +x "$T/bin/"*
# Stub daemon: answers `ram-scope-limit` per STUB_MEM_RC / STUB_MEM_OUT and
# records its argv; every other subcommand fails (no daemon features needed).
DAEMON_LOG="$T/daemon.log"
cat >"$T/bin/loom-daemon" <<STUB
#!/usr/bin/env bash
[[ "\$1" == ram-scope-limit ]] || exit 2
echo "\$*" >>"$DAEMON_LOG"
[[ -n "\${STUB_MEM_OUT:-}" ]] && echo "\$STUB_MEM_OUT"
exit "\${STUB_MEM_RC:-0}"
STUB
chmod +x "$T/bin/loom-daemon"

run() {
    : >"$SYSTEMD_RUN_LOG"
    : >"$DAEMON_LOG"
    env -u LOOM_SWEEP_CPU_QUOTA -u LOOM_SWEEP_MEMORY_MAX "$@" LOOM_WORKSPACE="$WS" LOOM_DAEMON_BIN="$T/bin/loom-daemon" \
        LOOM_SPAWN_NO_EXPORT=1 CLAUDE_CODE_OAUTH_TOKEN=fake-caller-token LOOM_SWEEP_INFLIGHT_SWEEPS=1 \
        LOOM_SYSTEMD_FORCE=1 PATH="$T/bin:$PATH" \
        "$SCRIPTS_DIR/spawn-claude.sh" -p ping 2>&1
}

OUT="$(run STUB_MEM_OUT=4096 STUB_MEM_RC=0)"
FINAL="$(tail -n1 "$SYSTEMD_RUN_LOG")"
if [[ "$FINAL" == *"-p MemoryMax=4096M"* && "$FINAL" == *"-p OOMPolicy=continue"* && "$FINAL" == *"CPUQuota="* ]]; then
    ok "exit 0 -> real scope carries MemoryMax=4096M with OOMPolicy=continue"
else bad "exit 0 -> real scope carries MemoryMax: $FINAL"; fi
if [[ "$OUT" == *"stub-claude ran"* ]]; then ok "stub claude still runs"; else bad "stub claude still runs"; fi
if grep -q -- "ram-scope-limit --workspace $WS --probe" "$DAEMON_LOG"; then
    ok "daemon asked with --workspace and --probe"
else bad "daemon asked with --workspace and --probe: $(cat "$DAEMON_LOG")"; fi

run STUB_MEM_RC=1 >/dev/null
if grep -q MemoryMax "$SYSTEMD_RUN_LOG"; then bad "exit 1 (no limit) -> no MemoryMax"; else ok "exit 1 (no limit) -> no MemoryMax"; fi

OUT="$(run STUB_MEM_OUT=4096 STUB_MEM_RC=3)"
FINAL="$(tail -n1 "$SYSTEMD_RUN_LOG")"
if [[ "$FINAL" != *MemoryMax* && "$FINAL" == *CPUQuota* && "$OUT" == *"stub-claude ran"* && "$OUT" == *"rejected by systemd"* ]]; then
    ok "exit 3 (probe rejected) degrades to no limit with a warning, CPU quota kept"
else bad "exit 3 (probe rejected) degrades: $FINAL"; fi

OUT="$(run STUB_MEM_OUT=4096 STUB_MEM_RC=2)"
FINAL="$(tail -n1 "$SYSTEMD_RUN_LOG")"
if [[ "$FINAL" != *MemoryMax* && "$OUT" == *"stub-claude ran"* ]]; then
    ok "unknown exit (older binary) -> no limit, spawn proceeds"
else bad "unknown exit (older binary) -> no limit: $FINAL"; fi

echo "passed=$PASS failed=$FAIL"
[[ $FAIL -eq 0 ]]
