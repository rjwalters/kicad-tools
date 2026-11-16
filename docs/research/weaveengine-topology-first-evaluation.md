# Evaluation: WeaveEngine (topology-first autorouter) — router ideas

**Issue**: #8993 (operator request, 2026-11-15)
**Date**: 2026-11-16
**Upstream**: https://github.com/c28ba/WeaveEngine (71 stars, Python, last push 2026-11-15)
**Upstream license**: GPL-3.0-or-later — the `LICENSE` file carries the GPLv3 text and the README's License section states "version 3 or (at your option) any later version"; GitHub's detector reports the deprecated `GPL-3.0` id. kicad-tools is MIT, so this is **ideas-only in both directions**: no source read, nothing vendored, nothing ported.
**Method**: read the upstream README and `docs/design.md` (v0.1, ~1,460 lines), fetched from raw.githubusercontent.com on 2026-11-16. No source file opened and nothing run. Everything below is stated in our own words; where a claim is the project's own (measurements, limitations), it is marked as such and not re-verified by us. If reading the source had been necessary, the ground rule says stop and ask the operator — it was not necessary.

## TL;DR

WeaveEngine is a desktop/CLI autorouter that reads Specctra DSN and writes SES. Its distinctive commitment is **topology first**: build a planar map of the free board (a triangulation whose interior edges are capacity-bounded "gates"), decide for every connection *which side of every pad and every placed trace it passes* — a planar embedding, so crossings are impossible by construction — choosing that embedding for **all nets jointly** before anything is committed, then negotiate (PathFinder-style rip-up and reroute), and only then draw geometry as taut traces at rule clearances.

