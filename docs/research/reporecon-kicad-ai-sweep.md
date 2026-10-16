# RepoRecon KiCad AI sweep (Issue #6749)

Sweep of `https://devbisme.github.io/RepoRecon/kicad_ai.json` (259 repos, fetched
2026-10-15) against the ecosystem registry (`kct ecosystem list`). Scope: every
repo at 5 stars or more that was not already tracked. The ~220 long-tail repos
below that line were not evaluated individually.

Method: the JSON was read once from a scratch directory and not vendored. Live
star counts, push dates and licenses came from the GitHub API, and each LICENSE
file was checked (the two `NOASSERTION` repos by reading the file). READMEs were
read; nothing was cloned or run. Stars are live values and differ from the
snapshot in places (the index lags).

## Already tracked

`mixelpixx/KiCAD-MCP-Server`, `aklofas/kicad-happy`, `copperheadhq/copperhead`,
`Seeed-Studio/kicad-mcp-server`, `oaslananka/kicad-mcp-pro`, and `mixelpixx/Konnect`
(the Konnect entry covers `neusse/konnect-codex`, below).

The issue listed `blwfish/kicad-mcp`, `traces` and `eve` as tracked. They were
not. `blwfish/kicad-mcp` and `usetraces/traces` now have entries. `abstring/eve`
has 2 stars and is AGPL-3.0, below the threshold, so it is not recorded.

## Verdicts

`ignore` is not a registry verdict; repos marked so have no registry entry.

| Repo | Live stars | License | `license_compat` | Verdict | Note |
|---|---|---|---|---|---|
| paul356/KiCad-AI-Assistant | 112 | MIT | mit-clean | watch | In-editor plugin with built-in MCP server |
| zxkmm/kicad-harness | 33 | AGPL-3.0 | copyleft-ideas-only | watch | Skill + `kh` tool, self-editing repo |
| blwfish/kicad-mcp | 25 | MIT | mit-clean | watch | End-to-end via Freerouting, 2,000+ tests |
| ProductOfAmerica/mcp-server-kicad | 13 | GPL-3.0 | copyleft-ideas-only | watch | 109 file-based tools, Freerouting |
| biosshot/kicad-copilot | 16 | MIT | mit-clean | watch | Local routing is KRT (already benchmarked) |
| belaszalontai/kipilot-mcp | 11 | Apache-2.0 | permissive-ideas-only | watch | IPC-based, live GUI |
| usetraces/traces | 6 | MIT | mit-clean | watch | Sourcing and semantic rule check |
| IxTechCrypto/kicad-skills | 6 | MIT | mit-clean | watch | Skills, same shape as `/kct:*` |
| jasiek/kicad-llm-plugin | 22 | MIT | mit-clean | watch | Schematic review only |
| thehengyao/iron-emperor | 11 | MIT | mit-clean | watch | LCSC-centred generator |
| nevenfo/kicad-agentic-mcp | 5 | AGPL-3.0 | copyleft-ideas-only | watch | Rust, days old; README not studied |
| devbisme/ai-circuit-design-tests | 6 | none | unlicensed | watch | Third-party bench, see below |
| 2262727886-stack/mspm0g-contest-skill | 320 | none | - | ignore | TI MSPM0G3507 / K230 car dev kit; KiCad files are incidental, no KiCad tooling |
| rjwalters/kicad-tools | 69 | MIT | - | ignore | This repo |
| FareedKhan-dev/ai-pc | 10 | proprietary | - | ignore | Windows desktop-automation agent, not KiCad-specific; all rights reserved |
| Finerestaurant/kicad-mcp-python | 41 | none | - | ignore | Last push 2025-07-15, unlicensed, IPC server superseded by kipilot-mcp / KiCad AI Assistant |
| 10-X-eng/KiChad | 22 | GPL-3.0 (KiCad fork) | - | ignore | A KiCad fork with a build environment; forking KiCad is a kct non-goal |
| neuroidss/create_function_chat | 7 | AGPL-3.0 | - | ignore | Last push 2025-01-29, generic LLM framework |
| swayerloren/KICAD_ENGINE | 9 | MIT | - | ignore | Prompts and task-router documents for an agent workspace; no tool surface to compare |
| walterkken/doc-driven-engineering-software-skills | 5 | proprietary | - | ignore | Generic engineering-software skills, all rights reserved |
| neusse/konnect-codex | 5 | AGPL-3.0 | - | ignore | Codex plugin for Konnect, already tracked |

Live stars for several repos exceed the snapshot's (for example `kicad-llm-plugin`
9 to 22, `kicad-copilot` 4 to 16); `biosshot/kicad-copilot` is below the 5-star
cut in the snapshot but above it live, so it is included.

## `devbisme/ai-circuit-design-tests`

A bench repo, not a tool. Each top-level directory holds the artifacts of one
tool run on a real design problem: `claude_transcript.txt`, an
`executive_summary.md`, and cost from `session_cost.py`. Tools are grouped as
generators (prompt in, design out), analyzers (design in, findings out) and
hybrids (tested in both roles). Generators are judged on correctness and
completeness; analyzers on whether findings are real and complete. Directories
at fetch time: copperhead, eda-agent, kicad-ai-assistant, kicad-happy,
kicad-harness, kicad-mcp-pro, kicad-mcp-server, Konnect and skidl-skills.

**kicad-tools is not tested there.** It would qualify as a hybrid. The README
invites outside evaluations as issues. Follow-up: #6766.

## Router and placer candidates

`biosshot/kicad-copilot` claims local routing and placement. Its router is
`eda-copilot-router` with a managed KiCadRoutingTools backend, which we have
benchmarked (`kicad-routing-tools-comparison.md`). Its placement is an LLM-driven
JavaScript DSL, not an algorithmic placer. Not benchmark-worthy. `blwfish/kicad-mcp`
and `ProductOfAmerica/mcp-server-kicad` route through Freerouting, also already
tracked. **No project in this sweep qualifies for a routing benchmark.**

## Follow-ups

- #6766: offer kicad-tools as a tool under test in `ai-circuit-design-tests`.
- No other project warrants a follow-up issue. The reeval triggers on each
  registry entry say what would change that.

## Re-running the sweep

The ignored rows above are exactly what the script still reports as untracked on the
2026-10-15 snapshot. `scripts/ecosystem_reporecon_diff.py` lists repos in a RepoRecon JSON at or above
a star threshold whose slug is not in the registry. It reports only; it never
edits `projects.toml` (registry rule 2).
