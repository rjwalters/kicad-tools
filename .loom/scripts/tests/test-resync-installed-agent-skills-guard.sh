#!/usr/bin/env bash
# test-resync-installed-agent-skills-guard.sh - marker-gated .agents/skills/
# resync in resync-installed.sh (#8673)
#
# Split out as its own file rather than grown into test-resync-installed.sh
# (frozen by the file-size ratchet, .loom/docs/file-size-policy.md) — same
# pattern as test-resync-installed-local-fix-guard.sh.
#
# Covers resync_agent_skills()'s marker gate: a destination
# .agents/skills/loom-<name>/SKILL.md WITHOUT the `<!-- loom-managed-skill -->`
# marker (consumer-authored, or a marker deliberately stripped to detach a
# file from generation) must be left alone and reported as skipped, never
# silently overwritten — the install-time counterpart of
# loom-daemon/src/init/scaffolding.rs's install_agent_skills(). A file that
# DOES carry the marker resyncs normally (ordinary sync_one() rules apply,
# including backfilling the whole surface when the destination directory does
# not exist yet at all — the same "unconditional" posture .loom/runtimes/ has,
# #4688).
#
# Usage:
#   ./.loom/scripts/tests/test-resync-installed-agent-skills-guard.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/resync-installed.sh"

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
}

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync-agentskills.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# --- fixture builder (trimmed copy of test-resync-installed.sh's) -----------
make_fixture() {
    local repo="$WORKDIR/repo"
    rm -rf "$repo"
    mkdir -p "$repo/defaults/hooks" "$repo/defaults/scripts/lib" \
             "$repo/.loom/hooks" "$repo/.loom/scripts/lib" \
             "$repo/defaults/.agents/skills/loom-foo"
    git -C "$repo" init -q

    printf 'A\n' > "$repo/defaults/hooks/guard.sh"
    printf 'S\n' > "$repo/defaults/scripts/foo.sh"
    printf 'L\n' > "$repo/defaults/scripts/lib/bar.sh"
    chmod +x "$repo/defaults/hooks/guard.sh" "$repo/defaults/scripts/foo.sh" \
             "$repo/defaults/scripts/lib/bar.sh"

    printf '%s\n' "---" "name: loom-foo" 'description: "Foo"' "---" \
        "<!-- loom-managed-skill -->" "<!-- GENERATED FILE -->" "" "# Foo" "" "Body." \
        > "$repo/defaults/.agents/skills/loom-foo/SKILL.md"

    printf '{\n  "version": "9.9.9"\n}\n' > "$repo/package.json"
    printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
        "$repo" > "$repo/.loom/install-metadata.json"

    git -C "$repo" add -A >/dev/null 2>&1
    git -C "$repo" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1

    echo "$repo"
}

# --- group 1: no destination at all -> backfilled (unconditional, like .loom/runtimes/) ---
echo "Test group 1: .agents/skills/ is backfilled when the destination does not exist yet (#8673)"
REPO="$(make_fixture)"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
if [[ -f "$REPO/.agents/skills/loom-foo/SKILL.md" ]] && \
   diff -q "$REPO/defaults/.agents/skills/loom-foo/SKILL.md" "$REPO/.agents/skills/loom-foo/SKILL.md" >/dev/null 2>&1; then
    pass "(#8673) a missing .agents/skills/ tree is created from defaults/"
else
    fail "(#8673) .agents/skills/loom-foo/SKILL.md was not backfilled; out=$OUT"
fi

# --- group 2: destination carries the marker and differs -> updated normally ---
echo "Test group 2: a marker-carrying installed SKILL.md that has drifted is updated (#8673)"
REPO2="$(make_fixture)"
mkdir -p "$REPO2/.agents/skills/loom-foo"
printf '%s\n' "---" "name: loom-foo" 'description: "Foo"' "---" \
    "<!-- loom-managed-skill -->" "<!-- STALE -->" > "$REPO2/.agents/skills/loom-foo/SKILL.md"
OUT="$(cd "$REPO2" && bash "$SCRIPT" 2>&1)"
if diff -q "$REPO2/defaults/.agents/skills/loom-foo/SKILL.md" "$REPO2/.agents/skills/loom-foo/SKILL.md" >/dev/null 2>&1; then
    pass "(#8673) a stale marker-carrying installed SKILL.md is refreshed from defaults/"
else
    fail "(#8673) a stale marker-carrying installed SKILL.md was not refreshed; out=$OUT"
fi

# --- group 3: destination has NO marker -> left alone, reported as skipped ---
echo "Test group 3: an installed SKILL.md with no ownership marker is never overwritten (#8673)"
REPO3="$(make_fixture)"
mkdir -p "$REPO3/.agents/skills/loom-foo"
printf 'Hand-authored content — not Loom-generated.\n' > "$REPO3/.agents/skills/loom-foo/SKILL.md"
OUT="$(cd "$REPO3" && bash "$SCRIPT" 2>&1)"
if [[ "$(cat "$REPO3/.agents/skills/loom-foo/SKILL.md")" == "Hand-authored content — not Loom-generated." ]]; then
    pass "(#8673) a marker-less installed SKILL.md is left byte-for-byte untouched"
else
    fail "(#8673) a marker-less installed SKILL.md was overwritten; out=$OUT"
fi
if grep -q "skipped" <<<"$OUT" && grep -q "loom-managed-skill" <<<"$OUT"; then
    pass "(#8673) the skip is reported and names the marker"
else
    fail "(#8673) the skip was not reported with the marker name; out=$OUT"
fi

# --- summary ------------------------------------------------------------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    exit 1
fi
exit 0
