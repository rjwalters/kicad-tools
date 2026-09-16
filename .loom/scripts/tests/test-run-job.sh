#!/usr/bin/env bash
# test-run-job.sh — tests for the `run-job` seam (epic #6896 Phase 4, #7853):
# `run-job.sh` (client) + `lib/run-job-exec.sh` (executor).
#
# Style matches the sibling suites — plain bash, hand-rolled assertions. Bats
# is NOT used in this repository.
#
# Everything here runs against a FAKE docker (and, for the ssh transport, a
# fake ssh that really executes the piped executor program locally), so the
# suite is hermetic and needs no docker daemon, no ssh daemon and no network.
#
# Usage:
#   ./.loom/scripts/tests/test-run-job.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

RUN_JOB="$SCRIPTS_DIR/run-job.sh"
EXEC_LIB="$SCRIPTS_DIR/lib/run-job-exec.sh"

if [[ ! -f "$RUN_JOB" || ! -f "$EXEC_LIB" ]]; then
    echo "SKIP: run-job seam not found (run-job.sh / lib/run-job-exec.sh)" >&2
    exit 0
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq is required by the run-job seam and is not installed" >&2
    exit 0
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}
fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
    [[ -n "${2:-}" ]] && echo "        $2"
}
assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3" "expected '$1', got '$2'"; fi
}
assert_contains() {
    if [[ "$1" == *"$2"* ]]; then pass "$3"; else fail "$3" "expected to contain '$2'; got: $(head -c 400 <<<"$1")"; fi
}
assert_not_contains() {
    if [[ "$1" != *"$2"* ]]; then pass "$3"; else fail "$3" "expected NOT to contain '$2'; got: $(head -c 400 <<<"$1")"; fi
}

TMP_ROOT="$(mktemp -d)"
# Canonicalized on purpose: the seam refuses a symlinked mount source (it would
# break path parity), and on macOS `mktemp -d` hands back a `/var/...` path
# that is a symlink to `/private/var/...` — an uncanonicalized scratch path
# would make every "a real directory is still accepted" case fail for the
# wrong reason. Same treatment as docker/worker/test-run-job.sh.
TMP_ROOT="$(cd "$TMP_ROOT" && pwd -P)"
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

# --- Fake docker -------------------------------------------------------------
# State lives under $FAKE_DOCKER_STATE: one file per "container". Behaviour is
# driven by FAKE_JOB_* env vars set per test case.
FAKE_BIN="$TMP_ROOT/bin"
mkdir -p "$FAKE_BIN"
cat >"$FAKE_BIN/docker" <<'FAKE_DOCKER'
#!/usr/bin/env bash
set -uo pipefail
STATE="${FAKE_DOCKER_STATE:?}"
LOG="${FAKE_DOCKER_LOG:?}"
mkdir -p "$STATE"
printf '%s\n' "$*" >>"$LOG"

verb="${1:-}"; shift || true
case "$verb" in
  info) exit "${FAKE_DOCKER_INFO_RC:-0}" ;;
  run)
    name=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --name) name="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    [[ -n "$name" ]] || { echo "fake docker: no --name" >&2; exit 125; }
    if [[ "${FAKE_DOCKER_RUN_RC:-0}" != "0" ]]; then
      echo "fake docker: run refused" >&2
      exit "${FAKE_DOCKER_RUN_RC}"
    fi
    printf 'running\n' >"$STATE/$name"
    echo "deadbeefcafe"
    ;;
  logs)
    name="${!#}"
    [[ -f "$STATE/$name" ]] || { echo "No such container: $name" >&2; exit 1; }
    [[ -n "${FAKE_JOB_STDOUT:-}" ]] && printf '%s\n' "$FAKE_JOB_STDOUT"
    [[ -n "${FAKE_JOB_STDERR:-}" ]] && printf '%s\n' "$FAKE_JOB_STDERR" >&2
    true
    ;;
  wait)
    name="${1:-}"
    [[ -f "$STATE/$name" ]] || { echo "No such container: $name" >&2; exit 1; }
    sleep "${FAKE_JOB_WAIT_SLEEP:-0}"
    printf 'exited %s\n' "${FAKE_JOB_EXIT:-0}" >"$STATE/$name"
    echo "${FAKE_JOB_EXIT:-0}"
    ;;
  inspect)
    # Widens the window in which the executor is inside a `_rj_docker …`
    # FUNCTION call that redirects the shell's own stderr, so a test can land a
    # signal there deterministically (see section 9b).
    sleep "${FAKE_DOCKER_INSPECT_SLEEP:-0}"
    fmt=""
    if [[ "${1:-}" == "--format" ]]; then fmt="$2"; shift 2; fi
    name="${1:-}"
    [[ -f "$STATE/$name" ]] || exit 1
    read -r status code <"$STATE/$name"
    case "$fmt" in
      *State.Status*) echo "$status" ;;
      *State.ExitCode*) echo "${code:-0}" ;;
      *) echo "{}" ;;
    esac
    ;;
  rm)
    name="${!#}"; rm -f "$STATE/$name" ;;
  stop)
    name="${!#}"; printf 'exited 143\n' >"$STATE/$name" ;;
  kill)
    name="${!#}"; printf 'exited 137\n' >"$STATE/$name" ;;
  *) echo "fake docker: unhandled verb '$verb'" >&2; exit 125 ;;
esac
FAKE_DOCKER
chmod +x "$FAKE_BIN/docker"

# --- Fake ssh ----------------------------------------------------------------
# Really executes the piped program locally with `bash -s`, so the ssh
# transport is exercised end to end (program on stdin, verb+args in argv,
# stdout/stderr/exit code relayed) without an sshd.
cat >"$FAKE_BIN/ssh" <<'FAKE_SSH'
#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "$*" >>"${FAKE_SSH_LOG:?}"
if [[ "${FAKE_SSH_FAIL:-0}" == "1" ]]; then
  echo "ssh: connect to host ${FAKE_SSH_HOST:-somewhere} port 22: Connection refused" >&2
  exit 255
fi
# Drop ssh options and the target, keep the remote command + args.
args=("$@")
i=0
while [[ $i -lt ${#args[@]} ]]; do
  case "${args[$i]}" in
    -o | -p | -i | -l | -F | -J) i=$((i + 2)) ;; # options that take a value
    -*) i=$((i + 1)) ;;
    *) break ;;
  esac
