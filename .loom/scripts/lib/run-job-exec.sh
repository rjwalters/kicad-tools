#!/usr/bin/env bash
# run-job-exec.sh — the EXECUTOR half of the `run-job` seam (epic #6896
# Phase 4, issue #7853).
#
# This file is both:
#
#   1. The program that runs ON THE EXECUTOR HOST (a real docker host). It is
#      self-contained on purpose — `run-job.sh`'s ssh transport pipes this
#      exact file to `bash -s` on the remote host, so the executor host needs
#      NO Loom installation: bash, docker, jq and coreutils (base64,
#      mktemp, mkfifo, readlink) are the whole dependency list.
#   2. A sourceable library. `run-job.sh` sources it (with
#      LOOM_RUN_JOB_SOURCE_ONLY=1) to reuse the SAME spec validation and the
#      SAME `docker run` argv builder it would execute remotely — so
#      `run-job.sh --dry-run` prints the argv the executor will really run,
#      and client-side pre-flight rejection and executor-side defence-in-depth
#      rejection can never drift apart. One implementation, two call sites.
#
# ## Why an executor at all
#
# Per ADR-0017 Decision 3 (and the operator decision of 2026-08-24), a Loom
# worker container gets NO docker socket — not mounted, not reachable. A
# mounted `/var/run/docker.sock` is host-root-equivalent, which defeats the
# containment boundary it would be punched through. Docker-requiring
# workloads therefore travel as a *job spec* to a process that already runs on
# a real docker host. The worker's own host over loopback/ssh is the
# mandatory degenerate case (a single-host install must be self-sufficient); a
# right-sized remote host is the general case. Elastic *placement* of executor
# hosts is out of scope here and hands off to #3979.
#
# ## Verbs
#
#   run <base64-spec>   Start the job and stream it: detached `docker run`,
#                       `docker logs --follow`, `docker wait` for the exit
#                       code. Emits the exit sentinel and exits with the
#                       job's own exit code.
#   attach <job-id>     Re-stream an already-started job from its first line
#                       and wait for its exit code (the reattach path after a
#                       client/daemon restart).
#   status <job-id>     Print one machine-readable status line. Exit 0.
#   cancel <job-id>     GRACEFUL stop (`docker stop --time`, i.e. SIGTERM then
#                       a grace period). Never `docker kill`.
#
# ## Restart safety (issue #5119 drain semantics, ADR-0017 Decision 4)
#
# The job container is started **detached** and deliberately WITHOUT `--rm`:
#
#   - Killing this executor process — SIGTERM on a graceful drain, SIGHUP when
#     an ssh connection drops, or SIGKILL when a daemon is hard-killed — does
#     not stop the job. The container is owned by the executor host's docker
#     daemon, not by this process tree.
#   - On a *catchable* signal this process DETACHES (stops streaming, prints
#     the reattach hint, exits 75/EX_TEMPFAIL). It never `docker kill`s and
#     never `docker stop`s the job on teardown. Teardown is not cancellation:
#     cancelling is an explicit operator/daemon act (`cancel`), and even that
#     is a graceful `docker stop`.
#   - Because the container is not `--rm`, its exit code survives this
#     process's death and is retrievable later via `attach`/`status`. The
#     container is removed only after its exit code has actually been read.
#
# ## Exit codes
#
#   0..255  The job's own exit code, passed through faithfully (`run`/`attach`).
#   69      EX_UNAVAILABLE — docker unreachable, or the job container is gone.
#   75      EX_TEMPFAIL — detached while still in flight (reattach to resume).
#   78      EX_CONFIG — invalid job spec, or a missing executor dependency.
#
# ## Environment
#
#   LOOM_RUN_JOB_DOCKER        docker CLI to use (default `docker`; set to
#                              `podman` or an absolute path as needed).
#   LOOM_RUN_JOB_STOP_GRACE    seconds passed to `docker stop --time`
#                              (default 30).
#   LOOM_RUN_JOB_SOURCE_ONLY   when non-empty, define functions and return
#                              without dispatching a verb (library mode).

# NOTE: no `set -euo pipefail` at file scope — this file is sourced by
# run-job.sh, and inheriting shell options into a caller is a side effect a
# library must not have. `main` sets them for the program path.

LOOM_RUN_JOB_SCHEMA="loom.run-job/v1"
LOOM_RUN_JOB_CONTAINER_PREFIX="loom-job-"

_rj_docker() { command "${LOOM_RUN_JOB_DOCKER:-docker}" "$@"; }
_rj_err() { printf 'run-job-exec: %s\n' "$*" >&2; }

