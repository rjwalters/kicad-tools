#!/usr/bin/env bash
# canonical-path.sh — shared, symlink-aware path canonicalization for the guard
# boundary (issue #4495, epic #4489 Phase 6).
#
# WHY THIS EXISTS
#
# `guard-worktree-paths.sh` historically normalized a target path with
# `python3 os.path.normpath`, which is a *lexical* operation: it collapses `.`
# and `..` but does NOT resolve symlinks. A path like
#
#     <worktree>/link-to-main/CLAUDE.md      (link-to-main -> <main-root>)
#
# therefore stayed "inside the worktree" as a string while actually resolving
# into the main checkout — the prefix-string bypass class #4495 requires to be
# closed for both Claude and Codex inputs.
#
# `loom_canonical_path` resolves symlinks in every component that EXISTS and
# keeps the (normalized) tail that does not, so it works for a `Write` to a
# brand-new file whose parent directories do not exist yet. This is exactly
# `os.path.realpath`'s non-strict behavior.
#
# CONTRACT
#
#   loom_canonical_path <path> [cwd]
#
#   - Prints the canonical absolute path on stdout, returns 0.
#   - A relative <path> is resolved against [cwd] when given, else $PWD.
#   - An empty <path> returns 1 and prints nothing.
#   - NEVER exits the calling shell and never emits anything on stderr, so a
#     guard's fail-open/fail-closed contract stays the caller's decision.
#
# RESOLUTION CHAIN (first available wins; all three agree on non-symlink input)
#
#   1. python3 `os.path.realpath`     — symlink-aware, tolerates missing tails
#   2. `realpath -m` (GNU coreutils)  — same semantics, `-m` = no existence req.
#   3. pure-bash lexical fallback     — collapses `.`/`..` only (pre-#4495
#                                       behavior), used only when neither of
#                                       the above is on PATH
#
# The bash fallback is deliberately a *degradation*, not an error: a host with
# no python3 and no GNU realpath still gets the historical protection rather
# than a broken guard. Callers that need symlink resolution to be load-bearing
# (the Codex bridge, for mutable roles) check `loom_canonical_path_is_exact`.

# True when a symlink-aware resolver is available, i.e. when
# `loom_canonical_path` performs real canonicalization rather than the lexical
# fallback. Cached after the first probe.
_LOOM_CANON_IMPL=""

_loom_canon_impl() {
    if [[ -z "$_LOOM_CANON_IMPL" ]]; then
        if command -v python3 >/dev/null 2>&1; then
            _LOOM_CANON_IMPL="python3"
        elif command -v realpath >/dev/null 2>&1 && realpath -m / >/dev/null 2>&1; then
            _LOOM_CANON_IMPL="realpath"
        else
            _LOOM_CANON_IMPL="lexical"
        fi
    fi
    printf '%s' "$_LOOM_CANON_IMPL"
}

# Returns 0 when canonicalization is symlink-aware (exact), 1 when the caller is
# getting the lexical fallback.
loom_canonical_path_is_exact() {
    [[ "$(_loom_canon_impl)" != "lexical" ]]
}

# Pure-bash lexical normalization: collapse `//`, `.` and `..`. No forks, no
# filesystem access, so it is safe for paths whose parents do not exist.
_loom_canon_lexical() {
    local raw="$1"
    local -a stack=()
    local comp
    local oldifs="$IFS"
    IFS='/'
    # shellcheck disable=SC2086  # deliberate word-splitting on '/'
    for comp in $raw; do
        case "$comp" in
            ''|'.')
                continue
                ;;
            '..')
                if [[ ${#stack[@]} -gt 0 ]]; then
                    unset 'stack[${#stack[@]}-1]'
                    stack=(${stack[@]+"${stack[@]}"})
                fi
                ;;
            *)
                stack+=("$comp")
                ;;
        esac
    done
    IFS="$oldifs"
    if [[ ${#stack[@]} -eq 0 ]]; then
        printf '/'
        return 0
    fi
    local out=""
    for comp in "${stack[@]}"; do
        out="$out/$comp"
    done
    printf '%s' "$out"
}

loom_canonical_path() {
    local raw="${1:-}"
    local base="${2:-}"
    [[ -n "$raw" ]] || return 1

    # Absolutize first, so every implementation below sees the same input.
    if [[ "$raw" != /* ]]; then
        if [[ -n "$base" ]]; then
            raw="${base%/}/$raw"
        else
            raw="${PWD%/}/$raw"
        fi
    fi

    local out=""
    case "$(_loom_canon_impl)" in
        python3)
            # `os.path.realpath` (non-strict) resolves symlinks in the existing
            # prefix and normalizes the missing tail. Read the path from stdin
            # so no shell metacharacter, quote, newline, or `$(` in the path is
            # ever interpolated into the python source (#4495: never build a
            # command from untrusted JSON values).
            out=$(printf '%s' "$raw" | python3 -c \
                'import os,sys; sys.stdout.write(os.path.realpath(sys.stdin.buffer.read().decode("utf-8","surrogateescape")))' \
                2>/dev/null) || out=""
            ;;
        realpath)
            out=$(realpath -m -- "$raw" 2>/dev/null) || out=""
            ;;
    esac

    if [[ -z "$out" ]]; then
        out="$(_loom_canon_lexical "$raw")"
    fi
    printf '%s' "$out"
}

# Canonicalize a directory that is expected to exist. Falls back to
# loom_canonical_path for a missing directory, so callers never have to branch.
loom_canonical_dir() {
    local dir="${1:-}"
    [[ -n "$dir" ]] || return 1
    if [[ -d "$dir" ]]; then
        local out
        out=$(cd "$dir" 2>/dev/null && pwd -P 2>/dev/null) || out=""
        if [[ -n "$out" ]]; then
            printf '%s' "$out"
            return 0
        fi
    fi
    loom_canonical_path "$dir" "${2:-}"
}
