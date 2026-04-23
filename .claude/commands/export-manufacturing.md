# Export Manufacturing Package

You are a PCB manufacturing engineer preparing a complete fabrication and assembly package from a KiCad PCB. Your goal is to produce a clean, validated package ready to submit to the manufacturer.

**PCB path**: `$ARGUMENTS`

If no path was provided, look for a `*-routed.kicad_pcb` file in the current project directory. If multiple exist, ask the user which to export.

---

## Phase 1: Pre-export validation

Before exporting, verify the board is manufacturing-ready.

### 1.1 DRC check

```bash
kicad-tools check <pcb> --format json --strict
```

Review results. A few inherent violations (e.g., fine-pitch pad clearance on QFP/QFN parts) are acceptable — flag them but don't block export. True routing or placement errors should be fixed first.

### 1.2 Net connectivity

```bash
kicad-tools net-status <pcb>
```

All nets must be fully routed. Incomplete nets block export — tell the user to run `/build-pcb` or `/repair-pcb` first.

### 1.3 Board summary

```bash
kicad-tools pcb summary <pcb> --format json
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

```bash
kicad-tools export <pcb> \
  --mfr <manufacturer> \
  -o <output-dir>
```

The default output directory is `manufacturing/` alongside the PCB file. This generates:

- **gerbers.zip** — Copper, mask, silk, drill files
- **bom_<mfr>.csv** — Bill of materials with LCSC part numbers (for JLCPCB)
- **cpl_<mfr>.csv** — Component placement list for SMT assembly
- **kicad_project.zip** — Source project files (excludes backups)
- **report/** — Design report (PDF via pandoc+TeX if available, otherwise Markdown)
- **manifest.json** — SHA256 checksums of all files

### 3.2 Verify the package

Check that all expected files were generated:

```bash
ls -la <output-dir>/
cat <output-dir>/manifest.json
```

Verify:
- Gerber ZIP exists and is non-empty
- BOM CSV has the expected component count
- CPL CSV has placement data for SMT parts
- Report was generated (PDF preferred over raw Markdown)
- Manifest includes checksums for all files

### 3.3 Review the BOM

Spot-check the BOM for:
- Missing LCSC part numbers (for JLCPCB orders)
- Incorrect values or footprints
- THT parts that should be excluded from SMT assembly

---

## Phase 4: Copy to destination

If the user specified a destination (e.g., Desktop), copy the entire package:

```bash
cp -r <output-dir> <destination>
```

---

## Phase 5: Report results

Print a summary:

```
============================================================
MANUFACTURING EXPORT COMPLETE
============================================================
Board: <name>
Manufacturer: <mfr>
Output: <output-dir>

Package contents:
  gerbers.zip       <size>
  bom_<mfr>.csv     <N> components
  cpl_<mfr>.csv     <N> placements
  kicad_project.zip <N> files
  report.pdf        <size>  (or report/report.md)
  manifest.json     <size>

DRC: <N errors>, <N warnings>
Nets: <N>/<N> complete

Status: READY TO ORDER / NEEDS ATTENTION
============================================================
```

---

## Phase 6: Tool Reflection

After the export completes, reflect on tools used during this session:

1. Collect observations about every `kicad-tools` command run
2. Categorize as TOOL_FAILURE, CAPABILITY_GAP, FALSE_POSITIVE, or IMPROVEMENT
3. For TOOL_FAILURE and CAPABILITY_GAP, file GitHub issues via loom pattern
4. Print reflection summary
