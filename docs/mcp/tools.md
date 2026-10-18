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
    "layers": ["top", "bottom"],
    "rotation_corrections": 2
  },
  "zip_file": "/path/to/board-jlcpcb-assembly.zip",
  "warnings": ["3 parts missing LCSC part numbers"]
}
```

`pnp.rotation_corrections` is the number of CPL rows whose rotation was shifted by the manufacturer's per-footprint correction table (e.g. SOIC-8, SOT-23 on JLCPCB); it is `0` for manufacturers without corrections.

---

## Analysis Tools

### board_summary

Get board summary including layers, components, nets, and dimensions.

**Description**: Returns a high-level overview of a PCB file (offline, no KiCad required): board dimensions, layer information, component counts by type, net summary with routing status, zones, and overall routing completion.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pcb_path` | string | Yes | - | Absolute path to .kicad_pcb file |

**Response** (from a real call on `tests/fixtures/projects/test_project.kicad_pcb`):

```json
{
  "file_path": "/path/to/test_project.kicad_pcb",
  "board_dimensions": {
    "width_mm": 60.0,
    "height_mm": 30.0,
    "area_mm2": 1800.0,
    "outline_type": "rectangle"
  },
  "layers": {
    "copper_layers": 2,
    "layer_names": ["F.Cu", "B.Cu"],
    "has_internal_planes": false
  },
  "components": {
    "total_count": 3,
    "smd_count": 3,
    "through_hole_count": 0,
    "by_type": {"resistor": 1, "capacitor": 1, "diode": 1},
    "fixed_count": 0,
    "unplaced_count": 0
  },
  "nets": {
    "total_nets": 3,
    "routed_nets": 1,
    "unrouted_nets": 2,
    "power_nets": ["VIN", "GND"],
    "high_fanout_nets": []
  },
  "zones": [],
  "routing_status": {
    "completion_percent": 33.3,
    "total_airwires": 2,
    "total_trace_length_mm": 18.4,
    "via_count": 0
  }
}
```

---

### board_inspect

Inspect one aspect of a PCB in detail.

**Description**: Unlike `board_summary`, returns focused, detailed data for a single aspect: `footprints` (position, type, pads), `nets` (per-net routing status), `layers` (stackup), `design_rules` (setup parameters) or `zones` (thermal relief settings). Works offline.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pcb_path` | string | Yes | - | Absolute path to .kicad_pcb file |
| `aspect` | string | Yes | - | One of `"footprints"`, `"nets"`, `"layers"`, `"design_rules"`, `"zones"` |
| `layer` | string | No | - | Filter by layer name |
| `reference_prefix` | string | No | - | Filter footprints by reference prefix (e.g. `"R"`). `footprints` only |
| `unrouted_only` | boolean | No | `false` | Only unrouted/incomplete nets. `nets` only |
| `net_name` | string | No | - | Filter zones by net name (e.g. `"GND"`). `zones` only |

The response shape depends on `aspect`.

---

### get_drc_violations

Run design rule check and return violations.

**Description**: Executes pure Python DRC against the board and returns violations with locations, affected items and a pass/fail flag. The `manufacturer` parameter is passed to the underlying function as `rules=`.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pcb_path` | string | Yes | - | Absolute path to .kicad_pcb file |
| `manufacturer` | string | No | `"jlcpcb"` | Manufacturer preset (`"jlcpcb"`, `"oshpark"`, `"pcbway"`, `"seeed"`) or path to a custom rules file |
| `severity_filter` | string | No | `"all"` | `"all"`, `"error"` or `"warning"` |

**Response**:

```json
{
  "passed": false,
  "violation_count": 3,
  "error_count": 2,
  "warning_count": 1,
  "violations": [
    {
      "id": "drc-0002-37624703",
      "type": "pin1_marker_missing",
      "severity": "warning",
      "message": "D1 (LED_SMD:LED_0603_1608Metric) has no silkscreen pin-1 / polarity marker ...",
      "location": {"x_mm": 139.2, "y_mm": 50.0, "layer": "F.Cu"},
      "affected_items": [{"item_type": "component", "reference": "D1", "net": null}],
      "fix_suggestion": null,
      "required_value_mm": 2.5,
      "actual_value_mm": null
    }
  ],
  "summary_by_type": {"connectivity": 2, "pin1_marker_missing": 1},
  "manufacturer": "jlcpcb",
  "layers": 4
}
```

