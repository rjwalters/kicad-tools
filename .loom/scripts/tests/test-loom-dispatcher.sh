#!/usr/bin/env bash
# test-loom-dispatcher.sh — tests for the machine-level `loom` dispatcher and its
# provisioning (Epic #3835 Phase 3a #4157, Phase 3b #4229).
#
# Covers:
#   - scripts/loom — checkout resolution (AC1), collision resolution (AC3),
#     the three status contexts (AC7), config resolution via the Phase 2 tier
#     resolver incl. the jq-absent case (AC5), and the thin `update` boundary
#     (Finding 3).
#   - scripts/install/provision-dispatcher.sh — the verifiable-globals contract
#     (#4053), the symlink checkout (AC1), and the console-script invariant (AC6).
#   - the no-shadow regression proving the two `loom` invocation forms stay
#     disjoint (AC4).
#   - Phase 3b (#4229): the `restart` verb (collision guard, supervised-IPC
#     drain-and-roll, stop+start fallback) and the LOOM_MACHINE_CHECKOUT
#     hand-off every delegating verb (start/stop/update/restart) gives its
#     lifecycle-script delegate, INCLUDING from a non-repo directory (the
#     concrete "loom update works outside a Loom source checkout" regression).
#
# Throwaway `mktemp -d` scratch per case, matching test-config-resolver.sh.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# defaults/scripts/tests -> defaults/scripts/tests/../../.. -> repo root
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

DISPATCHER="$REPO_ROOT/scripts/loom"
PROVISION_LIB="$REPO_ROOT/scripts/install/provision-dispatcher.sh"
REAL_RESOLVER="$REPO_ROOT/defaults/scripts/lib/config-resolver.sh"
REAL_SPAWN_WORKER="$REPO_ROOT/defaults/scripts/spawn-worker.sh"
REAL_HARVEST_LIB="$REPO_ROOT/defaults/scripts/lib/daemon-env-harvest.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_contains() {
    if [[ "$1" == *"$2"* ]]; then pass "$3"; else fail "$3 (missing substring: '$2' in: $1)"; fi
}
assert_not_contains() {
    if [[ "$1" != *"$2"* ]]; then pass "$3"; else fail "$3 (unexpected substring: '$2')"; fi
}
assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}

[[ -f "$DISPATCHER" ]]    || { echo "dispatcher not found at $DISPATCHER"; exit 1; }
[[ -f "$PROVISION_LIB" ]] || { echo "provisioning lib not found at $PROVISION_LIB"; exit 1; }

# Build a fake machine-level checkout with stub daemon-lifecycle scripts and a
# real copy of the config resolver. Echoes the checkout path.
make_checkout() {
    local c; c="$(mktemp -d)"
    mkdir -p "$c/defaults/scripts/cli" "$c/defaults/scripts/lib"
    # Each stub also echoes the LOOM_MACHINE_CHECKOUT it inherited (empty
    # string if unset) so tests can assert on the machine-mode hand-off
    # (#4229) without changing any existing substring assertion below.
    cat > "$c/defaults/scripts/cli/loom-daemon-start.sh" <<'EOF'
#!/usr/bin/env bash
echo "STUB_START args=[$*] machine_checkout=[${LOOM_MACHINE_CHECKOUT:-}]"
EOF
    cat > "$c/defaults/scripts/cli/loom-daemon-stop.sh" <<'EOF'
#!/usr/bin/env bash
echo "STUB_STOP args=[$*] machine_checkout=[${LOOM_MACHINE_CHECKOUT:-}]"
EOF
    cat > "$c/defaults/scripts/cli/loom-daemon-update.sh" <<'EOF'
#!/usr/bin/env bash
echo "STUB_UPDATE args=[$*] machine_checkout=[${LOOM_MACHINE_CHECKOUT:-}]"
EOF
    chmod +x "$c/defaults/scripts/cli/"*.sh
    cp "$REAL_RESOLVER" "$c/defaults/scripts/lib/config-resolver.sh"
    printf '%s\n' "$c"
}

