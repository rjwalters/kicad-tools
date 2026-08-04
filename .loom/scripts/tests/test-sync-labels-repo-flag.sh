#!/usr/bin/env bash
# test-sync-labels-repo-flag.sh - Unit tests for sync-labels.sh's --repo /
# --dry-run flags (issue #4498).
#
# Why --repo exists: bringing a new repo online in a daemon fleet needs that
# repo's Loom labels created on the forge. Every GitHub label operation in
# sync-labels.sh is already a `gh label` API call, so the only thing that
# forced a local checkout of the target was target *resolution* — the script
# inferred the NWO from the current directory's git remote via
# forge_detect/forge_get_repo_nwo. `--repo OWNER/NAME` names the target
# instead, so one workspace can sync labels onto klayout-tools and every
# gf180-*/sky130-* canary without cloning any of them.
#
# This is a black-box test: sync-labels.sh is a full CLI script, so we stub
# `gh` on PATH, run the real script as a subprocess from a scratch directory,
# and assert on exit codes, stderr, and the recorded `gh` argv log.
#
# The load-bearing assertions:
#   1. --repo resolves the target WITHOUT consulting the forge/git for the NWO
#      (no *bare* `gh repo view --json nameWithOwner` in the log) and works
#      from a directory that is not a git repo at all — the short-circuit,
#      proven rather than asserted.
#   2. Every mutating gh call carries `-R <override>`, never the invoking repo.
#   3. labels.yml is still read from WORKTREE_PATH (the source stays local;
#      only the target moves).
#   4. --dry-run is completely forge-free (zero gh invocations) — it is the
#      preview an operator runs before pointing deletions at a repo they are
#      not standing in.
#   5. The no-flag path is byte-for-byte the old behavior (additive flag):
#      NWO still comes from `gh repo view`, and a directory with no resolvable
#      remote still hard-errors.
#   6. Argument-parsing rejections: missing/invalid NWO, unknown option, extra
#      positional, and --repo combined with a Gitea forge.
#
# Issue #4524 hardened four things on top of that (PR #4506 judge follow-ups),
# each with its own section below:
#   7. `--` is honored as a real end-of-options terminator (a `-`-leading
#      WORKTREE_PATH after it is a path, not an "Unknown option").
#   8. The Gitea rejection also fires for forge.type=gitea resolved from the
#      config-tier chain, not just the LOOM_FORGE_TYPE env var.
#   9. The NWO regex rejects path-traversal ('../..') and `-`-leading segments
#      ('-foo/bar') while still accepting real names ('my-org/my-repo.name').
#  10. A real (non-dry-run) --repo sync preflights the named target with
#      `gh repo view <NWO>` BEFORE the first deletion, so a typo that names a
#      real administrable repo is caught before anything is destroyed.
#
# Usage:
#   ./defaults/scripts/tests/test-sync-labels-repo-flag.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"
SLS="$SCRIPTS_DIR/sync-labels.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
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
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if ! printf '%s' "$haystack" | grep -qF -- "$needle"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

if [[ ! -x "$SLS" ]]; then
    echo -e "${RED}FATAL${NC}: $SLS not found or not executable" >&2
    exit 2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP" 2>/dev/null || true' EXIT

STUB_DIR="$TMP/stub"
mkdir -p "$STUB_DIR"

# --- Stub gh on PATH ---------------------------------------------------------
# Appends every invocation's argv to $LOOM_TEST_GH_LOG (one line per call), so a
# test can assert both what WAS called and what was NOT.
#
#   gh repo view <NWO> ... -> the #4524 existence/permission preflight (an
#                         EXPLICIT target, not resolution). Answers from
#                         $LOOM_TEST_GH_REPO_VIEW: "" => ADMIN, "fail" =>
#                         exit 1 (repo missing / invisible), anything else is
#                         echoed as the viewerPermission.
#   gh repo view ...   -> NWO *resolution* (no positional target): echoes
#                         $LOOM_TEST_GH_NWO, or exits 1 when it is empty
#                         (simulating "no resolvable remote here")
#   gh label list ...  -> echoes nothing (script takes the `create` branch)
#   gh label create|edit|delete ... -> exit 0
#   gh api repos/.../issues -f labels=<L> ... -> the #5066 in-use lookup made
#                         by github_label_usage/github_maybe_delete_label.
#                         Echoes $LOOM_TEST_GH_USAGE_NUMBERS (one number per
#                         line) when <L> matches $LOOM_TEST_GH_USAGE_LABEL,
#                         otherwise empty (label not in use).
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${LOOM_TEST_GH_LOG:?stub gh: LOOM_TEST_GH_LOG not set}"

