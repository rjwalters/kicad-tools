# Kelvin escaped-shunt native evidence

This archive records four independently constructed four-pad fixtures routed
with Python/C++ negotiated routing, in collinear and separated configurations.
Each includes an unchanged shunt escape from (4, 10) to (6, 10). New branches
must tap the physical shunt at (4, 10), without sharing that escape outside
the 0.8 mm square shunt pad.

## Results and scope

All four saved boards and their native-refilled copies have four pads in one
connected component, zero unconnected items and zero errors under KiCad 10.0.5.
Each audit retains one dangling-track warning for the preserved stub, four
footprint-library warnings and four text-height warnings. The producer records
zero shared full-width branch copper area outside the shunt pad, including
the preserved stub. C++ generation asserts zero Python fallback.

These are fixture results, not dense-board completion evidence. Phase-level
baseline, completion and rescue acceptance remains in issue #5398.

## Provenance

`manifest.json` contains the archive hash and every member's SHA-256.
`source-escaped-root-manifest.json` inside the archive binds source and native
extension hashes to pre-rebase commit `dd28ca1f`. `source-snapshot/` retains
the measured routing modules and topology test, because the branch was later
rebased onto `5fd8aeff`. The compiled macOS extension itself is not included;
its hash and build version 26 are recorded. Rebuild native extensions on the
platform used for reproduction.

Native audit image:
`sha256:0d6887c861dd9926a02cdb57e3d649b72fc547ff2aef6605c5416131794d2115`.
Saved/refilled PCBs, DRC JSON, native component censuses, producer metadata and
the generation/audit scripts are retained without filtering findings.

## Reproduction

Extract into a temporary directory. From an installed checkout, run
`python generate_escaped_root.py NEW_OUTPUT_DIRECTORY`; the script requires
both Python and a rebuilt C++ backend. The repository regression suite is:

```sh
uv run pytest tests/router/test_kelvin_physical_topology.py --no-cov -q
```

The retained `audit_escaped_root.py` expects its working directory mounted at
`/fixtures` and `native_components.py` at `/evidence/native_components.py`, in
an environment with KiCad 10.0.5 CLI and its Python `pcbnew` bindings. Place
new producer output at `/fixtures/after-escaped-root`; use a fresh directory
without `/fixtures/native-escaped-root` so existing evidence is not overwritten.
Run `/usr/bin/python3 /fixtures/audit_escaped_root.py`. This performs both saved
and `--refill-zones --save-board` native audits and checks that producer inputs
remain unchanged.

## Isolated historical-board comparison

`isolated-isense-evidence.tar.gz` retains a separate supported-CLI experiment
using `--nets ISENSE_B-` on the historical 210-pad DRV8301 stress board. Its
member hashes are in `isolated-isense-manifest.json`. The input, commands,
source and native-extension hashes, logs, saved/refilled boards, project/rules
files and all native findings are included.

Baseline `58ee27d6` exits 1 and leaves target components of sizes 2, 1 and 1.
Patched `1c7ab6da` exits 0; KiCad 10.0.5 finds all four target pads connected
with zero DRC errors, both saved and refilled. Other nets remain incomplete:
the patched saved board has 125 unconnected items and the refilled board 120.
Warnings remain in the unfiltered reports. The failed baseline does not emit
the project/rules files produced by the successful patched run, so the DRC
contexts differ.

`topology.py` unions full-width track and pad copper by layer, removes the
actual rounded R11.2 shunt pad on F.Cu, and joins layers through physical vias.
Both patched boards leave U3.31, U3.39 and U10.6 in three separate components.
The negative control adds a bridge between U3.31 and U3.39 and correctly joins
those two components. The checker asserts that this net has no zones or arcs
and uses polygon approximations for circular edges. Run it from an installed
checkout against `native2/patched/saved/routed.kicad_pcb` or the corresponding
`refilled` path; add `--negative-control` to exercise the bridge control.

This isolated command generates three escapes, whereas the full-board command
generates 52. It does not establish full-board completion or phase acceptance.
The historical design is an electrically unsuitable stress fixture (see #4993),
not a manufacturing candidate. These results also predate the original-pad
obstacle and sense-impedance corrections in later commits of this PR.

The retained `audit2.py` audits disposable copies and verifies producer hashes
afterward. An earlier discarded audit triggered its directory hash guard when
KiCad created a `.kicad_prl` file; the source PCB was unchanged. The retained
audit's `before.json` includes that auxiliary file and its guard passed.
