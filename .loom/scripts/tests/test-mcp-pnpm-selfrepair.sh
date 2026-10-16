#!/usr/bin/env bash
# test-mcp-pnpm-selfrepair.sh — Tests for pnpm-aware MCP self-repair (#6779).
#
# Before this fix, claude-wrapper.sh's `_try_mcp_rebuild` self-repaired ANY
# MCP server's dependency tree with `npm ci`, gated only on a
# `package-lock.json` being present on disk — never on whether the package
# was actually npm-managed. A pnpm-managed server (declaring
# `"packageManager": "pnpm@..."` and tracking `pnpm-lock.yaml`) whose
# `package-lock.json` was untracked residue from an EARLIER wrong-manager
# self-repair got re-`npm ci`'d forever: pnpm install -> judged unusable
# (`_mcp_node_modules_unusable` doesn't recognize a pnpm layout) -> npm ci ->
# fresh untracked package-lock.json -> repeat. `npm run build` then failed
# with an unhelpful "sh: tsc: command not found" (typescript is a
# devDependency, and the npm-ci-against-a-stale-lockfile install doesn't
# reliably leave its bin in place).
#
# Covers:
#   A. `_mcp_resolve_package_manager` — honors `packageManager` in
#      package.json, prefers a git-tracked pnpm-lock.yaml/yarn.lock over an
#      untracked package-lock.json, and defaults to npm.
#   B. `_mcp_node_modules_unusable` — recognizes a valid pnpm layout
#      (node_modules/.pnpm or node_modules/.modules.yaml) as usable, and
#      still detects a genuinely broken npm-managed tree exactly as before
#      (no regression to the pre-existing npm path).
#   C. `_try_mcp_rebuild` end-to-end — a pnpm-managed package self-repairs
#      with `pnpm install --frozen-lockfile` / `pnpm run build` (never `npm
#      ci`/`npm run build`) and never creates a fresh package-lock.json; a
#      correctly-installed pnpm tree never triggers the install step at all;
#      an npm-managed package with a genuinely broken tree still self-repairs
#      via `npm ci` unchanged; a build failure after a successful self-repair
#      names the likely cause instead of only the raw "command not found".
#
# Style matches test-mcp-config.sh Section 8 (its own npm-ci self-repair
# suite, #5032) — fake `pnpm`/`npm` binaries logging every invocation to a
# file, driven via CLAUDE_WRAPPER_SOURCE_ONLY=1 so the functions under test
# can be called directly without the full CLI entry point running.
#
# Usage:
#   ./.loom/scripts/tests/test-mcp-pnpm-selfrepair.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WRAPPER="$SCRIPTS_DIR/claude-wrapper.sh"

# shellcheck source=lib/assert.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"
YELLOW='\033[1;33m'


