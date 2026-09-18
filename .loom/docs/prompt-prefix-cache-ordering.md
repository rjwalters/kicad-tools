# Finding (#8066): prefix **ordering** is not why cross-session cache hits are rare

**Status**: investigation complete, **no reorder implemented — the hypothesis
was measured and falsified.** #8053 item 3 proposed putting byte-stable content
(role prompt, skills, repo `CLAUDE.md`) first and volatile content (issue
number, PR body, timestamps, run ids) last, on the theory that interleaving is
what keeps two judge ticks minutes apart from sharing a prefix. Three things
came out of tracing and measuring it:

1. **A role tick's prompt contains no volatile content at all.** Loom's entire
   contribution to the assembled prompt is the argv string `/loom:judge` — 12
   bytes, byte-identical on every tick. There is nothing to reorder.
2. **Where volatile content does exist (a sweep's issue number), moving it is a
   no-op.** Prompt-cache lookups happen at *content-block* boundaries, so a
   byte difference anywhere in a block invalidates the whole block. Measured:
   volatile-first and volatile-last layouts of the same command body produced
   **identical** misses (25,029 vs 25,027 tokens re-written).
3. **The stable prefix is already fully cacheable, and does hit — when the same
   account runs it again.** Production judge ticks with
   `cache_creation_input_tokens = 0` and `cache_read_input_tokens = 109,056`
   exist: the whole 109k prefix, Loom-injected role prompt included, read from
   cache. Whether a tick lands there is governed by **which OAuth account the
   spawn selected** (the cache is per account/organization), not by ordering:
   **65%** hit rate when the previous same-role tick within an hour used the
   same account, **1.4%** when it did not.

The actionable levers are therefore elsewhere and are tracked separately:
**#8146** (cache-affinity in token selection — the 65%/1.4% gap) and **#8147**
(`CLAUDE.md`'s `**Loom Version**` stamp, which invalidates every downstream
cached byte on each bump). This doc records the trace, the three experiments,
and the recipe, so the ordering question does not have to be re-opened.

> Sibling finding, same measurement apparatus:
> [`prompt-prefix-loading.md`](prompt-prefix-loading.md) (#8065/#8110 — progressive
> disclosure *is* honored at spawn time). That doc's §6 parked the "~49k floor"
> to this issue; this doc is that answer.

## 1. The assembled prompt, traced end to end

There is no prompt *assembly* in Loom. Every dispatch path passes one short
slash-command reference and lets the CLI assemble the session:

| Hop | Code | Prompt payload |
|---|---|---|
| Role tick | `loom-daemon/src/role_runner.rs` → `resolve_role_prompt` (`:2664`) | `spec.prompt` verbatim — `/loom:judge`, `/loom:curator`, … (`RoleSpec` table at `:503`+). Only `architect` differs: `/loom:architect --max-proposals <n>`. |
| Role tick exec | same file, `run_role_with_timeout` (`:1439`–`:1451`) | `cmd.arg("-p").arg(prompt)`, then `--model` / `--dangerously-skip-permissions` / `--use-wrapper`. No file is read or concatenated. |
| Sweep dispatch | `loom-daemon/src/sweep_registry/dispatch.rs` (`:2952`–`:2970`) | `"/loom:sweep {issue} --claim-owned {issue}"` or `"/loom:sweep --prs {joined}"`, then `cmd.arg("-p").arg(&prompt)`. |
| Spawner | `defaults/scripts/spawn-claude.sh`, final `exec … claude "${PASSTHROUGH_ARGS[@]}"` (`:1237`) | forwards every non-wrapper token **verbatim**; it selects an OAuth token and never touches the `-p` value. |

No `--append-system-prompt`, `--system-prompt`, or equivalent is used anywhere
in the tree. So the order of role prompt / skills / `CLAUDE.md` / env in the
request is **entirely Claude Code's**, and it is:

```
[system]  CC base system prompt + tool schemas                    ~24k tok  (breakpoint)
[system]  + env block (cwd, platform, git branch/status/commits)   ~5.4k tok (breakpoint)
[user]    repo CLAUDE.md (system-reminder) + <command-args> +
          the expanded .claude/commands/loom/<role>.md body       ~20-80k tok (breakpoint)
[user]    attachments: deferred_tools_delta, agent_listing_delta,
          skill_listing, command_permissions
```

The three breakpoints are directly observable as the three `cache_read`
plateaus in the experiments below (23,976 / ~29,4xx / everything).

Loom's injected content is therefore already **last**, contiguous, and
downstream of every volatile thing in the request. The ordering #8053 asked for
is the ordering that already exists.

## 2. Experiment A — the reorder itself, measured

One pinned account (same cache namespace), six runs within ~4 minutes, cwd with
no `CLAUDE.md`, a ~25k-token deterministic body, two variants of the same
command file:

- **variant A (today's layout)**: `**Arguments**: $ARGUMENTS` near the top, stable body after.
- **variant B (the proposed reorder)**: stable body first, `**Arguments**: $ARGUMENTS` last.

| run | command | args | cache_read | cache_create | verdict |
|---|---|---|---|---|---|
| 1 | variant A | `111` | 29,371 | 25,029 | cold write |
| 2 | variant A | `111` | **54,400** | **0** | full hit — caching works cross-session |
| 3 | variant A | `222` | 29,371 | **25,029** | miss: whole body re-written |
| 4 | variant B | `111` | 29,371 | 25,027 | cold write |
| 5 | variant B | `111` | **54,398** | **0** | full hit |
| 6 | variant B | `222` | 29,371 | **25,027** | **miss — identical to run 3** |

Runs 2 and 5 are the control: an identical prompt from a *different session*
minutes later reads the entire prefix from cache. The apparatus can see a hit.

Runs 3 vs 6 are the answer: **moving the volatile substitution to the end of the
body changed the outcome by 2 tokens** (the two bodies differ slightly in
length). Both re-wrote everything. The expanded command body is one content
block; cache matching is prefix-exact up to a block boundary, so "volatile last"
and "volatile first" are the same thing to the cache.

This is why the acceptance criterion's conditional ("*if* volatile content
precedes stable content: reorder") does not resolve to an implementation: at the
one place Loom injects volatile content, the reorder provably buys nothing.

## 3. Experiment B — what *does* invalidate the prefix (→ #8147)

Same shape, one pinned account, three runs, identical command body and identical
arguments; a `CLAUDE.md` in the cwd, with **only its `**Loom Version**:` line
differing between run 2 and run 3:

| run | `CLAUDE.md` | cache_read | cache_create |
|---|---|---|---|
| 1 | `1.0.0` (cold) | 23,976 | 32,952 |
| 2 | unchanged — control | **56,928** | **0** |
| 3 | `1.0.1` — only that line | 29,373 | **27,555** |

Run 3 fell back to the *second* breakpoint (29,373 — base + env matched) and
re-wrote 27,555 tokens, i.e. `CLAUDE.md` **and the byte-identical command body
behind it**. `scripts/version.sh` rewrites exactly that line in `CLAUDE.md` on
every bump (`VERSION_FILES` at `:31`, the `sed` at `:197`), and `main` bumps on
nearly every merge — so each pull that carries a bump drops every account's warm
prefix for that repo. Filed as **#8147**; note it cannot be fixed by reordering
`CLAUDE.md` either (same block-granularity argument), only by keeping the
volatile bytes out of the prefix.

## 4. Experiment C — production correlation (→ #8146)

`rjwalters/loom` role ticks from this host's transcript store, 2026-09-16 →
2026-09-18, joined to the account recorded in `.loom/logs/role-<role>.log`
(`spawn-claude: using OAuth account '<name>'`). "Full hit" =
`cache_creation_input_tokens == 0` on the first assistant message.

| role | ticks | prev same-role tick <60m, **same** account | full hit | prev tick, **different** account | full hit |
|---|---|---|---|---|---|
| guide | 124 | 50 | **37** | 73 | 0 |
| doctor | 82 | 22 | **12** | 59 | 1 |
| judge | 71 | 13 | **6** | 57 | 1 |
| curator | 50 | 5 | **4** | 44 | 1 |
| champion | 44 | 4 | 2 | 39 | 1 |
| auditor | 7 | 0 | 0 | 6 | 0 |
| **total** | **378** | **94** | **61 (65%)** | **278** | **4 (1.4%)** |

The longest observed hit interval was 56 minutes (`agent6-2amlogic`, judge ticks
at 03:03 → 03:59 on 2026-09-18), consistent with the 1h ephemeral TTL Claude
Code writes with (`cache_creation.ephemeral_1h_input_tokens` is the populated
counter in these transcripts).

Two judge ticks 4 minutes apart *did* both miss (01:49:59 and 01:53:07 on
2026-09-17, each re-writing 79,379 tokens) — the scenario #8053 cites as proof
of an ordering bug. They ran on different accounts. That is the whole
explanation.

Fleet-wide over the same window (all 12 repos on this host, 5,216 role + sweep
sessions): **258.7M turn-1 cache-write tokens, 544 full hits (10.4%)**. The
ceiling implied by the same-account rate is ~65%.

## 5. What this means for the sweep path specifically

A sweep's prompt *does* carry volatile content — `/loom:sweep 8066 --claim-owned
8066` — and every daemon-dispatched sweep therefore re-writes `sweep.md`'s
~19.9k-token block (observed: `cache_create = 19,868`, identical across 40+
consecutive dispatches). Reordering cannot fix that (§2), and the number cannot
leave the argv string without breaking `live_claim.rs`'s cmdline-based ownership
detection (`:315`–`:329`, which greps the child's `/loom:sweep` argv for the
issue it claims) — so it is deliberately **not** attempted here. If it is ever
worth pursuing, the shape would be "pass the issue via env and teach live-claim
another source", which is a behaviour change, not a reorder.

## 6. Regression guard

`loom-daemon/src/role_runner/tests.rs` →
`test_role_tick_prompt_carries_no_volatile_content` pins the property that makes
the role-tick prefix cacheable at all: every shipped role's resolved prompt must
match `/loom:<name>` exactly, or `/loom:architect --max-proposals <n>` for
architect — no issue number, no timestamp, no run id, and byte-identical across
repeated resolution with the same config. If someone ever interpolates
per-invocation context into a role tick's prompt, that test fails and this
finding stops being true.

The sweep half is already pinned by #8065's
`dispatch_prompt_is_a_bare_slash_command_reference`
(`loom-daemon/src/sweep_registry/dispatch/prompt_shape_tests.rs`).

## 7. Recipe: re-measure any number above

Two halves. **First**, per-session turn-1 cache counters from the transcript
store (the same source as #8053's median table):

```python
import json, glob, os, re, collections, statistics
rows = collections.defaultdict(list)
for p in glob.glob(os.path.expanduser("~/.claude/projects/-home-ubuntu-GitHub-loom/*.jsonl")):
    role = ts = first = None
    for line in open(p, errors="replace"):
        try: d = json.loads(line)
        except Exception: continue
        if d.get("type") == "user" and role is None:
            c = d.get("message", {}).get("content")
            s = c if isinstance(c, str) else json.dumps(c)
            m = re.search(r"<command-name>(.*?)</command-name>", s)
            if m: role, ts = m.group(1), d.get("timestamp")
        if d.get("type") == "assistant":
            u = d.get("message", {}).get("usage") or {}
            if u: first = u; break
    if role and first and ts:
        rows[role].append((ts, first.get("cache_read_input_tokens", 0),
                                first.get("cache_creation_input_tokens", 0)))
for role, rs in sorted(rows.items()):
    print(role, len(rs),
          "median create", int(statistics.median(c for _, _, c in rs)),
          "full hits", sum(1 for _, _, c in rs if c == 0))
```

**Second**, the account each session ran on, to reproduce §4's correlation —
parse `.loom/logs/role-<role>.log` for
`[<ts>] spawn-claude: using OAuth account '<name>'` and join to a session whose
first timestamp is 0–8s later (the account line is emitted immediately before
`exec claude`).

For §2/§3 (the controlled runs) the shape is: pin one account by exporting
`CLAUDE_CODE_OAUTH_TOKEN` from `~/.loom/tokens/<name>.token` with
`LOOM_SPAWN_NO_EXPORT=1`, put a throwaway command file in a scratch dir's
`.claude/commands/`, and invoke
`spawn-claude.sh -p "/<cmd> <args>" --model sonnet --output-format json
--dangerously-skip-permissions` from that dir, reading `.usage` out of the JSON
result. Keep the body over ~1k tokens (the cache minimum) and ask for a
one-word answer so output cost is negligible. Budget ~25k cache-write tokens per
cold run.

## 8. What this does *not* cover

- **One host, one runtime.** The block-granularity conclusion is a property of
  the Anthropic caching API, but the *breakpoint placement* is Claude Code's and
  a different runtime adapter ([`runtime-adapters.md`](runtime-adapters.md))
  would need its own trace.
- **The env block's own volatility.** The `<env>` / git-status segment (~5.4k
  tokens, breakpoint 2) carries the branch and recent-commit subjects and is
  injected by the CLI upstream of everything Loom owns. It was not separately
  characterised here; it is a plausible second-order cause of misses on a repo
  whose `main` moves constantly, and no Loom-side lever for it is known.
- **Trimming the prefix.** Making the prefix smaller is #8064's scope; measuring
  and budgeting it is #8053's own narrowed scope. This doc only answers whether
  *ordering* it differently would help.
