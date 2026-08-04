#!/usr/bin/env bash
# test-config-tiers-cli-migration.sh — Tests for issue #4062: migrating the
# nine Bash config readers under defaults/scripts/ onto config-resolver.sh.
#
# Covers the CLI/script call sites NOT already covered by
# test-config-resolver.sh, test-worktree-root-override.sh,
# test-worktree-nested-symlinks.sh, or test-forge-helpers.sh:
#
#   1. loom-status.sh picks up terminals[] supplied ONLY via
#      .loom-project/project.json (no legacy .loom/config.json at all).
#   2. validate-roles.sh picks up terminals[] / roleFile values from the
#      project tier alone, and its `--json` machine-readable output reflects
#      them.
#   3. validate-roles.sh hard-fails (both --json and human branches) when NO
#      tier supplies a non-empty terminals array — the Trap 3 retargeted
#      precondition ("no terminals in the effective config" replacing "the
#      legacy file is missing").
#   4. loom-start.sh hard-fails, loudly and non-zero, when an EXISTING tier
#      file is malformed JSON — the regression guard called out in the issue:
#      loom_resolve_config soft-fails a malformed tier to `{}` silently, which
#      would otherwise make a typo'd config silently start zero terminals.
#   5. loom-start.sh hard-fails when no tier supplies terminals, and
#      succeeds (--dry-run) when terminals come from the project tier alone.
#   6. forge-helpers.sh's forge_detect / _load_gitea_config resolve config
#      from the CANONICAL repo root when invoked from inside a linked git
#      worktree, never the worktree's own (possibly divergent) checkout --
#      the root-resolution decision recorded in epic #4081 (canonical repo
#      root via `git rev-parse --git-common-dir`, mirroring the #3938
#      spawn-claude.sh precedent).
#
# IMPORTANT: these are #!/usr/bin/env bash scripts. Run this file (and any
# individual assertion) via `bash`, never sourced from an interactive zsh
# shell -- under zsh, _loom_config_soft_read silently returns {} for a valid
# config (looks exactly like a resolver bug; it isn't). See config-resolver.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

LOOM_STATUS_SH="$SCRIPTS_DIR/cli/loom-status.sh"
LOOM_START_SH="$SCRIPTS_DIR/cli/loom-start.sh"
VALIDATE_ROLES_SH="$SCRIPTS_DIR/validate-roles.sh"
FORGE_HELPERS_LIB="$SCRIPTS_DIR/lib/forge-helpers.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

if ! command -v jq >/dev/null 2>&1; then
    echo "jq not found on PATH -- skipping test-config-tiers-cli-migration.sh"
    exit 0
fi

# A plain workspace anchor: find_repo_root()/find_workspace_root() in these
# scripts only require a `.loom` directory to exist -- no git repo needed
# unless a test specifically exercises worktree/canonical-root resolution.
new_workspace() {
    local dir
    dir=$(mktemp -d)
    mkdir -p "$dir/.loom"
    echo "$dir"
}

# --- Test 1: loom-status.sh --json picks up terminals from the project tier alone ---
echo "Test 1: loom-status.sh --json -- terminals from .loom-project/project.json alone"
ws=$(new_workspace)
mkdir -p "$ws/.loom-project"
cat > "$ws/.loom-project/project.json" <<'JSON'
{"terminals": [{"id": "cfgtier-t1", "name": "Builder", "roleConfig": {"roleFile": "builder.md"}}]}
JSON
# stdout only: loom_locate_daemon_bin's resolution diagnostic (#4997) lands on
# stderr and, on a host where it fires, would land ahead of the JSON payload
# under a merged 2>&1 capture and break the jq -e parse below even though the
# JSON on stdout is correct (#5183).
out=$(cd "$ws" && "$LOOM_STATUS_SH" --json 2>/dev/null) || true
if echo "$out" | jq -e '(.stopped + .running) | map(select(.id == "cfgtier-t1")) | length == 1' >/dev/null 2>&1; then
    pass "loom-status.sh --json lists a terminal sourced only from the project tier"
else
    fail "loom-status.sh --json did not surface the project-tier-only terminal (got: $out)"
fi
rm -rf "$ws"

