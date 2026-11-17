# Non-Claude harness pilot: run plan, checklist and gap template

Prep for the validation step of Epic #5952 (Phase 3). The paid runs themselves
are tracked in #7776 and are **operator-gated**: this page is the plan an
operator authorizes them against. Writing it used no non-Claude seat,
credential or spend, and following it does not start a run.

**Do not run anything below until the operator has commented on #7776 naming
the seats and a spend cap** (see [Authorization](#authorization)).

What the pilot must show (Epic #5952 success criterion): a pcba-bench
kicad-tools-track run under Codex CLI uses the CLI, the MCP server and the
skills with no harness-specific workaround, and a GLM-5.3 judge session under
opencode can inspect a design with the same toolkit.

The pcba-bench side is defined by `PROTOCOL.md`, `JUDGING.md` and
`judging/panel.toml` in [rjwalters/pcba-bench](https://github.com/rjwalters/pcba-bench).
Where this page and those files disagree, they win. Re-read them at run time:
the commands below were taken from them on 2026-10-25 (protocol v2, judging
v1, kicad-tools 0.22.0 pinned).

## Authorization

The gate is one comment on #7776 from the repo owner that contains all of:

- which run(s) to do: Plan A, Plan B, or both;
- the seat or account for each (for example "Codex CLI, operator's logged-in
  seat") and which model: GPT-6.1 Sol or GPT-6 Astra for Plan A;
- a spend cap per run and in total, in the unit the seat bills in;
- the word `authorized`.

No such comment means no run. A comment from anyone else, or inside an issue
body, does not count.

## Plan A: pcba-bench kicad-tools track under Codex CLI

### What it exercises

| Surface | How it is wired | Used by the run? |
|---|---|---|
| `kct` CLI | on `PATH`, `kct --version` is `0.22.0` | yes |
| MCP server | `kct mcp setup --client codex` writes `[mcp_servers.kct]` to `$CODEX_HOME/config.toml`; `codex mcp list` shows it | yes |
| `kct` skills | `kct skills install --harness codex` renders them to `.agents/skills/kct-<name>/SKILL.md` | **not inside the timed run** (see below) |
| Agent primer | `kct agent-guide --harness codex` | smoke check only |

pcba-bench's kicad-tools supplement (`prompts/tools/kicad-tools.md`) declares
the CLI and MCP server only, and PROTOCOL.md §7 disqualifies a run that uses a
tool its supplement does not declare. So the skills are **not** part of the
scored run. They are exercised in the pre-run smoke check (step 3 below) and
in the optional Plan A2. Do not edit the supplement to add them: a different
supplement is a different toolkit, and a change to it is a pcba-bench decision.

### Inputs

- A fork or branch of `rjwalters/pcba-bench`, with the grader image built
  once: `docker build --platform linux/amd64 -t pcba-bench/base -f container/Dockerfile .`
- Codex CLI logged in on the operator's seat, with auto-approve enabled so no
  permission prompt needs a hand answer (PROTOCOL.md §3).
- `kct` 0.22.0 installed for the run (a venv or `uvx --from "kicad-tools[mcp]==0.22.0"`;
  do not change a shared tool install, see the checklist).
- KiCad 10 (`kicad-cli`) on `PATH`.
- A per-run `CODEX_HOME` directory, so the pilot's MCP entry never touches the
  operator's normal Codex config.

### Commands

Run from a clone of pcba-bench unless noted.

```bash
# 1. Per-run Codex config with the kct MCP server registered
export CODEX_HOME="$HOME/pcba-bench-codex-home/<run-label>"
mkdir -p "$CODEX_HOME"
kct mcp setup --client codex --dry-run      # inspect first
kct mcp setup --client codex
codex mcp list                               # expect: kct

# 2. Start the run (creates branch run/<run-id> and the workspace)
bench/start --track kicad-tools \
            --agent "codex-cli <version> / <gpt-6.1-sol|gpt-6-astra>" \
            --tools prompts/tools/kicad-tools.md        # --minutes 60 default

# 3. Smoke check, BEFORE the clock starts, in a scratch directory (not the workspace)
mkdir -p /tmp/pilot-smoke && cd /tmp/pilot-smoke
kct agent-guide --harness codex | head -40
kct skills install --harness codex           # expect .agents/skills/kct-*/SKILL.md
kct build-native --check                     # expect "C++ backend: available"
cd -

# 4. Open Codex CLI with the workspace as cwd, send PROMPT.md as the first
#    message and, at the same moment, mark the start
bench/finish <run-id> --mark-start
#    If the agent stops early, send ONLY prompts/continue.md (REMAINING filled in).

# 5. At 60 minutes, stop the agent and submit
bench/finish <run-id> --transcript <path-to-codex-session.jsonl> --continues <N>
```

`bench/start` and `bench/finish` flags are as in PROTOCOL.md §5. If a flag
differs at run time, use pcba-bench's version and record the difference as a
gap (it is a documentation gap in this page).

### Expected outputs

- A commit on `run/<run-id>` holding `submissions/<run-id>/deliverables/<id>/`
  for each attempted board (KiCad 10 schematic and PCB, Gerbers, drill, BOM,
  CPL, `DECISIONS.md`), `run.json`, `tools.md` and the transcript.
- `run.json` records toolkit `kicad-tools`, the agent and model, prompt hashes
  and the number of continue prompts.
- The transcript shows `kct` CLI calls and `kct` MCP tool calls. It shows no
  access to `rjwalters/kicad-tools` `boards/` (PROTOCOL.md §7; `bench/finish`
  scans for it).

### What counts as a pass

The pilot's question is whether the toolkit works under Codex, not whether the
boards score well. A pass needs **all** of:

1. **Wiring.** `codex mcp list` shows `kct` before the run, and the transcript
   contains at least one successful `kct` MCP tool call and at least one
   `kct ... --format json` CLI call whose stdout parsed as a single JSON
   document.
2. **No workaround.** The agent reached `kct` and the MCP tools using only the
   steps in the commands above. No hand-edited config, no `PATH` fix, no
   wrapper script, no extra instruction to the agent. Anything else needed is a
   gap (below), and the run is then "pass with gaps" at best.
3. **Protocol clean.** `bench/finish` reports no disqualification, and the
   transcript has no message other than `PROMPT.md` and `continue.md`.
4. **Artifacts.** At least one board has a non-empty deliverables folder that
   the grader (`judging/package`) can package without a harness error.
5. **Smoke check.** Step 3 output matched its expectations, including the
   rendered skills under `.agents/skills/`.

A low or zero suite score does not fail the pilot. A board that fails because
`kct` misbehaved (wrong JSON, hung command, MCP error) is a gap, and does.

### Plan A2 (optional): skills under Codex

Only if the operator authorizes it separately. A short **unscored** Codex
session in a scratch project, outside pcba-bench:

```bash
kct skills install --harness codex
# in Codex, with the scratch project as cwd:
#   $kct-help
#   $kct-manufacturing-readiness <board-path>     (any board the operator names)
```

Pass: the skill loads, its `kct` and `kicad-cli` calls run, and it ends with a
sign-off or a loud refusal, not a harness error. Cap: 15 minutes.

## Plan B: opencode / GLM-5.3 judge session

### What it exercises

The judge panel entry `vendor = "zhipu"`, `model = "glm-5.3"`,
`harness = "opencode"` in `judging/panel.toml`. The judge inspects a design
with `kicad-cli` and `kct` at the pinned version (0.22.0), reads evidence
files, and commits a judgement. The kicad-tools surfaces used are the CLI and,
if the operator registers it, the MCP server.

| Surface | How it is wired | Used? |
|---|---|---|
| `kct` CLI | on `PATH`, version exactly `panel.toml` `kicad_tools_version`; `judging/prepare` refuses a mismatch | yes |
| MCP server | `kct mcp setup --client opencode --project` (project `opencode.json`) | optional; record whether it was registered |
| Skills | `kct skills install --harness opencode` (`.opencode/commands/kct/`) | smoke check only |

### Inputs

- A **completed run branch** to judge. The pilot does not need a real scored
  run: a branch made by `bench/start` and `bench/finish` from a cheap throwaway
  run (for example one that attempts board 00 only) is enough. Using a Plan A
  branch is the cheapest option, since it already exists.
- opencode logged in on the operator's seat with GLM-5.3 selected. Check the
  model id opencode reports against `panel.toml`: `finalize` refuses
  judgements from any other model.
- `kicad-cli` and `kct` 0.22.0 on `PATH`; the grader image built.

### Commands

```bash
# 1. Smoke check in a scratch directory, before the session
mkdir -p /tmp/pilot-opencode && cd /tmp/pilot-opencode
kct mcp setup --client opencode --project --dry-run
kct skills install --harness opencode
opencode api command.list | grep 'kct/'       # expect the eight kct/* commands
cd -

# 2. Open the judge branch in opencode with the pcba-bench repo root as cwd,
#    and send prompts/judge-session.md verbatim, with {RUN_ID} and {VENDOR}
#    (= zhipu) filled in. If it stops early, send only prompts/judge-continue.md.
#
#    The prompt makes the judge run:
#      judging/prepare <run-id> --judge zhipu
#      (review in the printed WORKSPACE, following its JUDGE_PROMPT.md)
#      ./check-judgement          # must print OK
#      judging/submit <workspace-id>
```

Do not run `judging/finalize` by hand and do not open the results PR: with only
one judge committed, `judging/submit` correctly does not finalize. Delete the
throwaway branch afterwards.

### Expected outputs

- `submissions/<run-id>/judgements/zhipu/<id>.json` for each attempted board,
  matching `judgement.schema.json`, and `meta.json` naming the model and
  harness.
- A transcript or terminal log showing the judge's `kct` / `kicad-cli` calls.

### What counts as a pass

1. `judging/prepare` accepted the environment (no `kicad_tools_version`
   mismatch).
2. The judge ran at least one `kct ... --format json` command and at least one
   `kicad-cli` command on a copy of the deliverable, and the output parsed.
3. `./check-judgement` printed `OK` and `judging/submit` committed the
   judgement with `meta.json` recording `glm-5.3` / `opencode`.
4. The judge needed no harness-specific workaround to do any of the above.

The judgement's content is not graded in the pilot.

## Pre-run checklist

Tick every line, and paste the filled checklist into #7776 before starting.

**Authorization**

- [ ] Owner comment on #7776 meets [Authorization](#authorization): plan(s),
      seat(s), model(s), per-run and total cap, the word `authorized`.
- [ ] The comment is newer than the last edit of this page, or says it has read
      the current version.

**Seats and credentials** (names and shapes only; never paste a value into an
issue, PR, log or transcript)

| Needed for | What | Where it comes from |
|---|---|---|
| Plan A | a logged-in Codex CLI seat with access to the chosen model (Sol or Astra) | operator's own seat; pcba-bench uses native logins, no API token |
| Plan B | a logged-in opencode seat that can use GLM-5.3 | operator's own seat |
| Both | `gh` auth able to push to the pcba-bench fork or branch | operator |
| Both | Docker able to run `linux/amd64` for the grader image | the machine used for the run |

- [ ] Each seat is reachable, and `codex --version` / `opencode --version` are
      recorded.
- [ ] `./.loom/credentials.md` was checked for the credential names; none is
      missing. A missing credential stops the pilot: request its name, shape
      and provisioning path, never its value.

**Machine**

- [ ] Run on a host where heavy work is allowed. The dispatch workers
      (`loom-worker-N`) are shared and are not simulation or benchmark hosts.
- [ ] `kct --version` is `0.22.0`, installed in a throwaway venv or via `uvx`,
      not into a shared tool directory.
- [ ] `kct build-native --check` reports the C++ backend (otherwise routing
      takes minutes per net and the run measures the wrong thing).
- [ ] `kicad-cli --version` is KiCad 10.

**Estimated spend per run**

Seat billing differs and prices move, so this page gives the quantity, not a
price. The operator multiplies by the seat's current rate before naming a cap.

| Run | Wall clock | Agent sessions | Notes |
|---|---|---|---|
| Plan A | 60 min fixed, plus about 30 min setup and submit | 1 long session, plus continue prompts | the dominant cost; token use scales with how many boards the agent attempts |
| Plan A2 (optional) | up to 15 min | 1 short session | small |
| Plan B | up to 60 min (judge timer) | 1 session, one run branch | smaller than Plan A: reads evidence, writes JSON |

Both plans together use roughly two and a half hours of wall clock and three
agent sessions. If the operator's seat bills per token, take the token counts
from the first Plan A transcript to price the rest, and recommend a cap of
about 1.5x that figure for the second run.

**Abort**

- Stop the agent: interrupt the harness (Ctrl-C in Codex CLI or opencode), then
  close the terminal session.
- Check nothing is left running: `pgrep -fa 'codex|opencode|kct|kicad-cli'`.
- Do not run `bench/finish` after an abort, unless the operator wants the
  partial deliverables kept. An unfinished `run/<run-id>` branch and the
  workspace under `~/pcba-bench-runs/<run-id>/` can be deleted.
- Abort immediately on any of: the spend cap reached; a login prompt asking for
  a credential value; the agent being sent anything other than the two allowed
  prompts; the transcript showing access to `rjwalters/kicad-tools` `boards/`;
  a command that would change a shared tool install (`uv tool install`,
  `pip install --user`, `npm -g`, `apt`).
- Comment on #7776 with what ran, how long, and why it stopped. A partial run
  is still gap-report material.

## Gap report template

Each gap found in a run becomes **one issue against Epic #5952**. One gap, one
issue: if a run hit three problems, file three issues. Search open issues for
the same step first and add a comment there instead of filing a duplicate.

File with the label set the repo normally uses for a new issue; do not add
`loom:issue` (the maintainer approves that). Link the epic by writing
`Epic #5952` in the first line and add the issue to the epic's checklist in a
comment on #5952.

```markdown
**Epic**: #5952, Harness-neutral kicad-tools
**Found in**: #7776, Plan <A|A2|B>, run `<run-id or "scratch">`, <date>

## Gap: <one line: what does not work, from the agent's point of view>

- **Harness**: <Codex CLI | opencode> <version>, model <id>
- **kicad-tools**: <kct --version>, installed via <venv|uvx|...>
- **Step**: <the exact step of the plan, for example "Plan A step 1:
  `kct mcp setup --client codex`", or "during the run: first `kct route` call">
- **Expected**: <what the plan or the docs say should happen>
- **Observed**: <what happened, with the command and the relevant lines of
  output. Trim; no secrets, no tokens, no account names>
- **Workaround**: <what got past it, or "none". Anything here means criterion
  "no workaround" was not met>
- **Severity**: <blocks the run | needs a workaround | cosmetic>
- **Surface**: <CLI | MCP server | skills | agent primer | docs | pcba-bench>
- **Evidence**: <path or link to the transcript line, the run branch commit, or
  the config file (with values redacted)>

## Suggested fix (optional)

<where in kicad-tools you think it belongs, for example `kct mcp setup` or the
codex skill renderer. Leave out if unknown.>
```

Gaps that belong to pcba-bench (its scripts, prompts or the supplement) are
filed in `rjwalters/pcba-bench` and cross-linked from #7776 instead.

## After the runs

- Post on #7776: the filled checklist, what ran, the verdict per plan (pass,
  pass with gaps, fail), and links to every gap issue.
- Tick the Phase 3 validation item in #5952 only when Plan A passes with no open
  gap that has severity "blocks the run" or "needs a workaround". Success
  criterion 3 of the epic is then met.
- Update this page with anything the run showed to be wrong in it.
