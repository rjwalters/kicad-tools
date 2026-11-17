#!/usr/bin/env bash
# check-config-no-host-paths.sh — assert no TRACKED config tier carries a
# newly-committed absolute path under a home directory (issue #6504).
#
# Root cause this guards against: `.loom/config.json` (and, once a repo
# migrates onto it, `.loom-project/project.json`) is TRACKED and shared
# across every host, but a value like `observability.ingestKeyFile` or
# `safehouse.socket` is inherently per-host — an absolute path under
# `/home/<user>/…`, `/Users/<user>/…`, or `/root/…` committed there is
# correct on at most one host and silently wrong (or foreign-home, #5336) on
# every other one that `git pull`s it. This exact class recurred three times
# before this guard existed: #5354 (ingestKeyFile), #5464 (safehouse.socket),
# and #6499 (a stash-pop conflict re-introduced ingestKeyFile as a committed
# `/home/ubuntu/…` path, fixed reactively by #6506). This script is the
# structural gate that stops a fourth recurrence — the config-content
# counterpart to check-conflict-markers.sh's (#6499) structural gate against
# committed conflict markers.
#
# What counts as a violation: any STRING value, at any depth, in a scanned
# file that matches `^(/home/<user>|/Users/<user>|/root)(/|$)` — regardless
# of whose home it names. Even a value that happens to be correct on the
# committing host is wrong the moment a second host pulls it; there is no
# "acceptable" home-directory path to commit, only ones nobody has been
# bitten by yet.
#
# Allowlist: `daemon.delegatedTo` (issue #5345) is a documented, deliberate
# exception — an absolute path that is genuinely repo/host-specific BY
# DESIGN (it names the delegate repo this workspace's admin actions are
# routed to) and is never expected to be portable across hosts the way
# `ingestKeyFile`/`socket` are. See config_resolver.rs's
# `DAEMON_DELEGATED_TO_KEY` doc comment. Extend ALLOWLIST_KEYS below (a
# dotted-path exact match) for any future key with the same property —
# never widen the path regex itself to carve out an exception.
#
# The fix for a real violation is NOT always ".loom-local/local.json" — check
# whether the value's own $HOME-relative default already does the right
# thing per-host (the ingestKeyFile fix, #6506: just delete the committed
# line) before reaching for the local tier (the safehouse.socket fix, #5457
# + #5523: no code-level default exists, so the value must live somewhere,
# and that somewhere is `.loom-local/local.json`). See
# defaults/docs/fleet-config-lifecycle.md and
# docs/design/config-resolution-tiers.md §5 for the full runbook.
#
# Usage:
#   ./defaults/scripts/check-config-no-host-paths.sh              # scan the default tracked tiers
#   ./defaults/scripts/check-config-no-host-paths.sh FILE [FILE…] # scan specific files
#   ./defaults/scripts/check-config-no-host-paths.sh --self-test
#   ./defaults/scripts/check-config-no-host-paths.sh --help
#
# Default scanned files (repo-root-relative, each skipped silently when
# absent — most repos have never migrated onto the project tier):
#   .loom/config.json
#   .loom-project/project.json
#
# Deliberately NOT `.loom-local/local.json` — that tier is gitignored and
# THE documented home for exactly the values this guard forbids elsewhere;
# scanning it would be self-defeating.
#
# Exit codes:
#   0 - no scanned (existing) file carries a non-allowlisted home-directory
#       absolute path (including "no file to scan").
#   1 - usage error (bad argument, missing jq, or a scanned file that does
#       not parse as JSON — this guard cannot make a determination on
#       unparseable content; see check-conflict-markers.sh / `jq -e .` for
#       that failure mode).
#   2 - one or more violations found — each is named with its dotted key
#       path, the offending value, and the source file.

set -uo pipefail

EXIT_OK=0
EXIT_USAGE=1
EXIT_VIOLATIONS=2

# Dotted key paths exempted from the home-directory-path check (exact match
# only — see the header comment for why this list must stay a closed set of
# individually-justified exceptions, not a pattern).
ALLOWLIST_KEYS=(
    "daemon.delegatedTo"
)

usage() {
    awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
}

is_allowlisted() {
    local key="$1" entry
    for entry in "${ALLOWLIST_KEYS[@]}"; do
        [[ "$key" == "$entry" ]] && return 0
    done
    return 1
}

