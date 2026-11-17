# Atopile Layout Reuse & Sync Research

This document summarizes research into atopile's layout reuse system that preserves and syncs KiCad PCB layouts across design iterations.

## Source Files Analyzed

- `vendor/atopile/src/atopile/layout.py` - Layout management and trait system
- `vendor/atopile/src/faebryk/exporters/pcb/layout/layout_sync.py` - Core sync algorithm
- `vendor/atopile/src/atopile/build_steps.py` - Build integration
- `vendor/atopile/src/atopile/cli/kicad_ipc.py` - KiCad integration
- `vendor/atopile/examples/layout_reuse/` - Example project

## Key Questions Answered

### 1. How do they preserve placement/routing when regenerating PCB?

Atopile uses a **hierarchical address-based matching system**. Each footprint in the PCB file has an `atopile_address` property that contains a semantic path like `usb_c.esd_protection.package`. This address identifies the component's position in the module hierarchy rather than using designators (R1, C2) or UUIDs which can change between regenerations.

When the PCB is regenerated:
1. Existing footprint positions are preserved by matching on `atopile_address`
2. New footprints (those with addresses not in the original) can have layouts pulled from template sub-PCBs
3. Routes are copied with net remapping to handle net name changes

### 2. What's the sync algorithm for matching components?

The `LayoutSync` class implements the core algorithm:

```
1. Index footprints by atopile_address
2. Group footprints by parent module (using atopile_subaddresses)
3. For each group to sync:
   a. Find anchor footprint (largest by pad count)
   b. Calculate offset between source and target positions
   c. Generate net mapping by matching pads on corresponding footprints
   d. Sync footprints: copy positions from source, apply offset
   e. Sync routes: copy tracks/arcs/vias/zones with offset and net remapping
   f. Sync other elements: graphics, text, images
```

Key matching logic:

```python
# Net mapping via pad matching
for src_addr, tgt_addr in addr_map.items():
    src_fp = source_fps[src_addr]
    tgt_fp = target_fps[tgt_addr]

    for src_pad in src_fp.pads:
        # Match pads by number (name), then by size if multiple
        tgt_pad = match_pad_by_name_and_size(tgt_fp.pads, src_pad)
        if src_pad.net and tgt_pad.net:
            net_map[src_pad.net.name] = tgt_pad.net.name
```

### 3. How do they handle component additions/removals?

**Additions:**
- New footprints are detected by comparing current vs original `atopile_address` sets
- Groups containing only new footprints trigger a `pull_group_layout()` call
- The layout is pulled from the sub-PCB template (specified in `atopile_subaddresses`)

**Removals:**
- Footprints are removed by the normal netlist update process
- The sync algorithm filters out removed footprints from group membership
- Old routes/graphics in groups are cleaned via `_clean_group()` before re-syncing

**Modifications:**
- Changed footprints (same address, different footprint) are handled by re-syncing
- Position updates from source PCB are applied via offset calculation

### 4. Can we adopt similar layout preservation?

Yes, the key patterns applicable to kicad-tools:

## Architecture Overview

### SubPCB System

```
┌─────────────────────────────────────────────────────────────┐
│                      Top-Level PCB                          │
│                                                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │  SubPCB 1   │  │  SubPCB 2   │  │  SubPCB 3   │         │
│  │ (USB-C)     │  │ (LDO)       │  │ (ESP32)     │         │
│  │             │  │             │  │             │         │
│  │ Footprints  │  │ Footprints  │  │ Footprints  │         │
│  │ + Routes    │  │ + Routes    │  │ + Routes    │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
└─────────────────────────────────────────────────────────────┘

Each SubPCB:
- Has its own .kicad_pcb file with pre-laid-out components
- Components have atopile_address properties
- Routes are relative to an anchor footprint
```

### Address Property Format

```
property "atopile_address" "usb_c.esd_protection.package"
property "atopile_subaddresses" "[modules/usb-connectors/layouts/default.kicad_pcb:esd_protection.package]"
```

- `atopile_address`: Position in the design hierarchy
- `atopile_subaddresses`: Links to source layouts (can be multiple for multi-instantiated modules)

### Build Integration Flow

