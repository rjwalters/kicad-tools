#!/usr/bin/env bash
# test-check-branch-name.sh — the ref-operand guard against git option
# injection (#9106).
#
# THE DEFECT
#
# A PR author controls `headRefName` and `base.ref`. Git's own ref validator
# ACCEPTS a leading-dash name (`git check-ref-format refs/heads/--upload-pack=/x`
# exits 0 — only the `--branch` convenience form rejects it), so such a branch
# can exist and a forge will host a PR for it. Handed to git as a bare OPERAND
# it is re-parsed as a SWITCH:
#
#   git fetch origin '--upload-pack=/tmp/payload' main   # path/file:// origin:
#                                                        #   EXECUTES the payload
#   git fetch origin '--depth=1'                         # shallow-ifies the clone
#   git rebase <upstream> '--strategy=evil'              # exec merge-evil
#
# Two independent mitigations landed together, and this suite pins both:
#
#   A. `check_branch_name` (defaults/scripts/lib/default-branch.sh) — an
#      ALLOWLIST predicate, called before the git call, failing CLOSED.
#   B. the `--` end-of-options separator at every fetch/rebase call site.
#
# COVERAGE (the acceptance criteria of #9106)
#
#   AC1  the validator's own reject/accept table
#   AC2  a grep-auditable scan: no `git fetch`/`git rebase` in defaults/scripts
#        may take a variable ref operand without a preceding `--`, and every
#        file that has one must call the validator
#   AC3  the audit's repro — a path-based origin plus an `--upload-pack=`
#        branch name — is refused before any git process executes
#   AC4  merge-pr.sh denies the merge, naming the invalid ref, when a PR's
#        headRefName fails validation
#
# AC5 (the Rust half: `reconcile_stack` refuses with a named blocker) lives in
# loom-daemon/tests/reconcile_stack_refname_guard.rs, next to the code it
# guards; `loom-daemon/src/refname.rs` carries the Rust twin of AC1's table.
#
# Hermetic: every fixture is a `mktemp -d` git repo with a path-based origin.
# Nothing touches the network, a forge, or `gh` (merge-pr.sh is driven through
# a stub `gh` on PATH).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "worktree-base"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"
LIB="$SCRIPTS_DIR/lib/default-branch.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_contains() {
    local haystack="$1" needle="$2" what="$3"
    case "$haystack" in
        *"$needle"*) pass "$what" ;;
        *) fail "$what (expected to find '$needle' in: ${haystack:0:400})" ;;
    esac
}

# shellcheck source=../lib/default-branch.sh
source "$LIB"

# ───────────────────────────────────────────────────────────────────────────
# AC1 — the predicate
# ───────────────────────────────────────────────────────────────────────────

echo "AC1: check_branch_name reject/accept table"

# The audit's verified payloads, then the structural rules. Anything here that
# the validator ACCEPTS is a live injection vector.
REJECT_CASES=(
    '--upload-pack=/x'      # RCE on a path/file:// origin
    '--depth=1'             # silent shallow-ification of the local clone
    '--strategy=evil'       # external merge-driver exec via rebase
    '--filter=blob:none'    # silent partial-clone conversion
    '-d'                    # bare switch
    '-'                     # bare dash
    '--'                    # bare end-of-options
    '=a'                    # '=' with no leading dash
    'a/=b'                  # '=' inside a path segment
    ''                      # empty
    '.hidden'               # leading dot
    '/abs'                  # leading slash
    'a.'                    # trailing dot
    'a/'                    # trailing slash
    'a//b'                  # empty segment
    'a/.b'                  # dot-leading segment
    'a..b'                  # '..' segment
    'a b'                   # whitespace
    $'a\tb'                 # tab
    $'a\nb'                 # newline (a ref name git itself would also reject)
    'a;b'                   # shell metacharacter
    'a$b'                   # shell metacharacter
    'a*b'                   # glob
    'a?b'                   # glob
    'a:b'                   # refspec separator
    'a^b'                   # rev syntax
    'a~b'                   # rev syntax
    'a\b'                   # backslash
    'a@{b'                  # reflog syntax
)
for bad in "${REJECT_CASES[@]}"; do
    # Printed via %q: several cases contain backslash/control sequences that
    # `echo -e` in pass()/fail() would otherwise silently interpret.
    label="$(printf '%q' "$bad")"
    if check_branch_name "$bad" 2>/dev/null; then
        fail "rejects $label  <-- ACCEPTED an unsafe ref operand"
    else
        pass "rejects $label"
    fi
