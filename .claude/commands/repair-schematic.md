# Repair Schematic

You are a senior electrical engineer performing automated repairs on a KiCad schematic based on review findings. Your goal is to fix as many issues as possible using the `kicad-tools` CLI, report what you cannot fix, and reflect on tool gaps.

**Schematic path**: `$ARGUMENTS`

If no path was provided, ask the user for the path to the top-level `.kicad_sch` file before proceeding.

This skill expects to be run AFTER `/review-schematic` has produced findings in the conversation context, or after the user has pasted review findings. If no findings are present, run `/review-schematic` first and use its output.

---

## Phase 1: Gather Context

### 1.1 Validate the path

```bash
test -f "$ARGUMENTS" && echo "OK" || echo "NOT_FOUND"
```

If the file does not exist, report a clear error and stop.

### 1.2 Capture baseline state

Run these commands to establish the pre-repair baseline:

```bash
kicad-tools sch validate "$ARGUMENTS" --format json
```

```bash
kicad-tools sch preflight "$ARGUMENTS" --format json
```

Save the error/warning counts from both commands -- you will compare against these after repairs.

### 1.3 Enumerate sheets

```bash
kicad-tools sch hierarchy "$ARGUMENTS" list --format json
```

### 1.4 Collect review findings

Parse the review findings from the conversation context. For each finding, extract:
- **Sheet**: Which `.kicad_sch` file it applies to
- **Severity**: CRITICAL, WARNING, or INFO
- **Category**: What type of issue (missing value, wrong lib_id, dangling wire, etc.)
- **Details**: Component references, net names, pin numbers, coordinates mentioned

---

## Phase 2: Triage Findings

Classify each finding into one of four repair categories:

### AUTO-FIX (directly fixable with existing CLI commands)

Map findings to commands:

| Finding Type | Command |
|-------------|---------|
| Wrong lib_id / symbol mismatch | `kicad-tools sch replace <sch> <ref> <new_lib_id> [--value <val>] [--footprint <fp>]` |
| Missing or wrong component value | `kicad-tools sch set-value <sch> --ref <ref> --value <val>` |
| Missing or wrong footprint | `kicad-tools sch set-footprint <sch> --ref <ref> --footprint <fp>` |
| Duplicate or dangling wires | `kicad-tools sch cleanup-wires <sch>` |
| Missing no-connect markers | `kicad-tools sch add-no-connect <sch> --auto` |
| Signal naming inconsistency | `kicad-tools sch rename-signal <sch> --from <old> --to <new> --yes` |
| Hierarchy pin/label mismatch | `kicad-tools sch sync-hierarchy <sch> --add-labels` |

### GUIDED-FIX (fixable with `add-component` + coordinate math from reading the schematic)

These require reading the `.kicad_sch` file to compute placement coordinates:

| Finding Type | Command Pattern |
|-------------|----------------|
| Missing bypass/decoupling cap | `kicad-tools sch add-component <sch> --lib-id Device:C --reference <ref> --value <val> --footprint <fp> --at <X> <Y> --connect <pin:x,y>` |
| Missing PWR_FLAG | `kicad-tools sch add-component <sch> --lib-id power:PWR_FLAG --at <X> <Y> --connect 1:<x>,<y>` |
| Missing pull-down/pull-up resistor | `kicad-tools sch add-component <sch> --lib-id Device:R --reference <ref> --value <val> --footprint <fp> --at <X> <Y> --connect <pin:x,y>` |
| Missing capacitor (e.g., envelope hold) | Same as bypass cap pattern |

### MANUAL (no tool exists -- requires gap issue)

