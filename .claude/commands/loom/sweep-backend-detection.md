# Sweep — Run identity, peer detection, and Stage -1 backend detection

> **Reference file for [`sweep.md`](sweep.md)**, the `/loom:sweep` dispatcher.
>
> **Load when:** **always, at sweep start** — Step 0a (run id), Step 0b (peer detection) and Stage -1 (daemon vs. subagent backend, auto wave size) run before every other stage in every mode.
>
> **Flat, one level deep.** `sweep.md` names every file a given run needs up
> front; nothing here requires opening a *third* file to follow its own
> procedure. Cross-references worded "above"/"below" that do not resolve inside
> this file point at a sibling `sweep-*.md` section — see the reference-file map
> in [`sweep.md`](sweep.md). The body below is **verbatim** from the pre-split
> `sweep.md` (#7726): no rule, warning, edge case, table, or code block was
> reworded, softened, reordered, or dropped.

## Contents

- [Sweep Run Identity + Peer-`/loom:sweep` Detection (#3768)](#sweep-run-identity--peer-loomsweep-detection-3768)
  - [Step 0a: Generate the stable run id (once, at sweep start)](#step-0a-generate-the-stable-run-id-once-at-sweep-start)
  - [Step 0b: Peer-`/loom:sweep` detection (loud, NON-BLOCKING)](#step-0b-peer-loomsweep-detection-loud-non-blocking)
- [Stage -1: Backend detection (Phase D of #3449)](#stage--1-backend-detection-phase-d-of-3449)
  - [Decision tree (the contract)](#decision-tree-the-contract)
  - [The three probes](#the-three-probes)
  - [Resolve auto wave size (when `BUILDERS_PER_WAVE = auto`)](#resolve-auto-wave-size-when-builders_per_wave--auto)
  - [The daemon-dispatch path (when `DECIDE = use_daemon`)](#the-daemon-dispatch-path-when-decide--use_daemon)
  - [The subagent fallthrough (when `DECIDE = use_subagent`)](#the-subagent-fallthrough-when-decide--use_subagent)
  - [Smoke tests (documented expectations)](#smoke-tests-documented-expectations)
  - [What Stage -1 does NOT do](#what-stage--1-does-not-do)

---

## Sweep Run Identity + Peer-`/loom:sweep` Detection (#3768)

Before **any** other stage — including Backend detection (Stage -1), the dry-run gate, and all wave lifecycles — establish a **stable identity for this sweep invocation** and probe for a concurrently-running peer `/loom:sweep`. This runs for **all modes (A, B, and C)** — it is *not* short-circuited by Mode C or `--no-daemon` (those only affect the Stage -1 backend probes below).

This section exists because `/loom:sweep` was originally hardened (#3373 checkpoints, #3648 baseline) assuming a single sweep instance per repo. Two concurrent `/loom:sweep` runs in the same repo (observed live 2026-07-22) collided on shared run-state: they shared the single fixed main-clean baseline path (one clobbered the other's pre-sweep snapshot), and their checkpoints were indistinguishable because `task_id` was `sweep-$$` — the PID of each Bash *subshell*, which varies *within* a single sweep across tool calls, not a stable per-invocation id.

### Step 0a: Generate the stable run id (once, at sweep start)

Run this **exactly once**, before anything else:

```bash
RUN_ID=$(./.loom/scripts/sweep-run-registry.sh new --pid "$PPID")
echo "sweep run id: $RUN_ID"
```

`sweep-run-registry.sh new` generates a portable (macOS/Linux, no `uuidgen`) run id combining a UTC timestamp + PID + random suffix (e.g. `sweep-20260722T231500Z-84213-a3f9c1`), and registers it under `.loom/sweep-run/<RUN_ID>.json` (gitignored) with a liveness PID for peer detection.

**`--pid "$PPID"` is load-bearing, not decoration (#4691).** `$PPID` expanded *here* — in the tool-call shell — is the long-lived orchestrator (`claude -p /loom:sweep …`) that spans the whole sweep. The tool-call shell itself is a fresh one-shot `<shell> -c …` process that is reaped the moment this Bash block returns, so recording *it* would mark this run dead within seconds of registration; the very next peer scan would then prune this run's registry entry and delete its `main-clean-baseline-<RUN_ID>.txt` mid-sweep (and, because the entry vanishes before the baseline is written, orphan that baseline forever). Passing `$PPID` explicitly also makes the recovery lookup below — which matches on `$PPID` — actually find this entry. `sweep-run-registry.sh` resolves the same orchestrator PID itself when `--pid` is omitted, so an older installed copy of this skill still behaves correctly.

**Treat the printed `RUN_ID` as a fixed literal for the entire rest of this sweep.** Thread it — as that literal string — into every `--task-id "$RUN_ID"` checkpoint write and into the main-clean baseline path below. Do **NOT** regenerate it per Bash tool call, and do **NOT** fall back to `sweep-$$` (that is the exact bug this fixes: `$$` is a fresh subshell PID on every tool call). If you ever lose track of the literal mid-sweep, recover it from the registry rather than minting a new one:

```bash
RUN_ID=$(./.loom/scripts/sweep-run-registry.sh list | awk -v p="$PPID" '$2==p {print $1; exit}')
```

At sweep completion (or abort), remove this run's registry entry. On the subagent path this is after the wave lifecycle settles and just before the Summary Output; **on the daemon path, "completion" means immediately after the last `mcp__loom__dispatch_sweep` call returns** (see "The daemon-dispatch path" below) — dispatch-and-exit is that path's entire job, so there is no later in-session point to defer this to:

```bash
./.loom/scripts/sweep-run-registry.sh cleanup "$RUN_ID"
```

`cleanup` removes **both** RUN_ID-keyed transients of this run: the registry entry `.loom/sweep-run/<RUN_ID>.json` and the main-clean baseline `.loom/sweep-checkpoint/main-clean-baseline-<RUN_ID>.txt` (#4450 — before that, baselines accumulated forever).

This is best-effort cleanup — a dead run's entry *and* baseline are also pruned automatically by any later sweep's peer scan (dead-PID liveness check), so a crash that skips cleanup never leaves a permanent false-positive. The bulk backstop for a run whose peer scan never happens is `loom-daemon clean`, which prunes baselines of non-live runs older than 48h plus checkpoints of closed issues.

Both pruners bias toward **keeping** a transient when liveness is ambiguous (#4691): a `kill(pid, 0)` that fails with `EPERM` means the process *exists* but is not signallable by the pruning caller, so only `ESRCH` ("no such process") authorizes deletion. A never-pruned baseline is a bounded, harmless leak; a baseline deleted under a live sweep silently disables the #3648 contamination-subtraction backstop for the rest of that run.

**Heartbeat refresh, at each wave boundary (#5896).** PID liveness alone cannot tell a genuinely live peer from a same-process zombie: a `/clear` inside this long-lived `claude -p /loom:sweep …` orchestrator does not end the OS process, so a same-process `/clear` + re-invoke leaves a registry entry whose PID stays alive forever even though nothing will ever drive its work again — this was observed live (a dead run's lane held 4 open PRs and 4 `loom:building` claims that stalled for hours before an operator manually confirmed the peer was defunct). The same "PID outlives the sweep" failure also happens under a *different* PID (#6595): a sweep interrupted inside an interactive session that stays open leaves an entry whose PID is alive for as long as that session is. The registry entry's `heartbeat` field starts equal to `timestamp` at registration and must be refreshed periodically so `peers` (Step 0b, below) can label **any** entry whose heartbeat has gone stale — same-PID or not — distinctly from one still actively driving a run. Refresh it at every wave boundary — the Wave Lifecycle's "advance to the next wave" point (step 8, after the post-wave integration gate settles), the nearest existing per-wave hook:

```bash
./.loom/scripts/sweep-run-registry.sh heartbeat "$RUN_ID"
```

This call is best-effort and non-fatal — if it fails (e.g. this run's own entry was already pruned by something else), do not stop the sweep over it; a missed refresh only means this run's own entry may itself be mislabeled stale (`stale-same-pid` or `stale-heartbeat`) by a peer's *next* scan, not that anything is corrupted. `SWEEP_RUN_HEARTBEAT_STALE_SECS` (default 900, 15 minutes) controls how long any entry may go without a refresh before `peers` calls it stale.

### Step 0b: Peer-`/loom:sweep` detection (loud, NON-BLOCKING)

Immediately after registering, probe for other **live** `/loom:sweep` runs in this repo and warn if any are found — never block, never auto-stop (mirroring the Daemon Coexistence contract):

```bash
PEERS=$(./.loom/scripts/sweep-run-registry.sh peers "$RUN_ID")
if [[ -n "$PEERS" ]]; then
  echo "$PEERS" | while read -r rid pid ts hb status; do
    case "$status" in
      stale-same-pid:*)
        age="${status#stale-same-pid:}"
        echo "⚠️  LIKELY-STALE SAME-PROCESS RUN (probably a cleared context, #5896):" >&2
        echo "       run $rid (pid $pid, started $ts, heartbeat stale ${age}) shares THIS" >&2
        echo "       orchestrator's own PID but has not refreshed its heartbeat — almost" >&2
        echo "       certainly a pre-/clear run whose conversation is gone, not a genuine" >&2
        echo "       concurrent sweep. Safe to investigate adopting its lane (verify its" >&2
        echo "       open PRs are frozen first) rather than deferring to it as a live peer." >&2
        ;;
      stale-heartbeat:*)
        age="${status#stale-heartbeat:}"
        echo "⚠️  LIKELY-STALE RUN (pid alive, but not sweeping, #6595):" >&2
        echo "       run $rid (pid $pid, started $ts, heartbeat stale ${age}) is registered" >&2
        echo "       under a still-running process that has not refreshed its heartbeat —" >&2
        echo "       most likely a sweep that was interrupted inside a session that stayed" >&2
        echo "       open, not a genuine concurrent sweep. Treat as situational awareness," >&2
        echo "       not as a peer to defer to (confirm before touching its lane)." >&2
        ;;
      *)
        echo "⚠️  ANOTHER /loom:sweep IS RUNNING IN THIS REPO:" >&2
        echo "       run $rid (pid $pid, started $ts)" >&2
        ;;
    esac
  done
  echo "   Two concurrent sweeps merge into a moving default branch unaware of" >&2
  echo "   each other. Per-issue loom:building claims still prevent double-builds," >&2
  echo "   and each sweep now keys its own main-clean baseline + checkpoints by its" >&2
  echo "   own RUN_ID, so they will not clobber each other's run-state — but you" >&2
  echo "   should be aware both are advancing main. Proceeding (non-blocking)." >&2
fi
```

The `peers` subcommand only reports runs whose recorded PID is still alive (`kill -0`); it prunes any dead-PID entry as a side effect, so a sweep killed with SIGKILL mid-run does not produce a false-positive warning forever. Empty output → no peer → the single-sweep case, no warning printed (byte-for-byte the prior behaviour). Among the entries it does report, `status` distinguishes the two genuinely-active cases (`live` — a different PID, heartbeat fresh; `live-same-pid` — this run's own PID, heartbeat fresh) from the two stale ones: `stale-same-pid:Nm` (the #5896 post-`/clear` zombie, same PID as this run) and `stale-heartbeat:Nm` (#6595 — a different PID that is still alive but has not refreshed in N minutes, typically a sweep interrupted inside a session that stayed open; that entry otherwise warned as a `live` peer for the session's whole lifetime, observed at ~8 hours). Neither stale entry is deleted — PID liveness is ambiguous, and per #4691 only confirmed PID death authorizes removing another run's state — they are only labeled, so a stale label is a strong hint, not proof: confirm before adopting or touching that run's lane. **Do not block, do not auto-stop a genuine peer, do not abort** — a `live`/`live-same-pid` peer sweep is legitimate; this remains situational awareness only for those. See "Coexistence (peer `/loom:sweep` and legacy daemon)" for how this relates to the legacy daemon-PID check.

## Stage -1: Backend detection (Phase D of #3449)

Before the dry-run gate and all wave lifecycles (but **after** Sweep Run Identity above), decide whether to **delegate dispatch to the in-process loom-daemon** or **fall through to the existing in-process subagent dispatch**. This stage is prose for the LLM running this skill; it does not run a separate binary. Implementation is small, side-effect-free probes followed by a single routing decision.

This stage exists because Phase A of epic #3449 (#3452) shipped `mcp__loom__dispatch_sweep`, an MCP tool that queues a sweep on the daemon's spawn queue and returns immediately. When the daemon is reachable **and** a multi-account token pool is configured, dispatching to the daemon means each sweep runs in its own detached process with its own rotated OAuth token — load is balanced across accounts, and the orchestrator session exits sub-2-second after dispatch. When either precondition is missing, today's Mode A/B/C subagent path is the right choice — it works on a solo token, it doesn't depend on a running daemon, and it is the verified behaviour for the v0.9.x line.

The contract is **strict AND between two preconditions**, with an explicit Mode C short-circuit and an explicit `--no-daemon` opt-out. There is **no implicit auto-start** of the daemon if the pool exists but the daemon is down; there is **no implicit "use daemon if reachable even without a pool"** branch. Either probe failing → subagent fallthrough.

### Decision tree (the contract)

```text
PROBE_MODE:
  If --prs flag present OR any PR-side NL trigger detected → Mode C (subagent always)

PROBE_DAEMON:
  Ping ~/.loom/loom-daemon.sock with 500ms timeout. Pong → reachable.

PROBE_POOL:
  Count *.token files in .loom/tokens/ OR ACCOUNT_KEY_* lines summed across the merged
  claude-monitor / .loom/accounts.env / legacy .env account sources. Pool exists if count >= 2.

DECIDE:
  if Mode C: use_subagent()
  elif --no-daemon: use_subagent()
  elif LOOM_SWEEP_CLAIM_OWNED is set or CLAIM_OWNED is set: use_subagent()   # daemon-owned child — skip re-probe entirely (#3829/#4111)
  elif PROBE_DAEMON AND PROBE_POOL: use_daemon()
  else: use_subagent()
```

The precedence is deliberate:

1. **Mode C → subagent** (always, regardless of daemon/pool state). This is a routing choice about *this skill's own* `--prs` invocation, **not** a statement about daemon capability: the daemon's dispatch surface accepts `kind={"PrSet":[<n1>,<n2>,…]}` as well as `kind={"Issue":N}` (#5342 — see `.loom/docs/daemon-reference.md` → `dispatch_sweep`), so PR-set dispatch **is** available on the daemon. (An earlier version of this line claimed PR-set dispatch was an explicit non-goal with no v0.10.0 roadmap slot; #5342 landed it, so that claim is retired.) An operator who typed `--prs` is asking for an in-session PR-set run they can watch, so Mode C still routes to the in-process subagent path, which supports Mode C end-to-end. Daemon-side `PrSet` dispatch is used on the daemon path for a *different* purpose — re-routing an issue candidate the daemon's open-PR guard refuses, see "The daemon-dispatch path" below.
2. **`--no-daemon` → subagent** (operator opt-out, after Mode C but before any probes). When this flag is present, do not even attempt the `PROBE_DAEMON` Ping — saves a 500ms ceiling and produces predictable behaviour for debug/demo/scripted runs.
3. **`LOOM_SWEEP_CLAIM_OWNED` set (or the equivalent `--claim-owned N` flag, #4111) → subagent** (daemon-owned child self-detection, #3829 — after `--no-daemon`, still **before** any probes). This env var — and, as of #4111, the positional `--claim-owned <N>` flag in this invocation's own `$ARGUMENTS` — is present **only** on a child that `loom-daemon` itself dispatched (`SweepRegistry::dispatch` → `spawn_child`, `sweep_registry.rs`), carrying the issue number the daemon already claimed on this child's behalf (the same marker/flag the "1. Per-issue pre-flight" Step 1a self-claim check consumes one stage later). A daemon-dispatched child is **by construction** running in the exact environment that makes `PROBE_DAEMON ∧ PROBE_POOL` true — a live daemon plus a multi-account pool, since that is *why* it was dispatched there — so without this rule it would always land on `use_daemon` and issue a **circular** MCP round-trip back into the very daemon that spawned it (`mcp__loom__list_sweeps`, or worse a self-re-dispatch of its own issue number). In headless `claude -p` mode there is no operator to interrupt a stuck tool call and Stage -1's "500ms timeout" is LLM-directed prose, not a mechanically-enforced transport guard, so that round-trip can hang the whole session idle before it ever reaches the Builder phase. The child is already the daemon's work — it must run the lifecycle **itself**, in-process, exactly like `--no-daemon`. This short-circuit removes the entire class of hang. Mirrors `--no-daemon`: do not even attempt the `PROBE_DAEMON` Ping.
4. **`PROBE_DAEMON ∧ PROBE_POOL → daemon`** (the only way to land on the daemon path). **Strict AND**: both probes must succeed. Either missing → fallthrough.
5. **Else → subagent** (the universal fallthrough, equivalent to v0.9.x behaviour).

### The three probes

#### PROBE_MODE — mode classification (already done)

Mode classification happens in the existing "Mode-selection precedence" rules above (Arguments → Validation rules). By the time Stage -1 runs, the skill knows whether it is in Mode A, B, or C. **If the mode is C, the decision is already made — go straight to the subagent path** (the "Stage 0: Dry-run gate" section below, then "PR-set Wave Lifecycle"). Do not run the daemon or pool probes for Mode C.

#### PROBE_DAEMON — is the loom-daemon reachable?

The daemon listens on `~/.loom/loom-daemon.sock` (a Unix-domain socket). A reachability probe is a cheap `mcp__loom__list_sweeps` invocation — the daemon answers with the current sweep list (which may be an empty array if the daemon is up but no sweeps are queued). Either a successful response **or** an empty-list response is a "pong" — the daemon is reachable.

Use a **500ms timeout** on this probe. The MCP layer accepts a timeout parameter; do not raise it. The 500ms ceiling covers two failure modes simultaneously:

- **No daemon running.** The Unix socket file does not exist, or the connection refused immediately. The MCP call returns an error in well under 500ms; treat as `PROBE_DAEMON = false`.
- **Stale socket.** The socket file exists but no process is listening (e.g., the daemon crashed without cleanup). The connection hangs until the OS times out — that's the 500ms guard. Timeout → treat as `PROBE_DAEMON = false`. **Do not retry, do not auto-clean the stale socket, do not auto-start the daemon.** Those behaviours belong in operator tools, not in this skill.

A successful response (any well-formed `EventStream`/sweep-list payload, including the empty case) → `PROBE_DAEMON = true`.

```text
PROBE_DAEMON pseudocode (LLM-directed):

  if NO_DAEMON or LOOM_SWEEP_CLAIM_OWNED is set or CLAIM_OWNED is set:
      PROBE_DAEMON = false   # short-circuit; do not even issue the call
                             # (LOOM_SWEEP_CLAIM_OWNED / --claim-owned: daemon-owned
                             #  child, #3829/#4111 — re-probing the spawning daemon
                             #  is circular)
  else:
      try:
          response = mcp__loom__list_sweeps(timeout_ms=500)
          PROBE_DAEMON = true        # any structured response = reachable
      except timeout, connection_error, no_such_tool:
          PROBE_DAEMON = false
```

The `no_such_tool` case covers older Loom installs without Phase A's MCP additions — treat as "daemon not reachable" and fall through. Do not try to detect the daemon by other means (no `ps` parsing, no PID file reads — the socket probe is the authoritative reachability test).

#### PROBE_POOL — does a multi-account token pool exist?

A pool exists if **either** of these is true (logical OR, both checked):

1. **Materialized pool**: `.loom/tokens/*.token` contains **two or more** files. The bootstrap step (`loom-daemon tokens bootstrap`) writes one `*.token` file per `ACCOUNT_KEY_*` triple in the merged account set; a count `>= 2` means at least two distinct accounts are available for rotation.
2. **Configured pool**: **two or more** `ACCOUNT_KEY_*` lines are declared across the **merged account sources** — the claude-monitor master (`${LOOM_CLAUDE_MONITOR_DIR:-$HOME/.claude-monitor}/accounts.env`), the repo-local file (`.loom/accounts.env`, falling back to the legacy `.env`), and — **only when `LOOM_ACCOUNTS_ENV` is set** — the opt-in home master at that path. This catches the case where the operator has configured multiple accounts (in the post-#3695/#3704 claude-monitor-first layout, not just the legacy `.env`) but hasn't yet run `loom-daemon tokens bootstrap` — the daemon's spawn-time selector can still pick a token, and the pool will be materialized on demand.

Both checks are cheap, local, and side-effect-free. The configured-pool count mirrors `bootstrap.py`'s source precedence but does **not** dedupe by email — a raw sum of `ACCOUNT_KEY_*` lines is an accepted approximation for this boolean `>= 2` gate (worst case a single account declared in two sources double-counts at the `== 1` vs `== 2` boundary, a false-positive toward daemon use that still requires `PROBE_DAEMON` to also be true):

```bash
TOKEN_FILE_COUNT=$(find .loom/tokens -maxdepth 1 -name '*.token' 2>/dev/null | wc -l | tr -d ' ')

# Repo-local (mirrors bootstrap.py: .loom/accounts.env if present, else legacy .env)
# NOTE: `grep -c` prints `0` AND exits non-zero on an existing-but-empty file, so a
# `|| echo 0` fallback would emit a two-line "0\n0" and abort the arithmetic below under
# bash 3.2. Use `|| true` + `${var:-0}` so an existing-empty source yields exactly `0`.
if [[ -f .loom/accounts.env ]]; then
  REPO_KEY_COUNT=$(grep -c '^ACCOUNT_KEY_' .loom/accounts.env 2>/dev/null || true); REPO_KEY_COUNT=${REPO_KEY_COUNT:-0}
else
  REPO_KEY_COUNT=$(grep -c '^ACCOUNT_KEY_' .env 2>/dev/null || true); REPO_KEY_COUNT=${REPO_KEY_COUNT:-0}
fi

# claude-monitor master (primary source per CLAUDE.md; LOOM_CLAUDE_MONITOR_DIR override)
MONITOR_DIR="${LOOM_CLAUDE_MONITOR_DIR:-$HOME/.claude-monitor}"
MONITOR_KEY_COUNT=$(grep -c '^ACCOUNT_KEY_' "$MONITOR_DIR/accounts.env" 2>/dev/null || true); MONITOR_KEY_COUNT=${MONITOR_KEY_COUNT:-0}

# Opt-in home master — only consulted when LOOM_ACCOUNTS_ENV is set and non-empty (per #3704)
HOME_KEY_COUNT=0
if [[ -n "${LOOM_ACCOUNTS_ENV:-}" ]]; then
  HOME_KEY_COUNT=$(grep -c '^ACCOUNT_KEY_' "$LOOM_ACCOUNTS_ENV" 2>/dev/null || true); HOME_KEY_COUNT=${HOME_KEY_COUNT:-0}
fi

ENV_KEY_COUNT=$(( REPO_KEY_COUNT + MONITOR_KEY_COUNT + HOME_KEY_COUNT ))
if (( TOKEN_FILE_COUNT >= 2 )) || (( ENV_KEY_COUNT >= 2 )); then
  PROBE_POOL=true
else
  PROBE_POOL=false
fi

# Discoverable signal: accounts configured but not yet bootstrapped. Only fires when
# the merged sources declare a pool (ENV_KEY_COUNT >= 2) yet .loom/tokens/ has < 2
# token files — NOT on every subagent fallthrough.
if (( ENV_KEY_COUNT >= 2 )) && (( TOKEN_FILE_COUNT < 2 )); then
  echo "Configured account pool detected but not bootstrapped — run 'loom-daemon tokens bootstrap' to materialize .loom/tokens/." >&2
fi
```

A single-token configuration (`TOKEN_FILE_COUNT == 1` and `ENV_KEY_COUNT <= 1`) is **not** a pool — the daemon dispatch path needs at least two accounts to make rotation meaningful, and a single-token operator gets no benefit from delegating to the daemon. Fall through to the subagent path in that case.

> **Why >= 2 and not >= 1?** A pool of one is not a pool — it is a single token, and rotation requires alternatives. The daemon's dispatch advantage (per-sweep token selection, weekly-quota recovery) only materializes once two-or-more accounts are configured. Single-token operators see no degradation in the subagent path; this preserves the existing solo-token experience.

### Resolve auto wave size (when `BUILDERS_PER_WAVE = auto`)

Run this **after `DECIDE` is known** (both probes done) and **before** taking the daemon-dispatch or subagent-fallthrough branch below. If `BUILDERS_PER_WAVE` is a concrete integer (the operator passed `--builders-per-wave N`), **skip this entire block** — the explicit value wins and flows into the wave-partition consumers unchanged. Mode C also never reaches this block: Mode C is size-1 and ignores `--builders-per-wave` (the `DECIDE` precedence already routed it to the subagent path).

The disk math lives in a small sourceable helper so it is deterministic and unit-tested (`defaults/scripts/lib/disk-headroom.sh`, tested by `defaults/scripts/tests/test-disk-headroom.sh`). The skill sources it and calls two functions; it does not do the arithmetic inline:

```bash
source ./.loom/scripts/lib/disk-headroom.sh
REPO_ROOT="$(git rev-parse --show-toplevel)"
# df's the RESOLVED worktree root (scratch volume), not the repo drive.
# Unknown != zero (#4164): loom_worktree_root_free_gb prints NOTHING and
# returns non-zero when it cannot actually measure free space (missing arg,
# unresolvable worktree root, a failing/malformed `df`) — capture the exit
# status here and DO NOT feed a fake "0" into loom_wave_size_from_disk below;
# that used to be indistinguishable from a genuinely full disk.
DISK_PROBE_OK=true
FREE_GB="$(loom_worktree_root_free_gb "$REPO_ROOT")" || DISK_PROBE_OK=false
```

Then resolve by branch (`CAND` = number of surviving candidate issues):

```bash
if [[ "$DECIDE" == use_daemon ]]; then
    # Detached-process path: each sweep is its own OS process with its own
    # rotated token. NOT nested subagents, so #3289 does not apply — scale to 10.
    MECH=daemon;   MECHANISM="daemon detached-process"
else  # use_subagent (no daemon, single-token pool, --no-daemon, daemon-owned child, or Mode C)
    # In-session Task subagents, one level deep. WIDTH is bounded by the harness
    # concurrency cap (min(16, cores-2)), NOT by #3289 (which is a nesting rule,
    # not a width rule). Core-scale the subagent target within [3, 6] via
    # loom_subagent_target_from_cores (#3693); an operator-set LOOM_SUBAGENT_WAVE_CAP
    # always wins (the `:=` only fills an unset/empty value).
    : "${LOOM_SUBAGENT_WAVE_CAP:=$(loom_subagent_target_from_cores "$(loom_detect_cores)")}"
    export LOOM_SUBAGENT_WAVE_CAP
    MECH=subagent; MECHANISM="in-session subagent"
fi
if [[ "$DISK_PROBE_OK" == true ]]; then
    # The helper prints two lines: size on line 1, reason token on line 2.
    # Capture both without `mapfile` (a bash-4.0+ builtin) so this works under
    # macOS's default /bin/bash 3.2: grab stdout once, then split by line.
    _WS_OUT="$(loom_wave_size_from_disk "$MECH" "$CAND" "$FREE_GB")"
    WAVE_SIZE="$(sed -n '1p' <<<"$_WS_OUT")"; REASON="$(sed -n '2p' <<<"$_WS_OUT")"
else
    # Unknown != zero (#4164): the disk probe failed, so SKIP the disk clamp
    # entirely rather than feeding a bogus 0 into loom_wave_size_from_disk
    # (which would floor the wave size to 1 and log reason "floor" —
    # indistinguishable from a genuinely full disk). Fall back to
    # K = min(target, CAND) with no disk term at all.
    if [[ "$MECH" == daemon ]]; then
        _TARGET="${LOOM_DAEMON_WAVE_TARGET:-10}"
    else
        _TARGET="$LOOM_SUBAGENT_WAVE_CAP"
    fi
    if (( CAND < _TARGET )); then
        WAVE_SIZE="$CAND"
    else
        WAVE_SIZE="$_TARGET"
    fi
    (( WAVE_SIZE < 1 )) && WAVE_SIZE=1
    REASON="unknown"
fi
```

`loom_wave_size_from_disk` prints two lines — the clamped size `K = min(target, floor(free_gb / LOOM_PER_WORKTREE_GB), CAND)` with a floor of 1 (never 0, even on a full disk) on line 1, and a machine reason token (`target` / `candidates` / `disk` / `floor`) on line 2. `LOOM_PER_WORKTREE_GB` defaults to a conservative 2 GB and is env-overridable for large-repo operators. The target is **10** for the daemon path; for the subagent path it is the **core-scaled** `clamp(floor((cores-2)/4), 3, 6)` (#3693) — resolved into `LOOM_SUBAGENT_WAVE_CAP` just above via `loom_subagent_target_from_cores` / `loom_detect_cores`, floor 3 on small/shared hosts, ceiling 6 on big ones — and an operator-set `LOOM_SUBAGENT_WAVE_CAP` env value always overrides it.

**When the disk probe fails** (`DISK_PROBE_OK=false`, #4164), skip `loom_wave_size_from_disk` entirely — its pure-integer contract is unchanged and is not the caller-side fallback policy's home — and resolve `WAVE_SIZE = min(target, CAND)` (floor 1) with the reason token `unknown`, so an unmeasurable probe can never masquerade as a measured `0`.

**Emit a one-line reason** so the operator understands any reduction. Map the reason token to a human sentence, adding the backend-specific context:

| `DECIDE` / reason | One-line log |
|-------------------|--------------|
| `use_daemon`, `target` | `wave size 10, mechanism=daemon: daemon + multi-account pool → detached-process path (target 10)` |
| `use_subagent`, `target`, daemon not reachable | `wave size K, mechanism=subagent: daemon not reachable → subagent path (core-scaled target K, floor 3, ceiling 6)` |
| `use_subagent`, `target`, no pool | `wave size K, mechanism=subagent: single-token pool → subagent path (core-scaled target K, floor 3, ceiling 6)` |
| any, `candidates` | `wave size K, mechanism=<m>: reduced to K (only K candidate issues)` |
| any, `disk` | `wave size K, mechanism=<m>: reduced to K (only <FREE_GB> GB free on <worktree-root>)` |
| any, `floor` | `wave size 1, mechanism=<m>: reduced to 1 (only <FREE_GB> GB free on <worktree-root>)` |
| any, `unknown` | `wave size K, mechanism=<m>: disk headroom unknown (probe failed on <worktree-root>) — disk clamp skipped` |

The resolved `WAVE_SIZE` replaces `--builders-per-wave` everywhere the wave-partition consumers below reference it. On the **daemon path** `WAVE_SIZE` is the concurrency **target** the operator should expect (and that `--dry-run` reports) — the daemon runs each candidate as an independent detached process, so it is not a hard in-session partition. On the **subagent path** `WAVE_SIZE` is the literal wave partition size feeding the `min(...)` dispatch expression in the Wave Lifecycle. In both cases, **never raise the subagent ceiling toward 10** — the subagent auto default core-scales within `[3, 6]` (#3693), and true high parallelism toward 10 is the daemon path's job. (This is a width ceiling; the #3289 "one level deep" nesting rule is a separate, unchanged constraint the daemon path exists to route around.)

### The daemon-dispatch path (when `DECIDE = use_daemon`)

When `DECIDE` lands on `use_daemon`, the skill **dispatches each candidate issue** to the daemon and **exits sub-2-second**. There is no in-session orchestration after dispatch — operators monitor with `mcp__loom__list_sweeps` (Phase A) or the richer Phase C tools once they land.

**Housekeeping still applies on this path, at these two fixed points.** Before the first `dispatch_sweep` call below, run the Host Sleep Readiness (#3350) and Main Branch Freshness (#3770) checks (their trigger is now backend-independent — see "before the first wave, or on the daemon path before the first dispatch" in each section below). They matter *more* here than on the subagent path: each of the N children this loop is about to spawn runs for minutes-to-hours as a detached process, and a stale local default branch or a mid-run host sleep affects all N of them at once, not just one session. After the last `dispatch_sweep` call returns, run Step 0a's `sweep-run-registry.sh cleanup "$RUN_ID"` (this run's registry entry and main-clean baseline are no longer needed once dispatch is done) and the Session Transcript Archival step (its own daemon-path note, below, explains what it captures on this path).

**Derive `WORKSPACE_ROOT` once, before dispatching, and pass it explicitly on every `dispatch_sweep` call below.** Omitting `workspace_root` routes through the daemon's workspace-registry resolution (#4299/PR #4322): on a host with multiple managed workspaces registered, it either returns a structured ambiguity error, or — the dangerous case — silently resolves to the daemon's seeded default workspace when that default happens to be registered, targeting the wrong repo with no warning. Always pin the target explicitly:

```bash
WORKSPACE_ROOT=$(git rev-parse --show-toplevel)
```

For each candidate issue `N` in the candidate set:

```text
mcp__loom__dispatch_sweep(kind={"Issue": N}, workspace_root=$WORKSPACE_ROOT)
```

**When `AUTO_STACK=true` and edge detection populated `DEPENDS_ON[N]` for candidate `N`** (see "Auto-stack detection and wave ordering"), forward the detected parent on the dispatch:

```text
mcp__loom__dispatch_sweep(kind={"Issue": N}, depends_on=<parent>, workspace_root=$WORKSPACE_ROOT)
```

This is purely "start populating a parameter that already exists" — the daemon and the `mcp__loom__dispatch_sweep` schema already accept `depends_on` (#3729/#3742), forwarding it to the child as `--depends-on <parent>`, so there is **no daemon-side code change**. Candidates with no detected edge dispatch exactly as today (no `depends_on` argument). To respect the parent-before-child topological ordering on the daemon path, dispatch the reordered candidate list in order (a parent stacked-before its child is dispatched first so its `feature/issue-<parent>` branch exists when the child's Builder resolves the base).

The daemon enqueues the sweep, returns a sweep ID, and the skill logs the dispatch (`Dispatched sweep <sweep-id> for issue #N to daemon`). The daemon's spawn-time logic picks an OAuth token from the rotation pool, detaches a `claude -p "/loom:sweep N"` child, and runs the sweep in that child's session — completely independent of this orchestrator session.

**Safe burst dispatch (Issue #6592).** Candidate bursts in the 10-15 range are the designed usage of this path (the daemon targets 10-way dispatch concurrency), and issuing the `dispatch_sweep` calls for a candidate set concurrently — in one tool block, rather than one-at-a-time — is intentional and supported: the daemon's IPC handler processes the guard-chain + spawn for each request under a brief lock hold and runs the multi-second account-selection poll **off** the shared registry mutex (`dispatch_sweep_nonblocking` in `loom-daemon/src/ipc.rs`), so a burst's acks no longer serialize behind each other and stay well under the client's 30s deadline. Two things to keep in mind regardless:

- **Always pass a stable `idempotency_key` per candidate issue** (e.g. derived from the issue number and run ID) on every `dispatch_sweep` call. If a client-side ack ever does time out anyway — a genuinely oversized burst, host distress, or a slow forge round trip — the request is far more likely to still be queued or executing than actually failed; the timeout error text says so explicitly and names the same key as the safe retry. Retrying with that same key returns the already-dispatched sweep (`was_new: false`) instead of spawning a duplicate.
- **A timed-out ack with no `idempotency_key` is not safe to blindly retry** — check `mcp__loom__list_sweeps` for a sweep that already started on that issue before re-dispatching, since a keyless retry has no dedup protection against a double-spawn.

**The skill does NOT subscribe to events.** Phase B's pub/sub bus is consumed by long-running monitors and the spawn loop, not by the skill itself. The skill is fire-and-forget: dispatch, log, exit.

**Mode C is excluded — by routing, not by daemon capability.** Mode C uses `--prs` (or NL triggers) and the `DECIDE` precedence sends it to the subagent path before this branch is evaluated, so if `PROBE_MODE` returned Mode C this branch is unreachable. That exclusion is a property of *this skill's* precedence table (rule 1 above), **not** a daemon limitation: the daemon does accept `kind={"PrSet":[…]}` (#5342). The paragraph immediately below is exactly where this path issues one.

**Candidates with an open linked PR CANNOT be dispatched issue-keyed on this path (#6593).** The daemon runs its own open-PR guard (#4123) *before* it spawns anything: `dispatch_sweep(kind={"Issue":N})` for an issue whose closes-graph shows an open linked PR `#P` is refused outright with

```text
refusing to dispatch issue #N: it already has an open linked PR #P (#4123 open-PR guard). …
To drive the existing PR forward instead of rebuilding, dispatch kind={"PrSet":[P]} (Mode C, #5342) …
```

This means **the aggressive taxonomy's "Has an open linked PR → drive it through Judge / Doctor → Merge, do not build a duplicate" row (see "Aggressive candidate taxonomy") is NOT reachable via issue-keyed dispatch here.** That routing normally happens in the spawned child's "1. Per-issue pre-flight" existing-PR probe — but on this path no child is ever spawned for such a candidate, so the pre-flight never runs. Dispatch the PR set instead:

```text
mcp__loom__dispatch_sweep(kind={"PrSet": [P]}, workspace_root=$WORKSPACE_ROOT)
```

Rules for this re-route:

- **Only the PR(s) actually linked to the refused candidate.** Build the `PrSet` from the PRs the taxonomy's step-1 union probe attributes to *that* issue — never widen it to unrelated open PRs, and never batch several candidates' PRs into one `PrSet` call (one refused candidate → one `PrSet` dispatch naming only its own PR(s)).
- **Pre-empt the refusal when the candidate set already told you.** The candidate resolution for `all` (and the confirmation-gate listing) already knows which candidates have an open linked PR — dispatch those as `kind={"PrSet":[…]}` in the first place rather than issuing a dispatch you know will be refused.
- **Ambiguity skips, as elsewhere.** If a candidate has more than one open linked PR, skip it with a log (same rule as the per-issue pre-flight's multi-PR ambiguity skip) rather than guessing which PR to drive.
- **A `PrSet` dispatch claims no issue.** It never flips `loom:building` and takes `.loom/locks/pr-<N>/` locks rather than `.loom/locks/issue-<N>/`, so it does not conflict with the issue's existing claim state. The daemon does **not** auto-convert a refused issue-keyed dispatch into a `PrSet` one — the two claim different things, so the re-dispatch is deliberate and explicit.

**Exit immediately after the last `mcp__loom__dispatch_sweep` returns and its housekeeping above (run-registry cleanup, transcript archival) completes.** Do **not** run the dry-run gate, the issue-side wave lifecycle, or any of the "0." through "8." stages below — those are subagent-path-only and would double-orchestrate. This exclusion does **not** cover the pre-/post-dispatch housekeeping named above: that is orchestrator-session bookkeeping (host-sleep, main-freshness, run-registry cleanup, transcript archival) that applies on both paths, not part of the subagent-only "0." through "8." lifecycle. The skill's job in the daemon path is dispatch (plus that housekeeping) and exit; the daemon-side child runs the full Curator → Builder → Judge → Doctor → Merge lifecycle in its own session.

**Dry-run interaction:** when `--dry-run` is passed alongside the daemon path, **the dry-run gate (Stage 0) still runs and the skill EXITs without dispatching**. Dry-run is a read-only contract independent of backend choice; it prints the candidate plan and exits without mutation regardless of whether the daemon would have been used. This is intentional — operators previewing a sweep should see the plan before any backend dispatches.

### The subagent fallthrough (when `DECIDE = use_subagent`)

Otherwise — `DECIDE` is `use_subagent` for **any** of the reasons above (Mode C, `--no-daemon`, `LOOM_SWEEP_CLAIM_OWNED` set / `--claim-owned N` passed (daemon-owned child, #3829/#4111), daemon unreachable, no pool, or any probe error) — **continue to "0. Dry-run gate" below and run the existing Mode A/B/C lifecycle in-process exactly as today**. This is the v0.9.x behaviour, unchanged. The skill prose from "0. Dry-run gate" onward is the canonical subagent path.

No behaviour change for solo-token operators: their `PROBE_POOL` returns `false`, the `DECIDE` lands on `use_subagent`, and the rest of the skill runs as it always has.

### Smoke tests (documented expectations)

These are the AC #3 and AC #4 contracts, written for the operator.

**Daemon-on + multi-account pool (AC #3):**

```bash
# Preconditions:
#   - loom-daemon is running (`pgrep loom-daemon` matches, ~/.loom/loom-daemon.sock exists)
#   - At least 2 accounts configured — in .loom/tokens/, or ACCOUNT_KEY_* lines across
#     the merged claude-monitor / .loom/accounts.env / legacy .env account sources

/loom:sweep 123 456

# Expected:
#   1. Stage -1 runs: PROBE_MODE=A, PROBE_DAEMON=true, PROBE_POOL=true.
#   2. DECIDE = use_daemon.
#   3. Skill calls mcp__loom__dispatch_sweep for issue 123 → logs sweep ID.
#   4. Skill calls mcp__loom__dispatch_sweep for issue 456 → logs sweep ID.
#   5. Skill exits in < 2 seconds.
#   6. Daemon runs the two sweeps independently in detached processes.
#   7. Operator monitors progress via mcp__loom__list_sweeps or Phase C tools.
```

**Daemon-off OR single-token (AC #4):**

```bash
# Preconditions:
#   - Either loom-daemon is not running, OR the merged account sources
#     (claude-monitor / .loom/accounts.env / legacy .env) have < 2 ACCOUNT_KEY_* lines total.

/loom:sweep 123 456

# Expected:
#   1. Stage -1 runs: PROBE_MODE=A, PROBE_DAEMON or PROBE_POOL is false.
#   2. DECIDE = use_subagent.
#   3. Skill continues to "0. Dry-run gate" → "Resolve auto wave size" → "Wave Lifecycle".
#   4. Auto wave size resolves to the subagent path (core-scaled target in [3,6],
#      candidate- and disk-clamped): both issues land in one wave of 2 (clamped
#      to the candidate count, or fewer if the scratch volume is tight).
#   5. Each issue runs Curator→Builder→Judge→Doctor→Merge in-session.
#      (Pass an explicit --builders-per-wave 1 to force the old fully-sequential behaviour.)
#   6. Skill exits when both issues have settled (potentially many minutes).
```

**`--no-daemon` opt-out:**

```bash
# Preconditions: any. The flag forces the subagent path.

/loom:sweep 123 456 --no-daemon

# Expected:
#   1. Stage -1 sees NO_DAEMON=true → PROBE_DAEMON skipped entirely.
#   2. DECIDE = use_subagent.
#   3. Skill continues to "0. Dry-run gate" → "Wave Lifecycle" → ... exactly as today.
```

**Mode C (PR-set):**

```bash
# Preconditions: any. Mode C short-circuits Stage -1's daemon path.

/loom:sweep --prs 200 201

# Expected:
#   1. PROBE_MODE = C (because --prs is present).
#   2. DECIDE = use_subagent (regardless of daemon/pool state).
#   3. Skill continues to "0. Dry-run gate" → "PR-set Wave Lifecycle" → ... exactly as today.
```

**Daemon-owned child (`LOOM_SWEEP_CLAIM_OWNED` set / `--claim-owned N` passed, #3829/#4111):**

```bash
# Preconditions: this session is itself a child that loom-daemon dispatched, so
#   LOOM_SWEEP_CLAIM_OWNED=<N> is exported into its environment AND --claim-owned
#   N is embedded in its own -p prompt text / $ARGUMENTS (by
#   SweepRegistry::dispatch → spawn_child). The daemon and multi-account pool are
#   therefore reachable BY CONSTRUCTION — but this child must NOT re-dispatch.

# (the daemon internally runs, for the issue it claimed:)
#   LOOM_SWEEP_CLAIM_OWNED=123 claude -p "/loom:sweep 123 --claim-owned 123" --dangerously-skip-permissions

# Expected:
#   1. Stage -1 sees LOOM_SWEEP_CLAIM_OWNED is set → PROBE_DAEMON skipped entirely
#      (never issues mcp__loom__list_sweeps back into the spawning daemon).
#   2. DECIDE = use_subagent regardless of daemon/pool reachability.
#   3. Skill continues to "0. Dry-run gate" → "Wave Lifecycle" → runs the full
#      Curator→Builder→Judge→Doctor→Merge lifecycle IN-PROCESS, exactly like --no-daemon.
#   4. No circular re-dispatch of its own issue number; no idle-hang on a stuck
#      MCP round-trip. This is the #3829 fix — every daemon-dispatched child
#      progresses to build rather than stalling in Stage -1.
```

### What Stage -1 does NOT do

- **Does not auto-start the daemon** if the pool exists but the daemon is unreachable. Auto-start is operator policy, not skill policy.
- **Does not write `~/.loom/loom-daemon.sock` cleanup** for stale sockets. Stale-socket cleanup belongs to the daemon's own startup logic and to operator tools.
- **Does not subscribe to the Phase B event bus.** Subscription is consumed by long-running monitors and the spawn loop, not by this skill. Phase D is dispatch-only.
- **Does not retry probe failures.** Either probe returns within 500ms (or its natural latency) and is treated as authoritative; no retry, no backoff.
- **Does not mutate any forge state** during the probes. `mcp__loom__list_sweeps` and the local pool checks are read-only. Even in the daemon path, mutation happens inside the daemon-side child sweep, not in this orchestrator session.
- **Does not log to `.loom/daemon-state.json` or any daemon-owned state file.** Read-only access is fine for situational awareness; writes are forbidden (same constraint as the legacy-daemon subsection of "Coexistence (peer `/loom:sweep` and legacy daemon)").
- **Does not re-probe or re-dispatch to the daemon when it is itself a daemon-dispatched child (#3829/#4111).** If `LOOM_SWEEP_CLAIM_OWNED` is set or `--claim-owned N` was passed, the child is already the daemon's work — the `DECIDE` tree short-circuits to `use_subagent()` **before** `PROBE_DAEMON` runs, so no `mcp__loom__list_sweeps` (and no `mcp__loom__dispatch_sweep` of its own issue) is ever issued back into the spawning daemon. Re-probing/re-dispatching there is circular by construction and, in a headless `-p` session with no operator to interrupt a stuck tool call, was the cause of the idle-hang this rule removes.

