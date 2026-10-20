# Evaluation: devbisme/ai-circuit-design-tests — should kicad-tools be offered as a tool under test?

**Issue**: #6766 (follow-up from #6749, RepoRecon KiCad AI sweep)
**Date**: 2026-10-15
**Upstream**: https://github.com/devbisme/ai-circuit-design-tests, read at commit `98ac08075ca243cc09febd4d501ca60521640e34` (committed 2026-10-13)
**Upstream license**: none (no LICENSE file). Treated as **ideas-only**: everything below is paraphrased, nothing is copied from its prompts or summaries.
**Registry entry**: deliberately not added. PR #6767 (which shows how bench-type, non-tool projects are recorded) was still open when this note was written.

## TL;DR

The bench is a credible, independent place to get a comparable data point on `kct`. **Recommendation: offer, but as an analyzer run first** (and a generator run only with the honest caveats below). The cost to us is small, since the maintainer runs everything and we only supply setup notes. The risk is a public transcript of `kct` falling short on a full-board design prompt, which is also the most useful kind of failure report. Posting is an operator step; this note only prepares it.

## What the bench is

A single maintainer runs AI KiCad tools, driven by Claude Code, on a real design problem. Each tool gets a directory holding the artifacts of its runs, the session transcript, an equivalent-API-cost figure, and a per-run executive summary. Tools are classed three ways:

- **Generator**: turns a prompt into a design (specs, BOM, schematic, sometimes a board). Judged on correctness and completeness.
- **Analyzer**: reviews an existing KiCad project and reports findings. Judged on whether findings are real, relevant and complete. It runs on a design a generator produced.
- **Hybrid**: tested in both roles, one run each.

At the commit read, nine tool directories exist (copperhead, eda-agent, KiCad-AI-Assistant, kicad-happy, kicad-harness, kicad-mcp-pro, kicad-mcp-server, Konnect, skidl-skills). kicad-tools is absent. The README invites outside evaluations as issues on the repo.

### Run structure

- One directory per tool (`<tool>-tests/`) with a `setup.md` (install and configuration), a README, and numbered run directories that are never overwritten.
- Each run captures the verbatim prompt, everything the tool wrote, a Claude session transcript, a cost file produced by a bundled script that sums main plus subagent usage, and an `executive_summary.md` the model writes at the end because the design prompt demands it.
- The maintainer has not validated any design for correctness yet. Reported outcomes (ERC/DRC counts, "routed 4-layer board") are as stated by the session.

### The shared design prompt

Paraphrased: a two-channel oscilloscope-style digitizer, inputs of about ±10 V, 10 MS/s at 12 bits, at least 0.1 s of capture at full rate, scope-lead-compatible connectors, a USB 2.0 host link that also supplies power (USB-C if the budget is insufficient). The agent must list options and pick one without pausing for input, must start from a blank slate (no reuse of other designs in the tree), and must write an executive summary. A second prompt asks for analysis of an existing design. Cross-tool comparison is possible because seven generator-style runs share the prompt and six analyzer runs share one input design.

Typical outcome in the one executive summary read (skidl-skills run 8): a schematic of roughly 170 parts with ERC clean, a six-phase agent pipeline, an equivalent API cost in the mid-$20s, explicitly **not** laid out. Other tools report routed 4-layer boards in the $10–$60 range.

## What a `setup.md` equivalent must contain

From the existing ones: prerequisites with exact versions used (runtime, KiCad, `kicad-cli` on PATH), the install commands, how to point the tool at the model backend, how the tool is registered with Claude Code (MCP or CLI), and a smoke test. For `kct` that means:

- Python 3.10+ and `uv`; KiCad 8+ with `kicad-cli` on PATH (ERC/DRC/export cross-gates need it; the pure-Python `kct check` does not).
- `uv sync`, then `uv run kct build-native` (the C++ router extension is not built by `uv sync`; without it routing is 10-100x slower).
- `uv run kct build-native --check` should report the C++ backend available.
- Entry points: the `kct` CLI (JSON output, no running KiCad needed) and `kct mcp serve` / `kct mcp setup --client claude-code` for MCP registration.
- `uv run kct agent-guide` / `kct skills` for the agent-facing workflow notes.
- Versions pinned for the run (kct version, commit SHA, KiCad version).

## What `kct` can and cannot do on this design prompt (honest)

**Can do (strong fit)**

- Analyzer role: `kct check` (pure-Python DRC), `kct erc`/`kct drc` wrappers, `kct audit` and `kct readiness` (manufacturing readiness), `kct analyze`, connectivity and net-status reports, `kct creepage`, impedance checks, and an LVS-style schematic-to-PCB comparison. Findings are structured JSON, which suits an LLM reviewer.
- Placement, routing (native A* with C++ backend), zones, stitching, DRC repair, fab export bundles at a chosen manufacturer tier.
- Parts lookup (LCSC) and datasheet search for the BOM step.