# Like make_checkout(), but ALSO ships a real copy of lib/daemon-env-harvest.sh
# (#4581) and a loom-daemon-start.sh stub that echoes back the LOOM_WORK_FINDER
# / LOOM_MAIN_HEALTH_GATE env it inherited — so a restart-fallback test can
# assert the harvest-and-preserve pattern actually re-exported values from a
# live plist/unit BEFORE this stub ran, not merely that the stub ran at all.
make_checkout_with_harvest() {
    local c; c="$(mktemp -d)"
    mkdir -p "$c/defaults/scripts/cli" "$c/defaults/scripts/lib"
    cat > "$c/defaults/scripts/cli/loom-daemon-start.sh" <<'EOF'
#!/usr/bin/env bash
echo "STUB_START args=[$*] LOOM_WORK_FINDER=[${LOOM_WORK_FINDER:-}] LOOM_MAIN_HEALTH_GATE=[${LOOM_MAIN_HEALTH_GATE:-}]"
EOF
    cat > "$c/defaults/scripts/cli/loom-daemon-stop.sh" <<'EOF'
#!/usr/bin/env bash
echo "STUB_STOP args=[$*]"
EOF
    chmod +x "$c/defaults/scripts/cli/"*.sh
    cp "$REAL_RESOLVER" "$c/defaults/scripts/lib/config-resolver.sh"
    cp "$REAL_HARVEST_LIB" "$c/defaults/scripts/lib/daemon-env-harvest.sh"
    printf '%s\n' "$c"
}

# Build a fake consumer repo with a pool-manager stub + config tiers.
make_consumer_repo() {
    local r; r="$(mktemp -d)"
    mkdir -p "$r/.loom/bin"
    cat > "$r/.loom/bin/loom" <<'EOF'
#!/usr/bin/env bash
echo "POOL_MANAGER args=[$*]"
EOF
    chmod +x "$r/.loom/bin/loom"
    printf '%s\n' "$r"
}

# Build a fake consumer repo whose .loom/scripts carries a REAL spawn-worker.sh
# + config-resolver.sh, plus stub spawn-<runtime>.sh runners that just echo
# their own name and argv (no real claude/codex, no live tokens) — the pattern
# test-spawn-worker.sh uses. Exercises `loom sweep`'s runtime routing (#4480).
# Echoes the repo path.
make_sweep_repo() {
    local r; r="$(mktemp -d)"
    mkdir -p "$r/.loom/scripts/lib"
    cp "$REAL_SPAWN_WORKER" "$r/.loom/scripts/spawn-worker.sh"
    cp "$REAL_RESOLVER" "$r/.loom/scripts/lib/config-resolver.sh"
    # Stub runners: each announces which runtime ran and forwards its argv so a
    # test can assert on both the selected runner and the passthrough flags.
    cat > "$r/.loom/scripts/spawn-claude.sh" <<'EOF'
#!/usr/bin/env bash
echo "STUB_CLAUDE argv=[$*]"
EOF
    cat > "$r/.loom/scripts/spawn-codex.sh" <<'EOF'
#!/usr/bin/env bash
echo "STUB_CODEX argv=[$*]"
EOF
    chmod +x "$r/.loom/scripts/"spawn-*.sh
    printf '%s\n' "$r"
}

# ── AC1 / AC7: checkout resolution + status ABSENT checkout ───────────────────
echo "Test 1: status reports an ABSENT machine checkout (AC1 resolution)"
out=$(LOOM_HOME="/nonexistent/loom/checkout/xyz" bash "$DISPATCHER" status 2>&1)
assert_contains "$out" "ABSENT" "status shows ABSENT when checkout missing"
assert_contains "$out" "machine-level dispatcher" "status self-identifies as machine-level"

# ── AC7: three distinguishable status contexts ───────────────────────────────
echo "Test 2: status contexts are correct and distinguishable (AC7)"
CHK=$(make_checkout)
CONSUMER=$(make_consumer_repo)

# (1) consumer repo root
out=$(cd "$CONSUMER" && LOOM_HOME="$CHK" bash "$DISPATCHER" status 2>&1)
assert_contains "$out" "consumer-repo" "consumer repo context labelled consumer-repo"
assert_contains "$out" "pool manager:  present" "consumer repo notes the pool manager"

# (2) git worktree — a real Loom worktree of a repo that commits .loom/ carries
# its own checked-out .loom/ directory, so $WT gets one too. This exercises the
# ordering fix: the linked-worktree (.git file) check must win over the
# consumer-repo (.loom/ dir) check, else the worktree is misclassified as a
# consumer-repo rooted at itself instead of at the main checkout.
WT=$(mktemp -d); mkdir -p "$WT/.loom"
MAIN=$(mktemp -d); mkdir -p "$MAIN/.loom"
printf 'gitdir: %s/.git/worktrees/wt\n' "$MAIN" > "$WT/.git"
out=$(cd "$WT" && LOOM_HOME="$CHK" bash "$DISPATCHER" status 2>&1)
assert_contains "$out" "git-worktree" "worktree context labelled git-worktree"
assert_contains "$out" "$MAIN" "worktree resolves LOOM_CTX_ROOT to the main checkout, not the worktree"