# --- Test 2: validate-roles.sh picks up roles from the project tier alone ---
echo ""
echo "Test 2: validate-roles.sh --json -- roles from .loom-project/project.json alone"
ws=$(new_workspace)
mkdir -p "$ws/.loom-project"
cat > "$ws/.loom-project/project.json" <<'JSON'
{"terminals": [{"roleConfig": {"roleFile": "judge.md"}}, {"roleConfig": {"roleFile": "champion.md"}}]}
JSON
out=$(cd "$ws" && "$VALIDATE_ROLES_SH" --json 2>&1) || true
if echo "$out" | jq -e '.configured_roles | index("judge") != null and index("champion") != null' >/dev/null 2>&1; then
    pass "validate-roles.sh --json resolves configured_roles from the project tier alone"
else
    fail "validate-roles.sh --json did not resolve project-tier roles (got: $out)"
fi
rm -rf "$ws"

# --- Test 3: validate-roles.sh hard-fails when no tier supplies terminals ---
echo ""
echo "Test 3: validate-roles.sh hard-fails when no tier supplies a non-empty terminals array"
ws=$(new_workspace)  # .loom/ exists, but no config.json anywhere in any tier

set +e
out_human=$(cd "$ws" && "$VALIDATE_ROLES_SH" 2>&1)
status_human=$?
out_json=$(cd "$ws" && "$VALIDATE_ROLES_SH" --json 2>&1)
status_json=$?
set -e

if [[ "$status_human" -ne 0 ]]; then
    pass "validate-roles.sh (human) exits non-zero with no terminals in any tier"
else
    fail "validate-roles.sh (human) exited 0 with no terminals anywhere"
fi
if echo "$out_human" | grep -qi "terminals"; then
    pass "validate-roles.sh (human) error names terminals, not a single legacy path"
else
    fail "validate-roles.sh (human) error message unexpected: $out_human"
fi
if [[ "$status_json" -ne 0 ]]; then
    pass "validate-roles.sh --json exits non-zero with no terminals in any tier"
else
    fail "validate-roles.sh --json exited 0 with no terminals anywhere"
fi
if echo "$out_json" | jq -e '.error' >/dev/null 2>&1; then
    pass "validate-roles.sh --json still emits a JSON object with .error on the retargeted failure"
else
    fail "validate-roles.sh --json did not emit valid JSON with .error (got: $out_json)"
fi
rm -rf "$ws"

# --- Test 4: validate-roles.sh hard-fails on a PRESENT-but-empty terminals array too ---
echo ""
echo "Test 4: validate-roles.sh hard-fails when the only tier's terminals array is empty"
ws=$(new_workspace)
echo '{"terminals": []}' > "$ws/.loom/config.json"
set +e
out=$(cd "$ws" && "$VALIDATE_ROLES_SH" 2>&1)
status=$?
set -e
if [[ "$status" -ne 0 ]]; then
    pass "validate-roles.sh treats a present-but-empty terminals array as absent (matches Rust has_terminals)"
else
    fail "validate-roles.sh exited 0 for an empty terminals array"
fi
rm -rf "$ws"

# The remaining tests exercise loom-start.sh, which additionally requires
# tmux + claude on PATH (check_dependencies runs before config resolution).
# Skip gracefully rather than falsely reporting a config-resolution failure
# when the real blocker is a missing unrelated dependency.
if ! command -v tmux >/dev/null 2>&1 || ! command -v claude >/dev/null 2>&1; then
    echo ""
    echo "tmux and/or claude not found on PATH -- skipping loom-start.sh tests (5-7)"
