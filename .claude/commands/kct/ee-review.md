---
name: ee-review
invocation: /kct:ee-review
suggestedModel: opus
description: Produce an EE decision document (escalating intervention ladder + binding constraints, cited and confidence-graded) for an analog/placement-blocked board.
---

# EE Review

Produce an **EE decision document** for an analog/placement-blocked board — an *escalating intervention ladder* plus *binding electrical constraints*, cited and confidence-graded — so a builder can execute the fix artifact-first without waiting on the owner's calendar.

> **The `kct` namespace.** This skill lives in `.claude/commands/kct/` — the **kicad-tools-native** agent-tool namespace, invoked as `/kct:ee-review`. Keep kicad-tools' own agent tools here, *not* under `.claude/commands/loom/` or `.loom/roles/`: that tree is installed into this repo *from* [rjwalters/loom](https://github.com/rjwalters/loom) and belongs to the loom framework. The `kct` namespace is deliberately harness-agnostic — it hosts kicad-tools-native skills that live alongside installable harness frameworks (loom, [rjwalters/anvil](https://github.com/rjwalters/anvil), or any future orchestrator). Future kicad-tools skills should follow this convention: kicad-tools-native tools go under `.claude/commands/kct/`.

> **Scope (advisory only).** This skill produces **decisions, not copper**. It reads the board artifact, the `kct net-status --why` classification, the issue thread, and published/textbook/app-note evidence, then writes a single decision document (one issue comment in issue mode, or one `<board>/EE_REVIEW.md` in board mode). It **never routes copper, never places parts, never edits the `.kicad_pcb`, and never self-applies a work label.** The human approval gate is preserved: the owner ratifies the ladder + constraints before any builder executes them. Execution is the existing artifact-first builder pipeline's job, referee-gated by `kicad-cli pcb drc --refill-zones`.
>
> **Phase 1 (this doc) is a single opus-class reviewer.** It reads the inputs and writes the decision document directly. The Phase-2 research fan-out (4 sonnet researchers → opus planner) is *documented* here as the two protocols below, but Phase 1 does not spawn subagents and does not exercise the live paper-request / feature-request paths against paywalled sources. `--depth mvp` is the implicit default and the only supported mode in Phase 1.

## Model selection

Model resolves through the harness's normal precedence chain (explicit dispatch param → harness role config → this doc's frontmatter `suggestedModel` → session default).

- **Phase 1 (this doc): a single `opus`-class reviewer.** Synthesizing a binding EE decision is a judgment task where frontier capability pays off — the same reason the architect and judge roles use opus. `suggestedModel: opus` is set in the frontmatter above.
- **Phase 2 (not implemented here): opus planner + sonnet researchers.** The research fan-out (`--depth full`) would dispatch 4 `sonnet`-class researchers under distinct lenses (theory / app notes / papers / in-repo precedent) — breadth work cheap models do well — then one `opus`-class planner synthesizes their briefs into the decision document. Phase 1 collapses both stages into the single opus reviewer.

## Arguments

**Arguments**: `$ARGUMENTS`

`$ARGUMENTS` is `<issue-or-board> [--question "<free text>"] [--no-papers]`.

| Token | Meaning |
|-------|---------|
| `<issue-or-board>` | A GitHub issue number (e.g. `3766`) **or** a board path (e.g. `boards/05-bldc-motor-controller`). **Issue mode**: read the thread for the pending question + the latest `--why` table, write the decision as an issue comment. **Board mode**: derive the question from the live blocking nets, write the decision to `<board>/EE_REVIEW.md`. |
| `--question "<free text>"` | The specific EE decision to make. Overrides the default question derivation from the issue thread. Defaults to *"which parts may move + binding constraints for the blocked nets"*. Free text, e.g. `--question "is a snubber needed across Q5, and what R/C values"`. |
| `--no-papers` | Suppress the interactive paper-request protocol (fully autonomous / offline runs). Forces the open-access → app-note → textbook fallback chain immediately, downgrading confidence on any claim that would have relied on a paywalled paper. |
| `--depth mvp` | Implicit default and **only** supported mode in Phase 1 (single reviewer). `--depth full` (the research fan-out) is Phase 2 and is not implemented here. |

## Inputs (read-only, deterministic)

The reviewer gathers these before writing anything. All are read-only; the skill mutates nothing except the single output comment/file.