done
i=$((i + 1))   # skip the [user@]host target
remote=("${args[@]:$i}")
tee "${FAKE_SSH_STDIN_CAPTURE:-/dev/null}" | "${remote[@]}"
FAKE_SSH
chmod +x "$FAKE_BIN/ssh"

export FAKE_DOCKER_STATE="$TMP_ROOT/state"
export FAKE_DOCKER_LOG="$TMP_ROOT/docker.log"
export FAKE_SSH_LOG="$TMP_ROOT/ssh.log"
export PATH="$FAKE_BIN:$PATH"
# Pin the seam away from any host config/daemon: no config tiers, no real
# docker, no real ssh.
export LOOM_WORKSPACE="$TMP_ROOT/workspace"
mkdir -p "$LOOM_WORKSPACE/.loom"
echo '{}' >"$LOOM_WORKSPACE/.loom/config.json"
export LOOM_CONFIG_DEFAULTS_FILE=""

reset_state() {
    rm -rf "$FAKE_DOCKER_STATE"
    mkdir -p "$FAKE_DOCKER_STATE"
    : >"$FAKE_DOCKER_LOG"
    : >"$FAKE_SSH_LOG"
    unset FAKE_JOB_EXIT FAKE_JOB_STDOUT FAKE_JOB_STDERR FAKE_JOB_WAIT_SLEEP
    unset FAKE_DOCKER_RUN_RC FAKE_DOCKER_INFO_RC FAKE_DOCKER_INSPECT_SLEEP FAKE_SSH_FAIL
}

echo ""
echo "=== 1. Job spec normalization (--print-spec) ==="
reset_state
spec="$("$RUN_JOB" --print-spec --id job-alpha --image alpine:3 \
    --mount /srv/work --mount /srv/ro:ro --workdir /srv/work \
    --env FOO=bar --cpus 2 --memory 4g --timeout 90 -- echo hello world 2>/dev/null)"
assert_eq "loom.run-job/v1" "$(jq -r .schema <<<"$spec")" "spec carries the versioned schema id"
assert_eq "job-alpha" "$(jq -r .id <<<"$spec")" "spec id honours --id"
assert_eq "alpine:3" "$(jq -r .image <<<"$spec")" "spec image honours --image"
assert_eq '["echo","hello","world"]' "$(jq -c .command <<<"$spec")" "command captured as an argv array"
assert_eq "rw" "$(jq -r '.mounts[0].mode' <<<"$spec")" "mount defaults to rw"
assert_eq "ro" "$(jq -r '.mounts[1].mode' <<<"$spec")" "':ro' suffix parsed as a read-only mount"
assert_eq "bar" "$(jq -r '.env.FOO' <<<"$spec")" "--env captured"
assert_eq "2" "$(jq -r '.limits.cpus' <<<"$spec")" "--cpus captured under limits"
assert_eq "4g" "$(jq -r '.limits.memory' <<<"$spec")" "--memory captured under limits"
assert_eq "90" "$(jq -r '.timeoutSeconds' <<<"$spec")" "--timeout captured"
assert_eq "none" "$(jq -r '.network' <<<"$spec")" "network defaults to none"

echo ""
echo "=== 2. Spec validation rejects unsafe / malformed specs (exit 78) ==="
reset_state
out="$("$RUN_JOB" --dry-run --image alpine -- true 2>&1)"
rc=$?
assert_eq "0" "$rc" "a minimal valid spec passes validation"

out="$("$RUN_JOB" --dry-run -- true 2>&1)"
assert_eq "78" "$?" "missing image is EX_CONFIG"
assert_contains "$out" "image is required" "missing image names the field"

out="$("$RUN_JOB" --dry-run --image alpine 2>&1)"
assert_eq "78" "$?" "empty command is EX_CONFIG"
assert_contains "$out" "command must not be empty" "empty command names the field"

out="$("$RUN_JOB" --dry-run --image alpine --mount relative/path -- true 2>&1)"
assert_eq "78" "$?" "relative mount path is EX_CONFIG"
assert_contains "$out" "mount path must be absolute" "relative mount rejected by parity rule"

out="$("$RUN_JOB" --dry-run --image alpine --mount /srv/../etc -- true 2>&1)"
assert_eq "78" "$?" "mount path containing '..' is EX_CONFIG"

out="$("$RUN_JOB" --dry-run --image alpine --workdir relative -- true 2>&1)"
assert_eq "78" "$?" "relative workdir is EX_CONFIG"

out="$("$RUN_JOB" --dry-run --image alpine --network host -- true 2>&1)"
assert_eq "78" "$?" "host networking is refused"
assert_contains "$out" "host networking is not offered" "host network refusal is explicit"

out="$("$RUN_JOB" --dry-run --image alpine --memory lots -- true 2>&1)"
assert_eq "78" "$?" "malformed memory limit is EX_CONFIG"

out="$("$RUN_JOB" --dry-run --image alpine --cpus many -- true 2>&1)"
assert_eq "78" "$?" "malformed cpu limit is EX_CONFIG"

echo ""
echo "=== 3. THE security criterion: no docker socket can traverse the seam ==="
reset_state
for sock in /var/run/docker.sock /run/docker.sock /home/loom/docker.sock; do
    out="$("$RUN_JOB" --dry-run --image alpine --mount "$sock" -- true 2>&1)"
    assert_eq "78" "$?" "refuses to mount $sock"
    assert_contains "$out" "host-root-equivalent" "refusal for $sock cites the security rationale"
done
out="$("$RUN_JOB" --dry-run --image alpine --mount /run/podman/podman.sock -- true 2>&1)"
assert_eq "78" "$?" "refuses a podman socket mount too"
out="$("$RUN_JOB" --dry-run --image alpine --mount /proc -- true 2>&1)"
assert_eq "78" "$?" "refuses a /proc mount"

# ...NOR by mounting an ANCESTOR of the socket's directory (#7875 review).
# `/run` is docker.sock's real, non-symlink home on every mainstream distro and
# matches none of the socket-name patterns, yet a bind mount of it carries the
# socket along. This is refused by string against the canonical socket list,
# so it holds on the client pre-flight too, and whether or not the socket (or
# even the directory) exists on the host running this test.
for anc in /run /run/ /var /var/run /run/podman /run/containerd /var/run/crio; do
    out="$("$RUN_JOB" --dry-run --image alpine --mount "$anc" -- true 2>&1)"
    assert_eq "78" "$?" "refuses to mount $anc: an ancestor of a container-runtime socket"
    assert_contains "$out" "is an ancestor of container-runtime socket path" "refusal for $anc says why an innocent-looking directory is refused"
    assert_contains "$out" "host-root-equivalent" "refusal for $anc cites the security rationale"