| Finding Type | Missing Capability |
|-------------|-------------------|
| Add a new global label to a net | No `sch add-label` command |
| Add a wire between two arbitrary points | No `sch add-wire` command (only via add-component) |
| Change label direction (input/output/bidirectional) | No `sch set-label-direction` command |
| Move a component from one net to another | No `sch move-component` or combined disconnect+reconnect |
| Insert a component into an existing signal path | Requires `sch break-wire` + `sch add-component` (break-wire doesn't exist) |
| Remove a component | `modify_schematic.py --delete` exists but is not exposed as CLI command |

### DESIGN-DECISION (requires human judgment)

These findings involve architectural choices that the engineer must decide:
- "Should we use full-wave rectifier instead of half-wave?"
- "Should we add input protection (fuse, TVS, Schottky)?"
- "Should we expand J4 to a full SPI connector?"
- "Should we add a proper SWD debug header?"

Skip these with a note for the user.

### Triage output

Print a summary table before proceeding:

```
============================================================
REPAIR TRIAGE
============================================================
| Category | Count | Findings |
|----------|-------|----------|
| AUTO-FIX | N | <brief list> |
| GUIDED-FIX | N | <brief list> |
| MANUAL | N | <brief list> |
| DESIGN-DECISION | N | <brief list> |

Proceeding with AUTO-FIX and GUIDED-FIX items...
============================================================
```

---

## Phase 3: Execute Repairs

### Execution rules

1. **Always dry-run first**: Run every command with `--dry-run` before applying. Show the dry-run output.
2. **Backup on first change**: Use `--backup` on the first modification to each file. For `set-value` and `set-footprint` (which backup by default), this happens automatically.
3. **One command at a time**: Do not batch unrelated fixes. Apply each fix, verify it worked, then proceed.
4. **Stop on unexpected errors**: If a command fails unexpectedly, do NOT retry blindly. Record the failure for the reflection phase and move on.

### 3.1 AUTO-FIX execution

For each AUTO-FIX item, execute in this order (least to most impactful):

1. **cleanup-wires** first (removes noise from the schematic)
2. **set-value** / **set-footprint** (property updates, low risk)
3. **replace** (symbol swap, medium risk -- verify pin connections afterward)
4. **add-no-connect** (adds markers, low risk)
5. **rename-signal** (renames across hierarchy, verify with `--dry-run`)
6. **sync-hierarchy** (structural change, verify carefully)

### 3.2 GUIDED-FIX execution (per-sheet subagents)

For each sheet that has GUIDED-FIX findings, launch a **Task** subagent with this prompt template:

---

You are repairing the KiCad schematic sheet "[SHEET_NAME]" located at "[SHEET_FILE_PATH]".

**Your task**: Apply the following GUIDED-FIX repairs. For each repair, you must:
1. Read the `.kicad_sch` file to find exact coordinates for placement
2. Run the command with `--dry-run` first and show the output
3. If dry-run looks correct, run the command without `--dry-run`
4. Verify the change was applied by running `kicad-tools sch validate` on the file

**Repairs to apply**:
[LIST OF GUIDED-FIX FINDINGS FOR THIS SHEET]

**Tool reference**:

Adding a component:
```bash
kicad-tools sch add-component <schematic> \
  --lib-id <Library:Part> \
  --reference <REF> \
  --value <VALUE> \
  --footprint <FOOTPRINT> \
  --at <X> <Y> \
  [--rotation <DEG>] \
  [--connect <PIN:X,Y>] \
  [--backup] \
  [--dry-run]
```

Key rules for coordinate calculation:
- KiCad schematics use a 1.27mm grid. All coordinates will be snapped to this grid.
- To place a component near an IC, find the IC's position from its `(at X Y)` field, then offset by ~10-15mm in an unoccupied direction.
- To connect a power flag, find the wire or junction on the power net and place the PWR_FLAG at that coordinate.
- Wire connections use `--connect PIN:X,Y` where X,Y is the coordinate of the target wire endpoint or junction.
- When adding a decoupling cap near an IC:
  1. Find the IC's power pin position (VCC/VDD pin)
  2. Find the IC's ground pin position (GND/VSS pin)
  3. Place the capacitor between them, offset to the side
  4. Connect pin 1 to the power net coordinate and pin 2 to the ground net coordinate

**Output format**:
For each repair, report:
```
REPAIR: <description>
DRY-RUN: <command output>
APPLIED: <success/failure + details>
```

If a repair fails, report the error and move on. Do NOT retry with different parameters.

---

### 3.3 MANUAL items

For each MANUAL finding, record it for the reflection phase. Do not attempt to fix these.

### 3.4 DESIGN-DECISION items

Print these for the user with context:

```
============================================================
DESIGN DECISIONS (requires your input)
============================================================

1. [Sheet: <name>] <finding description>
   Options: <A or B>
   Recommendation: <your recommendation>

2. ...
============================================================
```

---

## Phase 4: Verification

After all repairs complete:

### 4.1 Re-run validation

```bash
kicad-tools sch validate "$ARGUMENTS" --format json
```

```bash
kicad-tools sch preflight "$ARGUMENTS" --format json
```

### 4.2 Compare before/after

Print a comparison table:

```
============================================================
REPAIR RESULTS
============================================================
| Check | Before | After | Delta |
|-------|--------|-------|-------|
| Validate errors | N | N | -N |
| Validate warnings | N | N | -N |
| Preflight errors | N | N | -N |
| Preflight warnings | N | N | -N |

Repairs applied: N of M findings
  AUTO-FIX: N applied, N failed
  GUIDED-FIX: N applied, N failed
  MANUAL: N skipped (tool gaps)
  DESIGN-DECISION: N skipped (user input needed)
============================================================
```

### 4.3 List remaining issues

Print any findings that were NOT fixed, grouped by reason:
- Failed to apply
- No tool available (MANUAL)
- Requires user decision (DESIGN-DECISION)

---

## Phase 5: Reflection

After all repairs and verification, reflect on the tools used during this session.

### 5.1 Collect tool observations

Review every `kicad-tools` command you ran (or attempted to run) during this session. For each, note:

- Did it succeed or fail?
- Was the output correct and useful?
- Did you need a tool that doesn't exist?
- Did a tool produce a false positive or miss something obvious?

### 5.2 Categorize observations

Classify each observation:

- **TOOL_FAILURE**: A command errored, crashed, or produced clearly wrong output
- **CAPABILITY_GAP**: You needed a tool or feature that doesn't exist in `kicad-tools`
- **FALSE_POSITIVE**: A tool flagged something that is not actually an issue
- **IMPROVEMENT**: A tool works but its output could be better, or it's missing a useful option

### 5.3 File gap issues

For each **TOOL_FAILURE** or **CAPABILITY_GAP**, file a GitHub issue using the project's loom pattern:

1. First check for duplicates:
```bash
./.loom/scripts/check-duplicate.sh "<issue title>" "<issue body summary>"
```

2. If no duplicate found (exit code 0), create the issue:
```bash
gh issue create --title "<title>" --body "$(cat <<'EOF'
## Problem Statement

<What was attempted and what failed or was missing>

## Observed During

Schematic repair of <project name> using `/repair-schematic`

## Impact

<How this affects repair automation quality -- e.g., "N findings could not be auto-repaired">

## Recommended Enhancement

<Specific tool improvement or new command needed>

## Acceptance Criteria

- [ ] <What "done" looks like>
EOF
)"
```

3. Add labels:
```bash
gh issue edit <number> --add-label "loom:architect"
gh issue edit <number> --add-label "tier:goal-supporting"
```

For **FALSE_POSITIVE** and **IMPROVEMENT** observations, note them in the output but do not file issues unless the impact is significant.

### 5.4 Print reflection summary

```
============================================================
TOOL REFLECTION
============================================================
| Category | Count | Issues Filed |
|----------|-------|-------------|
| Tool failures | N | #nnn, #nnn |
| Capability gaps | N | #nnn, #nnn |
| False positives | N | (noted) |
| Improvements | N | (noted) |

Details:
  [CATEGORY] <tool or feature>: <what happened>
    -> Filed #nnn / Noted

============================================================
```

---

## Additional Guidelines

- **Be conservative**: When in doubt about a fix, skip it and report it as MANUAL rather than risking corruption.
- **Preserve backups**: The first modification to any file should create a backup. If the user needs to revert, they can use the `.bak` or timestamped backup file.
- **No speculative fixes**: Only fix findings that were explicitly identified in the review. Do not "improve" things beyond what was flagged.
- **Coordinate with user on DESIGN-DECISION items**: Present options and wait for direction rather than making architectural choices.
- **Track every command**: Keep a running log of all commands executed and their results. This feeds the reflection phase.
