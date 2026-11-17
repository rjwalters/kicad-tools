# Konnect item 8 audit — natural-language design-rule store — 2026-09

**Issue:** #4902 (item 8 of #4880's 13-item Konnect audit)
**Verified at:** `7ad470f7` (2026-09-10)
**Scope:** read-only with respect to `src/`. This is an audit/decision
deliverable, not a code-change PR — see the Verdict section for why no
implementation is bundled.

> **Citation style — symbol anchors, not line numbers (repo policy #4764,
> `tests/test_docs_source_citations.py`).** Every claim below is anchored on a
> **symbol + file path**, not a line number, because `docs/*.md` is a policed
> glob and line citations rot silently on refactor
> (`docs/placement-pad-anchoring-audit.md` rotted three times in eight days
> before this policy existed). Verify any claim with:
> ```bash
> rg -n '<symbol>' <path>
> ```

---

## 0. Question this audit answers

Konnect (a 203-tool KiCAD MCP server, AGPL-3.0, ideas-only reference — see
Konnect source note below) layers config as: user config →
`.konnect/project.json` project overrides → a free-text rule store
(`add_design_rule("Always use 100nF X7R within 3mm of power pins")` /
`list_design_rules`) the LLM is told to honor on every edit.

**Does kicad-tools need a fifth mechanism to reproduce this shape, or does one
of its existing four-plus already cover it?**

**Verdict: DECLINE.** See §5.

---

## 1. Inventory of existing mechanisms

Five separate, only-loosely-connected pieces of the "declarative
project-level rule a tool should honor" design space already exist. The
issue's curation pass identified four; verifying the tree surfaced a fifth
(§1.5) that turns out to be the load-bearing one for the decline verdict.

### 1.1 `DesignIntent.constraints` — free-text list, verified inert

`DesignIntent` (`src/kicad_tools/spec/schema.py`) declares:

```python
constraints: list[str] | None = Field(
    default=None, description="Key constraints driving the design"
)
```

on `ProjectSpec.intent` (same file). This is the closest **shape** match to
Konnect's `add_design_rule` — an unstructured string list.

**Verified inert.** `rg -n "constraints" src/kicad_tools/spec/parser.py`
returns zero hits: the field round-trips through YAML load/save (via the
generic Pydantic model, not a dedicated read path) but nothing ever
constructs, validates, or displays it.
`rg -n "spec\.intent" src/kicad_tools/cli/commands/spec.py` shows only
`spec.intent.summary` is read, in `_run_spec_status` and its JSON twin
`_status_payload` — `.constraints` is dead data once written. There is no
CLI verb to write it either (`kct spec` subcommands are `init`, `validate`,
`status`, `decide`, `check` — dispatched in `run_spec_command`; no
`add-constraint`).

### 1.2 `kicad_tools.intent` package — typed constraint derivation, not prose

`src/kicad_tools/intent/{types,registry,constraints,protocol}.py` +
`intent/interfaces/{i2c,power,spi,usb}.py` is a **typed**, not free-text,
system: `derive_constraints` / `validate_intent`
(`src/kicad_tools/intent/constraints.py`) look up a registered interface
spec and derive typed `Constraint` objects (`src/kicad_tools/intent/types.py`)
consumed by predictive DRC during placement — `src/kicad_tools/drc/predictive.py`
has `for constraint in intent.constraints:` dispatching on `constraint.type`
(`length_match`, `differential_pair`, ...). This is a structured
constraint-derivation engine scoped to interface/net-level electrical intent
(USB2 length matching, I2C pull-ups, differential pairs), not general design
guidance ("always use X7R caps near power pins").

**Verified gap, not this issue's to fix.** The MCP-facing surface —
`declare_interface`, `declare_power_rail`, `list_intents`, `clear_intent`
(all in `src/kicad_tools/mcp/tools/session.py`, fully implemented and
docstring-complete, exercised by `tests/test_mcp_session_intent.py`) — has
**zero registrations** in `src/kicad_tools/mcp/tools/registry.py`:
`rg -n "declare_interface|declare_power_rail|list_intents|clear_intent"
src/kicad_tools/mcp/tools/registry.py` returns no matches, whereas the
sibling session-lifecycle tools `start_session`, `query_move`, `apply_move`,
`undo_move`, `commit_session`, `rollback_session` are all registered there
(each has a `_handler_*` wrapper + a `name=` / `handler=` registration
block). An MCP client cannot call `declare_interface` or `list_intents`
today even though the implementation is complete. Flagged as a follow-up
(§6), not fixed here — fixing it does not touch the free-text rule-store
question this issue is scoped to.

### 1.3 Manufacturer profiles — layered config, but numeric not prose