done
argv="$("$RUN_JOB" --dry-run --image alpine --mount /run -- true 2>/dev/null || true)"
assert_not_contains "$argv" "/run:/run" "no docker argv is produced for the ancestor-directory mount"

# ...AND the ancestor rule cannot be spelled around (#7896). The ancestor check
# is a PREFIX match, so `/run//`, `//run`, `/run/.` and `/run/./` all name
# `/run` while matching no prefix of `/run/docker.sock`. The executor collapses
# them with `readlink -f` and catches them anyway, but the CLIENT pre-flight
# cannot resolve a path that does not exist on the client host — on macOS
# `--mount /run//` used to sail through pre-flight and emit `-v /run//:/run//`.
# Every case below therefore runs on the client, on THIS host, whether or not
# `/run` exists here.
for spell in '/run//' '//run' '/run/.' '/run/./' '/var//' '/var/run//' '//run/./'; do
    out="$("$RUN_JOB" --dry-run --image alpine --mount "$spell" -- true 2>&1)"
    assert_eq "78" "$?" "refuses the non-canonical spelling $spell (client pre-flight, no filesystem resolution needed)"
    assert_contains "$out" "must be in canonical form" "refusal for $spell names the spelling as the cause"
    # A non-canonical spelling is not a symlink: the refusal must not say it is.
    assert_not_contains "$out" "is a symlink" "refusal for $spell does not misattribute the spelling to a symlink"
    argv="$("$RUN_JOB" --dry-run --image alpine --mount "$spell" -- true 2>/dev/null || true)"
    assert_not_contains "$argv" "$spell:" "no docker argv is produced for the non-canonical mount $spell"
done

# The executor re-checks the spelling rule on its own, as it does every other
# mount rule — a client that skipped its pre-flight gains nothing by it.
nc_evil="$(jq -nc '{schema:"loom.run-job/v1", id:"job-noncanon", image:"alpine",
                    command:["true"], workdir:"", network:"none",
                    mounts:[{path:"/run//", mode:"ro"}],
                    env:{}, limits:{cpus:"",memory:""}, timeoutSeconds:0}')"
out="$(bash "$EXEC_LIB" run "$(printf '%s' "$nc_evil" | base64 | tr -d '\n')" 2>&1)"
assert_eq "78" "$?" "executor independently rejects a non-canonically spelled mount spec"
assert_contains "$out" "must be in canonical form" "executor-side spelling refusal is explicit"
assert_not_contains "$out" "is a symlink" "executor-side spelling refusal does not call /run// a symlink"
assert_not_contains "$(cat "$FAKE_DOCKER_LOG")" "/run//" "no docker run was issued for the non-canonical spec"

# A single trailing slash stays legal on an ordinary path: `/srv/work/` and
# `/srv/work` name the same directory, and `readlink -f` returns the latter, so
# the resolved-vs-given comparison used to refuse the slash as "is a symlink
# to" — naming a cause that was not the real one.
TRAILDIR="$TMP_ROOT/trailing"
mkdir -p "$TRAILDIR"
out="$("$RUN_JOB" --dry-run --image alpine --mount "$TRAILDIR/" -- true 2>&1)"
assert_eq "0" "$?" "a real directory with one trailing slash is still accepted"
assert_not_contains "$out" "is a symlink" "a trailing slash is never reported as a symlink"

# The executor re-checks the ancestor rule on its own: a client that skipped
# its pre-flight (or lied) still cannot get `-v /run:/run` past it.
anc_evil="$(jq -nc '{schema:"loom.run-job/v1", id:"job-ancestor", image:"alpine",
                     command:["true"], workdir:"", network:"none",
                     mounts:[{path:"/run", mode:"ro"}],
                     env:{}, limits:{cpus:"",memory:""}, timeoutSeconds:0}')"
out="$(bash "$EXEC_LIB" run "$(printf '%s' "$anc_evil" | base64 | tr -d '\n')" 2>&1)"
assert_eq "78" "$?" "executor independently rejects an ancestor-directory mount spec"
assert_contains "$out" "is an ancestor of container-runtime socket path" "executor-side ancestor refusal is explicit"
assert_not_contains "$(cat "$FAKE_DOCKER_LOG")" "/run:/run" "no docker run was issued for the ancestor-directory spec"

# ...NOR by mounting a directory that holds a live socket the canonical list
# cannot know about (a daemon started on `-H unix:///srv/x.sock`, rootless
# docker under /run/user/<uid>): the validator walks the source for live
# sockets on the host that will do the mounting. Needs a real unix socket on
# disk, which bash cannot make on its own — python3 or perl, whichever is here.
LIVEDIR="$TMP_ROOT/live"
mkdir -p "$LIVEDIR/nested"
make_unix_socket() {
    python3 -c 'import socket, sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])' "$1" 2>/dev/null \
        || perl -e 'use Socket; socket(S, PF_UNIX, SOCK_STREAM, 0) or exit 1; bind(S, sockaddr_un($ARGV[0])) or exit 1' "$1" 2>/dev/null
}
if make_unix_socket "$LIVEDIR/nested/d.sock" && [[ -S "$LIVEDIR/nested/d.sock" ]]; then
    out="$("$RUN_JOB" --dry-run --image alpine --mount "$LIVEDIR" -- true 2>&1)"
    assert_eq "78" "$?" "refuses a directory that contains a live unix socket under an innocent name"
    assert_contains "$out" "live unix socket" "the live-socket refusal says what it found"
    assert_contains "$out" "nested/d.sock" "...and names the socket it found"
    out="$("$RUN_JOB" --dry-run --image alpine --mount "$LIVEDIR/nested/d.sock" -- true 2>&1)"
    assert_eq "78" "$?" "refuses a live unix socket itself, whatever it is called"
    live_evil="$(jq -nc --arg p "$LIVEDIR" \
        '{schema:"loom.run-job/v1", id:"job-livesock", image:"alpine",
          command:["true"], workdir:"", network:"none",
          mounts:[{path:$p, mode:"rw"}],
          env:{}, limits:{cpus:"",memory:""}, timeoutSeconds:0}')"
    out="$(bash "$EXEC_LIB" run "$(printf '%s' "$live_evil" | base64 | tr -d '\n')" 2>&1)"
    assert_eq "78" "$?" "executor independently rejects a directory holding a live socket"
    assert_not_contains "$(cat "$FAKE_DOCKER_LOG")" "$LIVEDIR" "no docker run was issued for the live-socket directory"
    rm -f "$LIVEDIR/nested/d.sock"
    out="$("$RUN_JOB" --dry-run --image alpine --mount "$LIVEDIR" -- true 2>&1)"
    assert_eq "0" "$?" "the same directory is accepted again once the socket is gone (a live walk, not a name check)"

    # The live-socket walk is SKIPPED once a name-based check has already
    # refused the path (#7896): the spec is rejected either way, and `find`ing
    # a tree the caller was told it may not name (`/proc`, `/sys`, `/run`, or
    # the host root) is pure executor cost. Observable hermetically: a path
    # refused by the socket-NAME pattern that also happens to be a live socket
    # reports the name refusal only.
    SKIPDIR="$TMP_ROOT/skipwalk"
    mkdir -p "$SKIPDIR"
    if make_unix_socket "$SKIPDIR/docker.sock" && [[ -S "$SKIPDIR/docker.sock" ]]; then
        out="$("$RUN_JOB" --dry-run --image alpine --mount "$SKIPDIR/docker.sock" -- true 2>&1)"
        assert_eq "78" "$?" "a name-refused path that is also a live socket is still rejected"
        assert_contains "$out" "refusing docker/container-runtime socket mount" "the name-based refusal is the one reported"
        assert_not_contains "$out" "live unix socket" "the live-socket walk is skipped once a name check has already refused the path"
        rm -f "$SKIPDIR/docker.sock"
    fi