# scan_file <path> — echoes one "VIOLATION\t<file>\t<key>\t<value>" line per
# offending key, or nothing when clean. Returns 1 (its actual function exit
# status, NOT a global — the caller invokes this via command substitution,
# which runs in a subshell, so a global assigned in here would never be
# visible to the caller) when the file is not valid JSON.
scan_file() {
    local file="$1"
    [[ -f "$file" ]] || return 0

    local pairs
    if ! pairs="$(jq -r '
            [paths(scalars) as $p
             | select(getpath($p) | type == "string")
             | ($p | map(tostring) | join(".")) as $key
             | "\($key)\t\(getpath($p))"
            ] | .[]
        ' "$file" 2>/dev/null)"; then
        echo "ERROR: $file does not parse as JSON — cannot check it for host-specific paths" >&2
        return 1
    fi

    local home_regex='^(/home/[^/]+|/Users/[^/]+|/root)(/|$)'
    local key val
    while IFS=$'\t' read -r key val; do
        [[ -n "$key" ]] || continue
        if [[ "$val" =~ $home_regex ]]; then
            if is_allowlisted "$key"; then
                continue
            fi
            printf 'VIOLATION\t%s\t%s\t%s\n' "$file" "$key" "$val"
        fi
    done <<< "$pairs"
}

# ---- Self-test --------------------------------------------------------------
#
# Proves the detector fires on the #6504 shape (a committed home-directory
# path) and stays silent on the legitimate near-misses, using synthetic
# fixtures. Runs the real scan path, not a reimplementation of it.
run_self_test() {
    local st_dir st_fail=0
    st_dir="$(mktemp -d)"
    trap 'rm -rf "$st_dir"' RETURN

    st_assert() {
        local label="$1" expected="$2" actual="$3"
        if [[ "$expected" == "$actual" ]]; then
            echo "  ok: $label"
        else
            echo "  FAIL: $label (expected '$expected', got '$actual')" >&2
            st_fail=1
        fi
    }

    # 1. A committed /home/ path (the #6504/#6499 shape) -> flagged.
    cat > "$st_dir/positive-home.json" <<'EOF'
{"observability": {"ingestKeyFile": "/home/ubuntu/.loom/observability/ingest.key"}}
EOF
    out="$(scan_file "$st_dir/positive-home.json")"
    st_assert "flags a committed /home/<user>/... path" "1" \
        "$(printf '%s\n' "$out" | grep -c '^VIOLATION')"
    st_assert "names the offending key" "1" \
        "$(printf '%s\n' "$out" | grep -c 'observability.ingestKeyFile')"

    # 2. A committed /Users/ path -> flagged.
    cat > "$st_dir/positive-users.json" <<'EOF'
{"safehouse": {"socket": "/Users/alice/.loom/safehoused/state/safehoused.sock"}}
EOF
    out="$(scan_file "$st_dir/positive-users.json")"
    st_assert "flags a committed /Users/<user>/... path" "1" \
        "$(printf '%s\n' "$out" | grep -c '^VIOLATION')"

    # 3. A /root/ path -> flagged.
    cat > "$st_dir/positive-root.json" <<'EOF'
{"worktree": {"root": "/root/scratch"}}
EOF
    out="$(scan_file "$st_dir/positive-root.json")"
    st_assert "flags a committed /root/... path" "1" \
        "$(printf '%s\n' "$out" | grep -c '^VIOLATION')"

    # 4. A relative / non-home value -> clean.
    cat > "$st_dir/negative-relative.json" <<'EOF'
{"buildGate": {"command": "bash .loom/scripts/build-gate.sh"}}
EOF
    out="$(scan_file "$st_dir/negative-relative.json")"
    st_assert "does not flag a relative command string" "0" \
        "$(printf '%s\n' "$out" | grep -c '^VIOLATION')"

    # 5. A non-home absolute system path -> clean (only home-relative paths
    #    are inherently host-specific; a shared system path like /etc/... is
    #    not, by design — see check-ingest-key-file.sh's parallel
    #    foreign-home-vs-system-path distinction).
    cat > "$st_dir/negative-system.json" <<'EOF'
{"observability": {"ingestKeyFile": "/etc/loom/observability-ingest.key"}}
EOF
    out="$(scan_file "$st_dir/negative-system.json")"
    st_assert "does not flag a non-home absolute system path" "0" \
        "$(printf '%s\n' "$out" | grep -c '^VIOLATION')"

    # 6. daemon.delegatedTo under a home dir -> allowlisted, clean.
    cat > "$st_dir/negative-allowlisted.json" <<'EOF'
{"daemon": {"delegatedTo": "/Users/alice/GitHub/other-repo"}}
EOF
    out="$(scan_file "$st_dir/negative-allowlisted.json")"
    st_assert "does not flag the allowlisted daemon.delegatedTo key" "0" \
        "$(printf '%s\n' "$out" | grep -c '^VIOLATION')"

    # 7. A DIFFERENT key at a home path is still flagged even alongside an
    #    allowlisted one in the same file (allowlist is per-key, not per-file).
    cat > "$st_dir/positive-mixed.json" <<'EOF'
{"daemon": {"delegatedTo": "/Users/alice/GitHub/other-repo"},
 "observability": {"ingestKeyFile": "/Users/alice/.loom/observability/ingest.key"}}
EOF
    out="$(scan_file "$st_dir/positive-mixed.json")"
    st_assert "allowlist is per-key, a sibling violation still fires" "1" \
        "$(printf '%s\n' "$out" | grep -c '^VIOLATION')"

    # 8. Missing file -> clean, not an error.
    out="$(scan_file "$st_dir/does-not-exist.json")"
    st_assert "a missing file is a silent no-op" "0" \
        "$(printf '%s\n' "$out" | grep -c '^VIOLATION')"

    # 9. Malformed JSON -> parse failure surfaced via the function's own exit
    #    status (the main loop below relies on this, not a global, since it
    #    calls scan_file via command substitution — a subshell).
    printf '{not valid json' > "$st_dir/malformed.json"
    scan_file "$st_dir/malformed.json" >/dev/null 2>&1
    st_assert "malformed JSON returns a non-zero exit status" "1" "$?"

    if [[ "$st_fail" -eq 1 ]]; then
        echo "Self-test FAILED" >&2
        return 1
    fi
    echo "Self-test passed"
    return 0
}

