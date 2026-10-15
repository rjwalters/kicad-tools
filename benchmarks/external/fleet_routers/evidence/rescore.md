# Independent re-score (Issue #6335)

Raw router outputs were copied (outside the driver) beside the **frozen** `.kicad_pro` and
`<stem>_routed.kicad_dru` from the committed board directory, then graded with
`kicad-cli pcb drc --refill-zones --severity-all --all-track-errors` 10.0.6. Rule hashes (first 16 hex)
are the manifest's: `.kicad_pro` / `.kicad_dru`.

| Row | Rules `.kicad_pro` / `.kicad_dru` | Re-scored errors | Unconnected | Warnings | `results.json` agrees |
|---|---|---|---|---|---|
| 00 kct (simple) | `3caf1e78675ae98e` / `b1be95e9fb56ff82` | 0 | 0 | 3 | yes |
| 03 tracemaker (dense, USB/diff-pair) | `09c8746ffab5b044` / `c44d93c7564deb55` | 1 (`starved_thermal`) | 0 | 36 | yes |
| 06 fastroute (diff-pair, non-comparable) | `48e2bf25ed4c9aa0` / `825b4a72d6415426` | 20 (`skew_out_of_range` 4, `solder_mask_bridge` 8, `shorting_items` 8) | 8 | 0 | yes |
