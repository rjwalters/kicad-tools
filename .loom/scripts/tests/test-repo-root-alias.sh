#!/usr/bin/env bash
# Asserts lib/loom-tools.sh is a working alias for lib/repo-root.sh (#12941):
# sourcing either defines _find_repo_root and run_loom_tool, under bash and zsh.
set -u
LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)"
FAILED=0
pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; FAILED=1; }

check_shell() {
    local sh="$1" file="$2" out
    command -v "$sh" >/dev/null 2>&1 || { echo "  SKIP: $sh not installed"; return; }
    out="$("$sh" -c '
        source "$1" || exit 10
        type _find_repo_root >/dev/null 2>&1 || exit 11
        type run_loom_tool >/dev/null 2>&1 || exit 12
        t="$(mktemp -d)"; mkdir -p "$t/.loom/sub"
        [ "$(_find_repo_root "$t/.loom/sub")" = "$t" ] || exit 13
        msg="$( (run_loom_tool tokens) 2>&1 )"; rc=$?
        [ "$rc" -eq 1 ] || exit 14
        case "$msg" in *"loom-daemon tokens"*) ;; *) exit 15 ;; esac
        rm -rf "$t"
    ' _ "$LIB/$file" 2>&1)"
    local rc=$?
    if [[ $rc -eq 0 ]]; then pass "$sh sources $file"; else fail "$sh sourcing $file failed (rc=$rc): $out"; fi
}

for sh in bash zsh; do
    for f in repo-root.sh loom-tools.sh; do
        check_shell "$sh" "$f"
    done
done

[[ $FAILED -eq 0 ]] && { echo "All tests passed"; exit 0; }
exit 1