1. **The board artifact** — the committed `*.kicad_pcb` (e.g. `boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb`). This is the shipping truth for artifact-first work.
2. **`kct net-status <pcb> --incomplete --why`** — the `PLACEMENT_BOUND` / `CONGESTION_SATURATED` classification and per-net nearest-blocker geometry. **This is the measured ground truth** the decision reasons over. Every measured claim in the output must trace to a value here.
3. **The board `README.md`** — net classes, per-class trace widths / current ratings, layer stack.
4. **The issue thread** (issue mode) — prior build attempts, prior EE guidance, the pending question, and standing memory constraints ("analog parts need manual care").
5. **`kct check` / committed `drc_report.json`** — the current referee baseline the builder must not regress.

## Two protocols (agent instructions)

Phase 1 embeds both protocols as instructions but does not exercise the live paths in the acceptance test (no paywalled block is encountered). Follow them when the situation arises.

### (a) Feature-request protocol — missing programmatic capability

When a rigorous decision needs a metric kicad-tools does not yet compute — e.g. *"I need `kct` to report the parallel-run length between a sense net and the nearest high-current net,"* or *"I need a copper-loop-area metric to bound sense-loop pickup"* — **do not hand-wave and do not invent the number**. Instead:

1. File a properly-formed feature issue via `gh issue create` with a clear problem statement, the proposed capability, and *why this EE decision needs it*.
2. Label it `loom:architect` so it re-enters normal triage. **Never** self-approve to `loom:issue`.
3. Reference the filed issue number in the decision document, proceed with the **best available proxy**, and record the **confidence cost** of using the proxy in the affected claim's tag.

This keeps a recurring "I wish kct measured X" from becoming a buried caveat — it becomes a tracked feature.

### (b) Paper-request protocol — load-bearing paper unavailable

When a load-bearing claim would rest on a paywalled paper (IEEE / Elsevier) with no open-access copy, **never fabricate its contents**. Emit a structured ask to the user:

```
PAPER REQUEST (load-bearing, no open-access copy found)
  Title:  <exact title>
  DOI:    <doi>
  Venue:  <IEEE Trans. ... / etc>
  Why it matters: <the specific claim this paper would ground>
  Fallback if unavailable: <the open-access / app-note / textbook substitute used instead>
```

Then walk the **fallback chain**, downgrading the stated confidence of any affected claim at each step:

1. **Open-access preprint / author copy** (confidence: as-cited).
2. **Free manufacturer app note** — TI / ADI / Infineon (confidence: downgrade one level; tag `[literature: <vendor> <doc#>]`).
3. **Textbook derivation from first principles** (confidence: downgrade again; tag `[literature: <title> §<n>]`, show the derivation).

Under `--no-papers`, skip the interactive request and walk the fallback chain immediately. Every fallback records its downgrade in the confidence header. An unavailable paper becomes an explicit `[confidence: low, paper unavailable — substituted with <fallback>]` tag on each affected claim. **The skill NEVER summarizes or quotes a paper it has not seen.**

## Output template

Write the decision document with these sections **in this exact order**. In issue mode post it as a single issue comment; in board mode write it to `<board>/EE_REVIEW.md`. Modeled verbatim on the #3766 2026-07-09 "EE review complete" comment.

```
## EE review — <board-or-issue> (YYYY-MM-DD)

**Confidence: [high|med|low]** | Evidence basis: <1-line summary of sources used, noting any papers unavailable or claims degraded>

### Escalating intervention ladder
(stop at the first level that completes all target nets)

1. **[Least invasive]** <description>
2. **[...]** <description>
N. **[Most invasive]** <description>

### Binding constraints
- <Constraint> — [measured | <citation>]
- <Constraint> — [measured | <citation>]

### Rationale
<Reconciliation of measured facts vs literature guidance; separates measured-on-this-board facts
(from kct output, tagged [measured]) from literature guidance (tagged with source).
Each load-bearing claim carries its citation. Conflicts between sources are stated and resolved.>

### Method + referee handoff
Artifact-first on `<path/to/board.kicad_pcb>` (recipe unchanged). Referee: `kicad-cli pcb drc --refill-zones` 0 new errors + `kct check`. Builder executes the approved ladder top-down; stops at the first level that clears all target nets.
```

## Grounding rules (mandatory, enforced)