done

# A false rejection denies legitimate merges, so this half matters just as much.
ACCEPT_CASES=(
    'feature/issue-42'
    'main'
    'master'
    'hotfix/x.y'
    'a-b_c.d'
    'release/v1.2.3'
    'chore/resync-installed'
    'docs/onboarding-cleanup'
    '9x'
    'a'
)
for good in "${ACCEPT_CASES[@]}"; do
    if check_branch_name "$good" 2>/dev/null; then
        pass "accepts '$good'"
    else
        fail "accepts '$good'  <-- FALSE REJECTION of a legitimate branch name"
    fi
done

# Non-ASCII, asserted under an explicitly UTF-8 locale.
#
# This is a regression test for a real defect in the first cut of the validator,
# not a hypothetical: bracket ranges in a bash `[[ =~ ]]` are resolved by the
# CURRENT locale's collation, so with LC_ALL=en_US.UTF-8 in the environment
# `[[ "ábc" =~ ^[A-Za-z0-9]+$ ]]` MATCHES. Without the `local LC_ALL=C` inside
# check_branch_name, the shell half therefore accepted names the Rust half
# (`refname::check_refname`, ASCII by construction) rejects — a silent
# cross-language parity break that only shows up on a UTF-8 host.
#
# The locale must be a real COLLATION locale (en_US.utf8 and friends), never
# C.utf8 — C.utf8 keeps C's byte-order collation, so `[A-Za-z0-9]` stays
# ASCII-only there and the assertion would pass vacuously whether or not the
# guard exists. Skipped, loudly, where no such locale is installed (a bare
# container), since there is then nothing to prove.
UTF8_LOCALE="$(locale -a 2>/dev/null | grep -iE '\.utf-?8$' | grep -viE '^(C|POSIX)\.' | head -1)"
if [[ -n "$UTF8_LOCALE" ]]; then
    for nonascii in 'ábc' 'feature/ıssue-42' 'ｍain'; do
        if LC_ALL="$UTF8_LOCALE" LANG="$UTF8_LOCALE" check_branch_name "$nonascii" 2>/dev/null; then
            fail "rejects non-ASCII '$nonascii' under $UTF8_LOCALE  <-- locale-dependent allowlist (shell/Rust parity break)"
        else
            pass "rejects non-ASCII '$nonascii' under $UTF8_LOCALE"
        fi
    done
else
    echo "  SKIP: no non-C UTF-8 locale on this host — cannot exercise the locale-collation case"
fi

# The refusal has to be usable: it names the offending ref (so a log grep and a
# forge comment both carry it) and cites the issue.
REFUSAL="$(check_branch_name '--upload-pack=/tmp/payload' 'head branch of PR #1' 2>&1 >/dev/null)"
assert_contains "$REFUSAL" '--upload-pack=/tmp/payload' "refusal names the invalid ref"
assert_contains "$REFUSAL" 'head branch of PR #1' "refusal names the caller's context"
assert_contains "$REFUSAL" '#9106' "refusal cites the issue"

# ───────────────────────────────────────────────────────────────────────────
# AC2 — the grep-auditable scan
# ───────────────────────────────────────────────────────────────────────────
#
# The invariant, stated so a reviewer can re-derive it by eye:
#
#   In every non-test shell file under defaults/scripts, any `git … fetch` or
#   `git … rebase` command line whose ref operands include a `$`-expansion must
#   carry a standalone `--` token between the subcommand and that operand.
#
# `--` is the machine-checkable half. The validator call is the other half, and
# it cannot be located mechanically to the same precision (it legitimately sits
# at a different scope from its call site — once per run, per PR, or per child),
# so it is asserted per known sink below instead.
#
# Scope notes: `defaults/scripts/tests/` is excluded (its fixtures build
# literal branch names in throwaway repos and are not a merge path), and lines
# whose git text is inside an `echo`/`printf` are remediation hints, not
# commands.

echo ""
echo "AC2: no unguarded 'git fetch/rebase <variable ref>' survives"

