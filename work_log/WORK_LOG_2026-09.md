# Work Log Archive — 2026-09

Sections moved byte-for-byte from [WORK_LOG.md](../WORK_LOG.md) by `loom-daemon work-docs archive`.

### 2026-09-10

- **PR #5104**: docs: audit Konnect item 8 natural-language design-rule store, decline
- **PR #5099**: ci: temporarily suspend dedicated Board 07 gates
- **Issue #5097** (closed): Temporarily suspend dedicated Board 07 CI gates
- **Issue #5057** (closed): pcb: added footprints lack unique persistent pad UUIDs for native DRC correlation
- **Issue #5053** (closed): check_mypy_baseline.py fails on main with 3 errors beyond the baseline
- **Issue #5049** (closed): pcb: newly added footprint pad mutations are silently omitted by save()
- **Issue #5047** (closed): board05: R10-R12 current-sense shunt and U1 buck regulator LCSC assignments don't match required parts
- **Issue #5046** (closed): board03: U1 MCU footprint mismatch — TQFP-32 7x7mm required, C44854 is a QFP-44 10x10mm ATMEGA32U4-AU
- **PR #5045**: fix(ci): restore demo-board integration checks after redesign
- **Issue #5044** (closed): main CI broadly red as of 2026-09-10 (d95b6eff): board readiness status='partial', new ruff-format drift, cascading E2E failures
- **PR #5040**: fix(parts): exclude unpopulated symbols from sourcing preflight
- **PR #5038**: fix(cli): handle suppressed format defaults in help
- **Issue #5028** (closed): parts CLI crashes on Python 3.14: SUPPRESS default interpolated in format help
- **PR #5026**: fix(router): count only fully routed nets in negotiated progress line
- **Issue #5023** (closed): export/check: preserve reviewed native rule floors to keep exported plane copper reproducible
- **Issue #5022** (closed): ci: synthetic routing recipes can check or overwrite assembled demo outputs
- **Issue #5019** (closed): pcb-modify flip reports success while leaving pads on front layers; modern references are not found
- **Issue #5018** (closed): Stripline geometry treats intervening signal copper as a reference plane and reverses upper/lower gaps
- **Issue #5017** (closed): PCB stackup parser drops native addsublayer dielectric strata and material properties
- **Issue #5016** (closed): physics: asymmetric stripline formula overestimates a verified six-layer geometry by about 42 percent
- **Issue #5012** (closed): via-in-pad DRC misses drill holes that partially cut across SMT soldering lands
- **Issue #5011** (closed): board04: schematic mirrors invalid AMS1117 power pin mapping, allowing false clean LVS
- **Issue #5007** (closed): check: copper-pour keepout rule areas falsely report disabled fill and missing net
- **Issue #5005** (closed): Off-grid pad-edge A* seeds ignore trace radius and repeat invalid escape tails
- **PR #5003**: chore(deps): bump @vitest/mocker and vitest in /site
- **Issue #5002** (closed): Router impedance synthesis ignores explicit PCB stackup and blocks fine-pitch escapes
- **Issue #5001** (closed): stitch: inner-plane targets create partial-stack spans on standard through vias
- **Issue #4998** (closed): JLCPCB profiles need layer-specific PTH annular-ring floors separate from vias
- **Issue #4997** (closed): physics: bottom microstrip reads exterior paste instead of inward substrate
- **Issue #4996** (closed): check: exact-minimum PTH annular rings fail due to floating-point rounding
- **Issue #4993** (closed): board05: DRV8301 power/charge-pump topology contains electrical shorts and unrealizable power footprints
- **Issue #4992** (closed): LVS: round header pad bounding boxes create false shorts across plane antipads
- **Issue #4990** (closed): parts suggest preflight counts BOM-excluded symbols as missing LCSC parts
- **Issue #4988** (closed): route: main CLI rejects supported --no-auto-pour option
- **Issue #4976** (closed): label LVS loses pad bindings after KiCad 10 saves name-only net syntax
- **Issue #4975** (closed): router: size final diffpair trombone to deficit instead of always adding a full loop
- **Issue #4974** (closed): Export includes through-hole parts in SMT CPL when footprint assembly attributes are absent
- **Issue #4972** (closed): route: cache ignores net-class sidecar and coupled-routing mode, replays invalid copper
- **Issue #4970** (closed): Gallery: require current manufacturing evidence for Ready and distinguish Gerbers from KiCad project downloads
- **Issue #4967** (closed): router progress counts empty and partial routes as completed nets
- **PR #4965**: chore(deps): bump astro from 7.2.1 to 7.2.8 in /site
- **PR #4964**: chore(deps): bump svgo from 4.0.2 to 4.1.0 in /site
- **PR #4961**: chore(deps): bump the python-minor-patch group across 1 directory with 9 updates
- **PR #4960**: chore(deps-dev): bump fast-uri from 3.1.5 to 3.1.7 in /site
- **PR #4958**: chore(deps-dev): bump the site-minor-patch group across 1 directory with 2 updates
- **Issue #4927** (closed): Lint & Format CI check fails on main: 58 pre-existing UP038 ruff errors
- **PR #4924**: chore(deps): bump rich from 14.2.0 to 15.0.0
- **PR #4921**: chore(deps): bump weasyprint from 68.1 to 69.0
- **PR #4918**: chore(deps): bump astral-sh/setup-uv from 9.0.0 to 10.0.1
- **Issue #4906** (closed): Explore Konnect item 12: IPC-API edit patterns as mocked-IPC test-harness reference
- **Issue #4904** (closed): Explore Konnect item 10: manufacturing pre-flight shape + offline JLCPCB catalog vs kct:tapeout
- **Issue #4902** (closed): Explore Konnect item 8: layered config with natural-language design rules vs manufacturer profiles/recipes
- **PR #4835**: chore(deps): bump nanoid from 3.3.12 to 3.3.18 in /site

### 2026-09-06

- **Issue #4954** (closed): mypy 2.x bump surfaces ~50 new type errors (implicit-Optional + missing generic args)


