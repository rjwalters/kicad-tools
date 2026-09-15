# Generic physical stitching controls

These small, synthetic three-pad boards retain native KiCad 10.0.6 filled copper.
They are independent of Board05's circuit, placement and reviewed routing.
`continuous.kicad_pcb` has one GNDA plane and one pad needing a via;
`split.kicad_pcb` has separated GNDA islands. Both use actual traces, plated vias,
SMD pads and filled zones, rather than matching net labels as connectivity proof.

Native reproduction at main `7368bf597eaefb2dbe171f79db3c3216843a2ae6`:

| Fixture | Native opens before geometric stitching | After stitching/refill | Vias added |
|---|---:|---:|---:|
| continuous | 1 | 0 | 1 |
| split | 2 | 1 | 2 |

The second result is geometric progress, but is not physical power completion.
`tests/test_stitch_physical_completion.py` calls the shared production stage with
these boards and native KiCad 10. It verifies accepted bytes, pad bonds, native
reports, and exact rollback. Variations exercise inner-layer planes, foreign
pad/segment/via/zone contact, authored drill constraints, refill-only completion,
untargeted orphan copper, and project-local library context. Tests skip explicitly
when KiCad 10 is unavailable; a skip is not a native acceptance measurement.

The intentionally minimal `Test` footprint library is not installed. Existing
library-availability warnings remain visible in native evidence; completion
requires no new findings, while `strict_drc=True` requires no findings at all.

Reproduce the generic controls with:

```sh
uv run pytest tests/test_stitch_physical_completion.py --no-cov -q
uv run python boards/05-bldc-motor-controller/design.py /tmp/board05-physical-stitch
```

Each native test retains its temporary-stage boards, native report JSON and
command logs under pytest's temporary directory. For an individual user board:

```sh
kct stitch board.kicad_pcb --net GND --complete --via-size 0.6 --drill 0.3 --evidence-dir new-stitch-evidence
```

The input remains in its original project context. The public API returns only
physically accepted results; ordinary `kct stitch` keeps its geometric scope.
