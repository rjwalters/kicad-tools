#!/usr/bin/env bash
# test-token-model-class.sh — per-model-class token selection and bad-marking
# (issue #8058).
#
# Split out of test-spawn-claude.sh rather than appended to it: that file is
# over scripts/check-file-size-budget.sh's threshold and therefore frozen at its
# current size, and #8058 is a self-contained behaviour with its own fixtures.
#
# Style matches test-spawn-claude.sh — plain bash, hand-rolled assertions.
# Bats is NOT used in this repository.
#
# Usage:
#   ./.loom/scripts/tests/test-token-model-class.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULTS_DIR="$(cd "$SCRIPTS_DIR/.." && pwd)"
REPO_ROOT="$(cd "$DEFAULTS_DIR/.." && pwd)"
WRAPPER="$SCRIPTS_DIR/claude-wrapper.sh"

# Resolve THIS checkout's own `loom-daemon` binary and pin it into every
# invocation below, so the assertions never exercise a host-level install that
# happens to be on PATH. Same rationale as test-spawn-claude.sh's block.
DAEMON_BIN=""
for _candidate in \
    "$REPO_ROOT/target/release/loom-daemon" \
    "$REPO_ROOT/target/debug/loom-daemon"; do
    if [[ -x "$_candidate" ]]; then
        DAEMON_BIN="$_candidate"
        break
    fi
done

# Pin the #5979 concurrent-sweep divisor so spawn-claude.sh's CPU-budget block
# never shells out to a live daemon on the host.
export LOOM_SWEEP_INFLIGHT_SWEEPS=1

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local needle="$1" haystack="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected to contain: '$needle'"
        echo "    In: '$haystack'"
    fi
}

assert_not_contains() {
    local needle="$1" haystack="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" != *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected NOT to contain: '$needle'"
        echo "    In: '$haystack'"
    fi
}

echo "==================================="
echo "test-token-model-class.sh (#8058)"
echo "==================================="

# A stub `claude` on PATH for every spawn-claude.sh invocation below.
STUB_DIR="$(mktemp -d)"
cat > "$STUB_DIR/claude" <<'STUB'
#!/usr/bin/env bash
case " $* " in
  *" -p "*) ;;
  *) exit 0 ;;
esac
echo "stub-claude success on token=${CLAUDE_CODE_OAUTH_TOKEN}"
exit 0
STUB
chmod +x "$STUB_DIR/claude"
trap 'rm -rf "$STUB_DIR"' EXIT

# ============================================================
# Section 10: per-model-class token selection (issue #8058)
# ============================================================
#
# An account that hit its Opus ceiling is still usable for Sonnet work, so
# spawn-claude.sh tells `tokens select` which model class the spawn will
# actually run. Two things are asserted here, and both matter:
#   * the flag reaches the selector's argv when the daemon advertises it, and
#   * it is OMITTED (and the spawn still succeeds) when the daemon does not —
#     the capability probe is the only thing standing between a mid-roll
#     daemon binary and a hard argument-parse failure on every spawn.

echo ""
echo "Testing spawn-claude.sh per-model-class token selection (#8058)..."

MC_WS="$(mktemp -d)"
mkdir -p "$MC_WS/.loom/tokens"
chmod 700 "$MC_WS/.loom/tokens"
echo -n "fake-token" > "$MC_WS/.loom/tokens/only.token"
chmod 600 "$MC_WS/.loom/tokens/only.token"

MC_ARGV_LOG="$(mktemp)"
MC_STUB_DIR="$(mktemp -d)"

# Stub 1: forwards everything to the real daemon (which DOES advertise
# --model), logging the `tokens select` argv it was handed.
cat > "$MC_STUB_DIR/loom-daemon" <<STUB
#!/usr/bin/env bash
if [[ "\$1" == "tokens" && "\$2" == "select" && "\$3" != "--help" ]]; then
    printf '%s\n' "\$*" >> "$MC_ARGV_LOG"
fi
exec "$DAEMON_BIN" "\$@"
STUB
chmod +x "$MC_STUB_DIR/loom-daemon"

# Stub 2: an "old" daemon whose `tokens select --help` does NOT mention
# --model, and which hard-fails if it is ever passed one — exactly how a
# pre-#8058 binary behaves under clap.
MC_OLD_STUB_DIR="$(mktemp -d)"
cat > "$MC_OLD_STUB_DIR/loom-daemon" <<STUB
#!/usr/bin/env bash
if [[ "\$1" == "tokens" && "\$2" == "select" ]]; then
    if [[ "\$3" == "--help" ]]; then
        # Deliberately advertises --auto-unpin but NOT --model.
        printf 'Usage: loom-daemon tokens select [OPTIONS]\n\n  --workspace <PATH>\n  --provider <PROVIDER>\n  --export\n  --no-key\n  --auto-unpin\n'
        exit 0
    fi
    printf '%s\n' "\$*" >> "$MC_ARGV_LOG"
    for _a in "\$@"; do
        if [[ "\$_a" == "--model" ]]; then
            echo "error: unexpected argument '--model' found" >&2
            exit 2
        fi
    done