SCAN_REPORT="$(
    cd "$REPO_ROOT" || exit 1
    git ls-files 'defaults/scripts/*.sh' 'defaults/scripts/**/*.sh' 2>/dev/null \
        | grep -v '^defaults/scripts/tests/' \
        | while IFS= read -r f; do
            awk -v FNAME="$f" '
              { line = $0 }
              # Strip full-line comments and obvious prose lines.
              line ~ /^[[:space:]]*#/ { next }
              # Remediation hints printed to the operator are not commands.
              line ~ /^[[:space:]]*(echo|printf)[[:space:]]/ { next }
              {
                # Locate the subcommand: the first " fetch " / " rebase " that
                # follows a "git" token on this line.
                gi = index(line, "git ")
                if (gi == 0) next
                rest = substr(line, gi)
                fi = match(rest, /[[:space:]](fetch|rebase)([[:space:]]|$)/)
                if (fi == 0) next
                # Leading space restored so a `--` that is the FIRST operand
                # (`git rebase -- "$a" "$b"`) still matches the " -- " token.
                operands = " " substr(rest, fi + RLENGTH)
                # No expansion among the operands -> nothing to protect.
                if (index(operands, "$") == 0) next
                # A standalone `--` must appear before the first expansion.
                dd = index(operands, " -- ")
                dollar = index(operands, "$")
                if (dd > 0 && dd < dollar) next
                printf "%s:%d: %s\n", FNAME, FNR, line
              }
            ' "$f"
          done
)"

if [[ -z "$SCAN_REPORT" ]]; then
    pass "every git fetch/rebase with a variable ref operand carries a '--' separator"
else
    fail "unguarded git fetch/rebase call site(s) found — add '--' before the ref operands:"
    printf '%s\n' "$SCAN_REPORT" >&2
fi

# The scan must be able to FAIL — a pattern test that cannot fire is decoration.
SELFTEST_DIR="$(mktemp -d /tmp/loom-cbn-selftest.XXXXXX)"
mkdir -p "$SELFTEST_DIR/defaults/scripts"
cat > "$SELFTEST_DIR/defaults/scripts/offender.sh" <<'EOF'
#!/usr/bin/env bash
git fetch origin "$BRANCH"
EOF
SELFTEST_HIT="$(
    awk -v FNAME="offender.sh" '
      { line = $0 }
      line ~ /^[[:space:]]*#/ { next }
      line ~ /^[[:space:]]*(echo|printf)[[:space:]]/ { next }
      {
        gi = index(line, "git ")
        if (gi == 0) next
        rest = substr(line, gi)
        fi = match(rest, /[[:space:]](fetch|rebase)([[:space:]]|$)/)
        if (fi == 0) next
        operands = " " substr(rest, fi + RLENGTH)
        if (index(operands, "$") == 0) next
        dd = index(operands, " -- ")
        dollar = index(operands, "$")
        if (dd > 0 && dd < dollar) next
        printf "%s:%d\n", FNAME, FNR
      }
    ' "$SELFTEST_DIR/defaults/scripts/offender.sh"
)"
assert_contains "$SELFTEST_HIT" "offender.sh:2" "the scan detects a synthetic unguarded sink"
rm -rf "$SELFTEST_DIR"

# Every audited sink must reach the validator, by one of exactly two routes.
# Listed explicitly: this is the inventory a future change has to keep honest,
# and an entry silently losing its validator is exactly the #9106 regression.
#
# Route 1 — the script calls `check_branch_name` itself. Required wherever the
# name is FORGE-derived (a PR's headRefName / base.ref) or operator-supplied
# (`--base`), i.e. wherever it did not come from the resolver.
echo ""
echo "AC2 route 1: forge/operator-supplied names are validated at the sink"
DIRECT_SINKS=(
    'defaults/scripts/merge-pr.sh'
    'defaults/scripts/rebase-stacked-children.sh'
    'defaults/scripts/reconcile-stack.sh'
    'defaults/scripts/worktree.sh'
    'defaults/scripts/lib/worktree-forge-pr-check.sh'
)
for sink in "${DIRECT_SINKS[@]}"; do
    if grep -q 'check_branch_name' "$REPO_ROOT/$sink" 2>/dev/null; then
        pass "$sink calls check_branch_name"
    else
        fail "$sink hands a branch name to git but never calls check_branch_name"
    fi
done

