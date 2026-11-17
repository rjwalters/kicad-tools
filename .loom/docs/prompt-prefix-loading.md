# Finding (#8065): `sweep-*` progressive disclosure IS honored at spawn time

> §1-§7 are #8065's finding (no bug in the spawn/dispatch layer).
> **§8 is #8110's** — the one anomaly §6 parked, where the mechanism works but
> a *needed* file's trigger was too weak to be reached. Two findings, one
> measurement apparatus.

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
  so it was out of scope here and filed separately as #8110.
  **Resolved — see [§8](#8-8110-the-summary-output-under-load-measured-and-fixed).**
  Short version: the null hypothesis explains the *denominator* (most finished
  sweeps never settle — they are killed mid-wave by an account limit) but not
  the *numerator*: among runs that genuinely settled, the transcript-archival
  hook ran on 15/15 that opened the file and 1/28 that did not.
- This measures one host's transcript store. The mechanism (`skill_listing` +
  `Skill` tool) is Claude Code's, so a different runtime adapter
  (see [`runtime-adapters.md`](runtime-adapters.md)) may resolve
  `/loom:sweep` differently and would need its own trace.
- The `~49k` post-split floor is not decomposed. #8066 (prefix ordering for
  cross-session cache hits) and #8064 (trimming `champion-*` /
  `judge-reference.md` / `watch.md`) own that.
  **#8066 is answered** — see
  [`prompt-prefix-cache-ordering.md`](prompt-prefix-cache-ordering.md): the
  ordering hypothesis is falsified (Loom's injected prefix is already last and
  contiguous, and cache matching is block-granular so intra-block reordering is
  a no-op), and the real determinant of a hit is which OAuth account the spawn
  selected — 65% full-hit rate on a same-account repeat within the hour vs 1.4%
  otherwise.

## 7. Regression guard

`loom-daemon/src/sweep_registry/dispatch/prompt_shape_tests.rs` →
`dispatch_prompt_is_a_bare_slash_command_reference` pins the finding on the
Loom side: the `-p` argv token must be exactly
`/loom:sweep <N> --claim-owned <N>` (or `/loom:sweep --prs …`), short, and
free of any sibling-file body. If someone ever "helpfully" pre-expands the
skill family into the dispatch prompt, that test fails. The CLI-side half
(which files the session actually fetches) is not unit-testable from here —
re-run the recipe in §5 instead.

## 8. #8110: the `summary-output` under-load, measured and fixed

§6 parked one anomaly out of #8065's scope: `sweep-summary-output.md` loaded in
only **12 of 123** finished Mode A/B runs, against a "Load when: the run is
settling" trigger that should fire on essentially every completed run. #8110
asked which of four candidate explanations held — a weak trigger, tail context
pressure, sessions ending before settling (the null hypothesis), or the content
being answered from `sweep.md`'s prose without fetching the sibling.

**Answer: the null hypothesis explains the denominator but not the numerator.**
Both halves are true at once, which is why the raw ratio looked so damning and
is not by itself the defect.

### 8.1 Cohort

Re-measured on 2026-09-18 with §5's recipe extended to also classify each
session's **terminal state**: 156 finished (idle > 1h), substantial (> 60
transcript records), post-#7726 Mode A/B `/loom:sweep` sessions across 12 repos
on one host, window 2026-09-16 → 2026-09-18. (Larger than #8065's 123 because
two more days of sessions had accumulated; same host, same filters.)