**Cannot do or weak (name the gaps)**

- **No requirements-to-architecture stage.** `kct` is a toolkit, not a design agent. Block selection (ADC, front-end buffering, USB bridge, FPGA or MCU choice, power tree) is left to whatever model drives it. In the other runs that is where most of the cost and risk lives.
- **Analog front-end and high-speed capture design** (±10 V attenuation, anti-alias filtering, 10 MS/s parallel ADC data paths, USB 2.0 high-speed with controlled impedance) has no dedicated synthesis support; we can check but not derive.
- **Schematic capture from scratch is the thinnest area.** There is a schematic-editing and spec/pipeline surface, but symbol and footprint availability for a 170-part design is the likely failure point. This should be expected and reported, not hidden.
- **Blank-slate rule.** The prompt forbids reusing other designs; `kct` project templates and pattern libraries are fair game as tool features but the transcript may show the agent leaning on them, which the maintainer may or may not count.
- A full end-to-end generator run is likely to cost real money and may stop short of a routed board. Cost per run is published by the bench, so a poor showing is public.
- Cross-run comparability depends on the maintainer's choice of driver model and any added line giving the KiCad executable path.

## Recommendation: offer (analyzer first, generator optional)

Reasons to offer:

1. Independent, comparable measurement on a shared design with a shared input for analyzers, which we cannot easily produce ourselves.
2. Free source of failure transcripts on a hard, realistic prompt.
3. Analyzer runs are cheap in the bench's own numbers (about $2–$4 each) and `kct` is strongest here.
4. Maintainer effort is low: setup notes plus a one-line invocation.

Reasons for caution:

1. A generator run on the full prompt will probably expose the capture and architecture gaps above. That is acceptable, but the offer should say so up front rather than over-claim.
2. We do not control the driver prompt or model, so results reflect the maintainer's harness as much as `kct`.
3. The bench has no license; we only read and cite, never copy, and should not place its material in this repo beyond this paraphrased note.

Suggested ask: an analyzer run on the existing shared input design, and (optionally) a generator run under the blank-slate prompt with `kct` MCP registered.

## DRAFT — not posted

> Posting this is an operator step. Do not create an issue or comment on `devbisme/*` from automation.

**Title**: Offer: evaluate kicad-tools (`kct`) as an analyzer and generator tool

**Body**:

> Hi, and thanks for running this bench. We would like to offer **kicad-tools** (https://github.com/rjwalters/kicad-tools, MIT) as a tool under test.
>
> **What it is.** A Python toolkit that lets an AI agent parse, check and modify KiCad schematics and PCBs without a running KiCad GUI: pure-Python DRC, ERC/DRC wrappers, manufacturing-readiness audit, connectivity and LVS-style checks, a native router (C++ backend), placement, zones, and fab exports. Output is JSON. It is a toolkit rather than a design agent, so we suggest classing it as an **analyzer**, and as a **hybrid** if you run it on the generation prompt too.
>
> **Honest expectations.** It is strongest on checking and finishing boards. It has no architecture or part-selection stage, so on the dual-ADC prompt the driving model has to supply the design decisions and we expect gaps in schematic capture for a design of that size.
>
> **Setup notes** (for a `setup.md`):
> - Python 3.10+, `uv`, KiCad 8 or newer with `kicad-cli` on PATH.
> - `git clone https://github.com/rjwalters/kicad-tools && cd kicad-tools`
> - `uv sync`
> - `uv run kct build-native` (builds the C++ router; `uv run kct build-native --check` should report it available)
> - CLI entry point: `uv run kct --help` (every command can emit JSON)
> - MCP entry point: `uv run kct mcp setup --client claude-code`, or run `uv run kct mcp serve`
> - Agent-facing guide: `uv run kct agent-guide`
>
> **Suggested runs.** (1) Analyzer: point it at the project from your shared-input analysis runs and ask for a design review. (2) Optional generator: the dual-ADC prompt with the MCP server registered.
>
> We are happy to supply a pinned commit SHA and answer setup questions. If useful we can also review any `kct` transcript for tool misuse so failures are attributed correctly.

## Next steps (operator)

- Decide whether to post the draft above. **Posting to `devbisme/*` is the operator's step, not automation's.**
- If PR #6767 has merged and records bench-type projects in the registry, consider adding an ideas-only entry (`license_compat` = ideas-only) and re-rendering with `uv run python scripts/ecosystem_render.py --write`.