`src/kicad_tools/manufacturers/base.py` declares `DesignRules` and
`load_design_rules_from_yaml`, backed by per-manufacturer YAML in
`src/kicad_tools/manufacturers/data/*.yaml` (e.g. `jlcpcb.yaml`) and
generated `.kicad_dru` files in `src/kicad_tools/manufacturers/rules/`. The
resolution precedence is implemented in `kct check`
(`src/kicad_tools/cli/check_cmd.py`, `_resolve_effective_check_mfr`):
explicit `--mfr` > auto-discovered `fab_profile.json` sidecar (written by
`kct route` via `_write_fab_profile_sidecar` in
`src/kicad_tools/cli/route_cmd.py`) > `project.kct` `target_fab`
(`resolve_target_fab_for_pcb` in `src/kicad_tools/sync/discover.py`) >
default. This **is** Konnect's "user config + project override" layering,
already implemented and load-bearing — but scoped to numeric/geometric fab
constraints (clearances, trace width), not natural-language guidance.

### 1.4 Recipe-embedded intent — code, not declarative data

Board recipes (`generate_design.py` per board, scaffolded by
`/kct:board-recipe-scaffold`, `.claude/commands/kct/board-recipe-scaffold.md`)
encode design intent as **code** (fill-in points in a nine-step pipeline
function). There is no data-driven design-rule file a recipe reads from.

### 1.5 `project.kct` `decisions:` — the mechanism the curation pass missed, and the one that settles this

**Not identified in the issue's own curation pass; found verifying the
tree.** `kct spec decide` (`_run_spec_decide` in
`src/kicad_tools/cli/commands/spec.py`) appends a `Decision`
(`topic`, `choice`, `rationale`, `date`, `phase`, `alternatives`, `author` —
`src/kicad_tools/spec/schema.py`) to `project.kct`'s `decisions:` list via
`append_decision` (`src/kicad_tools/spec/parser.py`) — a deliberately
byte-preserving splice, not a full rewrite, so comments and unrelated keys
survive. Unlike `constraints`, this is **not inert**: `_run_spec_status`
displays the three most recent decisions, and `_status_payload` includes the
full list in the JSON form of `kct spec status`.

This is functionally the closest analogue to Konnect's `add_design_rule` /
`list_design_rules` pair that exists in the repo today: an appendable,
rationale-bearing, natural-language project record with both a write CLI
verb and a read surface.

**More importantly, the repo has already litigated and settled this exact
question**, for a directly adjacent case, in
`.claude/commands/kct/board-recipe-scaffold.md`'s "Design memory" section:

> "A recipe records *how* the board was produced. It does not record **why**
> the material choices were made... Record a decision whenever a **material**
> choice is made — a topology, a part family, a fab tier, a stackup, a
> placement or routing constraint that a future reader could reasonably
> second-guess... **Do not invent a fifth artifact for this.** `project.kct`
> `decisions:` is the durable design-decision surface."

`.claude/commands/kct/layout-journal.md` disambiguates the three
natural-language "why" surfaces the repo has settled on (`LAYOUT_NOTES.md`
per-session journal; `project.kct` → `decisions:` for durable material
decisions; `<board>.kicad_pcb.decisions.json` for machine-recorded
router/optimizer rationale, queried via `kct decisions`) and states the rule
of thumb for which to use. A Konnect-style rule store would be a **sixth**
surface in a space the repo has already worked to keep at three (plus the
two structured mechanisms in §1.2/1.3), explicitly on the stated principle
of not multiplying artifacts for the same "why" question.

**Semantic gap, honestly noted.** `decisions:` is a past-tense record
("we chose X because Y"); Konnect's `add_design_rule` is a forward-looking
imperative ("always do X going forward"). A decision entry like
`--topic "Decoupling" --choice "100nF X7R within 3mm of every power pin"
--rationale "..."` captures the same information but reads as a historical
note, not a standing directive an LLM is told to check against on every
future edit. This gap is real but narrow (§5 addresses it) — the existing
surface already carries the content; it doesn't yet reformat it as a
checklist to actively enforce.

---

## 2. Verification commands

```bash
rg -n "constraints" src/kicad_tools/spec/parser.py
# expect: no hits — constraints is never read

rg -n "spec\.intent" src/kicad_tools/cli/commands/spec.py
# expect: only spec.intent.summary, in two places

rg -n "declare_interface|declare_power_rail|list_intents|clear_intent" \
  src/kicad_tools/mcp/tools/registry.py
# expect: no hits — these MCP tools are unregistered

rg -n "start_session|query_move|apply_move|undo_move|commit_session|rollback_session" \
  src/kicad_tools/mcp/tools/registry.py
# expect: multiple hits per name — the sibling tools ARE registered

gh issue list --search "declarative placement-constraint" --state all
gh issue list --search "declarative constraint spec" --state all
# expect: no open issue besides #4831 itself (closed) — see §4
```

---

## 3. Konnect source (reference only, not ported)

Konnect's `.konnect/project.json` layered config and
`add_design_rule` / `list_design_rules` skill are referenced for their
*mechanism* only. No Konnect code, prompts, or schema text is reproduced or
adapted in this document or any change proposed by it — Konnect is
AGPL-3.0, this repo is MIT, per the issue's own constraint.

---

## 4. Relationship to #4831 item 4

