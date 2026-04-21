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

### 6. Parallel Drift

When two or more classes/modules solve the same domain problem with different names and APIs, only one is used at runtime. The other is dead weight referenced only by its own tests.

```python
# BAD: Two session managers solving the same problem
# file: src/sessions/session_manager.py
class SessionManager:
    def create_session(self, user_id: str) -> Session:
        return Session(user_id=user_id, token=generate_token())
    def destroy_session(self, session_id: str) -> None:
        self._store.delete(session_id)

# file: src/core/session_handler.py
class SessionHandler:
    def new_session(self, uid: str) -> dict:
        return {"uid": uid, "tok": make_token(), "ts": time.time()}
    def end_session(self, sid: str) -> bool:
        return self._db.remove(sid)

# GOOD: Single canonical implementation
# file: src/sessions/session_manager.py
class SessionManager:
    def create_session(self, user_id: str) -> Session:
        return Session(user_id=user_id, token=generate_token())
    def destroy_session(self, session_id: str) -> None:
        self._store.delete(session_id)
```

```typescript
// BAD: Parallel drift in TypeScript
// file: src/utils/config-loader.ts
export class ConfigLoader {
  load(path: string): Config { /* ... */ }
}
// file: src/lib/settings-reader.ts
export class SettingsReader {
  read(filepath: string): Settings { /* ... */ }
}

// GOOD: One config module
// file: src/config/loader.ts
export class ConfigLoader {
  load(path: string): Config { /* ... */ }
}
```

**Detection scripts:**

```bash
# Python: Find classes with semantically similar names
rg "^class \w*(Session|Manager|Handler|Builder|Parser|Validator|Config|Store|Cache|Client)\w*" --type py -n | \
  sed 's/.*class \([A-Za-z]*\).*/\1/' | sort | uniq -d

# TypeScript: Same check
rg "^(export )?(class|interface) \w*(Session|Manager|Handler|Builder|Parser|Validator|Config|Store|Cache|Client)\w*" --type ts -n | \
  sed 's/.*\(class\|interface\) \([A-Za-z]*\).*/\2/' | sort | uniq -d

# Rust: Find structs with similar domain names
rg "^pub struct \w*(Session|Manager|Handler|Builder|Parser|Validator|Config|Store|Cache|Client)\w*" --type rust -n | \
  sed 's/.*struct \([A-Za-z]*\).*/\1/' | sort | uniq -d

# After finding candidates, verify: is one only referenced by its own tests?
# For each candidate file:
rg "SessionHandler" --type py --files-with-matches | grep -v test
# If only test files reference it, it's likely parallel drift
```

### 7. Stub Theater

Methods with rich interfaces (docstrings, type signatures, multiple parameters) that return hardcoded literal values. The external contract promises complex behavior, but the implementation is inert.

```python
# BAD: Rich interface, stub implementation
class ChangeDetector:
    def _is_component_modified(self, component: Component, baseline: Snapshot) -> bool:
        """Check if a component has been modified since the baseline snapshot.

        Compares component hash, metadata, and dependency graph against
        the baseline to detect meaningful changes. Ignores whitespace-only
        and comment-only changes.

        Args:
            component: The component to check for modifications.
            baseline: The reference snapshot to compare against.

        Returns:
            True if the component has meaningful modifications.
        """
        return False  # <-- entire subsystem is inert

    def _can_preserve(self, component: Component) -> bool:
        """Determine if a component can be safely preserved during rebuild.

        Validates three criteria:
        1. Component has no circular dependencies
        2. Component's interface hasn't changed
        3. All downstream consumers are compatible

        Returns:
            True if safe to preserve, False if rebuild required.
        """
        return True  # <-- validation never actually runs

# GOOD (preferred): Finish the feature — check if dependencies now exist
class ChangeDetector:
    def _is_component_modified(self, component: Component, baseline: Snapshot) -> bool:
        current_hash = component.content_hash()
        baseline_hash = baseline.get_hash(component.id)
        return current_hash != baseline_hash

# GOOD (alternative): Remove if the feature is clearly abandoned
```

```typescript
// BAD: Stub theater in TypeScript
class PermissionChecker {
  /**
   * Validates user has required permissions for the operation.
   * Checks role hierarchy, resource ownership, and temporal constraints.
   */
  canAccess(user: User, resource: Resource, operation: Operation): boolean {
    return true; // Everyone can access everything
  }
}

// GOOD (preferred): Finish the feature
function canAccess(user: User, resource: Resource, operation: Operation): boolean {
  return user.roles.some(role =>
    resource.allowedRoles.includes(role) && role.permits(operation)
  );
}

// GOOD (alternative): Remove if clearly abandoned
```

