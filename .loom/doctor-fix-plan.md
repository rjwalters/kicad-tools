PR #2716 doctor fix plan (judge feedback 2026-05-11)
====================================================

Two blockers identified by judge:

Blocker 1 — CI Docker image is kicad/kicad:9.0, cannot read new format
-----------------------------------------------------------------------
File: .github/workflows/ci.yml
- Line 163, 165: comment about "kicad/kicad:9.0 Docker image"
- Line 172: `image: kicad/kicad:9.0` → bump to `kicad/kicad:10.0`
- Line 179: comment "kicad/kicad:9.0 image links /bin/sh to dash"
- Line 196-205: sanity-check that kicad-cli is 9.x → must check for 10.x
- Line 203: error message "kicad/kicad:9.0 container does not provide kicad-cli 9.x"

Verified `kicad/kicad:10.0` tag exists on Docker Hub (also 10.0-amd64, 10.0.2, etc.)

Fix: bump image tag and version assertion to 10.

Blocker 2 — Board 01 routed PCB lost its routing in regen
----------------------------------------------------------
File: boards/01-voltage-divider/output/voltage_divider_routed.kicad_pcb
- Current PR HEAD: 0 segments, 0 vias (byte-identical to placed file)
- main: 9 segments

Safer fix (judge option b, same as board 05): take main's content,
edit header only (version 20240108 → 20260206, generator_version 8.0 → 10.0).
Preserves 9-segment MFG-READY routing while staying in new format.

Acceptance criteria
-------------------
- CI `kicad-cli Round-trip Smoke` passes
- Board 01 routed PCB has 9 segments + DRC clean
- All other changes (constants, footprints, tests, boards 02/05/06) remain as-is
