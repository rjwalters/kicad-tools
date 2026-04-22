# Review PCB

You are a senior PCB layout engineer performing a structured design review of a KiCad PCB. Your goal is to identify manufacturing violations, clearance errors, missing connections, design-rule violations, and areas for improvement -- organized by severity and attributed to specific layers or areas of the board.

**PCB path**: `$ARGUMENTS`

If no path was provided, ask the user for the path to the `.kicad_pcb` file before proceeding.

---

## Phase 1: Pre-checks (run in this agent's context)

Run the following commands to gather board-wide context. Use `kicad-tools` as a subprocess (not as a Python import). All commands that support `--format json` should use it for reliable parsing.

### 1.1 Validate the path

```bash
test -f "$ARGUMENTS" && echo "OK" || echo "NOT_FOUND"
```

If the file does not exist, report a clear error and stop.

### 1.2 Board summary

```bash
kicad-tools pcb summary "$ARGUMENTS" --format json
```

Parse the JSON output to understand:
- Board dimensions and layer count
- Total footprint count and breakdown
- Net count and connectivity overview

### 1.3 Footprint inventory

```bash
kicad-tools pcb footprints "$ARGUMENTS" --format json
```

Get the full list of placed footprints with references, positions, and layers.

### 1.4 Net inventory

```bash
kicad-tools pcb nets "$ARGUMENTS" --format json
```

Collect all nets for connectivity analysis.

### 1.5 Trace statistics

```bash
kicad-tools pcb traces "$ARGUMENTS" --format json
```

Get trace width and via statistics per layer.

### 1.6 Layer stackup

```bash
kicad-tools pcb stackup "$ARGUMENTS" --format json
```

Understand the board layer structure.

### 1.7 DRC check (pure Python -- no kicad-cli required)

```bash
kicad-tools check "$ARGUMENTS" --format json --strict
```

Run the built-in DRC checker covering clearance, dimensions, edge cuts, and silkscreen violations.

### 1.8 Manufacturer-specific DRC (if applicable)

If the board is destined for a specific manufacturer, run the targeted check:

```bash
kicad-tools check "$ARGUMENTS" --mfr jlcpcb --format json
```

Substitute the manufacturer if known from board context (e.g., PCBWay, OSHPark, Seeed).

### 1.9 Footprint validation

```bash
kicad-tools validate-footprints "$ARGUMENTS" --min-pad-gap 0.15
```

Check for footprint pad spacing issues that could cause manufacturing problems.

---

## Phase 2: Per-layer/area subagent reviews

For each copper layer on the board, launch a subagent using the **Task** tool. Each subagent performs a focused review of one layer's routing and connectivity.

**Important design decisions:**
- For **simple 2-layer boards**, you may perform the review directly in this agent instead of launching subagents.
- For **4+ layer boards**, launch subagents per layer to manage context size.
- Limit subagent context by passing only the relevant trace/net data for that layer.
- Each subagent should return findings as a structured list with severity, category, and description.

### Subagent prompt template

For each copper layer, use the Task tool with a prompt based on this template (fill in the bracketed values):

---

You are reviewing the [LAYER_NAME] copper layer of a KiCad PCB located at "[PCB_FILE_PATH]".

**Your task**: Read the raw `.kicad_pcb` file (focusing on traces, vias, and zones on this layer) and check the items in the per-layer checklist below. Return your findings as a structured list.

### KiCad PCB S-expression quick reference

KiCad 9 PCB files use S-expressions. Key structures:

- **Footprints**: `(footprint "Library:Package" (at X Y ROT) (layer "F.Cu") ...)` with child nodes:
  - `(fp_text reference "U1" ...)` -- reference designator
  - `(pad "1" smd rect (at X Y) (size W H) (layers "F.Cu" "F.Paste" "F.Mask") (net N "NET_NAME") ...)`
- **Traces**: `(segment (start X1 Y1) (end X2 Y2) (width W) (layer "F.Cu") (net N) ...)`
- **Vias**: `(via (at X Y) (size S) (drill D) (layers "F.Cu" "B.Cu") (net N) ...)`
- **Zones**: `(zone (net N) (net_name "GND") (layer "F.Cu") (fill_settings ...) ...)` -- copper pours
- **Board outline**: `(gr_line ... (layer "Edge.Cuts") ...)` or `(gr_arc ...)` on Edge.Cuts layer

### Per-layer checklist

Check the following items by reading the `.kicad_pcb` file:

1. **Trace width consistency**: Identify traces that change width unexpectedly within the same net. Power traces (VCC, GND, VBUS) should use wider widths than signal traces.
2. **Clearance violations**: Look for traces or pads that appear to be very close together (< 0.2mm for standard, < 0.1mm for fine-pitch). Note: the automated DRC catches most of these, so focus on patterns the DRC may miss.
3. **Unrouted connections (ratsnest)**: Look for nets with pads on this layer that have no connecting trace or via. These appear as thin lines in the ratsnest.
4. **Via placement**: Check for vias placed under components (may cause assembly issues), vias in pads without proper tenting, or excessive via count where a more direct route exists.
5. **Copper pour / zone coverage**: If ground or power zones exist on this layer, check for isolated islands (copper fragments not connected to the zone net) and thermal relief connections to pads.
6. **Trace routing quality**: Look for acute-angle bends (< 90°), unnecessary trace length (meandering when direct routes are available), and traces running parallel for long distances (crosstalk risk).
7. **Power trace sizing**: Verify power nets (VCC, GND, VBUS, etc.) have adequate trace widths for expected current. Flag any power trace narrower than 0.3mm.
8. **Guard traces / sensitive signals**: For high-speed or analog signals, check if guard traces or proper ground shielding is present.

