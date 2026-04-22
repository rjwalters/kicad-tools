# Repair PCB

You are a senior PCB layout engineer performing automated repairs on a KiCad PCB based on review findings. Your goal is to fix as many issues as possible using the `kicad-tools` CLI, report what you cannot fix, and reflect on tool gaps.

**PCB path**: `$ARGUMENTS`

If no path was provided, ask the user for the path to the `.kicad_pcb` file before proceeding.

This skill expects to be run AFTER `/review-pcb` has produced findings in the conversation context, or after the user has pasted review findings. If no findings are present, run `/review-pcb` first and use its output.

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
kicad-tools check "$ARGUMENTS" --format json --strict
```

```bash
kicad-tools pcb summary "$ARGUMENTS" --format json
```

Save the error/warning counts -- you will compare against these after repairs.

### 1.3 Manufacturer-specific baseline (if applicable)

```bash
kicad-tools check "$ARGUMENTS" --mfr jlcpcb --format json
```

Substitute the manufacturer if known from board context.

### 1.4 Collect review findings

Parse the review findings from the conversation context. For each finding, extract:
- **Layer/Area**: Which layer or board area it applies to
- **Severity**: CRITICAL, WARNING, or INFO
- **Category**: What type of issue (clearance violation, trace width, missing connection, etc.)
- **Details**: Reference designators, net names, coordinates, violation measurements

---

## Phase 2: Triage Findings

Classify each finding into one of four repair categories:

### AUTO-FIX (directly fixable with existing CLI commands)

Map findings to commands:

| Finding Type | Command |
|-------------|---------|
| Clearance violations (trace-to-trace, trace-to-pad) | `kicad-tools repair-clearance <pcb> [--drc-report <report>] [--mfr jlcpcb] [--max-displacement 0.1]` |
| Multiple DRC violation types | `kicad-tools fix-drc <pcb> [--drc-report <report>] [--max-displacement 0.5] [--only clearance\|drill-clearance]` |
| Via-related violations | `kicad-tools fix-vias <pcb>` |
| Silkscreen overlap / violations | `kicad-tools fix-silkscreen <pcb>` |
| Footprint pad spacing violations | `kicad-tools fix-footprints <pcb> --min-pad-gap 0.15` |
| Remove all traces (for re-routing) | `kicad-tools pcb strip <pcb> [--nets <names>]` |
| Batch reference designator rename | `kicad-tools pcb reannotate <pcb> --map <json_file>` |

### GUIDED-FIX (fixable by reading the PCB file and computing parameters)

These require reading the `.kicad_pcb` file to determine exact parameters:

| Finding Type | Approach |
|-------------|----------|
| Clearance violations needing larger displacement | Run `repair-clearance` with increased `--max-displacement` after reading affected area |
| Multi-pass DRC repair | Run `fix-drc` with `--max-passes` > 1 and `--local-reroute` after analyzing failure patterns |
| Net-specific trace stripping | Use `pcb strip --nets <list>` after identifying which nets need re-routing |
| Manufacturer-specific clearance repair | Use `repair-clearance --mfr <mfr> --margin 0.01` with exact manufacturer minimums |

### MANUAL (no tool exists -- requires gap issue)

| Finding Type | Missing Capability |
|-------------|-------------------|
| Add a new trace between two pads | No `pcb add-trace` command |
| Move a footprint to a new position | No `pcb move-footprint` command |
| Change a trace to a different layer | No `pcb change-layer` command |
| Add a via at a specific location | No `pcb add-via` command |
| Modify zone / copper pour settings | No `pcb edit-zone` command |
| Add thermal vias under a component | No `pcb add-thermal-vias` command |
| Adjust silkscreen position | No `pcb move-silkscreen` command |
| Modify board outline | No `pcb edit-outline` command |
| Add teardrops to trace-pad junctions | No `pcb add-teardrops` command |

### DESIGN-DECISION (requires human judgment)

These findings involve layout choices that the engineer must decide:
- "Should we move to a 4-layer stackup for better signal integrity?"
- "Should we add ground stitching vias along the board edges?"
- "Should we re-route this differential pair with length matching?"
- "Should we add a ground plane cutout under the antenna?"
- "Should we change component placement to reduce trace length?"

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
2. **Output to new file first**: Use `-o <output>` to write to a separate file, then compare before replacing the original. Or use `--dry-run` to preview changes.
3. **One command at a time**: Do not batch unrelated fixes. Apply each fix, verify it worked, then proceed.
4. **Stop on unexpected errors**: If a command fails unexpectedly, do NOT retry blindly. Record the failure for the reflection phase and move on.
5. **Verify connectivity**: After any trace modification, re-run DRC to check that no new violations or disconnections were introduced.

### 3.1 AUTO-FIX execution

For each AUTO-FIX item, execute in this order (least to most impactful):

1. **fix-silkscreen** first (cosmetic, very low risk)
2. **fix-footprints** (pad spacing, low risk)
3. **fix-vias** (via fixes, low risk)
4. **repair-clearance** (nudges traces, medium risk -- verify connectivity afterward)
5. **fix-drc** (orchestrated repair, higher risk -- uses connectivity rollback)
6. **pcb strip** (removes traces -- only when full re-route is needed, high risk)

### 3.2 GUIDED-FIX execution (subagents for complex repairs)

For complex repair scenarios, launch a **Task** subagent with this prompt template:

---

You are repairing the KiCad PCB at "[PCB_FILE_PATH]".

**Your task**: Apply the following GUIDED-FIX repairs. For each repair, you must:
1. Read the `.kicad_pcb` file to understand the affected area and determine parameters
2. Run the command with `--dry-run` first and show the output
3. If dry-run looks correct, run the command without `--dry-run`
4. Verify the change by re-running `kicad-tools check` on the file

**Repairs to apply**:
[LIST OF GUIDED-FIX FINDINGS]

**Tool reference**:

Clearance repair:
```bash
kicad-tools repair-clearance <pcb> \
  [--drc-report <report.json>] \
  [--mfr jlcpcb|pcbway|oshpark|seeed] \
  [--max-displacement 0.1] \
  [--margin 0.01] \
  [--prefer move-trace|move-via] \
  [-o <output.kicad_pcb>] \
  [--dry-run] \
  [--format text|json|summary]