fi
exec "$DAEMON_BIN" "\$@"
STUB
chmod +x "$MC_OLD_STUB_DIR/loom-daemon"

# LOOM_MODEL set + daemon advertises --model -> the flag is in the argv, once.
: > "$MC_ARGV_LOG"
LOOM_WORKSPACE="$MC_WS" LOOM_DAEMON_BIN="$MC_STUB_DIR/loom-daemon" \
    PATH="$STUB_DIR:$PATH" LOOM_MODEL="claude-opus-5" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" >/dev/null 2>&1 || true
argv="$(cat "$MC_ARGV_LOG")"
assert_contains "--model claude-opus-5" "$argv" \
    "LOOM_MODEL reaches 'tokens select --model' when the daemon advertises it (#8058)"
mc_model_count="$(printf '%s\n' "$argv" | grep -c -- '--model claude-opus-5' || true)"
assert_eq "1" "$mc_model_count" \
    "--model appears exactly once in the tokens select argv (#8058)"

# An explicit --model arg wins over LOOM_MODEL for selection too — the pool
# must be asked about the class the child will actually run.
: > "$MC_ARGV_LOG"
LOOM_WORKSPACE="$MC_WS" LOOM_DAEMON_BIN="$MC_STUB_DIR/loom-daemon" \
    PATH="$STUB_DIR:$PATH" LOOM_MODEL="claude-opus-5" \
    "$SCRIPTS_DIR/spawn-claude.sh" --model claude-sonnet-4-6 -p "ping" >/dev/null 2>&1 || true
argv="$(cat "$MC_ARGV_LOG")"
assert_contains "--model claude-sonnet-4-6" "$argv" \
    "an explicit --model arg wins over LOOM_MODEL for tokens select too (#8058)"
assert_not_contains "--model claude-opus-5" "$argv" \
    "the losing LOOM_MODEL value never reaches tokens select (#8058)"

# No LOOM_MODEL and no --model arg -> no --model in the selection argv
# (session default; selection stays account-wide, identical to pre-#8058).
: > "$MC_ARGV_LOG"
LOOM_WORKSPACE="$MC_WS" LOOM_DAEMON_BIN="$MC_STUB_DIR/loom-daemon" \
    PATH="$STUB_DIR:$PATH" LOOM_MODEL="" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" >/dev/null 2>&1 || true
argv="$(cat "$MC_ARGV_LOG")"
assert_not_contains "--model" "$argv" \
    "no resolved model emits NO --model in the tokens select argv (#8058)"

# Old daemon (no --model in --help) -> the flag is omitted and the spawn still
# succeeds. This is the whole point of the capability probe.
: > "$MC_ARGV_LOG"
set +e
mc_old_out="$(LOOM_WORKSPACE="$MC_WS" LOOM_DAEMON_BIN="$MC_OLD_STUB_DIR/loom-daemon" \
    PATH="$STUB_DIR:$PATH" LOOM_MODEL="claude-opus-5" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1)"
mc_old_rc=$?
set -e
argv="$(cat "$MC_ARGV_LOG")"
assert_not_contains "--model" "$argv" \
    "a daemon binary that does not advertise --model gets NO --model (capability probe, #8058)"
assert_eq "0" "$mc_old_rc" \
    "spawn still succeeds against a daemon binary that predates --model (#8058)"
assert_contains "stub-claude" "$mc_old_out" \
    "the child still launches on a pre---model daemon binary (#8058)"

# --- claude-wrapper.sh: a class-scoped death writes a class-scoped mark ---
#
# The other half of #8058. A `MODEL_CREDITS_EXHAUSTED` death is definitionally
# scoped to one model tier, so it must NOT bad-mark the whole account — while
# an account-wide weekly limit, with exactly the same LOOM_MODEL in flight,
# still must. That asymmetry is the whole fix, so both directions are asserted.

echo ""
echo "Testing claude-wrapper.sh class-scoped bad marks (#8058)..."

if [[ -z "$DAEMON_BIN" ]]; then
    echo "  (skipping class-scoped mark tests — loom-daemon binary not found)"
else
  run_class_mark_case() {
      # $1 = death text emitted by the stub on tok-alpha
      # $2 = workspace dir (already seeded with alpha/beta tokens)
      # $3 = stub dir
      local death_text="$1" ws="$2" stub="$3"
      cat > "$stub/claude" <<STUB
#!/usr/bin/env bash
case " \$* " in
  *" -p "*) ;;
  *) exit 0 ;;
esac
if [[ "\${CLAUDE_CODE_OAUTH_TOKEN}" == "tok-alpha" ]]; then
    echo "${death_text}"
    exit 1
