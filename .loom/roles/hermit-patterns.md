# Hermit Patterns Reference

This file contains detailed patterns, examples, and reference scripts for the Hermit role. It is meant to be consulted when needed, not loaded as primary instructions.

**When to use this file**:
- Looking for specific code smell examples
- Need detailed random file review workflow
- Implementing goal discovery scripts
- Checking command reference syntax
- Need worktree cleanup implementation

**Primary instructions**: See `hermit.md` for core role definition and workflow.

---

## Detailed Code Smell Examples

Look for these patterns that often indicate bloat:

### 1. Unnecessary Abstraction

```typescript
// BAD: Over-abstracted
class DataFetcherFactory {
  createFetcher(): DataFetcher {
    return new ConcreteDataFetcher(new HttpClient());
  }
}

// GOOD: Direct and simple
async function fetchData(url: string): Promise<Data> {
  return fetch(url).then(r => r.json());
}
```

### 2. One-Method Classes

```typescript
// BAD: Class with single method
class UserValidator {
  validate(user: User): boolean {
    return user.email && user.name;
  }
}

// GOOD: Just a function
function validateUser(user: User): boolean {
  return user.email && user.name;
}
```

### 3. Unused Configuration

```typescript
// Configuration options that are never changed from defaults
const config = {
  maxRetries: 3,        // Always 3 in practice
  timeout: 5000,        // Never customized
  enableLogging: true   // Never turned off
};
```

### 4. Generic Utilities That Are Used Once

```typescript
// Utility function used in exactly one place
function mapArrayToObject<T>(arr: T[], keyFn: (item: T) => string): Record<string, T>
```

### 5. Premature Generalization

```typescript
// Supporting 10 database types when only using one
interface DatabaseAdapter { /* complex interface */ }
class PostgresAdapter implements DatabaseAdapter { /* ... */ }
class MySQLAdapter implements DatabaseAdapter { /* never used */ }
class MongoAdapter implements DatabaseAdapter { /* never used */ }
```

### Additional Code Smells to Watch

```typescript
// One-method class (should be function)
class DataTransformer {
  transform(data: Data, options: Options): Result {
    // ...implementation
  }
}

// Over-parameterized function
function process(a, b, c, d, e, f, g, h) { /* ... */ }

// Unnecessary abstraction
interface IDataFetcher {
  fetch(): Data;
}
class DataFetcherFactory {
  create(): IDataFetcher { /* ... */ }
}

// Generic utility used once
function mapToObject<T>(arr: T[], keyFn: (item: T) => string) { /* only 1 caller */ }

// Commented-out code
// function oldMethod() {
//   return "deprecated behavior";
// }
```

---

## Analysis Scripts

### Dependency Analysis

```bash
# Frontend: Check for unused npm packages
cd {{workspace}}
npx depcheck

# Backend: Check Cargo.toml vs actual usage
rg "use.*::" --type rust | cut -d':' -f3 | sort -u
```

### Dead Code Detection

```bash
# Find exports with no external references
rg "export (function|class|const|interface)" --type ts -n

# For each export, check if it's imported elsewhere
# If no imports found outside its own file, it's dead code
```

### Complexity Metrics

```bash
# Find large files (often over-engineered)
find . -name "*.ts" -o -name "*.rs" | xargs wc -l | sort -rn | head -20

# Find files with many imports (tight coupling)
rg "^import" --count | sort -t: -k2 -rn | head -20
```

### Historical Analysis

```bash
# Find files that haven't changed in a long time (potential for removal)
git log --all --format='%at %H' --name-only | \
  awk 'NF==2{t=$1; next} {print t, $0}' | \
  sort -k2 | uniq -f1 | sort -rn | tail -20

# Find features added but never modified (possible unused)
git log --diff-filter=A --name-only --pretty=format: | \
  sort -u | while read file; do
    commits=$(git log --oneline -- "$file" | wc -l)
    if [ $commits -eq 1 ]; then
      echo "$file (only 1 commit - added but never touched)"
    fi
  done
```

---

## Random File Review - Detailed Workflow

### What Makes a Good Candidate

**High-value targets for random review:**