**Detection scripts:**

```bash
# Python: Find methods with docstrings that just return a literal
rg "def \w+\(self" -A 8 --type py |   awk '/def /{found=1; block=""} found{block=block"
"$0} /return (False|True|None|""|\[\]|\{\}|0)$/{if(found) print block; found=0}'

# Simpler heuristic: methods whose only non-docstring line is a return literal
rg "def \w+\(self" -A 5 --type py | grep -B 1 "return (False|True|None)$"

# TypeScript: methods returning hardcoded values
rg "(public|private|protected)?\s+\w+\(.*\).*\{" -A 3 --type ts |   grep -B 1 "return (false|true|null|\[\]|\{\}|0|"")"

# Rust: functions returning hardcoded values (less common but possible)
rg "fn \w+\(" -A 5 --type rust | grep -B 2 "^\s*(false|true|None|0|"")"
```

### 8. Framework Scaffolding

Validation, processing, or transformation pipelines that are structurally complete but operate on empty data. A builder/context/factory method returns empty collections while downstream consumers treat the result as meaningful.

```python
# BAD: Pipeline processes nothing
class PCBValidator:
    def validate(self, design_file: str) -> ValidationResult:
        """Run full validation pipeline on PCB design."""
        context = self._build_context(design_file)
        errors = self._check_design_rules(context)
        warnings = self._check_manufacturing_constraints(context)
        return ValidationResult(errors=errors, warnings=warnings)

    def _build_context(self, design_file: str) -> dict:
        # TODO: Implement actual PCB parsing
        return {
            "layers": {},        # empty
            "components": [],    # empty
            "nets": [],          # empty
            "rules": {}          # empty
        }
        # _check_design_rules and _check_manufacturing_constraints
        # iterate over empty collections -- validation always passes

# GOOD: Either implement or mark as not-yet-functional
class PCBValidator:
    def validate(self, design_file: str) -> ValidationResult:
        raise NotImplementedError("PCB validation not yet implemented")
```

```typescript
// BAD: Framework scaffolding in TypeScript
class DataPipeline {
  async process(input: RawData): Promise<ProcessedData> {
    const enriched = await this.enrich(input);
    const validated = this.validate(enriched);
    const transformed = this.transform(validated);
    return transformed;
  }

  private async enrich(data: RawData): Promise<EnrichedData> {
    // TODO: Connect to enrichment service
    return { ...data, metadata: {}, annotations: [] };
  }
}

// GOOD: Be explicit about what's implemented
class DataPipeline {
  async process(input: RawData): Promise<ProcessedData> {
    // Enrichment not yet implemented - pass through
    const validated = this.validate(input);
    return this.transform(validated);
  }
}
```

**Detection scripts:**

```bash
# Python: Find methods returning empty collections with TODOs nearby
rg "return \{\}" -B 5 --type py | grep -B 4 "TODO\|FIXME\|NotImplemented"
rg "return \[\]" -B 5 --type py | grep -B 4 "TODO\|FIXME\|NotImplemented"

# Find factory/builder methods returning empty dicts/lists
rg "(def (build|create|make|get|load)_\w+)" -A 10 --type py |   grep -B 5 "return \(\{\}\|\[\]\)"

# TypeScript: Same patterns
rg "return \{\}" -B 5 --type ts | grep -B 4 "TODO\|FIXME"
rg "return \[\]" -B 5 --type ts | grep -B 4 "TODO\|FIXME"

# Rust: Empty collections in builder methods
rg "fn (build|create|new|load)_?\w*" -A 10 --type rust |   grep -B 5 "Vec::new()\|HashMap::new()\|BTreeMap::new()"
```

### 9. Stateless Ceremony

Classes where `__init__` is `pass`/empty/missing and no methods assign to `self.*`. These are functions wearing a class costume -- instantiation is ceremony with no purpose.