case "$1" in
  repo)
    if [[ "${2:-}" == "view" && -n "${3:-}" && "$3" != -* ]]; then
      case "${LOOM_TEST_GH_REPO_VIEW:-}" in
        fail)
          echo "stub gh: Could not resolve to a Repository with the name '$3'." >&2
          exit 1
          ;;
        "")  printf 'ADMIN\n'; exit 0 ;;
        *)   printf '%s\n' "$LOOM_TEST_GH_REPO_VIEW"; exit 0 ;;
      esac
    fi
    if [[ -z "${LOOM_TEST_GH_NWO:-}" ]]; then
      echo "stub gh: no remote configured" >&2
      exit 1
    fi
    printf '%s\n' "$LOOM_TEST_GH_NWO"
    exit 0
    ;;
  label)
    case "$2" in
      list)   exit 0 ;;
      create|edit|delete) exit 0 ;;
    esac
    echo "stub gh: unhandled label args: $*" >&2
    exit 3
    ;;
  api)
    # Find the -f labels=<value> argument by argv POSITION, not by splitting
    # the space-joined log — a label value with spaces ("good first issue")
    # must survive intact.
    label=""
    prev=""
    for arg in "$@"; do
      if [[ "$prev" == "-f" && "$arg" == labels=* ]]; then
        label="${arg#labels=}"
      fi
      prev="$arg"
    done
    if [[ -n "$label" && "$label" == "${LOOM_TEST_GH_USAGE_LABEL:-}" ]]; then
      printf '%s\n' "${LOOM_TEST_GH_USAGE_NUMBERS:-}" | sed '/^$/d'
    fi
    exit 0
    ;;
esac
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

# --- Scratch source tree -----------------------------------------------------
# Deliberately NOT a git repo: `git remote get-url origin` and `git rev-parse`
# find nothing here, which is exactly the condition --repo must tolerate. It
# holds only the labels.yml the sync reads (the source stays local; --repo
# moves the target).
SRC="$TMP/src"
mkdir -p "$SRC/.github"
cat > "$SRC/.github/labels.yml" <<'EOF'
# BEGIN LOOM LABELS
- name: loom:issue
  description: "Approved and ready for a Builder"
  color: "3B82F6"
- name: loom:pr
  description: "Approved pull request"
  color: "10B981"
# END LOOM LABELS
EOF

# A SECOND labels.yml, in a subdirectory whose name begins with '-'. It exists
# to exercise the `--` end-of-options terminator (#4524): only a real
# terminator lets this be passed as WORKTREE_PATH instead of being rejected as
# an unknown option. It holds exactly ONE label, so "Synced 1 labels" proves
# the sync actually read THIS tree and not $SRC's two-label file.
mkdir -p "$SRC/-alt/.github"
cat > "$SRC/-alt/.github/labels.yml" <<'EOF'
# BEGIN LOOM LABELS
- name: loom:only
  description: "The lone label in the dash-prefixed tree"
  color: "AABBCC"
# END LOOM LABELS
EOF

# A config-tier root that declares Gitea WITHOUT setting LOOM_FORGE_TYPE — the
# #4524 case the old env-only guard missed. Passed to the script as $REPO_ROOT,
# which is the first tier of forge-helpers.sh's _forge_config_root precedence.
CFG_GITEA="$TMP/cfg-gitea"
mkdir -p "$CFG_GITEA/.loom"
echo '{"forge":{"type":"gitea"}}' > "$CFG_GITEA/.loom/config.json"