| Indicator | Threshold | Why It Matters |
|-----------|-----------|----------------|
| **File Size** | > 300 lines | May be doing too much, candidate for splitting |
| **Imports** | 10+ imports | Tight coupling, complex dependencies |
| **Nesting Depth** | 4+ levels | Complex control flow, hard to reason about |
| **Class Methods** | 1-2 methods | Should probably be functions |
| **Parameters** | 5+ params | Over-parameterized, needs refactoring |
| **Comments/Code Ratio** | > 30% | Either over-documented or has dead code |
| **Cyclomatic Complexity** | High branching | Many if/else, switch, match statements |

### What to Skip

**Don't waste time on:**

- **Tests** - Verbosity is acceptable, test clarity > brevity
- **Type definitions** - Long type files are normal (`**/*.d.ts`, interfaces)
- **Generated code** - Can't simplify auto-generated files
- **Small files** - < 50 lines are already concise
- **Recent files** - < 2 weeks old, let them stabilize
- **Config files** - Often need all options even if unused
- **Already flagged** - Check existing issues to avoid duplicates

```bash
# Before creating an issue, check for duplicates
gh issue list --search "filename.ts" --state=open
```

### Example Decision Process

**Scenario 1: Good Candidate**

```bash
# Random file: src/lib/data-transformer.ts
$ wc -l src/lib/data-transformer.ts
487 src/lib/data-transformer.ts

$ head -30 src/lib/data-transformer.ts | grep "import" | wc -l
15

$ rg "class " src/lib/data-transformer.ts
export class DataTransformer {

$ rg "transform\(" src/lib/data-transformer.ts --count
1

# Decision: 487 lines, 15 imports, class with complex transform method
# -> CREATE ISSUE: "Simplify data-transformer: extract logic, reduce params"
```

**Scenario 2: Already Simple**

```bash
# Random file: src/lib/logger.ts
$ wc -l src/lib/logger.ts
67 src/lib/logger.ts

$ head -20 src/lib/logger.ts
// Clean, well-structured logger utility
// Minimal dependencies, clear purpose

# Decision: 67 lines, clean structure, does one thing well
# -> SKIP: Already simple and focused
```

**Scenario 3: Marginal Value**

```bash
# Random file: src/components/Button.tsx
$ wc -l src/components/Button.tsx
142 src/components/Button.tsx

# Scan shows: Could reduce from 142 to ~120 lines
# Effort: 1 hour, LOC saved: ~20 lines, Risk: UI changes

# Decision: Small improvement, low ROI
# -> SKIP: Not worth the effort for 20 line reduction
```

### Random File Review Issue Template

```bash
gh issue create --title "Simplify <filename>: <specific improvement>" --body "$(cat <<'EOF'
## What to Simplify

<file-path> - <specific bloat identified>

## Why It's Bloat

<evidence from your scan>

Examples:
- "487 lines with 15 imports - class could be 3 simple functions"
- "One-method class with 8 parameters - should be a pure function"
- "50 lines of commented-out code from 6 months ago"

## Evidence

```bash
# Commands you ran
wc -l src/lib/data-transformer.ts
# Output: 487 lines

rg "class " src/lib/data-transformer.ts
# Output: Only 1 class with 3 methods, 2 private
```

## Impact Analysis

**Files Affected**: <list>
**LOC Removed**: ~<estimate>
**Complexity Reduction**: <description>

## Benefits of Simplification

- Reduced from 487 to ~150 lines
- Eliminated 8 unnecessary parameters
- Converted class to 3 pure functions
- Easier to test and maintain

## Proposed Approach

1. Extract internal methods to separate pure functions
2. Simplify transform() signature (8 params -> 2 params + options object)
3. Add unit tests for new functions
4. Update call sites (only 3 locations)

## Risk Assessment

**Risk Level**: Low
**Reasoning**: Only 3 call sites, easy to verify with tests

EOF
)" --label "loom:hermit"
```

---

## Goal Discovery Scripts

### Goal Discovery Function

Run goal discovery at the START of every autonomous scan:

```bash
# ALWAYS run goal discovery before creating proposals
discover_project_goals() {
  echo "=== Project Goals Discovery ==="

  # 1. Check README for milestones
  if [ -f README.md ]; then
    echo "Current milestone from README:"
    grep -i "milestone\|current:\|target:" README.md | head -5
  fi

  # 2. Check roadmap
  if [ -f docs/roadmap.md ] || [ -f ROADMAP.md ]; then
    echo "Roadmap deliverables:"
    grep -E "^- \[.\]|^## M[0-9]" docs/roadmap.md ROADMAP.md 2>/dev/null | head -10
  fi

  # 3. Check for urgent/high-priority goal-advancing issues
  echo "Current goal-advancing work:"
  gh issue list --label="tier:goal-advancing" --state=open --limit=5
  gh issue list --label="loom:urgent" --state=open --limit=5

  # 4. Summary
  echo "Simplification proposals should support these focus areas"
}

# Run goal discovery
discover_project_goals
```

