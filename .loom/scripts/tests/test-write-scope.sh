#!/usr/bin/env bash
# test-write-scope.sh — `loom_write_repo` (lib/forge-helpers.sh) and its
# callers refuse to write to a repository this installation does not manage
# (#9548).
#
# The main fixture is the shape behind #9548: a fork checkout whose `origin` is
# the fork and whose `upstream` is the project it was forked from. `gh`
# resolves such a checkout to `upstream` for every call without `--repo`, so an
# unguarded comment lands on the upstream project. The cases prove:
#
#   - the degraded path (no loom-daemon, or one predating `forge may-write`)
#     allows only a checkout whose one remote is origin, only to origin;
#   - the daemon verb's answer, when it gives one, always wins;
#   - end to end through post-verdict.sh and forge-helpers' comment wrapper,
#     a refused write never reaches `gh`, and an allowed one names the repo.
#
# Hermetic: stub `gh` and stub `loom-daemon` binaries, throwaway git repos.

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
HELPERS="$SCRIPTS_DIR/lib/forge-helpers.sh"

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo "  PASS: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo "  FAIL: $1"; [[ -n "${2:-}" ]] && echo "    $2"; }
check() { if [[ "$2" == "$3" ]]; then pass "$1"; else fail "$1" "expected '$2', got '$3'"; fi; }
contains() { if [[ "$2" == *"$3"* ]]; then pass "$1"; else fail "$1" "expected to find '$3' in: $2"; fi; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK" 2>/dev/null || true' EXIT
STUB="$WORK/bin"
mkdir -p "$STUB"

# Stub gh: log every invocation; answer the reads callers make before writing
# with empty-but-valid output.
cat > "$STUB/gh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
case "$1 $2" in
  "api graphql") echo '{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[]}}}}}' ;;
  "api "*) printf '' ;;
  "pr comment"|"issue comment") echo "https://github.com/x/y/pull/1#issuecomment-1" ;;
esac
exit 0
EOF
chmod +x "$STUB/gh"
export GH_LOG="$WORK/gh.log"
export PATH="$STUB:$PATH"

# Stub daemons, one per answer shape.
mk_daemon() { # <name> <body>
  printf '#!/usr/bin/env bash\n%s\n' "$2" > "$STUB/$1"
  chmod +x "$STUB/$1"
}
mk_daemon daemon-old 'echo "error: unrecognized subcommand '\''may-write'\''" >&2; exit 2'
mk_daemon daemon-deny 'echo "the credential in use cannot write to me/solo (repository role \`pull\`)" >&2; exit 1'
mk_daemon daemon-allow 'echo "me/widgets"; exit 0'
mk_daemon daemon-silent 'exit 0'
NO_DAEMON="$WORK/no-such-loom-daemon"

FORK="$WORK/fork"
git init -q "$FORK"
git -C "$FORK" remote add origin https://github.com/me/widgets.git
git -C "$FORK" remote add upstream https://github.com/acme/widgets.git
mkdir -p "$FORK/.loom"
SOLO="$WORK/solo"
git init -q "$SOLO"
git -C "$SOLO" remote add origin git@github.com:me/solo.git
mkdir -p "$SOLO/.loom"

run() { # <dir> <daemon> [repo] -> sets OUT, ERR, RC
  local dir="$1" d="$2"; shift 2
  OUT="$(cd "$dir" && export LOOM_DAEMON_BIN="$d" && source "$HELPERS" && loom_write_repo "$@" 2>"$WORK/err")"; RC=$?
  ERR="$(cat "$WORK/err")"
}

echo "== degraded path (no usable daemon verb) =="
run "$FORK" "$NO_DAEMON"
check "fork + upstream, no daemon: refused" 1 "$RC"
contains "the refusal says why" "$ERR" "only a checkout whose one remote is origin may write"
run "$FORK" "$NO_DAEMON" acme/widgets
check "an explicit upstream target is refused" 1 "$RC"
git -C "$FORK" config remote.origin.gh-resolved base
run "$FORK" "$NO_DAEMON"
check "even pinned to origin, a two-remote checkout is refused without the verb" 1 "$RC"
git -C "$FORK" config --unset remote.origin.gh-resolved
run "$FORK" "$STUB/daemon-old"
check "a daemon predating may-write degrades the same way" 1 "$RC"
run "$FORK" "$STUB/daemon-silent"
check "exit 0 without a repo on stdout is not an answer" 1 "$RC"