assert_contains() {
    local needle="$1" haystack="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

assert_not_contains() {
    local needle="$1" haystack="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" != *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

HAVE_PYTHON3=false
command -v python3 >/dev/null 2>&1 && HAVE_PYTHON3=true
HAVE_GIT=false
command -v git >/dev/null 2>&1 && HAVE_GIT=true
HAVE_NODE=false
command -v node >/dev/null 2>&1 && HAVE_NODE=true

# ============================================================
# Section A: _mcp_resolve_package_manager
# ============================================================
echo ""
echo "Testing _mcp_resolve_package_manager (#6779)..."

if $HAVE_PYTHON3; then
    _resolve() { # $1 = pkg_dir
        CLAUDE_WRAPPER_SOURCE_ONLY=1 bash -c '
            source "$1"
            _mcp_resolve_package_manager "$2"
        ' _ "$WRAPPER" "$1"
    }

    # A1: an explicit "packageManager" field wins outright, even with an
    # npm lockfile sitting right next to it.
    _a1="$(mktemp -d)"
    cat >"$_a1/package.json" <<'JSON'
{ "name": "squad", "packageManager": "pnpm@11.20.0" }
JSON
    echo '{"lockfileVersion":3}' >"$_a1/package-lock.json"
    assert_eq "pnpm" "$(_resolve "$_a1")" \
        "A1: explicit packageManager field wins over an npm lockfile on disk"
    rm -rf "$_a1"

    # A2: explicit npm declaration is honored too (not just pnpm).
    _a2="$(mktemp -d)"
    cat >"$_a2/package.json" <<'JSON'
{ "name": "squad", "packageManager": "npm@10.5.0" }
JSON
    assert_eq "npm" "$(_resolve "$_a2")" \
        "A2: explicit npm packageManager declaration is honored"
    rm -rf "$_a2"

    # A3: no declaration, no lockfiles at all -> npm default (unchanged).
    _a3="$(mktemp -d)"
    cat >"$_a3/package.json" <<'JSON'
{ "name": "squad" }
JSON
    assert_eq "npm" "$(_resolve "$_a3")" \
        "A3: no declaration and no lockfile falls back to npm (unchanged default)"
    rm -rf "$_a3"

    # A4: no declaration, only package-lock.json -> npm (unchanged existing
    # behavior for a genuinely npm-managed package).
    _a4="$(mktemp -d)"
    cat >"$_a4/package.json" <<'JSON'
{ "name": "squad" }
JSON
    echo '{"lockfileVersion":3}' >"$_a4/package-lock.json"
    assert_eq "npm" "$(_resolve "$_a4")" \
        "A4: no declaration, only package-lock.json present -> npm"
    rm -rf "$_a4"

    if $HAVE_GIT; then
        # A5: THE EXACT REPRO from #6779 — no packageManager field, a
        # git-TRACKED pnpm-lock.yaml, and an UNTRACKED package-lock.json
        # (residue from an earlier wrong-manager self-repair). Must resolve
        # to pnpm, not npm.
        _a5="$(mktemp -d)"
        cat >"$_a5/package.json" <<'JSON'
{ "name": "squad" }
JSON
        echo "lockfileVersion: '9.0'" >"$_a5/pnpm-lock.yaml"
        git init -q "$_a5"
        git -C "$_a5" config user.email t@t.com
        git -C "$_a5" config user.name t
        git -C "$_a5" add package.json pnpm-lock.yaml
        git -C "$_a5" commit -qm seed
        # The untracked residue, created AFTER the commit above.
        echo '{"lockfileVersion":3}' >"$_a5/package-lock.json"
        assert_eq "pnpm" "$(_resolve "$_a5")" \
            "A5 (#6779 exact repro): tracked pnpm-lock.yaml beats an untracked package-lock.json"
        rm -rf "$_a5"

        # A6: git-tracked yarn.lock also beats an untracked package-lock.json.
        _a6="$(mktemp -d)"
        cat >"$_a6/package.json" <<'JSON'
{ "name": "squad" }
JSON
        echo "# yarn lockfile v1" >"$_a6/yarn.lock"
        git init -q "$_a6"
        git -C "$_a6" config user.email t@t.com
        git -C "$_a6" config user.name t
        git -C "$_a6" add package.json yarn.lock
        git -C "$_a6" commit -qm seed
        echo '{"lockfileVersion":3}' >"$_a6/package-lock.json"
        assert_eq "yarn" "$(_resolve "$_a6")" \
            "A6: tracked yarn.lock beats an untracked package-lock.json"
        rm -rf "$_a6"
    else
        echo -e "  ${YELLOW}SKIP${NC}: A5/A6 git-tracked-lockfile tests (git not installed)"
    fi

    # A7: no git repo at all (fails open to raw presence) — pnpm-lock.yaml on
    # disk still outranks package-lock.json even with no git history to
    # consult, so a non-git MCP clone doesn't regress to npm.
    _a7="$(mktemp -d)"
    cat >"$_a7/package.json" <<'JSON'
{ "name": "squad" }
JSON
    echo "lockfileVersion: '9.0'" >"$_a7/pnpm-lock.yaml"
    echo '{"lockfileVersion":3}' >"$_a7/package-lock.json"
    assert_eq "pnpm" "$(_resolve "$_a7")" \
        "A7: no git repo — pnpm-lock.yaml presence still outranks package-lock.json"
    rm -rf "$_a7"
else
    echo -e "  ${YELLOW}SKIP${NC}: _mcp_resolve_package_manager tests (python3 not installed)"
fi

# ============================================================
# Section B: _mcp_node_modules_unusable pnpm-awareness
# ============================================================
echo ""
echo "Testing _mcp_node_modules_unusable pnpm-awareness (#6779)..."

if $HAVE_PYTHON3; then
    _unusable() { # $1 = pkg_dir -> echoes RC
        CLAUDE_WRAPPER_SOURCE_ONLY=1 bash -c '
            source "$1"
            rc=0
            _mcp_node_modules_unusable "$2" || rc=$?
            echo "$rc"
        ' _ "$WRAPPER" "$1"
    }

    # B1: a correctly-installed pnpm tree (node_modules/.pnpm present and
    # non-empty) is USABLE (rc 1) — must NOT fall through to the npm-shaped
    # per-dependency walk, which is exactly what misfired before #6779.
    _b1="$(mktemp -d)"
    cat >"$_b1/package.json" <<'JSON'
{ "name": "squad", "packageManager": "pnpm@11.20.0", "dependencies": {"@modelcontextprotocol/sdk": "^1.0.0"} }
JSON
    mkdir -p "$_b1/node_modules/.pnpm/@modelcontextprotocol+sdk@1.0.0"
    assert_eq "1" "$(_unusable "$_b1")" \
        "B1: valid pnpm layout (node_modules/.pnpm present) is usable, self-repair does not fire"
    rm -rf "$_b1"

    # B2: pnpm's node-linker=hoisted layout has no .pnpm dir but still writes
    # node_modules/.modules.yaml — also usable.
    _b2="$(mktemp -d)"
    cat >"$_b2/package.json" <<'JSON'
{ "name": "squad", "packageManager": "pnpm@11.20.0" }
JSON
    mkdir -p "$_b2/node_modules"
    : >"$_b2/node_modules/.modules.yaml"
    : >"$_b2/node_modules/some-hoisted-file"
    assert_eq "1" "$(_unusable "$_b2")" \
        "B2: pnpm hoisted layout (.modules.yaml present, no .pnpm dir) is usable"
    rm -rf "$_b2"

    # B3: pnpm-managed but node_modules has neither marker -> genuinely
    # unusable (rc 0), self-repair should fire.
    _b3="$(mktemp -d)"
    cat >"$_b3/package.json" <<'JSON'
{ "name": "squad", "packageManager": "pnpm@11.20.0" }
JSON
    mkdir -p "$_b3/node_modules"
    : >"$_b3/node_modules/leftover-junk"
    assert_eq "0" "$(_unusable "$_b3")" \
        "B3: pnpm-managed tree with no pnpm install marker is unusable"
    rm -rf "$_b3"

    # B4 (regression, #5032): npm-managed, genuinely broken half-install
    # (empty dependency directory) is still detected unusable exactly as
    # before this fix.
    _b4="$(mktemp -d)"
    cat >"$_b4/package.json" <<'JSON'
{ "name": "mcp-loom", "dependencies": {"@modelcontextprotocol/sdk": "^1.0.0"}, "devDependencies": {"typescript": "^5.0.0"} }
JSON
    mkdir -p "$_b4/node_modules/@modelcontextprotocol/sdk" "$_b4/node_modules/typescript"
    echo '{"name":"typescript"}' >"$_b4/node_modules/typescript/package.json"
    # sdk left as an EMPTY husk — the #5032 laptop-host root cause.
    assert_eq "0" "$(_unusable "$_b4")" \
        "B4 (no regression): npm half-install (empty dep dir) still detected unusable"
    rm -rf "$_b4"

    # B5 (regression, #5032): npm-managed, complete install is still usable.
    _b5="$(mktemp -d)"
    cat >"$_b5/package.json" <<'JSON'
{ "name": "mcp-loom", "dependencies": {"@modelcontextprotocol/sdk": "^1.0.0"} }
JSON
    mkdir -p "$_b5/node_modules/@modelcontextprotocol/sdk"
    echo '{"name":"@modelcontextprotocol/sdk"}' >"$_b5/node_modules/@modelcontextprotocol/sdk/package.json"
    assert_eq "1" "$(_unusable "$_b5")" \
        "B5 (no regression): npm complete install is still usable"
    rm -rf "$_b5"
else
    echo -e "  ${YELLOW}SKIP${NC}: _mcp_node_modules_unusable tests (python3 not installed)"
fi

# ============================================================
# Section C: _try_mcp_rebuild end-to-end
# ============================================================
echo ""
echo "Testing _try_mcp_rebuild pnpm/npm self-repair selection (#6779)..."

# A dist stub whose presence proves the smoke test ran against the
# PRE-rebuild bundle (never expected in the assertions below — a passing
# rebuild always overwrites this file before the smoke test runs).
_write_broken_dist() {
    local dir="$1"
    mkdir -p "$dir/dist"
    cat >"$dir/dist/index.js" <<'JS'
process.stderr.write("SMOKE_TEST_RAN_ON_STALE_BUNDLE\n");
process.exit(1);
JS
}

# Fake pnpm: records every invocation to $PNPM_STUB_LOG. `install` seeds a
# valid pnpm-shaped node_modules (including node_modules/.bin/tsc, so a
# following successful `run build` can "find" it); `run build` writes a
# healthy dist/index.js unless a marker file requests a specific failure.
_write_pnpm_stub() {
    local stub="$1"
    mkdir -p "$(dirname "$stub")"
    cat >"$stub" <<'STUB'
#!/usr/bin/env bash
echo "$*" >>"${PNPM_STUB_LOG}"
case "${1:-}" in
    install)
        if [[ -f "${PNPM_STUB_DIR}/install-fails" ]]; then
            echo "ERR_PNPM_FETCH_404  GET https://registry.npmjs.org/typescript: Not Found" >&2
            exit 1
        fi
        mkdir -p node_modules/.pnpm node_modules/.bin \
            node_modules/@modelcontextprotocol/sdk node_modules/typescript
        echo '{"name":"@modelcontextprotocol/sdk","version":"1.0.0"}' \
            >node_modules/@modelcontextprotocol/sdk/package.json
        echo '{"name":"typescript","version":"5.0.0"}' \
            >node_modules/typescript/package.json
        printf '#!/usr/bin/env bash\nexit 0\n' >node_modules/.bin/tsc
        chmod +x node_modules/.bin/tsc
        exit 0
        ;;
    run)
        if [[ -f "${PNPM_STUB_DIR}/build-fails-tool-missing" ]]; then
            echo "sh: tsc: command not found" >&2
            exit 127
        fi
        if [[ -f "${PNPM_STUB_DIR}/build-fails" ]]; then
            echo "src/index.ts(1,1): error TS1005: build source error" >&2
            exit 1
        fi
        mkdir -p dist
        cat >dist/index.js <<'JSEOF'
process.stderr.write("Loom MCP server running on stdio\n");
let buf = "";
process.stdin.on("data", (chunk) => {
    buf += chunk;
    if (buf.includes("\n")) {
        process.stdout.write(
            JSON.stringify({
                jsonrpc: "2.0",
                id: 1,
                result: { protocolVersion: "2024-11-05", capabilities: {}, serverInfo: { name: "stub", version: "0.0.0" } },
            }) + "\n"
        );
        process.exit(0);
    }
});
JSEOF
        exit 0
        ;;
esac
exit 0
STUB
    chmod +x "$stub"
}