# ...and the same shape declaring GitHub, to prove the guard keys on the value
# and not merely on "a config file exists".
CFG_GITHUB="$TMP/cfg-github"
mkdir -p "$CFG_GITHUB/.loom"
echo '{"forge":{"type":"github"}}' > "$CFG_GITHUB/.loom/config.json"

GH_LOG="$TMP/gh.log"

# Run the real script with the stub gh first on PATH, from $SRC.
#   run_sls [--nwo NWO] [--repo-view ANSWER] [--repo-root DIR] -- <script args...>
#
#   --repo-view   what the stub's targeted `gh repo view <NWO>` answers
#                 (default ADMIN; "fail" => the repo is missing/invisible)
#   --repo-root   value for $REPO_ROOT, i.e. the root the config-tier chain
#                 resolves from (default: unset/empty)
#
# LOOM_CONFIG_DEFAULTS_FILE is pinned to empty so the host's private-defaults
# tier (~/.local/share/loom/config/defaults.json) can never leak into a run.
# Sets: RC, OUT (merged stdout+stderr), LOG (recorded gh argv lines).
RC=0
OUT=""
LOG=""
run_sls() {
    local nwo="" repo_view="" repo_root=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --nwo) nwo="$2"; shift 2 ;;
            --repo-view) repo_view="$2"; shift 2 ;;
            --repo-root) repo_root="$2"; shift 2 ;;
            --) shift; break ;;
            *) break ;;
        esac
    done
    : > "$GH_LOG"
    OUT="$(
        cd "$SRC" || exit 99
        PATH="$STUB_DIR:$PATH" \
        LOOM_TEST_GH_LOG="$GH_LOG" \
        LOOM_TEST_GH_NWO="$nwo" \
        LOOM_TEST_GH_REPO_VIEW="$repo_view" \
        LOOM_CONFIG_DEFAULTS_FILE="" \
        REPO_ROOT="$repo_root" \
        LOOM_FORGE_TYPE="${LOOM_FORGE_TYPE_OVERRIDE:-}" \
        LOOM_TEST_GH_USAGE_LABEL="${LOOM_TEST_GH_USAGE_LABEL:-}" \
        LOOM_TEST_GH_USAGE_NUMBERS="${LOOM_TEST_GH_USAGE_NUMBERS:-}" \
        bash "$SLS" "$@" 2>&1
    )"
    RC=$?
    LOG="$(cat "$GH_LOG")"
}

echo ""
echo "=== --repo short-circuits NWO resolution (no git remote needed) ==="

# The scratch dir has no git remote and the stub `gh repo view` fails, so the
# legacy resolution path cannot produce an NWO. --repo must still succeed.
run_sls -- --repo octocat/hello-world
assert_eq "0" "$RC" "--repo succeeds in a directory with no resolvable remote"
assert_contains "$OUT" "Target repository: octocat/hello-world (github)" \
    "--repo resolves REPO to the flag value"
# The short-circuit proof, kept precise after #4524 added a preflight: what
# --repo must never do is ASK the forge which repo this is. That call is the
# bare, targetless `gh repo view --json nameWithOwner` in forge_get_repo_nwo;
# the preflight below is a different call (it NAMES the target).
assert_not_contains "$LOG" "repo view --json nameWithOwner --jq" \
    "--repo never resolves the NWO from the forge (forge_get_repo_nwo short-circuited)"
# Additive by default (#5066): no --prune-defaults, so no default-label
# deletion happens even when --repo names an explicit target.
assert_not_contains "$LOG" "label delete" \
    "bare --repo run performs no default-label deletion (additive by default)"
assert_contains "$OUT" "Leaving default labels untouched" \
    "bare --repo run explains why nothing was deleted"
assert_contains "$LOG" "label create loom:issue -R octocat/hello-world" \
    "label create targets the override NWO"
assert_contains "$OUT" "Synced 2 labels" \
    "labels.yml is still read from WORKTREE_PATH (both labels synced)"