### Backlog Balance Check

**Run this before creating proposals** to ensure the backlog has healthy distribution:

```bash
check_backlog_balance() {
  echo "=== Backlog Tier Balance ==="

  # Count issues by tier
  tier1=$(gh issue list --label="tier:goal-advancing" --state=open --json number --jq 'length')
  tier2=$(gh issue list --label="tier:goal-supporting" --state=open --json number --jq 'length')
  tier3=$(gh issue list --label="tier:maintenance" --state=open --json number --jq 'length')
  unlabeled=$(gh issue list --label="loom:issue" --state=open --json number,labels \
    --jq '[.[] | select([.labels[].name] | any(startswith("tier:")) | not)] | length')

  total=$((tier1 + tier2 + tier3 + unlabeled))

  echo "Tier 1 (goal-advancing): $tier1"
  echo "Tier 2 (goal-supporting): $tier2"
  echo "Tier 3 (maintenance):     $tier3"
  echo "Unlabeled:                $unlabeled"
  echo "Total ready issues:       $total"

  # Check balance
  if [ "$tier1" -eq 0 ] && [ "$total" -gt 3 ]; then
    echo ""
    echo "WARNING: No goal-advancing issues in backlog!"
    echo "RECOMMENDATION: Prioritize simplifications that support current milestone work."
  fi

  if [ "$tier3" -gt "$tier1" ] && [ "$tier3" -gt 5 ]; then
    echo ""
    echo "WARNING: More maintenance issues than goal-advancing issues."
    echo "RECOMMENDATION: Focus on simplifications that directly benefit active work."
  fi
}

# Run the check
check_backlog_balance
```

**Interpretation**:
- **Healthy**: Tier 1 >= Tier 3, and at least 1-2 goal-advancing issues available
- **Warning**: No goal-advancing issues, or maintenance dominates
- **Action**: If unhealthy, focus simplification proposals on Tier 1 opportunities

### Parallel Execution Example

When running autonomously (every 15 minutes), each Hermit run randomly selects ONE check:

```bash
# 5 Hermits running simultaneously at 3:00 PM

# Hermit Terminal 1 (random selection: dead-code)
cd {{workspace}}
rg "export.*function|export.*class" -n
# Check which exports are never imported
# -> Found unused function, create issue

# Hermit Terminal 2 (random selection: random-file)
mcp__loom__get_random_file
cat <file-path>
# -> Found over-engineered class, create issue

# Hermit Terminal 3 (random selection: unused-dependencies)
npx depcheck
# -> Found @types/jsdom, create issue

# Hermit Terminal 4 (random selection: commented-code)
rg "^\\s*//.*{|^\\s*//.*function" -n
# -> Found old commented functions, create issue

# Hermit Terminal 5 (random selection: old-todos)
rg "TODO|FIXME" -n --context 2
git log --all --format=%cd --date=short <file> | head -1
# -> Found TODOs from 2023, create issue

# Result: All 5 Hermits performed different checks, no duplicates!
```

---

## Creating Removal Proposals - Full Templates

### Standalone Issue Template

```bash
gh issue create --title "Remove [specific thing]: [brief reason]" --body "$(cat <<'EOF'
## What to Remove

[Specific file, function, dependency, or feature]

## Why It's Bloat

[Evidence that this is unused, over-engineered, or unnecessary]

Examples:
- "No imports found outside of its own file"
- "Dependency not imported anywhere: `rg 'library-name' returned 0 results"
- "Function defined 6 months ago, never called: `git log` shows no subsequent changes"
- "3-layer abstraction for what could be a single function"

## Evidence

```bash
# Commands you ran to verify this is bloat
rg "functionName" --type ts
# Output: [show the results]
```

## Impact Analysis