# Fake npm, same shape as test-mcp-config.sh's Section 8 stub — kept
# independent (not sourced across test files) so this suite stands alone.
_write_npm_stub() {
    local stub="$1"
    mkdir -p "$(dirname "$stub")"
    cat >"$stub" <<'STUB'
#!/usr/bin/env bash
echo "$*" >>"${NPM_STUB_LOG}"
case "${1:-}" in
    ci)
        if [[ -f "${NPM_STUB_DIR}/ci-fails" ]]; then
            echo "npm ERR! network request to https://registry.npmjs.org failed" >&2
            exit 1
        fi
        mkdir -p node_modules/@modelcontextprotocol/sdk node_modules/typescript
        echo '{"name":"@modelcontextprotocol/sdk","version":"1.0.0"}' \
            >node_modules/@modelcontextprotocol/sdk/package.json
        echo '{"name":"typescript","version":"5.0.0"}' \
            >node_modules/typescript/package.json
        exit 0
        ;;
    run)
        mkdir -p dist
        cat >dist/index.js <<'JSEOF'
process.stderr.write("Loom MCP server running on stdio\n");
let buf = "";
process.stdin.on("data", (chunk) => {
    buf += chunk;
    if (buf.includes("\n")) {
        process.stdout.write(
            JSON.stringify({
                jsonrpc: "2.0",
                id: 1,
                result: { protocolVersion: "2024-11-05", capabilities: {}, serverInfo: { name: "stub", version: "0.0.0" } },
            }) + "\n"
        );
        process.exit(0);
    }
});
JSEOF
        exit 0
        ;;