# Every recorded gh call must name exactly one target, and it must be the
# override: a stray fallback to remote-based resolution would surface here as a
# second distinct -R value (or as a call with no -R at all).
assert_eq "-R octocat/hello-world" \
    "$(printf '%s\n' "$LOG" | grep -o -- '-R [^ ]*' | sort -u | tr '\n' ' ' | sed 's/ *$//')" \
    "the override NWO is the ONLY -R target in the whole gh log"
# The preflight names its target positionally (`gh repo view` has no -R flag),
# so match on the NWO itself rather than on '-R': no call may go anywhere else.
assert_eq "" \
    "$(printf '%s\n' "$LOG" | grep -v -- 'octocat/hello-world' || true)" \
    "no gh call was made without the override target"

# --repo=OWNER/NAME is the same flag.
run_sls -- --repo=octocat/hello-world
assert_eq "0" "$RC" "--repo=OWNER/NAME form accepted"
assert_contains "$OUT" "Target repository: octocat/hello-world (github)" \
    "--repo= form resolves the same target"

# The flag order relative to the positional path must not matter.
run_sls -- --repo octocat/hello-world "$SRC"
assert_eq "0" "$RC" "--repo composes with an explicit WORKTREE_PATH positional"
assert_contains "$OUT" "Target repository: octocat/hello-world (github)" \
    "--repo plus positional still targets the override"

echo ""
echo "=== --dry-run is forge-free ==="

run_sls -- --repo octocat/hello-world --dry-run
assert_eq "0" "$RC" "--repo --dry-run exits 0"
assert_eq "" "$LOG" "--dry-run makes ZERO gh calls"
assert_contains "$OUT" "[dry-run] would leave default labels untouched" \
    "--dry-run (no --prune-defaults) reports that default labels are left alone"
assert_not_contains "$OUT" "[dry-run] would delete default label" \
    "--dry-run without --prune-defaults never previews a deletion"
assert_contains "$OUT" "[dry-run] would create or update label: loom:issue" \
    "--dry-run reports the labels it would sync"
assert_contains "$OUT" "Dry run complete: 2 labels would be synced to octocat/hello-world" \
    "--dry-run names the resolved target in its summary"

echo ""
echo "=== No-flag path is unchanged (the flag is additive) ==="

# With a resolvable NWO, the legacy path still resolves via `gh repo view` and
# targets whatever that returned — untouched by this change.
run_sls --nwo owner/from-remote --
assert_eq "0" "$RC" "no-flag run succeeds when the NWO resolves"
assert_contains "$LOG" "repo view" \
    "no-flag run still calls 'gh repo view' (legacy resolution intact)"
assert_contains "$OUT" "Target repository: owner/from-remote (github)" \
    "no-flag run resolves the NWO from the forge, not from a flag"
assert_contains "$LOG" "label create loom:issue -R owner/from-remote" \
    "no-flag run targets the resolved repo"
assert_not_contains "$OUT" "dry-run" \
    "no-flag run emits no dry-run output"

# ...and with nothing resolvable it must still hard-error, as before.
run_sls --
assert_eq "1" "$RC" "no-flag run in a non-repo still fails"
assert_contains "$OUT" "Could not determine repository from git remote" \
    "no-flag failure keeps its original error message"

# --dry-run alone (no --repo) previews against the detected repo.
run_sls --nwo owner/from-remote -- --dry-run
assert_eq "0" "$RC" "--dry-run without --repo exits 0"
assert_contains "$OUT" "Target repository: owner/from-remote (github)" \
    "--dry-run without --repo still detects the local repo"
assert_not_contains "$LOG" "label create" \
    "--dry-run without --repo performs no label mutations"

# Byte-identical proof for #4524's preflight: the no-flag path must NOT gain a
# second `gh repo view`. Exactly one `gh repo ...` call — the resolution one.
run_sls --nwo owner/from-remote --
assert_eq "1" "$(printf '%s\n' "$LOG" | grep -c '^repo ' || true)" \
    "no-flag run still makes exactly ONE 'gh repo view' call (no preflight added)"