fi
echo "stub-claude success on token=\${CLAUDE_CODE_OAUTH_TOKEN}"
exit 0
STUB
      chmod +x "$stub/claude"
      set +e
      LOOM_WORKSPACE="$ws" \
      LOOM_TOKEN_NAME="alpha" \
      CLAUDE_CODE_OAUTH_TOKEN="tok-alpha" \
      LOOM_DAEMON_BIN="$DAEMON_BIN" \
      LOOM_MODEL="claude-opus-5" \
      LOOM_MAX_RETRIES=1 \
      LOOM_SHEPHERD_TASK_ID="test-class-mark" \
      LOOM_STARTUP_MONITOR_WINDOW=1 \
      PATH="$stub:$PATH" \
      bash "$WRAPPER" -p "ping" >/dev/null 2>&1
      set -e
      cat "$ws/.loom/tokens/.bad_tokens" 2>/dev/null || true
  }

  seed_class_mark_pool() {
      local ws="$1"
      mkdir -p "$ws/.loom/tokens"
      chmod 700 "$ws/.loom/tokens"
      printf '%s' "tok-alpha" > "$ws/.loom/tokens/alpha.token"
      printf '%s' "tok-beta"  > "$ws/.loom/tokens/beta.token"
      chmod 600 "$ws/.loom/tokens/"*.token
  }

  # Case A: per-model-tier credit exhaustion -> class-scoped mark.
  CM_WS="$(mktemp -d)"
  CM_STUB="$(mktemp -d)"
  seed_class_mark_pool "$CM_WS"
  cm_bad="$(run_class_mark_case \
      "You're out of usage credits. Run /usage-credits or switch models with /model." \
      "$CM_WS" "$CM_STUB")"
  # The wrapper has no classifier of its own, so it writes the RAW resolved
  # model into the marker; `tokens_pool::bad_tokens` normalizes it through
  # `model_tiers::task_alias_of` on read (claude-opus-5 -> opus).
  assert_contains "[model-class:claude-opus-5]" "$cm_bad" \
      "a MODEL_CREDITS_EXHAUSTED death under LOOM_MODEL=claude-opus-5 writes a class-scoped mark (#8058)"
  assert_contains "alpha" "$cm_bad" \
      "the class-scoped mark names the account that died (#8058)"

  # Take the healthy sibling out of the pool so selection has exactly ONE
  # candidate. With two accounts the random tier could pick beta either way and
  # the assertion would prove nothing; with one, the sonnet/opus disagreement IS
  # the fix.
  rm -f "$CM_WS/.loom/tokens/beta.token"
  cm_sel="$("$DAEMON_BIN" tokens select --workspace "$CM_WS" --model claude-sonnet-4-6 \
      --export --no-key 2>/dev/null || true)"
  assert_contains "selected=alpha" "$cm_sel" \
      "an opus-marked account is still selected for sonnet work (#8058)"

  set +e
  "$DAEMON_BIN" tokens select --workspace "$CM_WS" --model claude-opus-5 \
      --export --no-key >/dev/null 2>&1
  cm_opus_rc=$?
  set -e
  assert_eq "78" "$cm_opus_rc" \
      "the same account is NOT selected for opus — the class it died on (#8058)"

  # Case B: an account-wide weekly limit, SAME LOOM_MODEL -> class-less mark.
  # The #4501 ceiling regex matches "reached your weekly limit" too, so this is
  # the case that would mis-scope if the scoping predicate were the bare regex.
  CM_WS2="$(mktemp -d)"
  CM_STUB2="$(mktemp -d)"
  seed_class_mark_pool "$CM_WS2"
  cm_bad2="$(run_class_mark_case \
      "You've reached your weekly limit. Your limit will reset later." \
      "$CM_WS2" "$CM_STUB2")"
  assert_not_contains "model-class" "$cm_bad2" \
      "an account-wide weekly limit writes a CLASS-LESS mark even with LOOM_MODEL set (#8058)"

  rm -f "$CM_WS2/.loom/tokens/beta.token"
  set +e
  "$DAEMON_BIN" tokens select --workspace "$CM_WS2" --model claude-sonnet-4-6 \
      --export --no-key >/dev/null 2>&1
  cm_sonnet_rc=$?
  set -e
  assert_eq "78" "$cm_sonnet_rc" \
      "a class-less mark still blocks every class, sonnet included (#8058)"

  cleanup_class_mark_fixtures() {
      rm -rf "$CM_WS" "$CM_STUB" "$CM_WS2" "$CM_STUB2"
  }
  cleanup_class_mark_fixtures
fi

cleanup_model_class_fixtures() {
    rm -rf "$MC_WS" "$MC_STUB_DIR" "$MC_OLD_STUB_DIR"
    rm -f "$MC_ARGV_LOG"
}
cleanup_model_class_fixtures

# ============================================================
# Summary
# ============================================================

echo ""
echo "==================================="
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "All tests passed."