# (3) non-repo directory
NR=$(mktemp -d)
out=$(cd "$NR" && LOOM_HOME="$CHK" bash "$DISPATCHER" status 2>&1)
assert_contains "$out" "non-repo" "non-repo context labelled non-repo"

# ── AC3: collision resolution for the overlapping verbs ──────────────────────
echo "Test 3: bare 'start' inside a consumer repo disambiguates, never runs silently (AC3)"
set +e
out=$(cd "$CONSUMER" && LOOM_HOME="$CHK" bash "$DISPATCHER" start 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "3" "bare 'loom start' in a repo exits 3 (disambiguating non-zero)"
assert_contains "$out" ".loom/bin/loom start" "disambiguation names the per-repo pool surface"
assert_contains "$out" "loom start --machine" "disambiguation names the machine surface"
assert_not_contains "$out" "STUB_START" "bare 'loom start' did NOT run the daemon start (no silent surface)"

echo "Test 4: 'start --machine' bypasses the guard and delegates (strips --machine)"
set +e
out=$(cd "$CONSUMER" && LOOM_HOME="$CHK" bash "$DISPATCHER" start --machine 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "0" "'loom start --machine' exits 0"
assert_contains "$out" "STUB_START" "'--machine' delegates to loom-daemon-start.sh"
assert_contains "$out" "args=[]" "the --machine selector is stripped before delegating"

# ── Finding 3: thin update verb ──────────────────────────────────────────────
echo "Test 5: 'update' is a thin delegator with no rebuild logic of its own (Finding 3)"
set +e
out=$(LOOM_HOME="$CHK" bash "$DISPATCHER" update --check 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "0" "'loom update' exits 0 via delegation"
assert_contains "$out" "STUB_UPDATE args=[--check]" "'update' delegates to loom-daemon-update.sh, passing args"
# Structural: the dispatcher must not itself implement rebuild/restart.
src="$(cat "$DISPATCHER")"
assert_not_contains "$src" "cargo build" "dispatcher source contains no 'cargo build' (thin boundary)"
assert_not_contains "$src" "launchctl" "dispatcher source contains no 'launchctl' (thin boundary)"
assert_not_contains "$src" "git pull" "dispatcher source contains no 'git pull' (thin boundary)"
# The daemon half stays thin (no cargo/launchctl), but the mcp-loom bundle
# refresh (#4230) DOES live here — assert the wiring is present.
assert_contains "$src" "loom_refresh_mcp_bundle" "dispatcher wires the mcp-loom bundle refresh into update (#4230)"

# ── #4230: update refreshes the served mcp-loom bundle before delegating ──────
echo "Test 5b: 'update' refreshes the mcp-loom bundle, then delegates (#4230)"
CHK2=$(make_checkout)
# Fresh bundle: dist present, NO src/ dir -> staleness check finds nothing newer,
# so the refresh reports 'already fresh' WITHOUT invoking npm (keeps the unit
# test hermetic/offline).
mkdir -p "$CHK2/mcp-loom/dist"; : > "$CHK2/mcp-loom/dist/index.js"
set +e
out=$(LOOM_HOME="$CHK2" bash "$DISPATCHER" update 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "0" "'loom update' exits 0 with an mcp-loom bundle present"
assert_contains "$out" "mcp-loom bundle already fresh" "'update' checks the served mcp-loom bundle (#4230)"
assert_contains "$out" "STUB_UPDATE" "'update' still delegates the daemon update after the bundle refresh"

echo "Test 5c: 'update --check' does not touch the mcp-loom bundle (#4230)"
set +e
out=$(LOOM_HOME="$CHK2" bash "$DISPATCHER" update --check 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "0" "'loom update --check' exits 0"
assert_contains "$out" "read-only mode" "'update --check' skips the bundle refresh (read-only)"
assert_contains "$out" "STUB_UPDATE args=[--check]" "'update --check' still delegates with --check"

# ── AC5: config resolution through the Phase 2 tier resolver ─────────────────
if command -v jq >/dev/null 2>&1; then
    echo "Test 6: status resolves config through the tier chain — local tier wins (AC5)"
    RTIER=$(make_consumer_repo)
    mkdir -p "$RTIER/.loom" "$RTIER/.loom-local"
    echo '{"autonomous":{"workFinder":{"enabled":false}}}' > "$RTIER/.loom/config.json"
    echo '{"autonomous":{"workFinder":{"enabled":true}}}'  > "$RTIER/.loom-local/local.json"
    out=$(cd "$RTIER" && LOOM_CONFIG_DEFAULTS_FILE="" LOOM_HOME="$CHK" bash "$DISPATCHER" status 2>&1)
    assert_contains "$out" "autonomous.workFinder.enabled = true" ".loom-local/local.json overrides .loom/config.json (resolver, not direct read)"
else
    echo "Test 6: SKIP (jq not on PATH)"
fi

echo "Test 7: status is explicit when jq is unavailable — not masqueraded as 'no config' (AC5)"
JQLESS_BIN=$(mktemp -d)
for t in sed dirname readlink; do
    p="$(command -v "$t" 2>/dev/null || true)"
    [[ -n "$p" ]] && ln -s "$p" "$JQLESS_BIN/$t"
done
REAL_BASH="$(command -v bash)"
set +e
out=$(cd "$NR" && PATH="$JQLESS_BIN" HOME="$NR" LOOM_HOME="$CHK" "$REAL_BASH" "$DISPATCHER" status 2>&1)
set -e 2>/dev/null || true
assert_contains "$out" "jq not available" "jq-absent status says so explicitly"

# ── AC4: no-shadow regression — the two invocation forms stay disjoint ───────
echo "Test 8: bare 'loom' and './.loom/bin/loom' resolve to different surfaces in the same shell (AC4)"
# Provision the dispatcher into a temp bin dir and put ONLY that dir on PATH.
BIN=$(mktemp -d)
HOME_T=$(mktemp -d)
# shellcheck source=/dev/null
source "$PROVISION_LIB"
provision_loom_dispatcher "$REPO_ROOT" "$BIN" "$HOME_T/.local/share/loom" >/dev/null 2>&1 || true
# Same shell: PATH has the dispatcher's bin dir but NOT any repo's .loom/bin.
run_path="$BIN:/usr/bin:/bin"
assert_not_contains ":$run_path:" "/.loom/bin:" "'.loom/bin' is absent from the test PATH"
# bare 'loom status' -> the machine dispatcher
set +e
bare_out=$(cd "$CONSUMER" && PATH="$run_path" HOME="$HOME_T" LOOM_HOME="$CHK" loom status 2>&1)
set -e 2>/dev/null || true
assert_contains "$bare_out" "machine-level dispatcher" "bare 'loom status' resolves to the machine dispatcher"
# path-qualified './.loom/bin/loom status' -> the pool manager
pool_out=$(cd "$CONSUMER" && PATH="$run_path" ./.loom/bin/loom status 2>&1)
assert_contains "$pool_out" "POOL_MANAGER" "'./.loom/bin/loom status' resolves to the pool manager"
assert_not_contains "$pool_out" "machine-level dispatcher" "the two forms are disjoint (no shadowing)"

# ── #4053 contract + AC1 symlink + AC6 console-script invariant ──────────────
echo "Test 9: provisioning exposes verifiable globals and links the checkout (AC1, #4053)"
assert_eq "$PROVISIONED_DISPATCHER_BIN" "$BIN/loom" "PROVISIONED_DISPATCHER_BIN points at the installed dispatcher"
assert_eq "$PROVISIONED_LOOM_CHECKOUT" "$HOME_T/.local/share/loom" "PROVISIONED_LOOM_CHECKOUT points at the checkout"
[[ -x "$BIN/loom" ]] && pass "dispatcher installed and executable" || fail "dispatcher not installed/executable"
if [[ -L "$HOME_T/.local/share/loom" ]]; then
    tgt="$(readlink "$HOME_T/.local/share/loom")"
    assert_eq "$tgt" "$REPO_ROOT" "checkout is a symlink to the source checkout (cannot diverge)"
else
    fail "checkout was not established as a symlink"
fi
# Idempotent second run.
set +e
provision_loom_dispatcher "$REPO_ROOT" "$BIN" "$HOME_T/.local/share/loom" >/dev/null 2>&1
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "0" "provisioning is idempotent (second run returns 0)"

echo "Test 10: provisioning never touches the 23 loom-* console-script symlinks (AC6)"
BIN2=$(mktemp -d)
HOME2=$(mktemp -d)
# Seed 23 dummy console-script files, snapshot their checksums.
for i in $(seq 1 23); do echo "console-script-$i" > "$BIN2/loom-fake$i"; done
before=$(cd "$BIN2" && for f in loom-fake*; do printf '%s:%s\n' "$f" "$(cksum < "$f")"; done | sort)
source "$PROVISION_LIB"
provision_loom_dispatcher "$REPO_ROOT" "$BIN2" "$HOME2/.local/share/loom" >/dev/null 2>&1 || true
after=$(cd "$BIN2" && for f in loom-fake*; do printf '%s:%s\n' "$f" "$(cksum < "$f")"; done | sort)
assert_eq "$after" "$before" "all 23 loom-* entries are byte-identical before/after provisioning"
[[ -f "$BIN2/loom" ]] && pass "only the 'loom' dispatcher was added" || fail "dispatcher not added to bin dir"
count=$(cd "$BIN2" && ls loom-fake* 2>/dev/null | wc -l | tr -d ' ')
assert_eq "$count" "23" "console-script count unchanged (23)"

# ── Phase 3b (#4229): LOOM_MACHINE_CHECKOUT hand-off ─────────────────────────
echo "Test 11: start/stop/update all hand LOOM_MACHINE_CHECKOUT to their delegates"
out=$(cd "$CONSUMER" && LOOM_HOME="$CHK" bash "$DISPATCHER" start --machine 2>&1)
assert_contains "$out" "machine_checkout=[$CHK]" "'start --machine' hands the resolved checkout to loom-daemon-start.sh"

out=$(cd "$CONSUMER" && LOOM_HOME="$CHK" bash "$DISPATCHER" stop --machine 2>&1)
assert_contains "$out" "machine_checkout=[$CHK]" "'stop --machine' hands the resolved checkout to loom-daemon-stop.sh"

out=$(LOOM_HOME="$CHK" bash "$DISPATCHER" update --check 2>&1)
assert_contains "$out" "machine_checkout=[$CHK]" "'update' hands the resolved checkout to loom-daemon-update.sh (Gap 1)"

echo "Test 12: 'loom update' hands off the checkout from a NON-REPO directory too (Gap 1 regression)"
out=$(cd "$NR" && LOOM_HOME="$CHK" bash "$DISPATCHER" update --check 2>&1)
assert_contains "$out" "machine_checkout=[$CHK]" "'update' from a non-repo dir still resolves+hands off the machine checkout"
rc_check=0
(cd "$NR" && LOOM_HOME="$CHK" bash "$DISPATCHER" update --check >/dev/null 2>&1) || rc_check=$?
assert_eq "0" "$rc_check" "'update --check' from a non-repo dir does not refuse (no 'only works inside a Loom source checkout')"

# ── Phase 3b (#4229): the `restart` verb ─────────────────────────────────────
echo "Test 13: bare 'loom restart' inside a consumer repo disambiguates like start/stop (same collision guard)"
set +e
out=$(cd "$CONSUMER" && LOOM_HOME="$CHK" bash "$DISPATCHER" restart 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "3" "bare 'loom restart' in a repo exits 3 (disambiguating non-zero)"
assert_contains "$out" "loom restart --machine" "disambiguation names the machine surface for restart"
assert_not_contains "$out" "STUB_STOP" "bare 'loom restart' did NOT run any delegate (no silent surface)"
assert_not_contains "$out" "STUB_START" "bare 'loom restart' did NOT run any delegate (no silent surface)"

echo "Test 14: 'loom restart --machine' prefers the supervised drain-and-roll IPC when loom-daemon accepts it"
RESTART_BIN_OK=$(mktemp -d)
cat > "$RESTART_BIN_OK/loom-daemon" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "restart" ]] && exit 0
exit 1
EOF
chmod +x "$RESTART_BIN_OK/loom-daemon"
set +e
out=$(cd "$CONSUMER" && PATH="$RESTART_BIN_OK:/usr/bin:/bin" LOOM_HOME="$CHK" bash "$DISPATCHER" restart --machine 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "0" "'loom restart --machine' exits 0 when the supervised restart IPC succeeds"
assert_contains "$out" "restart scheduled" "reports the supervised drain-and-roll path"
assert_not_contains "$out" "STUB_STOP" "supervised restart path never falls back to stop"
assert_not_contains "$out" "STUB_START" "supervised restart path never falls back to start"

echo "Test 15: 'loom restart --machine' falls back to stop-then-start when the supervised IPC refuses"
RESTART_BIN_REFUSE=$(mktemp -d)
cat > "$RESTART_BIN_REFUSE/loom-daemon" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$RESTART_BIN_REFUSE/loom-daemon"
set +e
out=$(cd "$CONSUMER" && PATH="$RESTART_BIN_REFUSE:/usr/bin:/bin" LOOM_HOME="$CHK" bash "$DISPATCHER" restart --machine 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "0" "'loom restart --machine' falls back to stop+start and still exits 0"
assert_contains "$out" "STUB_STOP" "fallback invokes the stop delegate"
assert_contains "$out" "STUB_START" "fallback invokes the start delegate"

echo "Test 16: 'loom restart --machine' falls back to stop-then-start when loom-daemon isn't on PATH at all"
set +e
out=$(cd "$CONSUMER" && PATH="/usr/bin:/bin" LOOM_HOME="$CHK" bash "$DISPATCHER" restart --machine 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "0" "'loom restart --machine' with no loom-daemon on PATH falls back to stop+start"
assert_contains "$out" "STUB_STOP" "fallback invokes the stop delegate (no loom-daemon on PATH)"
assert_contains "$out" "STUB_START" "fallback invokes the start delegate (no loom-daemon on PATH)"

# ── #4581: bare-exec fallback harvests + re-exports the live plist/unit env ──
echo "Test 16b: 'loom restart --machine' harvests + re-exports the live systemd unit's LOOM_* env before the bare-exec fallback (#4581)"
CHK_HARVEST=$(make_checkout_with_harvest)
HOME_HARVEST=$(mktemp -d)
mkdir -p "$HOME_HARVEST/.config/systemd/user"
cat > "$HOME_HARVEST/.config/systemd/user/loom-daemon.service" <<'EOF'
[Service]
Environment=LOOM_DAEMON_SUPERVISOR=systemd
Environment=LOOM_WORK_FINDER=1
Environment=LOOM_MAIN_HEALTH_GATE=1
Environment=PATH=/opt/sentinel-4581/bin:/usr/bin:/bin
EOF
set +e
out=$(cd "$CONSUMER" && PATH="/usr/bin:/bin" HOME="$HOME_HARVEST" LOOM_HOME="$CHK_HARVEST" bash "$DISPATCHER" restart --machine 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "0" "harvest-and-preserve restart fallback still exits 0"
assert_contains "$out" "LOOM_WORK_FINDER=[1]" "harvested LOOM_WORK_FINDER reaches start_target's re-render (#4581)"
assert_contains "$out" "LOOM_MAIN_HEALTH_GATE=[1]" "harvested LOOM_MAIN_HEALTH_GATE reaches start_target's re-render (#4581)"
assert_contains "$out" "preserved 2 LOOM_*/token env var(s)" "dispatcher reports the harvested count"

echo "Test 16c: 'loom restart --machine' skips harvesting (no-op) when no plist/unit is installed yet (first-ever start, #4581)"
CHK_HARVEST_EMPTY=$(make_checkout_with_harvest)
HOME_EMPTY=$(mktemp -d)
set +e
out=$(cd "$CONSUMER" && PATH="/usr/bin:/bin" HOME="$HOME_EMPTY" LOOM_HOME="$CHK_HARVEST_EMPTY" bash "$DISPATCHER" restart --machine 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "0" "restart fallback with nothing installed yet still exits 0"
assert_contains "$out" "STUB_START" "fallback still reaches start_target with nothing to harvest"
assert_not_contains "$out" "preserved" "no harvest-preserved message when no plist/unit exists"

echo "Test 16d: 'loom restart --machine' aborts (exit 6) rather than falling back silently when a live plist exists but cannot be read (#4581, the #4011 class)"
CHK_HARVEST_BAD=$(make_checkout_with_harvest)
HOME_DARWIN=$(mktemp -d)
mkdir -p "$HOME_DARWIN/Library/LaunchAgents"
echo "not a real plist" > "$HOME_DARWIN/Library/LaunchAgents/com.rjwalters.loom-daemon.plist"
# Fake `uname` -> Darwin so the launchd-gated harvest branch fires
# deterministically on any host (mirrors test-loom-daemon-update.sh's
# write_fake_launchd_loaded_bin pattern). `plutil` is genuinely absent on this
# (Linux) test host, which is exactly the "cannot harvest" failure this test
# exercises — no plutil stub needed.
FAKE_UNAME_BIN=$(mktemp -d)
cat > "$FAKE_UNAME_BIN/uname" <<'EOF'
#!/usr/bin/env bash
echo "Darwin"
EOF
chmod +x "$FAKE_UNAME_BIN/uname"
set +e
out=$(cd "$CONSUMER" && PATH="$FAKE_UNAME_BIN:/usr/bin:/bin" HOME="$HOME_DARWIN" LOOM_HOME="$CHK_HARVEST_BAD" bash "$DISPATCHER" restart --machine 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "6" "restart fallback aborts (exit 6) when the live plist cannot be parsed"
assert_contains "$out" "Refusing to fall back" "abort message explains the refusal"
assert_not_contains "$out" "STUB_START" "aborted fallback never reaches start_target (no silently-narrowed relaunch)"

echo "Test 16e: 'loom restart --machine' still falls back cleanly when this checkout predates daemon-env-harvest.sh (#4581 backward-compat)"
CHK_NO_HARVEST_LIB=$(make_checkout)
set +e
out=$(cd "$CONSUMER" && PATH="/usr/bin:/bin" LOOM_HOME="$CHK_NO_HARVEST_LIB" bash "$DISPATCHER" restart --machine 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "0" "restart fallback exits 0 even when daemon-env-harvest.sh is absent from the checkout"
assert_contains "$out" "daemon-env-harvest.sh not found" "missing-lib warning is surfaced, not silently swallowed"
assert_contains "$out" "STUB_START" "fallback still reaches start_target when the harvest lib predates #4581"

echo "Test 17: 'restart' is documented in help output"
help_out=$(LOOM_HOME="$CHK" bash "$DISPATCHER" help 2>&1)
assert_contains "$help_out" "restart" "'loom help' documents the restart verb"

echo "Test 18: 'migrate' verb routing (Epic #3835 Phase 6, #4254)"
# The real checkout ships scripts/install/migrate-consumer.sh; stub it into the
# fake checkout so the dispatcher's target resolution finds it.
mkdir -p "$CHK/scripts/install"
cat > "$CHK/scripts/install/migrate-consumer.sh" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "--help" ]]; then echo "MIGRATE_HELP"; exit 0; fi
echo "MIGRATE args=[$*] machine_checkout=[${LOOM_MACHINE_CHECKOUT:-}]"
EOF
chmod +x "$CHK/scripts/install/migrate-consumer.sh"

assert_contains "$help_out" "migrate" "'loom help' documents the migrate verb"

# --help is reachable from a non-repo directory (no .loom/ context needed).
migrate_help=$(cd "$(mktemp -d)" && LOOM_HOME="$CHK" bash "$DISPATCHER" migrate --help 2>&1)
assert_contains "$migrate_help" "MIGRATE_HELP" "'loom migrate --help' delegates without a repo context"

# Outside a Loom repo, 'migrate' refuses with a clear message.
migrate_norepo=$(cd "$(mktemp -d)" && LOOM_HOME="$CHK" bash "$DISPATCHER" migrate 2>&1 || true)
assert_contains "$migrate_norepo" "must run inside a Loom consumer repo" "'loom migrate' refuses outside a repo"

# Inside a consumer repo, 'migrate' delegates with the repo root + machine checkout.
MIGREPO="$(make_consumer_repo)"
migrate_run=$(cd "$MIGREPO" && LOOM_HOME="$CHK" bash "$DISPATCHER" migrate --dry-run 2>&1 || true)
assert_contains "$migrate_run" "MIGRATE args=" "'loom migrate' delegates to migrate-consumer.sh"
assert_contains "$migrate_run" "machine_checkout=[$CHK]" "'loom migrate' hands off LOOM_MACHINE_CHECKOUT"

# ── #4480: `loom sweep` is runtime-neutral (routes through spawn-worker.sh) ───
echo "Test 19: 'loom sweep' default path (no env, no config) routes to spawn-claude.sh unchanged"
SWEEPREPO="$(make_sweep_repo)"
set +e
out=$(cd "$SWEEPREPO" && env -u LOOM_RUNTIME -u LOOM_WORKSPACE LOOM_CONFIG_DEFAULTS_FILE="" LOOM_HOME="$CHK" bash "$DISPATCHER" sweep 4467 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "0" "'loom sweep' default path exits 0"
assert_contains "$out" "STUB_CLAUDE" "default (no env/config) resolves to spawn-claude.sh"
assert_not_contains "$out" "STUB_CODEX" "default path does not touch the codex runner"
assert_contains "$out" 'argv=[-p /loom:sweep 4467 --dangerously-skip-permissions]' "default path forwards the same claude args as before (byte-for-byte)"

echo "Test 20: 'LOOM_RUNTIME=codex loom sweep' routes to spawn-codex.sh"
set +e
out=$(cd "$SWEEPREPO" && LOOM_RUNTIME=codex LOOM_CONFIG_DEFAULTS_FILE="" LOOM_HOME="$CHK" bash "$DISPATCHER" sweep 4467 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "0" "'LOOM_RUNTIME=codex loom sweep' exits 0"
assert_contains "$out" "STUB_CODEX" "LOOM_RUNTIME=codex resolves to spawn-codex.sh"
assert_not_contains "$out" "STUB_CLAUDE" "codex env path does not touch the claude runner"
assert_contains "$out" 'argv=[-p /loom:sweep 4467 --dangerously-skip-permissions]' "codex path forwards the same passthrough args"

if command -v jq >/dev/null 2>&1; then
    echo "Test 21: 'loom sweep' honors .loom/config.json runtimes.default (no env)"
    SWEEPCFG="$(make_sweep_repo)"
    echo '{"runtimes":{"default":"codex"}}' > "$SWEEPCFG/.loom/config.json"
    set +e
    out=$(cd "$SWEEPCFG" && env -u LOOM_RUNTIME -u LOOM_WORKSPACE LOOM_CONFIG_DEFAULTS_FILE="" LOOM_HOME="$CHK" bash "$DISPATCHER" sweep 4467 2>&1)
    rc=$?
    set -e 2>/dev/null || true
    assert_eq "$rc" "0" "'loom sweep' with runtimes.default=codex exits 0"
    assert_contains "$out" "STUB_CODEX" "runtimes.default=codex (no env) resolves to spawn-codex.sh"
    assert_not_contains "$out" "STUB_CLAUDE" "config-selected codex path does not touch the claude runner"
else
    echo "Test 21: SKIP (jq not on PATH)"
fi

echo "Test 22: 'loom sweep' with an unknown runtime exits 78 naming runtime, source, and runners present"
set +e
out=$(cd "$SWEEPREPO" && LOOM_RUNTIME=nonexistent LOOM_CONFIG_DEFAULTS_FILE="" LOOM_HOME="$CHK" bash "$DISPATCHER" sweep 4467 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "78" "unknown runtime exits 78 (EX_CONFIG)"
assert_contains "$out" "nonexistent" "error names the resolved runtime"
assert_contains "$out" "env (LOOM_RUNTIME)" "error names where the runtime was resolved from"
assert_contains "$out" "claude" "error lists the runners actually present on disk"
assert_not_contains "$out" "STUB_CLAUDE" "unknown runtime never falls through to a runner"

echo "Test 23: 'loom sweep' still refuses outside a Loom repo and with no issue arg (unchanged)"
NRSWEEP=$(mktemp -d)
set +e
out=$(cd "$NRSWEEP" && LOOM_HOME="$CHK" bash "$DISPATCHER" sweep 4467 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "1" "'loom sweep' outside a Loom repo exits 1"
assert_contains "$out" "must run inside a Loom repo" "'loom sweep' outside a repo keeps the existing message"

set +e
out=$(cd "$SWEEPREPO" && LOOM_HOME="$CHK" bash "$DISPATCHER" sweep 2>&1)
rc=$?
set -e 2>/dev/null || true
assert_eq "$rc" "1" "'loom sweep' with no issue arg exits 1"
assert_contains "$out" "usage: loom sweep" "'loom sweep' with no arg keeps the existing usage message"

echo "Test 24: 'loom sweep' no longer hardcodes a Claude-only prerequisite gate (#4480)"
src="$(cat "$DISPATCHER")"
assert_not_contains "$src" "'claude' CLI not found on PATH; cannot dispatch a sweep." "the Claude-only prereq gate was removed from sweep"

echo ""
echo "======================================"
echo "test-loom-dispatcher.sh: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
echo "======================================"
[[ "$TESTS_FAILED" -eq 0 ]]