echo ""
echo "=== Argument-parsing rejections (exit 2, no forge contact) ==="

run_sls -- --repo
assert_eq "2" "$RC" "--repo with no value exits 2"
assert_contains "$OUT" "Option --repo requires an OWNER/NAME argument" \
    "--repo with no value explains itself"
assert_eq "" "$LOG" "--repo with no value contacts no forge"

run_sls -- --repo=
assert_eq "2" "$RC" "--repo= with an empty value exits 2"

for bad in bad-nwo owner/repo/extra "own er/repo" /repo owner/; do
    run_sls -- --repo "$bad"
    assert_eq "2" "$RC" "invalid --repo value '$bad' exits 2"
    assert_contains "$OUT" "Invalid --repo value" \
        "invalid --repo value '$bad' is reported as invalid"
done

echo ""
echo "=== NWO validation rejects traversal / '-'-leading segments (#4524) ==="

# The pre-#4524 class ([A-Za-z0-9._-]+ per segment) accepted every value below.
# '../..' and friends are path-traversal shapes that must never reach a gh
# argument; '-foo/bar' would be read by gh as a bundle of short flags rather
# than a repository. Each segment must now START with an alphanumeric.
for bad in "../.." "./x" "owner/.." "../owner" "-foo/bar" "owner/-bar" \
           ".hidden/repo" "owner/.hidden" "_leading/repo"; do
    run_sls -- --repo "$bad"
    assert_eq "2" "$RC" "hardened NWO check rejects '$bad'"
    assert_contains "$OUT" "Invalid --repo value" \
        "'$bad' is reported as an invalid --repo value"
    assert_eq "" "$LOG" "'$bad' never reaches the forge"
done

# ...and the tightening must not have caught real names. Dots, dashes and
# underscores stay legal INSIDE a segment. --dry-run keeps these assertions
# about parsing rather than about the stubbed forge.
for good in owner/repo my-org/my-repo owner-2/repo.name a/b \
            under_score/re_po owner/repo-1.2.3 0rg/9repo; do
    run_sls -- --repo "$good" --dry-run
    assert_eq "0" "$RC" "legitimate NWO '$good' is still accepted"
    assert_contains "$OUT" "Target repository: $good (github)" \
        "legitimate NWO '$good' resolves to itself"
done

echo ""
echo "=== '--' is a real end-of-options terminator (#4524) ==="

# Baseline: without --, a '-'-leading path is (still) an unknown option. That
# is exactly why -- has to work.
run_sls -- --repo octocat/hello-world -alt
assert_eq "2" "$RC" "a '-'-leading WORKTREE_PATH without -- is rejected as an option"
assert_contains "$OUT" "Unknown option: -alt" \
    "the '-'-leading path is named as the rejected option"

# With --, it becomes the positional WORKTREE_PATH — proven by the label count:
# the dash-prefixed tree holds ONE label, $SRC holds two.
run_sls -- --repo octocat/hello-world -- -alt
assert_eq "0" "$RC" "-- lets a '-'-leading WORKTREE_PATH through"
assert_contains "$OUT" "Synced 1 labels" \
    "-- routed '-alt' to WORKTREE_PATH (its one-label labels.yml was read)"
assert_contains "$LOG" "label create loom:only -R octocat/hello-world" \
    "the synced label came from the dash-prefixed tree, not from \$SRC"

# After --, an option-looking token is a PATH: --dry-run must not be re-parsed
# as the flag (it becomes a nonexistent directory, so the cd fails).
run_sls -- --repo octocat/hello-world -- --dry-run
assert_eq "1" "$RC" "a --dry-run after -- is treated as a (nonexistent) path"
assert_not_contains "$OUT" "[dry-run] would" \
    "a --dry-run after -- does not re-enable dry-run mode"

# A trailing -- with no operand stays the harmless no-op it always was.
run_sls -- --repo octocat/hello-world --
assert_eq "0" "$RC" "a trailing -- with no operand is a no-op"
assert_contains "$OUT" "Synced 2 labels" \
    "the trailing -- left WORKTREE_PATH at its default"

