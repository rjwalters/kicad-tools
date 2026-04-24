# Export Manufacturing Package

You are a PCB manufacturing engineer preparing a complete fabrication and assembly package from a KiCad PCB. Your goal is to produce a clean, validated package ready to submit to the manufacturer.

**PCB path**: `$ARGUMENTS`

If no path was provided, look for a `*-routed.kicad_pcb` file in the current project directory. If multiple exist, ask the user which to export.

---

## Phase 1: Pre-export validation

Before exporting, verify the board is manufacturing-ready.

### 1.1 DRC check

```bash
PYTHONPATH=src python3 -m kicad_tools.cli check <pcb> --format json --strict
```

Review results. A few inherent violations (e.g., fine-pitch pad clearance on QFP/QFN parts) are acceptable — flag them but don't block export. True routing or placement errors should be fixed first.

### 1.2 Net connectivity

```bash
PYTHONPATH=src python3 -m kicad_tools.cli net-status <pcb>
```

All nets must be fully routed. Incomplete nets block export — tell the user to run `/build-pcb` or `/repair-pcb` first.

### 1.3 Board summary

```bash
PYTHONPATH=src python3 -m kicad_tools.cli pcb summary <pcb> --format json
```

Note the board dimensions, layer count, component count, and net count for the export report.

---

## Phase 2: Determine manufacturer

Check the schematic title block and `.kicad_pro` for manufacturer hints. Common targets:

- **jlcpcb** (default) — Seeed Fusion / JLCPCB
- **pcbway**
- **oshpark**
- **generic**

If the schematic mentions "Seeed", "JLCPCB", or "OPL", use `jlcpcb`.

---

## Phase 3: Generate the manufacturing package

### 3.1 Run export

Use the project venv for full dependency support (cairosvg, markdown, weasyprint):

```bash
PYTHONPATH=src .venv/bin/python -m kicad_tools.cli export <pcb> \
  --mfr <manufacturer> \
  -o <output-dir>
```

The default output directory is `manufacturing/` alongside the PCB file. This generates:

- **gerbers.zip** — Copper, mask, silk, drill files
- **bom_<mfr>.csv** — Bill of materials with LCSC part numbers (for JLCPCB)
- **cpl_<mfr>.csv** — Component placement list for SMT assembly
- **kicad_project.zip** — Source project files (only the exported PCB, schematics, and project file; excludes backups)
- **report.pdf** — Full report with schematic figures, PCB layout renders, BOM, DRC status, routing status, and manufacturing readiness assessment (rendered via weasyprint or pandoc+TeX; falls back to report.md if no PDF renderer is available)
- **manifest.json** — SHA256 checksums of all top-level files

With `--keep-build-artifacts`, intermediate report files are preserved in `.build/report/` (markdown source, figures/, data/, metadata.json).

### 3.2 Verify the package

Check that all expected files were generated:

```bash
ls -la <output-dir>/
# If --keep-build-artifacts was used:
ls -la <output-dir>/.build/report/
ls -la <output-dir>/.build/report/figures/
```

Verify:
- Gerber ZIP exists and is non-empty
- BOM CSV has the expected component count
- CPL CSV has placement data for SMT parts
- Report PDF (or report.md) exists at the package root
- Manifest includes checksums for all files
- No `report/` subdirectory exists (artifacts are in `.build/report/` only with `--keep-build-artifacts`)

### 3.3 Review the report PDF

**Read the PDF** to verify visual quality:

```bash
# Read the PDF to check rendering
```

Check for:
- Title page renders correctly (project name, revision, date, manufacturer)
- Schematic figures are visible (not blank — known KiCad 10 issue with some sub-sheets)
- PCB layout renders show the board
- BOM table is readable (no overflow, footprints stripped of library prefixes)
- Tables are properly formatted
- Page breaks between major sections
- No raw markdown or LaTeX artifacts

### 3.4 Review the BOM

Spot-check the BOM for:
- Missing LCSC part numbers (for JLCPCB orders)
- Incorrect values or footprints
- THT parts that should be excluded from SMT assembly

---

## Phase 4: Report quality cycle (optional)

If the report PDF has quality issues, run a review/revise cycle:

```bash
# Review the report
/report-review <output-dir>

# Apply fixes based on review
/report-revise <output-dir>
```

Target score: >= 30/35 with 0 critical issues.

---

## Phase 5: Copy to destination

If the user specified a destination (e.g., Desktop), copy the entire package:

```bash
cp -r <output-dir> <destination>
```

---

## Phase 6: Report results

Print a summary:

```
============================================================
MANUFACTURING EXPORT COMPLETE
============================================================
Board: <name>
Manufacturer: <mfr>
Output: <output-dir>

Package contents:
  gerbers.zip         <size>
  bom_<mfr>.csv       <N> components
  cpl_<mfr>.csv       <N> placements
  kicad_project.zip   <size>
  report/report.pdf   <size> (<N> pages, <N> figures)
  manifest.json       <size>

Report figures:
  PCB renders:  <N> (front, back, copper, assembly)
  Schematics:   <N> sheets

DRC: <N errors>, <N warnings>
Nets: <N>/<N> complete

Status: READY TO ORDER / NEEDS ATTENTION
============================================================
```

---

## Known Issues

### Blank schematic sheets in SVG export

KiCad 10.0.1's `kicad-cli sch export svg` may produce blank SVGs for hierarchical sub-sheets that were generated programmatically (by kicad-tools). Sheets edited in KiCad GUI render correctly. This is an upstream kicad-cli bug — the workaround is to open and re-save affected sheets in KiCad's schematic editor.

### cairosvg on macOS

The figure generator requires `cairosvg` which depends on the native `libcairo` C library. On macOS with Homebrew, the tool auto-detects `/opt/homebrew/lib/libcairo.dylib`. If figure generation fails, ensure cairo is installed:

```bash
brew install cairo
```

### PDF renderer selection

The report renderer tries weasyprint first (styled HTML tables, better visual quality), then falls back to pandoc+TeX (simpler tables but proper LaTeX rendering). Both handle the YAML front matter and page breaks correctly.

---

## Phase 7: Tool Reflection

After the export completes, reflect on tools used during this session:

1. Collect observations about every `kicad-tools` command run
2. Categorize as TOOL_FAILURE, CAPABILITY_GAP, FALSE_POSITIVE, or IMPROVEMENT
3. For TOOL_FAILURE and CAPABILITY_GAP, file GitHub issues via loom pattern
4. Print reflection summary
