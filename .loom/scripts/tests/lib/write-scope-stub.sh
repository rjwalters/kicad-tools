#!/usr/bin/env bash
# write-scope-stub.sh — test helper: let a suite's fixture repo pass the #9548
# write-scope check, so the suite keeps testing what it tests.
#
# RETIRING (#9782): this replaces the real decision with an allow-all, which
# the operator ruled out for tests. New and converted suites use
# write-scope-fixture.sh, which registers the fixture so the real gate admits
# it. The six suites still sourcing this file are listed in #9782; it is
# deleted once they are converted. Do not add a new caller.
#
# Every Loom write path now vets its target with `loom_write_repo`
# (lib/forge-helpers.sh), which asks `loom-daemon forge may-write`. Suites use
# fixture repositories (`owner/repo`, `test-owner/test-repo`) that no real
# installation manages, with a stub `gh` the real verb's permission probe
# cannot read, and CI has no loom-daemon at all. So without this every fixture
# write is — correctly — refused, and whether it is depends on which daemon the
# host happens to have. This helper is how a suite says "the write-scope
# decision is not what I am testing"; test-write-scope.sh is the suite that
# tests it.
#
#   source "$TEST_DIR/lib/write-scope-stub.sh"
#   write_scope_allow_all "$STUB_DIR"
#
# It writes "$STUB_DIR/loom-daemon-write-scope" and exports LOOM_DAEMON_BIN at
# it. That wrapper answers `forge may-write [--repo R]` with R, or with
# $WRITE_SCOPE_STUB_REPO, or with what the (stubbed) `gh repo view` names when
# no repo is given (a suite that counts gh calls sets the variable), and hands
# every other
# invocation on: to the LOOM_DAEMON_BIN in force when this ran, else to
# whatever `loom-daemon` is first on PATH *at call time* (so a suite's own PATH
# stub, installed before or after this call, still answers), else exit 127.

write_scope_allow_all() {
  local dir="$1" inner="${LOOM_DAEMON_BIN:-}"
  cat > "$dir/loom-daemon-write-scope" <<EOF
#!/usr/bin/env bash
if [[ "\${1:-} \${2:-}" == "forge may-write" ]]; then
  if [[ "\${3:-}" == "--repo" ]]; then echo "\$4"; exit 0; fi
  if [[ -n "\${WRITE_SCOPE_STUB_REPO:-}" ]]; then echo "\$WRITE_SCOPE_STUB_REPO"; exit 0; fi
  nwo="\$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null | sed -E 's/.*"nameWithOwner" *: *"([^"]+)".*/\\1/')"
  echo "\${nwo:-owner/repo}"; exit 0
fi
inner="$inner"
[[ -n "\$inner" ]] || inner="\$(command -v loom-daemon 2>/dev/null)"
[[ -n "\$inner" ]] || exit 127
exec "\$inner" "\$@"
EOF
  chmod +x "$dir/loom-daemon-write-scope"
  WRITE_SCOPE_STUB_PREV_BIN="$inner"
  export LOOM_DAEMON_BIN="$dir/loom-daemon-write-scope"
}

# write_scope_unwrap: restore LOOM_DAEMON_BIN to what it was before
# write_scope_allow_all, e.g. before tests/lib/require-daemon-bin.sh pins a
# real binary (it prefers LOOM_DAEMON_BIN, and the wrapper is not one).
write_scope_unwrap() {
  if [[ -n "${WRITE_SCOPE_STUB_PREV_BIN:-}" ]]; then
    export LOOM_DAEMON_BIN="$WRITE_SCOPE_STUB_PREV_BIN"
  else
    unset LOOM_DAEMON_BIN
  fi
}
