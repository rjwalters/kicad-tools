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
without per-skill model selection ignore it, and the Codex and opencode renderers
drop it.

## Placeholder vocabulary

Skill sources live in `src/kicad_tools/agent_skills/kct/*.md` (package data, so
they ship in the wheel; issue #5950). `.claude/commands/kct/` is this repo's
byte-identical copy: edit the package data, then run
`uv run python scripts/sync_agent_skills.py --write` (a test fails on drift).
The bodies are shared by every harness. They never hard-code an invocation syntax
or install path. They use placeholders, which `kicad_tools.agent_surfaces.render`
(called by `kct skills install`) and `scripts/install-kct.sh` fill in per harness:

| Placeholder | Claude Code | Codex CLI | opencode |
|---|---|---|---|
| `{{skill:<name>}}` | `/kct:<name>` | `$kct-<name>` | `/kct/<name>` |
| `{{skill-file:<name>}}` | `.claude/commands/kct/<name>.md` | `.agents/skills/kct-<name>/SKILL.md` | `.opencode/commands/kct/<name>.md` |
| `{{skills-dir}}` | `.claude/commands/kct/` | `.agents/skills/` | `.opencode/commands/kct/` |
| `{{skills-readme}}` | `.claude/commands/kct/README.md` | `.agents/skills/kct-help/README.md` | `.opencode/commands/kct/README.md` |
| `{{agent-guide}}` | `CLAUDE.md` | `AGENTS.md` | `AGENTS.md` |

`$ARGUMENTS` is not a placeholder. Claude Code commands, Codex prompts and
opencode commands all accept it, so it passes through unchanged. An unknown
`{{...}}` token makes the renderer raise an error, so a typo cannot ship.

## opencode: commands, not skills (#5951)

`kct skills install --harness opencode` writes each skill as an opencode
**command**, `.opencode/commands/kct/<name>.md`, invoked as `/kct/<name>`.
`--user` writes to opencode's global `commands/` dir instead.

Checked against opencode v2.0.22, using the v2 docs at
`opencode.ai/v2/docs/commands` and `opencode.ai/v2/docs/skills`, and the live
binary:

- **Commands** are Markdown files in `.opencode/commands/` (project) or
  `~/.config/opencode/commands/` (global). The legacy singular `command/` still
  loads, but new files belong in `commands/`. Nested paths become names with
  `/` separators, so `kct/tapeout.md` is `/kct/tapeout`. The body is the prompt
  template and takes `$ARGUMENTS`. Frontmatter accepts `description`, `agent`,
  `model` (`provider/model[#variant]`) and `subagent`.
- **Skills** are `.opencode/skills/<id>/SKILL.md` (opencode also reads
  `.claude/skills` and `.agents/skills`). The *model* loads them through its
  `skill` tool when their description matches the task, or a user mentions
  `@<id>` in a prompt. They take no arguments.
- **Global dir.** opencode resolves it as `$OPENCODE_CONFIG_DIR`, then
  `$XDG_CONFIG_HOME/opencode`, then `~/.config/opencode` (confirmed by
  `opencode debug paths` and the binary). `--user` follows the same order.
- **Live check.** Run `kct skills install --harness opencode` into a scratch
  project, then `opencode api command.list`. It lists all eight `kct/*` commands
  with their descriptions.

Every `kct` skill is a user-started workflow with an argument contract:
`$ARGUMENTS` is `<board-path> [--mfr <tier>] ...`. That matches an opencode
command, not a model-selected skill. Commands are therefore the closest
equivalent of the Claude Code `/kct:<name>` slash commands. Codex users already
reach the same skills through `.agents/skills/`, which opencode also reads. So
installing both layouts into one project gives opencode both forms.

How the frontmatter is mapped:

| Source key | opencode command |
|---|---|
| `description` | kept (rendered, as a YAML folded scalar) |
| `name` | dropped; opencode takes the command name from the path |
| `invocation` | dropped (Claude Code metadata) |
| `suggestedModel` | dropped. It is a bare tier (`sonnet`, `opus`), and `model:` needs a provider-qualified id. Mapping it would pin every user to one provider, so the session model stays in charge. |

Things that differ from the other harnesses:

- **Template hazards.** opencode expands `` !`cmd` `` in a command template by
  running the shell command, outside the agent's permission flow. It also
  replaces `$1`, `$2`, ... with positional arguments. The opencode renderer
  refuses a skill body containing either, so a future skill cannot run a
  command, or lose text, by accident.
- **`kct/README.md`.** The README is installed beside the commands so that
  `{{skill:help}}` can read it. opencode therefore also lists it as
  `/kct/README`, just as Claude Code lists `/kct:README`.
- **Paths in the text.** In a `--user` install, `{{skill-file:...}}` still
  renders the project path (`.opencode/commands/kct/...`). The Claude Code and
  Codex `--user` installs behave the same way.

When you read a source file inside this repo, `{{skill:tapeout}}` means "the
`tapeout` skill, invoked however your harness invokes skills".

### `{{agent-guide}}` vs `kct agent-guide`

`{{agent-guide}}` still renders to the harness's **project instructions file**
(`CLAUDE.md` for Claude Code, `AGENTS.md` for Codex). It is not retargeted at
the shipped primer (issue #5960). The placeholder names a real file in the
consumer repo: the `help` skill uses it as the fallback location of the
guarded kicad-tools block that `scripts/install-kct.sh` writes into that file.
Pointing it at a command would break that fallback.

The shipped primer needs no placeholder, because it is reached the same way in
every harness: `kct agent-guide`. The `help` skill's closing pointer names it
directly.

## The agent primer (`kct agent-guide`)

`src/kicad_tools/agent_skills/AGENT_GUIDE.md` is package data, printed by
`kct agent-guide`. Most of it is plain harness-neutral Markdown. Harness-specific
examples live only in its `<!-- per-harness -->` block, which may use the
placeholders above plus the literal token `<harness>` (the harness id).
`kicad_tools.agent_skills.guide.render_guide` renders that block through
`render()` for `--harness X`, or once per harness when `--harness` is omitted.
A placeholder outside the block is an error. opencode joins `--harness` as soon
as `render()` supports it (#5951); the guide needs no edit.

## Neutrality rules (enforced)

`tests/test_agent_surface_neutrality.py` lints every skill body, every skill
`description`, the agent primer (`AGENT_GUIDE.md`), and every MCP tool and
parameter description in `kicad_tools.mcp.tools.registry.TOOL_REGISTRY`. It
fails if it finds any of these:

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
