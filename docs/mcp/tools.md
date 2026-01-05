# MCP Tool Reference

Complete documentation for all kicad-tools MCP server tools.

## Export Tools

### export_gerbers

Export Gerber files for PCB manufacturing.

**Description**: Generates all required Gerber layers (copper, soldermask, silkscreen, outline) and optionally drill files in Excellon format. Supports manufacturer presets for common PCB fabs.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pcb_path` | string | Yes | - | Path to .kicad_pcb file |
| `output_dir` | string | Yes | - | Directory for output files |
| `manufacturer` | string | No | `"generic"` | Manufacturer preset: `"generic"`, `"jlcpcb"`, `"pcbway"`, `"oshpark"`, `"seeed"` |
| `include_drill` | boolean | No | `true` | Include drill files (Excellon format) |
| `zip_output` | boolean | No | `true` | Create zip archive of all files |

**Response**:

```json
{
  "success": true,
  "output_dir": "/path/to/output",
  "zip_file": "/path/to/output/gerbers.zip",
  "files": [
    {"filename": "board-F_Cu.gbr", "layer": "F.Cu", "file_type": "copper", "size_bytes": 12345},
    {"filename": "board-B_Cu.gbr", "layer": "B.Cu", "file_type": "copper", "size_bytes": 11234}
  ],
  "layer_count": 2,
  "warnings": []
}
```

**Example**:
```
"Export Gerbers for my board at /projects/board.kicad_pcb for JLCPCB"
```

---

### export_bom

Export Bill of Materials from a schematic file.

**Description**: Generates a component list with quantities, values, footprints, and part numbers. Supports multiple output formats including CSV, JSON, and manufacturer-specific formats. Automatically extracts LCSC part numbers from component fields.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `schematic_path` | string | Yes | - | Path to .kicad_sch file |
| `output_path` | string | No | - | Output file path (omit for data-only response) |
| `format` | string | No | `"csv"` | Output format: `"csv"`, `"json"`, `"jlcpcb"`, `"pcbway"`, `"seeed"` |
| `group_by` | string | No | `"value+footprint"` | Grouping: `"value"`, `"footprint"`, `"value+footprint"`, `"mpn"`, `"none"` |
| `include_dnp` | boolean | No | `false` | Include Do Not Place components |

**Response**:

```json
{
  "success": true,
  "total_parts": 127,
  "unique_parts": 45,
  "output_path": "/path/to/bom.csv",
  "missing_lcsc": ["U3", "J2"],
  "items": [
    {
      "reference": "R1, R2, R3",
      "value": "10k",
      "footprint": "0402",
      "quantity": 3,
      "lcsc_part": "C25744",
      "description": "Resistor 10k 1%"
    }
  ],
  "format": "csv"
}
```

---

### export_assembly

Generate complete assembly package for manufacturing.

**Description**: Creates a comprehensive manufacturing package including Gerber files, BOM, and pick-and-place (PnP/CPL) files tailored to specific manufacturers. Outputs a single zip file ready for upload.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pcb_path` | string | Yes | - | Path to .kicad_pcb file |
| `schematic_path` | string | Yes | - | Path to .kicad_sch file |
| `output_dir` | string | Yes | - | Directory for output files |
| `manufacturer` | string | No | `"jlcpcb"` | Target manufacturer: `"jlcpcb"`, `"pcbway"`, `"seeed"`, `"generic"` |

**Response**:

```json
{
  "success": true,
  "output_dir": "/path/to/output",
  "manufacturer": "jlcpcb",
  "gerbers": {
    "success": true,
    "files": [...],
    "layer_count": 4
  },
  "bom": {
    "output_path": "/path/to/bom.csv",
    "component_count": 45,
    "unique_parts": 45,
    "missing_lcsc": 3
  },
  "pnp": {
    "output_path": "/path/to/cpl.csv",
    "component_count": 127,
    "layers": ["top", "bottom"]
  },
  "zip_file": "/path/to/board-jlcpcb-assembly.zip",
  "warnings": ["3 parts missing LCSC part numbers"]
}
```

---

## Analysis Tools

### placement_analyze

Analyze current component placement quality.

**Description**: Evaluates placement with metrics for wire length, congestion, thermal characteristics, signal integrity, and manufacturing concerns. Returns an overall score, category scores, identified issues with suggestions, detected functional clusters, and routing difficulty estimates.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pcb_path` | string | Yes | - | Path to .kicad_pcb file |
| `check_thermal` | boolean | No | `true` | Include thermal analysis (power components, heat spreading) |
| `check_signal_integrity` | boolean | No | `true` | Include signal integrity hints (high-speed nets, crosstalk) |
| `check_manufacturing` | boolean | No | `true` | Include DFM checks (clearances, assembly) |

**Response**:

```json
{
  "file_path": "/path/to/board.kicad_pcb",
  "overall_score": 78.5,
  "categories": {
    "wire_length": 85.0,
    "congestion": 72.0,
    "thermal": 90.0,
    "signal_integrity": 75.0,
    "manufacturing": 80.0
  },
  "issues": [
    {
      "severity": "warning",
      "category": "routing",
      "description": "Congestion hotspot with 12 vias, density 0.85mm/mm2",
      "affected_components": ["U1", "C1", "C2"],
      "location": [45.2, 32.1],
      "suggestion": "Spread components to reduce congestion"
    }
  ],
  "clusters": [
    {
      "name": "power_cluster_U1",
      "components": ["U1", "C1", "C2", "L1"],
      "centroid": [45.0, 30.0],
      "compactness_score": 85.0
    }
  ],
  "routing_estimate": {
    "estimated_routability": 75.0,
    "congestion_hotspots": [[45.2, 32.1], [80.5, 15.3]],
    "difficult_nets": ["SPI_CLK", "USB_D+", "USB_D-"]
  }
}
```

---

### measure_clearance

Measure clearance between items on the PCB.

**Description**: Measures the minimum edge-to-edge clearance between two items (components or nets) on the PCB. If item2 is not specified, finds the nearest neighbor to item1. Returns detailed measurements and design rule pass/fail status.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pcb_path` | string | Yes | - | Path to .kicad_pcb file |
| `item1` | string | Yes | - | Component reference (e.g., `"U1"`) or net name (e.g., `"GND"`) |
| `item2` | string | No | - | Second item, or omit for nearest neighbor search |
| `layer` | string | No | - | Specific layer (e.g., `"F.Cu"`), or omit for all layers |