run "$SOLO" "$NO_DAEMON"
check "origin-only checkout, no daemon: allowed" 0 "$RC"
check "and the vetted repo is origin" "me/solo" "$OUT"
run "$SOLO" "$NO_DAEMON" acme/widgets
check "origin-only checkout, naming another repo: refused" 1 "$RC"
OUT="$(cd "$SOLO" && export GH_REPO=acme/widgets LOOM_DAEMON_BIN="$NO_DAEMON" && source "$HELPERS" && loom_write_repo 2>/dev/null)"; RC=$?
check "GH_REPO pointing away from origin: refused" 1 "$RC"

echo "== the daemon verb decides when it answers =="
run "$SOLO" "$STUB/daemon-deny"
check "a daemon refusal wins where the fallback would allow" 1 "$RC"
contains "the daemon's reason is passed through" "$ERR" "cannot write to me/solo"
run "$FORK" "$STUB/daemon-allow"
check "a daemon allowance is used" 0 "$RC"
check "with the repo it printed" "me/widgets" "$OUT"

echo "== callers never reach gh with a refused write =="
: > "$GH_LOG"
OUT="$(cd "$FORK" && LOOM_DAEMON_BIN="$NO_DAEMON" bash "$SCRIPTS_DIR/post-verdict.sh" 7 changes-requested \
  0123456789abcdef0123456789abcdef01234567 --body "needs work" 2>&1)"; RC=$?
check "post-verdict.sh refuses with exit 4" 4 "$RC"
if grep -q "pr comment" "$GH_LOG"; then fail "post-verdict.sh posted nothing" "$(cat "$GH_LOG")"; else pass "post-verdict.sh posted nothing"; fi

: > "$GH_LOG"
RC=0
(cd "$FORK" && export LOOM_DAEMON_BIN="$NO_DAEMON" && source "$HELPERS" && forge_gh_comment_rl_safe acme/widgets 7 "hello" 2>/dev/null) || RC=$?
check "forge_gh_comment_rl_safe refuses the upstream repo" 1 "$RC"
if grep -q "comment" "$GH_LOG"; then fail "the wrapper made no comment call" "$(cat "$GH_LOG")"; else pass "the wrapper made no comment call"; fi

: > "$GH_LOG"
OUT="$(cd "$FORK" && LOOM_DAEMON_BIN="$STUB/daemon-allow" bash "$SCRIPTS_DIR/post-verdict.sh" 7 changes-requested \
  0123456789abcdef0123456789abcdef01234567 --body "needs work" 2>&1)"; RC=$?
check "an allowed post-verdict.sh succeeds" 0 "$RC"
contains "and names the vetted repo on the write" "$(cat "$GH_LOG")" "issue comment 7 --repo me/widgets"

# Scripts vetted since the first review: a real run from the fork is refused
# before any write, and only the read that resolves the repo is made.
: > "$GH_LOG"
OUT="$(cd "$FORK" && LOOM_DAEMON_BIN="$NO_DAEMON" bash "$SCRIPTS_DIR/sync-labels.sh" 2>&1)"; RC=$?
check "sync-labels.sh refuses a real sync from the fork" 1 "$RC"
contains "and says why" "$OUT" "loom-daemon forge may-write refused the repo"
if grep -qE "^label (create|edit|delete)" "$GH_LOG"; then fail "sync-labels.sh wrote no label" "$(cat "$GH_LOG")"; else pass "sync-labels.sh wrote no label"; fi

: > "$GH_LOG"
OUT="$(cd "$FORK" && LOOM_DAEMON_BIN="$NO_DAEMON" bash "$SCRIPTS_DIR/clean-stale-building-labels.sh" 2>&1)"; RC=$?
check "clean-stale-building-labels.sh refuses a real run from the fork" 1 "$RC"
if grep -qE -- "-X DELETE|--remove-label" "$GH_LOG"; then fail "no label was stripped" "$(cat "$GH_LOG")"; else pass "no label was stripped"; fi

# The real verb, when a built binary is supplied (LOOM_TEST_DAEMON_BIN): the
# fork checkout is refused because gh would resolve it to upstream.
if [[ -n "${LOOM_TEST_DAEMON_BIN:-}" && -x "${LOOM_TEST_DAEMON_BIN}" ]] \
  && "$LOOM_TEST_DAEMON_BIN" forge may-write --help >/dev/null 2>&1; then
  echo "== the real loom-daemon forge may-write =="
  OUT="$(cd "$FORK" && "$LOOM_TEST_DAEMON_BIN" forge may-write 2>&1)"; RC=$?
  check "real verb: the fork checkout is refused" 1 "$RC"
  contains "real verb: because gh resolves it to upstream" "$OUT" "acme/widgets (via remote \`upstream\`), not its origin me/widgets"
fi

echo
echo "write-scope: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
