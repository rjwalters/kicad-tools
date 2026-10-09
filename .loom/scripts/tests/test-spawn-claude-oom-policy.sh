#!/usr/bin/env bash
# Tests that spawn-claude.sh's systemd --user scope carries
# `-p OOMPolicy=continue` (#11076). systemd's default OOMPolicy=stop tears down
# the WHOLE scope when the kernel OOM-kills any one child (a `git`/`rustc`),
# SIGTERMing the claude CLI with it; `continue` fails only the offending command.
#
# Hermetic: stub `systemd-run` / `systemctl` / `nproc` / `claude` under
# mktemp -d, LOOM_SYSTEMD_FORCE=1 (lib/systemd-user.sh's test-only seam) to
# drive the enforced path on any OS, and an explicit caller credential
# (LOOM_SPAWN_NO_EXPORT) so no real loom-daemon or token pool is needed.
# Split from test-spawn-claude.sh, which is frozen by the file-size ratchet.
set -uo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); echo "  ok: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
WS="$T/ws"
mkdir -p "$WS/.loom" "$T/bin"
ln -s "$SCRIPTS_DIR" "$WS/.loom/scripts"
echo '{"runtimes": {"containment": {"enabled": false}}}' >"$WS/.loom/config.json"

cat >"$T/bin/claude" <<'STUB'
#!/usr/bin/env bash
echo "stub-claude ran"
STUB
printf '#!/usr/bin/env bash\necho 8\n' >"$T/bin/nproc"
printf '#!/usr/bin/env bash\nexit 0\n' >"$T/bin/systemctl"
# Logs its argv, then execs whatever follows `--` (as a real --scope would).
SYSTEMD_RUN_LOG="$T/systemd-run.log"
cat >"$T/bin/systemd-run" <<STUB
#!/usr/bin/env bash
echo "\$*" >>"$SYSTEMD_RUN_LOG"
while [[ \$# -gt 0 && "\$1" != "--" ]]; do shift; done
shift
exec "\$@"
STUB
chmod +x "$T/bin/"*

echo "spawn-claude systemd scope OOMPolicy (#11076)"

OUT="$(env -u LOOM_SWEEP_CPU_QUOTA LOOM_WORKSPACE="$WS" LOOM_DAEMON_BIN=/bin/false \
    LOOM_SPAWN_NO_EXPORT=1 CLAUDE_CODE_OAUTH_TOKEN=fake-caller-token \
    LOOM_SWEEP_INFLIGHT_SWEEPS=1 LOOM_SYSTEMD_FORCE=1 PATH="$T/bin:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p ping 2>&1)"
LOG="$(cat "$SYSTEMD_RUN_LOG" 2>/dev/null || true)"
# The last systemd-run call is the real exec (an earlier one is the probe).
FINAL="$(tail -n1 "$SYSTEMD_RUN_LOG" 2>/dev/null || true)"

if [[ "$OUT" == *"enforcing CPUQuota="* ]]; then ok "the enforced systemd scope path engaged"; else bad "the enforced systemd scope path engaged"; fi
if [[ "$OUT" == *"stub-claude ran"* ]]; then ok "the wrapped stub claude still runs"; else bad "the wrapped stub claude still runs"; fi
if [[ "$FINAL" == *"-p OOMPolicy=continue"* && "$FINAL" == *"CPUQuota="* ]]; then
    ok "the real systemd-run scope passes -p OOMPolicy=continue"
else
    bad "the real systemd-run scope passes -p OOMPolicy=continue"
fi

if [[ $FAIL -gt 0 ]]; then
    echo "--- spawn-claude output ---"; echo "$OUT"; echo "--- systemd-run log ---"; echo "$LOG"
fi
echo "passed=$PASS failed=$FAIL"
[[ $FAIL -eq 0 ]]
