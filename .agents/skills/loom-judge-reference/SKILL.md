---
name: loom-judge-reference
description: "Detailed reference material for the Judge role, split out of `judge.md` (the largest prompt in the repo) to keep the main workflow scannable — mirroring how `champion.md` references `champion-reference.md` / `champion-common.md`."
---
<!-- loom-managed-skill -->
<!-- GENERATED FILE — DO NOT EDIT DIRECTLY.
     Produced by `loom-daemon generate-agent-skills` from
     defaults/.claude/commands/loom/judge-reference.md (the same source Claude Code
     reads as /loom:judge-reference via .claude/commands/loom/). This is the
     cross-vendor skill-discovery surface (.agents/skills/<name>/SKILL.md)
     read natively by Codex, Kimi Code, Mistral Vibe, and Grok — see
     runtime-adapters.md §5. To change this file, edit the source above
     and re-run the generator; CI (`loom-daemon generate-agent-skills
     --check`) fails if this file is stale. -->

# Judge Reference

Detailed reference material for the Judge role, split out of `judge.md` (the largest
prompt in the repo) to keep the main workflow scannable — mirroring how `champion.md`
references `champion-reference.md` / `champion-common.md`.

Read the relevant section when `judge.md` points you here.

---

## Scoped Test Execution

When running quality checks (step 7), use **scoped test execution** to run only the tests relevant to changed files. This cuts evaluation time while keeping confidence that changed code is correct.

### Step 1: Detect Changed Files

```bash
# gh, not local git: avoids exit-128 when the branch is checked out in a
# worktree or a concurrent builder holds the git lock (#2828).
CHANGED_FILES=$(gh pr diff $PR_NUMBER --name-only 2>/dev/null)
# Empty result = detection failed; fall through to the full suite.
echo "$CHANGED_FILES"
```

### Step 2: Check for Config File Changes

If the PR touches configuration files that affect the entire project, **skip scoping and run the full test suite**:

```bash
# Config files that should trigger full suite
CONFIG_PATTERNS="pyproject.toml|setup.cfg|setup.py|package.json|pnpm-lock.yaml|yarn.lock|Cargo.toml|Cargo.lock|tsconfig.json|jest.config|vitest.config|.eslintrc|Makefile|CMakeLists"

if echo "$CHANGED_FILES" | grep -qE "($CONFIG_PATTERNS)"; then
    echo "Config files changed — running full test suite"
    # Run full suite (skip to Fallback section below)
fi
```

### Step 3: Classify Changed Files by Language

Classify the changed files to determine which scoped test strategies to apply:

| Extension/Path | Language | Scoped Strategy |
|----------------|----------|-----------------|
| `.py`, `.pyi` | Python | `pytest --testmon` or full pytest |
| `.ts`, `.tsx` | TypeScript | `jest --changedSince` or `vitest --changed` |
| `.js`, `.jsx`, `.mjs`, `.cjs` | JavaScript | `jest --changedSince` or `vitest --changed` |
| `.rs` | Rust | `cargo test -p <crate>` |
| Other | Unknown | Full test suite |

### Step 4: Run Scoped Tests by Language

#### Python Repositories

**Important**: use `python3`, never bare `python` (not in PATH on macOS or modern Linux).