These are non-negotiable. A document that violates any of them is not a valid EE review.

- **Citations required for every load-bearing claim.** No bare assertions in the ladder or the constraints. Acceptable citation forms:
  - textbook — title + §/page (e.g. `Ott, Electromagnetic Compatibility Engineering §11.5`);
  - app note — vendor + doc number (e.g. `TI SLVA959`);
  - paper — DOI;
  - measured — `measured on this board via kct net-status --why @ <commit>`.
- **Measured-vs-literature separation.** Claims that come from `kct` output are tagged `[measured]` and cite the exact net + value. Claims from references are tagged with their source. A constraint like *"differential ≤0.5mm gap"* is literature-derived; *"ISENSE_A- nearest strict blocker 0.050mm"* is `[measured]`. The two must be visibly distinct in the Rationale.
- **Confidence header required, reflects the weakest link.** The overall confidence must reflect the evidence chain's weakest load-bearing step. A single degraded (paper-unavailable) load-bearing claim caps the header at that claim's confidence.
- **Never fabricate paper contents.** An unavailable paper becomes `[confidence: low, paper unavailable — substituted with <fallback>]` on each affected claim (see protocol (b)). Do not summarize or quote a paper you have not read.
- **Read-only.** Post exactly one comment (issue mode) or write exactly one file (board mode). Do not apply labels. Do not create issues (except via protocol (a), which files a `loom:architect` feature request — this is not exercised in the Phase-1 acceptance test). Do not edit the `.kicad_pcb`.

## Canonical worked example — `/kct:ee-review 3766`

This is the acceptance exemplar. Applying this skill to **#3766's pre-decision state** — the 2026-07-05 stall-guard comment (4 ISENSE nets blocked: 3× `PLACEMENT_BOUND` + 1× `CONGESTION_SATURATED`, `--why` blocker distances measured on the committed artifact) — must produce a document structurally equivalent to the real 2026-07-09 EE review comment. That comment is the ground truth.

