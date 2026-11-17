# Truth-contract audit: `kct mcp` tools and `kct check` (issue #6913)

Konnect's "truth contract" says: every response field comes from an observed
result (never echoed from the request), a verdict with no evidence is
INCOMPLETE, and a safety check that cannot run is BLOCKED rather than passed.
Ideas only: Konnect is AGPL-3.0 and nothing here is copied from it (see
[konnect-connectivity-audit.md](konnect-connectivity-audit.md)).

We map Konnect's BLOCKED onto the vocabulary we already have: `NOT RUN` per
sub-check, rolling up to `INCOMPLETE` (exit 2) in `kct check`, and
`coverage[].status == "incomplete"` in `detect_mistakes`. No new status was added.

Method: a read-through of each handler and the result fields it builds, as of
`afbac285d` (2026-10-17). Nothing was fuzzed. Verdicts:
**observed**, **echoes request** (field is the caller's input, by design or not),
**success without evidence** (a pass/ready/success flag reachable with nothing checked).

## `kct check` output fields

| Field | Verdict | Where |
|---|---|---|
| `drc` | observed (pure-Python DRC over the loaded board) | `cli/check_cmd.py` `run_meta_checks` |
| `erc` | observed; kicad-cli absent -> `NOT RUN`, load/parse failure -> `FAILED` | `check_cmd.py:611-632` |
| `lvs` | observed; no schematic -> `NOT RUN`; label-leg vacuity reported as `vacuous` | `check_cmd.py:675-720` |
| `manifest` | observed; missing bundle -> `NOT RUN` | `check_cmd.py:874-905` |
| `overall` | derived: `FAILED` > `INCOMPLETE` (any `NOT RUN`) > `PASSED`; exit 2 on INCOMPLETE unless `--allow-incomplete` | `check_cmd.py:249-272` |

No gap found. Missing gap-closing test added: with `find_kicad_cli -> None` and a
board+schematic+manifest present, `ERC: NOT RUN`, `Overall: INCOMPLETE`, exit 2
(`tests/test_cli_check.py::TestCheckMetaCheck::test_meta_check_kicad_cli_absent_is_incomplete_not_passed`).
Only ERC needs kicad-cli; DRC, LVS and manifest are pure Python.

## `kct mcp` tools

| Tool(s) | Verdict | Notes |
|---|---|---|
| `validate_assembly_bom` | **success without evidence** (fixed) | Empty or all-DNP BOM gave `assembly_ready: true` from `any([])`. Now `assembly_ready: false` plus a warning (`mcp/tools/export.py:98-106`). The library property keeps its vacuous-true behaviour (pinned by `tests/test_assembly_validation.py`); only the agent-facing result changed. |
| `validate_pattern` (decoupling) | **success without evidence** (fixed) | `capacitors=[]` validated nothing and returned `passed: true`. Now a `no_capacitors` error (`patterns/validation.py:451-462`). Missing refs already yield `component_exists` errors. |
| `export_gerbers` | **success without evidence** (fixed) | Listed every file in `output_dir`, including stale ones from earlier runs, and reported `success` with zero copper layers. Now lists only files written by this call and fails when no copper Gerber was produced (`export.py:~205`, `:240`). |
| `export_assembly` | **success without evidence** (fixed) | Gerber sub-result was `success=True` whenever a path came back, even with no copper layers. Now `success=bool(copper_layers)` with an error. The stale-file listing in this path is not changed. |
| `export_bom` | observed | Rows come from the parsed BOM; `missing_lcsc` is computed per group. |
| `detect_mistakes`, `list_mistake_categories` | observed | `coverage` / `coverage_complete` already report checks that could not run (issue #4899). |
| `route_net`, `route_net_auto` | observed | Success is refused when no segments/vias were produced (issue #2913, `routing.py:~944`). |
| `get_unrouted_nets`, `board_summary`, `board_inspect`, `measure_clearance` | observed | Values computed from the loaded PCB; `get_unrouted_nets` takes status from `NetStatusAnalyzer`. |
| `screenshot_board`, `board_view`, `screenshot_schematic` | observed | kicad-cli absent returns an error dict (`screenshot.py:206`, `:419`, `:558`), never a blank image. `board_view` is matplotlib-based. |
| `placement_*`, `optimize_placement`, `evaluate_placement`, `resolve_placement_overlaps` | observed | Scores and positions recomputed from the session/optimizer. |
| `start_session`, `query_move`, `apply_move`, `undo_move`, `commit_session`, `rollback_session`, checkpoints, summaries | observed | Counts and scores recomputed from session state (e.g. `session.py:~689`). |
| `declare_interface`, `declare_power_rail`, `record_decision`, `annotate_decision`, `clear_intent`, `list_intents`, `get_*context/history/intent` | echoes request, by design | These store what the caller declared and return it; the response does not claim verification. |
| `adapt_pattern`, `get_component_requirements`, `list_pattern_components` | observed | Read from the requirements database; an unknown MPN is not invented. |
| `create_pcb_from_schematic`, `get_recent_calls`, `ecosystem_list`, `ecosystem_show` | not traced in depth | Read-level only; no vacuous pass seen. |

## Not fixed / follow-ups

- Depth limit: the observed rows are a read-through, not an exhaustive trace of
  every field in every result dataclass.
- `export_assembly`'s stale-file listing (same cause as `export_gerbers`) is left
  as is: it is a different code path and the fix touches `AssemblyPackage`.
