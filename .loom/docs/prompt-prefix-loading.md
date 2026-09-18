# Finding (#8065): `sweep-*` progressive disclosure IS honored at spawn time

**Status**: investigation complete, **no bug found**. Nothing in Loom's spawn
or dispatch layer inlines a slash command's sibling markdown. The "Load when"
table in `.claude/commands/loom/sweep.md` (source:
`defaults/.claude/commands/loom/sweep.md`)
is a real runtime contract, not aspirational prose. Measured over 134
post-split sweep sessions, a sibling file marked "Mode C only" is loaded in
**0 of 131** Mode A/B runs, and a sibling marked "Modes A and B only" is
loaded in **0 of 3** Mode C runs.

This doc records the trace and the evidence so the question does not have to
be re-opened, and gives the reproducible recipe for re-measuring it. It is
also the correction to parent issue #8053's item 2/4 framing ("the `sweep-*`
skill family is loaded whole (12 files) even when only one mode runs") — that
was true of the **pre-#7726** monolithic `sweep.md`, and stopped being true
when the split landed.

## 1. The spawn chain, traced

A daemon-dispatched sweep is three hops, and the prompt payload is a short
*reference* at every one of them:

| Hop | Code | What it passes |
|---|---|---|
| Daemon builds the prompt | `loom-daemon/src/sweep_registry/dispatch.rs` (`SweepRegistry::spawn_child`, the `let prompt = match kind` block) | `"/loom:sweep {issue} --claim-owned {issue}"` (Issue) or `"/loom:sweep --prs {joined}"` (PrSet). Roughly 40 bytes. No file is read, opened, or concatenated. |
| Daemon execs the spawner | same function, `cmd.arg("-p").arg(&prompt)` | one `-p` arg plus `--model` / `--effort` / `--dangerously-skip-permissions` / `--use-wrapper` |
| Spawner execs the CLI | `defaults/scripts/spawn-claude.sh`, final `exec … claude "${PASSTHROUGH_ARGS[@]}"` | every non-wrapper token forwarded **verbatim**. The script parses only `--use-wrapper`, `--help`, and `--`; it never touches the `-p` value. |

The role-runner path (`loom-daemon/src/role_runner.rs`,
`resolve_role_prompt`) has the same shape: it returns `spec.prompt`
unchanged for every role except `architect`, which appends
`--max-proposals <n>` — again a flag, not file content.

So **no Loom-owned code can inline a sibling file**, because no Loom-owned
code on this path reads one.

## 2. What the CLI does with `/loom:sweep`