**Files Affected**: [list of files that reference this code]
**Dependencies**: [what depends on this / what this depends on]
**Breaking Changes**: [Yes/No - explain if yes]
**Alternative**: [If removing functionality, what's the simpler alternative?]

## Benefits of Removal

- **Lines of Code Removed**: ~[estimate]
- **Dependencies Removed**: [list any npm/cargo packages that can be removed]
- **Maintenance Burden**: [Reduced complexity, fewer tests to maintain, etc.]
- **Build Time**: [Any impact on build/test speed]

## Proposed Approach

1. [Step-by-step plan for removal]
2. [How to verify nothing breaks]
3. [Tests to update/remove]

## Risk Assessment

**Risk Level**: [Low/Medium/High]
**Reasoning**: [Why this risk level]

EOF
)" --label "loom:hermit"
```

### Example Standalone Issue

```bash
gh issue create --title "Remove unused UserSerializer class" --body "$(cat <<'EOF'
## What to Remove

`src/lib/serializers/user-serializer.ts` - entire file

## Why It's Bloat

This class was created 8 months ago but is never imported or used anywhere in the codebase.

## Evidence

```bash
# Check for any imports of UserSerializer
$ rg "UserSerializer" --type ts
src/lib/serializers/user-serializer.ts:1:export class UserSerializer {

# Only result is the definition itself - no imports
```

```bash
# Check git history
$ git log --oneline src/lib/serializers/user-serializer.ts
a1b2c3d Add UserSerializer for future API work
# Only 1 commit - added but never used
```

## Impact Analysis

**Files Affected**: None (no imports)
**Dependencies**: None
**Breaking Changes**: No - nothing uses this code
**Alternative**: Not needed - we serialize users directly in API handlers

## Benefits of Removal

- **Lines of Code Removed**: ~87 lines
- **Dependencies Removed**: None (but simplifies serializers/ directory)
- **Maintenance Burden**: One less class to maintain/test
- **Build Time**: Negligible improvement

## Proposed Approach

1. Delete `src/lib/serializers/user-serializer.ts`
2. Run `pnpm check:ci` to verify nothing breaks
3. Remove associated test file if it exists
4. Commit with message: "Remove unused UserSerializer class"

## Risk Assessment

**Risk Level**: Low
**Reasoning**: No imports means no code depends on this. Safe to remove.

EOF
)" --label "loom:hermit"
```

### Comment Template (for existing issues)

```bash
gh issue comment <number> --body "$(cat <<'EOF'
<!-- CRITIC-SUGGESTION -->
## Simplification Opportunity

While reviewing this issue, I identified potential bloat that could simplify the implementation:

### What Could Be Removed/Simplified

[Specific code, dependency, or complexity that could be eliminated]

### Why This Simplifies the Issue

[Explain how removing this reduces scope, complexity, or dependencies for this issue]

Examples:
- "Removing this abstraction layer would eliminate 3 files from this implementation"
- "This dependency is only used here - removing it reduces the PR scope"
- "This feature is unused - we don't need to maintain it in this refactor"

### Evidence

```bash
# Commands you ran to verify this is bloat/unnecessary
rg "functionName" --type ts
# Output: [show the results]
```

### Impact on This Issue

**Current Scope**: [What the issue currently requires]
**Simplified Scope**: [What it would require if this suggestion is adopted]
**Lines Saved**: ~[estimate]
**Complexity Reduction**: [How this makes the issue simpler to implement]

### Recommended Action

1. [How to incorporate this simplification into the issue]
2. [What to remove from the implementation plan]
3. [Updated test plan if needed]

---
*This is a Critic suggestion to reduce complexity. The assignee can choose to adopt, adapt, or ignore this recommendation.*
EOF
)"
```

### Example Comment

```bash
gh issue comment 42 --body "$(cat <<'EOF'
<!-- CRITIC-SUGGESTION -->
## Simplification Opportunity

While reviewing issue #42 (Add user profile editor), I identified potential bloat that could simplify the implementation:

### What Could Be Removed/Simplified

The `ProfileValidator` class in `src/lib/validators/profile-validator.ts` - this entire abstraction layer

### Why This Simplifies the Issue

This issue proposes adding a user profile editor. The current plan includes creating a `ProfileValidator` class, but we can use inline validation instead, reducing the scope from 3 files to 1.

### Evidence

```bash
# Check where ProfileValidator would be used
$ rg "ProfileValidator" --type ts
# No results - it doesn't exist yet, but the issue proposes creating it

# Check existing validation patterns
$ rg "validate" src/components/ --type ts
src/components/LoginForm.tsx:  const isValid = email && password; // inline validation
src/components/SignupForm.tsx:  const isValid = validateEmail(email); // simple function
```

We already use inline validation elsewhere. No need for a class-based abstraction.

