# Judge Reference

Detailed reference material for the Judge role, split out of `judge.md` (the largest
prompt in the repo) to keep the main workflow scannable — mirroring how `champion.md`
references `champion-reference.md` / `champion-common.md`.

Read the relevant section when `judge.md` points you here.

---

## Scoped Test Execution

When running quality checks (step 7), use **scoped test execution** to run only the tests relevant to changed files. This reduces evaluation time while maintaining confidence that changed code is correct.

### Step 1: Detect Changed Files

```bash
# Use gh API to list changed files — avoids local git dependency and
# exit-128 errors when the branch is checked out in a worktree or when
# concurrent builder operations hold a git lock. (issue #2828)
CHANGED_FILES=$(gh pr diff $PR_NUMBER --name-only 2>/dev/null)
if [ -z "$CHANGED_FILES" ]; then
    echo "Warning: Could not detect changed files via gh pr diff — running full test suite"
    # Fall through to full suite
fi
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

**Important**: Always use `python3`, never bare `python` — `python` is not in PATH on macOS or most modern Linux systems.

**CRITICAL: Use `./.loom/scripts/run-tests.sh` instead of bare `python3 -m pytest` in worktrees**

Loom installs `loom-tools` as an editable package from the main repo root. When you `cd` into an
issue worktree (`.loom/worktrees/issue-N`) and run `python3 -m pytest`, Python imports from the
*main branch's* source — not the worktree's code. This produces false test failures for any PR
that modifies `loom-tools`. (Observed in PR #2818 review.)

`./.loom/scripts/run-tests.sh` detects the worktree automatically and sets
`PYTHONPATH=<worktree>/loom-tools/src` before invoking pytest, ensuring tests import the
worktree's version. Use it everywhere you would otherwise call `python3 -m pytest`.

**Preferred: Use `pytest-testmon` when available**

```bash
# Use run-tests.sh wrapper — sets PYTHONPATH automatically when inside a worktree
if ./.loom/scripts/run-tests.sh --co --testmon 2>/dev/null; then
    # Check if .testmondata exists and is reasonably current
    if [ -f .testmondata ]; then
        TESTMON_AGE=$(( $(date +%s) - $(stat -f %m .testmondata 2>/dev/null || stat -c %Y .testmondata 2>/dev/null) ))
        if [ "$TESTMON_AGE" -lt 86400 ]; then
            echo "Using pytest-testmon for scoped test execution"
            ./.loom/scripts/run-tests.sh --testmon -x -q
            SCOPED_STRATEGY="pytest-testmon"
        else
            echo "Testmon data is stale (>24h) — falling back to full pytest"
            ./.loom/scripts/run-tests.sh -x -q
            SCOPED_STRATEGY="full-pytest (stale testmon data)"
        fi
    else
        echo "No .testmondata found — running full pytest (consider installing pytest-testmon)"
        ./.loom/scripts/run-tests.sh -x -q
        SCOPED_STRATEGY="full-pytest (no testmon data)"
    fi
else
    echo "pytest-testmon not available — running full pytest"
    ./.loom/scripts/run-tests.sh -x -q
    SCOPED_STRATEGY="full-pytest (testmon not installed)"
fi
```

**Recommendation if testmon is unavailable:**
Note in evaluation comment: "Consider installing `pytest-testmon` (`pip install pytest-testmon`) for faster scoped test execution in future reviews."

#### JavaScript/TypeScript Repositories

**Detect and use the project's test runner:**

```bash
# Check for Jest
if npx jest --version 2>/dev/null; then
    echo "Using Jest with --changedSince for scoped tests"
    npx jest --changedSince=origin/main
    SCOPED_STRATEGY="jest --changedSince"

# Check for Vitest
elif npx vitest --version 2>/dev/null; then
    echo "Using Vitest with --changed for scoped tests"
    npx vitest run --changed origin/main
    SCOPED_STRATEGY="vitest --changed"

# Fallback: run whatever test script is configured
else
    echo "No Jest or Vitest detected — running configured test script"
    npm test 2>/dev/null || pnpm test 2>/dev/null || yarn test 2>/dev/null
    SCOPED_STRATEGY="full-test-script (no scoping tool detected)"
fi
```

#### Rust Repositories

**Scope to changed crates in workspace projects:**

```bash
# Check if this is a Cargo workspace
if grep -q '^\[workspace\]' Cargo.toml 2>/dev/null; then
    # Find which crates have changed files
    CHANGED_CRATES=$(echo "$CHANGED_FILES" | grep '\.rs$' | \
        sed 's|/.*||' | sort -u | \
        while read dir; do
            if [ -f "$dir/Cargo.toml" ]; then
                grep '^name' "$dir/Cargo.toml" | head -1 | sed 's/name *= *"\(.*\)"/\1/'
            fi
        done)

    if [ -n "$CHANGED_CRATES" ]; then
        echo "Scoping Rust tests to changed crates: $CHANGED_CRATES"
        for crate in $CHANGED_CRATES; do
            cargo test -p "$crate"
        done
        SCOPED_STRATEGY="cargo test -p ($(echo $CHANGED_CRATES | tr '\n' ', '))"
    else
        echo "Changed Rust files not in identifiable crates — running full cargo test"
        cargo test --workspace
        SCOPED_STRATEGY="full-cargo-test"
    fi
else
    # Single-crate project, just run tests
    cargo test
    SCOPED_STRATEGY="cargo-test (single crate)"
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

**Strategy**: `pytest-testmon`
**Changed files**: 3 Python files in `src/utils/`
**Scoped result**: 12 tests selected, all passed
**Note**: Full suite has 847 tests; scoped execution covered tests affected by changes.
```

Or when falling back:

```markdown
## Test Scoping

**Strategy**: `full-suite` (config files changed)
**Reason**: PR modifies `pyproject.toml` — full test suite required
**Result**: 847 tests, all passed
```

Or when recommending a missing tool:

```markdown
## Test Scoping

**Strategy**: `full-pytest` (testmon not installed)
**Result**: 847 tests, all passed
**Recommendation**: Consider installing `pytest-testmon` for faster scoped test execution in future reviews.
```

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| PR touches only docs/markdown | Skip test execution entirely (no code changes) |
| PR touches files in multiple languages | Run scoped tests for each language independently |
| Scoped tests pass but you suspect missed coverage | Note in evaluation; do not block approval |
| `pytest-testmon` DB is from wrong branch | Fall back to full pytest (check DB age) |
| No test framework detected | Note absence in evaluation; check if project has tests at all |
| PR touches shared utilities | Scoped tools may miss downstream tests — note this risk in evaluation |

### Why Scoped Test Execution Matters

| Metric | Full Suite | Scoped |
|--------|-----------|--------|
| Typical duration | 2-10 minutes | 10-60 seconds |
| Tests executed | All | Only affected |
| Confidence | Maximum | High (with caveats) |
| Use case | Config changes, first run | Focused code changes |

**Key principle**: Scoped execution is an optimization, not a replacement for CI. The full test suite still runs in CI (step 8 verifies CI status). Scoped execution gives the Judge faster local feedback during evaluation.

---

## Terminal Probe Protocol

When you receive a probe command, respond with: `AGENT:Judge:<brief-task>` — e.g. `AGENT:Judge:evaluating-PR-123`.

**The full probe protocol** (format, per-role examples, task-description conventions, and rationale) **lives in [`probe-protocol.md`](probe-protocol.md).**
