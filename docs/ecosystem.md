# Where kicad-tools sits

kicad-tools is one tool in a crowded, fast-moving KiCad automation ecosystem.
This document is the positioning narrative: what makes this project distinct,
what it deliberately does not do, who the neighbours are, and the rules we
follow when we study them.

The machine-readable version of everything below is the registry at
`src/kicad_tools/ecosystem/data/projects.toml`. Query it rather than grepping
this file:

```bash
kct ecosystem where-we-sit              # invariants, non-goals, neighbour map
kct ecosystem list --relation upstream  # who produces files we consume
kct ecosystem show kicadroutingtools    # one project, with our verdict
```

Agents reach the same data through the `ecosystem_list` and `ecosystem_show`
MCP tools. Issue #5839 has the design rationale.

## Our invariants

These are the properties that make kicad-tools a distinct tool rather than a
reimplementation of a neighbour. They are recorded as data in the registry's
`[positioning]` table, so this list and `kct ecosystem where-we-sit` cannot
drift apart.

1. **Offline and file-based.** Every tool reads and writes `.kicad_sch` /
   `.kicad_pcb` directly. No running KiCad instance, no IPC, no GUI, no cloud
   service. This is the single constraint that most often decides whether a
   neighbour's approach is adoptable here at all — it is why
   [kipy](https://gitlab.com/kicad/code/kicad-python) was evaluated and
   declined (see [research/kipy-ipc-api-evaluation.md](research/kipy-ipc-api-evaluation.md)).
2. **Machine-readable by default.** Analysis and query commands emit JSON via
   `--format json`, because the primary caller is an agent, not a human. See
   [reference/machine-output.md](reference/machine-output.md) for the contract.
3. **Verification is two-engine.** `kct check --mfr` and `kicad-cli pcb drc
   --refill-zones` must *both* report zero errors. Neither alone is sign-off;
   they disagree, and the disagreements are where real defects hide.
4. **Electrical correctness is gated by LVS**, not by DRC alone: emitted copper
   is compared against the schematic netlist, so a board that routes cleanly
   but connects the wrong pins fails.
5. **"Done" means 100% of nets routed, 0 DRC violations, 0 sync drift.** Not a
   partial-reach percentage. A board at 86% is not 86% shippable; it is not
   shippable.

## What we deliberately do not do

A non-goal with no owner is a gap, not a non-goal — so each of these names who
does it instead.

| We do not | Who does |
|---|---|
| Ship a GUI or fork KiCad | Zeo, Konnect |
| Provide a schematic-capture language | atopile, SKiDL |
| Run routing as a cloud service | DeepPCB (at the cost of offline/air-gapped CI) |
| Round-trip through Specctra DSN/SES | freerouting, AutoRoute |
| Compete on breadth of fabrication outputs | KiBot |

The DSN/SES row is the one worth expanding, because it looks like a gap and is
not. A DSN round-trip routes a *translated view* of the board. We stay on the
`.kicad_pcb` itself so that LVS, `kct check --mfr` and `kicad-cli` all see the
exact artifact that gets fabricated. Giving that up would mean giving up
invariants 3 and 4.

## The neighbour map

Four relations, recorded per project in the registry:

- **upstream** — produces files we consume. A design-as-code tool emits a
  netlist and a rough layout; we route, verify and package it. atopile and
  SKiDL are the two today, and neither routes, which is precisely why the
  relationship is complementary rather than competitive.
- **peer** — overlaps some part of our own surface. Most of the registry.
  Overlap is not duplication: KiCadRoutingTools beats us on sparse and medium
  boards and shorts nets on dense ones, so "who is better" is a
  per-board-class question with a measured answer, not a slogan.
- **downstream** — consumes what we emit. None recorded yet.
- **reference** — studied, but in no pipeline with us. OmniLayout's evaluation
  protocol is the clearest example: we adopted its metric vocabulary and
  declined its unlicensed dataset.

## License rules (load-bearing)

`license_compat` in the registry is the field with legal weight, and the
vocabulary is defined by `LICENSE_COMPAT` in
`src/kicad_tools/ecosystem/models.py`. kicad-tools is MIT. Therefore:

| `license_compat` | Meaning for this repo |
|---|---|
| `mit-clean` | MIT/BSD/ISC. Code may move, with attribution. |
| `permissive-ideas-only` | Apache-2.0 and similar. One-way compatible in principle; our standing decision is ideas-only, to keep this tree uniformly MIT. |
| `copyleft-ideas-only` | GPL/AGPL. Capability-level engagement only — **no code in either direction**. |
| `unlicensed` | No `LICENSE` committed. Treat as all-rights-reserved, whatever the README claims. |
| `cloud-service` | Open client, closed service. The service is the constraint, not the client's license. |

Two consequences worth stating plainly:

- Our Konnect, Zeo and KiBot work is a **capability audit**, never a port. The
  audits in [research/konnect-connectivity-audit.md](research/konnect-connectivity-audit.md),
  [konnect-item1-toolset-economy-audit.md](konnect-item1-toolset-economy-audit.md)
  and [konnect-item8-design-rules-audit.md](konnect-item8-design-rules-audit.md)
  are anchored against our own codebase for exactly this reason.
- A README claiming MIT is not a license. `Seeed-Studio/kicad-mcp-server`
  commits no `LICENSE` file, so we copy nothing from it even though its
  offline-parsing approach is the closest peer to `kct mcp serve`.

## Honesty rule

A registry that records only where we win is worse than no registry, because
it invites us to skip the measurement. Two entries exist mainly to hold us to
that:

- **KiCadRoutingTools** — its `summary` states that it is faster and completes
  more nets than `kct route` on the sparse and medium boards. The full table
  in [research/kicad-routing-tools-comparison.md](research/kicad-routing-tools-comparison.md)
  also records that our own default times out on boards 05 and 07 and leaves
  pour-net pads stranded on 02, 03 and 04.
- **component-importer-for-kicad** — verdict was *pursue*, and the work is
  still not built. The registry says so rather than quietly dropping it.

## How this file stays true

Three mechanisms, in increasing order of automation:

1. **The registry is the single source.** The README's Related Projects
   section is generated from it by `render_block` in
   `scripts/ecosystem_render.py`, between `<!-- BEGIN kct:ecosystem -->` and
   `<!-- END kct:ecosystem -->` markers. Editing that block by hand is
   pointless; the next render overwrites it.
2. **A test fails on drift.** `tests/test_ecosystem_readme_block.py` re-renders
   and compares, so editing the registry without re-rendering is a loud CI
   error. `validate_research_docs` in
   `src/kicad_tools/ecosystem/registry.py` additionally asserts every cited
   note still exists — a renamed research note cannot rot silently.
3. **A weekly job re-checks upstream.**
   `.github/workflows/ecosystem-drift.yml` runs
   `scripts/ecosystem_refresh.py`, which polls each project's forge for
   license changes, renames, archives and staleness. It is **warn-only** and
   never edits the registry: a human ratifies every fact, and the script only
   prints the diff to apply. A license flip or a rename opens a tracking
   issue, because those are the two changes that silently invalidate a verdict
   or a link.

Adding a project? Append an entry to
`src/kicad_tools/ecosystem/data/projects.toml`, run
`uv run python scripts/ecosystem_render.py --write`, and commit both. The
field reference is the `EcosystemProject` docstring in
`src/kicad_tools/ecosystem/models.py`.