```
prepare-build
    └── attach_sub_pcbs_to_entry_points(app)
           │
           ▼
     Scans for ato.yaml files
     Indexes modules with layouts
     Attaches has_subpcb traits
           │
           ▼
update-pcb
    └── attach_subaddresses_to_modules(app)
    └── LayoutSync(pcb_file.kicad_pcb)
    └── sync.sync_groups()
    └── For new groups: sync.pull_group_layout(group_name)
```

### KiCad Integration

Users can manually trigger layout sync via a KiCad button:

1. Select footprints in KiCad
2. Click "Pull Group Layout" button
3. Calls `ato kicad-ipc layout_sync --legacy --board <pcb> --include-fp <uuids>`
4. Sync pulls layout from source, applies to selection
5. PCB is saved and reloaded

## Potential Improvements for kicad-tools

Based on this research, here are applicable improvements:

### 1. Layout Preservation During PCB Regeneration

Add component matching by hierarchical path:

```typescript
interface FootprintAddress {
  path: string;           // e.g., "power.ldo.package"
  subaddresses?: string[]; // links to source layouts
}

function matchFootprints(
  original: Footprint[],
  updated: Footprint[]
): Map<Footprint, Footprint> {
  // Match by path, preserve positions for matched components
}
```

### 2. Layout Templates for Subcircuits

Enable reusable layout blocks:

```typescript
interface LayoutTemplate {
  name: string;
  pcbPath: string;
  components: Map<string, Position>;
  routes: Route[];
  anchor: string; // component to use for offset calculation
}

function applyLayoutTemplate(
  target: PCB,
  template: LayoutTemplate,
  targetComponents: Map<string, string>, // template path -> target path
  offset: Position
): void;
```

### 3. Incremental Layout Updates

Only modify changed components:

```typescript
function incrementalLayoutUpdate(
  original: PCB,
  updated: PCB
): PCBDiff {
  const added = findNewComponents(original, updated);
  const removed = findRemovedComponents(original, updated);
  const modified = findModifiedComponents(original, updated);

  return {
    added,      // Need placement
    removed,    // Remove from PCB
    modified,   // May need re-routing
    unchanged   // Preserve exactly
  };
}
```

### 4. Net Remapping

Handle net name changes gracefully:

```typescript
function generateNetMap(
  source: PCB,
  target: PCB,
  componentMap: Map<string, string>
): Map<string, string> {
  // For each component pair, match pads by name
  // Build net correspondence from pad connections
  // Use majority voting for ambiguous cases
}
```

### 5. Route Preservation

Copy routes with transformation:

```typescript
function syncRoutes(
  source: PCB,
  target: PCB,
  netMap: Map<string, string>,
  offset: Position
): Route[] {
  return source.routes.map(route => ({
    ...route,
    net: netMap.get(route.net) ?? route.net,
    points: route.points.map(p => addOffset(p, offset))
  }));
}
```

## Use Case: Agent PCB Modifications

When an agent modifies a schematic and regenerates the PCB:

```
Before:                          After:
┌─────────────────────┐         ┌─────────────────────┐
│ Schematic Modified  │         │ PCB Updated         │
│ - Added C3          │   -->   │ - C1, C2 preserved  │
│ - Changed R1 value  │         │ - C3 needs placement│
│ - Removed R2        │         │ - R2 removed        │
└─────────────────────┘         │ - Routes preserved  │
                                └─────────────────────┘
```

Key behaviors:
1. Existing component placements preserved (matched by path/address)
2. Existing routes preserved where nets unchanged
3. Only new components need placement
4. Net changes flagged for route review

## Limitations and Considerations

1. **Footprint Changes**: If a component's footprint changes (e.g., 0402 to 0603), position is preserved but routing may conflict
2. **Net Topology Changes**: Major netlist changes may invalidate existing routes
3. **Group Boundaries**: The system works best with well-defined subcircuit boundaries
4. **Template Maintenance**: Source layouts must be maintained separately
5. **Rotation Handling**: Current implementation notes "TODO rotation?" - rotation sync is incomplete

## Conclusion

Atopile's layout reuse system provides a solid pattern for preserving PCB work across regeneration cycles. The key innovation is using semantic hierarchical addresses instead of designators/UUIDs for component matching. This approach would be valuable for kicad-tools, particularly for:

1. Preserving agent-made layout changes when regenerating from modified schematics
2. Enabling reusable subcircuit layouts (LED matrices, power supplies, etc.)
3. Supporting incremental PCB updates without full re-layout
