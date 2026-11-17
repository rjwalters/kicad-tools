# Evaluation: Backplane (i2cjak) and KiStack (American Embedded)

**Issue**: #6406
**Date**: 2026-10-12
**Upstreams and pinned commits read**:

- [i2cjak/Backplane](https://github.com/i2cjak/Backplane) at `40379b590dc52e00c3ede1b1ebe3133a11e3c4da`
- [American-Embedded/KiStack](https://github.com/American-Embedded/KiStack) at `8494dbde095669df081950cbb6b24d08a21e25b0`

**Registry entries**: `kct ecosystem show backplane`, `kct ecosystem show kistack`
**Copying**: none. Both repos were read as read-only clones in a scratch directory outside this tree. Everything below is restated in our own words, and no upstream file was copied into this repo (see [Licenses](#licenses)).

## TL;DR

Backplane is an agentic KiCad/FreeCAD hub. Claude Code, Codex and other agents edit a local project while board, schematic, 3D and mechanical viewers redraw on every save. Its one idea that fits our mission well is the **board diff**:
- every board item is keyed by uuid and given a content hash;
- two versions are merge-walked into removed, changed and added items.

We have no geometric diff today. `kct check --diff` diffs findings, not copper. Reviewers of #6342-style routing PRs worked around that by hand. The diff is filed as **#6417, `kct pcb diff OLD NEW`**, which is a re-implementation and not a port, with two deliberate changes to Backplane's model. Everything else in Backplane is GUI or live-editor territory, which falls under our no-GUI non-goal.

KiStack is ten prose agent skills (553 lines). It overlaps our `/kct:*` skills, and its layout skill tells agents to route by hand and avoid autorouters. That is the opposite of our bar, so the verdict is **evaluated-not-adopted**.

The AC3 integration check turned up a real **bug in our own setup command, filed as #6418**:
- Backplane's Claude session does merge in a user's or project's own MCP servers and project commands.
- But `kct mcp setup --client claude-code` writes `~/.claude/mcp.json`, a file Claude Code never reads, inside Backplane or outside it.
- `/kct:*` commands work in a Backplane-launched session today. The `kct` MCP tools work only once the server is registered somewhere Claude Code reads (see [AC3](#ac3-would-kct-plug-into-a-backplane-thread)).

| Follow-up | Issue | Status |
|---|---|---|
| `kct pcb diff OLD NEW`: uuid plus content-hash geometric board diff, with `REV:path` loading | #6417 | filed |
| `kct mcp setup --client claude-code` writes a file Claude Code never reads | #6418 | filed |
| Per-stage design history on a hidden git ref | none | deferred until #6417 lands |
| KiStack coverage gaps | none | no gap an automated pipeline needs |

## What Backplane is

Backplane is written mostly in **Bend**. Pure logic lives in `src/core/*.bend` and IO in `src/server/*.bend`, each with C/JS effect twins. GitHub's "TypeScript" label comes from the web client, the `tools/` scripts (bun) and the Kotlin/Swift phone apps. It runs locally against the user's own KiCad, FreeCAD and agent subscriptions, and remote access goes over Tailscale. Other surfaces include voice, chat bots, a headless browser tool and a FreeCAD mechanical loop. Those are out of scope here and get one line each under [Won't adopt](#what-we-deliberately-wont-adopt).

### Snapshots: per-tool-call history on a hidden ref

`src/server/git.bend`, `Git.step.script()` (around lines 569-637), runs after every agent tool call that touched KiCad files:

1. **Find the repository.** It uses the project's own git repository. If the folder is not a repository, it uses a shadow bare repository at `<folder>/.backplane/history.git`.
2. **Stage through a private index.** It sets `GIT_INDEX_FILE=<gitdir>/backplane-<thread>.steps.index` and stages `*.kicad_pcb` and `*.kicad_sch`, excluding `*_autosave-*` and `*-backups/*`. The user's real index is never touched.
3. **Write the tree.** It runs `git write-tree`. If the tree equals the previous step's, nothing is recorded.
4. **Commit and advance the ref.** `git commit-tree` creates a commit whose parent is the previous step. `git update-ref` then moves `refs/backplane/checkpoints/<b64url(thread)>/steps` with a **compare-and-swap** on the old value; the first step uses `update-ref --stdin create`. Two concurrent captures therefore cannot fork the chain.
5. **Serialise captures.** A lock directory holding the owner's pid makes captures take turns, and a dead owner's lock is cleared.
6. **Record turns.** Per-turn refs, `refs/backplane/checkpoints/<thread>/turn/<n>`, mark the end of each agent turn and are what "revert this turn" restores.

HEAD, branches and the working tree are never moved. History is invisible to `git log` and `git status`, but it can be fetched and inspected like any ref.

### Board diff: uuid-keyed merge walk

The diff is in `src/core/board.bend`, which builds items, and `src/core/hist.bend`, section "Visual diff" with `Hist.mark` (around lines 479-590). The model:

- **Items.** Every drawable board item becomes an `Item{key, sig, layer, prim, info}`:
  - segments, arcs, vias, pads, footprint graphics, zone fill polygons and board graphics are all items;
  - a via also emits a `<key>/h` drill item;
  - a footprint's children are keyed `<fp>/<child>`;
  - a zone's filled polygons are keyed `<zone>/f<i>`.
- **Keys.** The `key` is the item's `uuid`, else its `tstamp`, else a positional `n<i>`.
- **Signature.** `sig` is a 32-bit **FNV-1a hash of the top-level form's raw bytes**. Footprint children inherit their footprint's `sig`.
- **Merge walk.** Both lists are sorted by key and walked together:
  - a key only in the old list is **removed**;
  - a key only in the new list is **added**;
  - the same key with a different `sig` is drawn twice, the old version as removed (`~was`) and the new one as **changed**;
  - the same key and the same `sig` is drawn **dimmed**.
- **Laws.** `LAWS.bend` states laws such as "a version diffed with itself is all dimmed" (`hist_mark_self`), and `PROOF.bend` proves them.
- **Inputs.** A version is either a live path or a `git<TAB><gitdir><TAB><commit>…` blob address, so a diff can compare any two steps, turns, commits or branches.

It is **geometric only**: no net, DRC, LVS or connectivity semantics. It also inherits two properties that would hurt us:

1. **uuid churn reads as change.** Our writers mint `uuid4()` by default (`pcb/editor.py`, `pcb/exporter.py`), and the router does too unless deterministic uuids are on (`router/primitives.py`, #3272). A re-run route would diff as "everything removed, everything added".
2. **Raw-byte hashing reads formatting as change.** The tabs-vs-spaces and float-format churn of #3925 would show up as "changed". Because children inherit the footprint hash, one moved pad also marks every child of the footprint as changed.

#6417 keeps the merge walk and the removed/changed/added vocabulary but deviates in two ways. It adds a second pairing pass on a content key (kind, net, layer, quantised geometry), which is the content-addressed key #3925 asked for. It also hashes normalised parsed values instead of bytes.

### Agent context and skills

`Hub.agent_args` in `src/server/server.bend` (around line 2719) and `Claude.args` in `src/core/prompt.bend` (around line 262) build the long-lived Claude Code session:

```
env MCP_TOOL_TIMEOUT=86400000 CLAUDE_CODE_ENABLE_TODO_TOOLS=1 claude
  -p --verbose --input-format stream-json --output-format stream-json
  --include-partial-messages --permission-mode <plan|default|acceptEdits|auto|bypassPermissions>
  --append-system-prompt <runtime + kicad_runtime + project files + orchestration + ...>
  [--permission-prompt-tool mcp__backplane__permission_prompt]   (all modes but full access)
  [--model M] [--resume S] --add-dir <dirs...> <effort args>
  --plugin-dir <KiStack revision dir>...
  --mcp-config '{"mcpServers":{"backplane":{"type":"http","url":".../mcp/<thread>"}}}'
```

The process runs in the project directory (`H.Proc.spawn(..., cwd, 0)`) and does not override `HOME` or `CLAUDE_CONFIG_DIR`.

The system prompt has these blocks:
- **`<kicad_runtime>`** names the exact `kicad-cli` to use, with full path, for exports, ERC and DRC. When the companion fork is installed and IPC is enabled, it also tells the agent to start the fork's `api-server` (NNG/protobuf) for programmatic edits.
- **Project files** names the canonical `.kicad_pro`, `.kicad_pcb` and `.kicad_sch`, chosen by `.backplane.json`, and tells the agent to save each meaningful step because the viewer is watching.
- **Orchestration** covers delegation, background work and similar instructions.

The `backplane` MCP server exposes orchestration and viewer tools: `viewer_show`, `delegate_task`, `thread_*`, `work_run`, a `preview_*` browser and `permission_prompt`. **None of them reads or edits KiCad data.** KiCad knowledge comes from the KiStack plugin (skills as `kistack:<name>`) plus the `kicad-cli` hint.

Codex sessions get `-c mcp_servers.backplane.url="…"`, which overrides one key and does not replace the table. They also get KiStack skills and the user's own skills inlined into the prompt (`AG.USkill.prompt`).

## AC3: would `kct` plug into a Backplane thread?

**Method.** Backplane was **not installed or run**. Instead:

1. I read the argv builder at the pinned commit (above). Only the one-shot helper `OneShot.claude` (`src/core/agent.bend:460`) passes `--strict-mcp-config`. The main session does not, and it passes no `--setting-sources`.
2. I **replayed that argv** with the local Claude Code 2.1.286, using the supervised and default permission mode, without `--plugin-dir`, and with `--mcp-config` pointing the `backplane` server at a dead port. It ran in a throwaway `HOME` and project directory, with `ANTHROPIC_BASE_URL` pointed at a dead endpoint, so no model call or real config was involved. I read the `system/init` stream-json event, which lists the loaded MCP servers, tools and slash commands before any model call.

| Setup in the throwaway HOME/project | `kicad-tools` MCP server | `kct` tools | `/kct:*` commands |
|---|---|---|---|
| `kct mcp setup --client claude-code` + `kct skills install` (as shipped) | **absent** | 0 | 8 loaded |
| Control: same files, plain `claude -p`, no Backplane flags | **absent** | 0 | n/a |
| Server registered with `claude mcp add --scope user` (`~/.claude.json`) | connected, `source: user` | 47 | 8 loaded |
| Server in the project's `.mcp.json` | connected, `source: project` | 47 | 8 loaded |

In every row the `backplane` server showed up as `source: dynamic`, failing only because nothing was listening. So `--mcp-config` **adds to** the user and project servers rather than replacing them.

**Conclusions:**

- Backplane's argv does not block `kct`. A `kicad-tools` server registered where Claude Code reads it, and the `/kct:*` commands from `kct skills install`, both load in a Backplane-shaped session next to the `backplane` server.
- **`kct mcp setup --client claude-code` is broken on its own.** It writes `~/.claude/mcp.json`, which Claude Code does not read, so the shipped recipe yields `/kct:*` but **no `kct` tools**. The control row shows this is not Backplane-specific. Filed as **#6418**.
- **Workaround until #6418 lands:**

  ```bash
  claude mcp add --scope user kicad-tools -- <the command kct mcp setup --dry-run prints>
  kct skills install          # in the Backplane project directory
  ```

**Not verified:**
- **A real Backplane thread.** This was not run, so the following were not observed: the hub's `permission_prompt` approving `kct` tool calls in non-full-access modes, the composer UI, and the KiStack plugin loading alongside `kct`.
- **Composer autocomplete.** Backplane's picker scans only `~/.claude/skills`, `<cwd>/.claude/skills` and KiStack (`Skills.scan`, `server.bend` around line 3165). `kct skills install` writes `.claude/commands/kct/`, so `/kct:*` will run when typed but is probably **not suggested** by Backplane's picker.
- **The Codex path.** `kct mcp setup --client codex` writing `~/.codex/config.toml`, merged with Backplane's `-c mcp_servers.backplane.url=…`, is inferred from the argv only and was not run.
- **Agent behaviour.** Whether an agent following KiStack's layout skill would choose `kct route` at all. It tells agents to avoid autorouters.

## Licenses

| Component | License | Bearing on us |
|---|---|---|
| Backplane root (`LICENSE`) | MIT, © 2026 i2cjak | Compatible. We take ideas only, because the code is Bend. |
| `backplane-step2glb` (embeds OpenCascade through `occt-import-js`) | **LGPL-2.1**, notices in `dist/licenses/step2glb/` | Not MIT. Never copy; we need no STEP-to-GLB converter. |
| `assets/fonts/` Spleen font | BSD-2-Clause | Irrelevant. |
| `tools/browser` dependency `playwright-core` | Apache-2.0 | Irrelevant. |
| Companion KiCad fork [i2cjak/Backplane_KiCad](https://github.com/i2cjak/Backplane_KiCad) | **GPL-3.0** | Copyleft. Its IPC protocol and docs are not to be copied; not evaluated beyond this line. |
| KiStack `LICENSE` | MIT text, © 2026 American Embedded, LLC; GitHub reports **`NOASSERTION`** | Recorded as `license = "MIT"` with `upstream_license_detected = "NOASSERTION"`. The same file is also vendored in kicadmium (`skills/kistack/LICENSE`). |
| KiStack `skills/export/references/kicad_cli_command_*.md` (44 files) | Read as transcriptions of KiCad's user documentation (inferred from wording, not confirmed). docs.kicad.org is GPL-3.0-or-later / CC-BY-3.0, which KiStack's MIT file cannot relicense. | **Copy none of them.** Our own `kicad-cli` wrappers cite the real docs. |

No file from either repository is in this PR: `git diff --stat origin/main` shows only the note, the registry and the regenerated README block.

## Ideas, ranked by fit with fully automated agent-driven PCB creation

1. **uuid plus content-hash geometric board diff. ADOPTED, as #6417.** This is the highest fit. It answers "what copper did this PR change, on which nets and layers" as JSON for gates and an SVG overlay for PR descriptions. It reuses `check_diff.py`'s `REV:path` materialisation so `origin/main:boards/05/…` works as an argument. The implementation is Python on our `sexp` parser, not a Bend port, and makes the two deviations described above (content-key pairing after uuid matching, and normalised hashing). A PR-comment bot for board-touching PRs is the natural next step, to be filed once #6417 lands.
2. **Per-stage design history on a private index and hidden ref. DEFERRED.** The technique is clean and costs nothing to the user's repository: `GIT_INDEX_FILE` + `write-tree` + `commit-tree` + CAS `update-ref`. It would map onto `kct pipeline` / `kct route` stage boundaries. But without a diff tool the history has no reader, and no reviewer workflow today needs intra-run stage snapshots; PR-level before/after is the actual ask. Revisit after #6417, if a reviewer asks "which stage introduced this".
3. **"kct as Backplane's agent toolset" recipe. ADOPTED AS DOCUMENTATION**, in the AC3 section above, plus the #6418 bug fix. No integration code is needed on either side.
4. **KiStack coverage comparison. DECLINED, no follow-up.** See the table below. Every KiStack skill either maps onto something `kct` already does or is prose advice, which is not a gap in itself. The closest candidate, KLC compliance linting, is library-submission hygiene (pin grid, text sizes, pin placement) rather than a manufacturability or LVS gate, so it does not meet the "an automated pipeline would use it" bar.

### KiStack skills vs `kct`

| KiStack skill (lines) | What it tells the agent | `kct` coverage | Gap? |
|---|---|---|---|
| `schematic` (98) | how to draw and wire schematics, research parts | `kct sch …`, `kct erc`, `kct fix-erc`, `kct sync`, `/kct:ee-review` | no |
| `symbol` (19) | generate symbols from easyeda2kicad or a datasheet pin table, check against KLC | `kct datasheet` pin-table extraction; `kicad_tools.datasheet.symbol_generator` (#100/#102); `kct lib` | KLC lint only; declined (hygiene, not a gate) |
| `footprint` (25) | drive KiCad's `kicad-library-tools` generators from YAML | `kct footprint generate` (parametric), `validate-footprints`, `fix-footprints` | no |
| `layout` (20) | place and route **by hand**, avoid autorouters, swap pins freely | `kct route`, `kct optimize-placement`, `kct pipeline`, LVS-gated | conflicts with our mission |
| `pcb` (32) | placement heuristics, review with `kicad-cli pcb drc` and renders | `kct check` (pure-Python DRC), `kct drc`, `kct render`, `kct board-view` | no |
| `gerbers` (18) | render Gerbers to PNG with pygerber and eyeball them for shorts | `kct export` (Gerbers), `kct check --mfr`, copper LVS, `kct render` | no; we check shorts semantically, not by eye |
| `export` (120 + `convert_position.py`) | `kicad-cli` export recipes (references: see license caveat) | `kct export` (full manufacturing package incl. CPL rotation) | no |
| `bom` (78) | part search, resolving BOM items | `kct bom`, `kct parts` (LCSC), `kct suggest` | no |
| `panelize` (90) | KiKit panel JSON recipes | `kct panel` | no |
| `pcb-product-render` (53) | STEP/GLB export plus a product-shot recipe | `kct render` (3D PNGs) | marketing renders; not a pipeline need |

## What we deliberately won't adopt

- **Live viewers, phone apps, the native window and Tailscale remote access.** These hit our "No GUI" non-goal (`kct ecosystem where-we-sit`). Our review surface is static artifacts (SVG, PNG, JSON) in CI and PRs, which is exactly what #6417 produces.
- **The FreeCAD mechanical loop**, which models enclosures and brackets from the board outline. It is outside the PCB-automation mission. Watch only.
- **Routing through the Backplane KiCad fork's IPC server.** The fork is GPL-3.0 and the approach is a live editor. We stay offline and file-based, and `kct ipc` already covers upstream KiCad's own IPC where needed.
- **KiStack's layout guidance** ("prefer not to use autorouters, route manually", plus free schematic pin swaps for layout). It contradicts fully automated, LVS-gated creation. Pin swaps in particular must go through our schematic-to-PCB sync and LVS, not ad hoc edits.
- **Bend source, even though it is MIT.** A language port is a rewrite anyway. We take the model, not the code.
- **Voice, chat bots and the headless browser tool.** These are agent-harness features unrelated to PCB generation.

## Re-evaluation triggers

- **Backplane:** when #6417 merges, move the verdict to `ideas-adopted`. If Backplane gains DRC- or LVS-aware diffing, or exposes its history and diff as a CLI, check whether it can review our routing PRs directly.
- **KiStack:** a layout skill that drives an autorouter behind DRC/LVS gates, or skills that call `kct`, would make it worth recommending alongside `/kct:*`.