else
    echo "  (skip: neither python3 nor perl could create a unix socket here; live-socket walk not exercised)"
fi

# ...AND the refusal cannot be walked around with a symlink whose own name is
# innocent (#7875). Docker resolves a bind mount's SOURCE on the executor host
# at run time, so matching the literal path string alone was never a boundary:
# an agent inside a worker container can drop this link into any rw parity
# mount it already holds and the real socket would land in the job container.
SYMDIR="$TMP_ROOT/symlinks"
mkdir -p "$SYMDIR"
ln -sf /var/run/docker.sock "$SYMDIR/innocent.sock"
out="$("$RUN_JOB" --dry-run --image alpine --mount "$SYMDIR/innocent.sock" -- true 2>&1)"
assert_eq "78" "$?" "refuses a symlink that POINTS AT a docker socket, despite its innocent name"
assert_contains "$out" "is a symlink to" "the symlink refusal names the real target"
assert_contains "$out" "host-root-equivalent" "the resolved path is still matched against the socket refusal"
argv="$("$RUN_JOB" --dry-run --image alpine --mount "$SYMDIR/innocent.sock" -- true 2>/dev/null || true)"
assert_not_contains "$argv" "innocent.sock" "no docker argv is produced for the smuggled socket mount"

# Parity, not just sockets: ANY symlinked mount source is refused, because the
# executor would bind the resolved path while the spec promises the given one.
mkdir -p "$SYMDIR/real-target"
ln -sf "$SYMDIR/real-target" "$SYMDIR/link-to-dir"
out="$("$RUN_JOB" --dry-run --image alpine --mount "$SYMDIR/link-to-dir" -- true 2>&1)"
assert_eq "78" "$?" "refuses a symlinked mount source even when its target is harmless (path parity)"
assert_contains "$out" "pass \"$SYMDIR/real-target\" explicitly instead" "the refusal tells the caller the resolvable fix"

# A plain, real directory is of course still accepted — the check must not
# turn every legitimate mount into a rejection.
out="$("$RUN_JOB" --dry-run --image alpine --mount "$SYMDIR/real-target" -- true 2>&1)"
assert_eq "0" "$?" "a real (non-symlinked) mount source is still accepted"

# The executor re-checks on its OWN host, which is the only place resolution
# means anything: a client that never resolved (or lied) is still refused.
sym_evil="$(jq -nc --arg p "$SYMDIR/innocent.sock" \
    '{schema:"loom.run-job/v1", id:"job-symevil", image:"alpine",
      command:["true"], workdir:"", network:"none",
      mounts:[{path:$p, mode:"rw"}],
      env:{}, limits:{cpus:"",memory:""}, timeoutSeconds:0}')"
out="$(bash "$EXEC_LIB" run "$(printf '%s' "$sym_evil" | base64 | tr -d '\n')" 2>&1)"
assert_eq "78" "$?" "executor independently rejects a symlinked socket mount"
assert_contains "$out" "host-root-equivalent" "executor-side symlink refusal cites the security rationale"
assert_not_contains "$(cat "$FAKE_DOCKER_LOG")" "innocent.sock" "no docker run was issued for the smuggled socket mount"

# The executor re-validates independently: a client that skips its own
# pre-flight (or lies) still cannot talk the executor into a socket mount.
evil="$(jq -nc '{schema:"loom.run-job/v1", id:"job-evil", image:"alpine",
                 command:["true"], workdir:"", network:"none",
                 mounts:[{path:"/var/run/docker.sock", mode:"rw"}],
                 env:{}, limits:{cpus:"",memory:""}, timeoutSeconds:0}')"
out="$(bash "$EXEC_LIB" run "$(printf '%s' "$evil" | base64 | tr -d '\n')" 2>&1)"
assert_eq "78" "$?" "executor independently rejects a socket-mount spec"
assert_contains "$out" "refusing docker/container-runtime socket mount" "executor-side refusal is explicit"
assert_not_contains "$(cat "$FAKE_DOCKER_LOG")" "docker.sock" "no docker run was issued for the rejected spec"

# And the generated argv never contains a socket mount or a privilege escalation.
argv="$("$RUN_JOB" --dry-run --image alpine --mount /srv/work -- true 2>/dev/null)"
assert_not_contains "$argv" "docker.sock" "generated argv contains no docker socket"
assert_not_contains "$argv" "--privileged" "generated argv is never privileged"
assert_not_contains "$argv" "--cap-add" "generated argv adds no capabilities"

echo ""
echo "=== 4. --dry-run prints the real docker argv (parity mounts, limits) ==="
reset_state
argv="$("$RUN_JOB" --dry-run --id job-argv --image alpine:3 \
    --mount /srv/work --mount /srv/ro:ro --workdir /srv/work \
    --env FOO=bar --cpus 2 --memory 4g -- bash -lc 'echo hi' 2>/dev/null)"