# _rj_docker_quiet <args...> — `_rj_docker` with both streams discarded.
#
# The SUBSHELL is the point here, not decoration (#7875). `_rj_docker` is a
# shell FUNCTION, and `_rj_docker … >/dev/null 2>&1` applies that redirection
# to THIS SHELL for the entire duration of the call. A drain SIGTERM arriving
# inside that window runs `_rj_on_signal` while fd 2 still points at
# /dev/null, so `# LOOM_RUN_JOB_DETACHED` is written and silently discarded —
# and because the executor's own `run-job-exec:` diagnostics are suppressed by
# the same redirection, `run-job.sh` then sees nothing at all and reports
# "the executor is unreachable — the job did NOT run" (69) for a job that is
# alive and well on the executor host. That is a fabricated result in exactly
# the drain scenario #5119 restart safety exists for. Redirecting a subshell
# instead leaves this shell's descriptors — and so the handler's — untouched.
_rj_docker_quiet() { (_rj_docker "$@") >/dev/null 2>&1; }

# Decode base64 on stdin. GNU coreutils and newer macOS use `-d`; older BSD
# base64 only understands `-D`. Both spellings are tried before giving up.
_rj_b64_decode() {
    local data
    data="$(cat)"
    printf '%s' "$data" | base64 -d 2>/dev/null ||
        printf '%s' "$data" | base64 -D 2>/dev/null
}

# _rj_realpath <path>
#
# Best-effort canonicalization: every symlink in every component resolved.
# Prints the resolved path, or the input unchanged when THIS host cannot
# resolve it (the path does not exist here, or neither `readlink -f` nor
# `realpath` is available). Never fails, never prints nothing.
#
# Only the EXECUTOR host's answer is meaningful — it is the host whose docker
# daemon resolves a bind mount's source — which is why the mount checks that
# use this are part of `loom_job_validate_spec`, the function that runs again
# executor-side. The client's pre-flight copy simply cannot resolve a path it
# cannot see, and falls back to the literal path there by design.
_rj_realpath() {
    local p="$1" r=""
    if r="$(readlink -f -- "$p" 2>/dev/null)" && [[ -n "$r" ]]; then
        printf '%s' "$r"
        return 0
    fi
    if r="$(realpath -- "$p" 2>/dev/null)" && [[ -n "$r" ]]; then
        printf '%s' "$r"
        return 0
    fi
    printf '%s' "$p"
}

