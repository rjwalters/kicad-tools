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

# --- group 4: retired generated skills are removed, ownership-gated (#12507) ---
echo "Test group 4: retired marker-carrying skills are removed; everything else is kept (#12507)"
MARK="<!-- loom-managed-skill -->"
REPO4="$(make_fixture)"
S4="$REPO4/.agents/skills"
mkdir -p "$S4/loom-foo" "$S4/loom-retired" "$S4/loom-detached" "$S4/loom-pinned" \
         "$S4/loom-extra" "$WORKDIR/ext-target" "$S4/custom"
cp "$REPO4/defaults/.agents/skills/loom-foo/SKILL.md" "$S4/loom-foo/SKILL.md"
printf '%s\n%s\n' "---" "$MARK" > "$S4/loom-retired/SKILL.md"
printf 'mine\n' > "$S4/loom-detached/SKILL.md"
printf '%s\n%s\n' "---" "$MARK" > "$S4/loom-pinned/SKILL.md"
printf '%s\n%s\n' "---" "$MARK" > "$S4/loom-extra/SKILL.md"
printf 'notes\n' > "$S4/loom-extra/NOTES.md"
printf '%s\n%s\n' "---" "$MARK" > "$S4/custom/SKILL.md"
ln -s "$WORKDIR/ext-target" "$S4/loom-linkdir"
printf '%s\n' "---" "$MARK" > "$WORKDIR/ext-target/SKILL.md"
mkdir -p "$S4/loom-linkfile"
ln -s "$S4/loom-retired/SKILL.md" "$S4/loom-linkfile/SKILL.md"
printf 'agents-skills/loom-pinned/SKILL.md\n' > "$REPO4/.loom/resync-ignore"
git -C "$REPO4" add -A >/dev/null 2>&1; git -C "$REPO4" commit -qm fixture >/dev/null 2>&1

OUT="$(cd "$REPO4" && bash "$SCRIPT" --dry-run 2>&1)"
if [[ -f "$S4/loom-retired/SKILL.md" ]] && grep -q "would remove.*loom-retired" <<<"$OUT"; then
    pass "(#12507) --dry-run reports the retired skill and changes nothing"
else
    fail "(#12507) --dry-run behaved wrongly; out=$OUT"
fi

OUT="$(cd "$REPO4" && bash "$SCRIPT" 2>&1)"
if [[ ! -e "$S4/loom-retired" ]]; then
    pass "(#12507) retired marker-carrying skill and its now-empty dir are removed"
else
    fail "(#12507) retired skill not removed; out=$OUT"
fi
if [[ -f "$S4/loom-foo/SKILL.md" && -f "$S4/loom-detached/SKILL.md" && -f "$S4/loom-pinned/SKILL.md" \
   && -f "$S4/custom/SKILL.md" && -L "$S4/loom-linkdir" && -f "$WORKDIR/ext-target/SKILL.md" \
   && -L "$S4/loom-linkfile/SKILL.md" ]]; then
    pass "(#12507) current, detached, pinned, non-loom-, and symlinked skills are preserved"
else
    fail "(#12507) a preserved skill was removed; out=$OUT"
fi
if [[ ! -e "$S4/loom-extra/SKILL.md" && -f "$S4/loom-extra/NOTES.md" ]]; then
    pass "(#12507) a retired dir with extra files keeps the extra file (no rm -r)"
else
    fail "(#12507) retired dir with extra file handled wrongly; out=$OUT"
fi
OUT2="$(cd "$REPO4" && bash "$SCRIPT" 2>&1)"
if ! grep -q "retired generated skill" <<<"$OUT2" || ! grep -E "^ +removed .*loom-retired" <<<"$OUT2" >/dev/null; then
    pass "(#12507) a second resync does not remove anything again"
else
    fail "(#12507) second resync not idempotent; out=$OUT2"
fi

# invalid source: empty skills dir must not wipe installed skills
REPO5="$(make_fixture)"
mkdir -p "$REPO5/.agents/skills/loom-old"
printf '%s\n%s\n' "---" "$MARK" > "$REPO5/.agents/skills/loom-old/SKILL.md"
rm -rf "$REPO5/defaults/.agents/skills/loom-foo"
git -C "$REPO5" add -A >/dev/null 2>&1; git -C "$REPO5" commit -qm fixture >/dev/null 2>&1
OUT="$(cd "$REPO5" && bash "$SCRIPT" 2>&1)"
if [[ -f "$REPO5/.agents/skills/loom-old/SKILL.md" ]]; then
    pass "(#12507) an empty source skill set never triggers removal"
else
    fail "(#12507) skills removed against an empty source; out=$OUT"
fi

# --- summary ------------------------------------------------------------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    exit 1
fi
exit 0