# The extra-positional rejection still applies on the post---- path (the two
# branches share one take_positional helper).
run_sls -- -- "$SRC" extra
assert_eq "2" "$RC" "a second positional after -- still exits 2"
assert_contains "$OUT" "Unexpected extra argument: extra" \
    "the post-'--' extra positional is named"

run_sls -- --bogus
assert_eq "2" "$RC" "unknown option exits 2"
assert_contains "$OUT" "Unknown option: --bogus" "unknown option is named"

run_sls -- "$SRC" extra
assert_eq "2" "$RC" "a second positional argument exits 2"
assert_contains "$OUT" "Unexpected extra argument: extra" \
    "the extra positional is named (previously silently ignored)"

echo ""
echo "=== --repo is GitHub-only ==="

LOOM_FORGE_TYPE_OVERRIDE="gitea" run_sls -- --repo octocat/hello-world
assert_eq "2" "$RC" "--repo with LOOM_FORGE_TYPE=gitea exits 2"
assert_contains "$OUT" "--repo is GitHub-only" \
    "the Gitea combination fails loudly instead of silently syncing GitHub"
assert_eq "" "$LOG" "the Gitea rejection contacts no forge"

# #4524: forge.type=gitea resolved from the CONFIG TIERS, with LOOM_FORGE_TYPE
# unset, must reject --repo exactly like the env var does. The pre-#4524 guard
# read only the env var, so this combination silently synced GitHub instead.
run_sls --repo-root "$CFG_GITEA" -- --repo octocat/hello-world
assert_eq "2" "$RC" "--repo with config-tier forge.type=gitea exits 2"
assert_contains "$OUT" "--repo is GitHub-only" \
    "the config-tier Gitea setting fails just as loudly as the env var"
assert_contains "$OUT" "forge.type=gitea" \
    "the rejection names the config setting, not a phantom LOOM_FORGE_TYPE"
assert_eq "" "$LOG" "the config-tier Gitea rejection contacts no forge"

# The guard must key on the VALUE, not on "a config file exists".
run_sls --repo-root "$CFG_GITHUB" -- --repo octocat/hello-world
assert_eq "0" "$RC" "--repo with config-tier forge.type=github is allowed"
assert_contains "$OUT" "Synced 2 labels" "the GitHub config-tier run syncs normally"

# Env var beats the config tier, matching forge_detect's precedence.
LOOM_FORGE_TYPE_OVERRIDE="github" run_sls --repo-root "$CFG_GITEA" -- --repo octocat/hello-world
assert_eq "0" "$RC" "LOOM_FORGE_TYPE=github overrides a config-tier gitea setting"

# The Gitea guard is scoped to --repo: a config-tier gitea repo without the
# flag still takes the normal (Gitea) detection path, not this rejection.
run_sls --repo-root "$CFG_GITEA" -- --dry-run
assert_not_contains "$OUT" "--repo is GitHub-only" \
    "config-tier gitea WITHOUT --repo is not rejected"

echo ""
echo "=== --repo preflights the target before any deletion (#4524) ==="

# --dry-run is deliberately forge-free, so it cannot tell a typo'd NWO apart
# from a real repo. The real run therefore verifies the target FIRST.
run_sls -- --repo octocat/hello-world
assert_eq "0" "$RC" "a reachable, writable --repo target syncs normally"
assert_contains "$LOG" "repo view octocat/hello-world --json nameWithOwner,viewerPermission" \
    "the real --repo path preflights the named target with 'gh repo view'"
assert_contains "$(printf '%s\n' "$LOG" | head -1)" "repo view octocat/hello-world" \
    "the preflight is the FIRST gh call — it precedes every label operation"

# A typo that resolves to nothing this identity can see must abort before the
# deletion loop, not partway through it.
run_sls --repo-view fail -- --repo octocat/typo-repo
assert_eq "1" "$RC" "an unreachable --repo target aborts"
assert_contains "$OUT" "is not reachable" \
    "the abort explains that the target could not be reached"