# Route 2 — the script's only branch name comes from `loom_default_branch`,
# which validates its OWN result before echoing it (`default-branch.sh` step 6)
# and returns non-zero otherwise. These scripts therefore cannot receive an
# unsafe name at all, which is strictly stronger than each of them repeating
# the check: a future script that resolves its branch the same way is covered
# the day it is written, with nothing to remember.
#
# Both halves are asserted, because either one alone is satisfiable while the
# guard is gone: that the resolver still validates, AND that each script still
# gets its name from the resolver rather than from somewhere new.
echo ""
echo "AC2 route 2: resolver-derived names are validated inside loom_default_branch"
# Captured then matched with `case`, NOT `awk … | grep -q`: this file runs under
# `set -o pipefail`, and `grep -q` closes the pipe as soon as it matches, so awk
# can take SIGPIPE (141) and fail the whole pipeline. That is the flaky class
# scripts/check-pipefail-early-exit.sh ratchets (#7060/#7285/#7540/#7736).
LDB_BODY="$(awk '/^loom_default_branch\(\) \{/{f=1} f; f && /^}/{exit}' "$LIB")"
case "$LDB_BODY" in
    *check_branch_name*) pass "loom_default_branch validates its own result before echoing it" ;;
    *) fail "loom_default_branch no longer validates its result — every route-2 sink below is unguarded" ;;
esac
for sink in 'defaults/scripts/check-main-freshness.sh' \
            'defaults/scripts/docs-worktree.sh' \
            'defaults/scripts/pr-worktree.sh' \
            'defaults/scripts/land-resync-commit.sh'; do
    if grep -q 'loom_default_branch' "$REPO_ROOT/$sink" 2>/dev/null; then
        pass "$sink takes its branch name from loom_default_branch"
    else
        fail "$sink no longer resolves its branch via loom_default_branch — it now needs its own check_branch_name call"
    fi
done

# …and the behavioural half of route 2, so it is proof rather than structure.
# LOOM_DEFAULT_BRANCH is tier 1 of the resolver and a plain environment
# variable — the cheapest place to inject a switch-shaped name — so it is the
# case worth driving end to end.
if LOOM_DEFAULT_BRANCH='--upload-pack=/tmp/x' loom_default_branch >/dev/null 2>&1; then
    fail "loom_default_branch RETURNED an unsafe LOOM_DEFAULT_BRANCH to its callers"
else
    pass "loom_default_branch refuses an unsafe LOOM_DEFAULT_BRANCH (non-zero, nothing echoed)"
fi
LDB_OUT="$(LOOM_DEFAULT_BRANCH='--upload-pack=/tmp/x' loom_default_branch 2>/dev/null || true)"
if [[ -z "$LDB_OUT" ]]; then
    pass "…and emits nothing on stdout, so a \$(…) caller gets an empty name, never the payload"
else
    fail "loom_default_branch echoed an unsafe name: '$LDB_OUT'"
fi

# The Rust half must stay wired too — `reconcile_stack::plan` is the only
# non-shell path a forge ref reaches a git argv through.
if grep -q 'refname::check_all' "$REPO_ROOT/loom-daemon/src/reconcile_stack.rs" 2>/dev/null; then
    pass "loom-daemon reconcile_stack calls refname::check_all"
else
    fail "loom-daemon/src/reconcile_stack.rs no longer validates its refs (refname::check_all)"
fi

# Two sinks live OUTSIDE defaults/scripts and cannot source the validator: the
# installer runs before `.loom/scripts/lib/` exists, and the uninstaller is
# removing it. Their $DEFAULT_BRANCH comes from `git symbolic-ref
# refs/remotes/origin/HEAD`, which `git remote set-head -a` fills in from the
# REMOTE — the same sink shape, so they carry the `--` half of the mitigation
# and are asserted by name rather than left to the scan above (whose scope is
# defaults/scripts).
echo ""
echo "AC2: dependency-free sinks outside defaults/scripts carry '--'"
for standalone in 'scripts/install/create-worktree.sh' 'scripts/uninstall-loom.sh'; do
    if grep -qE 'git fetch origin -- "\$\{DEFAULT_BRANCH\}"' "$REPO_ROOT/$standalone" 2>/dev/null; then
        pass "$standalone fetches with the '--' end-of-options separator"
    elif grep -qE 'git fetch origin "\$\{DEFAULT_BRANCH\}"' "$REPO_ROOT/$standalone" 2>/dev/null; then
        fail "$standalone fetches \$DEFAULT_BRANCH without '--' (#9106)"
    else
        fail "$standalone: the audited 'git fetch origin \$DEFAULT_BRANCH' sink moved — re-audit it"
    fi