assert_contains "$argv" "--detach" "job container is started detached (restart safety)"
assert_not_contains "$argv" "--rm" "job container is NOT --rm (exit code must survive a restart)"
assert_contains "$argv" "loom-job-job-argv" "container name is derived from the job id"
assert_contains "$argv" "loom.job.id=job-argv" "container carries a loom.job.id label"
assert_contains "$argv" "/srv/work:/srv/work" "rw mount uses an identical-path (parity) bind"
assert_contains "$argv" "/srv/ro:/srv/ro:ro" "ro mount uses an identical-path (parity) bind, read-only"
assert_contains "$argv" "FOO=bar" "env var forwarded"
assert_contains "$(tr '\n' ' ' <<<"$argv")" "--cpus 2" "--cpus forwarded to docker"
assert_contains "$(tr '\n' ' ' <<<"$argv")" "--memory 4g" "--memory forwarded to docker"
assert_contains "$(tr '\n' ' ' <<<"$argv")" "--network none" "--network forwarded to docker"
assert_contains "$(tr '\n' ' ' <<<"$argv")" "-w /srv/work" "workdir forwarded to docker"
assert_contains "$argv" "alpine:3" "image appears in the argv"

echo ""
echo "=== 5. Local executor: exit-code + log passthrough (success) ==="
reset_state
export FAKE_JOB_EXIT=0
export FAKE_JOB_STDOUT="job stdout line"
export FAKE_JOB_STDERR="job stderr line"
outfile="$TMP_ROOT/out.5"
errfile="$TMP_ROOT/err.5"
"$RUN_JOB" --executor local --id job-ok --image alpine -- true >"$outfile" 2>"$errfile"
rc=$?
assert_eq "0" "$rc" "successful job exits 0"
assert_contains "$(cat "$outfile")" "job stdout line" "job stdout passed through to the caller's stdout"
assert_contains "$(cat "$errfile")" "job stderr line" "job stderr passed through to the caller's stderr"
assert_contains "$(cat "$errfile")" "# LOOM_RUN_JOB_EXIT id=job-ok code=0" "exit sentinel emitted"
assert_contains "$(cat "$FAKE_DOCKER_LOG")" "rm --force loom-job-job-ok" "container removed once its exit code was read"

echo ""
echo "=== 6. Local executor: exit-code passthrough (failure) ==="
reset_state
export FAKE_JOB_EXIT=42
export FAKE_JOB_STDERR="boom"
errfile="$TMP_ROOT/err.6"
"$RUN_JOB" --executor local --id job-fail --image alpine -- false 2>"$errfile"
assert_eq "42" "$?" "failing job's exit code is passed through verbatim"
assert_contains "$(cat "$errfile")" "boom" "failing job's stderr is passed through"

reset_state
export FAKE_JOB_EXIT=255
"$RUN_JOB" --executor local --id job-255 --image alpine -- false >/dev/null 2>&1
assert_eq "255" "$?" "a job exiting 255 is reported as 255 (not confused with an ssh failure)"

echo ""
echo "=== 7. SSH executor: transport shape + end-to-end passthrough ==="
reset_state
export FAKE_JOB_EXIT=7
export FAKE_JOB_STDOUT="remote stdout"
export FAKE_SSH_STDIN_CAPTURE="$TMP_ROOT/ssh-stdin"
outfile="$TMP_ROOT/out.7"
errfile="$TMP_ROOT/err.7"
LOOM_JOB_EXECUTOR_HOST=executor.example LOOM_JOB_EXECUTOR_USER=loom \
    "$RUN_JOB" --executor ssh --id job-ssh --image alpine -- true >"$outfile" 2>"$errfile"
assert_eq "7" "$?" "ssh executor passes the remote job's exit code through"
assert_contains "$(cat "$outfile")" "remote stdout" "ssh executor passes remote stdout through"
assert_contains "$(cat "$FAKE_SSH_LOG")" "loom@executor.example" "ssh target honours host/user resolution"
assert_contains "$(cat "$FAKE_SSH_LOG")" "BatchMode=yes" "default ssh options are applied"
assert_contains "$(cat "$FAKE_SSH_LOG")" "bash -s --" "executor program is fed to a remote 'bash -s'"
assert_contains "$(cat "$FAKE_SSH_LOG")" "run " "verb + base64 spec travel in argv"
assert_contains "$(cat "$FAKE_SSH_STDIN_CAPTURE")" "LOOM_RUN_JOB_EXIT" "the executor program itself is piped on stdin (no remote Loom install needed)"
unset FAKE_SSH_STDIN_CAPTURE

echo ""
echo "=== 8. Transport failure is EX_UNAVAILABLE, never a fake job exit code ==="
reset_state
export FAKE_SSH_FAIL=1
errfile="$TMP_ROOT/err.8"
LOOM_JOB_EXECUTOR_HOST=down.example "$RUN_JOB" --executor ssh --id job-down --image alpine -- true 2>"$errfile"
assert_eq "69" "$?" "unreachable executor exits 69 (EX_UNAVAILABLE), not 255"
assert_contains "$(cat "$errfile")" "is unreachable" "transport failure is named as such"
assert_contains "$(cat "$errfile")" "did NOT run" "transport failure makes clear the job never ran"
unset FAKE_SSH_FAIL

echo ""
echo "=== 9. Restart safety: a drain signal detaches, it never kills the job ==="
reset_state
export FAKE_JOB_EXIT=5
export FAKE_JOB_WAIT_SLEEP=30
errfile="$TMP_ROOT/err.9"
spec="$(jq -nc '{schema:"loom.run-job/v1", id:"job-drain", image:"alpine",
                 command:["sleep","300"], workdir:"", network:"none", mounts:[],
                 env:{}, limits:{cpus:"",memory:""}, timeoutSeconds:0}')"
bash "$EXEC_LIB" run "$(printf '%s' "$spec" | base64 | tr -d '\n')" >/dev/null 2>"$errfile" &
exec_pid=$!
# Wait for the container to exist, then send the drain signal. The budget is
# deliberately generous (30s, not 5s): this suite runs inside run-ci-suites.sh's
# parallel pool, and on a loaded host a 5s cap can expire before the executor
# has even installed its trap — which lands the signal on bash's DEFAULT
# handler and turns a real assertion into a 143-exit flake.
for _ in $(seq 1 300); do
    [[ -f "$FAKE_DOCKER_STATE/loom-job-job-drain" ]] && break
    sleep 0.1
