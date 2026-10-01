#!/usr/bin/env bash
# test-create-pr-provenance.sh — create-pr.sh appends exactly one hidden
# `<!-- loom:provenance v1 ... -->` line to the PR body (#9027, D33).
#
# Hermetic: `gh` and the daemon are stubs on PATH / $LOOM_DAEMON_SELF_BIN. The
# marker's *content* is `loom-daemon provenance pr-marker`'s, covered by the
# Rust tests (loom-daemon/tests/provenance_trailers.rs); this suite pins only
# create-pr.sh's side: pass-through, the all-`unknown` fallback (a daemon
# without the subcommand), no duplicate on a re-run, and the argv it hands the daemon.
#
# Usage:
#   ./.loom/scripts/tests/test-create-pr-provenance.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/write-scope-fixture.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/write-scope-fixture.sh"
CREATE_PR="$(cd "$SCRIPT_DIR/.." && pwd)/create-pr.sh"

TESTS_RUN=0
TESTS_FAILED=0

assert_eq() {
  local expected="$1" actual="$2" msg="$3"
  TESTS_RUN=$((TESTS_RUN + 1))
  if [[ "$expected" == "$actual" ]]; then
    echo "  PASS: $msg"
  else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo "  FAIL: $msg"
    echo "    Expected: '$expected'"
    echo "    Actual:   '$actual'"
  fi
}

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR"' EXIT

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
if [[ "$1" == "pr" && "$2" == "list" ]]; then exit 0; fi
if [[ "$1" == "issue" && "$2" == "view" ]]; then echo "OPEN"; exit 0; fi
if [[ "$1" == "pr" && "$2" == "create" ]]; then
  args=("$@")
  for ((i = 0; i < ${#args[@]}; i++)); do
    if [[ "${args[i]}" == "--body" ]]; then
      printf '%s' "${args[i + 1]}" > "$LOOM_TEST_STUB_DIR/body.txt"
    fi
  done
  echo "https://github.com/owner/repo/pull/9999"
  exit 0
fi
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

MARKER='<!-- loom:provenance v1 build=0.0.1 abc clean prompts=unknown unknown sweep=s-1 story=o/r#42 trace=unknown host=h base=unknown run=none -->'
FALLBACK='<!-- loom:provenance v1 build=unknown unknown unknown prompts=unknown unknown sweep=unknown story=unknown trace=unknown host=unknown base=unknown run=unknown -->'

cat > "$STUB_DIR/daemon-ok" <<STUB
#!/usr/bin/env bash
# create-pr.sh also consults the daemon for the #9453 phase 5 1:1
# review-gate guard (\`forge check-open-pr <issue>\`) before it ever gets to
# the provenance call this suite is pinning -- log that invocation
# separately so it never shows up in daemon-args.txt / daemon-stdin.txt,
# which stay scoped to the \`provenance pr-marker\` call this suite tests.
# Exit 1 (verified no open PR) so the review-gate guard never refuses.
if [[ "\$1" == "forge" && "\$2" == "check-open-pr" ]]; then
  printf '%s\n' "\$*" > "\$LOOM_TEST_STUB_DIR/daemon-forge-args.txt"
  exit 1
fi
printf '%s\n' "\$*" > "\$LOOM_TEST_STUB_DIR/daemon-args.txt"
cat > "\$LOOM_TEST_STUB_DIR/daemon-stdin.txt"
echo '$MARKER'
STUB
cat > "$STUB_DIR/daemon-old" <<'STUB'
#!/usr/bin/env bash
echo "error: unrecognized subcommand 'provenance'" >&2
exit 2
STUB
cat > "$STUB_DIR/version-check-ok.sh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$STUB_DIR/daemon-ok" "$STUB_DIR/daemon-old" "$STUB_DIR/version-check-ok.sh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"
export LOOM_FORGE_TYPE=github
export LOOM_VERSION_CHECK_SCRIPT="$STUB_DIR/version-check-ok.sh"

# #9548: create-pr.sh vets its write target through the write scope before it
# writes. It runs from a checkout registered as owner/repo (origin, .loom/, push
# reported to the permission probe), so the real decision admits it.
write_scope_register "$STUB_DIR/checkout" owner/repo
cd "$STUB_DIR/checkout"

run_create_pr() {
  rm -f "$STUB_DIR/body.txt" "$STUB_DIR/daemon-args.txt" "$STUB_DIR/daemon-forge-args.txt"
  "$CREATE_PR" --title "fix: x" --head "feature/issue-42" "$@" > /dev/null 2>&1
}

last_line() { tail -n1 "$STUB_DIR/body.txt"; }
marker_count() { grep -c '<!-- loom:provenance ' "$STUB_DIR/body.txt" || true; }

echo "Testing create-pr.sh provenance marker (#9027)..."

# T1: the daemon's marker is appended as the body's last line, once.
LOOM_DAEMON_SELF_BIN="$STUB_DIR/daemon-ok" run_create_pr --body "Closes #42" --base main
assert_eq "$MARKER" "$(last_line)" "T1: daemon marker is the body's last line"
assert_eq "1" "$(marker_count)" "T1: exactly one marker"
assert_eq "Closes #42" "$(head -n1 "$STUB_DIR/body.txt")" "T1: original body preserved"
assert_eq "provenance pr-marker --body-file - --base-ref origin/main" \
  "$(cat "$STUB_DIR/daemon-args.txt")" "T1: base ref is passed through"
assert_eq "Closes #42" "$(cat "$STUB_DIR/daemon-stdin.txt")" \
  "T1: the body goes to the daemon, which derives the D32 story from it"

# T2: a daemon without the subcommand -> the explicit all-unknown record.
LOOM_DAEMON_SELF_BIN="$STUB_DIR/daemon-old" run_create_pr --body "Closes #42"
assert_eq "$FALLBACK" "$(last_line)" "T2: old daemon -> all-unknown record, never omitted"
assert_eq "1" "$(marker_count)" "T2: exactly one marker"

# T3: a body that already carries a record (re-run) is not stamped twice.
LOOM_DAEMON_SELF_BIN="$STUB_DIR/daemon-ok" run_create_pr --body "Closes #42

$FALLBACK"
assert_eq "1" "$(marker_count)" "T3: existing record is not duplicated"
assert_eq "" "$(cat "$STUB_DIR/daemon-args.txt" 2>/dev/null || true)" "T3: daemon not consulted"

# T4: prose that merely quotes the marker mid-line is not a record.
LOOM_DAEMON_SELF_BIN="$STUB_DIR/daemon-ok" run_create_pr --body "Closes #42
Appends a \`<!-- loom:provenance v1 ... -->\` line."
assert_eq "$MARKER" "$(last_line)" "T4: a quoted marker in prose still gets a record"

echo ""
echo "Tests run: $TESTS_RUN, failed: $TESTS_FAILED"
[[ "$TESTS_FAILED" -eq 0 ]]