# loom_job_spec_defaults <spec-json>
#
# Echoes the spec with every optional key filled in, so validation and argv
# construction never have to special-case a missing key. Echoes nothing and
# returns 1 when the input is not valid JSON.
loom_job_spec_defaults() {
    local spec="$1"
    jq -c '
      {
        schema: (.schema // "'"$LOOM_RUN_JOB_SCHEMA"'"),
        id: (.id // ""),
        image: (.image // ""),
        command: (.command // []),
        workdir: (.workdir // ""),
        network: (.network // "none"),
        mounts: [ (.mounts // [])[] | { path: (.path // ""), mode: (.mode // "rw") } ],
        env: (.env // {}),
        limits: { cpus: (.limits.cpus // ""), memory: (.limits.memory // "") },
        timeoutSeconds: (.timeoutSeconds // 0)
      }' <<<"$spec" 2>/dev/null || return 1
}

# Canonical (symlink-free) homes of the container-runtime sockets that the
# refusal patterns in `loom_job_validate_spec` name — on every mainstream Linux
# `/var/run -> /run` since the /run merge; both spellings are kept so the check
# still means something on a host where they are separate directories. A bind
# mount propagates a directory's ENTIRE contents, socket special files
# included, so a mount whose source is a proper ancestor of one of these
# (`/run`, `/var`, `/var/run`, `/run/podman`, ...) hands the job container the
# socket just as surely as naming the socket would (#7875 review finding).
_RJ_RUNTIME_SOCKET_PATHS=(
    /run/docker.sock /var/run/docker.sock
    /run/docker /var/run/docker
    /run/containerd/containerd.sock /var/run/containerd/containerd.sock
    /run/podman/podman.sock /var/run/podman/podman.sock
    /run/crio/crio.sock /var/run/crio/crio.sock
)

# _rj_socket_ancestor <path>
#
# Prints the first known container-runtime socket path that lives BENEATH
# <path> (i.e. <path> is a proper ancestor of it), or nothing. Pure string
# work against the canonical list above — no filesystem access — so it holds
# on the client's pre-flight exactly as it does on the executor host, and does
# not depend on the socket existing at validation time.
#
# PRECONDITION: <path> is already in canonical form. Being a prefix match, this
# is spelling-sensitive — `/run//` and `/run/.` name `/run` but are a prefix of
# nothing — so `loom_job_validate_spec` refuses a non-canonical mount path
# outright before it gets here (#7896). Do not weaken that refusal without
# giving this function a normalizer of its own.
_rj_socket_ancestor() {
    local dir="${1%/}" s
    [[ -n "$dir" ]] || return 0 # "/" is refused outright by the caller
    for s in "${_RJ_RUNTIME_SOCKET_PATHS[@]}"; do
        if [[ "$s" == "$dir"/* ]]; then
            printf '%s' "$s"
            return 0
        fi
    done
}

# _rj_live_socket_under <path>
#
# Prints the first live unix-domain socket found AT or BENEATH <path> on THIS
# host, or nothing. Best effort by construction: it is meaningful only on the
# executor host (the client cannot see what the executor's filesystem holds),
# stops at the first hit, never follows symlinks, and ignores subtrees it
# cannot read. It is the second layer behind `_rj_socket_ancestor`: whatever a
# socket is called and wherever a daemon was told to put it, a socket that is
# live right now is refused.
_rj_live_socket_under() {
    local p="$1"
    if [[ -S "$p" ]]; then
        printf '%s' "$p"
        return 0
    fi
    [[ -d "$p" ]] || return 0
    find "$p" -type s -print -quit 2>/dev/null || true
}

# loom_job_validate_spec <spec-json>
#
# Returns 0 when the spec is well-formed and safe to execute; otherwise prints
# every violation to stderr and returns 1.
#
# This is a SECURITY boundary, not just a typo check: it is what makes "no
# docker socket ever reaches a job container" a property of the seam rather
# than a convention its callers are asked to remember. It runs twice — once on
# the client as pre-flight, once here on the executor host — precisely so a
# buggy or compromised client cannot talk this executor into a privileged
# container.
#
# For that claim to hold, the mount checks below match the RESOLVED source
# path, not just its spelling: docker resolves a bind's source on this host, so
# a pattern list that only ever saw a symlink's innocent name would be no
# boundary at all. Resolution is meaningful only here, on the host that will do
# the mounting — which is what makes the executor-side run load-bearing rather
# than merely belt-and-braces.
loom_job_validate_spec() {
    local spec="$1"
    local errors=()
    local v

    if ! jq -e . >/dev/null 2>&1 <<<"$spec"; then
        _rj_err "job spec is not valid JSON"
        return 1
    fi

    v="$(jq -r '.schema // ""' <<<"$spec")"
    [[ "$v" == "$LOOM_RUN_JOB_SCHEMA" ]] ||
        errors+=("schema must be \"$LOOM_RUN_JOB_SCHEMA\" (got \"$v\")")

    v="$(jq -r '.id // ""' <<<"$spec")"
    [[ "$v" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] ||
        errors+=("id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\$ (got \"$v\")")

    v="$(jq -r '.image // ""' <<<"$spec")"
    if [[ -z "$v" ]]; then
        errors+=("image is required")
    elif [[ "$v" =~ [[:space:]] ]]; then
        errors+=("image must not contain whitespace (got \"$v\")")
    fi

    v="$(jq -r 'if (.command | type) == "array" then (.command | length) else -1 end' <<<"$spec")"
    if [[ "$v" == "-1" ]]; then
        errors+=("command must be an array of strings")
    elif [[ "$v" == "0" ]]; then
        errors+=("command must not be empty")
    elif ! jq -e '[.command[] | type] | all(. == "string")' >/dev/null 2>&1 <<<"$spec"; then
        errors+=("command must contain only strings")
    fi

    v="$(jq -r '.workdir // ""' <<<"$spec")"
    if [[ -n "$v" ]]; then
        [[ "$v" == /* ]] || errors+=("workdir must be an absolute path (got \"$v\")")
        [[ "$v" == *..* ]] && errors+=("workdir must not contain '..' (got \"$v\")")
    fi

    v="$(jq -r '.network // ""' <<<"$spec")"
    case "$v" in
        none | bridge) ;;
        *) errors+=("network must be \"none\" or \"bridge\" (got \"$v\"); host networking is not offered by this seam") ;;
    esac

    # --- Mounts: path parity + the docker-socket refusal ---------------------
    #
    # A mount is a single ABSOLUTE PATH plus a mode; the executor always mounts
    # it at the identical absolute path inside the job container. There is no
    # src:dst form to get wrong, so docker/worker/MOUNT-CONTRACT.md §1 (path
    # parity) is structurally enforced by the spec shape itself rather than by
    # a rule callers have to follow.
    local path mode real spelled candidate below live refused
    while IFS=$'\t' read -r path mode; do
        [[ -z "$path$mode" ]] && continue

        # Mode is checked first, and unconditionally, so that a path refused
        # outright below still reports a bad mode alongside its own reason.
        case "$mode" in
            ro | rw) ;;
            *) errors+=("mount mode must be \"ro\" or \"rw\" (got \"$mode\" for \"$path\")") ;;
        esac

        # --- Refused outright: nothing further to learn about this path ------
        #
        # Each of these `continue`s, rather than falling through: the spec is
        # already rejected, and the checks below would only add a second reason
        # — at the cost of running a filesystem walk (`_rj_live_socket_under`)
        # over a path the caller has already been told it may not name, and of
        # reporting a *misleading* second reason for a non-canonical spelling
        # (#7896).
        if [[ "$path" != /* ]]; then
            errors+=("mount path must be absolute (got \"$path\")")
            continue
        fi
        if [[ "$path" == *..* ]]; then
            errors+=("mount path must not contain '..' (got \"$path\")")
            continue
        fi
        # Canonical spelling is REQUIRED, not merely preferred. Every check
        # below either matches the path as a string (the socket-name patterns,
        # `_rj_socket_ancestor`) or compares it against its resolved form, and
        # a non-canonical spelling of the same directory defeats the string
        # side of that: `/run//`, `//run`, `/run/.` and `/run/./` all name
        # `/run`, yet none of them is a prefix match for `/run/docker.sock`.
        # The EXECUTOR would still catch them (its `readlink -f` collapses the
        # spelling, and the ancestor check then fires on the resolved
        # candidate), but the CLIENT pre-flight cannot resolve a path that does
        # not exist on the client host — a macOS client pre-flighting a job for
        # a Linux executor emitted `-v /run//:/run//` (#7896). Refusing the
        # spelling here, next to the `..` refusal, keeps that a property of one
        # rule rather than of a normalizer every later check must be trusted to
        # have run.
        if [[ "$path" == *//* || "$path" == */./* || "$path" == */. ]]; then
            errors+=("mount path must be in canonical form: no '//' or '/.' path segments (got \"$path\")")
            continue
        fi
        if [[ "$path" == "/" ]]; then
            errors+=("refusing to mount the host root '/'")
            continue
        fi

        # Resolve the source path BEFORE the refusal patterns below, and check
        # BOTH spellings against them.
        #
        # Matching the literal path string alone is not a boundary: docker
        # resolves a bind mount's source on the executor host at run time, so
        # `/srv/work/innocent.sock -> /var/run/docker.sock` would put the real
        # socket inside the job container while sailing past a pattern list
        # that only ever saw the link's own harmless name. An agent inside a
        # worker container can create exactly that link inside any `rw` parity
        # mount it already holds — i.e. precisely the adversary ADR-0017
        # Decision 3 is about.
        #
        # A symlink is refused outright rather than silently rewritten: the
        # seam's mount shape is ONE path, bound at the identical path inside
        # the container (MOUNT-CONTRACT.md §1), so substituting the resolved
        # path would quietly break the parity guarantee callers rely on. Pass
        # the resolved path explicitly instead.
        #
        # The comparison is against the given spelling minus ONE trailing
        # slash: `/srv/work/` and `/srv/work` name the same directory and
        # `readlink -f` always returns the latter, so without the `%/` an
        # ordinary trailing slash was refused as `is a symlink to "/srv/work"`
        # — a message naming a cause that was not the real one (#7896). Every
        # other spelling difference is refused outright above, so a mismatch
        # that survives to here means a genuine symlink.
        spelled="${path%/}"
        real="$(_rj_realpath "$path")"
        [[ "$real" == "$spelled" ]] && real=""
        refused=""
        if [[ -n "$real" ]]; then
            errors+=("mount path \"$path\" is a symlink to \"$real\": the executor's docker daemon binds the RESOLVED path, so a link would break this seam's path-parity guarantee and could smuggle a refused path past the checks below — pass \"$real\" explicitly instead")
            refused=1
        fi

        for candidate in "$path" ${real:+"$real"}; do
            case "$candidate" in
                */docker.sock | */docker.sock/* | /var/run/docker* | /run/docker* | */containerd.sock | */podman.sock | */crio.sock)
                    errors+=("refusing docker/container-runtime socket mount \"$candidate\": a mounted container-runtime socket is host-root-equivalent and is exactly what this seam exists to avoid (ADR-0017 Decision 3)")
                    refused=1
                    ;;
                /proc* | /sys* | /dev*)
                    errors+=("refusing host kernel-interface mount \"$candidate\" (/proc, /sys and /dev are not mountable through this seam)")
                    refused=1
                    ;;
            esac

            # The name patterns above match the socket itself, or a path
            # inside it — NOT the directory that contains it, and a bind mount
            # of a directory carries every socket beneath it (#7875 review).
            # `/run` is docker.sock's real, non-symlink home on essentially
            # every mainstream distro and matches none of the name patterns,
            # so without this an innocent-looking `--mount /run` handed the
            # job container the docker socket.
            below="$(_rj_socket_ancestor "$candidate")"
            if [[ -n "$below" ]]; then
                errors+=("refusing mount \"$candidate\": it is an ancestor of container-runtime socket path \"$below\", and a bind mount of a directory carries every socket beneath it — host-root-equivalent, exactly what this seam exists to avoid (ADR-0017 Decision 3)")
                refused=1
            fi
        done

        # Every remaining check reads the filesystem. Once a name-based check
        # above has already refused this path there is nothing left to decide,
        # so skip the walk rather than pay for it: `find`ing a huge tree the
        # caller was told it may not name (`/proc`, `/sys`, `/run`) just to add
        # a second reason to an already-rejected spec is pure executor cost
        # (#7896).
        [[ -n "$refused" ]] && continue

        # Second, host-local layer behind the name-based checks: whatever the
        # path is called, refuse to bind a live unix socket, or a directory
        # that holds one, on THIS host. This is what catches a runtime socket
        # the canonical list cannot know about — a daemon started with
        # `-H unix:///srv/x.sock`, rootless docker under `/run/user/<uid>` —
        # at the cost of being meaningful only on the executor host, and only
        # for sockets that exist at validation time (a socket created later
        # inside an `rw` mount is the residual TOCTOU the seam does not claim
        # to close; see run-job-seam.md).
        live="$(_rj_live_socket_under "${real:-$path}")"
        if [[ -n "$live" ]]; then
            errors+=("refusing mount \"$path\": it is, or contains, a live unix socket (\"$live\") — this seam binds filesystem paths, never host IPC endpoints, because a socket bound into a job container is host-root-equivalent in the worst case (ADR-0017 Decision 3)")
        fi
    done < <(jq -r '.mounts[]? | [.path // "", .mode // ""] | @tsv' <<<"$spec")

    while IFS= read -r v; do
        [[ -z "$v" ]] && continue
        [[ "$v" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] ||
            errors+=("env key must match ^[A-Za-z_][A-Za-z0-9_]*\$ (got \"$v\")")
    done < <(jq -r '.env // {} | keys[]?' <<<"$spec")

    v="$(jq -r '.limits.cpus // ""' <<<"$spec")"
    [[ -z "$v" || "$v" =~ ^[0-9]+(\.[0-9]+)?$ ]] ||
        errors+=("limits.cpus must be a number (got \"$v\")")

    v="$(jq -r '.limits.memory // ""' <<<"$spec")"
    [[ -z "$v" || "$v" =~ ^[0-9]+([bkmgBKMG]|[kKmMgG][bB])?$ ]] ||
        errors+=("limits.memory must be a docker size like 4g/512m (got \"$v\")")

    v="$(jq -r '.timeoutSeconds // 0' <<<"$spec")"
    [[ "$v" =~ ^[0-9]+$ ]] ||
        errors+=("timeoutSeconds must be a non-negative integer (got \"$v\")")

    if ((${#errors[@]} > 0)); then
        local e
        for e in "${errors[@]}"; do _rj_err "invalid job spec: $e"; done
        return 1
    fi
    return 0
}

# loom_job_container_name <job-id>
loom_job_container_name() { printf '%s%s' "$LOOM_RUN_JOB_CONTAINER_PREFIX" "$1"; }

# loom_job_docker_argv <spec-json>
#
# Prints the full `docker run` argv, one argument per line, that this executor
# will run for the spec. `run-job.sh --dry-run` prints exactly this, so an
# operator can audit the real command without running it.
#
# Deliberately absent, and never constructible from a spec: `--privileged`,
# `--cap-add`, `--device`, `--security-opt`, `--pid=host`, `--user 0`, and any
# `-v` of a container-runtime socket. The spec has no field that maps to them.
loom_job_docker_argv() {
    local spec="$1"
    local id image workdir network cpus memory name
    id="$(jq -r '.id' <<<"$spec")"
    image="$(jq -r '.image' <<<"$spec")"
    workdir="$(jq -r '.workdir // ""' <<<"$spec")"
    network="$(jq -r '.network // "none"' <<<"$spec")"
    cpus="$(jq -r '.limits.cpus // ""' <<<"$spec")"
    memory="$(jq -r '.limits.memory // ""' <<<"$spec")"
    name="$(loom_job_container_name "$id")"

    local argv=(run --detach --name "$name")
    # NOTE: no `--rm`. The container must outlive this process so its exit code
    # is still readable after a client/daemon restart (see the restart-safety
    # note in this file's header). It is removed explicitly once its exit code
    # has been read.
    argv+=(--label "loom.job=1" --label "loom.job.id=${id}" --label "loom.job.schema=${LOOM_RUN_JOB_SCHEMA}")
    argv+=(--network "$network")
    [[ -n "$cpus" ]] && argv+=(--cpus "$cpus")
    [[ -n "$memory" ]] && argv+=(--memory "$memory")

    local path mode
    while IFS=$'\t' read -r path mode; do
        [[ -z "$path" ]] && continue
        if [[ "$mode" == "ro" ]]; then
            argv+=(-v "${path}:${path}:ro")
        else
            argv+=(-v "${path}:${path}")
        fi
    done < <(jq -r '.mounts[]? | [.path, .mode] | @tsv' <<<"$spec")

    local kv
    while IFS= read -r kv; do
        [[ -z "$kv" ]] && continue
        argv+=(-e "$kv")
    done < <(jq -r '.env // {} | to_entries[]? | "\(.key)=\(.value)"' <<<"$spec")

    [[ -n "$workdir" ]] && argv+=(-w "$workdir")
    argv+=("$image")

    local arg
    while IFS= read -r arg; do
        argv+=("$arg")
    done < <(jq -r '.command[]' <<<"$spec")

    printf '%s\n' "${argv[@]}"
}

# --- Program half ------------------------------------------------------------

_RJ_DETACHED=0
_RJ_LOGS_PID=""
_RJ_WAIT_PID=""
_RJ_TIMEOUT_PID=""
_RJ_JOB_ID=""

# _rj_watchdog_sleep <seconds>
#
# Sleep for N seconds WITHOUT forking a `sleep(1)` child, so a watchdog
# subshell that runs it can be killed cleanly and leaves NOTHING behind.
#
# Why this is not just `sleep N` (issue #7875): a watchdog written the obvious
# way — `( sleep N; ...; ) &` — makes `sleep` a SEPARATE child of the subshell.
# `kill`ing the subshell therefore orphans that sleep, and the orphan keeps
# every descriptor it inherited open, including this executor's stderr.
# `run-job.sh` captures the executor's stderr through a FIFO drained by `tee`,
# so `tee` never reaches EOF and the CLIENT blocks for the whole remaining
# sleep on a job that already finished. A file-backed transport hides this (a
# held fd on a file blocks nobody); a real, pipe-backed ssh hop does not.
#
# `read -t` on a FIFO opened read-write (`<>`, so it never sees EOF) blocks
# inside this very process: there is no child to orphan. `mkfifo` is coreutils,
# same package as the `base64`/`mktemp` this executor already needs; if it is
# somehow missing we fall back to plain `sleep` rather than failing the job.
_rj_watchdog_sleep() {
    local secs="$1" fifo=""
    fifo="$(mktemp -u 2>/dev/null)" || fifo=""
    if [[ -n "$fifo" ]] && mkfifo "$fifo" 2>/dev/null; then
        exec 9<>"$fifo"
        rm -f "$fifo"
        read -r -t "$secs" -u 9 || true
        exec 9>&-
        return 0
    fi
    sleep "$secs"
}

# _rj_cancel_timeout
#
# Stop the job-timeout watchdog. Called on BOTH exits from a streamed job — the
# normal one (the job finished before its timeout) and the detach path in
# `_rj_on_signal` — because either way the watchdog is now pointless and, until
# it is gone, it holds this executor's stderr open (see `_rj_watchdog_sleep`).
_rj_cancel_timeout() {
    [[ -n "$_RJ_TIMEOUT_PID" ]] && kill "$_RJ_TIMEOUT_PID" 2>/dev/null
    _RJ_TIMEOUT_PID=""
    return 0
}

_rj_on_signal() {
    _RJ_DETACHED=1
    # Kill only OUR OWN observer processes (`docker logs -f`, `docker wait`,
    # the timeout watchdog). Restart safety: DO NOT stop, kill, or remove the
    # container here. The job keeps running on the executor host; only this
    # stream is going away.
    [[ -n "$_RJ_LOGS_PID" ]] && kill "$_RJ_LOGS_PID" 2>/dev/null
    [[ -n "$_RJ_WAIT_PID" ]] && kill "$_RJ_WAIT_PID" 2>/dev/null
    _rj_cancel_timeout
    # This marker reaching the REAL stderr is load-bearing: it is the only
    # thing that tells `run-job.sh` "detached, the job is still running on the
    # executor" rather than "the executor never spoke". See `_rj_docker_quiet`
    # for why every quieted docker call runs in a subshell — without that, a
    # signal landing mid-call would find fd 2 pointing at /dev/null here.
    printf '# LOOM_RUN_JOB_DETACHED id=%s reason=signal\n' "$_RJ_JOB_ID" >&2
    exit 75
}

_rj_require() {
    local missing=()
    command -v jq >/dev/null 2>&1 || missing+=(jq)
    command -v "${LOOM_RUN_JOB_DOCKER:-docker}" >/dev/null 2>&1 || missing+=("${LOOM_RUN_JOB_DOCKER:-docker}")
    if ((${#missing[@]} > 0)); then
        _rj_err "executor host is missing required command(s): ${missing[*]}"
        _rj_err "the run-job executor needs: bash, docker (or LOOM_RUN_JOB_DOCKER), jq, coreutils (base64/mktemp/mkfifo/readlink)"
        return 78
    fi
    return 0
}

# _rj_stream_and_wait <job-id>
#
# Stream the container's logs from its first line and block on its exit code.
# Used by both `run` (after the detached start) and `attach` (after a restart)
# — one implementation, so a reattached job's output and exit code are
# byte-identical to a job watched from the start.
_rj_stream_and_wait() {
    local id="$1"
    local name
    name="$(loom_job_container_name "$id")"

    if ! _rj_docker_quiet inspect "$name"; then
        _rj_err "no job container for id '$id' (already reaped, or never started)"
        return 69
    fi

    _rj_docker logs --follow "$name" &
    _RJ_LOGS_PID=$!

    # `docker wait` runs in the BACKGROUND and is awaited with the `wait`
    # builtin, deliberately: bash defers a trapped signal until the current
    # FOREGROUND command finishes, so a foreground `docker wait` would make
    # this executor ignore a drain SIGTERM for the whole life of the job.
    # `wait` is interruptible, so the detach handler runs promptly.
    local code_file
    code_file="$(mktemp)"
    _rj_docker wait "$name" >"$code_file" 2>/dev/null &
    _RJ_WAIT_PID=$!
    wait "$_RJ_WAIT_PID" 2>/dev/null
    _RJ_WAIT_PID=""

    local code
    code="$(tail -n 1 "$code_file" 2>/dev/null | tr -d '[:space:]')"
    rm -f "$code_file"
    [[ "$code" =~ ^[0-9]+$ ]] || code=""

    # The follower exits on its own once the container exits; a watchdog keeps
    # a wedged `docker logs -f` from hanging the executor forever. Its timer is
    # fork-free on purpose — see `_rj_watchdog_sleep`: killing this subshell
    # below must not leave an orphan holding the executor's stderr, or the
    # client stalls for the remaining 10s on an already-finished job.
    (
        _rj_watchdog_sleep 10
        kill "$_RJ_LOGS_PID" 2>/dev/null || true
    ) &
    local watchdog=$!
    wait "$_RJ_LOGS_PID" 2>/dev/null || true
    kill "$watchdog" 2>/dev/null || true
    _RJ_LOGS_PID=""

    if [[ -z "$code" ]]; then
        _rj_err "could not read an exit code for job '$id'"
        return 69
    fi

    # The exit sentinel. `run-job.sh` treats this line — not the transport's
    # own exit status — as the authoritative job result, which is what makes
    # "the job exited 255" distinguishable from "ssh failed with 255".
    printf '# LOOM_RUN_JOB_EXIT id=%s code=%s\n' "$id" "$code" >&2
    _rj_docker_quiet rm --force "$name" || true
    return "$code"
}

_rj_verb_run() {
    local spec_b64="${1:-}"
    [[ -n "$spec_b64" ]] || {
        _rj_err "run: missing base64 job spec"
        return 78
    }

    local spec
    spec="$(printf '%s' "$spec_b64" | _rj_b64_decode)"
    if [[ -z "$spec" ]]; then
        _rj_err "run: job spec is not valid base64"
        return 78
    fi
    spec="$(loom_job_spec_defaults "$spec")" || {
        _rj_err "run: job spec is not valid JSON"
        return 78
    }
    loom_job_validate_spec "$spec" || return 78

    _RJ_JOB_ID="$(jq -r '.id' <<<"$spec")"
    local timeout
    timeout="$(jq -r '.timeoutSeconds // 0' <<<"$spec")"

    local argv=()
    while IFS= read -r line; do argv+=("$line"); done < <(loom_job_docker_argv "$spec")

    printf '# LOOM_RUN_JOB_START id=%s image=%s network=%s cpus=%s memory=%s\n' \
        "$_RJ_JOB_ID" "$(jq -r '.image' <<<"$spec")" "$(jq -r '.network' <<<"$spec")" \
        "$(jq -r '.limits.cpus // "none"' <<<"$spec")" "$(jq -r '.limits.memory // "none"' <<<"$spec")" >&2

    if ! _rj_docker "${argv[@]}" >/dev/null; then
        _rj_err "failed to start job container for id '$_RJ_JOB_ID'"
        return 69
    fi

    _RJ_TIMEOUT_PID=""
    if [[ "$timeout" =~ ^[0-9]+$ ]] && ((timeout > 0)); then
        (
            _rj_watchdog_sleep "$timeout"
            printf '# LOOM_RUN_JOB_TIMEOUT id=%s after=%ss\n' "$_RJ_JOB_ID" "$timeout" >&2
            _rj_docker_quiet stop --time "${LOOM_RUN_JOB_STOP_GRACE:-30}" "$(loom_job_container_name "$_RJ_JOB_ID")" || true
        ) &
        _RJ_TIMEOUT_PID=$!
    fi

    local rc=0
    _rj_stream_and_wait "$_RJ_JOB_ID" || rc=$?
    _rj_cancel_timeout
    return "$rc"
}

_rj_verb_attach() {
    local id="${1:-}"
    [[ -n "$id" ]] || {
        _rj_err "attach: missing job id"
        return 78
    }
    _RJ_JOB_ID="$id"
    local rc=0
    _rj_stream_and_wait "$id" || rc=$?
    return "$rc"
}

_rj_verb_status() {
    local id="${1:-}"
    [[ -n "$id" ]] || {
        _rj_err "status: missing job id"
        return 78
    }
    local name state code
    name="$(loom_job_container_name "$id")"
    state="$(_rj_docker inspect --format '{{.State.Status}}' "$name" 2>/dev/null || true)"
    if [[ -z "$state" ]]; then
        printf '# LOOM_RUN_JOB_STATUS id=%s state=absent exit=-\n' "$id"
        return 0
    fi
    code="$(_rj_docker inspect --format '{{.State.ExitCode}}' "$name" 2>/dev/null || echo '-')"
    [[ "$state" == "exited" ]] || code='-'
    printf '# LOOM_RUN_JOB_STATUS id=%s state=%s exit=%s\n' "$id" "$state" "$code"
    return 0
}

_rj_verb_cancel() {
    local id="${1:-}"
    [[ -n "$id" ]] || {
        _rj_err "cancel: missing job id"
        return 78
    }
    local name
    name="$(loom_job_container_name "$id")"
    if ! _rj_docker_quiet inspect "$name"; then
        _rj_err "cancel: no job container for id '$id'"
        return 69
    fi
    # GRACEFUL only: `docker stop` sends SIGTERM and waits out the grace period
    # before docker itself escalates. This executor never runs `docker kill`.
    _rj_docker_quiet stop --time "${LOOM_RUN_JOB_STOP_GRACE:-30}" "$name" || true
    printf '# LOOM_RUN_JOB_CANCELLED id=%s grace=%ss\n' "$id" "${LOOM_RUN_JOB_STOP_GRACE:-30}" >&2
    return 0
}

_rj_main() {
    set -uo pipefail
    # requires-daemon: private-workspace >= 0.19.335  No host execution from a private worker.
    if [[ -n "${LOOM_PRIVATE_WORKSPACE:-}" || -e /workspace/identity.json ]]; then
        "${LOOM_DAEMON_SELF_BIN:-loom-daemon}" private-workspace check-host-job || return 78
    fi
    trap _rj_on_signal TERM INT HUP

    local verb="${1:-}"
    shift || true

    case "$verb" in
        run | attach | status | cancel) ;;
        "" | -h | --help)
            _rj_err "usage: run-job-exec.sh {run <base64-spec>|attach <id>|status <id>|cancel <id>}"
            return 78
            ;;
        *)
            _rj_err "unknown verb '$verb'"
            return 78
            ;;
    esac

    local rc=0
    _rj_require || return $?
    "_rj_verb_${verb}" "$@" || rc=$?
    return "$rc"
}

if [[ -z "${LOOM_RUN_JOB_SOURCE_ONLY:-}" ]]; then
    _rj_main "$@"
    exit $?
fi