**CRITICAL: Use `./.loom/scripts/run-tests.sh` instead of bare `python3 -m pytest` in worktrees.**
With an editable install (`pip install -e .`) the `.pth` entry points at the *main* checkout, so
`python3 -m pytest` inside `.loom/worktrees/issue-N` imports main's code, not the PR's —
results that describe the wrong tree (observed in PR #2818). `run-tests.sh` detects the worktree
and prepends its source root(s) to `PYTHONPATH` first. Use it everywhere you would call pytest.

> **Loom's own repo is not a Python repo — and has no Python at all** (epic #4081 Phase 4 /
> #4557 / #4970). Its orchestration layer is the `loom-daemon` binary plus bash, so on a Loom PR
> the relevant suites are `cargo test --workspace` and the bash suites under
> `defaults/scripts/tests/` + `scripts/test-installer.sh`. This "Python Repositories" section
> applies to the other repos Loom orchestrates, not to Loom's own repo.

**Preferred: Use `pytest-testmon` when available**

```bash
# run-tests.sh sets PYTHONPATH automatically when inside a worktree.
# Use testmon only when its data exists and is <24h old; otherwise full pytest.
if ./.loom/scripts/run-tests.sh --co --testmon 2>/dev/null && [ -f .testmondata ] &&
   [ $(( $(date +%s) - $(stat -f %m .testmondata 2>/dev/null || stat -c %Y .testmondata) )) -lt 86400 ]; then
    ./.loom/scripts/run-tests.sh --testmon -x -q
    SCOPED_STRATEGY="pytest-testmon"
else
    ./.loom/scripts/run-tests.sh -x -q
    SCOPED_STRATEGY="full-pytest (testmon missing/stale/not installed)"
fi
```

**Recommendation if testmon is unavailable:**
Note in evaluation comment: "Consider installing `pytest-testmon` (`pip install pytest-testmon`) for faster scoped test execution in future reviews."

#### JavaScript/TypeScript Repositories

**Detect the project's test runner:**

```bash
if npx jest --version 2>/dev/null; then
    npx jest --changedSince=origin/main
    SCOPED_STRATEGY="jest --changedSince"
elif npx vitest --version 2>/dev/null; then
    npx vitest run --changed origin/main
    SCOPED_STRATEGY="vitest --changed"
else   # no scoping tool — run whatever test script is configured
    npm test 2>/dev/null || pnpm test 2>/dev/null || yarn test 2>/dev/null
    SCOPED_STRATEGY="full-test-script (no scoping tool detected)"
fi
```

#### Rust Repositories

**Scope to changed crates in workspace projects:**

```bash
# Map each changed .rs file to its crate name; fall back to the full workspace.
CHANGED_CRATES=$(echo "$CHANGED_FILES" | grep '\.rs$' | sed 's|/.*||' | sort -u |
    while read -r dir; do
        [ -f "$dir/Cargo.toml" ] && sed -n 's/^name *= *"\(.*\)"/\1/p' "$dir/Cargo.toml" | head -1
    done)

if grep -q '^\[workspace\]' Cargo.toml 2>/dev/null && [ -n "$CHANGED_CRATES" ]; then
    for crate in $CHANGED_CRATES; do cargo test -p "$crate"; done
    SCOPED_STRATEGY="cargo test -p ($(echo "$CHANGED_CRATES" | tr '\n' ','))"
else
    cargo test --workspace   # single crate, or changed files not in an identifiable one
    SCOPED_STRATEGY="full-cargo-test"
fi
```

### Step 5: Fallback to Full Suite

Run the full test suite when:
- Config files are changed (detected in step 2)
- Changed files span unknown languages
- Scoped tools are not available
- First run in a repository with no scoping data

```bash
# Generic fallback — use whatever the project's standard check command is
pnpm check:ci 2>/dev/null || \
    npm test 2>/dev/null || \
    ./.loom/scripts/run-tests.sh 2>/dev/null || \
    cargo test 2>/dev/null || \
    make test 2>/dev/null
SCOPED_STRATEGY="full-suite (fallback)"
```

### Step 6: Document Strategy in Evaluation Comment

**Always log which scoping strategy was used.** Include a "Test Scoping" section in your evaluation comment:

```markdown
## Test Scoping

**Strategy**: `pytest-testmon`   <!-- or: `full-suite` (config files changed) -->
**Changed files**: 3 Python files in `src/utils/`
**Scoped result**: 12 tests selected, all passed
**Note**: Full suite has 847 tests; scoped execution covered tests affected by changes.
**Recommendation**: (only when a scoping tool is missing) install `pytest-testmon`.
```

When falling back, give the reason on the Strategy line (`full-suite` (config files changed), `full-pytest` (testmon not installed), …) and report the full-suite result instead of a scoped one.

### Merge-Base Run for a `TDD: yes` Claim

`judge.md` → "Test-First (TDD) Claim Verification" requires a `TDD: yes — <path>` claim to be *falsified*, not just path-matched: the referenced test must **fail on the merge-base tree**. Build that tree with `git archive` (no checkout, no stash, no `git worktree` — safe while the PR branch is checked out elsewhere), lay the PR's test file on top, and run it.

```bash
# tdd_merge_base_run <base-ref> <test-path> <run command...>   (#8265)
# Run from the PR head. Echoes VERIFIED / CONTRADICTED / UNRUNNABLE.
# Exit 0 = verified (failed at base), 1 = contradicted (passed), 3 = unrunnable.
tdd_merge_base_run() {
    local base="$1" test_path="$2"; shift 2
    local tree out rc
    tree=$(mktemp -d)
    # The test file is the ONE thing taken from head, so a test needing other
    # new files reports UNRUNNABLE rather than a false VERIFIED. pipefail:
    # git archive's failure must not be masked by tar's exit status.
    if ! (set -o pipefail; git archive "$base" | tar -x -C "$tree") 2>/dev/null ||
       ! (set -o pipefail; git archive HEAD -- "$test_path" | tar -x -C "$tree") 2>/dev/null; then
        rm -rf "$tree"
        echo "UNRUNNABLE: cannot build $base tree + $test_path"; return 3
    fi
    out=$(cd "$tree" && "$@" 2>&1); rc=$?
    rm -rf "$tree"
    printf '%s\n' "$out" | tail -5
    if [ "$rc" -eq 0 ]; then
        echo "CONTRADICTED: $test_path passes at $base — it does not test the fix"; return 1
    elif [ "$rc" -ge 126 ]; then
        echo "UNRUNNABLE: command exited $rc at $base (harness/env limit, not a test failure)"; return 3
    fi
    echo "VERIFIED: $test_path fails at $base (exit $rc)"
}
```

The run command is the Step 4 scoped one narrowed to that path (`run-tests.sh <path>`, `npx vitest run <path>`, `cargo test --test <name>`, `bash <path>`).

**Reading the result.** `VERIFIED` is necessary, not sufficient: check the tail output and confirm it failed *for the reason the fix addresses*, not on an unrelated import error. `CONTRADICTED` is the blocking row; `UNRUNNABLE` is the advisory one — name what blocked the run, and never record it as verified.

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| PR touches only docs/markdown | Skip test execution entirely (no code changes) |
| PR touches files in multiple languages | Run scoped tests for each language independently |
| Scoped tests pass but you suspect missed coverage | Note in evaluation; do not block approval |
| No test framework detected | Note absence in evaluation; check if project has tests at all |
| PR touches shared utilities | Scoped tools may miss downstream tests — note this risk in evaluation |

**Key principle**: Scoped execution is an optimization, not a replacement for CI — the full suite still runs there (step 8 verifies CI status); this just gives the Judge faster local feedback. Duration/confidence comparison: `.loom/docs/judge-reference-rationale.md`.

---

## Terminal Probe Protocol

When you receive a probe command, respond with: `AGENT:Judge:<brief-task>` — e.g. `AGENT:Judge:evaluating-PR-123`.

**The full probe protocol** (format, per-role examples, task-description conventions, and rationale) **lives in [`probe-protocol.md`](probe-protocol.md).**