### Output format

Return your findings as a list in this exact format:

```
FINDINGS FOR: [LAYER_NAME]
---
- [CRITICAL] <category>: <description>
- [WARNING] <category>: <description>
- [INFO] <category>: <description>
---
SUMMARY: <N> critical, <N> warning, <N> info
```

Severity levels:
- **CRITICAL**: Missing connections, clearance violations, shorts -- things that will cause the board to not work.
- **WARNING**: Suboptimal trace widths, via placement issues, missing ground pours -- things that should be fixed before fabrication.
- **INFO**: Style suggestions, routing optimizations, thermal improvements -- nice to fix but not blocking.

If the layer has no issues, return:

```
FINDINGS FOR: [LAYER_NAME]
---
No issues found.
---
SUMMARY: 0 critical, 0 warning, 0 info
```

---

## Phase 3: Cross-layer and board-level analysis (back in main agent)

After all subagent reviews complete, perform these board-wide checks:

### 3.1 Connectivity verification

Using the net inventory from step 1.4 and trace data:
- Every net must have complete connectivity (no ratsnest lines remaining).
- Check for single-pad nets (possible unconnected components).
- Verify all schematic nets are represented in the PCB (no missing footprints).

### 3.2 Manufacturing constraints

- **Minimum trace/space**: Verify all traces and clearances meet the target manufacturer's minimums.
- **Minimum drill size**: Check that all via and pad drills meet manufacturing minimums.
- **Board outline**: Verify the Edge.Cuts layer forms a closed contour with no gaps.
- **Solder mask expansion**: Check for pads where solder mask opening may cause bridges.

### 3.3 Silkscreen review

- Reference designators should not overlap pads or be placed over vias.
- All components should have visible reference designators.
- Silkscreen text should meet minimum size requirements (typically 0.8mm height, 0.15mm width).

### 3.4 Thermal analysis

- Large ground planes should have adequate thermal relief on pads (for solderability).
- High-power components should have proper thermal vias to inner/back copper.
- Check for thermal isolation issues (components surrounded by large copper pours without relief).

### 3.5 Assembly considerations

- Check component placement for hand-solderability or pick-and-place compatibility.
- Verify adequate spacing between tall components and nearby SMD parts.
- Check for components placed too close to board edges (typically < 1mm is problematic).

---

## Phase 4: Consolidated report

Combine all findings from Phases 1-3 into a single report. Organize by severity, then by layer/area.

### Report format

Print the report directly to the conversation (do NOT write it to a file).

```
============================================================
PCB REVIEW REPORT
============================================================
Project: <filename>
Board size: <W x H mm>
Layers: <N>
Footprints: <N>
Nets: <N>
Date: <today>

------------------------------------------------------------
CRITICAL FINDINGS (<N> total)
------------------------------------------------------------

[Layer: <name> / Board-level]
  1. <category>: <description>
  2. <category>: <description>

------------------------------------------------------------
WARNINGS (<N> total)
------------------------------------------------------------

[Layer: <name> / Board-level]
  1. <category>: <description>

------------------------------------------------------------
INFO (<N> total)
------------------------------------------------------------

[Layer: <name> / Board-level]
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

DRC check: <N> errors, <N> warnings
Manufacturer DRC: <N> errors, <N> warnings (if run)
============================================================
```

### Additional guidelines

- **Do not repeat** findings already reported by `kicad-tools check` unless you are adding additional context or a different perspective. Reference the automated findings in your summary counts but focus your per-layer analysis on things the automated tools do not catch.
- **Be specific**: Include reference designators, net names, layer names, and coordinates when possible.
- **Be actionable**: Every finding should suggest what to do about it.
- **Avoid false positives**: If you are uncertain whether something is an issue, classify it as INFO rather than WARNING or CRITICAL. Defer to the automated DRC tools for anything requiring geometric computation.

---

## Phase 5: Tool Reflection

After producing the consolidated report, reflect on the tools used during this review session. This phase identifies broken tools, missing capabilities, and false positives so the project can improve its automation over time.

### 5.1 Collect tool observations

Review every `kicad-tools` command you ran (or attempted to run) during this review. For each, note:

- Did it succeed or fail?
- Was the output correct and useful?
- Did you need a tool that doesn't exist?
- Did a tool produce a false positive or miss something the manual review caught?
- Did you have to read raw `.kicad_pcb` S-expressions to check something a tool should have handled?

### 5.2 Categorize observations

Classify each observation into one of:

- **TOOL_FAILURE**: A command errored, crashed, or produced clearly wrong output
- **CAPABILITY_GAP**: You needed a tool or feature that doesn't exist in `kicad-tools` (e.g., had to manually parse S-expressions for something that should be automated)
- **FALSE_POSITIVE**: A tool flagged something that the manual review determined is not actually an issue
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

PCB review of <project name> using `/review-pcb`

## Impact

<How this affects review quality -- e.g., "Reviewer had to manually parse S-expressions to verify trace widths">

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

Append this section to the end of the Phase 4 report:

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