```

Orchestrated DRC repair:
```bash
kicad-tools fix-drc <pcb> \
  [--drc-report <report.json>] \
  [--max-displacement 0.5] \
  [--margin 0.01] \
  [--only clearance|drill-clearance] \
  [--max-passes 1] \
  [--local-reroute|--no-local-reroute] \
  [--no-connectivity-check] \
  [-o <output.kicad_pcb>] \
  [--dry-run]
```

Net-specific strip:
```bash
kicad-tools pcb strip <pcb> \
  [--nets <net1> <net2> ...] \
  [--no-keep-zones] \
  [--output <output.kicad_pcb>] \
  [--dry-run]
```

Key rules:
- Always `--dry-run` before applying
- Use `-o <output>` to write to a new file, then verify before replacing original
- After each repair, re-run `kicad-tools check` to verify no new violations
- `repair-clearance` nudges traces by small amounts -- if `--max-displacement` is too small, increase gradually
- `fix-drc` with `--local-reroute` can reroute short trace segments to fix violations
- If connectivity is broken after a repair, the `fix-drc` tool automatically rolls back

**Output format**:
For each repair, report:
```
REPAIR: <description>
DRY-RUN: <command output>
APPLIED: <success/failure + details>
VERIFY: <post-repair DRC result>
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

1. [Layer/Area: <name>] <finding description>
   Options: <A or B>
   Recommendation: <your recommendation>

2. ...
============================================================
```

---

## Phase 4: Verification

After all repairs complete:

### 4.1 Re-run DRC

```bash
kicad-tools check "$ARGUMENTS" --format json --strict
```

### 4.2 Manufacturer-specific re-check (if applicable)

```bash
kicad-tools check "$ARGUMENTS" --mfr jlcpcb --format json
```

### 4.3 Compare before/after

Print a comparison table:

```
============================================================
REPAIR RESULTS
============================================================
| Check | Before | After | Delta |
|-------|--------|-------|-------|
| DRC errors | N | N | -N |
| DRC warnings | N | N | -N |
| Mfr DRC errors | N | N | -N |
| Mfr DRC warnings | N | N | -N |

Repairs applied: N of M findings
  AUTO-FIX: N applied, N failed
  GUIDED-FIX: N applied, N failed
  MANUAL: N skipped (tool gaps)
  DESIGN-DECISION: N skipped (user input needed)
============================================================
```

### 4.4 List remaining issues

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

PCB repair of <project name> using `/repair-pcb`

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

If no tool issues were observed, print:

```
============================================================
TOOL REFLECTION
============================================================
No tool issues observed during this review session.
============================================================
```

---

## Additional Guidelines

- **Be conservative**: When in doubt about a fix, skip it and report it as MANUAL rather than risking corruption.
- **Preserve originals**: Use `-o <output>` to write to a new file when possible, so the original is preserved.
- **No speculative fixes**: Only fix findings that were explicitly identified in the review. Do not "improve" things beyond what was flagged.
- **Coordinate with user on DESIGN-DECISION items**: Present options and wait for direction rather than making layout choices.
- **Track every command**: Keep a running log of all commands executed and their results. This feeds the reflection phase.
- **Connectivity is sacred**: After any trace modification, always verify that no nets were broken. The `fix-drc` tool has built-in connectivity rollback, but `repair-clearance` does not -- verify manually.