done

# ───────────────────────────────────────────────────────────────────────────
# AC3 — the audit's repro, refused before any git process runs
# ───────────────────────────────────────────────────────────────────────────

echo ""
echo "AC3: path-origin + --upload-pack= payload"

REPRO="$(mktemp -d /tmp/loom-cbn-repro.XXXXXX)"
MARKER="$REPRO/PAYLOAD-EXECUTED"
cat > "$REPRO/payload.sh" <<EOF
#!/bin/sh
: > "$MARKER"
exit 1
EOF
chmod +x "$REPRO/payload.sh"
EVIL_REF="--upload-pack=$REPRO/payload.sh"

git init -q --bare "$REPRO/origin.git"
git init -q -b main "$REPRO/work"
git -C "$REPRO/work" config user.email loom@example.com
git -C "$REPRO/work" config user.name "Loom Test"
git -C "$REPRO/work" config commit.gpgsign false
git -C "$REPRO/work" commit -q --allow-empty -m base
git -C "$REPRO/work" remote add origin "$REPRO/origin.git"
git -C "$REPRO/work" push -q origin main

# Premise: git's own validator accepts the dangerous ref name, which is why
# such a branch can exist on a forge at all.
if git -C "$REPRO/work" check-ref-format "refs/heads/$EVIL_REF" 2>/dev/null; then
    pass "premise: git check-ref-format ACCEPTS 'refs/heads/$EVIL_REF'"
else
    fail "premise: git check-ref-format unexpectedly rejected the payload ref"
fi

# The vector is real — unguarded, the payload executes. Asserted rather than
# assumed, so this stays a regression test of an observed fact.
git -C "$REPRO/work" fetch origin "$EVIL_REF" main >/dev/null 2>&1
if [[ -f "$MARKER" ]]; then
    pass "unguarded 'git fetch origin <payload-ref> main' EXECUTES the payload (vector confirmed)"
else
    fail "the #9106 vector did not reproduce — this environment's git may already block it"
fi

# Mitigation A: the validator refuses, so no git command is reached at all.
rm -f "$MARKER"
if check_branch_name "$EVIL_REF" "head branch" >/dev/null 2>&1; then
    fail "check_branch_name accepted the payload ref"
else
    pass "check_branch_name refuses the payload ref (no git command reached)"
fi
[[ -f "$MARKER" ]] && fail "validator path executed the payload" || pass "…and nothing executed"

# Mitigation B: `--` alone also closes it, independently of A.
rm -f "$MARKER"
git -C "$REPRO/work" fetch origin -- "$EVIL_REF" main >/dev/null 2>&1
[[ -f "$MARKER" ]] && fail "'--' did not stop the payload" || pass "'git fetch origin -- <payload-ref>' does not execute it"

# End to end through a real sink: worktree.sh's --base arm must refuse.
# Asserted on the refusal's OWN wording, not merely on "#9106" — this script is
# invoked for issue 9106, so its ordinary progress output mentions that number
# too and a bare '#9106' match would pass whatever happened.
WT_OUT="$(cd "$REPRO/work" && bash "$SCRIPTS_DIR/worktree.sh" 9106 --base "$EVIL_REF" 2>&1)"
WT_RC=$?
assert_contains "$WT_OUT" "REFUSING this --base branch" "worktree.sh --base <payload-ref> refuses, naming which operand"
assert_contains "$WT_OUT" 'not a safe git ref operand' "…citing the validator's reason"
assert_contains "$WT_OUT" "$REPRO/payload.sh" "…and quoting the offending ref itself"
if [[ $WT_RC -ne 0 ]]; then
    pass "…and exits non-zero"
else
    fail "worktree.sh exited 0 on an unsafe --base"
fi
[[ -f "$MARKER" ]] && fail "worktree.sh executed the payload" || pass "…without executing the payload"

rm -rf "$REPRO"

# ───────────────────────────────────────────────────────────────────────────
# AC4 — merge-pr.sh denies the merge, naming the invalid ref
# ───────────────────────────────────────────────────────────────────────────
#
# Driven through a stub `gh` that reports a PR whose `.head.ref` is the payload
# name. The refusal must fire on the ref name alone — before any merge API call
# and before any git command touches the branch.

echo ""
echo "AC4: merge-pr.sh denies a PR whose headRefName fails validation"