The classifier keys on the last assistant text blocks: an account/session/
weekly/spend-limit message ⇒ *killed*; a terminal report (summary table, "sweep
complete", `→ merged`/`blocked`/`skipped`, "issue closed", "doctor cycle
exhausted") ⇒ *settled*; a "continuing to poll / dispatching X" tail with
neither ⇒ *killed mid-poll*.

| Terminal state | n | loaded `summary-output` | ran archival hook | loaded `wave-lifecycle` |
|---|---|---|---|---|
| **KILLED**: account/session/weekly/spend limit | 71 | 0 | 0 | 56 |
| **KILLED**: mid-poll on a role subagent | 36 | 0 | 0 | 30 |
| Indeterminate tail | 6 | 0 | 0 | 5 |
| **SETTLED**: printed a terminal report | 39 | 8 | 16 | 24 |
| **SETTLED**: merged, no report in tail | 4 | 0 | 0 | 4 |
| | **156** | **8** | **16** | **119** |

**107 of 156 (69%) never reached a settle point at all** — they were killed
mid-wave, overwhelmingly by a Claude account limit (`"You've hit your
session/weekly limit"`, `"hit your org's monthly spend limit"`). None of the 107
loaded the file and none ran the archival hook, which is **correct**: there was
no settle to summarise. That is candidate (3), and it accounts for the bulk of
the 12/123 shortfall. It is a sharper cause than #8110 guessed — not merges,
blocks, or operator gates exiting early, but rate-limit kills mid-flight.

### 8.2 The defect is in the 43 that did settle

Of the 43 sessions that genuinely settled, only **15 (35%)** consulted
`sweep-summary-output.md`. Whether they did is almost perfectly predictive of
whether the transcript-archival completion hook (`archive-transcripts.sh`, the
file's one load-bearing side effect) ran:

| | ran archival hook | did not |
|---|---|---|
| opened `sweep-summary-output.md` | **15** | 0 |
| did not open it | 1 | **27** |

`P(archived | opened) = 15/15`; `P(archived | not opened) = 1/28`. Fisher exact
two-sided **p ≈ 1.1 × 10⁻¹⁰**. So **27 of 43 settled sweeps (63%) skipped the
archival hook** — the cost #8110 predicted, now measured. These were not
half-finished runs: sampled tails show PRs merged, issues closed, worktrees and
branches cleaned, checkpoints and run-registry entries removed, summary tables
printed — everything except the one step that only that file names. One tail
ends literally at step 8's integration gate ("running the post-wave integration
gate as a final health check before wrapping up") and stops there.

Candidate (4) is also **ruled out**: a session that skipped the file did not
answer from `sweep.md`'s prose instead — it skipped the *action*, not just the
*format*. Candidate (2) (tail context pressure) is not separable from (1) with
this data and needs no separate remedy: the fix for (1) removes the judgment
call that pressure was eroding.

**Root cause.** The only thing naming the archival hook was `sweep.md`'s
load-order item 8, expanded at turn 1 and 20-200 turns out of view by the time a
run settles. `sweep-wave-lifecycle.md` — the file the session is *actually*
executing, loaded in 119/156 runs — ended at step 8 ("advance to the next wave")
and step 8a (a conditional wave-boundary re-check). Neither had a terminal
branch for "there is no next wave", so reaching the settle step depended on
recognising a mood rather than following the procedure.

**Fix (this PR).** `sweep-wave-lifecycle.md` gains **step 8b** — reached
mechanically from step 8 when the candidate list is exhausted — which inlines
the `archive-transcripts.sh` invocation (so the load-bearing action needs no
second file fetch) and then names `sweep-summary-output.md` for the table
format. `sweep-mode-c-lifecycle.md` C3 gains the same terminal branch: Mode C
prints its summary inline but had the identical hook gap, unmeasurable at n=3.
The "Load when" rows in `sweep-summary-output.md` and `sweep.md` now name those
steps instead of "the run is settling".

### 8.3 Two methodology corrections to §5

1. **`Skill`-only counting undercounts consultation.** 7 of the 15 openers
   opened the file with the **`Read`** tool against its
   `.claude/commands/loom/` path, not the `Skill` tool — so the true
   consultation rate is 15/156, not 8/156, and §5's table understates this file
   by ~47%. Count both. (It does not affect §5's load-bearing mode-gate
   assertions, which are *absences*: neither counter contains the wrong
   lifecycle file.)
2. **Settle state must be classified, not assumed.** "Finished" (file idle >
   1h) is not "settled". On a fleet running near its account limits, most
   finished sweep transcripts are kills, and any per-file load rate computed
   over them is diluted by a factor of ~3.6 here. Condition on the terminal
   state before reading a load rate as a compliance rate.

### 8.4 Caveat: the realised loss is currently zero

Transcript archival is **not opted in on this host** — `LOOM_TRANSCRIPT_ARCHIVE`
is unset and no repo's `.loom/config.json` carries a `loom.transcriptArchive`
block — so `archive-transcripts.sh` is a no-op today and the 27 skipped hooks
destroyed nothing. That is also why the gap went unnoticed for so long: the
symptom is invisible until someone opts in, at which point ~63% of settled
sweeps would silently fail to capture their subagent transcripts. The measured
signal is therefore "did the session perform the step", which is exactly the
compliance question — but the *cost* is latent, not realised. Re-running §5 +
§8.1 after this change is the verification; the honest post-fix check is a
fresh cohort of settled runs, and on this fleet accumulating 43 of those takes
days.

### 8.5 Recipe delta: add settle-state classification to §5

§5's loop already yields per-session `Skill` loads. Collect three more fields
inside it — every `Bash` tool_use `command`, every `Read` `file_path`, and the
assistant **text** blocks — then classify. Note `Agent`, not `Task`, is the
subagent-dispatch tool in this CLI; keying on `Task` finds zero dispatches and
makes every session look like a pre-flight skip.

```python
# inside §5's per-block loop, alongside the Skill branch:
if b.get("type") == "tool_use":
    if b["name"] == "Bash":  bash.append((b.get("input") or {}).get("command", ""))
    if b["name"] == "Read":  reads.add(str((b.get("input") or {}).get("file_path", "")))
    if b["name"] == "Agent": agents[(b.get("input") or {}).get("subagent_type", "?")] += 1
elif b.get("type") == "text" and b.get("text", "").strip():
    texts.append(b["text"])

# then, per session:
import re
LIMIT = re.compile(r"hit your (session|weekly|usage) limit|monthly spend limit", re.I)
TERM  = re.compile(r"sweep complete|(→|->) *(merged|blocked|skipped|routed)|"
                   r"issue #?\d+ (is )?closed|doctor cycle exhausted", re.I)
tail     = "\n".join(texts[-3:])
killed   = bool(LIMIT.search(tail))
settled  = (not killed) and (bool(TERM.search(tail)) or "merge-pr.sh" in "\n".join(bash))
opened   = ("loom:sweep-summary-output" in skills
            or any("sweep-summary-output" in r for r in reads))
archived = "archive-transcripts.sh" in "\n".join(bash)
```

Verified: run as written on 2026-09-18 (cohort of 157 by then, one more session
having aged past the 1h idle cut) it reports 8 `Skill` loads, 15 openers, 72
killed, 42 settled, and the same contingency — 15/15 archived among openers vs
1/27 among non-openers. §8.1's table came from a slightly wider regex set (it
also catches `"resets <time>"` limit tails, `"## Summary"` headings, and a
"continuing to poll" mid-poll tail as its own bucket), which moves one session
between *settled* and *indeterminate*; the conclusion is identical either way.

The assertion that matters post-fix: among sessions with `settled == True`,
`archived` should approach 1.0 **independently** of `opened`, because step 8b
now carries the command inline. A post-fix cohort where `archived` still tracks
`opened` means the terminal step is still not being reached.