assert_contains "$OUT" "nothing was deleted" \
    "the abort states explicitly that nothing was deleted"
assert_not_contains "$LOG" "label delete" \
    "the unreachable target aborted BEFORE any label deletion"
assert_not_contains "$LOG" "label create" \
    "the unreachable target aborted before any label creation"
assert_eq "1" "$(printf '%s\n' "$LOG" | grep -c . || true)" \
    "the preflight was the only gh call made against the bad target"

# Existing-but-read-only is the other half of "existence/permission": deleting
# labels there would fail partway, so refuse up front.
run_sls --repo-view READ -- --repo octocat/hello-world
assert_eq "1" "$RC" "a read-only --repo target aborts"
assert_contains "$OUT" "is not writable" "the read-only abort names the problem"
assert_contains "$OUT" "READ" "the read-only abort reports the observed permission"
assert_not_contains "$LOG" "label delete" \
    "the read-only target aborted before any label deletion"

# Some tokens omit viewerPermission entirely. Existence is still proven, so
# warn and continue rather than blocking a legitimate sync.
run_sls --repo-view " " -- --repo octocat/hello-world
assert_eq "0" "$RC" "an indeterminate permission does not block the sync"
assert_contains "$OUT" "Could not determine your permission" \
    "an indeterminate permission is warned about"
assert_contains "$LOG" "label create loom:issue -R octocat/hello-world" \
    "the sync proceeds once existence is proven"

# The preflight is real-run only: --dry-run must stay forge-free.
run_sls -- --repo octocat/hello-world --dry-run
assert_eq "" "$LOG" \
    "--dry-run makes no preflight call (it stays completely forge-free)"

echo ""
echo "=== --help documents the new flags ==="

run_sls -- --help
assert_eq "0" "$RC" "--help exits 0"
assert_contains "$OUT" "--repo OWNER/NAME" "--help documents --repo"
assert_contains "$OUT" "--dry-run" "--help documents --dry-run"
assert_contains "$OUT" "End of options" "--help documents the -- terminator (#4524)"
assert_contains "$OUT" "--prune-defaults" "--help documents --prune-defaults (#5066)"
assert_contains "$OUT" "--force" "--help documents --force (#5066)"

echo ""
echo "=== Additive by default: no deletion without --prune-defaults (#5066) ==="

# A bare run (no --repo, no --prune-defaults) against the locally-resolved
# repo must create/update Loom labels and touch none of GitHub's defaults —
# the core regression this issue exists to prevent.
run_sls --nwo owner/from-remote --
assert_not_contains "$LOG" "label delete" \
    "a bare run performs no default-label deletion"
assert_contains "$OUT" "Leaving default labels untouched" \
    "a bare run explains that default labels were left alone"
assert_contains "$LOG" "label create loom:issue -R owner/from-remote" \
    "a bare run still creates/updates the Loom labels"

# Same, but --repo (a real forge round-trip location, not just the resolved
# path) confirms the additive default holds there too.
run_sls -- --repo octocat/hello-world
assert_not_contains "$LOG" "label delete" \
    "a bare --repo run performs no default-label deletion"

echo ""
echo "=== --prune-defaults restores the old (pre-#5066) deletion behavior ==="

run_sls -- --repo octocat/hello-world --prune-defaults
assert_eq "0" "$RC" "--prune-defaults run exits 0"
assert_contains "$OUT" "Pruning default labels (--prune-defaults)..." \
    "--prune-defaults announces that it is pruning"
assert_contains "$LOG" "label delete bug -R octocat/hello-world" \
    "--prune-defaults deletes GitHub's default labels (unused label, no in-use guard triggered)"
assert_contains "$LOG" "label delete wontfix -R octocat/hello-world" \
    "--prune-defaults deletes every default label, not just the first"
assert_contains "$LOG" "label create loom:issue -R octocat/hello-world" \
    "--prune-defaults still creates/updates the Loom labels"

echo ""
echo "=== --prune-defaults skips an in-use default label unless --force ==="