Claude Code expands the named command file and **only** that file. Straight
from a real transcript
(`~/.claude/projects/-home-ubuntu-GitHub-loom/256996c9-….jsonl`, a daemon
dispatch of issue #8086):

| Record | Type | Size | Content |
|---|---|---|---|
| 2 | `user` | 139 chars | `<command-message>loom:sweep</command-message>` / `<command-name>/loom:sweep</command-name>` / `<command-args>8086 --claim-owned 8086</command-args>` |
| 3 | `user` (`isMeta`) | **13,530 chars** | the body of `sweep.md`, verbatim |
| 4 | `attachment` | 692 B | `deferred_tools_delta` |
| 5 | `attachment` | 4,331 B | `agent_listing_delta` |
| 6 | `attachment` | 12,184 B | `skill_listing` — **79 skills, one line each** |
| 7 | `attachment` | 51 B | `command_permissions` |

`sweep.md` on disk is 13,607 bytes. The expanded record is 13,530 chars — the
file, minus trailing whitespace. Markdown links inside it are *not* followed.

The eleven sibling `sweep-*.md` files appear at turn 1 only as **names**, in
two places: `sweep.md`'s own reference-file map, and one line each in the
`skill_listing` attachment —

```
- loom:sweep-mode-c-lifecycle: Sweep — PR-set wave lifecycle (Mode C only)
- loom:sweep-wave-lifecycle: Sweep — Wave lifecycle (Modes A and B only — issue-set)
```

That listing is the whole progressive-disclosure mechanism: every
`.claude/commands/loom/*.md` is surfaced as a skill by name + first-heading
description, and its body is fetched later by the `Skill` tool. Headless
`claude -p` gets the same `Skill`/`ToolSearch` deferred-tool machinery an
interactive session gets — that was the open question in #8065's
implementation guidance, and the answer is yes.

## 3. Measured: siblings load on demand, and the gates hold

704 `/loom:sweep` sessions from this host's transcript store, split by whether
the expanded command body is the pre-#7726 monolith or the post-split
dispatcher:

| Cohort | n | Median expanded body | Median turn-1 input tokens |
|---|---|---|---|
| Pre-split (`sweep.md` = 426,781 B @ `9e12c1ff`) | 570 | 421,421 chars | **199,127** |
| Post-split (`sweep.md` = 13,607 B @ `3cb55893`) | 134 | 13,530 chars | **49,277** |

`Skill`-tool load counts across the 134 post-split sessions, against each
file's own "Load when" annotation:

| Sibling | "Load when" | Mode A/B (n=131) | Mode C (n=3) |
|---|---|---|---|
| `sweep-arguments` | Always, first | 121 | 2 |
| `sweep-backend-detection` | Always, at sweep start | 120 | 2 |
| `sweep-execution-model` | Always, before dispatch | 118 | 1 |
| `sweep-run-hygiene` | Before the first wave | 119 | 2 |
| `sweep-scheduling-signals` | Before the confirmation gate | 91 | 1 |
| `sweep-wave-lifecycle` | **Modes A and B only** | 118 | **0** |
| `sweep-mode-c-lifecycle` | **Mode C only** | **0** | 2 |
| `sweep-summary-output` | The run is settling | 12 | 0 |
| `sweep-dry-run` | `--dry-run` present | 0 | 0 |
| `sweep-examples` | Optional, never required | 0 | 0 |
| `sweep-reference` | Look-up only | 0 | 0 |

The two mutually-exclusive lifecycle files are the load-bearing rows: each is
loaded in its own mode and **never** in the other. `sweep-dry-run` is 0
because no sampled run passed `--dry-run`; `sweep-examples` and
`sweep-reference` are 0 because nothing reached their trigger. Those three are
consistent-but-weaker evidence (absence of a trigger, not a demonstrated
gate); the mode-C/wave pair is the demonstrated one, in both directions.

## 4. Where #8053's 201k came from

#8053 measured "last 2 days, 2,480 sessions" and reported a 201k median
turn-1 context for sweep. That number is real, and it was taken from a window
(2026-09-15/16) that sat entirely on the **pre-split** `sweep.md`. The split
landed the next day:

```
3cb55893  2026-09-16  docs(sweep): restructure sweep.md under progressive disclosure (#7726) (#7759)   13,607 B
9e12c1ff  2026-09-15  feat(daemon): start the lease-renewal loop …                                    426,781 B
```

Daily median turn-1 tokens for sweep sessions track that commit exactly:

| Day | n | Median turn-1 | Median expanded body |
|---|---|---|---|
| 2026-09-13 | 64 | 199,153 | 421,421 |
| 2026-09-14 | 70 | 199,197 | 421,421 |
| 2026-09-15 | 127 | 199,118 | 421,421 |
| 2026-09-16 | 128 | 198,620 | 423,776 |
| 2026-09-17 | 101 | **49,409** | **13,530** |

A ~75% cut in sweep's turn-1 context, already banked by #7726 before #8053
was filed. The residual ~49k is Claude Code's own system prompt + tool
schemas (incl. MCP servers), repo `CLAUDE.md` (19,090 B), the `skill_listing`
and `agent_listing` attachments, and `sweep.md` itself (~3.4k est. tokens) —
it is **not** further decomposed here, and reducing it is #8064/#8066's scope,
not this finding's.

## 5. Recipe: re-measure it yourself

No script ships for this (see the language policy — new executable logic goes
into `loom-daemon`, not a new `.sh`). Paste this to reproduce any number
above:

```python
import json, glob, os, statistics
from collections import Counter
rows = []
for p in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
    sweep = False; args = ""; exp = None; first = None; skills = set()
    for line in open(p, errors="replace"):
        try: d = json.loads(line)
        except Exception: continue
        t = d.get("type"); m = d.get("message") or {}
        if t == "user" and not sweep:
            c = m.get("content")
            if isinstance(c, str) and "<command-name>/loom:sweep</command-name>" in c:
                sweep = True
                args = c.split("<command-args>")[1].split("</command-args>")[0]
            continue
        if not sweep: continue
        if t == "user" and d.get("isMeta") and exp is None:
            c = m.get("content")
            exp = len(c if isinstance(c, str) else (c[0].get("text", "") if c else ""))
        if t == "assistant":
            u = m.get("usage") or {}
            if u and first is None:   # turn-1 context = every input counter
                first = (u.get("input_tokens", 0)
                         + u.get("cache_read_input_tokens", 0)
                         + u.get("cache_creation_input_tokens", 0))
            for b in m.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
                    s = (b.get("input") or {}).get("skill", "")
                    if s.startswith("loom:sweep-"): skills.add(s)
    if sweep and first and exp:
        rows.append((exp, first, args, skills))

post = [r for r in rows if r[0] <= 20_000]          # post-#7726 dispatcher
ab   = [r for r in post if "--prs" not in r[2]]     # Modes A/B
c    = [r for r in post if "--prs" in r[2]]         # Mode C
print(len(post), int(statistics.median([r[1] for r in post])))
print("A/B:", Counter(s for r in ab for s in r[3]))
print("C:  ", Counter(s for r in c  for s in r[3]))
```

The two assertions that matter: `loom:sweep-mode-c-lifecycle` must be absent
from the A/B counter, and `loom:sweep-wave-lifecycle` absent from the C one.

## 6. What this does *not* cover

- **`sweep-summary-output` loads in only 12 of 123 finished, substantial
  Mode A/B runs** — a file whose trigger ("the run is settling") should fire
  on essentially every completed run, and which carries the transcript-archival
  completion hook. That is the *opposite* failure mode from the one this issue
  investigated (under-loading a needed file, not over-loading an unneeded one),
  so it is out of scope here and filed separately.
- This measures one host's transcript store. The mechanism (`skill_listing` +
  `Skill` tool) is Claude Code's, so a different runtime adapter
  (see [`runtime-adapters.md`](runtime-adapters.md)) may resolve
  `/loom:sweep` differently and would need its own trace.
- The `~49k` post-split floor is not decomposed. #8066 (prefix ordering for
  cross-session cache hits) and #8064 (trimming `champion-*` /
  `judge-reference.md` / `watch.md`) own that.

## 7. Regression guard

`loom-daemon/src/sweep_registry/dispatch/prompt_shape_tests.rs` →
`dispatch_prompt_is_a_bare_slash_command_reference` pins the finding on the
Loom side: the `-p` argv token must be exactly
`/loom:sweep <N> --claim-owned <N>` (or `/loom:sweep --prs …`), short, and
free of any sibling-file body. If someone ever "helpfully" pre-expands the
skill family into the dispatch prompt, that test fails. The CLI-side half
(which files the session actually fetches) is not unit-testable from here —
re-run the recipe in §5 instead.