done
kill -TERM "$exec_pid" 2>/dev/null
wait "$exec_pid"
rc=$?
assert_eq "75" "$rc" "a drain SIGTERM detaches (EX_TEMPFAIL), it does not fail the job"
assert_contains "$(cat "$errfile")" "# LOOM_RUN_JOB_DETACHED id=job-drain" "detach is announced machine-readably"
docker_log="$(cat "$FAKE_DOCKER_LOG")"
assert_not_contains "$docker_log" "kill loom-job-job-drain" "teardown never SIGKILLs the in-flight job"
assert_not_contains "$docker_log" "stop " "teardown never even stops the in-flight job"
assert_not_contains "$docker_log" "rm --force loom-job-job-drain" "teardown never removes the in-flight container"
if [[ -f "$FAKE_DOCKER_STATE/loom-job-job-drain" ]]; then
    pass "the job container is still in flight after the drain"
else
    fail "the job container is still in flight after the drain" "container state file is gone"
fi

echo ""
echo "=== 9b. The detach marker survives a signal that lands mid-redirection ==="
# REGRESSION GUARD (#7875). `_rj_docker` is a shell FUNCTION, so
# `_rj_docker inspect "$name" >/dev/null 2>&1` redirects THIS SHELL's fd 2 to
# /dev/null for the duration of the call. A drain SIGTERM landing inside that
# window used to run the trap with stderr still pointing at /dev/null, so
# `# LOOM_RUN_JOB_DETACHED` was written and discarded — and with the executor's
# own diagnostics suppressed by the same redirection, run-job.sh saw nothing at
# all and reported "the executor is unreachable — the job did NOT run" (69) for
# a job that was alive and well on the executor host.
#
# The fake docker's `inspect` sleeps here purely to make that window wide
# enough to hit on purpose, instead of ~1-in-50 by luck.
#
# NOTE: deliberately no `reset_state` — section 10 reattaches to the container
# section 9 left in flight, so the fake docker's state must survive. Only the
# call log is cleared (its section 9 assertions have already run), and this
# case uses its own job id.
: >"$FAKE_DOCKER_LOG"
export FAKE_JOB_EXIT=5
export FAKE_JOB_WAIT_SLEEP=30
export FAKE_DOCKER_INSPECT_SLEEP=4
errfile="$TMP_ROOT/err.9b"
spec="$(jq -nc '{schema:"loom.run-job/v1", id:"job-redir", image:"alpine",
                 command:["sleep","300"], workdir:"", network:"none", mounts:[],
                 env:{}, limits:{cpus:"",memory:""}, timeoutSeconds:0}')"
bash "$EXEC_LIB" run "$(printf '%s' "$spec" | base64 | tr -d '\n')" >/dev/null 2>"$errfile" &
exec_pid=$!
# Wait for the container, then for the executor to be INSIDE the slow inspect.
for _ in $(seq 1 300); do
    [[ -f "$FAKE_DOCKER_STATE/loom-job-job-redir" ]] && break
    sleep 0.1
done
for _ in $(seq 1 300); do
    grep -q '^inspect ' "$FAKE_DOCKER_LOG" && break
    sleep 0.1
done
sleep 0.5
kill -TERM "$exec_pid" 2>/dev/null
wait "$exec_pid"
rc=$?
assert_eq "75" "$rc" "a signal landing mid-redirection still detaches (EX_TEMPFAIL)"
assert_contains "$(cat "$errfile")" "# LOOM_RUN_JOB_DETACHED id=job-redir" "the detach marker is NOT swallowed by a function-call redirection"
unset FAKE_DOCKER_INSPECT_SLEEP FAKE_JOB_WAIT_SLEEP

echo ""
echo "=== 10. Reattach after a restart recovers logs AND the exit code ==="
export FAKE_JOB_WAIT_SLEEP=0
export FAKE_JOB_STDOUT="output survived the restart"
outfile="$TMP_ROOT/out.10"
errfile="$TMP_ROOT/err.10"
"$RUN_JOB" --executor local attach job-drain >"$outfile" 2>"$errfile"
assert_eq "5" "$?" "reattach returns the in-flight job's real exit code"
assert_contains "$(cat "$outfile")" "output survived the restart" "reattach replays the job's output from its first line"
assert_contains "$(cat "$errfile")" "# LOOM_RUN_JOB_EXIT id=job-drain code=5" "reattach emits the exit sentinel"

echo ""
echo "=== 11. status / cancel verbs ==="
reset_state
printf 'running\n' >"$FAKE_DOCKER_STATE/loom-job-job-st"
out="$("$RUN_JOB" --executor local status job-st 2>/dev/null)"
assert_contains "$out" "state=running" "status reports a running job"
out="$("$RUN_JOB" --executor local status job-absent 2>/dev/null)"
assert_contains "$out" "state=absent" "status reports an unknown job as absent"

errfile="$TMP_ROOT/err.11"
"$RUN_JOB" --executor local cancel job-st 2>"$errfile"
assert_eq "0" "$?" "cancel of a live job succeeds"
assert_contains "$(cat "$errfile")" "# LOOM_RUN_JOB_CANCELLED id=job-st" "cancel is announced"
docker_log="$(cat "$FAKE_DOCKER_LOG")"
assert_contains "$docker_log" "stop --time" "cancel stops the job GRACEFULLY (SIGTERM + grace)"
assert_not_contains "$docker_log" "kill loom-job-job-st" "cancel never uses docker kill"

echo ""
echo "=== 12. Executor resolution: auto picks local when docker is reachable ==="
reset_state
export FAKE_JOB_EXIT=0
errfile="$TMP_ROOT/err.12"
"$RUN_JOB" --id job-auto --image alpine -- true >/dev/null 2>"$errfile"
assert_eq "0" "$?" "auto-resolved executor runs the job"
assert_contains "$(cat "$errfile")" "mode=local" "auto resolves to the local executor when docker is reachable"
assert_contains "$(cat "$errfile")" "auto:local" "the resolution path is logged"

reset_state
export FAKE_DOCKER_INFO_RC=1
export FAKE_SSH_FAIL=1
errfile="$TMP_ROOT/err.12b"
LOOM_JOB_EXECUTOR_HOST=host.example "$RUN_JOB" --id job-auto2 --image alpine -- true >/dev/null 2>"$errfile"
assert_eq "69" "$?" "auto falls back to ssh when no docker daemon is reachable here"
assert_contains "$(cat "$errfile")" "auto:ssh" "auto resolves to ssh (the loopback/remote executor) with no local docker"
unset FAKE_DOCKER_INFO_RC FAKE_SSH_FAIL