# 'bug' is reported in use on issues #1 and #2; every other default label is
# reported unused (LOOM_TEST_GH_USAGE_LABEL only matches one label per run).
LOOM_TEST_GH_USAGE_LABEL="bug" LOOM_TEST_GH_USAGE_NUMBERS=$'1\n2' \
    run_sls -- --repo octocat/hello-world --prune-defaults
assert_eq "0" "$RC" "an in-use default label does not fail the whole run"
assert_contains "$OUT" "Refusing to delete in-use default label 'bug'" \
    "the in-use label is refused, not silently deleted"
assert_contains "$OUT" "#1 #2" \
    "the refusal names the affected issue/PR numbers"
assert_contains "$OUT" "pass --force to delete anyway" \
    "the refusal names the escape hatch"
assert_not_contains "$LOG" "label delete bug " \
    "the in-use label is NOT deleted without --force"
assert_contains "$LOG" "label delete wontfix -R octocat/hello-world" \
    "an unrelated, unused default label is still deleted normally"

# --force overrides the refusal, but still warns first.
LOOM_TEST_GH_USAGE_LABEL="bug" LOOM_TEST_GH_USAGE_NUMBERS=$'1\n2' \
    run_sls -- --repo octocat/hello-world --prune-defaults --force
assert_eq "0" "$RC" "--force run exits 0"
assert_contains "$OUT" "Deleting in-use default label 'bug' (--force)" \
    "--force warns before deleting an in-use label"
assert_contains "$OUT" "#1 #2" \
    "the --force warning still names the affected issue/PR numbers"
assert_contains "$LOG" "label delete bug -R octocat/hello-world" \
    "--force actually deletes the in-use label"

echo ""
echo "=== --prune-defaults --dry-run flags in-use deletions (#5066) ==="

# Unlike a bare --dry-run (forge-free), --dry-run combined with
# --prune-defaults performs a READ-ONLY in-use lookup so the preview can flag
# which deletions would be skipped without --force.
LOOM_TEST_GH_USAGE_LABEL="bug" LOOM_TEST_GH_USAGE_NUMBERS=$'1\n2' \
    run_sls -- --repo octocat/hello-world --prune-defaults --dry-run
assert_eq "0" "$RC" "--prune-defaults --dry-run exits 0"
assert_contains "$OUT" "[dry-run] would delete default label: bug (IN USE: #1 #2" \
    "the dry-run preview flags the in-use default label"
assert_contains "$OUT" "[dry-run] would delete default label: wontfix" \
    "the dry-run preview lists an unused default label plainly"
assert_not_contains "$OUT" "would delete default label: wontfix (IN USE" \
    "the dry-run preview does not flag an unused label as in-use"
assert_not_contains "$LOG" "label delete" \
    "--prune-defaults --dry-run never actually deletes anything"
assert_not_contains "$LOG" "label create" \
    "--prune-defaults --dry-run never actually creates/updates anything"
assert_contains "$LOG" "api repos/octocat/hello-world/issues" \
    "--prune-defaults --dry-run DOES make a read-only in-use lookup (unlike a bare --dry-run)"

echo ""
echo "=== Installed-tree parity (.loom/scripts is a symlinked dir) ==="

# AC: ".loom/scripts/sync-labels.sh reflects the change automatically". In this
# repo .loom/scripts is a whole-directory symlink to defaults/scripts, so the
# two paths must resolve to the same file — one edit covers both surfaces.
INSTALLED="$REPO_ROOT/.loom/scripts/sync-labels.sh"
if [[ -e "$INSTALLED" ]]; then
    LINKED_REAL="$(cd "$(dirname "$INSTALLED")" && pwd -P)/$(basename "$INSTALLED")"
    SOURCE_REAL="$(cd "$(dirname "$SLS")" && pwd -P)/$(basename "$SLS")"
    assert_eq "$SOURCE_REAL" "$LINKED_REAL" \
        ".loom/scripts/sync-labels.sh resolves to defaults/scripts/sync-labels.sh"
else
    echo "  SKIP: $INSTALLED not present (source-only checkout)"
fi

echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