esac
exit 0
STUB
    chmod +x "$stub"
}

_run_try_mcp_rebuild() { # $1 = mcp_entry path, remaining args = env assignments
    local mcp_entry="$1"
    shift
    env "$@" CLAUDE_WRAPPER_SOURCE_ONLY=1 bash -c '
        source "$1"
        rc=0
        _try_mcp_rebuild "$2" || rc=$?
        echo "RC=$rc"
    ' _ "$WRAPPER" "$mcp_entry" 2>&1 || true
}

if $HAVE_NODE && $HAVE_GIT; then
    # --- C1: THE EXACT REPRO — pnpm-managed (declared + tracked
    # pnpm-lock.yaml), untracked stale package-lock.json residue, unusable
    # node_modules. Self-repair must run `pnpm install --frozen-lockfile` /
    # `pnpm run build`, NEVER npm, and must not create a fresh
    # package-lock.json. ---------------------------------------------------
    _c1="$(mktemp -d)"
    _pkg="$_c1/squad"
    mkdir -p "$_pkg/src"
    _write_broken_dist "$_pkg"
    cat >"$_pkg/package.json" <<'JSON'
{
  "name": "squad",
  "version": "1.0.0",
  "packageManager": "pnpm@11.20.0",
  "dependencies": { "@modelcontextprotocol/sdk": "^1.0.0" },
  "devDependencies": { "typescript": "^5.0.0" }
}
JSON
    echo "lockfileVersion: '9.0'" >"$_pkg/pnpm-lock.yaml"
    git init -q "$_pkg"
    git -C "$_pkg" config user.email t@t.com
    git -C "$_pkg" config user.name t
    git -C "$_pkg" add package.json pnpm-lock.yaml
    git -C "$_pkg" commit -qm seed
    # The untracked residue, exactly as #6779 describes.
    echo '{"lockfileVersion":3}' >"$_pkg/package-lock.json"
    _lockfile_before="$(cat "$_pkg/package-lock.json")"

    _stub1="$_c1/stub"
    _write_pnpm_stub "$_stub1/pnpm"
    : >"$_stub1/pnpm-calls.log"

    _out_c1="$(_run_try_mcp_rebuild "$_pkg/dist/index.js" \
        PNPM_STUB_DIR="$_stub1" PNPM_STUB_LOG="$_stub1/pnpm-calls.log" \
        LOOM_PNPM_BIN="$_stub1/pnpm")"
    _log_c1="$(cat "$_stub1/pnpm-calls.log")"

    assert_contains "RC=0" "$_out_c1" \
        "C1 (#6779 repro): self-repair + rebuild succeeds via pnpm"
    assert_contains "install --frozen-lockfile" "$_log_c1" \
        "C1: self-repair invoked 'pnpm install --frozen-lockfile', not npm ci"
    assert_contains "run build" "$_log_c1" \
        "C1: build invoked 'pnpm run build', not npm run build"
    assert_eq "$_lockfile_before" "$(cat "$_pkg/package-lock.json")" \
        "C1: the untracked package-lock.json residue is left untouched (not regenerated)"

    rm -rf "$_c1"

    # --- C2: a correctly-installed pnpm tree never triggers the install
    # step at all — only the (always-run) build + smoke test. -------------
    _c2="$(mktemp -d)"
    _pkg2="$_c2/squad"
    # node_modules/.pnpm must be NON-empty — an empty dir is indistinguishable
    # from "no pnpm install ever ran" (matches _mcp_node_modules_unusable's
    # own missing/empty-dir check).
    mkdir -p "$_pkg2/src" "$_pkg2/node_modules/.pnpm/@modelcontextprotocol+sdk@1.0.0"
    _write_broken_dist "$_pkg2"
    cat >"$_pkg2/package.json" <<'JSON'
{ "name": "squad", "packageManager": "pnpm@11.20.0" }
JSON
    echo "lockfileVersion: '9.0'" >"$_pkg2/pnpm-lock.yaml"

    _stub2="$_c2/stub"
    _write_pnpm_stub "$_stub2/pnpm"
    : >"$_stub2/pnpm-calls.log"

    _out_c2="$(_run_try_mcp_rebuild "$_pkg2/dist/index.js" \
        PNPM_STUB_DIR="$_stub2" PNPM_STUB_LOG="$_stub2/pnpm-calls.log" \
        LOOM_PNPM_BIN="$_stub2/pnpm")"
    _log_c2="$(cat "$_stub2/pnpm-calls.log")"

    assert_contains "RC=0" "$_out_c2" \
        "C2: correctly-installed pnpm tree still rebuilds+smoke-tests successfully"
    assert_not_contains "install" "$_log_c2" \
        "C2: self-repair install step never fires against an already-valid pnpm tree"
    assert_contains "run build" "$_log_c2" \
        "C2: the (always-run) build step still executes"

    rm -rf "$_c2"

    # --- C3 (regression, #5032): npm-managed, genuinely broken tree still
    # self-repairs via `npm ci` exactly as before. -------------------------
    _c3="$(mktemp -d)"
    _pkg3="$_c3/mcp-loom"
    mkdir -p "$_pkg3/src"
    _write_broken_dist "$_pkg3"
    cat >"$_pkg3/package.json" <<'JSON'
{
  "name": "mcp-loom",
  "version": "1.0.0",
  "dependencies": { "@modelcontextprotocol/sdk": "^1.0.0" },
  "devDependencies": { "typescript": "^5.0.0" }
}
JSON
    echo '{"lockfileVersion":3}' >"$_pkg3/package-lock.json"

    _stub3="$_c3/stub"
    _write_npm_stub "$_stub3/npm"
    : >"$_stub3/npm-calls.log"

    _out_c3="$(_run_try_mcp_rebuild "$_pkg3/dist/index.js" \
        NPM_STUB_DIR="$_stub3" NPM_STUB_LOG="$_stub3/npm-calls.log" \
        LOOM_NPM_BIN="$_stub3/npm")"
    _log_c3="$(cat "$_stub3/npm-calls.log")"

    assert_contains "RC=0" "$_out_c3" \
        "C3 (no regression): npm-managed broken tree still self-repairs successfully"
    assert_contains "ci" "$_log_c3" \
        "C3 (no regression): self-repair still invokes 'npm ci' for an npm-managed package"

    rm -rf "$_c3"

    # --- C4: a build failure AFTER a successful self-repair names the
    # likely cause instead of only the raw "command not found". -----------
    _c4="$(mktemp -d)"
    _pkg4="$_c4/squad"
    mkdir -p "$_pkg4/src"
    _write_broken_dist "$_pkg4"
    cat >"$_pkg4/package.json" <<'JSON'
{ "name": "squad", "packageManager": "pnpm@11.20.0" }
JSON
    echo "lockfileVersion: '9.0'" >"$_pkg4/pnpm-lock.yaml"

    _stub4="$_c4/stub"
    _write_pnpm_stub "$_stub4/pnpm"
    : >"$_stub4/pnpm-calls.log"
    : >"$_stub4/build-fails-tool-missing"

    _out_c4="$(_run_try_mcp_rebuild "$_pkg4/dist/index.js" \
        PNPM_STUB_DIR="$_stub4" PNPM_STUB_LOG="$_stub4/pnpm-calls.log" \
        LOOM_PNPM_BIN="$_stub4/pnpm")"

    assert_not_contains "RC=0" "$_out_c4" \
        "C4: build failure after successful self-repair is reported as a failure"
    assert_contains "command not found" "$_out_c4" \
        "C4: the raw tool-not-found error is still surfaced"
    assert_contains "packageManager" "$_out_c4" \
        "C4: a manager-aware hint is added, not just the raw command-not-found line"

    rm -rf "$_c4"
else
    echo -e "  ${YELLOW}SKIP${NC}: _try_mcp_rebuild end-to-end tests (node and/or git not installed)"
fi

# ============================================================
# Summary
# ============================================================
echo ""
echo "========================================"
echo "Test Results:"
echo "  Total:  $TESTS_RUN"
echo -e "  ${GREEN}Passed: $TESTS_PASSED${NC}"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "  ${RED}Failed: $TESTS_FAILED${NC}"
    exit 1
fi
echo -e "  ${GREEN}All tests passed!${NC}"
