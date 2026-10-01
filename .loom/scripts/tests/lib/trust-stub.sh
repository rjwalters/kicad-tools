#!/usr/bin/env bash
# trust-stub.sh -- a `loom-daemon` stand-in for shell suites whose subject
# calls the daemon's `forge` verbs. Source it, then call `loom_trust_stub
# <stub-dir>`: it writes <stub-dir>/loom-daemon and exports LOOM_DAEMON_BIN to
# it. Covers two verbs:
#
# `forge trusted-comments` (#9548, loom-daemon/src/comment_trust.rs) -- the
# stub mirrors the real predicate for the shapes fixtures use: a repo insider
# by author_association, or this fleet's default App family spelled as an App
# (`loom-fleet-dispatch[bot]`). A fixture comment with NO author at all is
# read as the fleet's own, so fixtures written before #9548 keep modelling
# fleet comments; the real predicate never trusts a missing author, and each
# suite carries its own outsider case. Input is one array, concatenated
# arrays or NDJSON; output is one array.
#
# LOOM_TEST_NO_TRUST_VERB=1 simulates a binary predating the verb (clap exits 2
# with nothing on stdout).
#
# `forge check-branch <issue>` (#9453 Phase 4, loom-daemon/src/forge_check_branch.rs)
# -- sweep-lease-fence.sh's branch-collision hard stop. Defaults to the
# verified-absent answer (exit 1, empty stdout) so every suite that does not
# care about this leg is unaffected; a scenario that DOES care overrides it
# via two fixture files in `LOOM_TEST_STUB_DIR` (mirroring the gh stub's
# comments.json/comments-fail convention): `check-branch-rc` (the exit code
# to return) and `check-branch-stdout` (its stdout, e.g. a timestamp or SHA).

loom_trust_stub() {
    local dir="${1:?loom_trust_stub: stub dir required}"
    cat > "$dir/loom-daemon" <<'STUB'
#!/usr/bin/env bash
if [[ "${1:-} ${2:-}" == "forge trusted-comments" ]]; then
    [[ "${LOOM_TEST_NO_TRUST_VERB:-}" == "1" ]] && exit 2
    exec jq -c -s '[.[] | if type == "array" then .[] else . end
      | select(((.user.login // null) == null and (.author_association // null) == null)
          or ((.author_association // "") | ascii_upcase | IN("OWNER", "MEMBER", "COLLABORATOR"))
          or ((.user.login // "") | test("^loom-fleet-dispatch(-[0-9]+)?\\[bot\\]$")))]'
fi
if [[ "${1:-} ${2:-}" == "forge check-branch" ]]; then
    d="${LOOM_TEST_STUB_DIR:-}"
    rc=1
    if [[ -n "$d" && -f "$d/check-branch-rc" ]]; then
        rc="$(cat "$d/check-branch-rc")"
    fi
    if [[ -n "$d" && -f "$d/check-branch-stdout" ]]; then
        cat "$d/check-branch-stdout"
    fi
    exit "$rc"
fi
# #9548: `forge may-write` goes to a real daemon when WRITE_SCOPE_DAEMON names
# one (see write-scope-fixture.sh), else answers as a binary predating the
# verb, which sends loom_write_repo to its shell fallback.
if [[ "${1:-} ${2:-}" == "forge may-write" ]]; then
    [[ -n "${WRITE_SCOPE_DAEMON:-}" ]] && exec "$WRITE_SCOPE_DAEMON" "$@"
    echo "error: unrecognized subcommand 'may-write'" >&2
    exit 2
fi
echo "trust stub: unexpected loom-daemon $*" >&2
exit 64
STUB
    chmod +x "$dir/loom-daemon"
    export LOOM_DAEMON_BIN="$dir/loom-daemon"
}