Half of that we already ship: the mesh router (epic #4267, slice #4268) is the same "triangulate the board, pull the traces taut" geometry, behind `--strategy mesh`, default OFF. What we never built is the other half — the **global ordering step**: per-net route candidates generated and then chosen *jointly*, with cost terms that see the whole board (demand-aware congestion, airwire-separation, barrier closure) and a commit order driven by each net's cost of choice (regret). Our equivalents are indirect and expensive: net-order heuristics, `route_all_tuned` Monte Carlo over full-run order shuffles, and negotiated rip-up that discovers ordering damage only after it happens. That is exactly the decision shape epic #5509 and the perf items #8486/#8610 are about.

Its documented gaps are our documented strengths: no differential pairs, no length matching, no impedance control, net-class widths taken as given, no HV concept, pours delegated to KiCad. Our gaps are where its ideas point: a cheap joint plan before committing, and costs that predict blocked channels instead of discovering them.

Verdict per idea (detail below; "fit" is against our grid A\* + `Autorouter`/`RoutingOrchestrator` split):

1. **Joint per-net candidate selection before any commit** — **ADAPT** (follow-up filed).
2. **Airwire-separation cost for candidate paths** — **ADAPT** (follow-up filed).
3. **Barrier-closure warning (severance prediction)** — **ADAPT, diagnostic first** (follow-up filed).
4. **Regret ordering for net commit order** — **ADAPT** (follow-up filed).
5. **Pour-integrity reservation during routing** — **ADAPT, narrowly** (follow-up filed).
6. **Detour-repair pass after settle** — **ADAPT, narrowly** (follow-up filed).
7. Slot-aware topological search as a routing substrate — **SKIP** (watch; see prior art).
8. Mutable-map via sites (vias inserted into the planar map mid-search) — **SKIP** (their own documented weak point; our grid handles vias natively).
9. DSN side-car recovery (reading `.kicad_pcb` beside the DSN) — **SKIP** (we do not route through DSN).
10. Racing variants, time limit, best-so-far output — **SKIP** (already covered: `route_all_tuned`, deterministic budgets).
11. Geometry realisation refinements (teardrops, rounded corners, sliding vias, pad-exit windows) — **SKIP for now** (tied to their substrate; no failing case in our fleet benchmarks yet).

**Net**: six follow-up issues filed, each `loom:triage`; four rejected ideas recorded with reasons. The registry entry lands as `verdict = "ideas-adopted"`, `license_compat = "copyleft-ideas-only"`.

## How the topology-first plan works (as documented)

From `docs/design.md` (v0.1); the project itself marks several of the load-bearing pieces as **[HYPOTHESIS]** or prototype-quality, and the notes below keep that honesty.

- **Board as a planar map.** Free space is triangulated (constrained Delaunay, obstacles as holes, no Steiner points by default so every vertex lies on copper). Every interior edge is a *gate* whose capacity is how many trace centrelines fit through it at width+clearance. A wire is the ordered sequence of gates it crosses.
- **Ordering without geometry.** The key structure records, per gate, the left-to-right order of wires crossing it, with a per-triangle invariant that makes the whole set a *planar embedding*: wires can be inserted and removed in constant time per touched gate, and crossing is not merely penalised — it is unrepresentable. Their central argument: on one layer a single wire between interior pads disconnects nothing; harm arrives in two grades — *soft* (a wire forces later nets to detour around its ends) and *hard* (a closed loop of wires, pads and full gates severs a region outright) — and the two are priced separately.
- **Whole-board costs before committing.** For each net, several topologically distinct candidates are generated. Candidates are scored with lookahead terms: a demand map (each gate priced by how many not-yet-routed nets need it, from a pass with all nets routed shortest on an empty board), a count of how many *unrouted* connections' straight-line airwires a candidate would separate ("keep other pads on the same side"), and a barrier-closure penalty (a union-find over copper groups predicts when committing a wire would close a severing loop). One candidate per net is then chosen for **all nets at once** by iterative refinement over a candidate-conflict table.
- **Commit by regret.** Nets commit highest-regret-first — the net that loses most from not getting its best candidate goes first — instead of shortest-first or input order. Negotiated rip-up and reroute (present-congestion plus per-gate history, rip-up sets named by a relaxed search) cleans up afterwards; nothing is final.
- **Geometry last.** A trace is the shortest curve its embedding allows: straight runs tangent to, and arcs around, "clearance discs" whose radii follow from the embedding alone (the funnel/string-pull problem with thickness), then a check-and-repair loop, teardrops, rounded corners, and a self-measurement of the written file.
- **Multi-layer.** Vias are sites created inside the map during routing; the document records at length that this is the weakest subsystem (prototype vias were too many and decided too locally; layer choice was reframed as a whole-board decision — a crossing-colouring argument — whose first two executions did not hold up; their measured default via price sits above the congestion cap so single-layer routes survive rip-up).

## Capability comparison, both directions

Read from the README's settings/limitations sections and the design doc. "They" = WeaveEngine as documented; "we" = kicad-tools' router surface (`Autorouter`, `RoutingOrchestrator`, `kct route`).

| Dimension | They | We | Edge |
|---|---|---|---|
| Layers & vias | Multi-layer with vias inserted as sites during routing; sliding-via cleanup pass. Their own doc concedes vias are the weak subsystem: decided late and locally, counts run high, rip-up unsettles | Full multi-layer grid A\* with vias placed during search, via-conflict management, stackup-aware layer costs | **We**, on evidence; their machinery is honest but immature |
| Differential pairs | None — named a v1 non-goal; custom rules about pairs are read-and-skipped | First-class: `diffpair*.py` modules (detection, impedance, gap tightening, length tuning, tail fold, bump gates), `NetIntent(is_differential=True)` | **We**, outright |
| Length matching | None — named a v1 non-goal | `length_tuning.py`, `match_group_length.py`, `pair_centerline_meander.py`, `pcb_retune.py`, `kicad_length.py` | **We**, outright |
| Net classes | Width/clearance taken from the DSN as given; a fine-pitch net is narrowed toward the board minimum as a fallback; custom `.kicad_dru` rules read *in part* (per-net clearance/width), the rest skipped | `net_class_map: dict[str, NetClassRouting]` is a constructor argument of `Autorouter`; per-class widths, clearances and priorities throughout | **We**, outright |
| HV / custom clearance rules | Partial `.kicad_dru` support: per-net clearance floors and width bounds from net/reference/area rules; no voltage-aware concept | `kct zones hv-keepout`, `board_clearance_rules.py`, `class_clearance_floors.py`, authored-clearance machinery | **We**, outright |
| Copper pours | Delegated to KiCad: poured nets are not routed as traces; post-route fan-out places vias/stubs so the fill reaches every pad; a recent addition reserves a per-pour connectivity skeleton during routing so traces cannot cut the fill, with a fill estimator tuned against KiCad's filler | `auto_pour.py` inserts zones for power nets before routing; poured nets excluded from trace routing; zones exist as grid state; correctness delegated to the kicad-cli DRC cross-gate after refill | Roughly at parity on intent; **their skeleton idea is the adoptable delta** (idea 5) — we verify pour integrity after the fact, they preserve it during routing |
| Signal integrity | None (their own limitation list: no impedance, no return paths, no crosstalk) | Stackup+physics width selection for target impedances in `Autorouter` | **We**, outright |
| Any-angle geometry | Native: taut traces, arcs, teardrops | Grid + mesh/lattice strategies; mesh does any-angle via funnel string-pull (#4267), default OFF | Them, on their substrate; parity available to us via `--strategy mesh` for geometry |
| Determinism / reproducibility | They document per-DSN byte-identical runs; KiCad's own DSN export is the nondeterministic step on their side | Strong determinism commitments (documented tie-breaking, deterministic budgets, #3881/#4730 lineage) | Parity in intent; not comparable without measurement |
| Interface | DSN in / SES out (same lossy boundary as freerouting) | `.kicad_pcb` native, in place | **We** — the DSN round-trip is a lossy interchange boundary; `[projects.freerouting]` records why we keep manufacturing gates on the native artifact. WeaveEngine inherits that caveat wholesale |

## Its stated known limitations (their own list, paraphrased)

Signal-integrity blind (no impedance, length matching, pairs, return paths, crosstalk) — hobby/logic-class boards only. A DSN alone is missing many rules KiCad enforces, so they read the sibling `.kicad_pcb`/`.kicad_pro` to recover them (drill sizes, local clearances, custom pad true shapes, mask openings, board minimums) and the result degrades without the side-cars. One via size for the whole board (a class asking for a bigger drill yields a thin ring on import). Custom `.kicad_dru` rules read only in part; pins they would allow closer stay open. Closed solder jumpers and net-tie copper confuse KiCad's check regardless of routing. Old routing and KiCad's own teardrop zones must be stripped before export. Margin-layer drawings are held only for *added* copper by default. Dense boards (thousands of pads) take tens of minutes. Their measure of quality: routing-caused DRC errors over a 44-open-source-board sweep, driven from 3,075 to 337 by the side-car recovery work — 284 of the remainder being one Margin-layer case — with KiCad's DRC as the referee. That referee-first discipline is itself worth noticing: it is the same shape as our fleet benchmark (#5943).

## Prior art we must not rediscover

- **Our mesh router** (epic #4267, slice #4268; `src/kicad_tools/router/cpp/src/mesh.cpp` header): constrained Delaunay triangulation of the board (outline as outer boundary, pad keep-outs as holes), endpoints as Steiner points, A\* on the triangle dual, funnel string-pull to straighten. This **is** their geometry half, already vendored (BSD-3 poly2tri) and already honest about being default-OFF. What the mesh router lacks is their *ordering* half: it is per-net A\* plus string-pull, so net order still decides who gets blocked. The ideas below target that gap; none of them requires adopting their substrate.
- **Negotiated congestion**: `router/algorithms/negotiated.py` is PathFinder-style (present + history costs, adaptive increments) — the same family as their Phase 3. The delta is again what happens *before* negotiation: their Phase 1 chooses candidates jointly; we negotiate from an order.
- **Demand/supply forecasting**: `router/capacity_forecast.py` computes per-tile demand/supply as a *diagnostic*. Their demand map is the same census used as an *in-search cost*. Idea 2/1 reuse work we already have.
- **Topological peer already tracked**: `[projects.topola]` (watch, MIT, Rust) — interactive topological rubber-band routing, pre-release and unrun by us. The obvious question: what does reading a GPL-3.0 Python project's *documentation* buy that the permissive Rust one on our watch list does not? Finding, not assumption: WeaveEngine ships a complete written design (mechanisms, measured trade-offs, failed experiments recorded in prose) and 44-board evidence through a real referee, while Topola offers a codebase we have not run and would have to read to learn from — and reading GPL source is the one thing this ground rule forbids. The design doc alone carried this evaluation; that is the answer.
- **DSN/SES verdict already recorded**: `[projects.freerouting]` (complementary, GPL-3.0) — the DSN round-trip is a lossy interchange boundary, which is why we route the native artifact. WeaveEngine inherits that whole caveat; the issue's ground rule ("no DSN/SES routing path") restates our settled position and the note adds nothing to it.

## Disposition table

| # | Idea (our words) | Verdict | Existing prior art in kicad-tools | One-line rationale |
|---|---|---|---|---|
| 1 | Choose one route per net for all nets jointly, before any commit | **Adapt** | `route_all_tuned` Monte Carlo searches net *orders* with full runs; negotiated mode starts from one order | Joint candidate selection is the direct, cheap form of the whole-board decision we currently approximate |
| 2 | Price a candidate path by how many unrouted airwires it separates | **Adopt** | None in-search (`capacity_forecast.py` is diagnostic-only) | Cheap grid-computable term; attacks early-net-blocks-later-net at its actual mechanism |
| 3 | Warn when placed copper + full channels close a severing loop | **Adapt (diagnostic first)** | None (we discover severance by failed searches) | Predicts unrouteability instead of discovering it; feeds reporting before burning rip-up rounds |
| 4 | Commit nets by regret (cost of not getting their best route) | **Adapt** | MST-interleaved ordering, base orders, Monte Carlo shuffles | Principled, cheap drop-in ordering candidate alongside the existing ones |
| 5 | Reserve each pour's connectivity skeleton while routing so refill cannot cut it | **Adapt (narrow)** | `auto_pour.py`, zone-aware grid; integrity checked only post-refill by DRC | Turns isolated-copper DRC findings into a routing-time constraint; dense boards are where it pays |
| 6 | Post-settle detour repair: rip the blockers of the worst detoured connections, route short, restore | **Adapt (narrow)** | Rip-up and `partial_rescue` exist; no detour-ratio-driven quality pass | Targeted quality pass with a cheap accept gate; orthogonal to completion |
| 7 | Planar-embedding search as the routing substrate | **Skip (watch)** | Mesh router covers the geometry half (#4267) | Research-scale bet on their own evidence ([HYPOTHESIS] markers, unsettled rip-up, vias rework); revisit on external evidence of steady multi-layer wins |
| 8 | Vias as sites inserted into a mutable planar map mid-search | **Skip** | Grid router places vias natively during search | Their own doc's weakest subsystem; nothing to borrow that our substrate doesn't already do better |
| 9 | Recover DSN-lossy fields from sibling KiCad files | **Skip** | `benchmarks/external/fleet_routers/run.py` `dsn_audit` already enumerates DSN lossiness | We don't route through DSN; the harness already audits the boundary |
| 10 | Racing variants / time limit / best-so-far output | **Skip** | `route_all_tuned`, `--deterministic-budget`, per-net iteration caps | Already covered by our own tuning machinery |
| 11 | Taut-trace geometry refinements (teardrops, rounded corners, sliding vias, pad-exit windows) | **Skip for now** | Mesh strategy has funnel geometry; our grid output has its own tidying passes | Tied to their substrate; no failing case in our fleet benchmarks yet — revisit if `--strategy mesh` graduates |

## Per-idea detail, fit, and smallest next step

### 1. Joint per-net candidate selection before any commit — Adapt

**The behaviour, our words.** Before any trace is committed, give every net a handful of genuinely different route options (different corridors across the board, not perturbations of one), score every net's options against every other net's, and pick one option per net to minimise the *sum* — length plus mutual interference — as one optimisation. Commit only after that choice is made, in an order the choice itself suggests (idea 4). The point: the decision "who goes where" is made once, with everyone at the table, instead of emerging from who happened to be routed first.

**Fit.** Our closest existing mechanisms are `route_all_tuned` (Monte Carlo over full runs with shuffled net orders — expensive, and it explores *orders*, not routes) and `route_all_negotiated` (fixes order damage afterwards with rip-up). A selection phase is the cheap middle: candidate routes are found on an empty/near-empty board (cheap searches), the conflict table is pairwise and sparse, and the joint pick is a small optimisation over per-net candidate lists. It targets the #5509 class — ordering and blocked channels — at its root rather than through rip-up healing. Where it lands: a driver-level phase in `Autorouter` (or the negotiated driver) feeding chosen seeds into the existing commit path; no change to the per-net search itself.

**Smallest next step.** In the negotiated driver only: before the first pass, generate 2–3 candidates per net on the empty grid, score candidate pairs by shared-segment conflict, pick jointly by a few sweeps of local improvement, then run the existing pipeline from those seeds. Measure overflow entering rip-up and final completion on the fleet benchmark boards against the same driver without selection. Kill criterion: if overflow entering rip-up does not drop, the phase is dead weight.

### 2. Airwire-separation cost — Adopt

**The behaviour, our words.** Every unrouted connection has an obvious naive route: the straight line between its endpoints. Before committing a path, count how many of those naive lines of *other still-unrouted* connections it would force to detour (they cross the path), and make that count a cost. A route that slips around other nets' pending desire-lines is preferred to one that cuts across them, even at slightly greater length.

**Fit.** Directly implementable in our per-net search as an extra cost term, or in candidate scoring if idea 1 lands first: we can rasterise each unrouted connection's straight line on the grid once (cheap, updated incrementally as nets commit), and a candidate path's crossing count is a sum over its cells. This is the most mechanistically direct attack on the failure mode where an early net's convenient route makes a later net impossible — and it costs far less than the Monte Carlo order search that currently discovers the same information. Independent of idea 1; composes with it.

**Smallest next step.** Add the term behind a flag in the negotiated driver; A/B on two fleet boards with known ordering pain (the #8610 dense mains board is the obvious one), measuring completion and rip-up round count. Guard determinism: the term must be a pure function of the committed set, with documented tie-breaking.

### 3. Barrier-closure warning — Adapt (diagnostic first)

**The behaviour, our words.** Maintain, during routing, a union-find over regions of the board separated by copper: placed traces weld their endpoints' regions together, and a gate that fills to capacity welds the regions on its two sides. When a weld would join two regions that are already joined, a closed loop has formed; count the unrouted connections whose endpoints now lie on opposite sides and report (and optionally price) that severance immediately.

**Fit.** We discover unrouteability the expensive way: a later net's search fails, rip-up churns, and the diagnosis is forensic. A closure detector predicts it at the moment the closing copper is placed, which is exactly when a different choice is still cheap. As a first step it needs no search changes at all — it is bookkeeping over capacity state we already track — and it slots into the same reporting surface as `capacity_forecast.py`. Promoting it to a cost term can come later; the diagnostic alone changes how fast we can triage a board that will not route.

**Smallest next step.** Prototype the union-find inside the negotiated driver as a reporting hook only (no cost change): on each commit, detect closures and log severed-net counts. Validate against boards with known unroutable orderings: the warning should fire rounds before the failing search does.

### 4. Regret ordering — Adapt

**The behaviour, our words.** Rank nets for committing by how much they lose if they do not get their preferred route — the difference in cost between their best and second-best option — and commit the biggest-loss nets first. Nets with many equally good options go late; they can adapt to whatever is left.

**Fit.** We already treat net ordering as a first-class lever (MST-interleaved ordering, base orders, `--order-method`, Monte Carlo over shuffles) — this is another *principled* order in that existing menu, computable from the same candidate data idea 1 produces (or from two searches per net on an empty board if standalone). It is cheap, deterministic, and directly comparable against the existing orders using our current evaluation harness. Standalone value even if idea 1 never lands.

**Smallest next step.** Compute a regret order (two empty-board searches per net) in the negotiated driver behind a flag; benchmark against the existing order methods on the fleet boards through the standard completion/DRC gate.

### 5. Pour-integrity reservation — Adapt (narrow)

**The behaviour, our words.** When a net has a zone, don't just exclude the net from trace routing and hope the refill fits: compute a minimal connectivity skeleton for the zone (a spanning tree joining its pads, at the zone's width and clearance) and reserve that skeleton's room in the router the way a wide trace would be reserved. Traces then route around the fill's standing room, and the refill cannot be cut into islands by post-hoc trace placement.

**Fit.** We already do the first half (`auto_pour.py` inserts zones, poured nets are excluded from trace routing) and verify integrity only afterwards via the kicad-cli DRC cross-gate (`isolated_copper` findings after refill). The delta is making "keep the pour whole" a routing-time constraint instead of a post-route finding. Cost/risk: on dense boards the reservation competes with traces for room — the upstream project's own measured history is instructive here (their negotiated-with-traces variant lost connections and doubled runtime before they settled on traces-first, skeletons inserted after settle, moving traces only where the skeleton does not fit) — so our version should start where theirs ended: insert skeletons after the trace routing settles, then a bounded make-way pass, never losing a trace to a skeleton. Net value even in the narrow form: fewer isolated-copper/starved-thermal findings on the dense boards where refill integrity is worst (#8610's board class).

**Smallest next step.** Off-line experiment first, no router change: post-route, compute for each zone a skeleton and check (with the existing clearance machinery) whether it would fit in the routed board; report where it would not. If dense fleet boards show real conflicts, file the router-reservation work with those numbers as the baseline.

### 6. Detour-repair pass — Adapt (narrow)

**The behaviour, our words.** After routing settles, score every connection's drawn length against what it would have cost alone on the empty board; for the worst offenders, identify the (few) placed wires actually blocking the short corridor, lift them plus the offender, route the offender the short way, put the lifted wires back where they still fit, and keep the rearrangement only if nothing that was routed is now unrouted and total copper shrank.

**Fit.** We have the substrate: rip-up machinery, `partial_rescue`, and exact-cost measurement (`route_copper_order` exists precisely so the length model measures what the checker sees, #7790). What we lack is the *driver*: a bounded quality pass that targets the detour distribution rather than completion. The accept gate (nothing opens, copper shrinks) makes it safe and inherently terminating. Value is length/manufacturability, not completion — a quality lever, sized small.

**Smallest next step.** Instrument first: add a detour-ratio report (drawn vs alone-on-empty per connection) to the routing journal on a fleet board, and check the distribution's tail. If the tail is fat (a few connections at multiples of their alone-cost), file the repair pass with those numbers; if the tail is thin, reject with data.

## License note

GPL-3.0-or-later (LICENSE = GPLv3 text; README declares "version 3 or any later version"; GitHub detector reports the deprecated `GPL-3.0`, recorded in the registry entry as `upstream_license_detected` so the refresh script does not report a phantom license change). Per the registry's standing rule this is `copyleft-ideas-only`: capability-level engagement only, no code in either direction. This evaluation read the README and design document only; the follow-up issues are stated as behaviours with acceptance criteria, not as ports. If any follow-up ever needs WeaveEngine's source to evaluate further, the ground rule applies: stop and ask the operator first. The optional black-box run (their released binary on our public boards through the fleet harness) remains permitted and un-attempted here — the harness (`benchmarks/external/fleet_routers/`) already supports adding a fourth adapter, and the issue's bar for it is "if cheap"; it was not cheap in this environment (pinned-toolchain referee runs and a scratch-tree GPL build), so it is left to the follow-ups' discretion rather than half-done.

## Acceptance-criteria mapping

| AC (issue #8993) | Where satisfied |
|---|---|
| No WeaveEngine entry today; add one with verdict + `license_compat`; re-render | `[projects.weaveengine]` added to `src/kicad_tools/ecosystem/data/projects.toml` (`verdict = "ideas-adopted"`, `license_compat = "copyleft-ideas-only"`, `research_docs` → this note); README block regenerated via `scripts/ecosystem_render.py --write` |
| Research note in `docs/research/`, linked from the registry entry, covering the plan as documented, both capability directions, and stated limitations | This note: "How the topology-first plan works", "Capability comparison, both directions", "Its stated known limitations" |
| Concrete adoptable ideas, in our own words, with fit judgements vs the grid router and `Autorouter`/`RoutingOrchestrator` split | "Disposition table" + "Per-idea detail, fit, and smallest next step" |
| Follow-up issue per idea worth pursuing; rejected ideas listed with reasons | Six filed (numbers below); rejected ideas in disposition rows 7–11 with reasons |
| Optional black-box run | Not performed — environment cost (pinned referee toolchain + scratch-tree GPL build) judged above the "if cheap" bar; disclosed here and in the PR |

## Follow-up issues filed

Each filed at `loom:triage`, one per adoptable idea, self-contained, stated as behaviour + smallest next step (see the per-idea sections above for rationale):

1. #8998 — joint per-net candidate selection before commit (negotiated driver experiment)
2. #8999 — airwire-separation cost term
3. #9000 — barrier-closure diagnostic for negotiated routing
4. #9001 — regret ordering as an order method
5. #9002 — pour-skeleton fit experiment (post-route, off-line first)
6. #9003 — detour-ratio report and targeted repair pass

## Recommendation

Adopt the *planning* ideas, not the *substrate*: WeaveEngine's transferable value is the shape of its global decision (joint candidate choice, airwire-separation, severance prediction, regret ordering), all of which compose with our grid router and none of which requires touching the C++ A\* or the mesh strategy. Ideas 2 and 4 are cheap and measurable immediately; idea 1 is the strategic one against the #5509 ordering class; ideas 3, 5, 6 are diagnostics/quality passes with instrument-first steps. Keep the project in the registry with `ideas-adopted` and revisit its via story (their weakest subsystem, actively being reworked) if a future release demonstrates steady multi-layer completion on boards like our dense fleet cases — and keep Topola on `watch` beside it as the permissive-code escape hatch if topological routing ever needs to be tried in earnest.
