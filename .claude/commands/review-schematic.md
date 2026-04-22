# Review Schematic

You are a senior electrical engineer performing a structured design review of a KiCad schematic project. Your goal is to identify electrical errors, missing connections, design-rule violations, and areas for improvement -- organized by severity and attributed to the specific sheet where each finding occurs.

**Schematic path**: `$ARGUMENTS`

If no path was provided, ask the user for the path to the top-level `.kicad_sch` file before proceeding.

---

## Phase 1: Pre-checks (run in this agent's context)

Run the following commands to gather project-wide context. Use `kicad-tools` as a subprocess (not as a Python import). All commands that support `--format json` should use it for reliable parsing.

### 1.1 Validate the path

```bash
test -f "$ARGUMENTS" && echo "OK" || echo "NOT_FOUND"
```

If the file does not exist, report a clear error and stop.

### 1.2 Project summary

```bash
kicad-tools sch summary "$ARGUMENTS" --format json
```

Parse the JSON output to understand:
- Total component count and breakdown by type
- Number of sheets and hierarchy depth
- BOM summary (unique parts, total parts)

### 1.3 Automated validation checks

```bash
kicad-tools sch validate "$ARGUMENTS" --format json
```

This runs combined ERC, unconnected-pin, footprint, and hierarchy checks. Capture and categorize all issues by severity.

### 1.4 Pre-layout preflight checks

```bash
kicad-tools sch preflight "$ARGUMENTS" --format json
```

This checks footprint resolution, net completeness, and power flag coverage.

### 1.5 Enumerate sheets

```bash
kicad-tools sch hierarchy "$ARGUMENTS" list --format json
```

Parse the JSON array to get every sheet's `name`, `path` (hierarchy path), `file` (filesystem path), and `hierarchical_labels`.

### 1.6 Cross-sheet label inventory

```bash
kicad-tools sch labels "$ARGUMENTS" --type global --format json
```

Collect all global labels. These will be checked for driver/receiver consistency in Phase 3.

### 1.7 Hierarchical label connections

```bash
kicad-tools sch hierarchy "$ARGUMENTS" labels --format json
```

Capture the signal-level match information (pins vs. labels) for cross-sheet analysis.

---

## Phase 2: Per-sheet subagent reviews

For each sheet discovered in Phase 1, launch a subagent using the **Task** tool. Each subagent performs a focused review of one schematic sheet.

**Important design decisions:**
- For **flat schematics** (only one sheet with no children), skip subagent dispatch and perform the review directly in this agent.
- Limit subagent context by passing only the relevant CLI outputs and the sheet file path -- do NOT paste the entire project summary into every subagent.
- Each subagent should return findings as a structured list with severity, category, and description.

### Subagent prompt template

For each sheet, use the Task tool with a prompt based on this template (fill in the bracketed values):

---

You are reviewing the KiCad schematic sheet "[SHEET_NAME]" located at "[SHEET_FILE_PATH]".

**Your task**: Read the raw `.kicad_sch` file and check the items in the per-sheet checklist below. Return your findings as a structured list.

### KiCad S-expression quick reference

KiCad 9 schematic files use S-expressions. Key structures:

- **Symbols**: `(symbol (lib_id "Library:Part") (at X Y ROT) (mirror ...) (unit N) ...)` with child nodes:
  - `(property "Reference" "U1" ...)` -- reference designator
  - `(property "Value" "LM1117" ...)` -- component value
  - `(property "Footprint" "Package_SO:SOIC-8" ...)` -- assigned footprint
  - `(pin "1" ...)` -- pin connections (with UUID)
- **Labels**: `(label "NET_NAME" (at X Y ROT) ...)` -- local net labels
- **Global labels**: `(global_label "VCC" (at X Y ROT) (shape input) ...)` -- cross-sheet nets
- **Hierarchical labels**: `(hierarchical_label "CLK" (at X Y ROT) (shape input) ...)` -- connect to parent sheet pins
- **Power symbols**: Symbols with `(lib_id "power:GND")` or similar from the `power` library
- **No-connect flags**: `(no_connect (at X Y) ...)` -- intentionally unconnected pins
- **Wires**: `(wire (pts (xy X1 Y1) (xy X2 Y2)) ...)` -- electrical connections
- **Junctions**: `(junction (at X Y) ...)` -- wire crossing connections
- **Power flags**: `(symbol (lib_id "power:PWR_FLAG") ...)` -- required on power nets for ERC

### Per-sheet checklist

Check the following items by reading the `.kicad_sch` file:

1. **Missing values**: Components (R, C, L, D) with empty or generic `Value` property (e.g., Value is "R" instead of "10k").
2. **Missing footprints**: Components with empty `Footprint` property.
3. **Bypass capacitors**: For each IC (reference starting with U), check whether there are decoupling capacitors nearby. Look for capacitors (reference starting with C) that share a power net with the IC. Note: you cannot reliably compute exact pin positions from the schematic alone -- focus on whether bypass caps exist and are connected to the same power nets, not physical proximity.
4. **Power net sourcing**: If the sheet contains power symbols (from the `power` library), check for `PWR_FLAG` symbols on power nets. Missing PWR_FLAG causes ERC warnings.
5. **Unconnected pins**: Look for symbol pins that are not connected to any wire, label, or no-connect flag. Note: precise pin-position math is error-prone. Flag only obvious cases (e.g., symbols with very few wire connections relative to their pin count). Prefer deferring to the automated `kicad-tools sch unconnected` output when available.
6. **Dangling wires**: Wires that end without connecting to a symbol pin, label, or junction.
7. **Reference designator gaps**: Look for unusual gaps in reference designators (e.g., R1, R2, R5 -- missing R3, R4) within this sheet.
8. **Component values sanity**: Check for unusual values (e.g., a 1-ohm resistor in a signal path, or a 1uF capacitor used as a timing component where pF is expected).
9. **Label naming conventions**: Check for inconsistent naming (e.g., mixing `VCC` and `Vcc`, or `GND` and `gnd`).
10. **DNP (Do Not Place) components**: Note any components marked as DNP for awareness.

### Output format

Return your findings as a list in this exact format:

```
FINDINGS FOR: [SHEET_NAME]
---
- [CRITICAL] <category>: <description>
- [WARNING] <category>: <description>
- [INFO] <category>: <description>
---
SUMMARY: <N> critical, <N> warning, <N> info
```

Severity levels:
- **CRITICAL**: Missing connections, wrong power nets, shorted signals -- things that will cause the board to not work.
- **WARNING**: Missing decoupling caps, unassigned footprints, missing values -- things that should be fixed before layout.
- **INFO**: Style suggestions, naming inconsistencies, DNP notes -- nice to fix but not blocking.

If the sheet has no issues, return:

```
FINDINGS FOR: [SHEET_NAME]
---
No issues found.
---
SUMMARY: 0 critical, 0 warning, 0 info
```

---

## Phase 3: Cross-sheet analysis (back in main agent)

After all subagent reviews complete, perform these cross-sheet checks using the data gathered in Phase 1:

### 3.1 Global label consistency

Using the global label inventory from step 1.6:
- Every global label must appear at least twice across the project (one driver, one or more receivers). A global label that appears only once is likely an error (orphaned net).
- Check for near-duplicate global labels that may indicate typos (e.g., `MOSI` vs `M0SI`, `UART_TX` vs `UART_Tx`).

### 3.2 Hierarchical label matching

Using the hierarchy label connection data from step 1.7:
- Every sheet pin in a parent must have a corresponding hierarchical label in the child sheet, and vice versa.
- Flag any mismatches (pin without label, or label without pin).

### 3.3 Power net verification

- Confirm that every power net (VCC, VDD, 3V3, 5V, GND, etc.) has at least one PWR_FLAG symbol somewhere in the project.
- Check for multiple voltage sources on the same net (possible short).

### 3.4 Orphaned nets

- Nets that connect to only one pin across the entire project (not counting power nets and intentional test points) are likely errors.

---

## Phase 4: Consolidated report

Combine all findings from Phases 1-3 into a single report. Organize by severity, then by sheet.

### Report format

Print the report directly to the conversation (do NOT write it to a file).

```
============================================================
SCHEMATIC REVIEW REPORT
============================================================
Project: <filename>
Sheets reviewed: <N>
Date: <today>

------------------------------------------------------------
CRITICAL FINDINGS (<N> total)
------------------------------------------------------------

[Sheet: <name>]
  1. <category>: <description>
  2. <category>: <description>

[Cross-sheet]
  1. <category>: <description>

------------------------------------------------------------
WARNINGS (<N> total)
------------------------------------------------------------

[Sheet: <name>]
  1. <category>: <description>

------------------------------------------------------------
INFO (<N> total)
------------------------------------------------------------

[Sheet: <name>]
  1. <category>: <description>

============================================================
SUMMARY
============================================================
| Severity | Count |
|----------|-------|
| Critical |   N   |
| Warning  |   N   |
| Info     |   N   |
| TOTAL    |   N   |

Automated checks (validate): <N> errors, <N> warnings
Automated checks (preflight): <N> errors, <N> warnings
============================================================
```

### Additional guidelines

- **Do not repeat** findings already reported by `kicad-tools sch validate` or `sch preflight` unless you are adding additional context or a different perspective. Reference the automated findings in your summary counts but focus your per-sheet analysis on things the automated tools do not catch.
- **Be specific**: Include reference designators, net names, and pin numbers when possible.
- **Be actionable**: Every finding should suggest what to do about it.
- **Avoid false positives**: If you are uncertain whether something is an issue, classify it as INFO rather than WARNING or CRITICAL. Do not guess at pin positions or connection status -- defer to the automated tools for anything requiring geometric computation.
- **Context file**: If the user provided an additional context file (e.g., IC pinout reference, design requirements), incorporate that information into your checks. Look for a second argument after the schematic path.
