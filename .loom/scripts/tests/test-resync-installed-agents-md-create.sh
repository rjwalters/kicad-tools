#!/usr/bin/env bash
# test-resync-installed-agents-md-create.sh - resync creates an absent
# .loom/AGENTS.md (#13576)
#
# An install that predates .loom/AGENTS.md and was only ever resynced left the
# root AGENTS.md pointing at a file that never existed. resync-installed.sh now
# creates it when absent (and a root AGENTS.md exists), rendered like a fresh
# install, and never touches a present copy.
#
# Usage:
#   ./.loom/scripts/tests/test-resync-installed-agents-md-create.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/resync-installed.sh"
TEMPLATE_SRC="$(cd "$HELPERS_DIR/.." && pwd)/.loom/AGENTS.md"

source "$SCRIPT_DIR/lib/assert.sh"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync-agents-md.XXXXXX")"
# shellcheck disable=SC2329,SC2317  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# make_fixture <name> <with-root-agents-md yes|no> [with-metadata yes|no]
make_fixture() {
    local name="$1" with_root="$2" with_meta="${3:-yes}"
    local consumer="$WORKDIR/$name" src="$WORKDIR/$name-src"
    rm -rf "$consumer" "$src"

    mkdir -p "$src/defaults/hooks" "$src/defaults/.loom"
    printf 'A\n' > "$src/defaults/hooks/guard.sh"
    chmod +x "$src/defaults/hooks/guard.sh"
    cp "$TEMPLATE_SRC" "$src/defaults/.loom/AGENTS.md"
    printf '{\n  "version": "1.2.3"\n}\n' > "$src/package.json"
    git -C "$src" init -q
    git -C "$src" add -A >/dev/null 2>&1
    git -C "$src" commit -qm "source" >/dev/null 2>&1

    mkdir -p "$consumer/.loom/hooks"
    git -C "$consumer" init -q
    printf 'OLD\n' > "$consumer/.loom/hooks/guard.sh"
    if [[ "$with_meta" == yes ]]; then
        printf '{\n  "loom_version": "0.19.100",\n  "loom_commit": "aaaaaaaabbbbbbbbccccccccddddddddeeeeeeee",\n  "install_date": "2020-01-01",\n  "installed_files": []\n}\n' \
            > "$consumer/.loom/install-metadata.json"
    fi
    printf '%s\n' "$src" > "$consumer/.loom/loom-source-path"
    [[ "$with_root" == yes ]] && printf '# Agents\nSee .loom/AGENTS.md\n' > "$consumer/AGENTS.md"
    git -C "$consumer" add -A >/dev/null 2>&1
    git -C "$consumer" commit -qm "chore: install Loom v0.19.100" >/dev/null 2>&1
    echo "$consumer"
}

echo ""
echo "Test group 1: absent .loom/AGENTS.md is created, rendered, idempotent (#13576)"
C1="$(make_fixture create yes)"
OUT="$(cd "$C1" && bash "$SCRIPT" 2>&1)"
if [[ -f "$C1/.loom/AGENTS.md" ]]; then
    pass "(#13576) .loom/AGENTS.md created"
else
    fail "(#13576) .loom/AGENTS.md not created"
fi
assert_contains "$OUT" "created" "(#13576) output reports the creation"
if grep -q '{{' "$C1/.loom/AGENTS.md" 2>/dev/null; then
    fail "(#13576) unsubstituted placeholder left in .loom/AGENTS.md"
else
    pass "(#13576) no placeholders remain"
fi
if grep -q '2020-01-01' "$C1/.loom/AGENTS.md" 2>/dev/null; then
    pass "(#13576) install date taken from install-metadata.json"
else
    fail "(#13576) install date not taken from install-metadata.json"
fi
if grep -q '](\.loom/' "$C1/.loom/AGENTS.md" 2>/dev/null; then
    fail "(#13576) .loom/ links not localized"
else
    pass "(#13576) .loom/ links localized"
fi
BEFORE="$(cksum < "$C1/.loom/AGENTS.md")"
(cd "$C1" && bash "$SCRIPT" >/dev/null 2>&1)
AFTER="$(cksum < "$C1/.loom/AGENTS.md")"
assert_eq "$BEFORE" "$AFTER" "(#13576) second run is a byte-identical no-op"

echo ""
echo "Test group 2: a present .loom/AGENTS.md is untouched (#13576)"
C2="$(make_fixture present yes)"
printf 'CUSTOM\n' > "$C2/.loom/AGENTS.md"
(cd "$C2" && bash "$SCRIPT" >/dev/null 2>&1)
assert_eq "CUSTOM" "$(cat "$C2/.loom/AGENTS.md")" "(#13576) existing copy not overwritten"

echo ""
echo "Test group 3: --dry-run creates nothing and says would create (#13576)"
C3="$(make_fixture dry yes)"
OUT="$(cd "$C3" && bash "$SCRIPT" --dry-run 2>&1)"
if [[ -e "$C3/.loom/AGENTS.md" ]]; then
    fail "(#13576) dry-run wrote .loom/AGENTS.md"
else
    pass "(#13576) dry-run wrote nothing"
fi
assert_contains "$OUT" "would create" "(#13576) dry-run reports would create"

echo ""
echo "Test group 4: no root AGENTS.md -> nothing created (#13576)"
C4="$(make_fixture noroot no)"
(cd "$C4" && bash "$SCRIPT" >/dev/null 2>&1)
if [[ -e "$C4/.loom/AGENTS.md" ]]; then
    fail "(#13576) created .loom/AGENTS.md without a root AGENTS.md"
else
    pass "(#13576) not created without a root AGENTS.md"
fi

echo ""
echo "Test group 5: resync-ignore pin is honored (#13576)"
C5="$(make_fixture pinned yes)"
printf '.loom/AGENTS.md\n' > "$C5/.loom/resync-ignore"
(cd "$C5" && bash "$SCRIPT" >/dev/null 2>&1)
if [[ -e "$C5/.loom/AGENTS.md" ]]; then
    fail "(#13576) pinned .loom/AGENTS.md was created"
else
    pass "(#13576) pinned path not created"
fi

echo ""
echo "Test group 6: missing install-metadata.json falls back to today's date (#13576)"
C6="$(make_fixture nometa yes no)"
(cd "$C6" && bash "$SCRIPT" >/dev/null 2>&1)
if [[ -f "$C6/.loom/AGENTS.md" ]] && ! grep -q '{{' "$C6/.loom/AGENTS.md"; then
    pass "(#13576) created with no placeholders when metadata is absent"
else
    fail "(#13576) not created / placeholders left when metadata is absent"
fi

# --- summary -----------------------------------------------------------------
echo ""
echo "========================================"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
echo "========================================"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