```python
# BAD: Class with no instance state
class PatternAdapter:
    def __init__(self):
        pass  # No state

    @staticmethod
    def convert_glob_to_regex(pattern: str) -> str:
        return fnmatch.translate(pattern)

    @staticmethod
    def match(text: str, pattern: str) -> bool:
        return fnmatch.fnmatch(text, pattern)

    def adapt(self, patterns: list[str]) -> list[re.Pattern]:
        return [re.compile(self.convert_glob_to_regex(p)) for p in patterns]

# GOOD: Module-level functions
def convert_glob_to_regex(pattern: str) -> str:
    return fnmatch.translate(pattern)

def match(text: str, pattern: str) -> bool:
    return fnmatch.fnmatch(text, pattern)

def adapt_patterns(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(convert_glob_to_regex(p)) for p in patterns]

# NOT a stateless ceremony: Dispatch-table class (uses self for method dispatch)
class CommandRouter:
    """Routes commands to handler methods. Stateless by design --
    the class provides method-dispatch organization, not instance state."""

    def route(self, command: str, args: dict) -> Result:
        handler = {
            "create": self.handle_create,
            "update": self.handle_update,
            "delete": self.handle_delete,
            "list": self.handle_list,
        }.get(command)
        if not handler:
            return Result(error=f"Unknown command: {command}")
        return handler(args)

    def handle_create(self, args: dict) -> Result:
        return Result(data=create_record(args))

    def handle_update(self, args: dict) -> Result:
        return Result(data=update_record(args["id"], args))

    def handle_delete(self, args: dict) -> Result:
        return Result(data=delete_record(args["id"]))

    def handle_list(self, args: dict) -> Result:
        return Result(data=list_records(args.get("filter")))
    # This class has no self.x = assignments but DOES use self.method()
    # for internal dispatch. Converting to module functions would lose
    # the dispatch-table organization. Do NOT flag this.
```

```typescript
// BAD: Stateless class in TypeScript
export class SnapshotBuilder {
  // No constructor, no properties
  build(data: RawData): Snapshot {
    return { timestamp: Date.now(), data: this.normalize(data) };
  }

  private normalize(data: RawData): NormalizedData {
    return Object.fromEntries(
      Object.entries(data).map(([k, v]) => [k.toLowerCase(), v])
    );
  }
}

// GOOD: Export functions directly
export function buildSnapshot(data: RawData): Snapshot {
  return { timestamp: Date.now(), data: normalizeData(data) };
}

function normalizeData(data: RawData): NormalizedData {
  return Object.fromEntries(
    Object.entries(data).map(([k, v]) => [k.toLowerCase(), v])
  );
}
```

**Detection scripts:**

```bash
# Python: Find classes with no instance state (AST-based, excludes dispatch-table classes)
python3 -c "
import ast, sys, os
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'node_modules']
    for f in files:
        if not f.endswith('.py'): continue
        path = os.path.join(root, f)
        try:
            tree = ast.parse(open(path).read())
        except: continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef): continue
            has_self_assign = any(
                isinstance(n, ast.Assign) and
                any(isinstance(t, ast.Attribute) and
                    isinstance(t.value, ast.Name) and t.value.id == 'self'
                    for t in n.targets)
                for n in ast.walk(node)
            )
            if has_self_assign:
                continue  # Has instance state -- not a stateless ceremony
            # Exclusion 1: Internal method dispatch (self.method() calls)
            has_self_method_call = any(
                isinstance(n, ast.Call) and
                isinstance(getattr(n, 'func', None), ast.Attribute) and
                isinstance(getattr(n.func, 'value', None), ast.Name) and
                n.func.value.id == 'self'
                for n in ast.walk(node)
            )
            if has_self_method_call:
                continue  # Uses internal dispatch -- likely a namespace
            # Exclusion 2: Method count threshold (10+ methods = namespace)
            method_count = sum(
                1 for n in ast.walk(node)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            if method_count >= 10:
                continue  # Too many methods to practically convert
            # Exclusion 3: Dispatch-table pattern (self.method refs inside dict/list/set)
            has_dispatch_table = False
            for n in ast.walk(node):
                if not isinstance(n, (ast.Dict, ast.List, ast.Set)):
                    continue
                for val in ast.walk(n):
                    if (isinstance(val, ast.Attribute) and
                        isinstance(getattr(val, 'value', None), ast.Name) and
                        val.value.id == 'self'):
                        has_dispatch_table = True
                        break
                if has_dispatch_table:
                    break
            if has_dispatch_table:
                continue  # Builds dispatch table from self.method references
            print(f'{path}:{node.lineno}: {node.name} (no instance state)')
"

# TypeScript: classes with no 'this.' property assignments
rg "class \w+" --type ts -l | while read file; do
  class_count=$(rg "class \w+" "$file" --count 2>/dev/null || echo 0)
  this_count=$(rg "this\.\w+\s*=" "$file" --count 2>/dev/null || echo 0)
  if [ "$this_count" -eq 0 ] && [ "$class_count" -gt 0 ]; then
    echo "$file: $class_count classes, 0 instance state assignments"
  fi
done

# Rust: structs with no fields (unit structs used as namespaces)
rg "^pub struct \w+;$" --type rust -n
rg "^pub struct \w+ \{\}$" --type rust -n
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
