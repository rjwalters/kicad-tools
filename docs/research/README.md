# Research Notes Index

Point-in-time working documents; dated entries describe the tree as of their date.
Every note in this folder is linked below (a test enforces that, Issue #6947).

## Ecosystem evaluations

Notes about neighbouring projects. The authoritative list, with verdicts and
licence-compatibility, is the ecosystem registry: `kct ecosystem list`,
`kct ecosystem show <name>`, `kct ecosystem where-we-sit` (narrative:
[`../ecosystem.md`](../ecosystem.md)).

| Note | Description |
|------|-------------|
| [ai-circuit-design-tests](ai-circuit-design-tests-evaluation.md) | Should kicad-tools be offered as a tool under test in devbisme/ai-circuit-design-tests |
| [Backplane and KiStack](backplane-kistack-evaluation.md) | Evaluation of i2cjak Backplane and American Embedded KiStack |
| [circuit-skills](circuit-skills-evaluation.md) | punkfab circuit-skills as a source of field-tested PCB lessons |
| [component-importer-for-kicad](component-importer-for-kicad-evaluation.md) | Vendor-ZIP ingestion backend evaluation |
| [copperhead](copperhead-workflow-ideas.md) | Adoptable workflow ideas from the copperhead AI PCB agent |
| [Diode `pcb`](diode-pcb-comparison.md) | Diode (Zener) vs kicad-tools feature comparison |
| [KiCadRoutingTools (KRT)](kicad-routing-tools-comparison.md) | KRT vs kicad-tools comparison and benchmark |
| [kipy IPC API](kipy-ipc-api-evaluation.md) | Official KiCad Python API as a parser cross-check |
| [OmniLayout / OmniRouting](omnilayout-recon.md) | External LLM layout/routing benchmark recon |
| [RepoRecon KiCad AI sweep](reporecon-kicad-ai-sweep.md) | Sweep of KiCad AI repositories |
| [tscircuit](tscircuit-evaluation.md) | tscircuit as an upstream producer and peer autorouter |

### Atopile / Faebryk

| Note | Description |
|------|-------------|
| [Research synthesis](atopile-research-synthesis.md) | Best Atopile ideas mapped onto kicad-tools |
| [Constraint solving](atopile-constraint-solving.md) | Constraint solving and parametric part selection |
| [Error handling](atopile-error-handling.md) | Rich diagnostics and error handling |
| [Interface patterns](atopile-interface-patterns.md) | Interface-based design patterns |
| [Layout reuse](atopile-layout-reuse.md) | Layout reuse and sync |
| [LSP server](atopile-lsp-analysis.md) | LSP server analysis for IDE integration |
| [MCP server](atopile-mcp-analysis.md) | Atopile MCP server analysis |
| [Package registry](atopile-package-registry.md) | Package registry and dependency management |
| [Faebryk component library](faebryk-component-library.md) | Faebryk component-library research |

### Konnect audits

| Note | Description |
|------|-------------|
| [Connectivity audit](konnect-connectivity-audit.md) | Konnect schematic connectivity primitives vs our ERC/LVS/net-status surface |
| [Truth-contract audit](konnect-truth-contract-audit.md) | Truth-contract audit of `kct mcp` tools and `kct check` |
| [v0.8 to v0.13 delta](konnect-v013-delta.md) | What changed in Konnect since the original audit |

## Router experiments

Measured experiments on `kct route`; several record negative results.

| Note | Description |
|------|-------------|
| [Escape-flow assignment](kct-route-escape-flow-experiment.md) | Joint min-cost-flow escape assignment: no gain |
| [Net ordering](kct-route-net-order-experiment.md) | Crossing-aware net ordering: no gain |
| [Nogood cache](kct-route-nogood-cache-experiment.md) | Validity-aware nogood cache for negotiated retries |
| [Phase profile](kct-route-phase-profile.md) | Where `kct route` time goes on boards 02 and 03 |
| [GPU / batched routing design](gpu-routing-design.md) | Profile-first GPU routing go/no-go: don't build (Amdahl ceilings below 2x) |
| [Pad-access expansion reassessment](pad-access-expansion-reassessment.md) | Epic #5508 Phase 4 (lattice/mesh) gate: defer |
| [Rip-up](kct-route-ripup-experiment.md) | KRT-style sequential N+1 rip-up: no gain |
| [Expand, route, shrink](expand-route-shrink-experiment.md) | Routing an expanded placement then shrinking it back |
| [Diff-pair bench, phase 0](diffpair-bench-phase0.md) | Diff-pair benchmark baseline |
| [Diff-pair primitive pairing spec](diffpair-primitive-pairing-spec.md) | Black-box, probe-only spec of how kicad-cli pairs P/N primitives into coupled length (Issue #7565) |
| [Tuning-pattern uncoupled exclusion](kicad-tuning-pattern-uncoupled-exclusion.md) | Black-box probes: a KiCad tuning pattern drops the bump from the uncoupled figure but not the partner beside it, unless that partner span is a member too (Issue #7632, Q31) |
| [Diff-pair skew compensation](diffpair-skew-compensation.md) | Sourced skew limits and uncoupled-length budgets for the diff-pair skew fixer |
| [Bundle crossing report](bundle-crossing-report.md) | Measured hit and false-alarm counts |
| [Pad-budget via footprint](pad-budget-via-footprint-6415.md) | Via-footprint charging on board 05 |
| [`--reserve-pour-only-passives` cross-board](reserve-pour-only-passives-cross-board.md) | Flag off/on on boards 03-07: closes board 05's last 2 pour links and changes no other board's outcome; the note recommends flipping the default with an opt-out (Issue #7794) |
| [Board 05 unattended route](board-05-unattended-route.md) | `kct route` from scratch at seeds 42/7/1337: 37/37, both DRC engines 0 errors, LVS clean, byte-identical across seeds; which wall clocks stay live; the nightly gate (Issue #7834) |
| [kct check vs kicad-cli lengths](kicad-length-bench-6725.md) | Per-net length and pair skew comparison |

## Calibration notes

| Note | Description |
|------|-------------|
| [FOM weight calibration](fom_calibration.md) | Hybrid figure-of-merit weight calibration |
| [Learned FOM, phase 0](learned_fom_phase0.md) | Learned residual feasibility study |
| [Placement score calibration](placement_score_calibration.md) | Pre-route placement signals vs routing outcomes |
| [Corpus benchmark feasibility](corpus-benchmark-feasibility.md) | Capacity calibration and route-vs-human cases |

## Audits

| Note | Description |
|------|-------------|
| [Grid pad rotation](grid-pad-rotation.md) | Grid pad rotation acceptance |
| [Grid pad shape](grid-pad-shape.md) | Router pad shape coverage |
| [Router pad rotation audit](router-pad-rotation-audit.md) | Router pad rotation consumer audit |
| [MCP clearance and grid audit](mcp-clearance-grid-audit.md) | MCP clearance and router grid audit |
| [Clearance-kernel acceptance audit](clearance-kernel-acceptance-audit.md) | Epic #5509 criterion-to-evidence reconciliation (#7577) |
| [Connectivity contact paths](connectivity-contact-paths-5133.md) | Existing physical contact paths in the Board06 extractor |
| [Copper LVS zone suppression](copper-lvs-zone-suppression.md) | Independent Board05 evidence |
| [3D model coverage](3d-model-coverage.md) | Per-board 3D body coverage, model licensing and gate interactions, plus the plan for the JLCPCB catalog |

## Adding a note

1. Add `docs/research/<topic>.md` and date it; say how each source was obtained.
2. Add one line (title plus a one-sentence summary) to the matching table above.
   `tests/test_docs_reachability.py` fails if a note is not listed here.
3. If the note evaluates a neighbouring project, also add or update its entry in
   the ecosystem registry (`src/kicad_tools/ecosystem/data/projects.toml`, then
   `uv run python scripts/ecosystem_render.py --write`).
