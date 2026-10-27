# Evaluation: KiCAD-MCP-Server (mixelpixx, MIT) vs `kct mcp serve`

**Issue**: #7990
**Date**: 2026-10-27
**Upstream**: https://github.com/mixelpixx/KiCAD-MCP-Server, MIT, pinned at
commit `628d320f4cd51b51e55489fd6f29f20c228e2741` (2026-10-25, "Merge pull
request #469 from mixelpixx/fix/467-export-svg"; `package.json` 2.8.3).
**Method**: shallow `git clone` into a scratch directory outside this repo,
read-only. Nothing was installed or executed and no upstream code was copied.
The tool catalog was read from the `server.tool(...)` registrations in
`src/tools/*.ts` (247 distinct tool names) and the category map in
`src/tools/registry.ts`. Our side is `TOOL_REGISTRY` in
`src/kicad_tools/mcp/tools/` (48 tools) plus the `kct` CLI behind it.
Star count and push date in the registry were not re-fetched (the forge read
path was unavailable); `last_push` is taken from the pinned commit date.

## TL;DR

**Verdict: `complementary`.** KiCAD-MCP-Server is a *wide, imperative* editing
surface: ~250 fine-grained tools that each mutate a KiCad file (or drive the
KiCad GUI / IPC) one call at a time. `kct mcp serve` is a *narrow, intent- and
verification-oriented* surface: 48 tools built around sessions with
rollback, placement/routing optimisation, design intent, DRC/mistake
detection, and manufacturing export. Almost no tool overlaps one-to-one; we do
not want to chase its count, and most of its schematic-surgery tools are
served on our side by the `kct` CLI and Python API rather than MCP.

Three areas are worth a closer look because they are real capability we lack
at the MCP layer (follow-ups below): a Freerouting *runner* (we export DSN and
import SES, but never run Freerouting), a live JLCPCB parts *search* tool
(we have `kct parts` and an offline catalog, not MCP tools), and symbol
*creation*/library registration tools. Being MIT, code from these can move
with attribution; each follow-up says so.

## Where the two surfaces differ

| Aspect | KiCAD-MCP-Server | `kct mcp serve` |
|--------|------------------|-----------------|
| Size | 247 tools, toolbox mode to cap the visible count | 48 tools |
| Language | TypeScript server over Python command modules | Python, in-process |
| Style | CRUD on files and the live GUI (pcbnew, IPC, AT-SPI) | Sessions, transactions, checkpoints, intent, analysis |
| Safety net | none beyond KiCad's own undo / `snapshot_project` | `start_session` / `commit_session` / `rollback_session`, `undo_move` |
| Verification | `run_drc`, `run_erc`, `validate_*` | DRC, `detect_mistakes`, `measure_clearance`, BOM validation, placement scoring |
| Routing | trace-by-trace plus Freerouting | `route_net`, `route_net_auto` (own A* router) |

## Tool-by-tool table

Every upstream tool appears exactly once. Status: **have** (equivalent
capability exists, in MCP or the `kct` CLI/API), **partial** (some of it),
**missing** (we have nothing comparable), **out of scope** (conflicts with our
headless, file-based model or non-goals).

### Project and session (`project.ts`, `ui.ts`, `gui-driver.ts`)

| Upstream tool(s) | Status | Reason |
|---|---|---|
| `create_project` | partial | `kct` project scaffolding exists in the CLI/recipes, not as an MCP tool |
| `open_project`, `open_board`, `reload_board`, `close_project`, `is_dirty`, `discard_or_reload`, `get_project_info` | out of scope | Stateful live-editor handle; we operate on files, with `start_session` for scoped state |
| `save_project`, `save_board`, `save_as` | out of scope | Our writes are explicit file writes committed by `commit_session` |
| `snapshot_project` | have | `create_checkpoint` / `restore_checkpoint` |
| `get_backend_state`, `check_kicad_ui`, `launch_kicad_ui` | out of scope | We never drive a running KiCad UI |
| `kicad_gui_tree`, `kicad_gui_click`, `kicad_run_action_plugin`, `kicad_gui_wait_for`, `kicad_gui_screenshot`, `kicad_pcb_snapshot`, `kicad_reload_and_open_plugin`, `kicad_run_drc`, `kicad_gui_tree_atspi`, `kicad_gui_click_atspi`, `install_gui_driver` | out of scope | GUI automation; `screenshot_board` renders headlessly instead |

### Board setup and graphics (`board.ts`, `pcb-import.ts`, `eagle.ts`)

| Upstream tool(s) | Status | Reason |
|---|---|---|
| `set_board_size`, `add_board_outline`, `clear_board_outline`, `replace_board_outline` | partial | Outline is created by board generators/recipes, not an MCP call |
| `set_board_origin`, `get_board_origin`, `get_board_extents` | partial | Extents come from `board_summary`; origin has no tool |
| `get_board_info`, `get_layer_list` | have | `board_summary`, `board_inspect` |
| `add_layer`, `set_active_layer` | missing | Not exposed; low value for our flows |
| `list_graphics`, `delete_graphic`, `update_graphic`, `add_board_text` | partial | Silkscreen tooling exists in `kicad_tools.silkscreen`; no MCP tool |
| `add_mounting_hole` | partial | Available through the Python API, no MCP tool |
| `add_zone` | partial | `kicad_tools.zones` creates pours; no MCP tool |
| `get_board_2d_view` | have | `board_view`, `screenshot_board` |
| `import_svg_logo` | missing | Niche; idea only |
| `import_pcb`, `import_eagle_project` | out of scope | Foreign-format import is a non-goal |

### Components and placement (`component.ts`)

| Upstream tool(s) | Status | Reason |
|---|---|---|
| `place_component`, `move_component`, `batch_move_components`, `rotate_component` | have | `apply_move` (transactional, with `query_move` preview) |
| `delete_component`, `edit_component`, `duplicate_component`, `replace_component`, `set_footprint_type`, `add_component_annotation`, `group_components`, `place_component_array`, `align_components`, `move_footprint_text` | partial | Done through the Python API / CLI (`kct pcb ...`); not MCP tools |
| `find_component`, `get_component_properties`, `get_component_list`, `get_component_geometry` | have | `board_inspect` |
| `get_component_pads`, `get_pads`, `get_net_pads`, `get_pad_position` | have | `board_inspect`, `get_component_requirements` |
| `get_ratsnest`, `estimate_airwire_lengths` | have | `get_unrouted_nets`, `placement_analyze` |
| `check_placement_clearance`, `check_courtyard_overlaps` | have | `measure_clearance`, `evaluate_placement`, `resolve_placement_overlaps` |
| `suggest_placement`, `hierarchical_place` | have | `placement_suggestions`, `optimize_placement`, `placement_place_unplaced`; ours is stronger (scored, constraint-aware) |

### Routing and nets (`routing.ts`, `freerouting.ts`, `router.ts`)

| Upstream tool(s) | Status | Reason |
|---|---|---|
| `add_net`, `get_nets_list`, `create_netclass` | partial | Net classes are read from `.kicad_pro`; no MCP creation tool |
| `route_trace`, `route_arc_trace`, `route_pad_to_pad`, `route_differential_pair` | have | `route_net`, `route_net_auto` (own router, diff-pair aware) |
| `add_via`, `delete_trace`, `modify_trace`, `copy_routing_pattern`, `set_net_color` | partial | Python API only; `undo_move`/sessions cover rollback |
| `query_traces`, `query_zones` | have | `board_inspect` |
| `add_copper_pour`, `refill_zones`, `add_gnd_stitching_vias` | partial | `kicad_tools.zones` and `stitching.py` exist; no MCP tools |
| `autoroute`, `check_freerouting` | missing | We have no Freerouting runner (see below) |
| `export_dsn`, `import_ses` | partial | `kct` exports DSN and imports SES from the CLI; not MCP tools |
| `list_tool_categories`, `get_category_tools`, `search_tools` | out of scope | Tool-discovery layer needed because of 247 tools; 48 do not need it |

### Design rules and DRC (`design-rules.ts`, `validation.ts`)

| Upstream tool(s) | Status | Reason |
|---|---|---|
| `set_design_rules`, `get_design_rules`, `set_layer_constraints`, `assign_net_to_class` | partial | Rules come from manufacturer profiles and `.kicad_dru` generation, not MCP tools |
| `run_drc`, `get_drc_violations`, `check_clearance` | have | `get_drc_violations`, `measure_clearance` |
| `validate_schematic`, `validate_symbol_library` | partial | Covered by `kct erc` / library validators in the CLI |

### Export (`export.ts`)

| Upstream tool(s) | Status | Reason |
|---|---|---|
| `export_gerber`, `export_gerbers`, `export_gerber_single`, `export_drill`, `export_position_file`, `export_pos` | have | `export_gerbers`, `export_assembly` |
| `export_bom`, `export_sch_bom`, `export_sch_python_bom` | have | `export_bom`, `validate_assembly_bom` |
| `export_pdf`, `export_pcb_pdf`, `export_sch_pdf`, `export_svg`, `export_pcb_svg`, `export_sch_svg` | partial | `screenshot_board` / `screenshot_schematic` render images; PDF/SVG via CLI |
| `export_netlist`, `export_3d`, `export_3d_cli`, `export_vrml` | partial | CLI exports; not MCP tools |
| `export_ipc2581`, `export_odb`, `export_ipcd356`, `export_gencad`, `export_pcb_dxf`, `export_sch_dxf`, `export_sch_hpgl`, `export_sch_ps` | missing | Thin `kicad-cli` wrappers; we cover the fab formats JLCPCB/PCBWay need. Idea only |

### Schematic editing and analysis (`schematic.ts`, `schematic-batch.ts`, `schematic-hierarchy.ts`, `schematic-layout.ts`)

| Upstream tool(s) | Status | Reason |
|---|---|---|
| `create_schematic`, `add_schematic_component`, `delete_schematic_component`, `edit_schematic_component`, `set_schematic_component_property`, `remove_schematic_component_property`, `remove_schematic_component`, `get_schematic_component`, `move_schematic_component`, `rotate_schematic_component`, `annotate_schematic` | partial | Python schematic API and `kct sch ...` edit commands; no MCP tools |
| `add_schematic_wire`, `delete_schematic_wire`, `add_schematic_net_label`, `delete_schematic_net_label`, `move_schematic_net_label`, `add_no_connect`, `connect_to_net`, `connect_passthrough`, `add_schematic_hierarchical_label`, `add_schematic_text`, `add_sheet_pin` | partial | Same: API/CLI only |
| `get_net_connections`, `get_wire_connections`, `get_net_at_point`, `get_schematic_pin_locations`, `list_schematic_components`, `list_schematic_nets`, `list_schematic_wires`, `list_schematic_labels`, `list_schematic_texts`, `list_floating_labels`, `find_orphaned_wires`, `find_overlapping_elements`, `find_wires_crossing_symbols`, `get_elements_in_region` | have | Schematic query layer in `kicad_tools.schematic` / `kct sch`; breadth comparable, exposure is CLI not MCP |
| `export_schematic_svg`, `export_schematic_pdf`, `get_schematic_view`, `get_schematic_view_region` | have | `screenshot_schematic` |
| `run_erc` | have | `kct erc`; ERC findings also surface via the mistake detector |
| `generate_netlist`, `sync_schematic_to_board`, `create_board_from_schematic`, `backannotate_footprints` | have | `create_pcb_from_schematic`, `kicad_tools.sync` |
| `snap_to_grid`, `lint_offgrid`, `lint_schematic_cosmetic`, `suggest_schematic_declutter`, `autoplace_schematic_fields`, `set_schematic_property_position`, `batch_set_schematic_property_positions` | partial | Grid lint exists in the schematic package; cosmetic/declutter linting is an idea worth a look |
| `batch_add_components`, `batch_edit_schematic_components`, `batch_add_no_connects`, `batch_connect`, `batch_add_and_connect`, `replace_instance_lib_ids`, `replace_schematic_component`, `update_symbol_from_library`, `add_library_symbol_property` | partial | Our answer is generating schematics from code (recipes, blocks); `kct sch replace-symbol` covers replacement |
| `add_hierarchical_sheet`, `remove_hierarchical_sheet`, `set_sheet_property`, `get_sheet_properties`, `create_hierarchical_subsheet` | partial | Hierarchy is supported by the parser/blocks; no editing tools |

### Libraries, symbols, footprints (`library*.ts`, `symbol-creator.ts`, `footprint.ts`)

| Upstream tool(s) | Status | Reason |
|---|---|---|
| `list_libraries`, `list_symbol_libraries`, `list_library_symbols`, `list_library_footprints`, `search_symbols`, `search_footprints`, `get_symbol_info`, `get_footprint_info`, `list_symbol_pins`, `batch_list_symbol_pins` | have | `kct lib symbols`, `kct lib footprint`, `kct symbols`; CLI not MCP |
| `list_library_table`, `remove_library_table_entry`, `set_library_table_uri`, `register_footprint_library`, `list_footprint_libraries`, `register_symbol_library` | partial | `footprints/fp_lib_table.py` manages tables; no MCP tool |
| `create_footprint` | have | `kct footprint generate` (SOIC, QFP, QFN, DFN, SOT, BGA, chip, through-hole), standards-aware |
| `edit_footprint_pad` | missing | We regenerate rather than patch pads |
| `add_footprint_3d_model`, `import_3d_model`, `add_component_3d_model` | partial | `modelgen` generates 3D models; no import-and-attach tool |
| `create_symbol`, `delete_symbol`, `list_symbols_in_library`, `add_symbol_property`, `import_symbol`, `export_symbol`, `rename_symbol` | partial | `schematic/symbol_generator.py` and `datasheet/symbol_generator.py` generate symbols from pin data; no library CRUD |
| `set_symbol_pin_type`, `repair_flat_symbols`, `find_duplicate_symbols` | missing | Library-hygiene helpers; idea only |

### Parts sourcing (`jlcpcb-api.ts`, `digikey-api.ts`, `parts-registry.ts`, `datasheet.ts`)

| Upstream tool(s) | Status | Reason |
|---|---|---|
| `search_jlcpcb_parts`, `get_jlcpcb_part`, `get_jlcpcb_database_stats`, `download_jlcpcb_database`, `suggest_jlcpcb_alternatives` | partial | `kct parts` (LCSC search/lookup) and the offline `jlcparts_catalog`; none are MCP tools. Alternatives-suggestion is missing |
| `digikey_test_connection`, `digikey_search_parts`, `digikey_check_library_availability` | missing | No DigiKey source; credential-bound, idea only |
| `search_parts_registry`, `get_registry_part`, `download_registry_part` | missing | Third-party part registry downloads; not pursued |
| `enrich_datasheets`, `get_datasheet_url` | have | `kicad_tools.datasheet` (fetch, parse, pin inference) |

### Toolbox controls (`toolboxes.ts`)

| Upstream tool(s) | Status | Reason |
|---|---|---|
| `list_toolboxes`, `open_toolbox`, `close_toolbox` | out of scope | Only needed to keep 247 tools under client caps |

## Specific comparisons requested by the issue

### Symbol and footprint generation

Upstream (`python/commands/symbol_creator.py`, `footprint.py`) takes explicit
JSON (pins with positions/types, rectangles, polylines; pads, courtyards)
and writes a `.kicad_sym` / `.kicad_mod` block, plus library-table
registration. It does not know package standards.

Ours (`kct footprint generate`, `library/generators/`) is *parametric and
standards-based* (IPC-style families by pitch/pin count) and
`datasheet/symbol_generator.py` derives symbols from datasheet pin data.
We lack upstream's generic "write this symbol into that library and register
the library" plumbing, and its per-pad editing. Both are cheap and would let
an agent finish a part end to end through MCP. MIT, so code is reusable with
attribution, but our generators are the better source of geometry.

### JLCPCB parts lookup

Upstream keeps a local SQLite+FTS5 database of the JLCPCB catalog (imported
from the official parts export or the jlcsearch API), with package→footprint
mapping and Basic/Extended-aware alternative suggestion. Ours has an LCSC
client and an offline `yaqwsx/jlcparts` mirror, and gates BOMs
(`validate_assembly_bom`). Missing: Basic-vs-Extended-aware
**alternatives suggestion** and exposing parts search as an MCP tool.

### Freerouting integration

Upstream's `freerouting.py` exports DSN, runs Freerouting (local JAR with
Java-version detection, or a Docker image), retries with varied `-mp` pass
counts and keeps the best-scoring SES, then imports it with net-name
reconciliation and protects `.kicad_pro` settings. We export DSN
(`export/dsn.py`) and import SES (`export/ses.py`) and use Freerouting as a
benchmark competitor, but never *run* it from the product. Closing that gap
makes Freerouting a selectable fallback behind `route_net_auto`. It is the
most valuable idea here; the multi-attempt scoring and SES net-name
reconciliation are worth reusing.

## Follow-up issues

Filed via `create-issue.sh`, one per gap:

| Issue | Gap | Upstream code |
|-------|-----|---------------|
| #7991 | Run Freerouting from kct as a fallback router | reusable under MIT with attribution |
| #7992 | Basic-library JLCPCB alternatives suggestion | idea only (package map reusable with attribution) |
| #7993 | Parts search/lookup as MCP tools | idea only |
| #7995 | Symbol library write and registration helpers | reusable under MIT with attribution |
| #7996 | Schematic cosmetic lint and declutter | reusable under MIT with attribution |

## Conclusion

Do not chase parity: roughly half of upstream's tools are GUI/IPC or
file-surgery calls our session model deliberately avoids, and another large
share exist in the `kct` CLI/API without an MCP wrapper (a separate question
from this evaluation). Verdict **`complementary`**; re-evaluate when upstream
changes its tool architecture or a follow-up lands.