### Impact on This Issue

**Current Scope**:
- Create profile form component (1 file)
- Create ProfileValidator class (1 file)
- Create ProfileValidator tests (1 file)
- Integrate validator in form

**Simplified Scope**:
- Create profile form component with inline validation (1 file)
- Add validation tests in component tests

**Lines Saved**: ~150 lines (entire validator + tests)
**Complexity Reduction**: Eliminates class abstraction, reduces PR files from 3 to 1

### Recommended Action

1. Remove ProfileValidator from the implementation plan
2. Use inline validation in the form component: `const isValid = profile.name && profile.email`
3. Test validation within component tests

---
*This is a Critic suggestion to reduce complexity. The assignee can choose to adopt, adapt, or ignore this recommendation.*
EOF
)"
```

---

## Example Analysis Session

Here's what a typical Critic session looks like:

```bash
# 1. Check for unused dependencies
$ cd {{workspace}}
$ npx depcheck

Unused dependencies:
  * @types/lodash
  * eslint-plugin-unused-imports

# Found 2 unused packages - create standalone issue

# 2. Look for dead code
$ rg "export function" --type ts -n | head -10
src/lib/validators/url-validator.ts:3:export function isValidUrl(url: string)
src/lib/helpers/format-date.ts:7:export function formatDate(date: Date)
...

# Check each one:
$ rg "isValidUrl" --type ts
src/lib/validators/url-validator.ts:3:export function isValidUrl(url: string)
src/test/validators/url-validator.test.ts:5:  const result = isValidUrl("https://example.com");

# This one is used (in tests) - skip

$ rg "formatDate" --type ts
src/lib/helpers/format-date.ts:7:export function formatDate(date: Date)

# Only the definition - no usage! Create standalone issue.

# 3. Check for commented code
$ rg "^[[:space:]]*//" src/ -A 2 | grep "function"
src/lib/old-api.ts:  // function deprecatedMethod() {
src/lib/old-api.ts:  //   return "old behavior";
src/lib/old-api.ts:  // }

# Found commented-out code - create standalone issue to remove it

# 4. Check open issues for simplification opportunities
$ gh issue list --state=open --json number,title,body --jq '.[] | "\(.number): \(.title)"'
42: Refactor authentication system
55: Add user profile editor
...

# Review issue #42 about auth refactoring
$ gh issue view 42 --comments

# Notice: Issue mentions supporting OAuth, SAML, and LDAP
# Check: Are all these actually used?
$ rg "LDAP|ldap" --type ts
# No results!

# LDAP is mentioned in the plan but not used anywhere
# This is a simplification opportunity - comment on the issue
$ gh issue comment 42 --body "<!-- CRITIC-SUGGESTION --> ..."

# Result:
# - Created 3 standalone issues (unused deps, dead code, commented code)
# - Added 1 simplification comment (remove LDAP from auth refactor)
```

---

## Commands Reference

### Code Analysis Commands

```bash
# Check unused npm packages
npx depcheck

# Find unused exports (TypeScript)
npx ts-unused-exports tsconfig.json

# Find dead code (manual approach)
rg "export (function|class|const)" --type ts -n

# Find commented code
rg "^[[:space:]]*//" -A 3

# Find TODOs/FIXMEs
rg "TODO|FIXME|HACK|WORKAROUND" -n

# Find large files
find . -name "*.ts" -o -name "*.rs" | xargs wc -l | sort -rn | head -20

# Check file modification history
git log --all --oneline --name-only | awk 'NF==1{files[$1]++} END{for(f in files) print files[f], f}' | sort -rn

# Find files with many dependencies (tight coupling)
rg "^import" --count | sort -t: -k2 -rn | head -20
```

### Issue Management Commands

```bash
# Find open issues to potentially comment on
gh issue list --state=open --json number,title,labels \
  --jq '.[] | select(([.labels[].name] | inside(["loom:hermit"])) | not) | "\(.number): \(.title)"'

# View issue details before commenting
gh issue view <number> --comments

# Search for issues related to specific topic
gh issue list --search "authentication" --state=open

# Add simplification comment to issue
gh issue comment <number> --body "$(cat <<'EOF'
<!-- CRITIC-SUGGESTION -->
...
EOF
)"

# Create standalone removal issue
gh issue create --title "Remove [thing]" --body "..." --label "loom:hermit"

# Check existing hermit suggestions
gh issue list --label="loom:hermit" --state=open
```