**Response**:

```json
{
  "item1": "U1",
  "item2": "C3",
  "min_clearance_mm": 0.254,
  "location": [45.2, 32.1],
  "layer": "F.Cu",
  "clearances": [
    {
      "from_item": "U1-1",
      "from_type": "pad",
      "to_item": "C3-1",
      "to_type": "pad",
      "clearance_mm": 0.254,
      "location": [45.2, 32.1],
      "layer": "F.Cu"
    }
  ],
  "passes_rules": true,
  "required_clearance_mm": 0.15
}
```

---

## Session Tools

Session tools enable interactive, step-by-step placement refinement with undo capability.

### start_session

Start a new placement refinement session.

**Description**: Creates a stateful session for interactively refining component placement through query-before-commit operations. Returns a session ID used for subsequent operations.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pcb_path` | string | Yes | - | Absolute path to .kicad_pcb file |
| `fixed_refs` | array | No | - | Component references to keep fixed (optional) |

**Response**:

```json
{
  "success": true,
  "session_id": "a1b2c3d4",
  "component_count": 127,
  "fixed_count": 5,
  "initial_score": 78.5
}
```

---

### query_move

Query the impact of a hypothetical component move without applying it.

**Description**: Returns score changes, new/resolved violations, and routing impact. Use this to evaluate moves before applying them.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `session_id` | string | Yes | - | Session ID from start_session |
| `ref` | string | Yes | - | Component reference designator (e.g., `"C1"`, `"R5"`) |
| `x` | number | Yes | - | Target X position in millimeters |
| `y` | number | Yes | - | Target Y position in millimeters |
| `rotation` | number | No | - | Target rotation in degrees (keeps current if not specified) |

**Response**:

```json
{
  "success": true,
  "would_succeed": true,
  "score_delta": -2.5,
  "new_violations": [],
  "resolved_violations": [
    {
      "type": "clearance",
      "description": "C1 too close to U1",
      "severity": "warning"
    }
  ],
  "affected_components": ["C1", "U1", "R3"],
  "routing_impact": {
    "affected_nets": ["VCC", "GND"],
    "estimated_length_change_mm": -2.3,
    "crossing_changes": 0
  },
  "warnings": []
}
```

---

### apply_move

Apply a component move within the session.

**Description**: The move can be undone with undo_move and is not written to disk until commit_session is called. Returns updated component position and score delta.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `session_id` | string | Yes | - | Session ID from start_session |
| `ref` | string | Yes | - | Component reference designator |
| `x` | number | Yes | - | New X position in millimeters |
| `y` | number | Yes | - | New Y position in millimeters |
| `rotation` | number | No | - | New rotation in degrees (optional) |

**Response**:

```json
{
  "success": true,
  "move_id": 1,
  "component": {
    "ref": "C1",
    "x": 45.2,
    "y": 32.1,
    "rotation": 0.0,
    "fixed": false
  },
  "new_score": 81.0,
  "score_delta": -2.5,
  "pending_moves": 1
}
```

---

### undo_move

Undo the last applied move in the session.

**Description**: Restores the component to its previous position. Can be called multiple times to undo multiple moves.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `session_id` | string | Yes | - | Session ID from start_session |

**Response**:

```json
{
  "success": true,
  "restored_component": {
    "ref": "C1",
    "x": 43.0,
    "y": 30.5,
    "rotation": 0.0,
    "fixed": false
  },
  "pending_moves": 0,
  "current_score": 78.5
}
```

---

### commit_session

Commit all pending moves to the PCB file and close the session.

**Description**: Writes changes to disk. Optionally specify output_path to save to a different file instead of overwriting the original.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `session_id` | string | Yes | - | Session ID from start_session |
| `output_path` | string | No | - | Output file path (overwrites original if not specified) |

**Response**:

```json
{
  "success": true,
  "output_path": "/path/to/board.kicad_pcb",
  "moves_applied": 5,
  "initial_score": 78.5,
  "final_score": 85.0,
  "score_improvement": 6.5,
  "components_moved": ["C1", "C2", "R5", "L1", "U3"],
  "session_closed": true
}
```

---

### rollback_session

Discard all pending moves and close the session.

**Description**: No changes are written to disk. Use this to abandon a session without saving.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `session_id` | string | Yes | - | Session ID from start_session |

**Response**:

```json
{
  "success": true,
  "moves_discarded": 5,
  "session_closed": true
}
```

---

## Error Handling

All tools return consistent error responses:

```json
{
  "success": false,
  "error": "Error message describing what went wrong"
}
```

Common errors:

| Error | Cause | Solution |
|-------|-------|----------|
| `PCB file not found` | Invalid file path | Verify the path exists |
| `Invalid file extension` | Wrong file type | Use .kicad_pcb or .kicad_sch |
| `Session not found` | Invalid or expired session ID | Start a new session |
| `Component not found` | Invalid reference designator | Check component exists |
| `Unknown manufacturer` | Invalid manufacturer preset | Use supported manufacturer |