MP="$(mktemp -d /tmp/loom-cbn-mergepr.XXXXXX)"
MP_MARKER="$MP/PAYLOAD-EXECUTED"
MP_EVIL="--upload-pack=$MP/payload.sh"
cat > "$MP/payload.sh" <<EOF
#!/bin/sh
: > "$MP_MARKER"
exit 1
EOF
chmod +x "$MP/payload.sh"

git init -q --bare "$MP/origin.git"
git init -q -b main "$MP/repo"
git -C "$MP/repo" config user.email loom@example.com
git -C "$MP/repo" config user.name "Loom Test"
git -C "$MP/repo" config commit.gpgsign false
git -C "$MP/repo" commit -q --allow-empty -m base
git -C "$MP/repo" remote add origin "$MP/origin.git"
git -C "$MP/repo" push -q origin main
mkdir -p "$MP/repo/.loom/scripts/lib"
cp "$SCRIPTS_DIR/merge-pr.sh" "$MP/repo/.loom/scripts/merge-pr.sh"
cp -R "$SCRIPTS_DIR"/lib/* "$MP/repo/.loom/scripts/lib/" 2>/dev/null || true
chmod +x "$MP/repo/.loom/scripts/merge-pr.sh"

# Stub `gh`: enough of the surface merge-pr.sh reads before it validates the
# head ref. `gh pr merge` / `gh api ... -X PUT` record themselves so a merge
# that slipped past the guard is visible as a recorded call, not just a
# missing error message.
mkdir -p "$MP/bin"
cat > "$MP/bin/gh" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *"repo view"*)   echo '{"nameWithOwner":"loom/test","defaultBranchRef":{"name":"main"}}' ;;
  *"auth status"*) exit 0 ;;
  "api repos/loom/test --jq"*) echo '{"push":true}' ;;
  *"pr merge"*|*"-X PUT"*|*"--method PUT"*)
                   echo "MERGE-ATTEMPTED \$*" >> "$MP/gh-calls.log"; echo '{}' ;;
  *)               echo '{"state":"open","merged":false,"mergeable":true,"title":"t","number":1,"labels":[{"name":"loom:pr"}],"head":{"ref":"$MP_EVIL","sha":"deadbeef"},"base":{"ref":"main"}}' ;;
esac
EOF
chmod +x "$MP/bin/gh"

# merge-pr.sh vets its target through the real #9548 write scope before it
# reaches the ref check, so the fixture is registered as a repository this
# installation may write to rather than stubbed past the check: its origin
# names loom/test (the repo the stub gh resolves), it is a Loom-installed
# checkout (.loom/ above), and the stub gh answers the permission probe with
# push. With a loom-daemon on PATH the real `forge may-write` decides; without
# one, the shell fallback admits it because origin is the only remote. Setup
# pushed to the local bare repo above; nothing after this point fetches.
git -C "$MP/repo" remote set-url origin https://github.com/loom/test.git
MP_OUT="$(cd "$MP/repo" && PATH="$MP/bin:$PATH" GH_TOKEN=x \
    LOOM_GH_BIN="$MP/bin/gh" LOOM_WRITE_SCOPE_CACHE_DIR="$MP/write-scope-cache" \
    env -u GH_REPO -u LOOM_REPO \
    bash .loom/scripts/merge-pr.sh 1 2>&1)"
MP_RC=$?

assert_contains "$MP_OUT" "$MP_EVIL" "merge-pr.sh names the invalid head ref in its refusal"
assert_contains "$MP_OUT" '#9106' "merge-pr.sh cites the issue in its refusal"
# The refusal must be THIS one, not some unrelated failure that happens to echo
# the branch name back.
assert_contains "$MP_OUT" 'Merge blocked' "merge-pr.sh refuses for the ref-operand reason specifically"
if [[ $MP_RC -ne 0 ]]; then
    pass "merge-pr.sh exits non-zero (merge denied)"
else
    fail "merge-pr.sh exited 0 on a PR with an unsafe head ref"
fi
if [[ -s "$MP/gh-calls.log" ]]; then
    fail "merge-pr.sh attempted the merge anyway: $(cat "$MP/gh-calls.log")"
else
    pass "…with no merge API call attempted"
fi
if [[ -f "$MP_MARKER" ]]; then
    fail "merge-pr.sh executed the payload"
else
    pass "…and no git command executed the payload"
fi

rm -rf "$MP"

# --- Summary ----------------------------------------------------------------
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
