# Agent surfaces: what each `kct` skill needs from its harness

kicad-tools reaches agents through three surfaces: the `kct` CLI (`--format json`),
the MCP server (`kct mcp serve`), and the `kct` skills. The CLI and MCP server
behave the same in every harness. The skills are prose instructions, so they
must not assume one harness either (issue #5954, Epic #5952). This page lists
what each skill needs from the harness running it, and the rules that keep the
skill text neutral.

## Per-skill harness requirements

Every skill needs the same baseline: **run shell commands** (`kct ...`, usually
via `uv run`) and **read files** in the consumer repo. The table lists what each
skill needs beyond that.

| Skill | Extra shell tools | Writes | Asks the user | Network / forge | Helper agents | Claude Code only |
|---|---|---|---|---|---|---|
| `help` | `ls`, `cat` | nothing (strictly read-only) | no | no | no | no |
| `ee-review` | `kct net-status --why`, `kct check`; `gh` in issue mode | one issue comment (issue mode) or `<board>/EE_REVIEW.md` (board mode); optionally files one `gh issue create` feature request | paper-request protocol (skipped with `--no-papers`) | GitHub via `gh` (issue mode); literature lookup, with an offline fallback chain (app note, then textbook derivation) | Phase 2 fan-out only, which is not implemented | Phase 2 fan-out only; marked note with a sequential fallback. Phase 1 (the only implemented mode) has no Claude-only step |
| `manufacturing-readiness` | `kicad-cli pcb drc --refill-zones`, `kct check`, `kct export`, `kct audit` (HV boards) | export bundle in `--output <dir>` | fab tier, when the recipe/manifest does not name one | no | no | no |
| `tapeout` | as `manufacturing-readiness`, plus `kicad-cli sch/pcb export pdf`, `kct render` | full fab bundle, `README.txt`, manifest | fab tier; selection for ambiguous BOM matches | parts matching for BOM resolution (refuses rather than shipping an empty BOM when unavailable) | no | no |
| `hv-isolation-loop` | `kct creepage`, `kct zones hv-keepout`, `kct optimize-placement --voltage-map`, `kct audit`, `kicad-cli pcb drc --refill-zones` | the board, through the documented mutating `kct` commands | the human EE authors the voltage map and ratifies before fab | no | no | no |
| `board-recipe-scaffold` | `uv run python`, `kct check`, `kct export`, `kicad-cli pcb drc` | a new `generate_design.py` recipe in `<board-path>` | fab tier, if not given | no | no | no |
| `layout-journal` | `kct net-status`, `kct check`, `kicad-cli pcb drc --refill-zones` | `<board-path>/LAYOUT_NOTES.md` | no | no | no | no |

`suggestedModel` in a skill's frontmatter is advisory dispatch metadata. Harnesses
without per-skill model selection ignore it, and the Codex renderer drops it.

## Placeholder vocabulary

Skill sources live in `src/kicad_tools/agent_skills/kct/*.md` (package data, so
they ship in the wheel; issue #5950). `.claude/commands/kct/` is this repo's
byte-identical copy: edit the package data, then run
`uv run python scripts/sync_agent_skills.py --write` (a test fails on drift).
The bodies are shared by every harness. They never hard-code an invocation syntax
or install path. They use placeholders, which `kicad_tools.agent_surfaces.render`
(called by `kct skills install`) and `scripts/install-kct.sh` fill in per harness:

| Placeholder | Claude Code | Codex CLI |
|---|---|---|
| `{{skill:<name>}}` | `/kct:<name>` | `$kct-<name>` |
| `{{skill-file:<name>}}` | `.claude/commands/kct/<name>.md` | `.agents/skills/kct-<name>/SKILL.md` |
| `{{skills-dir}}` | `.claude/commands/kct/` | `.agents/skills/` |
| `{{skills-readme}}` | `.claude/commands/kct/README.md` | `.agents/skills/kct-help/README.md` |
| `{{agent-guide}}` | `CLAUDE.md` | `AGENTS.md` |

`$ARGUMENTS` is not a placeholder. Claude Code commands, Codex prompts and
opencode commands all accept it, so it passes through unchanged. opencode
rendering is Phase 2 work (#5951). An unknown `{{...}}` token makes the renderer
raise an error, so a typo cannot ship.

When you read a source file inside this repo, `{{skill:tapeout}}` means "the
`tapeout` skill, invoked however your harness invokes skills".

## Neutrality rules (enforced)

`tests/test_agent_surface_neutrality.py` lints every skill body, every skill
`description`, and every MCP tool and parameter description in
`kicad_tools.mcp.tools.registry.TOOL_REGISTRY`. It fails if it finds any of these:

- a Claude Code tool name (a backticked `` `Read` `` / `` `Bash` ``, "the Task tool",
  `TodoWrite`, `AskUserQuestion`, ...) or a subagent assumption. Write neutral
  verbs instead: "read the file", "run `kct check`";
- a hard-coded `/kct:` invocation. Use `{{skill:<name>}}`;
- a `CLAUDE.md` / `.claude/` pointer or the word "Claude". Use the placeholders;
- a `boards/NN-*` demo-board path. Use `<board>` / `<board-path>`. pcba-bench
  forbids pointing agents at its reference designs, and packaged skills must not
  steer users to repo-internal files.

There are two exceptions:

1. **Marked notes.** A blockquote paragraph that opens with
   `> **Claude Code only` is exempt, but it must contain a fallback for other
   harnesses (the test checks for the word "fallback"). The ee-review Phase 2
   fan-out note is the only one today.
2. **`ALLOWLIST`** in the test: exact `(source, line substring)` pairs, for the rare
   case a marked note cannot express. A stale entry fails the test. The list is
   empty today.

The Claude Code frontmatter keys `invocation` and `suggestedModel` are not linted.
They are harness metadata, and the Codex renderer drops them.
