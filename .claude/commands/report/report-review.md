# Review PCB Manufacturing Report

You are a PCB manufacturing report reviewer. Your task is to critically review a manufacturing report and produce a scored review with actionable issues. **This skill is read-only with respect to the report -- it does not modify it. Use `/report-revise` to apply changes.**

## Invocation

```
/report-review <path-to-report.md>
/report-review <path-to-manufacturing-dir>
```

**Arguments**: `$ARGUMENTS`

If a directory is given, look for `report/report.md` inside it.
If no argument is given, look for the most recent `manufacturing/report/report.md` relative to the current project.

## Locate the Report

1. Find `report.md` (the source markdown)
2. Find `report.pdf` (the rendered output) -- read it to assess visual quality
3. Find the sibling manufacturing artifacts (`bom_*.csv`, `cpl_*.csv`, `gerbers/`, `manifest.json`) -- cross-reference against report claims
4. Find the `.kicad_pcb` file the report was generated from -- verify numbers

## Review Framework -- 7 Dimensions

Score each 1-5 (total /35). Be adversarial -- find problems before the manufacturer or customer does.

### 1. Technical Accuracy (weight: CRITICAL)

- Do component counts match the actual PCB? (footprints, nets, vias, segments)
- Does the board size match the actual Edge.Cuts outline?
- Do DRC error/warning counts match a fresh `kicad-tools check` run?
- Are net completion percentages accurate?
- Are cost estimates plausible for the board complexity?
- Cross-reference BOM table against the actual BOM CSV -- same component count, same groupings?

### 2. Completeness (weight: CRITICAL)

- Are all expected sections present? Required:
  - Board Summary
  - Bill of Materials
  - DRC Status
  - Routing Status
  - Manufacturing Readiness / Action Items
  - Cost Estimate
- Are there sections that should be present but are missing?
  - Stackup / layer assignment (for 4+ layer boards)
  - Power distribution (zones, planes)
  - Signal integrity notes (for high-speed designs)
  - Assembly notes (THT vs SMT split, special handling)

### 3. BOM Quality (weight: HIGH)

- Are LCSC part numbers populated? What percentage are missing?
- Are component values complete and unambiguous (e.g., "100nF" vs "100nF 25V X7R")?
- Are footprint names human-readable or raw KiCad library paths?
- Is the BOM grouped sensibly (by type, not random order)?
- Are MPN fields populated for key ICs?
- Are THT parts clearly distinguished from SMT?

### 4. Clarity (weight: HIGH)

- Is the report readable by a manufacturing engineer who doesn't have the schematic?
- Are action items specific and actionable?
- Are status indicators clear (PASS/FAIL/WARNING)?
- Is internal tooling jargon avoided? (e.g., raw file paths, tool command lines)
- Are section headings properly formatted (no raw markdown artifacts in PDF)?

### 5. Visual Quality (weight: HIGH)

Read the PDF and assess:
- Do tables render correctly? (columns aligned, no overflow, readable)
- Is there a proper title/cover section?
- Are there page breaks between major sections?
- Do long strings (paths, footprint names) wrap or overflow?
- Is the overall layout professional enough to send to a manufacturer?
- Are there rendering artifacts (raw HTML, unprocessed markdown syntax)?

### 6. Manufacturing Readiness Assessment (weight: HIGH)

- Does the readiness verdict accurately reflect the board state?
- Are blocking issues correctly identified vs optional items?
- Is the distinction between inherent violations (fine-pitch pads) and real errors clear?
- Are zone-connected nets correctly identified (not flagged as unrouted)?
- Does the report guide the user on what to fix vs what is acceptable?

### 7. Presentation (weight: MEDIUM)

- Is the report appropriately concise? (No unnecessary verbosity, but complete)
- Are units consistent throughout?
- Is the ordering of sections logical?
- Does the report have a professional tone?
- Are empty columns (MPN, LCSC when unpopulated) handled gracefully?

## Output

Write the review to a sibling file next to the report:
- If report is at `manufacturing/report/report.md`, write to `manufacturing/report/review.md`

```markdown
# Report Review: {board-name}

**Reviewer:** Claude (automated report review)
**Date:** {date}
**Report reviewed:** `{path}`

---

## Overall Assessment: {READY TO SHIP / NEEDS WORK / NOT READY}

**Score: {N}/35**

| Dimension | Score | Key Issue |
|-----------|-------|-----------|
| Technical Accuracy | X/5 | {one-line} |
| Completeness | X/5 | {one-line} |
| BOM Quality | X/5 | {one-line} |
| Clarity | X/5 | {one-line} |
| Visual Quality | X/5 | {one-line} |
| Mfr Readiness Assessment | X/5 | {one-line} |
| Presentation | X/5 | {one-line} |

---

## Critical Issues (must fix)

1. **{Issue}** (Dimension: {N})
   - Problem: {what is wrong}
   - Impact: {why it matters}
   - Fix: {specific recommendation -- markdown template change, pandoc flag, or code change}

---

## Important Issues (should fix)

1. **{Issue}** (Dimension: {N})
   - Problem: ...
   - Fix: ...

---

## Suggestions (nice to have)

1. {suggestion}

---

## Cross-Reference Check

| Claim in Report | Actual Value | Match? |
|-----------------|-------------|--------|
| {e.g., "71 footprints"} | {from PCB} | {yes/no} |
| ... | ... | ... |

---

## Next Step

Run `/report-revise {path}` to create an improved version incorporating this review.
```

Also present the summary to the user in conversation.

## Convergence Threshold

| Score | Assessment | Action |
|-------|-----------|--------|
| >= 30/35, 0 critical | **Ready to ship** | Report is good enough to include in manufacturing package |
| 22-29/35, 0 critical | **Nearly ready** | One revise cycle should suffice |
| 15-21/35 or any critical | **Needs work** | Address critical issues first |
| < 15/35 | **Fundamental issues** | Report template needs significant rework |

## Important Notes

### Two Layers of Issues

Report problems fall into two categories:

1. **Template issues** -- the markdown generator in `src/kicad_tools/report/` emits bad content. These need code changes.
2. **Rendering issues** -- pandoc+TeX doesn't handle the markdown well. These need pandoc flags, a YAML header, or a TeX template.

Clearly label which category each issue falls into so `/report-revise` knows where to fix it.

### Verify Against the PCB

Don't just read the report -- load the actual PCB data. Run:

```bash
PYTHONPATH=src python3 -m kicad_tools.cli pcb summary <pcb> --format json
PYTHONPATH=src python3 -m kicad_tools.cli net-status <pcb>
```

Cross-reference every number in the report against these ground-truth sources.
