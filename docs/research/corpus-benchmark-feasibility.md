# Corpus benchmark feasibility: capacity calibration and route-vs-human

*Point-in-time working document — issue [#4830](https://github.com/rjwalters/kicad-tools/issues/4830),
slice 4 of 4. Measured on 2026-08-16 against the 22 PCB entries of
`scripts/corpus/manifests/open-schematics-sample.json` (revision-pinned
`bshada/open-schematics` payloads) plus this repo's own 8 routed boards.*

Slices 1–3 answered *"can our parsers read real-world KiCad files?"* (yes:
30/30 manifest entries parse). This slice answers the question the two proposed
follow-on capabilities actually depend on: **is the corpus usable as benchmark
input at all?**

- **Capability A — capacity-predictor calibration** (feeds
  [#4799](https://github.com/rjwalters/kicad-tools/issues/4799)): needs, per
  board, a feature vector *and* a routability label.
- **Capability B — route-vs-human benchmark harness**: needs all of that plus
  reference copper to diff our router's output against.

## Verdicts

| Question | Answer |
|----------|--------|
| Is the corpus usable for capacity-predictor calibration? | **Yes, but the sample must be ~3× larger than the naive estimate.** Only **36%** of pinned PCBs (8/22) yield a feature vector *and* a positive label; the rest are blocked, mostly by one fixable parser gap. |
| Is a route-vs-human harness feasible? | **Yes — demonstrated end-to-end**, but not as a one-liner. Of 3 pilot boards, 1 routed out of the box, 1 crashed the router, 1 was refused by the grid-safety gate. The harness needs a per-board preflight, not just `strip_traces` + `kct route`. |
| Should either be built next? | **Build the harness first, on a handful of boards; defer predictor calibration.** The harness surfaced three generic library defects in three boards. Calibration needs hundreds of labeled boards, which is gated on the same parser gap. |
| Biggest single lever | **Legacy `(module …)` support.** It alone blocks 41% of the sampled corpus (9/22 boards), *all* of which carry copper. |

Reproduce every number below with:

```bash
uv run python scripts/corpus/check_manifest.py        # populate the payload cache once
uv run python scripts/corpus/benchmark_readiness.py   # the census, ~40 s, offline
```

---

## 1. What the census measures

`scripts/corpus/benchmark_readiness.py` scores each board against the
preconditions the two capabilities need, one blocker code per distinct
engineering action:

| Blocker | Meaning | Fix lives in |
|---------|---------|--------------|
| `no-pad-graph` | parsed, but zero footprints/pads came out | parser |
| `legacy-module-schema` | the diagnosable cause of most of the above: pre-KiCad-6 `(module …)` | parser |
| `no-net-binding` | pads exist, but no net has ≥ 2 pads | the design (not fixable) |
| `no-outline` | no Edge.Cuts bounding box with positive area | the design |
| `outline-not-polygonal` | bounding box fine, but `get_board_outline()` returns < 3 points | `schema.pcb` arc chaining |
| `no-reference-copper` | no copper at all — an unrouted *input*, never a reference | the design |
| `partial-reference-routing` | < 95% of multi-pad nets carry copper — too incomplete to score against | the design |

Two definitional choices matter more than the thresholds:

1. **A pour counts as copper.** A ground net served entirely by a filled zone
   has zero segments. Measured on this repo's own 8 routed boards, a
   trace-only definition scores 7 of them "partially routed" (`simple_led` at
   0.33!) and only 1 complete — versus 7/8 complete once pours count. Any
   harness that forgets this will mis-grade every board with a plane.
2. **Completion is measured at net granularity, and is optimistic.** A net
   counts as routed if it carries *any* copper, not if all its pads are
   connected. That makes the census a *screen*: a board it rejects is certainly
   unusable, a board it accepts still needs the harness to confirm.

## 2. Measured: the corpus (22 pinned PCBs)

```
featurizable (#4799)   : 10 (45.5%)
labeled examples       :  8 (36.4%)
route-vs-human cases   :  8 (36.4%)
  of which pour-served :  1

blockers                          boards
  legacy-module-schema                 9
  no-pad-graph                         9
  no-net-binding                       2
  no-reference-copper                  2
  no-outline                           1
  outline-not-polygonal                1
```

### 2.1 The dominant blocker is one parser gap

Nine of the 22 boards (41%) parse cleanly and yield **zero footprints and zero
pads**. Every one of them declares a pre-KiCad-6 file version (`3`, `4`,
`20160815`, `20170901`, `20171130` ×5) and uses the legacy `(module …)` token
where KiCad 6+ writes `(footprint …)`. Their nets, segments, vias and zones all
parse fine — 8 of the 9 carry copper (up to 5,024 segments) — so what is lost is
exactly the component graph both capabilities need.

This repo has **no version-reject path**: such a file is neither read nor
refused, it is silently half-understood. Two possible fixes, in increasing
order of value:

1. **Refuse** — raise on `(module …)` boards, so a caller learns the truth.
2. **Read them** — map `(module …)` onto `Footprint`. The corpus is heavy with
   KiCad-4/5 designs; supporting them roughly doubles the usable sample
   (8 labeled → an estimated 16, since 8 of the 9 have reference copper).

Either is generic library capability and neither is corpus-specific. This is
the highest-leverage follow-up this slice found.

### 2.2 The remaining blockers are properties of the designs

`no-net-binding` (2 boards) is real: one is a 15-footprint panel with no nets,
one is a 7-footprint mechanical board. `no-reference-copper` (2 boards) are
genuinely unrouted layouts — *useful as predictor inputs*, useless as
references. `no-outline` (1) has no Edge.Cuts at all. None of these are defects
on our side; a bigger sample simply has to expect ~20% attrition from them.

### 2.3 The usable slice is more diverse than the in-repo fleet

The 8 route-vs-human candidates span 20–825 pads, 6–209 routable nets, 2–4
copper layers, 5–223 cm², and 1.1–48.9 pads/cm². The densest (`os-0050425-pcb0`,
391 pads on 8 cm², 275 vias) is far outside anything in `boards/`, where the
median is 86 pads at 2.5 pads/cm². That density span is the point of using the
corpus at all — it is exactly the region where a capacity predictor has to be
right.

## 3. Measured: three route-vs-human pilots

The harness recipe is short — `PCB.load` → `strip_traces()` → `kct route` →
compare — and every piece already exists. Running it on three candidates
produced one comparison and two library defects:

| Board | Size | Human reference | `kct route` result |
|-------|------|-----------------|--------------------|
| `os-0007182-pcb0` | 20 pads, 6 nets, 5.2 cm², 2 layers | 22 seg, 0 vias, **26.9 mm** | **83% (5/6 nets)**, 36 seg, 1 via, **42.5 mm** (1.58× the human wirelength); `/GND` left 4/5 pads connected |
| `os-0003340-pcb0` | 73 pads, 15 nets, 8.4 cm² | 143 seg, 45 vias, 305.9 mm | **crash** — off-board preflight false-positives 25/47 footprints, then `ValueError: A linearring requires at least 4 coordinates` |
| `os-0009947-pcb1` | 110 pads, 19 nets, 9.6 cm², 11.4 pads/cm² | 183 seg, 45 vias, 295.5 mm | **refused** — auto-grid safety gate: 0.127 mm grid > clearance/2 (0.075 mm) under the default manufacturer profile |

Each failure is informative, and none is fatal to the idea:

- **`os-0003340-pcb0` — arc outlines.** Its Edge.Cuts is a rounded rectangle:
  4 `gr_line` + 4 `gr_arc`. `get_board_outline()` chains only the straight
  segments and returns **2 points**, which makes the off-board preflight
  reject two-thirds of the board and then kills the run inside shapely. This
  reproduces on a hand-written 8-primitive board (see
  `tests/test_corpus_readiness.py::…::test_rounded_outline_has_area_but_no_usable_polygon`) —
  it is a `schema.pcb` gap, not a corpus artifact, and it will hit any user
  with a rounded-corner board. The census now predicts it
  (`outline-not-polygonal`) instead of discovering it at crash time.
- **`os-0009947-pcb1` — imported rules vs. our profile.** The human routed to
  that board's *own* design rules; we applied the default manufacturer profile
  and the grid-safety rule (correctly) refused rather than emit shorts. Raising
  `--max-cells` to 4 M does not change the verdict. A harness must adopt each
  board's `(setup …)` clearance/track-width — comparing our router at
  JLCPCB-tier rules against a human at their own rules is not a comparison.

**What the one successful pilot already tells us**: on a trivially small board
our router completes 5/6 nets and spends 1.58× the human's copper length. That
is the first time this repo has measured itself against human-authored copper
at all, and it took ~4 minutes of compute.

## 4. Recommendation

Build **Capability B first, scoped small**; defer **Capability A**.

**Why B first.** The harness pays for itself immediately: three pilot boards
produced three generic findings (arc outlines, rule import, a real wirelength
ratio). It needs no labels, no statistics, and no bigger corpus — a
`kct bench route-vs-human <board>` over 5–10 pinned boards is a complete,
useful increment. And its per-board preflight is what makes A trustworthy
later.

**Why A is not ready.** A predictor calibrated on 8 boards is a curve fit, not
a calibration; and the path to hundreds of boards runs straight through the
legacy-`(module …)` gap plus a much larger manifest. #4799's own scope note
already defers corpus calibration to "future work" — this slice confirms the
sequencing with numbers.

Concrete follow-up issues to file (all generic library capability):

1. **`(module …)` boards: read or refuse.** Unblocks 41% of the sampled corpus.
   Include a `kct check`-visible signal so a user is never silently handed an
   empty component graph.
2. **Chain arcs into `get_board_outline()`.** Rounded-rectangle outlines are
   ordinary; today they yield a 2-point polygon, a false off-board preflight
   failure, and a shapely crash inside `kct route`.
3. **Import a board's own design rules when routing an imported board** (or a
   `kct route --rules-from-board` flag). Prerequisite for any fair comparison
   against third-party copper.
4. **`route-vs-human` benchmark command** — strip, route, and report the
   OmniRouting-style metric set (completion, DRC-attributed completion,
   wirelength ratio, via ratio, runtime), reusing the metric vocabulary adopted
   in [`omnilayout-recon.md`](omnilayout-recon.md) §5.
5. *(Later, gated on 1)* **Grow the manifest to 100+ labeled boards** and
   revisit predictor calibration for #4799.

## 5. Scope and constraints honored

- No corpus payload is committed. The census reads the gitignored
  `scripts/corpus/.cache/`, which `check_manifest.py` fills from pinned URLs +
  `sha256`; pilots wrote stripped boards to a scratch dir outside the repo.
- The census is offline and does no network I/O; it is not referenced from any
  workflow (asserted in `tests/test_corpus_readiness.py`).
- Attribution: contains data from the *Open Schematics* dataset by bshada
  (<https://huggingface.co/datasets/bshada/open-schematics>), used under
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Individual boards
  carry their upstream projects' own licences; nothing from the corpus is
  redistributed here.
