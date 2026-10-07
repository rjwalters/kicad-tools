#!/usr/bin/env bash
# trust-stub.sh -- a `loom-daemon` stand-in for shell suites whose subject
# calls the daemon's `forge` verbs. Source it, then call `loom_trust_stub
# <stub-dir>`: it writes <stub-dir>/loom-daemon and exports LOOM_DAEMON_BIN to
# it. Covers these verbs (plus `forge may-write`, `lease renewer` and
# `forge token`, documented at their branches):
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
# Every check-branch argv is appended to `check-branch-args.log` (#10027).
# `check-branch-legacy-rc`, when present, answers only a flagless call (the
# fence's old-daemon re-ask).

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
    [[ -n "$d" ]] && echo "$*" >> "$d/check-branch-args.log"
    if [[ -n "$d" && -f "$d/check-branch-rc" ]]; then
        rc="$(cat "$d/check-branch-rc")"
    fi
    # A pre-#10027 daemon's answer to the flagless legacy re-ask.
    if [[ -n "$d" && -f "$d/check-branch-legacy-rc" && "$*" != *--closed-pr-head* ]]; then
        rc="$(cat "$d/check-branch-legacy-rc")"
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
# #10229: `lease renewer` goes to a real daemon when LEASE_RENEWER_DAEMON names
# one; else claim grants --pid (or answers the live peer in `renewer-claim-pid`),
# check exits `renewer-check-rc` (default 0) and release succeeds. Every argv
# is appended to `renewer-args.log`.
if [[ "${1:-} ${2:-}" == "lease renewer" ]]; then
    [[ -n "${LEASE_RENEWER_DAEMON:-}" ]] && exec "$LEASE_RENEWER_DAEMON" "$@"
    d="${LOOM_TEST_STUB_DIR:-/dev/null/x}"
    echo "$*" >> "$d/renewer-args.log" 2> /dev/null
    case "${3:-}" in
        claim)
            [[ ! -f "$d/renewer-claim-pid" ]] || exec cat "$d/renewer-claim-pid"
            while [[ $# -gt 0 && "$1" != --pid ]]; do shift; done
            echo "${2:-}"
            ;;
        check) exit "$(cat "$d/renewer-check-rc" 2> /dev/null || echo 0)" ;;
        # #10203: a stub answers as a binary predating `sanitize-exec`, so
        # `start` skips its re-entry and stays fail-open.
        sanitize-exec) exit 2 ;;
    esac
    exit 0
fi
# #10229: `forge token --repo R --access A` answers not_configured unless
# `app-token` exists in LOOM_TEST_STUB_DIR, in which case the token is that
# file's content plus "-<access>" (so a gh stub can tell reader from writer).
# Every argv is appended to `forge-token-args.log`.
if [[ "${1:-} ${2:-}" == "forge token" ]]; then
    d="${LOOM_TEST_STUB_DIR:-/dev/null/x}"
    echo "$*" >> "$d/forge-token-args.log" 2> /dev/null
    access="write"
    while [[ $# -gt 0 ]]; do [[ "$1" != --access ]] || access="${2:-}"; shift; done
    [[ -f "$d/app-token" ]] || { echo '{"status":"not_configured","access":"'"$access"'"}'; exit 0; }
    echo '{"status":"ok","token":"'"$(cat "$d/app-token")-$access"'","access":"'"$access"'"}'
    exit 0
fi
echo "trust stub: unexpected loom-daemon $*" >&2
exit 64
STUB
    chmod +x "$dir/loom-daemon"
    export LOOM_DAEMON_BIN="$dir/loom-daemon"
}
