# Does joint candidate selection help the negotiated router? (measured: no — and the metric is unsafe)

Issue #8998 (epic #5509 ordering class; sibling ideas #8999 / #9001 / #9003;
idea source [`weaveengine-topology-first-evaluation.md`](weaveengine-topology-first-evaluation.md)).
Companion to [`kct-route-net-order-experiment.md`](kct-route-net-order-experiment.md)
(ordering family, also no gain) and
[`kct-route-escape-flow-experiment.md`](kct-route-escape-flow-experiment.md).

**Verdict: the kill criterion fires.** Overflow entering rip-up did not drop on
any board the feature can reach: it **regressed on board 02** (25 → 49 entering
rip-up, 25 → 27 final) and **tied on the mains fixture (0 → 0) while the ON arm
failed the HV pairwise-clearance gate** (`/NEUT` vs `/LINE` copper at 0.400 mm
where 1.600 mm is required). Per the issue's own acceptance criterion
("no drop in overflow entering rip-up ⇒ report the null result, do not merge
the feature"), the feature was **not merged**; this note and the benchmark
script are the durable record. A prototype implementation existed on the issue
worktree long enough to produce every number below, then was removed.

Tool: `scripts/research/joint_candidate_selection_experiment.sh`
(`KCT_JOINT_CANDIDATE_SELECTION=1` is the A/B env arm; the prototype read it
inside `route_all_negotiated`).

## What the prototype did (as specified by the issue)

Opt-in `joint_candidate_selection` kwarg on `route_all_negotiated`, default
OFF, byte-identical OFF (verified against the pre-issue tree on a synthetic
board: identical copper, only wall-clock log lines differ). Before the
Iteration-0 loop it:

1. generated up to 3 corridor-distinct candidate route sets per multi-pad net
   on the not-yet-committed grid (commit-free dry-run re-runs of the existing
   per-net negotiated search; candidate *k* searched with a temporary
   `history_cost` bump along the previous candidates' cells — a pure COST
   term on both backends, unlike `usage_count` which flips passability),
2. scored candidates pairwise by shared-cell conflict,
3. picked one candidate per net jointly (local-improvement sweeps minimising
   `cells + 2.0 × pairwise shared cells`, ties by candidate index),
4. committed the picks during Iteration 0; the rip-up loop ran unchanged.

Deterministic (no RNG; same seed twice ⇒ identical copper — verified). All
cost bumps were exactly restored before Iteration 0.

## Results

Production recipes from `scripts/ci/board_route_determinism_smoke.sh` (02, 03,
04) and `boards/05-bldc-motor-controller/design.py` (05) / the mains fixture
README (mains). Deterministic-budget + seed flags; one run per arm.

| Board | Strategy path taken | Entering rip-up OFF → ON | Final overflow OFF → ON | Completion OFF → ON |
|---|---|---|---|---|
| 02 charlieplex (12 nets) | plain negotiated | **25 → 49** | 25 → 27 | 12/12 → 12/12 |
| 05 BLDC (recipe) | auto-layers → **two-phase** | not reachable | — | — |
| mains fixture (4 nets, ordering-sensitive) | plain negotiated | 0 → 0 | 0 → 0 | 4/4 → **ROUTING FAILED** (2 HV clearance violations) |
| 03 usb-joystick | auto → **two-phase** | not reachable | — | — |
| 04 stm32-devboard | auto → **two-phase** | not reachable | — | — |

Board 02, the picker's own log line: `conflict 34 -> 17 cells, length 4623 ->
4634` — the joint pick DID minimise its objective, and the committed result
still carried ~2x the overflow of the plain sequential process.

Mains ON, the fatal finding:

```
Joint candidate selection: 4 net(s) x up to 3 candidates; conflict 2 -> 0 cells
Routed 4/4 nets, overflow: 0 (11.6s)
ROUTING FAILED: pairwise HV clearance violations
  [pairwise] /NEUT vs /LINE: 0.400mm (required 1.600mm) at (131.600, 120.180)
  [pairwise] /NEUT vs /LINE: 0.400mm (required 1.600mm) at (131.600, 124.400)
```

## Why it loses (mechanism, from the evidence)

1. **Shared-cell conflict is the wrong conflict notion.** Two nets whose
   candidates pass through the same corridor in *adjacent* cells share zero
   cells and still violate clearance/creepage (mains: 0.400 mm vs 1.600 mm
   required). The sequential process cannot make this mistake: every net
   after the first sees the committed copper as passability obstacles, so
   legality is enforced by construction. Candidate generation on the empty
   grid loses exactly that guarantee, and a joint pick over cell overlap
   cannot restore it — a correct scorer would need the full pairwise
   clearance kernel (net-class-aware, HV pair maps) over every candidate
   pair.
2. **Empty-grid candidates are too myopic to beat sequential adaptation.**
   Every net's candidate 0 is its empty-grid favourite; on board 02 twelve
   favourites overlap heavily (baseline conflict 34 for the picker's
   hypothetical all-favourites assignment), and the sequential process —
   which lets later nets detour around *committed* copper — lands at 25
   overflow. Three penalty-diversified candidates per net only got the joint
   assignment down to 17 shared cells ⇒ 49 real overflow. The issue's
   premise ("net order alone decides who goes where") is true, but the
   sequential process already exploits information (commit-as-you-go
   feedback) that a decide-once joint assignment throws away.
3. **The addressable surface is narrower than the issue assumed.** Boards 03,
   04 and 05 auto-select Two-Phase (Global + Detailed) routing through their
   production recipes, so the plain `route_all_negotiated` entry point sees
   exactly one fleet production board (02) plus the mains fixture. Any
   future re-run of this idea must first answer "which boards would even
   execute it".

## What would be needed to re-open

Not recommended on this evidence, but for the record: a clearance-aware
pairwise scorer (reuse the commit-time clearance kernel, not cell overlap),
corridor diversity generated against *committed* state (sequencing the
candidates, which collapses into the #9001 ordering family), and a CLI-free
path into the two-phase boards. None of these change the board-02 mechanism
finding.

## Reproducing

```bash
scripts/research/joint_candidate_selection_experiment.sh /tmp/bench8998 02 mains
```

(Boards 03/04/05 print `not reachable` rows: their recipes route two-phase.
The prototype code itself is not in the tree; the script's ON arm is inert
without it — both arms then measure the same default path, which is itself a
useful self-check.)
