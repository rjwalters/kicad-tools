# Guardrail Parity: Codex

This is the **required guardrail-parity document** for the Codex runtime adapter
(`defaults/scripts/spawn-codex.sh`), per contract point 6 of
[`runtime-adapters.md`](runtime-adapters.md). No runtime is admitted to Loom
without one. It maps **Loom guard *intent* → Codex enforcement mechanism** and
then names, explicitly, every protection Loom has that a Codex worker does
**not** get.

Read this before dispatching a Codex worker at anything you care about.

> **Provenance.** The Codex adapter is a port of the Codex support built in the
> [gpeyton/loom](https://github.com/gpeyton/loom) fork by Graham Peyton (fork
> PRs #15/#16/#20/#40, including its `GUARDRAIL-PARITY.md`, the template for
> this document). Every claim below was **re-verified against codex-cli
> 0.146.0** on 2026-07-29 (epic #4167 Phase 2, issue #4468) and several of the
> fork's statements no longer hold on that version — those are called out
> inline. Do not carry a claim from the fork's doc into this one without
> re-checking the CLI.

> **Path convention.** This file lives at
> `defaults/docs/guardrail-parity-codex.md` in the Loom source repo and cites
> `defaults/` paths. A consumer install maps `defaults/docs/` → `.loom/docs/`,
> so the installed copy is `.loom/docs/guardrail-parity-codex.md`.

## Tier status

**Codex is tier-2: CI-gated, no operator dogfooding.** It passes a mocked spawn
+ classifier CI leg; it is not run against production workloads. Promotion to
tier-1 requires someone committing to tier-1 ownership of this adapter, this
document, and that CI leg — see the contract's tier policy. Nothing in this
document should be read as "Codex is safe to point at your repos".

## The enforcement mechanisms Codex actually has (0.146.0)

| Mechanism | What it controls | How the adapter drives it |
|---|---|---|
| `-s` / `--sandbox <mode>` | Filesystem + network confinement for model-run shell commands. Modes: `read-only`, `workspace-write`, `danger-full-access`. | The adapter's central knob — see the mapping table below. |
| `[sandbox_workspace_write] network_access` | Outbound network from inside a `workspace-write` sandbox. **Off by default.** | `LOOM_CODEX_NETWORK=1` → `-c sandbox_workspace_write.network_access=true`. Read only under `workspace-write`; inert otherwise. |
| `sandbox_permissions` / `writable_roots` / `--add-dir` | Widen a `workspace-write` sandbox to extra readable/writable roots. | **Not driven by the adapter.** Passes through if an operator supplies it. |
| `--skip-git-repo-check` | Waives Codex's refusal to run outside a git work tree. | Injected **only** when the cwd is genuinely not inside a work tree (see "Trusted-directory check" below). |
| `$CODEX_HOME/hooks.json` (`pre_tool_use`, `permission_request`, `post_tool_use`, `user_prompt_submit`, `session_start`, `session_end`, `pre_compact`, `post_compact`, `subagent_start`, `subagent_stop`) | Per-tool-call and per-prompt interception — the direct analogue of Claude Code's hook taxonomy. | **`pre_tool_use` is now WIRED** (issue #4495) via the managed bridge `defaults/hooks/guard-codex-bridge.sh`, installed by `defaults/scripts/provision-codex-hooks.sh`. See "Managed `pre_tool_use` hook bridge" below. Every other event remains unwired. |
| `$CODEX_HOME/config.toml` → `hooks.state."<id>".trusted_hash` | Persisted hook trust. A hook that has not been trusted does not run. | **Verified, never bypassed.** `spawn-codex.sh` fails closed (exit 78) for mutable roles when trust cannot be observed. `--dangerously-bypass-hook-trust` is never passed. |
| `approval_policy` / `-a` | When Codex pauses to ask a human. | **Irrelevant to Loom.** `codex exec` is non-interactive and exposes no `-a` at all; there is no human to answer, so approvals gate nothing. The sandbox is the only load-bearing guard. |
| `AGENTS.md` | Repository instructions, read natively by Codex via ancestor traversal. | Advisory context, not a boundary. Loom's `AGENTS.md` codegen is a separate issue (contract point 5). |

### Corrections to the fork's parity doc, verified on 0.146.0

1. **`--full-auto` does not exist on `codex exec`.** The fork maps its safe mode
   to `--full-auto`; that flag is absent from `codex exec --help` on 0.146.0.
   `-s workspace-write` is the replacement, and it is what this adapter emits.
2. **`-a` / `--ask-for-approval` is top-level only**, not an `exec` flag. Any
   parity claim that rests on `approval_policy = "on-request"` is inert for
   Loom's headless dispatch.
3. **Codex has a hook system now.** The fork states Codex has no hooks "as a
   concept". On 0.146.0 it has a `hooks.json` engine with a `pre_tool_use`
   event, a persisted hook-trust model, and a
   `--dangerously-bypass-hook-trust` escape hatch. The gap was therefore
   *unwired*, not *impossible* — and issue #4495 wired it. See "Managed
   `pre_tool_use` hook bridge" below.
4. **A sandbox denial does not fail the run.** Verified with `-s read-only`: a
   blocked `touch` returns `Operation not permitted` to the model and the
   `codex exec` process still exits **0**. Denials are in-session tool
   failures, so they never reach error classification (see
   `defaults/scripts/lib/classify-error.sh`'s `codex` table for why no
   "sandbox denial" pattern is encoded there).

## Loom guard intent → Codex mechanism

Loom's guards are Claude Code `PreToolUse` / `Stop` hooks wired in
`.claude/settings.json` to scripts under `.loom/hooks/`. As of issue #4495 the
three `PreToolUse` guards DO fire for a Codex worker — not as Claude hooks, but
through the managed `pre_tool_use` bridge described in the next section, which
normalizes Codex's payload into the shape those same scripts already accept.
The "Codex coverage" column below still records what the *sandbox* covers on its
own, because the bridge only applies where it is provisioned AND trusted; a
session without a ready managed hook falls back to sandbox-only coverage (and
mutable roles are refused outright rather than run that way).

## Managed `pre_tool_use` hook bridge (issue #4495)

**Tested schema: codex-cli 0.146.0** (verified 2026-07-31 against the JSON
schemas embedded in the shipped binary: `pre-tool-use.command.input` and
`pre-tool-use.command.output`). Re-verify and bump this pin before wiring a
newer Codex.

### Shape

```text
Codex pre_tool_use JSON
   │  { hook_event_name:"PreToolUse", tool_name, tool_input, cwd,
   │    session_id, transcript_path, turn_id, tool_use_id, model,
   │    permission_mode, agent_id, agent_type }
   ▼
defaults/hooks/guard-codex-bridge.sh
   1. validate the event + classify the tool
   2. normalize → a Claude-shaped internal guard request
        shell  → { tool_name:"Bash",  tool_input:{command}, cwd }
        patch  → { tool_name:"Write", tool_input:{file_path}, cwd }  (one per target)
   3. dispatch into the EXISTING guards, unmodified:
        guard-loom-workflow.sh · guard-destructive.sh · guard-worktree-paths.sh
   4. encode the outcome on Codex's wire
```

There is **no second policy table**. Step 3 runs the same scripts the Claude
path runs, so a policy change lands for both runtimes at once.

### Runtime discrimination

The Codex and Claude `PreToolUse` payloads share field names, so payload
sniffing would be ambiguous and spoofable. The bridge is an **explicit wrapper**
instead: it is installed *only* as a Codex `hooks.json` `pre_tool_use` command,
it accepts only `hook_event_name == "PreToolUse"`, and Claude's
`settings.json` never points at it.

### Response encoding — 0.146.0 only supports `deny`

The output schema advertises `permissionDecision: allow|deny|ask` plus
`decision`, `continue`, `stopReason`, `suppressOutput`. The 0.146.0 engine
**rejects almost all of it**; the binary carries these literal refusals:

```text
PreToolUse hook returned unsupported permissionDecision:allow
PreToolUse hook returned unsupported permissionDecision:ask
PreToolUse hook returned unsupported decision:approve
PreToolUse hook returned unsupported continue:false
PreToolUse hook returned unsupported stopReason
PreToolUse hook returned unsupported suppressOutput
PreToolUse hook returned permissionDecision:deny without a non-empty permissionDecisionReason
```

So exactly two things are expressible:

| Outcome | Wire form |
|---|---|
| allow | emit **nothing**, exit 0 |
| deny | `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"<non-empty>"}}` |

**`ask` is therefore not a policy choice, it is not on the wire at all.** Every
Claude `ask` outcome is translated to `deny` with the original reason preserved
and prefixed — which is also the only correct answer for `codex exec`, where
there is no operator to ask.

### Tool classification

| Class | Tool names (0.146.0) | Handling |
|---|---|---|
| shell | `shell`, `shell_command`, `local_shell`, `exec_command`, `unified_exec`, `container.exec` | command string extracted from `command` / `cmd` / `action.command` / `input` (argv `<sh> -lc <script>` unwrapped to the script); run through `guard-loom-workflow.sh` then `guard-destructive.sh` |
| patch | `apply_patch`, `write_file`, `edit_file`, `create_file`, `str_replace_editor` | every target path extracted (`*** Add/Update/Delete File:` + `*** Move to:` for the patch envelope, else `file_path`/`path`), canonicalized, run through `guard-worktree-paths.sh` |
| opaque-mutating | `write_stdin` | **denied** — bytes into a live PTY are a second, un-inspectable command channel |
| readonly | `update_plan`, `view_image`, `read_file`, `list_dir`, `glob`, `grep`, `web_search`, `read_mcp_resource`, `request_permissions` | allowed |
| mcp | `mcp__*` / `mcp.*` | **passed through**, matching Claude exactly (Claude's matchers are `Bash` and `Edit\|Write`; MCP tools match neither). Denying them here would be stricter than Claude, not parity — see gap 10 |
| unknown | anything else | **denied** (fail closed) |

### Fail-closed contract (deliberately the inverse of the Claude guards)

The Claude guards fail **open** so a broken guard cannot wedge every tool call.
The bridge fails **closed**: malformed JSON, a wrong/missing `hook_event_name`,
a missing `tool_name`, a mutating tool whose payload yields no command or path,
an unknown tool, a sub-guard that exits non-zero, and a sub-guard that emits
non-JSON all produce `deny`. It still always exits 0 — a non-zero exit is a
*hook failure* on Codex's wire, not a decision.

**The bridge's own death is also a deny.** On this wire an allow is expressed as
**no output**, which makes a crashed guard indistinguishable from an approval —
the single most dangerous way for a fail-closed design to fail. An `EXIT` trap
therefore emits the deny for any exit path that has not already committed a
decision (a `set -u` unbound variable, an unexpected abort, a `SIGTERM` from
Codex's hook timeout), and forces the exit-0 contract so the deny is not
discarded as a hook failure. Two residuals, both tested-around rather than
fixable:

- **`SIGKILL` is untrappable** by definition, so a hook killed outright after
  the timeout grace period still produces silence. This is a named reason the
  capability manifest stays `partial`.
- Bash defers a trap until the **foreground child returns**, so a `SIGTERM`
  arriving while a slow sub-guard is running lands after that sub-guard
  finishes, not immediately.

### Canonicalization

Path decisions use `defaults/scripts/lib/canonical-path.sh`
(`loom_canonical_path`), which resolves symlinks in every component that exists
and normalizes the tail that does not — so a `Write` to a brand-new file still
resolves, and `<worktree>/link-to-main/x` is recognized as the main checkout.
This replaced `guard-worktree-paths.sh`'s purely lexical `os.path.normpath`, and
it applies to the Claude path too. `guard-destructive-generic.sh`'s #4178 Bash
write-confinement additionally compares against both the physical and the
logical spelling of the main-checkout root, closing the same symlinked-ancestor
hole for shell-derived writes.

Untrusted JSON values are **never interpolated into a shell command**: paths and
commands are passed to `jq`/`awk`/`python3` on stdin or as `--arg` values.

### `workdir` anchor validation (issue #4767)

`tool_input.workdir` on the `shell` tool is a **model-chosen** field — unlike
the event's top-level `cwd`, which is runtime-supplied and never reachable
from the model (the same distinction "Runtime discrimination" above draws for
the payload shape). The bridge merges `workdir` into `GUARD_CWD`, which becomes
the `cwd` of the normalized guard request `guard-destructive-generic.sh`
consumes for its #4178 Bash write-confinement block.

That block derives its main-checkout anchor (`_WT_MAIN_ROOT`) from `cwd` via
`git rev-parse --git-common-dir`, falling back to `REPO_ROOT` (itself resolved
from the same `cwd` via `git rev-parse --show-toplevel`) when that resolution
is unavailable. A `cwd` that is not inside a git repo at all makes **both**
come up empty, and the block's containment test **silently `continue`s past
every write** when the anchor is empty — it has nothing to compare against. Because `workdir` is model-controlled, a Codex worker could
therefore choose `workdir: "/tmp"` (or `"/"`, or any other out-of-repo path) to
turn off managed-worktree write confinement for every subsequent redirect/
`tee`/`sed -i`/`cp`/`mv` in the same call — while `rm -rf /`, protected-branch
force-push, and the Loom workflow denials (all cwd-independent pattern
matches) stayed correctly enforced throughout, so the gap was narrow but real.

**Resolution.** After `GUARD_CWD` is computed, the bridge requires it to
resolve to the same `git-common-dir/..` main-checkout root as the trusted
anchor — the event's own `cwd`, falling back to the provisioned
`--project-root` if the event `cwd` is not itself a repo (`main_root_of()` in
`guard-codex-bridge.sh`, the identical idiom `guard-destructive-generic.sh`
already uses for the same comparison). A `workdir` that fails this — not
inside a git repo, nonexistent, or a *different* repo than the acting
session's — `fail_closed`s instead of handing the sub-guards a rootless `cwd`.
A `workdir` that resolves into the SAME repo (absolute or relative, including
the common `"."` no-op) is unaffected. Covered by the `workdir anchor
validation` section of `test-guard-codex-bridge.sh`: `/tmp`, `/`, a
foreign/non-repo absolute path, a nonexistent directory, and a relative
`workdir` that walks out of both the worktree and the repo, each asserted
against `>` redirect, `tee`, `cp`, `mv`, and `sed -i`.

### Provisioning and trust

`defaults/scripts/provision-codex-hooks.sh install|verify|remove` owns the
managed entry in `$CODEX_HOME/hooks.json`:

- **`hooks.json` is shared user configuration.** The script parses it, merges
  exactly one Loom-owned `PreToolUse` entry (self-identified by the
  `guard-codex-bridge.sh` command plus a `--loom-hook-version N` marker),
  validates the result, and replaces the file **atomically** with mode `0600`.
  Every other event, group, handler, and unknown top-level key survives
  byte-for-byte. A malformed operator file is never overwritten (exit 78).
- **`remove`** deletes only Loom's entry (and any empty structure it leaves),
  preserving credentials and user configuration.
- **Credentials are never read, copied, parsed, or logged.** `auth.json` is not
  touched by any subcommand; only the profile *directory name* is ever printed.
- **`verify`** is read-only and reports readiness as JSON
  (`{profile, ready, installed, trusted, trustSignal, stale, bridgeReadable,
  version, reason}`), exit 0 ready / 78 not ready. `trustSignal` is
  `"baseline-diff"` (the strengthened signal, issue #5005) or `"legacy-coarse"`
  (the old any-`trusted_hash`-exists fallback, used only for a profile whose
  receipt predates baseline tracking) or `"none"` (nothing trusted at all).
- **`--all-profiles`** applies any subcommand to **every** pooled profile under
  `LOOM_CODEX_PROFILE_ROOT` (default `~/.loom/codex-profiles` — the same root
  `spawn-codex.sh` resolves `LOOM_CODEX_PROFILE` against and `loom-daemon
  accounts add codex` populates), so "the managed hook is installed in every
  selected pooled `CODEX_HOME`" is one command, not a hand-written loop. It
  re-invokes the single-profile path once per profile (one implementation, not
  two), emits JSONL under `--json`, and returns the **worst** per-profile exit —
  a single untrusted profile fails the whole pool-wide `verify`.

**Hook trust.** Codex persists trust as `hooks.state."<id>".trusted_hash` in
`$CODEX_HOME/config.toml`, established interactively or waived with
`--dangerously-bypass-hook-trust` (equivalently the `bypass_hook_trust` config
key — Loom passes **neither**).

**Decision (issue #5005): no non-interactive trust mechanism exists on 0.146.0,
so the accepted readiness path is the operator-attested one-time step below,
gated by a strengthened `verify`.** This was independently re-verified against
the real `@openai/codex@0.146.0` npm package (2026-08-03, superseding the
2026-07-31 note that first recorded the gap):

- `codex --help` / `codex exec --help` list no `hooks` subcommand — only the
  `--dangerously-bypass-hook-trust` waiver Loom refuses to pass.
- `codex doctor --json`, run against a fresh temporary profile, emits **no
  hook check at all** among its 18 checks (`app_server.status`,
  `auth.credentials`, `config.load`, `git.environment`, `installation`,
  `mcp.config`, `network.*`, `runtime.*`, `sandbox.helpers`, `state.*`,
  `system.environment`, `terminal.*`, `updates.status`).
- `codex features list` shows `hooks` itself as a stable, enabled feature, but
  no companion trust-management feature or flag.
- The `codex app-server` JSON-RPC surface (extracted from the shipped binary's
  string table) exposes exactly one hook-related method, `hooks/list`
  (read-only), plus `hook/started` / `hook/completed` notifications — **no**
  `hooks/trust` or equivalent write method.
- A `strings` pass over the shipped binary confirms hook trust is a **TUI-only
  internal action**: `TrustHook { key, current_hash }` and a
  `HookTrustUpdate { current_hash }` app-server notification exist only in the
  interactive keybinding/action-dispatch tables, reachable from a keypress in
  the TUI's hook-trust prompt (`config/batchWrite failed while updating hook
  trust in TUI` is the exact log string), never from a CLI flag, config key, or
  RPC method a script could drive headlessly.

Loom will not guess the identity string (`hooks.state.<id>`) or the hash
algorithm behind `trusted_hash` — reverse-engineering an undocumented internal
format is exactly the kind of "prove it's safe by assumption" shortcut this
whole preflight exists to avoid — and it will not pass the bypass flag. So
`verify` fails closed instead, checking three things:

1. **Structure** — Loom's entry is present at the expected version and names a
   readable bridge belonging to this workspace.
2. **Codex trust, strengthened (issue #5005)** — a NEW `hooks.state`
   `trusted_hash` value appeared in `config.toml` **after** Loom's
   currently-installed entry was (re)provisioned. `provision-codex-hooks.sh
   install` snapshots the trust state that already existed at install time
   into the receipt (`trustBaselineHashes`); `verify` diffs the CURRENT trust
   state against that baseline. This still cannot prove Codex trusted LOOM'S
   SPECIFIC entry — Codex exposes no identity string Loom can observe, per the
   evidence above — but it is a materially stronger correlation than "some
   hook, at some point, was trusted": it requires a trust decision to have
   happened **after** this content was installed, not merely to exist
   somewhere in the file, possibly from years ago or for something else
   entirely. A profile whose receipt predates this tracking (an older Loom
   install) falls back to the previous coarse signal — any `trusted_hash`
   present — so a Loom upgrade never un-trusts an already-ready profile. An
   idempotent reinstall of unchanged content preserves the recorded baseline
   rather than resetting it to "whatever is trusted now" (which would
   silently erase credit for an earlier trust decision); a reinstall that
   actually changes the managed entry's content resets the baseline, correctly
   requiring a fresh trust decision for the new content. That imprecision —
   correlating timing, not identity — is one of the reasons the capability
   manifest stays `partial` (gap 11).
3. **Staleness** — the Loom-owned, non-secret receipt
   `$CODEX_HOME/loom-codex-hooks.json` pins the SHA-256 of the managed entry as
   installed. If the entry changed since, readiness is STALE and mutable roles
   fail closed.

### Procedure: bringing a fresh Codex profile to `verify --json` ready

Reproducible end-to-end, install pool-wide once, then trust **once per
profile** (the trust prompt is interactive by necessity, gap 11 — for N pooled
profiles this is N manual acceptances, not a one-time global step; budget for
it explicitly when sizing a multi-account Codex pool):

```bash
# 1. install the managed hook into every pooled profile (idempotent, credential-free)
.loom/scripts/provision-codex-hooks.sh install --all-profiles --workspace "$PWD"

# 2. accept Codex's hook-trust prompt once per profile (interactive — see above)
CODEX_HOME=~/.loom/codex-profiles/alice codex

# 3. gate the pool: exit 0 only when EVERY profile is ready
.loom/scripts/provision-codex-hooks.sh verify --all-profiles --workspace "$PWD" --json
```

Step 3 succeeding (`ready: true` for every profile) is what unblocks
`LOOM_ROLE=builder .loom/scripts/spawn-codex.sh` against that profile — without
it, the mutable-role preflight in `spawn-codex.sh` (`defaults/scripts/spawn-codex.sh:635-693`)
exits 78 before the CLI starts. Test coverage for the readiness signal itself:
`defaults/scripts/tests/test-provision-codex-hooks.sh` (trust-baseline
scenarios); coverage that no code path ever waives trust:
`defaults/scripts/tests/test-provision-codex-hooks.sh`'s credential-hygiene
section, which greps both `spawn-codex.sh` and `provision-codex-hooks.sh` for
`--dangerously-bypass-hook-trust` and the `bypass_hook_trust` config-key
equivalent.

Repeat step 1 after any `loom update` that changes the bridge — a changed
managed entry reads as STALE and every mutable-role spawn fails closed until it
is reinstalled and re-trusted.

### Role-aware spawn preflight

`spawn-codex.sh` emits one audit line per spawn:

```text
spawn-codex: hooks=<ready|not-ready|unavailable> role=<name> mutable=<bool> trust-bypass=never reason="…"
```

- **Mutable roles (`builder`, `doctor`, and their aliases)** exit **78 before the
  CLI starts** unless `hooks=ready`. Missing, stale, wrong-version, untrusted,
  unreadable, or ambient-profile states all fail closed.
- **Read-only roles** keep the conservative sandbox fallback but are told
  explicitly that hook parity was unavailable, and are never reported as
  Builder-capable.
- The audit line carries the profile *name* and the verdict only — no profile
  path contents, no credential bytes.

| Loom guard (`defaults/hooks/`) | Claude matcher | Intent | Codex coverage | How / why |
|---|---|---|---|---|
| `guard-destructive.sh` → `guard-destructive-generic.sh` | `Bash` | Deny catastrophic Bash (`rm -rf /`, force-push to `main`, `gh repo delete`, fork bombs, `curl … \| sh`, cloud/SQL destruction); ask on borderline ops; scope `rm` to the repo; Bash-tool write-confinement (`>`, `tee`, `sed -i`, `cp`/`mv`, #4178) | **partial** &rarr; **enforced with the managed hook** | With the #4495 bridge provisioned+trusted, the full command-pattern policy runs for every classified Codex shell tool, so this row becomes real parity. Without it, only the sandbox applies: `read-only` blocks every write, so under the adapter's default the destructive-write half is fully covered — more strictly than the guard itself. `workspace-write` blocks writes and `rm` outside the workspace root, and (with network off) blocks `curl \| sh` and remote cloud destruction by making the network unreachable. **Not covered:** command-pattern semantics. Codex cannot recognize `DROP DATABASE`, `DELETE` without `WHERE`, `git push --force` to `main`, or a fork bomb *as such* — anything reachable without leaving the workspace or the network proceeds. With `LOOM_CODEX_NETWORK=1` (which a Builder needs to push) the network-derived coverage evaporates and a force-push to `main` becomes reachable with nothing to stop it. |
| `guard-worktree-paths.sh` | `Edit\|Write` | Confine Edit/Write to the builder's own `issue-N` worktree; deny escapes into the main checkout (#2441, #4007) | **partial** &rarr; **enforced with the managed hook** | With the #4495 bridge, `guard-worktree-paths.sh` runs for every Codex native patch/write target AND (via `guard-destructive-generic.sh`) for shell-derived write targets, giving per-worktree confinement. Without it, only the sandbox applies: `workspace-write` confines writes to the **workspace root** — a strictly coarser boundary. It blocks escaping the repo, but **not** the per-worktree boundary: a Codex builder can write into a sibling `issue-M` worktree, or into the main checkout, because all of those live under the same root. This is the exact class of escape #4178 documented. Mitigation: one Codex worker per workspace root, or narrow `writable_roots` by hand. |
| `guard-loom-workflow.sh` | `Bash` | `gh pr merge` → `merge-pr.sh` redirect; `pip install -e` worktree block (#2495 + #4079 — still live after #4557 retired Loom's own Python package: it protects orchestrated *Python* repos and stops new frozen `~/.local/bin` console scripts shadowing the `loom-daemon` binary); `loom-daemon workspace` registry-mutation ask (#4326) | **none** &rarr; **enforced with the managed hook** | With the #4495 bridge these are real denies (and the `loom-daemon workspace` ASK becomes a deny, since Codex cannot express `ask` headless). Without it they are pure command-pattern convention with no OS analogue, learned only from `AGENTS.md` / role prompts — advisory, never enforced. |
| `guard-background-subagents.sh` | `Stop` | Block one stop when the transcript shows dispatched-but-unresolved `Task` subagents, so ending a headless turn does not kill live background work (#4257) | **none** | Depends on Claude Code's `Stop` event and transcript shape. Codex has `session_end` and `subagent_stop` events that could host an equivalent, but nothing is wired (gap 1). Note the underlying hazard is *also* absent today: Loom does not dispatch Codex subagents at all. |
| `guard-readonly-dirs.sh.template` | `Edit\|Write` | Optional per-project read-only path protection | **partial** | Expressible as narrowed `writable_roots` under `workspace-write`, but the adapter does not generate it — an operator must configure it manually. |
| `skill-router.sh` | `UserPromptSubmit` | Inject an agent routing table / `AGENT_ROUTE` suggestion per prompt (opt-in) | **none** | Context injection, not a boundary. Codex has a `user_prompt_submit` hook event that could host it; unwired (gap 1). Static equivalent: `AGENTS.md`. |
| `methodology-inject.sh` | *(present, not wired here)* | Inject universal/role/topic context from `.loom/context/` | **none** | Same as above: not a boundary, and a `user_prompt_submit` equivalent exists but is unwired. |
| `post-worktree.sh` | *(invoked by `worktree.sh`)* | Copy the `loom-daemon` binary into a new worktree | **covered** | Runtime-neutral — `worktree.sh` runs it regardless of which runtime drives the work. Not a Claude hook. |

## Sandbox-mode mapping (what the adapter emits)

Precedence, highest first:

| # | Signal | Effective sandbox |
|---|---|---|
| 1 | An explicit `-s` / `--sandbox` (or `--dangerously-bypass-approvals-and-sandbox`) in the passthrough args | as given |
| 2 | `LOOM_CODEX_SANDBOX=read-only\|workspace-write\|danger-full-access` | as given (invalid value → exit 78) |
| 3 | Loom's runner-neutral `--dangerously-skip-permissions` convention | `workspace-write` |
| 4 | *(default)* | `read-only` |

### Why the default is `read-only`, and why skip-permissions is **not** full access

The fork maps Loom's skip-permissions convention to
`--dangerously-bypass-approvals-and-sandbox` — no sandbox at all — on the
argument that Loom's Claude workers already run unattended with full tool
access, so Codex should match: *"parity, not a new exposure."*

**Upstream declines that mapping**, for one reason: the premise is not parity.
Claude's unattended posture is backstopped by `PreToolUse` guards that fire on
every Bash/Edit/Write call *even under* `--dangerously-skip-permissions`. Those
guards do not exist for a Codex worker. Handing Codex the same flag therefore
produces a **strictly weaker** trust boundary than the Claude path it is
imitating — an agent with Claude's authority and none of Claude's backstops.

`workspace-write` is the closest honest analogue of what the Claude guards
actually enforce (`guard-worktree-paths.sh`'s write confinement, plus
`guard-destructive-generic.sh`'s out-of-repo `rm`/write scoping). It is
deliberately imperfect — see gap 2 — but it is a real boundary rather than an
assumed one.

`read-only` is the *default* because a tier-2 runtime with no wired guards
should not be able to write anything unless someone said so. It is also the
right mode for the read-only Loom roles (Judge, Curator, Guide, Champion
evaluation), which is where a Codex canary should start.

Operators who want the fork's posture opt in explicitly:

```bash
LOOM_CODEX_SANDBOX=danger-full-access .loom/scripts/spawn-codex.sh -p "…"
```

The adapter emits `-s danger-full-access` rather than
`--dangerously-bypass-approvals-and-sandbox` for that case: same sandbox
posture, without additionally waiving Codex's hook-trust prompt (which is a
separate protection, and one Loom will want intact once gap 1 is closed).

### The network coupling (read this before dispatching a Builder)

`workspace-write` blocks outbound network by default. A Loom **Builder** must
`git push` and call `gh` — so a Builder-equivalent Codex worker needs
`LOOM_CODEX_NETWORK=1`, which sets
`-c sandbox_workspace_write.network_access=true`.

That single flag removes most of what the sandbox was contributing to the
`guard-destructive.sh` row above: with the network reachable, force-push to
`main`, `gh repo delete`, cloud-CLI destruction, and `curl … | sh` all become
possible again, and Codex has no pattern matcher to stop any of them. **A
networked `workspace-write` Codex Builder is meaningfully less protected than a
Claude Builder.** Keep Codex on read-only roles until gap 1 is closed.

### Trusted-directory check

`codex exec` refuses to start outside a git work tree ("Not inside a trusted
directory and `--skip-git-repo-check` was not specified.", exit 1). That is a
real guardrail, so the adapter injects `--skip-git-repo-check` **only** when
`git rev-parse --is-inside-work-tree` says the cwd is not inside one — never
unconditionally. Worktree dispatch (`.loom/worktrees/issue-N`) is inside a work
tree and keeps the check enabled. Scratch-dir dispatch gets the waiver plus a
warning line. The refusal itself classifies as `FATAL` (not `RECOVERABLE`), so a
mis-set cwd fails fast instead of retrying forever.

## Residual gaps

Known, documented, and accepted for tier-2. None is silent.

1. ~~**Loom's guard hooks do not run at all.**~~ **CLOSED for `pre_tool_use`
   (issue #4495)**, conditional on provisioning + trust: the managed bridge
   dispatches the three `PreToolUse` guards for every classified Codex tool
   call, and mutable roles refuse to start without it. What remains open:
   **every other hook event is still unwired** — `user_prompt_submit`,
   `post_tool_use`, `session_end`, `subagent_stop`, `pre_compact`. So
   `skill-router` / `methodology-inject` (gap 6) and the `Stop`-event
   background-subagent guard have no Codex equivalent. Also, a session running
   **without** a provisioned/trusted managed hook still has zero guard coverage;
   that state is now loud (`hooks=unavailable` in the audit line) and fatal for
   Builder/Doctor rather than silent.
2. **Per-worktree write isolation is enforced by the hook, not the sandbox.**
   `workspace-write` still confines only to the workspace root, so a sibling
   `issue-M` worktree and the main checkout remain inside the *kernel* boundary.
   The managed hook is what denies those writes, which means the isolation is
   only as strong as hook provisioning + trust. The sandbox-level alternative
   (narrowing `sandbox_workspace_write.writable_roots` to the dispatched
   worktree) is **not implemented** — it was evaluated as defense-in-depth and
   deferred; the flag/TOML-key syntax still needs verification against a live
   CLI.

   **Sibling worktrees.** Whether a write into a *sibling* `issue-M` worktree is
   denied is decided by the same two-mechanism split the Claude path has, not by
   anything Codex-specific:

   - With `LOOM_WORKTREE_PATH` set (a session pinned to one worktree — tmux,
     manual, or any dispatcher that exports it), the fast path confines writes
     to that worktree and **denies siblings**. The Codex hook process inherits
     the variable, so this holds for Codex through the bridge exactly as it does
     for Claude directly; the parity suite asserts both.
   - Without it, the path-derived fallback cannot tell which managed worktree
     the acting session owns (#4245), so a sibling write is **allowed** — again
     for both runtimes identically.

   Making Codex stricter here would fork the policy table, which this phase
   explicitly does not do. Closing the fallback case is a shared-policy change
   for both runtimes, and is out of scope for #4495.

   **~~A model-chosen `workdir` could defeat this entirely.~~ CLOSED (issue
   #4767).** See "`workdir` anchor validation" above — the bridge now requires
   `workdir` to resolve into the same repo as the acting session before
   trusting it, instead of handing the sub-guards a rootless `cwd`.
3. **Command-pattern blocking now comes from Loom, not Codex.** With the managed
   hook in place, `DROP DATABASE`, `DELETE` without `WHERE`, `git push --force
   origin main`, fork bombs, and `curl … | sh` are matched by
   `guard-destructive-generic.sh` for a Codex worker. Codex itself still
   recognizes none of them, so this coverage disappears entirely if the hook is
   not provisioned/trusted.
4. **Loom's workflow policy is now enforced, when the hook runs.** The
   `gh pr merge` → `merge-pr.sh` redirect and the `pip install -e` worktree block
   are real denies for a Codex worker; the `loom-daemon workspace` ASK becomes a
   **deny** (Codex cannot express `ask`, and headless has nobody to ask), which
   is stricter than Claude, not weaker. Without a ready hook they revert to
   advisory-only.
5. **Label-mutation commands are ungated for every runtime.** Nothing in Loom —
   Claude or Codex — gates `gh issue edit --add-label` / `--remove-label`. A
   worker can move an issue anywhere in the state machine. This is *not* a
   Codex-specific regression, but it is worth stating in a trust-boundary
   document: Codex inherits it with no compensating guard, so a Codex worker
   that mishandles labels leaves no enforcement layer between it and the
   coordination state.
6. **No per-prompt context injection.** `skill-router` / `methodology-inject`
   have no wired Codex equivalent (a `user_prompt_submit` event exists —
   gap 1). Not a safety boundary; the static substitute is `AGENTS.md`.
7. **Approvals gate nothing.** `codex exec` is non-interactive and exposes no
   approval flag. Any parity argument resting on `approval_policy` is void for
   Loom dispatch. The sandbox is the only enforced guard.
8. **Cost/usage fidelity is aggregate, not per-turn.** The adapter reports the
   `tokens used` total and resolves the session JSONL path
   (`$CODEX_HOME/sessions/<Y>/<M>/<D>/rollout-<ts>-<session-id>.jsonl`), but
   nothing parses that transcript into per-message usage the way the Claude
   archiver does. Not a safety gap; a contract-point-4 fidelity gap.
9. **Native Codex agents are not a supported backend.** Codex exposes
   in-session collaboration primitives (`spawn_agent`, `wait_agent`,
   `interrupt_agent`, …). Per the fork's finding (fork PR #59), these are
   **prohibited** for Loom lifecycle dispatch: they are not a Loom
   orchestration backend, they bypass the label state machine and the worktree
   model entirely, and a supervisor holding them has been observed to kill live
   children and take over their work. This is enforced by documentation only —
   Codex has no policy hook that can block a session from calling them. Loom
   dispatch is one process per role via `spawn-worker.sh`, never native agents.

10. **MCP tool calls are passed through, on purpose.** The bridge does not
    inspect `mcp__*` tools, because Claude's own guards do not either (their
    matchers are `Bash` and `Edit|Write`). This is parity, not coverage: an MCP
    server that writes files is unguarded on **both** runtimes.
11. **Hook trust readiness is DECIDED (issue #5005), still imprecise by
    construction.** Codex 0.146.0 offers no documented non-interactive way to
    establish hook trust (re-verified against the real npm package, see
    "Provisioning and trust" above), and no observable way to prove that
    *Loom's* entry specifically is the trusted one — Codex exposes no identity
    string. The accepted path is therefore the operator-attested one-time
    trust step, gated by `verify`'s trust-baseline diff: readiness requires a
    NEW `hooks.state` `trusted_hash` to have appeared **after** Loom's entry
    was installed, not merely for one to exist anywhere in `config.toml`. This
    strengthens the correlation between "Loom's content" and "a trust
    decision" without being able to prove identity — which remains the reason
    the capability manifest stays `partial`. Loom never passes
    `--dangerously-bypass-hook-trust`, enforced by a test
    (`test-provision-codex-hooks.sh`'s credential-hygiene section).
12. **`write_stdin` is denied, not confined.** Writing bytes into an
    already-running PTY session is a mutation channel Loom cannot inspect, so it
    is refused outright. A Codex worker that needs interactive input must run a
    fresh, inspectable shell call instead.
13. **The `hooks.json` matcher wildcard is unverified against a live CLI.** Loom
    emits the Claude-compatible `"*"` (Codex's hook config format is a port of
    Claude's, down to reading `CLAUDE_PLUGIN_ROOT`), but the 0.146.0 binary does
    not document whether the matcher is a regex or a glob. If it is a regex,
    `"*"` may not compile. Confirming this is a listed item in
    `defaults/runtimes/codex.json`'s `capabilityGate.pending`, and it is one of
    the reasons promotion waits on the real-CLI canary. `--matcher` /
    `LOOM_CODEX_HOOK_MATCHER` let an operator override it without a code change.
14. **A `SIGKILL`ed hook is silence, and silence is allow.** The bridge's `EXIT`
    trap converts a crash or a `SIGTERM` into a deny, but a hook killed outright
    after Codex's timeout grace period cannot emit anything, and this wire has no
    way to express "allow" other than emitting nothing. Loom cannot close this
    from inside the hook; the mitigations are the generous per-entry `timeout`
    (default 30s, `--timeout` / `LOOM_CODEX_HOOK_TIMEOUT`) and the fact that the
    sandbox is still underneath. Named reason the manifest stays `partial`.
15. **No real-CLI canary evidence yet.** Every parity claim above is proven by
    fixture-driven tests against the pinned 0.146.0 schemas, not by a live
    `codex exec` run. Gap 11 now has an accepted readiness path (issue #5005),
    which makes an untrusted-hook false-negative no longer the reason a canary
    would prove nothing — but the canary still requires a profile that has
    been through the one-time interactive trust step (gap 11 is decided, not
    eliminated: the step is still manual), and the live-CLI evidence itself
    (allow/deny filesystem+git proof, matcher semantics, `write_stdin`, the
    `SIGKILL` silence-means-allow gap) remains separate, unclaimed scope. See
    "Promotion gate" below.

## Admission checklist (contract point 5/6)

- [x] Guard-intent → mechanism map (above)
- [x] Explicit residual-gap section (above)
- [x] Sandbox-mode mapping with stated precedence and rationale
- [x] Fork's native-agent prohibition recorded (gap 9)
- [x] Error-classification table for the runtime
      (`defaults/scripts/lib/classify-error.sh`, `codex` provider)
- [x] Mocked CI smoke leg (`defaults/scripts/tests/test-spawn-codex.sh`)
- [x] Loom guard intent mechanically enforced under Codex for `pre_tool_use`
      (issue #4495) — bridge + provisioning + fail-closed spawn preflight, with
      CI suites `test-guard-codex-bridge.sh` and `test-provision-codex-hooks.sh`
- [ ] Real-CLI canary evidence recorded — **open** (gap 15); required before
      capability promotion. No longer blocked on gap 11 having *no* answer
      (issue #5005 decided the readiness path), but still needs a profile
      taken through that one-time interactive step before a canary can run.

## Promotion gate (`hooks` / `worktreeIsolation`)

`defaults/runtimes/codex.json` keeps `hooks: partial` and
`worktreeIsolation: partial`, so `check-runtime-capabilities.sh` (and the
daemon's `runtime_admission`) continue to reject **Builder + Codex** and
**Doctor + Codex** with exit 78. `defaults/roles/doctor.json` now declares the
same `runtimeRequirements` as Builder (`worktreeIsolation`, `mcp`), so Doctor
fails closed for the same reason instead of slipping through unconstrained.

The implementation and its test coverage have landed; promotion has not,
because the evidence gate is not satisfied. `codex.json`'s `capabilityGate`
block carries the machine-readable list; in prose the outstanding items are:

1. **Real-CLI canary** in a disposable repository: one ALLOWED mutation inside a
   managed worktree, plus DENIALS for a main-checkout file mutation and a
   protected-branch destructive shell command, with before/after filesystem and
   `git` evidence proving no denied side effect occurred.
2. **Matcher semantics** confirmed on the live CLI (gap 13).
3. **Hook trust** — **DECIDED 2026-08-03 (issue #5005)**: operator-attested
   one-time trust step per profile, gated by `verify`'s trust-baseline diff
   (see "Provisioning and trust" above). Item 1 was previously blocked on this
   having *no* answer at all — an untrusted hook does not run, so a canary
   would have proven nothing. It is no longer blocked for that reason, but
   still requires a profile that has actually been through the one-time
   interactive step; #5005 defines and hardens the readiness path, it does not
   itself run the canary (that remains item 1/#4496's job, and is unclaimed).

**#4478 (operator sandbox posture) — DECIDED 2026-07-31.** Path 1-then-2: the
interim posture is a **read-only default** with **Builder-role-only** escalation
to `workspace-write` + `LOOM_CODEX_NETWORK=1`, scoped to one worker pool. This
issue's implementation is the durable path-2 half. **No adapter change was
needed to honor it** — `spawn-codex.sh` already defaults to `read-only` and
already requires an explicit `LOOM_CODEX_SANDBOX=workspace-write` +
`LOOM_CODEX_NETWORK=1` to escalate (see "Sandbox-mode mapping" above). Recording the
decision here is deliberately *not* the same as promoting capability: promotion
still waits on items 1–3, which are technical evidence, not posture.

Builder-role-only scope means `suggestedWorkerType: "codex"` belongs only on
`defaults/roles/builder.json` — the hints that previously sat on
`judge.json`/`curator.json` (read-and-comment roles) predated this decision and
have been corrected to `"claude"` accordingly. `suggestedWorkerType` is a
dispatch *preference* hint only (see `runtime-adapters.md` § "Daemon runtime
binding and admission"); actual admission still runs through
`runtimeRequirements` + this document's promotion gate above, so the hint
correction changes no enforced behavior on its own.

When items 1–3 are satisfied, promote **only** the capabilities the evidence
proves, append the evidence links to this document, and leave `mcp`,
`subagents`, and `skills` untouched. `subagents` stays `no` regardless — native
Codex agents remain prohibited (gap 9).

## CODEX_HOME profile layout, refresh, and security posture

The adapter selects a Codex account by pointing `CODEX_HOME` at a profile
directory. This section is the auth-surface documentation absorbed from the
companion provisioning issue #4469 so it has exactly one owner.

### Layout

```text
~/.loom/codex-profiles/            # profile root (LOOM_CODEX_PROFILE_ROOT)
└── <account>/                     # one CODEX_HOME per account, mode 0700
    ├── auth.json                  # OAuth/refresh-token bundle, mode 0600
    ├── sessions/<Y>/<M>/<D>/…      # per-session rollout JSONL transcripts
    └── …                          # Codex's own state (caches, logs, skills)
```

Provision profiles through the secret-safe lifecycle CLI:

```bash
loom-daemon accounts add codex alice --device-auth
loom-daemon accounts import codex bob --auth-file ~/.codex/auth.json
loom-daemon accounts status codex alice --json
loom-daemon accounts disable codex alice
loom-daemon accounts reauth codex alice --device-auth
loom-daemon accounts remove codex alice       # recoverable quarantine
loom-daemon accounts remove codex alice --purge
```

`add` and `reauth` inherit the terminal for browser/device login. Import accepts
only an explicit non-empty regular file, installs it atomically with mode
`0600`, and never parses or prints it. Codex also supports `codex login
--with-api-key` and `--with-access-token`; pipe those secrets on stdin while
targeting the profile's `CODEX_HOME`—never put a secret in argv or registry
metadata.

Every lifecycle command is all-or-nothing over the (profile, registry) pair. A
failed `add` or `import` removes the profile it created, so the name stays
reusable; concurrent creations of the same name are serialized by an exclusive
profile-directory claim, so a losing call can never delete the winner's
credential. A failed `remove` restores the live profile's registry entry
**and** removes any `recovery.json` that invocation staged, so a later
recoverable removal is never blocked by residue from a failed one. `remove`
commits the registry update *before* moving the profile: if the process dies
mid-command, the residue is an inert orphan directory (only that name is
blocked until an operator clears it), never a registry entry pointing at a
vanished directory that would poison `accounts list` for every account.
`--purge` destroys credential bytes only after the registry commit succeeds.

Manually provisioned profiles (directories created under the profile root
before `.loom/accounts.json` exists) appear in `accounts list` as discovered
accounts. The first `disable`, `enable`, or `remove` adopts the whole
discovered set into the registry, so mutating one discovered account never
hides the others and lifecycle commands work on exactly the accounts `list`
shows.

Select a profile at spawn time by any of:

| Precedence | Env var | Meaning |
|---|---|---|
| 1 | `LOOM_CODEX_HOME` | Absolute profile directory |
| 2 | `CODEX_HOME` | Honored verbatim if pre-set |
| 3 | `LOOM_CODEX_PROFILE` | Bare account name under `LOOM_CODEX_PROFILE_ROOT` (default `~/.loom/codex-profiles`) |
| 4 | *(none)* | Codex's ambient `~/.codex` login state |

### Refresh

`auth.json` holds a refresh-token bundle; Codex refreshes the access token
itself and rewrites `auth.json` in place. Consequences:

- The profile directory must be **writable** by the spawned worker, not just
  readable.
- A profile is an **authoritative copy, not a cache**. `~/.codex/auth.json` and
  a `~/.loom/codex-profiles/<account>/auth.json` copied from it diverge the
  moment either refreshes. Copying a live profile produces two bundles racing
  to rotate the same credential; re-run `codex login` per profile instead.
- Refresh failure is a real runtime error mode, and it is what the `codex`
  classifier table's `TOKEN_EXPIRED` patterns are matching ("refresh token has
  expired", "Failed to refresh token", "Not signed in. Please run 'codex
  login'", `401 Unauthorized`).

### Security posture

- Profile dirs `0700`, `auth.json` `0600`. A profile is a live credential.
- The adapter **assigns** the directory to `CODEX_HOME`; lifecycle import makes
  the one intentional copy into the machine profile root. Repository `.loom/`,
  token pools, daemon JSON, logs, and registry metadata never hold credential
  contents.
- Logging discipline: the adapter logs the profile **directory name** only
  (`spawn-codex: using Codex profile 'alice'`). Never the path's contents, never
  a byte of `auth.json`. Preserve this in any change to the adapter.
- An **explicitly requested** profile with no usable `auth.json` exits **78**
  (`EX_CONFIG`) rather than silently degrading to a different account — a silent
  fallback would attribute work and cost to the wrong account. Ambient auth
  (tier 4) is not a request and never fails here; Codex reports its own auth
  error, which classifies as `TOKEN_EXPIRED`.
- Provider-aware inventory and lifecycle management are available, but there is
  no health ranking, cooldown, automatic rotation, or failure feedback yet.
  Those policies remain epic #4167 Phase 4 issue #4493.
- The registry currently exposes no active-use lease/interlock. Operators must
  not remove a profile while a worker is using it; lifecycle removal cannot yet
  prove that condition and #4493 remains the integration point for runtime
  feedback.

## References

- [`runtime-adapters.md`](runtime-adapters.md) — the seven-point contract and tier policy
- [ADR-0012](https://github.com/rjwalters/loom/blob/main/docs/adr/0012-runtime-adapter-contract.md) — runtime adapter contract
- [`guard-hooks.md`](guard-hooks.md) — the Loom guard catalog this maps against
- `defaults/scripts/spawn-codex.sh` — the adapter
- `defaults/scripts/lib/classify-error.sh` — the `codex` provider table
- Codex config reference: <https://developers.openai.com/codex/config-reference>
- Codex sandboxing concepts: <https://developers.openai.com/codex/concepts/sandboxing>
- Fork: <https://github.com/gpeyton/loom> — `defaults/.codex/GUARDRAIL-PARITY.md`
- Epic #4167 · Phase 2 issue #4468 · companion auth issue #4469 · canary #4470
