#!/usr/bin/env bash
# trust-stub.sh -- a `loom-daemon forge trusted-comments` stand-in for shell
# suites whose subject filters comments through the daemon's trust predicate
# (#9548, loom-daemon/src/comment_trust.rs). Source it, then call
# `loom_trust_stub <stub-dir>`: it writes <stub-dir>/loom-daemon and exports
# LOOM_DAEMON_BIN to it.
#
# The stub mirrors the real predicate for the shapes fixtures use: a repo
# insider by author_association, or this fleet's default App family spelled as
# an App (`loom-fleet-dispatch[bot]`). A fixture comment with NO author at all
# is read as the fleet's own, so fixtures written before #9548 keep modelling
# fleet comments; the real predicate never trusts a missing author, and each
# suite carries its own outsider case. Input is one array, concatenated arrays
# or NDJSON; output is one array.
#
# LOOM_TEST_NO_TRUST_VERB=1 simulates a binary predating the verb (clap exits 2
# with nothing on stdout).

loom_trust_stub() {
    local dir="${1:?loom_trust_stub: stub dir required}"
    cat > "$dir/loom-daemon" <<'STUB'
#!/usr/bin/env bash
[[ "${1:-} ${2:-}" == "forge trusted-comments" ]] || { echo "trust stub: unexpected loom-daemon $*" >&2; exit 64; }
[[ "${LOOM_TEST_NO_TRUST_VERB:-}" == "1" ]] && exit 2
exec jq -c -s '[.[] | if type == "array" then .[] else . end
  | select(((.user.login // null) == null and (.author_association // null) == null)
      or ((.author_association // "") | ascii_upcase | IN("OWNER", "MEMBER", "COLLABORATOR"))
      or ((.user.login // "") | test("^loom-fleet-dispatch(-[0-9]+)?\\[bot\\]$")))]'
STUB
    chmod +x "$dir/loom-daemon"
    export LOOM_DAEMON_BIN="$dir/loom-daemon"
}
