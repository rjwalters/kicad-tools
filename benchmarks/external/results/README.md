# Committed zero-touch benchmark results

Epic #4932 Phase 2, issue #4942. These are the JSON + markdown artifacts
`kct bench external` produced for the two boards Phase 2 covers beyond
STRF (#4941's own smoke-tested fixture run): **PocketBeagle** and
**BeagleConnect Freedom**. Schema: `docs/benchmark-external-report-schema.md`.

| File | What it is |
|---|---|
| `pocketbeagle.zero-touch.json` | Full schema-v1 report for `pocketbeagle` |
| `beagleconnect_freedom.zero-touch.json` | Full schema-v1 report for `beagleconnect_freedom` |
| `report.md` | The combined markdown table `kct bench external` renders from both reports (exact tool output, unedited) |

Regenerate with (see the top-level `CLAUDE.md` — build the C++ router
backend first, `uv run kct build-native`, or timing is refused):

```bash
uv run kct bench external \
  --board pocketbeagle --board beagleconnect_freedom \
  --output-dir /tmp/bench-out   # a scratch dir; copy the .json/report.md here manually
```

`kct bench external`'s own `--output-dir` also writes `normalized/` and
`routed/` subdirectories containing full copies of the (third-party,
separately licensed) board files — those are **never** committed here,
matching `benchmarks/external/README.md`'s no-vendoring policy. Only the
measurement artifacts (JSON reports + the rendered markdown table) are
tracked.

## Result: both boards refuse to route zero-touch (0 tracks placed)

Neither board produced a single track under the zero-touch protocol
(rules as shipped, no tuning, `--seed 42`) — in both cases the router's
own pre-route safety gates refused before laying any copper, rather than
producing an unsafe or partial result:

- **`pocketbeagle`**: the auto-grid resolver's off-grid-escalation path
  blows its memory-cell budget on the board's two large low-density
  through-hole headers (P1/P2) and the router safely refuses to route at
  all (`Auto-grid selected 0.127mm > clearance/2 ... rejects this grid`).
  Filed as **#4945**.
- **`beagleconnect_freedom`**: one footprint (`SH1`, an RF shielding-can
  land pattern with real GND-net pads) sits outside `Edge.Cuts` in the
  pinned board, and the placement preflight gate aborts the *entire*
  route rather than routing the other ~455 connections and reporting
  just that footprint's nets as unresolved. Filed as **#4946**.

The `completion_pct` figures in the JSON reports (31.8% / 32.6%) are
**not** router output — they are connections that were already trivially
satisfied pre-route (single-pad nets, zone-stitched nets) in the
ripped-up board. `copper.via_count` and `copper.wirelength_mm` are both
`0` for both boards, confirming the router placed no copper.

Per this issue's explicit convention, these are filed as **generic
router-capability issues**, not board-specific patches — the pinned board
files are not modified or special-cased anywhere in this repo.

## License notes (Phase 1 convention)

Both boards' licenses are recorded in `benchmarks/external/boards.toml`
(the `license` field, dated verification comment) per Phase 1's
fetch-at-runtime + license-recording convention:

- **PocketBeagle** (`github.com/beagleboard/pocketbeagle`): CC-BY-4.0
  (repo root `LICENSE`, verified 2026-08-24).
- **BeagleConnect Freedom** (`git.beagleboard.org/beagleconnect/freedom`):
  CC-BY-4.0 (repo root `LICENSE`, verified 2026-08-24).

Neither board's `.kicad_pcb` is vendored into this repo — `fetch_boards.py`
downloads both at their pinned commits into a gitignored cache directory
at run time (see `benchmarks/external/README.md`).
