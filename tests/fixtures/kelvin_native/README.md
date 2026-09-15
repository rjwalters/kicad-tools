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
