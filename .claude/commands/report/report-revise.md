# Revise PCB Manufacturing Report

You are a PCB manufacturing report revision specialist. Your task is to consume a report and its review, then produce an improved version with all review issues addressed.

## Invocation

```
/report-revise <path-to-report.md>
/report-revise <path-to-manufacturing-dir>
```

**Arguments**: `$ARGUMENTS`

## Locate Inputs

1. Find `report.md` (the source markdown to revise)
2. Find `review.md` (the review to address -- sibling to report.md)
3. Find the `.kicad_pcb` and `.kicad_sch` files (for ground-truth data)
4. Find the report generator source code at `src/kicad_tools/report/`

If no `review.md` exists, tell the user to run `/report-review` first.

## Issue Categories

Review issues fall into two categories that require different fixes:

### Template Issues (fix in code)

These are problems with what the markdown generator emits. Fix them in the source:

- `src/kicad_tools/report/generator.py` -- report structure, section ordering, content
- `src/kicad_tools/report/templates/` -- Jinja2 templates for each section
- `src/kicad_tools/report/models.py` -- data models for report content
- `src/kicad_tools/report/collector.py` -- data collection from PCB/schematic

### Rendering Issues (fix in pandoc pipeline)

These are problems with how markdown becomes PDF. Fix them in:

- `src/kicad_tools/report/renderers.py` -- the `render_pdf_pandoc()` function
- Pandoc YAML front matter (add to the markdown template)
- A custom pandoc template or LaTeX header file
- Pandoc command-line flags

## Workflow

### Step 1: Read Everything

Read ALL of these:

- `report.md` (current report)
- `review.md` (the review with scored issues)
- The report generator source code (templates, generator, models)
- The rendered `report.pdf` (to see visual problems firsthand)

### Step 2: Triage Issues

Parse the review into actionable items. Categorize each:

| Category | Where to Fix |
|----------|-------------|
| **Template content** | `src/kicad_tools/report/templates/*.md.j2` or `generator.py` |
| **Template structure** | `generator.py` (section ordering, missing sections) |
| **Data collection** | `collector.py` or `models.py` |
| **Pandoc rendering** | `renderers.py` (flags, YAML header, TeX template) |
| **Table formatting** | Template (simplify column content) AND/OR pandoc (column widths) |

Present the triage:

```
## Revision Plan

### Template fixes ({count}):
1. {issue} -> {which file, what change}

### Rendering fixes ({count}):
1. {issue} -> {which file, what change}

### Deferred ({count}):
1. {issue} -> {why deferred}
```

### Step 3: Apply Fixes

#### Rendering fixes first (highest impact on visual quality)

Common pandoc rendering fixes:

1. **Add YAML front matter to markdown template** for pandoc metadata:
   ```yaml
   ---
   title: "{project} Design Report"
   geometry: margin=1in
   fontsize: 11pt
   colorlinks: true
   header-includes:
     - \usepackage{longtable}
     - \usepackage{booktabs}
     - \usepackage{array}
   ---
   ```

2. **Remove raw HTML** -- pandoc+TeX doesn't render HTML divs. Replace `<div class="cover-block">` with pure markdown or TeX-compatible formatting.

3. **Fix table overflow** -- long footprint names need to be:
   - Shortened (strip `Capacitor_SMD:` prefix, show just package)
   - Or wrapped (use pandoc grid tables or `longtable`)

4. **Add page breaks** -- insert `\newpage` between major sections

5. **Update `render_pdf_pandoc()`** with better flags:
   ```python
   cmd = [
       "pandoc", str(markdown_path),
       "-o", str(output_path),
       f"--pdf-engine={pdf_engine}",
       "--variable=geometry:margin=1in",
       "--variable=colorlinks:true",
       "--from=markdown+pipe_tables+yaml_metadata_block",
   ]
   ```

#### Template content fixes

1. **Simplify footprint names** in BOM table -- strip library prefix, show only package name
2. **Remove empty columns** -- if MPN/LCSC are all empty, don't include them
3. **Remove raw file paths** -- replace `/Users/rwalters/GitHub/...` with relative paths or just command names
4. **Fix heading hierarchy** -- ensure no `###` headings appear inline with body text
5. **Add missing sections** based on review findings

#### Data accuracy fixes

1. Cross-reference report data against fresh PCB queries
2. Fix any stale or incorrect numbers
3. Update status assessments to match reality

### Step 4: Regenerate and Verify

After code changes:

1. Re-run the export to generate a new report:
   ```bash
   PYTHONPATH=src python3 -m kicad_tools.cli export <pcb> --mfr <mfr> -o <output-dir>
   ```

2. Read the new `report.pdf` to verify visual improvements

3. Compare against review checklist -- are critical issues resolved?

### Step 5: Self-Check

- [ ] Every critical review issue addressed
- [ ] Every important review issue addressed or explicitly deferred
- [ ] PDF renders cleanly (tables fit, no overflow, no raw markdown)
- [ ] No raw HTML in the markdown source
- [ ] No absolute file paths in report content
- [ ] All numbers match the actual PCB data
- [ ] BOM table is readable with proper column widths
- [ ] Report has clear section breaks
- [ ] Title/header section renders properly

### Step 6: Present Summary

```
## Revision Complete

**Previous review score:** {X}/35
**Issues resolved:** {M}/{N} critical, {P}/{Q} important
**Files changed:**
- src/kicad_tools/report/templates/{file} -- {what changed}
- src/kicad_tools/report/renderers.py -- {what changed}
- ...

### Key Improvements
1. {most impactful change}
2. {second}

### Remaining Issues
- {anything deferred, with rationale}

### Next Step
Run `/report-review {path}` for another review cycle.
When score >= 30/35 with 0 critical issues, the report is ready to ship.
```

## Convergence

| Review Score | Assessment | Action |
|-------------|-----------|--------|
| >= 30/35, 0 critical | **Ready to ship** | Stop cycling |
| 22-29/35, 0 critical | **Nearly ready** | One more cycle |
| < 22/35 or critical issues | **Needs work** | Continue cycling |

## Important Notes

### Don't Over-Revise

Fix what the review flagged. Don't refactor the entire report generator if only the BOM table needs work. Scope changes to what's needed.

### Test the PDF

Every revision must produce a new PDF. Read it. If the tables still overflow or the formatting is still broken, the revision isn't done.

### Template vs One-Off

If a fix improves ALL future reports (e.g., stripping footprint prefixes), put it in the template/generator code. If it's specific to this board, note it but prefer the general fix.