# ---- Arg parsing --------------------------------------------------------

FILES=()
SELF_TEST=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit "$EXIT_OK"
            ;;
        --self-test)
            SELF_TEST=1
            shift
            ;;
        -*)
            echo "check-config-no-host-paths.sh: unknown argument: $1" >&2
            echo "Run with --help for usage." >&2
            exit "$EXIT_USAGE"
            ;;
        *)
            FILES+=("$1")
            shift
            ;;
    esac
done

if ! command -v jq >/dev/null 2>&1; then
    echo "check-config-no-host-paths.sh: jq is required but not on PATH" >&2
    exit "$EXIT_USAGE"
fi

if [[ "$SELF_TEST" -eq 1 ]]; then
    if run_self_test; then
        exit "$EXIT_OK"
    else
        exit "$EXIT_VIOLATIONS"
    fi
fi

if [[ ${#FILES[@]} -eq 0 ]]; then
    REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
    FILES=(
        "$REPO_ROOT/.loom/config.json"
        "$REPO_ROOT/.loom-project/project.json"
    )
fi

ALL_OUT=""
ANY_PARSE_FAILED=0
for f in "${FILES[@]}"; do
    if ! out="$(scan_file "$f")"; then
        ANY_PARSE_FAILED=1
        continue
    fi
    [[ -n "$out" ]] && ALL_OUT="${ALL_OUT}${ALL_OUT:+$'\n'}${out}"
done

if [[ "$ANY_PARSE_FAILED" -eq 1 ]]; then
    exit "$EXIT_USAGE"
fi

if [[ -z "$ALL_OUT" ]]; then
    echo "PASS: no tracked config file carries a non-allowlisted home-directory absolute path"
    exit "$EXIT_OK"
fi

echo "check-config-no-host-paths.sh: found host-specific absolute path(s) committed to tracked config:" >&2
while IFS=$'\t' read -r _tag file key val; do
    [[ -n "$file" ]] || continue
    echo "  $file: $key = $val" >&2
done <<< "$ALL_OUT"
echo "" >&2
echo "A path under /home/<user>/, /Users/<user>/, or /root/ is correct on at most ONE host and" >&2
echo "silently wrong on every other host that pulls it (#5354, #5464, #6499, #6504). Move it to" >&2
echo "the gitignored .loom-local/local.json tier (or check whether its \$HOME-relative built-in" >&2
echo "default already does the right thing per-host, and simply delete the committed line) — see" >&2
echo "defaults/docs/fleet-config-lifecycle.md and docs/design/config-resolution-tiers.md §5." >&2
exit "$EXIT_VIOLATIONS"