else
    # --- Test 5: loom-start.sh malformed tier JSON -> loud non-zero failure ---
    echo ""
    echo "Test 5: loom-start.sh fails loudly on a malformed EXISTING tier file (regression guard)"
    ws=$(new_workspace)
    printf '{ "terminals": [ invalid json' > "$ws/.loom/config.json"
    set +e
    out=$(cd "$ws" && "$LOOM_START_SH" --dry-run 2>&1)
    status=$?
    set -e
    if [[ "$status" -ne 0 ]]; then
        pass "loom-start.sh exits non-zero on a malformed tier file"
    else
        fail "loom-start.sh exited 0 despite malformed .loom/config.json (silent-degrade regression)"
    fi
    if echo "$out" | grep -qi "Invalid JSON"; then
        pass "loom-start.sh names the malformed file explicitly (does not silently report 'no terminals')"
    else
        fail "loom-start.sh malformed-JSON error message unexpected: $out"
    fi
    rm -rf "$ws"

    # --- Test 6: loom-start.sh hard-fails when no tier supplies terminals ---
    echo ""
    echo "Test 6: loom-start.sh hard-fails when no tier supplies a non-empty terminals array"
    ws=$(new_workspace)
    set +e
    out=$(cd "$ws" && "$LOOM_START_SH" --dry-run 2>&1)
    status=$?
    set -e
    if [[ "$status" -ne 0 ]]; then
        pass "loom-start.sh exits non-zero with no terminals in any tier"
    else
        fail "loom-start.sh exited 0 with no terminals anywhere"
    fi
    if echo "$out" | grep -qi "terminals"; then
        pass "loom-start.sh error names terminals, not a single legacy path"
    else
        fail "loom-start.sh error message unexpected: $out"
    fi
    rm -rf "$ws"

    # --- Test 7: loom-start.sh --dry-run picks up terminals from the project tier alone ---
    echo ""
    echo "Test 7: loom-start.sh --dry-run -- terminals from .loom-project/project.json alone"
    ws=$(new_workspace)
    mkdir -p "$ws/.loom-project"
    cat > "$ws/.loom-project/project.json" <<'JSON'
{"terminals": [{"id": "cfgtier-start-t1", "name": "Builder", "roleConfig": {"roleFile": "builder.md"}}]}
JSON
    set +e
    out=$(cd "$ws" && "$LOOM_START_SH" --dry-run 2>&1)
    status=$?
    set -e
    if [[ "$status" -eq 0 ]]; then
        pass "loom-start.sh --dry-run succeeds with terminals from the project tier alone"
    else
        fail "loom-start.sh --dry-run failed with project-tier-only terminals (exit $status): $out"
    fi
    if echo "$out" | grep -q "cfgtier-start-t1"; then
        pass "loom-start.sh --dry-run lists the project-tier-only terminal"
    else
        fail "loom-start.sh --dry-run did not list the project-tier terminal: $out"
    fi
    rm -rf "$ws"
fi

# --- Test 8: forge-helpers.sh resolves from the CANONICAL repo root, never
#     the worktree's own (possibly divergent) checkout, per the #4081
#     decision record (git rev-parse --git-common-dir, mirroring the #3938
#     spawn-claude.sh precedent). ---
echo ""
echo "Test 8: forge_detect / _load_gitea_config resolve from canonical repo root inside a worktree"
if ! command -v git >/dev/null 2>&1; then
    echo "git not found on PATH -- skipping Test 8"
else
    main_repo=$(mktemp -d)
    git init -q -b main "$main_repo"
    (
        cd "$main_repo"
        git config user.email t@t
        git config user.name t
        mkdir -p .loom
        cat > .loom/config.json <<'JSON'
{"forge": {"type": "gitea", "gitea": {"url": "https://gitea.example.com", "token": "tok-main-root", "username": "main-user"}}}
JSON
        git add .loom/config.json
        git commit -q -m "add gitea config"
    )

    worktree_dir="${main_repo}-wt"
    (cd "$main_repo" && git worktree add -q "$worktree_dir" -b wt-branch)

    # Diverge: delete the WORKTREE-LOCAL copy of .loom/config.json (git
    # checks it out into every worktree since it's tracked). If forge_detect
    # wrongly resolved against the worktree's own CWD instead of the
    # canonical repo root, this file would now be absent there and detection
    # would fall through to the git-remote autodetect / github default --
    # exposing exactly the regression this test guards against.
    rm -f "$worktree_dir/.loom/config.json"

    result=$(
        cd "$worktree_dir"
        unset REPO_ROOT WORKSPACE_ROOT LOOM_FORGE_TYPE 2>/dev/null || true
        # shellcheck source=../lib/forge-helpers.sh
        source "$FORGE_HELPERS_LIB"
        forge_detect
        echo "TYPE=$FORGE_TYPE"
        echo "TOKEN=$_GITEA_TOKEN"
        echo "URL=$_GITEA_BASE_URL"
    )

    if echo "$result" | grep -q "^TYPE=gitea$"; then
        pass "forge_detect resolves forge.type from the canonical repo root inside a worktree"
    else
        fail "forge_detect did not resolve gitea from canonical root (got: $result)"
    fi
    if echo "$result" | grep -q "^TOKEN=tok-main-root$"; then
        pass "_load_gitea_config resolves the token from the canonical repo root, not worktree CWD"
    else
        fail "_load_gitea_config did not resolve the main-repo token (got: $result)"
    fi

    rm -rf "$main_repo" "$worktree_dir"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