#4831 item 4 ("declarative placement-constraint spec": `near`/`far`/`group`/
`keepout`/`fixed`, pad-addressed) was **never spun out as its own issue** —
re-verified via `gh issue list --search "declarative placement-constraint"`
/ `"declarative constraint spec"` (2026-09-10): only #4831 itself (closed,
its item-1 pad-anchoring-audit scope landed via PR #4883) mentions it; items
2-5, including item 4, remain un-filed stubs in #4831's closed body
(`docs/placement-pad-anchoring-audit.md` §6 M3 records the same fact from
the placement side). **There is no live issue to defer to.**

The overlap is real but narrow: #4831 item 4 is scoped to **placement
geometry** (pad-to-pad distance/grouping/keepout during placement); this
issue is scoped to **general design guidance** (component selection,
decoupling practice, manufacturing preference) that need not be geometric at
all. If #4831 item 4 is ever filed, it and this issue's declined store
would share a *rule-storage* mechanism (both are "declarative project-level
statements an LLM should honor") but very likely need different
vocabularies — typed `near`/`far`/`group`/`keepout`/`fixed` with pad
addressing for placement, vs. free-text or lightly structured guidance
(exactly `project.kct` `decisions:`, per §1.5) for design intent. This is
noted rather than deferring the whole issue to #4831 item 4, since that
issue does not exist to defer to.

---

## 5. Verdict: DECLINE

A dedicated Konnect-style natural-language design-rule store is declined.
Rationale:

1. **The repo already has five overlapping mechanisms in this space**
   (§1.1-§1.5), not the four the curation pass identified — adding a sixth
   duplicates territory rather than filling a gap.
2. **The repo has already articulated and applied the "no sixth artifact"
   principle to the nearest adjacent case** (§1.5): board-recipe design
   memory is explicitly routed to `project.kct` `decisions:`, with a
   disambiguation table (`layout-journal.md`) drawn specifically to stop
   proliferation of "why"-recording surfaces. A new rule store would
   contradict that stated convention without a distinguishing reason.
3. **The one honest semantic gap — past-tense decisions vs. forward
   imperative rules — does not require new storage**, only a read-side
   reframing: an LLM-facing surface that reads `intent.constraints` (or
   `decisions`) at session start and presents them as "things to keep
   honoring," rather than a new writable store. That is a smaller, safer
   change than a new mechanism, and is captured as a follow-up (§6.2)
   rather than bundled here because it needs its own design pass (which
   entries count as "standing rules" vs. one-off historical notes is a real
   product decision, not a mechanical wiring fix like §6.1).
4. **The real gaps are activation gaps, not storage gaps**: `constraints`
   is inert (§1.1) and the typed intent MCP tools are unregistered (§1.2).
   Both are small, well-scoped, independently valuable fixes that this
   audit recommends as separate follow-up issues (§6) rather than folding
   into a new rule-store feature.

This verdict does not depend on #4831 item 4 (§4) — it is grounded entirely
in mechanisms that exist in the tree today.

---

## 6. Follow-up issues to file

Per the decline path, two follow-ups are recommended and have been filed
as `loom:triage` issues (not self-claimed, per the decomposition
playbook's "file, don't self-claim" rule):

### 6.1 Register the unregistered intent-declaration MCP tools — filed as #5102

**Title:** `mcp: register declare_interface/declare_power_rail/list_intents/clear_intent in the tool registry`
**Scope:** `src/kicad_tools/mcp/tools/session.py` already implements and
tests these four tools; `src/kicad_tools/mcp/tools/registry.py` never wires
them in (compare the `start_session` family, which is registered). Add the
`_handler_*` wrappers + registration entries mirroring the existing
session-tool pattern so an MCP client can actually call them.

### 6.2 Surface `project.kct` design intent into LLM/MCP context — filed as #5103

**Title:** `spec+mcp: surface project.kct intent.constraints and decisions as standing design guidance for LLM sessions`
**Scope:** Neither `DesignIntent.constraints` (dead — §1.1) nor
`project.kct`'s `decisions:` log (live but only shown by `kct spec status`
text/JSON — §1.5) is surfaced into MCP session context, so an LLM agent
editing a board today has no way to see prior human-recorded constraints or
material decisions without a human re-pasting them. Add a minimal read path
(e.g. an MCP tool returning `intent.constraints` plus the `decisions` list
for the active `project.kct`, called at session start) and, if `constraints`
is to be made non-inert as a write path too, a `kct spec add-constraint`
CLI verb. No schema changes needed — keep `constraints: list[str]` as-is.
This is the natural home for the "forward-looking rule" reframing noted in
§5.3, and should design that framing explicitly rather than treating it as
a pure wiring fix.

---

## 7. Stub index

| # | Filed as | Title | Depends on |
|---|---|---|---|
| 6.1 | #5102 | `mcp: register declare_interface/declare_power_rail/list_intents/clear_intent in the tool registry` | None — pure registration, mirrors existing pattern |
| 6.2 | #5103 | `spec+mcp: surface project.kct intent.constraints and decisions as standing design guidance for LLM sessions` | None — independent of 6.1 |

Related documents: `docs/placement-pad-anchoring-audit.md` (the audit-doc
precedent this file follows) and
`.claude/commands/kct/board-recipe-scaffold.md` /
`.claude/commands/kct/layout-journal.md` (the design-memory convention this
verdict leans on).
