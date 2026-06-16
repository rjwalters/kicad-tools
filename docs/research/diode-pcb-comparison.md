# Diode `pcb` (Zener) vs kicad-tools — Research Comparison

**Date**: 2026-06-15
**Issue**: #3726 (research only — no kicad_tools source changes)
**Subject**: [`cybernetic-physics/pcb`](https://github.com/cybernetic-physics/pcb)
(public mirror of [`diodeinc/pcb`](https://github.com/diodeinc/pcb), by
**Diode Computers, Inc.**, MIT-licensed, Rust). Docs: <https://docs.pcb.new>.

All citations below are paths inside the `cybernetic-physics/pcb` repo, read via
`gh api` at the time of writing (no web access assumed).

## TL;DR

Diode `pcb` and kicad-tools share the mission (code → KiCad-10 → manufacturable
board) but sit at **opposite ends of the same pipeline**:

- **Diode owns the front end.** Their differentiator is *Zener*, a Starlark-based
  schematic-as-code HDL, plus a polished toolchain/registry/agent-skills story.
  Their KiCad automation is deliberately thin: they generate/sync the layout and
  hand routing+DRC to the human in KiCad. They have **no autorouter** and rely on
  KiCad's DRC engine.
- **kicad-tools owns the back end.** Our differentiator is autorouting, placement
  optimization, DRC remediation, and manufacturer-profile export — the parts
  Diode leaves to a human in KiCad. Our front end (the `intent`/`design` modules)
  is comparatively thin.

So most of their bets are *complementary*, not competitive. The few genuinely
portable ideas are in their **board-config / design-rules data model**, their
**agent skills packaging**, and their **layout-sync lens** (which we've already
partly reinvented for layout preservation).

---

## 1. Schematic-as-code (Zener) vs our intent/design path

### What Zener is

A `.zen` file is Starlark (Bazel's Python-subset) extended with PCB primitives
(`skills/zener-language/SKILL.md`, README "Core Concepts"). Design entry looks like:

```python
Resistor = Module("@stdlib/generics/Resistor.zen")
VCC = Power(); GND = Ground(); LED_ANODE = Net()
Resistor(name="R1", value="1kohm", package="0402", P1=VCC, P2=LED_ANODE)
Board(name="blinky", layers=4, layout_path="layout/blinky")
```

Key modeling primitives (`skills/zener-language/SKILL.md`):
- **Nets** — `Net()`, with specialized `Power`/`Ground`/`NotConnected` subtypes and
  promotion/demotion rules across module boundaries.
- **Hierarchy** — `Module("./Foo.zen")` instantiates a subcircuit; modules declare
  inputs with `io(template, ...)` (nets/interfaces) and parameters with
  `config(typ, default=..., allowed=[...])`.
- **Interfaces** — reusable grouped-signal bundles (`I2c`, `Spi`, `Usb2`,
  `DiffPair`) from `@stdlib/interfaces.zen`.
- **Typed physical units** — `Voltage`, `Current`, `Resistance` from
  `@stdlib/units.zen`; configs are expected to use them, with `allowed=[...]`
  for discrete choices and string auto-conversion (`"3.3V"`).
- **Checks** — `check(cond, msg)`, `warn`, `error` for inline electrical
  validation (`@stdlib/checks.zen`).
- **DNP discipline** — a strong idiom: configs may change *values* and `dnp=`
  state but **must not change which instances/nets exist**. Mutually-exclusive
  straps are all instantiated, with inactive ones marked `dnp=`, so schematic
  topology stays stable across parameterizations.

### How it compares to us

Our equivalent is `src/kicad_tools/intent/` (interface-driven *constraints*:
`intent/types.py` `IntentDeclaration`, `InterfaceCategory`, `Constraint`) plus
`src/kicad_tools/design/` (`decomposition.py`, `subsystems.py`, `strategies.py`).
These are **constraint/intent declarations layered on an existing netlist**, not
a from-scratch schematic-authoring language. We do not have a composable
human-or-agent-authored HDL that *produces* the netlist — boards in `boards/` are
Python scripts using our API, which is closer to Zener in spirit but ad hoc and
without Zener's units/io/config/interface vocabulary or the DNP-topology-stability
rule.

**Cost/benefit of adopting a `.zen`-like front end:** High cost, uncertain fit.
Zener is a whole Starlark runtime + LSP + package manager (`crates/pcb-zen*`,
`pcb-starlark-lsp`) — easily a multi-month effort to clone, and it would compete
with, not extend, our Python-API design path. atopile (already researched in
`docs/research/atopile-*`) occupies the same niche and we declined to adopt its
DSL wholesale for the same reason. **Verdict: ignore the language itself**; mine
specific *idioms* instead (units-typed configs, DNP-stable topology, interface
bundles) as incremental improvements to our Python design API if/when we
formalize one.

---

## 2. KiCad-10 automation, design-rules, manufacturer profiles

### What they do

- `pcb layout` generates/updates `*.kicad_pcb` and opens KiCad; `pcb open`
  reopens; `cargo run -p pcbc -- layout --no-open` for headless
  (README "Command Reference", `AGENTS.md`).
- The interesting part is **`crates/pcb-layout`**, a *lens-based* netlist↔layout
  synchronizer (`crates/pcb-layout/README.md`). It models the board as
  `D ≅ View ⊕ Complement`: **View** (reference/value/fpid/nets) is always
  source-authoritative; **Complement** (position/rotation/layer/locked/tracks/
  vias/zones) is always dest-authoritative. Sync is
  `sync(s,d) = join(get(s), adapt_complement(get(s), *extract(d)))`, with stated
  laws: View-consistency, Complement-preservation, idempotence, structural
  fidelity. Footprint identity = `(EntityPath, FPID)`; an FPID change is a
  delete+add with position inheritance. The sync core is **Python**
  (`crates/pcb-layout/src/scripts/lens/`, with Hypothesis property/stateful
  tests).
- **No autorouter.** Routing is authored by the human in KiCad and *preserved*
  by the lens across regenerations. DRC is KiCad's own.

### Design-rules / manufacturer profiles (directly relevant to #3719/#3720)

`stdlib/board_config.zen` is a clean, declarative design-rule + stackup data model:
- `Constraints` record grouping `Copper` (clearance, track/connection/annular
  width, via diameter, copper-to-hole/edge), `Holes`, `Uvias`, `Silkscreen`,
  `SolderMask`, `Zones`.
- `NetClass` record (clearance, track width, via dims, diff-pair width/gap/via-gap,
  priority, color, single-ended & diff-pair impedance).
- `PredefinedSizes` (track widths, via dimensions for KiCad dropdowns).
- `Stackup` (materials with εr/loss-tangent, copper/dielectric layers, finish,
  colors, symmetric assertion).
- A `deep_merge` / `merge_configs(*configs)` so a base profile can be layered with
  board-specific overrides (later wins), field-by-field.

This is the same problem we solved in #3720 (`feat(export): emit sibling
.kicad_pro DRC constraints from manufacturer profile`). Our model lives in
`src/kicad_tools/manufacturers/` (`base.py` `DesignRules` dataclasses loaded from
per-vendor YAML in `data/`, plus `dru_generator.py`, `project_generator.py`,
vendor profiles `jlcpcb*.py`, `oshpark.py`, `pcbway.py`, `seeed.py`,
`flashpcb.py`). We're already as capable here, arguably more so (multiple
concrete vendor tiers).

**Two honest deltas worth noting:**
1. Their `merge_configs` layering (base profile + per-board override, deterministic
   "later wins" deep-merge) is a clean pattern. We have YAML profiles but the
   override/layering story is less explicit. Low-effort idea to borrow.
2. They candidly document KiCad SWIG API gaps (e.g. diff-pair dimensions
   unsupported, `minimum_text_thickness` unsupported — comments in
   `board_config.zen`). Useful cross-check: confirm our `.kicad_pro`/DRU export
   doesn't silently claim to set fields the KiCad API can't actually round-trip.

**Borrow for our router/DRC pipeline?** Their *routing* side has nothing to
borrow (no autorouter — that's our strength). Their *lens sync* is the part worth
studying, and we've already independently built layout-preservation
(`docs/research/atopile-layout-reuse.md`, issue #305 lineage). Their lens-law
framing (idempotence + property tests with Hypothesis) is a quality bar we could
adopt for our own preservation code.

---

## 3. stdlib generics

`stdlib/generics/*.zen` are parametric component factories: `Resistor`,
`Capacitor`, `Inductor`, `Led`, `Diode`, `Mosfet`, `Bjt`, `Crystal`,
`OperationalAmplifier`, `Tvs`, `Zener`, `FerriteBead`, `Thermistor`,
`TestPoint`, `Fiducial`, `MountingHole`, `NetTie`, `SolderJumper`, etc.

`stdlib/generics/Resistor.zen` is representative:
- `Package = enum("0201".."2512")`, `value = config(Resistance)`, optional
  `voltage`/`power`/`mpn`/`manufacturer`/parasitic `esl`/`cp`.
- A `_footprint()` map from package enum → `@kicad-footprints/...kicad_mod`, and a
  `_symbol()` map to `@kicad-symbols/Device.kicad_sym`.
- Emits a `Component(... type="resistor", spice_model=SpiceModel("spice/Resistor.lib",
  ...))` — i.e. each generic carries a **SPICE model with package-derived
  parasitics** (`_spice_args` returns ESL/Cp from a per-package table).
- Carries deprecation warnings inline (`do_not_populate` → `dnp`).

Compared to our `src/kicad_tools/library/generators/`, `parts/`, `footprints/`:
we generate symbols/footprints too, but Diode's generics are notable for
(a) one file = symbol + footprint map + BOM properties + **SPICE model** in a
single parametric unit, and (b) the package→parasitic tables that make every
passive simulation-ready by default. Our library is more about KiCad artifact
generation; theirs ties the part to simulation and sourcing metadata.

**Verdict:** Mostly a structural-organization lesson, not a port. If we ever add
SPICE-backed verification, the "generic carries its own parasitic model keyed by
package" pattern is a good template.

---

## 4. AI-agent integration (AGENTS.md + skills/) vs loom roles

This is where Diode is clearly ahead of a generic project, and the most directly
*adoptable* area.

**`AGENTS.md`** is a tight, mechanism-level agent guide: exact CLI invocations
(`cargo run -p pcbc -- build/test/fmt/layout`), a "Where to Look" crate map,
"Working Rules" (smallest correct change; Zener-is-Starlark-not-Python; no
f-strings), and **Documentation/Verification rules** ("run the narrowest relevant
check first", "do not run full-workspace checks after every small edit",
"leave snapshot acceptance to the user"). This is our `CLAUDE.md` + `.loom/roles`
equivalent but more *task-routing-oriented* (it tells an agent **where** code
lives and **which** narrow check to run).

**`skills/`** are packaged, front-mattered capabilities — each is a directory with
a `SKILL.md` whose YAML `description` states *when* to invoke it:
- `zener-language` — canonical HDL semantics, used before touching `.zen`.
- `librarian` — authoring reusable registry components, datasheet-backed, with
  guardrails ("Do not invent datasheet facts… find evidence or ask").
- `registry-search` — `pcb search -m registry:modules|components` before authoring.
- `datasheet-reader` — `pcb scan <pdf|url>` → markdown → read markdown.
- `spice-sim` — add an ngspice testbench via `Simulation(...)`.

The pattern: **skill = trigger description + workflow + guardrails + exact
commands**, scoped to one capability and discoverable by an agent. Compared to
loom roles (`.loom/roles/*.md`, which are *who-am-I* personas — builder, judge,
curator), Diode skills are *what-can-I-do* capability cards orthogonal to role.
We have a thematically similar `loom:builder` skill notion, but our domain
knowledge (routing recipes, anchor-weight tricks, fleet status) is scattered
across `CLAUDE.md` + memory notes rather than packaged as invokable, front-mattered
skills with explicit trigger conditions and guardrails.

**Strong "librarian" guardrail worth importing verbatim in spirit:** "Do not
invent datasheet facts, pin mappings, footprints, passive values, limits,
sourceability, or application topology. Find evidence or ask." That directly maps
to our recurring failure mode where agents fabricate part/footprint data — and our
memory already says "never suggest manual KiCad; file gaps instead", which is the
same anti-hallucination posture.

---

## 5. Toolchain / versioning UX (`pcb`/`pcbc` shim) vs `uv`/`kct`

Diode ships a two-binary model (README "Installation", `AGENTS.md`,
`crates/pcb/src/main.rs` is "the shim/version manager"):
- **`pcb`** — a thin shim installed via `curl … install.sh | bash` to
  `~/.local/bin`. It reads the project's pinned `pcb-version` (in `pcb.toml`
  `[workspace]`) and **downloads + runs the matching `pcbc` toolchain**.
- **`pcbc`** — the actual compiler/CLI.
- Projects pin `pcb-version = "0.3"` in `pcb.toml`; a `pcb.sum` lock file pins
  dependencies; `members = ["components/**", "modules/*"]` defines the workspace.

This is the rustup/`.tool-versions` model applied to PCB tooling: per-project
toolchain pinning so a board built two years ago still compiles with the exact
toolchain it expects. Our distribution is `uv` + the `kct` entrypoint, which gives
us reproducible *Python deps* via `uv.lock` but **not** a per-board pin of the
`kicad-tools` version itself — a board script just imports whatever `kicad_tools`
is installed in the env.

**Verdict:** The shim's value (reproducible per-board toolchain) is real but the
cost is high (build a downloader/version-manager + hosted toolchain artifacts).
`uv` already gets us most of the reproducibility for free if we pin `kicad-tools`
itself per board. The lighter-weight lesson: **record the `kicad-tools` version a
board was generated with** (in board metadata) so regenerations are reproducible —
much cheaper than a full shim.

---

## 6. Licensing / attribution

- License: **MIT** (`LICENSE`, README "License"). Zener-the-language is MIT.
- They themselves attribute: built on Meta's `starlark-rust`, `ruff fmt` (MIT) for
  `pcb fmt`, and credit atopile + tscircuit as inspiration (README
  "Acknowledgments", "Third-Party Software").
- **What we may reuse:** MIT permits adapting code/ideas with attribution. The
  most reuse-friendly artifacts are the **declarative data models** in
  `stdlib/board_config.zen` (design-rule/stackup record shapes) and the
  **lens algebra** documented in `crates/pcb-layout/README.md` — both are
  conceptual and easy to reimplement in Python without copying source. If we ever
  copy non-trivial code, retain the MIT notice and credit "Diode Computers, Inc.
  (diodeinc/pcb)".

---

## Concrete takeaways (adopt / ignore / follow-up)

| # | Takeaway | Action | Effort | Candidate follow-up issue? |
|---|----------|--------|--------|----------------------------|
| 1 | **Package our routing/manufacturing know-how as front-mattered "skills"** (trigger description + workflow + guardrails + exact commands), the way `skills/librarian` etc. do. Esp. an anti-hallucination guardrail for part/footprint data ("find evidence or ask"). Today this is scattered in `CLAUDE.md` + memory. | **Adopt** | Low–Med (docs/skills authoring, no code) | Yes — "Package agent capabilities as invokable skill cards (mirror Diode `skills/`)" |
| 2 | **Layer manufacturer profiles with an explicit deep-merge override** (base vendor profile + per-board override, "later wins") like `board_config.zen` `merge_configs`. We have per-vendor YAML but a thin layering story. | **Adopt** | Low (extend `manufacturers/base.py`) | Yes — "Add base+override deep-merge layering to manufacturer DesignRules" |
| 3 | **Audit our `.kicad_pro`/DRU export against KiCad-10 API round-trip gaps** that Diode documents (diff-pair dimensions, silk text thickness unsupported via SWIG). Make sure #3720's export doesn't silently emit fields KiCad ignores. | **File follow-up** | Low (verification) | Yes — "Verify manufacturer-profile DRC export round-trips through KiCad-10 (no silently-dropped fields)" |
| 4 | **Adopt lens-law-style property tests for our layout-preservation code** (idempotence + Hypothesis property/stateful tests, per `pcb-layout/README.md`). Raises confidence that regeneration never clobbers placement/routing. | **File follow-up** | Med (test-only) | Yes — "Property-test layout-preservation invariants (idempotence, complement-preservation)" |
| 5 | **Record the `kicad-tools` version + manufacturer profile a board was generated with** in board metadata, for reproducible regeneration — the cheap 80% of Diode's per-project toolchain pin without building a shim. | **File follow-up** | Low | Optional |
| — | **Zener language front end** (clone a Starlark HDL + LSP + registry) | **Ignore** | Very High | No — out of scope; overlaps atopile we already declined |
| — | **`pcb`/`pcbc` shim toolchain manager** (hosted artifacts + downloader) | **Ignore** | High | No — `uv` + per-board version pin (TO #5) covers the need |

### Honest caveats

- Diode's biggest strengths (Zener, the registry, the shim) are **front-end and
  distribution** bets that don't map onto kicad-tools' back-end strengths
  (autorouting, placement optimization, DRC remediation). It would be a mistake to
  chase their language; the complementary view is more accurate than the
  competitive one.
- We are **at or ahead of parity** on design-rules/manufacturer profiles
  (#3719/#3720, multiple concrete vendor tiers) and **strictly ahead** on routing
  (they have no autorouter). The portable wins are small, targeted, and mostly
  about *packaging* (skills) and *test rigor* (lens laws), not new core capability.
- All five "adopt/follow-up" items are independently shippable and none requires
  touching the router or core data path.