**Measured input (from the #3766 issue thread `--why` table, the ground truth the reviewer ingests):**

| Net | `--why` class | Unconnected pads | Nearest strict blocker | Blocking nets |
|---|---|---|---|---|
| `ISENSE_A-` | PLACEMENT_BOUND | R12.2, U3.33, U3.44 | 0.050mm (34 obstructions) | ISENSE_A+, PHASE_A, GATE_AL |
| `ISENSE_B+` | CONGESTION_SATURATED | R11.1, U3.26, U3.30 | 0.250mm (boxed) | PHASE_A, ISENSE_A+, PWM_CL (1:1 trade) |
| `ISENSE_B-` | PLACEMENT_BOUND | U10.6, U3.31, U3.39 | 0.250mm (32 obstructions) | PHASE_A, GATE_BL, ISENSE_A+ |
| `ISENSE_C-` | PLACEMENT_BOUND | U10.7, U3.34 | 0.354mm (39 obstructions) | PHASE_A, GATE_CL, ISENSE_A+ |

The reviewer emits:

---

## EE review — #3766 board-05 BLDC current-sense (2026-07-09)

**Confidence: med** | Evidence basis: measured `kct net-status --why` on the committed artifact + Kelvin-sense / differential-pair layout theory (Ott EMC §11) + TI current-sense layout app notes; no paywalled paper required (fallback chain not triggered).

### Escalating intervention ladder
(stop at the first level that completes all 4 nets)

1. **[Least invasive] Sense-bundle re-dress (no part moves).** Rip all 6 ISENSE nets' copper and re-route them as one Kelvin-ordered bundle through the U3-south band. No footprint moves; nearest blockers are 0.050–0.354mm `[measured]`, i.e. tight but non-zero, so a coordinated re-dress may open the channel.
2. **[...] Re-dress non-analog copper.** `PHASE_A` + `GATE_AL/BL/CL` + `PWM_CL` may be re-dressed within the U3-south gap to widen the sense escape. These are the named blocking nets on the 4 ISENSE nets `[measured]`; moving *their* copper (not parts) is less invasive than moving parts.
3. **Move / rotate U10** toward U3's south-east so the codec's sense inputs (U10.6, U10.7 — the unconnected pads on ISENSE_B-/C- `[measured]`) face the band.
4. **[Most invasive] Move / rotate shunts R10–R12** (90° rotation / x-nudge; taps stay at-pad). R11.1 / R12.2 are unconnected pads on ISENSE_B+/A- `[measured]`; relocating the shunts is the last resort because it perturbs the sense network.

### Binding constraints
- Each ± pair routed **differentially, ≤0.5mm adjacent gap over the full run** — [literature: differential-pair matching, Ott *Electromagnetic Compatibility Engineering* §11.5 / Johnson & Graham *High-Speed Digital Design* §6].
- **Taps at shunt pad metallization only** (Kelvin connection at the pad, not on the approach trace) — [literature: Kelvin four-terminal sensing; TI current-sense layout app note].
- **No sense trace runs parallel to, or crosses, PHASE/high-current copper on the same layer** (perpendicular crossings on other layers OK) — [literature: magnetic-coupling / EMC, Ott §6]. `PHASE_A` is a named blocker on all 4 nets `[measured]`, so this rule directly binds the re-dress.
- Inner layers OK over the GND pour region; **vias adjacent to (not in) shunt pads** — [literature: Kelvin tap integrity + this board's GND-pour layer stack, board README].

### Rationale
The `--why` verdict is `PLACEMENT_BOUND` on ISENSE_A-/B-/C- and `CONGESTION_SATURATED` on ISENSE_B+ `[measured on this board via kct net-status --why @ committed artifact]` — i.e. not a targeted-copper fix but a "part must move" or "1:1 rip trade" call. Because the nearest strict blockers are non-zero (0.050mm on ISENSE_A-, 0.250–0.354mm on the others) `[measured]`, the ladder opens with the least-invasive re-dress rather than a part move: the measured geometry says there is *some* room. Literature governs *how* the re-dress must be done — Kelvin taps at-pad and ≤0.5mm differential gap (differential-pair / four-terminal-sensing theory) — because relocating or re-dressing sense copper without those rules would trade a routing win for a sense-accuracy loss, which is the exact reason the autonomous builders correctly punted (standing repo guidance: "analog parts need manual care"). The measured blocker identities (`PHASE_A`, `GATE_*L`, `PWM_CL` on every net `[measured]`) are what makes ladder step 2 (re-dress non-analog copper) viable before any part move. No paywalled paper was load-bearing here, so the confidence header is `med` (measured geometry + textbook/app-note theory), not low.

### Method + referee handoff
Artifact-first on `boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb` (recipe unchanged; regen does not reproduce the artifact). Referee: `kicad-cli pcb drc --refill-zones` 0 new errors + `kct check` (jlcpcb-tier1) + 45°-census. Builder executes the approved ladder top-down and stops at the first level that clears all 4 ISENSE nets.

---

**Why this passes the acceptance bar:** it reproduces the 4-level escalating ladder (re-dress bundle → re-dress non-analog copper → move U10 → move shunts R10–R12), the binding-constraint block (differential ≤0.5mm gap, at-pad taps, no parallel high-current copper on the same layer), the confidence header with an evidence-basis line, at least one `[measured]` citation naming a net and its nearest-blocker distance from the `kct --why` output, at least one verifiable literature citation (textbook § / vendor app note), and the artifact-first method + `kicad-cli pcb drc --refill-zones` referee handoff — matching the structure of the real 2026-07-09 comment.

## Limitations (Phase 1)

- **Single reviewer, no research fan-out.** The 4-lens researcher stage + planner synthesis is Phase 2 (`--depth full`). Phase 1 is one opus-class agent reading inputs directly.
- **Protocols documented, not live-exercised.** The feature-request and paper-request paths are embedded as instructions; the Phase-1 acceptance test does not encounter a paywalled block, so neither live path runs.
- **Advisory only.** Output is a proposal for human ratification, never an auto-merged mutation and never a self-applied work label.

## References

- **#3766** — the motivating case; the 2026-07-09 "EE review complete" comment is the acceptance-test ground truth; the 2026-07-05 stall-guard comment is the pre-decision input state.
- **#3906 / #3908** — the artifact-first builder runs that proved the execution half (PHASE tree 7→0) and stalled on the 4 ISENSE nets for lack of an EE ladder.
- `.claude/commands/loom/sweep.md` — model for skill-doc structure (Arguments → Inputs → Output → Grounding rules) and the "one level deep, never spawn `/shepherd`" subagent-dispatch safety rule the Phase-2 fan-out would reuse.