echo ""
echo "=== 13. Config-driven executor resolution ==="
reset_state
cat >"$LOOM_WORKSPACE/.loom/config.json" <<'CFG'
{
  "jobs": {
    "executor": {
      "mode": "ssh",
      "host": "cfg.example",
      "user": "cfguser",
      "sshOptions": ["-o", "BatchMode=yes", "-p", "2222"]
    }
  }
}
CFG
export FAKE_JOB_EXIT=0
"$RUN_JOB" --id job-cfg --image alpine -- true >/dev/null 2>"$TMP_ROOT/err.13"
assert_eq "0" "$?" "config-selected ssh executor runs the job"
assert_contains "$(cat "$FAKE_SSH_LOG")" "cfguser@cfg.example" "host/user come from jobs.executor config"
assert_contains "$(cat "$FAKE_SSH_LOG")" "-p 2222" "jobs.executor.sshOptions are applied"
assert_contains "$(cat "$TMP_ROOT/err.13")" "config (jobs.executor.mode)" "the config source is logged"
echo '{}' >"$LOOM_WORKSPACE/.loom/config.json"

echo ""
echo "=== 14. A spec file is accepted verbatim (--spec) ==="
reset_state
export FAKE_JOB_EXIT=0
cat >"$TMP_ROOT/job.json" <<'SPECFILE'
{
  "schema": "loom.run-job/v1",
  "id": "job-fromfile",
  "image": "ghcr.io/rjwalters/loom-worker:latest",
  "command": ["bash", "-lc", "cargo build --release"],
  "workdir": "/home/loom/workspaces/loom",
  "mounts": [{"path": "/home/loom/workspaces/loom", "mode": "rw"}],
  "env": {"CARGO_TARGET_DIR": "/home/loom/workspaces/loom/target"},
  "limits": {"cpus": "4", "memory": "8g"},
  "timeoutSeconds": 1800
}
SPECFILE
argv="$("$RUN_JOB" --spec "$TMP_ROOT/job.json" --dry-run 2>/dev/null)"
assert_contains "$argv" "loom-job-job-fromfile" "spec file id is used"
assert_contains "$argv" "/home/loom/workspaces/loom:/home/loom/workspaces/loom" "spec file mount is parity-bound"
assert_contains "$(tr '\n' ' ' <<<"$argv")" "--memory 8g" "spec file limits are applied"
"$RUN_JOB" --spec - --executor local </dev/null >/dev/null 2>&1
assert_eq "78" "$?" "an empty spec on stdin is EX_CONFIG"

echo ""
echo "=== 15. No shipped Loom script mounts a container-runtime socket ==="
# AC: "No docker socket is mounted into any worker container as part of this
# seam." Structural check, not a promise: grep every shipped script for a
# `-v …docker.sock` bind.
offenders=""
while IFS= read -r f; do
    if grep -nE '^[^#]*-v[[:space:]]*[^[:space:]]*(docker|containerd|podman|crio)\.sock' "$f" >/dev/null 2>&1; then
        offenders+="$f "
    fi
done < <(find "$SCRIPTS_DIR" -name '*.sh' -type f)
assert_eq "" "$offenders" "no shipped script binds a container-runtime socket into a container"

echo ""
echo "=== 16. A nonzero --timeout never outlives the job it guards (#7875) ==="
# REGRESSION GUARD, and the reason it lives here rather than in the E2E suite.
#
# The timeout watchdog used to be `( sleep N; ...; ) &`. `sleep` is a SEPARATE
# child of that subshell, so killing the subshell on the normal path orphaned
# it — and an orphaned `sleep` keeps every descriptor it inherited open,
# including the EXECUTOR'S STDERR. run-job.sh drains that stderr through a FIFO
# piped to `tee`, so `tee` never reached EOF and the client blocked for the
# FULL timeout on a job that had already exited.
#
# Every transport exercised below is PIPE-backed, which is the whole point: a
# file-backed transport stand-in cannot observe this class of bug at all (a
# held descriptor on a regular file blocks nobody), which is exactly how a
# 100%-green suite shipped a broken `--timeout`. `timeout 30` bounds a
# regression to a fast failure instead of a 45s one.
reset_state
export FAKE_JOB_EXIT=0
start=$SECONDS
timeout 30 "$RUN_JOB" --executor local --id job-to-local --image alpine --timeout 45 -- true >/dev/null 2>&1
rc=$?
elapsed=$((SECONDS - start))
assert_eq "0" "$rc" "local transport: a job carrying --timeout 45 still exits 0"
if ((elapsed < 15)); then
    pass "local transport: the client returns when the JOB does (${elapsed}s), not when the timeout does"
else
    fail "local transport: the client returns when the JOB does, not when the timeout does" \
        "took ${elapsed}s with --timeout 45 — the watchdog is stranding a descriptor again"
fi

reset_state
export FAKE_JOB_EXIT=0
start=$SECONDS
LOOM_JOB_EXECUTOR_HOST=to.example \
    timeout 30 "$RUN_JOB" --executor ssh --id job-to-ssh --image alpine --timeout 40 -- true >/dev/null 2>&1
rc=$?
elapsed=$((SECONDS - start))
assert_eq "0" "$rc" "ssh transport: a job carrying --timeout 40 still exits 0"
if ((elapsed < 15)); then
    pass "ssh transport: the client returns when the JOB does (${elapsed}s), not when the timeout does"
else
    fail "ssh transport: the client returns when the JOB does, not when the timeout does" \
        "took ${elapsed}s with --timeout 40 — the watchdog is stranding a descriptor again"
fi

# The fix must not have simply disarmed the feature: a job that really does
# outlive its timeout is still stopped, gracefully, and says so.
reset_state
export FAKE_JOB_EXIT=0
export FAKE_JOB_WAIT_SLEEP=5
errfile="$TMP_ROOT/err.16"
timeout 40 "$RUN_JOB" --executor local --id job-to-fire --image alpine --timeout 1 -- true >/dev/null 2>"$errfile"
assert_contains "$(cat "$errfile")" "# LOOM_RUN_JOB_TIMEOUT id=job-to-fire after=1s" "an expired timeout still fires and announces itself"
assert_contains "$(cat "$FAKE_DOCKER_LOG")" "stop --time" "an expired timeout stops the job GRACEFULLY (never docker kill)"
assert_not_contains "$(cat "$FAKE_DOCKER_LOG")" "kill loom-job-job-to-fire" "an expired timeout never SIGKILLs the job"
unset FAKE_JOB_WAIT_SLEEP

