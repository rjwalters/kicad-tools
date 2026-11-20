# PCBJam blind PCB study: what a frontier agent does with a stripped KiCad board (Issue #9596)

**Date**: 2026-11-19. Facts are as of blind-pcb-runs `b5a047f3d8e7963bca46da485b05c6a7041d9cfb`
(HEAD, committed 2026-11-18) unless stamped otherwise.
**Upstream**: [PCBJam/blind-pcb-runs](https://github.com/PCBJam/blind-pcb-runs) — the repo's
scripts, briefs and docs are MIT; every board under `boards/` keeps its own licence
(see the upstream `boards/README.md`).
**Registry**: `kct ecosystem show pcbjam-blind-pcb-runs` (the study repo),
`kct ecosystem show pcbjam` (the sibling GPL-3.0 WASM pcbnew port — not studied here beyond
what it is and its licence).
**Downstream issues**: #9590, #9591, #9592, #9593, #9594, #9595.

## What the study is

PCBJam ran a blind-PCB experiment in November 2026: Claude Code running the
Fable 5.2 model gets a real, human-routed KiCad 10 board with its copper
stripped (and, in placement runs, the free parts moved off the board), and has
to place and route it inside a network-isolated Docker sandbox that cannot
reach the original design. **21 runs over 9 human-routed boards** (as of
`b5a047f3`, 2026-11-19), from a 2-layer macropad to a 6-layer ECP5 + DDR3
board. The runs came in five batches: bare `pcbnew`/`kicad-cli` (where the
agents wrote their own tooling, up to a self-written A* router on H4E2),
post-run interviews about what tooling would have helped, autorouters the
agents found and installed themselves (both Arduino Mega agents fetched
Freerouting through the proxy), a KiCad MCP server, and `bt` — a stateless
board-geometry CLI that PCBJam built while the runs were going, shaped by
what earlier agents asked for.
Everything the agents saw and produced is published: briefs, full transcripts,
scoring reports and renders. Scoring is KiCad 10 DRC with zones refilled, plus
open-item counts per net and track/via metrics. Upstream's own caveat, which
we adopt: one run per setup — anecdotes with receipts, not measurements with
error bars.

## Method of this note

Read from raw.githubusercontent.com on 2026-11-19, at the commit pinned above:
the repo README, `boards/README.md`, `runs/INDEX.md`, each run's `SUMMARY.md`,
`brief.md` and `work/BT-REVIEW.md` — plus PCBJam's public write-up of the
study ([pcbjam.com/blog/fable-blind-pcb-layout](https://www.pcbjam.com/blog/fable-blind-pcb-layout),
read 2026-11-19) for the study's own "finished" definition, its per-board
commentary and its account of the tooling batches. No board file was opened
and nothing was run — that was #9595's job (since done for the first board;
see `pcbjam-blind-pcb-results.md`). Quotations below come from two
sources: those attributed to a run are from the MIT-licensed repo; those marked
"(write-up)" — including the CM4 passages — are from PCBJam's separate article,
which the repo's MIT licence does not cover (see Licence hygiene), so they are
short quotations with the link retained. `bt` itself is **not
published**; what we know of
it comes only from its public command description (`sandbox/briefs/tools-bt.md`)
and the agents' reviews in each run's `work/BT-REVIEW.md` — ideas only.
Nothing from the GPL-3.0 `PCBJam/pcbjam` repository is copied; that repo
contributes two facts to us (what it is; its licence), stated in our own words.

## Findings

Per-run scores cited below are from `runs/INDEX.md` and each run's `SUMMARY.md`
at the pinned commit. "Clean" = 0 DRC errors and 0 open items on the saved board.

### 1. Boards that aren't dense finish — small or large

By the raw INDEX scores, 11 of 21 runs came back literally clean (0 DRC, 0
open): all five macropad runs, both H4E2 keypad runs,
the M.2 PCIe adapter ($6.93 — the cheapest run), and all three ESP32-P4
RedBoard runs. That raw count
mixes tooling conditions — none of it was "unaided" in a strong sense: five of
the eleven ran with `bt` (a macropad, the M.2 adapter, all three RedBoards),
one with the KiCad MCP server, and five with no tools given, where the agents
wrote their own scripts (on H4E2, their own A* router). Our raw bar is also
stricter than the study's own: the write-up defines "finished" as 0 DRC errors
that weren't already in the input and nothing unconnected, and by that
definition **16 of 21 runs finished** — the raw-clean eleven plus both Arduino
Mega runs, the Glasgow route-only run, and both CM4 NAS runs. For those 16 finished runs,
the pinned [run index](https://github.com/PCBJam/blind-pcb-runs/blob/b5a047f3d8e7963bca46da485b05c6a7041d9cfb/runs/INDEX.md)
records wall times from 22 minutes for the M.2 adapter to 4 hours 44 minutes
for RedBoard placement and routing.

The CM4 NVMe NAS is what kills the "only small boards finish" reading, and it
was the study's real test: a 298-footprint, 4-layer Raspberry Pi CM4 carrier —
ASMedia PCIe switch feeding four M.2 NVMe slots, Gigabit Ethernet, two USB-C
ports, six buck regulators — and "no hint at all that the pairs are special:
one default net class, no differential-pair rules" (write-up). The study's
largest board by footprints, track length and outline, and both of its runs
finished (route-only 2:51 / $34.64, place+route 2:55 / $33.95; the 17 DRC on
each are input `footprint_type_mismatch` findings, and the route-only run's
one open item is the USB-C shield pads the netlist leaves unconnected on
purpose). Of the place+route result the write-up says: "this is the best PCIe
in the study, the human's included" — switch moved next to the sockets, ports
1–3 at 26–52 mm against the human's 76–101 mm, all 15 pairs on the top layer
over solid ground. Sparse, not small, is what predicts finishing; the dense
failures are finding 2.

### 2. Dense BGA boards run out of room

The 0.5 mm-pitch BGA + DDR3 OrangeCrab (6 layers) is the study's clearest
failure: the agent placed all 163 footprints and fully routed DDR3, but ended
with **70 open items in 26 nets** — including FPGA power-ring balls, USB data
lines, flash MISO — and said so plainly: "I ran out of working budget in the
dense areas around the FPGA" (orangecrab run, agent's NOTES.md). Its 86 DRC
errors are the castellated-edge findings the input board already had, not new
damage. The Glasgow Interface Explorer (0.8 mm BGA-121, 4 layers) splits the
finding in an instructive way: **route-only completed** (0 open, 4 DRC), while
**place+route hit the 4-hour time limit with 73 items in 22 nets still open**
(glasgow-place run, exit 124). Blind placement before routing, not the BGA
escape itself, is where the dense boards die. The 4-layer ESP32-PoE-ISO (dense
but no BGA) also stayed incomplete in all three of its runs (48, 43 and 39
open items).

### 3. The slow nets get autorouter treatment — and cut the planes

Across runs the agents converge on the same workflow: hand-compute the
critical geometry (diff pairs, USB, PoE, PHY fan-out, BGA escapes, DDR buses),
check it with a probe, apply it — then hand the remaining bulk to the router.
Sometimes "the router" is literally an external autorouter: the mega-route
agent fetched Freerouting through the sandbox proxy (GitHub blocked, so via
JitPack and Maven Central) and routed most of the board through it in stages —
its own verdict, "The signal routing is autorouter quality, not hand quality"
(mega-route run, NOTES.md). That bulk is where planes die. The
ESP32-PoE-ISO route-only agent allowed an
inner layer as overflow "without shove" and got **~450 mm of GPIO tracks run
through the +3.3 V and GND zones**; after refill the +3.3 V net was in 11
pieces and "one track cut the regulator off from the ESP32" (esp32-route run,
BT-REVIEW, late addition 9). The RedBoard place+route agent's progress log has
an entry titled "the power layer came back in pieces" (step 007). The Glasgow
route-only review records shoved nets silently moved onto In1.Cu, the ground
plane (its review, item on layer drift). The agents' own fix is a loop the
tools should have had: route one link → apply on a copy → refill → compare the
net's group counts → keep or drop (esp32-route review) — i.e. a plane-integrity
guard around every bulk decision.

### 4. The model found impedance control on its own

The RedBoard route-only A brief never mentions impedance (verified: no
occurrence in that run's `brief.md`), yet its review reports: "About a third
of the board's copper (all controlled-impedance pairs, the QFN escape, buses,
regulator areas) is hand-computed and bt-checked" (redboard-route-bt-a run).
The ESP32-PoE-ISO agent likewise hand-computed Ethernet pairs, USB and RMII
lanes against isolation rules without being asked to care. The CM4 NAS is the
clearest case of all: its project carries one default net class and no
differential-pair rules, yet the route-only agent changed the stack-up on its
own — "outer layers 0.21 mm above their planes, which is what makes ~90 Ω
pairs possible at a sane width" (write-up) — added a differential-pair net
class, and kept In1 as unbroken ground under the pairs. Given a board and
its net classes, the model treats high-speed pairs as controlled-impedance
problems unprompted — it plans topology, lanes and skew itself and wants the
tool to check geometry, not to make design decisions.

## What the agents asked `bt` for

`bt` is unpublished, so this section is evidence about *what agents want from
a board tool*, drawn only from the published briefs and the agents'
`work/BT-REVIEW.md` files. Recurring asks, attributed to the review that made
them:

- **Check-then-apply as one atomic step, and probe must check proposed items
  against each other**, not only against the board: in the 2026-11-12
  OrangeCrab run (`orangecrab-bt-20261112-1613`), two hand-made stubs passed
  probe while violating each other and were caught only by KiCad DRC
  (orangecrab review, item 7). That was an earlier tool state: the later
  RedBoard placement run (2026-11-16, `redboard-place-bt-20261116-1017`)
  confirmed in its review that probe checked new items against each other, and
  its brief specified that a checked apply writes nothing when the probe fails.
  `bt apply --checked` and `bt probe` were the most-trusted commands in the
  study. → #9590
- **Plane protection as a routing cost, not an afterthought**: a zone-cost or
  keep-planes option, a warning when a route crosses another net's zone, and
  the route-refill-group-count loop built in (esp32-route review, late
  addition 9). → #9591
- **Router hygiene**: shove/rip must respect the allowed layer list and report
  which nets it moved (esp32 review, item 1 — the run was thrown away over
  this); greedy diagonals wall off neighbouring BGA targets (orangecrab item
  3); router-chosen vias land in other balls' escape lanes (item 4); batch
  JSON rejects CLI spellings with misleading errors (item 1); bulk mode stops
  at the first failed link (esp32 item 10); the default grid is too fine for
  long nets (esp32 item 2). → #9592
- **Footprint-local geometry as obstacles**: the router crossed net-less
  copper logos (KiCad `shorting_items`) and mishandled custom-shaped QFN pads
  (orangecrab item 6); the RedBoard review asks to "fix the footprint-outline
  case" first — that board's outline sits inside footprint H1. → #9593
- **Layout-quality metrics**: per-net/per-layer length reports (the only thing
  that exposed shove moving nets onto plane layers — esp32 item 1), lane
  capacity queries, via-near-pad search with rejection reasons, pair skew
  reporting and a skew-fix primitive. → #9594
- **A benchmark with published scoring** — which is exactly what this repo
  is. → #9595 (first board scored; see `pcbjam-blind-pcb-results.md`)
- Smaller recurring asks, recorded for whoever builds agent-facing tooling:
  a topology helper ("can these two pad groups be joined planarly; if not,
  which net crosses" — redboard-route-bt-b), QFN/QFP fan-out helpers, `bt rip`
  and zone edit/delete as first-class commands, `bt same` matching by UUID
  instead of reference, `bt near` time limits, incremental output and progress
  for bulk routing, and zone-refill-aware ratsnest counts.

## Finding-to-issue map

| Study finding | kct issue | What we would build |
|---|---|---|
| Early `bt` (OrangeCrab, 2026-11-12) probed only against the board; the later tool (RedBoard placement, 2026-11-16) checked proposed items against each other and a checked apply wrote nothing on a failed probe. How the unpublished tool handles a crash *during* a write is not established | #9590 | Propose the same check-before-write behaviour for `kct probe` batches and `kct apply --checked` |
| Bulk routing cuts planes; no zone-crossing cost or warning | #9591 | Router zone-split cost + `kct check` plane-integrity finding |
| Shove ignores layers; batch JSON traps; greedy ordering; grid scaling | #9592 | Audit `kct route` against ten reported failure modes |
| Net-less copper graphics, custom pads, footprint-local outlines ignored | #9593 | Footprint-level Edge.Cuts handled everywhere the outline is used |
| Agents wanted lengths, lanes, vias-near-pads, skew | #9594 | `kct` layout-quality metrics report |
| A scored external benchmark on real boards | #9595 — first board scored, see `pcbjam-blind-pcb-results.md` | Score the remaining eight boards with their briefs and scoring |

## Licence hygiene

`blind-pcb-runs` is MIT (`LICENSE`, Copyright 2026 Gergő Törcsvári): quoting
and paraphrasing its run material with attribution, as above, is fine. The
PCBJam article (pcbjam.com/blog/fable-blind-pcb-layout) is a separate work whose
licence scope the repository README does not extend to it: quote it sparingly,
mark each quotation "(write-up)", and prefer paraphrase. The repo's scoring method
and sandbox design are reusable with attribution. The **boards** keep their own
licences (CERN-OHL-P-2.0, CERN-OHL 1.2, 0BSD, WTFPL, MIT, Apache-2.0, and one
SparkFun board with no LICENSE file at the pinned commit) — fetch by their
recipe, never vendor. `PCBJam/pcbjam` (the WASM pcbnew port) is GPL-3.0:
subject to the clean-room policy (#9589), ideas-only in both directions,
nothing copied.
