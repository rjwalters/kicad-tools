# Final four host-bus nets

These are diagnostic inputs/logs, not release artifacts. Both sources use the
same placement and completed power/support copper. SCL, SDA, MON_ALERT and
PD_ALERT remain open; the refilled name-only source has zero native violations
and ten unconnected items. The command JSON files record exact router arguments.
Copy the canonical output project, schematic and local libraries into a scratch
directory and install the chosen source as `usbc_pd_power.kicad_pcb` to reproduce.
Adapt only scratch paths in the saved commands; use the same project basename.

- Native KiCad's refilled name-only source: router recognizes zero target nets
  and incorrectly reports success. Known bug #4983.
- Numbered source, 0.1 mm grid: zero of four nets complete, 29.1 seconds.
- Numbered source, 0.05 mm grid: zero of four nets complete, 99.6 seconds.
  Both strict outer-layer experiments only partially connect MON_ALERT.

The canonical generated PCB now contains a manual route completing all four
nets on this placement, using outer layers only; native refill gives zero
violations and zero opens. It is a feasible comparison for improving the
router's obstacle escape and multi-terminal routing. See the parent engineering
review for thermal and procurement gates still blocking manufacture.

Host-bus benchmark request: [#5072](https://github.com/rjwalters/kicad-tools/issues/5072).