# The DETACH path must release the watchdog too: `_rj_on_signal` never touched
# it, so detaching stranded a timer on the same pipe. `tee` below is the
# client's own drain, so `wait`ing on it is precisely the client-side hang.
reset_state
export FAKE_JOB_EXIT=5
export FAKE_JOB_WAIT_SLEEP=30
dt_fifo="$TMP_ROOT/detach.fifo"
dt_cap="$TMP_ROOT/detach.cap"
rm -f "$dt_fifo"
mkfifo "$dt_fifo"
tee "$dt_cap" <"$dt_fifo" >/dev/null &
dt_tee=$!
spec="$(jq -nc '{schema:"loom.run-job/v1", id:"job-todetach", image:"alpine",
                 command:["sleep","300"], workdir:"", network:"none", mounts:[],
                 env:{}, limits:{cpus:"",memory:""}, timeoutSeconds:45}')"
start=$SECONDS
bash "$EXEC_LIB" run "$(printf '%s' "$spec" | base64 | tr -d '\n')" >/dev/null 2>"$dt_fifo" &
exec_pid=$!
for _ in $(seq 1 300); do
    [[ -f "$FAKE_DOCKER_STATE/loom-job-job-todetach" ]] && break
    sleep 0.1
done
kill -TERM "$exec_pid" 2>/dev/null
wait "$exec_pid"
rc=$?
wait "$dt_tee" 2>/dev/null # EOF arrives only once NOTHING still holds the pipe
elapsed=$((SECONDS - start))
assert_eq "75" "$rc" "detaching from a job that carries a timeout still exits 75"
if ((elapsed < 15)); then
    pass "detaching releases the timeout watchdog too (pipe drained in ${elapsed}s, not 45s)"
else
    fail "detaching releases the timeout watchdog too" \
        "the stderr pipe stayed open for ${elapsed}s after the detach — _rj_on_signal is stranding the watchdog"
fi
unset FAKE_JOB_WAIT_SLEEP

echo ""
echo "=== 17. A malformed invocation errors out; it never spins (#7875) ==="
# `shift 2` with only one positional left does NOT shift — bash leaves the
# positional parameters untouched and returns non-zero — so the parser's
# `while [[ $# -gt 0 ]]` loop spun at 100% CPU forever on a trailing
# value-taking flag. `set -e` is deliberately off in run-job.sh, so nothing
# else caught it. For a CLI the daemon invokes programmatically, a silent spin
# is a far worse failure shape than an error.
reset_state
for flag in --spec --image --mount --workdir --env --cpus --memory --network --timeout --id --executor; do
    out="$(timeout 10 "$RUN_JOB" "$flag" 2>&1)"
    rc=$?
    assert_eq "78" "$rc" "'run-job.sh $flag' with no value exits 78 (it does not spin)"
    assert_contains "$out" "'$flag' requires a value" "'$flag' with no value names the offending flag"
done
out="$(timeout 10 "$RUN_JOB" --nonesuch 2>&1)"
assert_eq "78" "$?" "an unknown option still exits 78"
assert_contains "$out" "unknown option" "an unknown option is named as such"
out="$(timeout 10 "$RUN_JOB" attach 2>&1)"
assert_eq "78" "$?" "'attach' with no job id exits 78 (it does not spin)"
assert_contains "$out" "requires a job id" "'attach' with no id says a job id is required"

echo ""
echo "=== 18. Caller migration (#7854): no shipped worker-container-path script shells a bare docker command ==="
# AC: "No remaining caller in the worker-container path mounts or assumes a
# docker socket." Section 15 above is the load-bearing structural check for a
# SOCKET MOUNT specifically; this one is the broader regression guard for the
# migration itself — a shipped script that shells a literal `docker run` /
# `docker exec` is, by construction, assuming it runs somewhere docker is
# actually reachable, which a worker container (per ADR-0017 Decision 3) never
# is. Any future build-gate stage or sim/build wrapper that needs docker-backed
# work belongs on THIS seam (`run-job.sh`), not a direct `docker` invocation —
# see `.loom/docs/run-job-seam.md` § "Caller migration" and
# `.loom/docs/build-gate.md` § "Docker-requiring toolchains" for the pattern.
#
# Matched the same way section 15 matches a socket mount: only text before a
# `#` on its own line counts (a comment explaining docker is excluded), so
# this stays a check on actual invocations, not prose that merely mentions
# `docker run`/`docker exec` (this file's own narrative strings above do,
# hence its own exclusion below).
#
# `lib/run-job-exec.sh` (the executor) is deliberately NOT exempt: it invokes
# docker via `"$DOCKER_BIN" run`/`"$DOCKER_BIN" exec` — a variable expansion,
# never the literal words — precisely so it stays configurable
# (`LOOM_RUN_JOB_DOCKER=podman`) and so this check needs no special case for
# the one file that legitimately runs docker-backed jobs.
#
# Two files ARE exempt, by design, because they are HOST-SIDE dispatch, not a
# worker-container-path caller: `spawn-claude.sh`'s containerized dispatch
# mode and `spawn-codex.sh`'s session-exec mode both run on the daemon's own
# host (which has a real docker) to START a worker/session container in the
# first place — they are the mechanism a worker container arrives FROM, not
# code that runs INSIDE one assuming a socket it does not have. Their own test
# doubles (`tests/test-spawn-codex.sh`) reference `docker exec` only in
# assertion strings, not as a literal invocation, and are exempt for the same
# reason this file is exempt from scanning itself.
exempt_files=(
    "$SCRIPTS_DIR/spawn-claude.sh"
    "$SCRIPTS_DIR/spawn-codex.sh"
    "$SCRIPTS_DIR/tests/test-spawn-codex.sh"
    "$SCRIPT_DIR/test-run-job.sh"
)
_is_exempt() {
    local candidate="$1" ex
    for ex in "${exempt_files[@]}"; do
        [[ "$candidate" == "$ex" ]] && return 0
    done
    return 1
}
offenders=""
while IFS= read -r f; do
    _is_exempt "$f" && continue
    if grep -nE '^[^#]*\bdocker[[:space:]]+(run|exec)\b' "$f" >/dev/null 2>&1; then
        offenders+="$f "
    fi
done < <(find "$SCRIPTS_DIR" -name '*.sh' -type f)
assert_eq "" "$offenders" "no non-exempt shipped script shells a literal 'docker run'/'docker exec' (route docker-backed work through run-job.sh instead)"

echo ""
echo "======================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
if ((TESTS_FAILED > 0)); then
    echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
    exit 1
fi
echo "All tests passed."
exit 0