---

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
  "location": {"x": 45.2, "y": 32.1},
  "layer": "F.Cu",
  "clearances": [
    {
      "from_item": "U1-1",
      "from_type": "pad",
      "to_item": "C3-1",
      "to_type": "pad",
      "clearance_mm": 0.254,
      "location": {"x": 45.2, "y": 32.1},
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

`routing_impact.estimated_length_change_mm` is the change in total Euclidean pin-MST length over the nets of the moved component (positive = longer). `routing_impact.crossing_changes` is the signed change in inter-net crossings (`crossing_count`, star topology) between the board before and after the move.

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

### placement_suggestions

Get AI-friendly placement improvement recommendations.

**Description**: Analyzes current placement and returns specific suggestions for component moves that would improve placement quality. Each suggestion includes rationale explaining why the move would help.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pcb_path` | string | Yes | - | Path to .kicad_pcb file |
| `component` | string | No | - | Specific component reference to analyze |
| `max_suggestions` | integer | No | 10 | Maximum number of suggestions to return (1-50) |

Suggestions are ordered most-in-need-first (most violated constraints, then lowest confidence). An unknown `component` returns `success: false` with an error naming it.

**Response**:

```json
{
  "success": true,
  "pcb_path": "/path/to/board.kicad_pcb",
  "suggestions": [
    {
      "reference": "C1",
      "suggested_x": 45.2,
      "suggested_y": 32.1,
      "suggested_rotation": 0.0,
      "confidence": 0.62,
      "rationale": ["Close to U1 (decoupling)"],
      "constraints_satisfied": [],
      "constraints_violated": ["decoupling distance"],
      "alternatives": []
    }
  ],
  "count": 1
}
```

---

## Routing Tools

### route_net

Route a specific net with configurable strategy.

**Description**: Attempts to route a single net using the autorouter and writes the result to a PCB file.

> **The input file is not overwritten by default.** When `output_path` is omitted the routed board is written to `<stem>_routed.kicad_pcb` next to the input. The input is overwritten **only** if you pass `output_path` equal to `pcb_path`. (Before Issue #7111, omitting `output_path` overwrote the input.)

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pcb_path` | string | Yes | - | Path to .kicad_pcb file |
| `net_name` | string | Yes | - | Name of the net to route (e.g. `"GND"`, `"SPI_CLK"`) |
| `output_path` | string | No | `<stem>_routed.kicad_pcb` | Output file. Set equal to `pcb_path` to overwrite the input. The result's `output_path` is `null` when nothing was written (net already routed, or fewer than 2 pads) |
| `strategy` | string | No | `"auto"` | `"auto"`, `"shortest"` or `"avoid_vias"` |
| `layer_preference` | string | No | - | Preferred layer (e.g. `"F.Cu"`, `"B.Cu"`) |

There is no trace-width parameter; width comes from the board's design rules.

**Response**:

```json
{
  "success": true,
  "net_name": "SPI_CLK",
  "routed_connections": 2,
  "total_connections": 2,
  "trace_length_mm": 12.5,
  "vias_used": 1,
  "layers_used": ["B.Cu", "F.Cu"],
  "output_path": "/path/to/board_routed.kicad_pcb",
  "error_message": null,
  "suggestions": [],
  "warnings": [],
  "grid": null
}
```

On failure `success` is false, `error_message` explains why and `suggestions` lists next steps.

`grid` is the routing grid actually used
(`requested_mm`, `used_mm`, `cols`, `rows`, `layers`, `cells`, `selection`,
`coarsened`, `coarsened_reason`). On a large board the adaptive cell cap can
route on a coarser pitch than the board's rules ask for; `grid.coarsened` is
then true and `warnings` says so (Issue #6875).

---

### get_unrouted_nets

List nets that still need routing.

**Description**: Returns nets that are unrouted or (optionally) partially routed, with difficulty estimates.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pcb_path` | string | Yes | - | Path to .kicad_pcb file |
| `include_partial` | boolean | No | `true` | Include partially routed nets in results |

**Response**:

```json
{
  "total_nets": 3,
  "unrouted_count": 0,
  "partial_count": 2,
  "complete_count": 1,
  "nets": [
    {
      "name": "NET1",
      "status": "incomplete",
      "pins": 3,
      "routed_connections": 1,
      "total_connections": 2,
      "estimated_length_mm": 38.4,
      "difficulty": "medium",
      "reason": "Moderate routing distance"
    }
  ]
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
