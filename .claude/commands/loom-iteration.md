# Loom Daemon - Iteration Mode

You are the Layer 2 Loom Daemon running in ITERATION MODE in the {{workspace}} repository.

**This file is for ITERATION MODE ONLY.** If you are running parent mode (`/loom` without `iterate`), you should be reading `loom-parent.md` instead.

## Your Role (Iteration Mode)

**You are the subagent spawned by the parent loop. Execute exactly ONE daemon iteration with fresh context, then return a compact summary.**

In iteration mode, you:
1. Load state from JSON
2. Check shutdown signal
3. Assess system state via `loom-tools snapshot`
4. Check shepherd completions (process-tree + progress files)
5. Auto-promote proposals (if force mode)
6. Spawn shepherds for ready issues (via agent-spawn.sh)
7. Trigger work generation
8. Ensure support roles
9. Detect stuck agents
10. Save state to JSON
11. **Return a compact 1-line summary and EXIT**

**CRITICAL**: After completing the iteration, return ONLY the summary line. Do NOT loop. Do NOT spawn iteration subagents. The parent loop handles repetition.

## Execution Model

The daemon uses **tmux-based agent execution** exclusively. All workers run in ephemeral tmux sessions via `agent-spawn.sh`, `agent-wait.sh`, and `agent-destroy.sh`.

- **Shepherds**: Spawned as on-demand tmux sessions (e.g., `loom-shepherd-issue-42`)
- **Support roles**: Spawned as on-demand tmux sessions (e.g., `loom-guide`, `loom-champion`)
- **Completion detection**: `agent-wait.sh` polls process trees + progress file cross-reference
- **Cleanup**: `agent-destroy.sh` removes sessions after completion
- **Observability**: `tmux -L loom attach -t <session-name>`

## Iteration Execution

**CRITICAL**: The iteration MUST use `python3 -m loom_tools.snapshot` (or `loom-tools snapshot`) for state assessment and act on its `recommended_actions`. This ensures deterministic behavior and proper work generation triggering.

```python
def loom_iterate(force_mode=False, debug_mode=False):
    """Execute exactly ONE daemon iteration. Called by parent loop."""

    # Helper function for debug logging
    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    # 1. Load state from JSON (enables stateless execution)
    state = load_daemon_state(".loom/daemon-state.json")
    iteration = state.get("iteration", 0) + 1
    our_session_id = state.get("daemon_session_id")
    debug(f"Iteration {iteration} starting at {now()}")
    debug(f"Session ID: {our_session_id}")

    # 1b. Reconcile force_mode from argument and state
    # The argument takes precedence (it comes from the current daemon invocation)
    effective_force_mode = force_mode or state.get("force_mode", False)
    if force_mode and not state.get("force_mode", False):
        state["force_mode"] = force_mode
        save_daemon_state(state)
    debug(f"Force mode: {effective_force_mode} (arg={force_mode}, state={state.get('force_mode')})")

    # 2. Check shutdown signal
    if exists(".loom/stop-daemon"):
        debug("Shutdown signal detected")
        return "SHUTDOWN_SIGNAL"

    # 3. CRITICAL: Get system state via loom-tools snapshot
    # This is the CANONICAL source for all state and recommended actions
    snapshot = run("python3 -m loom_tools.snapshot")
    snapshot_data = json.loads(snapshot)

    # Extract computed decisions (these are authoritative)
    recommended_actions = snapshot_data["computed"]["recommended_actions"]
    ready_count = snapshot_data["computed"]["total_ready"]
    needs_work_gen = snapshot_data["computed"]["needs_work_generation"]
    architect_cooldown_ok = snapshot_data["computed"]["architect_cooldown_ok"]
    hermit_cooldown_ok = snapshot_data["computed"]["hermit_cooldown_ok"]

    debug(f"Pipeline state: ready={ready_count} building={snapshot_data['computed']['total_building']}")
    debug(f"Recommended actions: {recommended_actions}")

    # 4. Check shepherd completions (process-tree AND progress file signals)
    completions = check_shepherd_completions(state, snapshot_data, debug_mode)
    if debug_mode and completions:
        for c in completions:
            debug(f"Completion detected: {c['name']} issue=#{c.get('issue', 'N/A')} reason={c.get('reason', 'unknown')}")

    # 4b. Check support role completions (all 7 roles)
    completed_support_roles = check_support_role_completions(state, debug_mode)
    if completed_support_roles:
        debug(f"Support roles completed: {completed_support_roles}")

    # 5. CRITICAL: Act on recommended_actions: promote_proposals (force mode only)
    promoted_count = 0
    if "promote_proposals" in recommended_actions and effective_force_mode:
        promotable = snapshot_data["computed"]["promotable_proposals"]
        debug(f"Auto-promoting {len(promotable)} proposals in force mode")
        promoted_count = auto_promote_proposals(promotable, state, debug_mode)

    # 6. Act on recommended_actions: spawn_shepherds
    spawned_shepherds = []
    if "spawn_shepherds" in recommended_actions:
        debug(f"Shepherd pool: {format_shepherd_pool(state)}")
        spawned_shepherds = auto_spawn_shepherds(state, snapshot_data, debug_mode)

    # 7. Check workflow demand (demand-based spawning for champion/doctor/judge)
    demand_spawned = check_workflow_demand(state, snapshot_data, recommended_actions, debug_mode)

    # 8. CRITICAL: Act on recommended_actions: trigger ALL support roles (interval-based)
    # This now includes architect and hermit alongside guide, champion, doctor, auditor, judge.
    # All 7 roles use the unified spawn-support-role.sh infrastructure.
    ensured_roles = auto_ensure_support_roles(state, snapshot_data, recommended_actions, debug_mode, demand_spawned)

    # Extract work generation results from ensured_roles (for summary formatting)
    triggered_generation = {
        "architect": ensured_roles.get("architect", False),
        "hermit": ensured_roles.get("hermit", False)
    }

    # Log work generation even when not triggered (for debugging)
    if needs_work_gen and not triggered_generation["architect"] and not triggered_generation["hermit"]:
        debug(f"Work generation needed but not triggered: architect_cooldown_ok={architect_cooldown_ok}, hermit_cooldown_ok={hermit_cooldown_ok}")

    # 9. Stuck detection
    stuck_count = check_stuck_agents(state, debug_mode)

    # 9b. Health warnings: emit WARN: codes from snapshot health data
    health_warnings = snapshot_data["computed"].get("health_warnings", [])
    health_status = snapshot_data["computed"].get("health_status", "healthy")
    debug(f"Health status: {health_status}, warnings: {len(health_warnings)}")

    # 9c. LLM-side systematic failure detection
    # When pipeline is stalled (blocked>0 and ready=0), inspect blocked issue
    # details for shared error patterns. This complex pattern matching is
    # natural for the LLM but hard to express in shell.
    if snapshot_data["computed"]["total_blocked"] > 0 and snapshot_data["computed"]["total_ready"] == 0:
        systematic_failure = detect_systematic_failure(snapshot_data, state, debug_mode)
        if systematic_failure:
            health_warnings.append({
                "code": "systematic_failure",
                "level": "warning",
                "message": systematic_failure["message"]
            })
            debug(f"Systematic failure detected: {systematic_failure['message']}")

    # 9d. Populate daemon-state.json warnings array (fulfills existing schema)
    state["warnings"] = format_state_warnings(health_warnings)

    # 10. Orphan recovery (every 5 iterations) - catches crashed shepherds mid-session
    recovered_count = 0
    if state.get("iteration", 0) % 5 == 0:
        debug("Running orphan recovery check (every 5 iterations)")
        recovered_count = check_orphaned_shepherds(state, debug_mode)

    # 10b. Stale building detection (every 10 iterations) - catches issues without PRs
    if state.get("iteration", 0) % 10 == 0:
        debug("Running stale building check (every 10 iterations)")
        stale_recovered = check_stale_building(state, debug_mode)
        recovered_count += stale_recovered

    # 11. Save state to JSON (with session ID validation)
    # Before writing, verify our session ID still owns the state file
    # This prevents dual-daemon state corruption
    if our_session_id:
        current_state = load_daemon_state(".loom/daemon-state.json")
        current_session_id = current_state.get("daemon_session_id")
        if current_session_id and current_session_id != our_session_id:
            debug(f"SESSION CONFLICT: State file session changed!")
            debug(f"  Our session:  {our_session_id}")
            debug(f"  File session: {current_session_id}")
            debug(f"  Refusing to write state - another daemon has taken over")
            return f"SESSION_CONFLICT our={our_session_id} file={current_session_id}"

    state["iteration"] = state.get("iteration", 0) + 1
    state["last_poll"] = now()
    state["debug_mode"] = debug_mode
    save_daemon_state(state)

    # 12. Return compact summary (ONE LINE)
    # Include WARN: codes from health_warnings at the end of the summary line
    summary = format_iteration_summary(snapshot_data, spawned_shepherds, triggered_generation, ensured_roles, demand_spawned, promoted_count, stuck_count, recovered_count, health_warnings)
    debug(f"Iteration {iteration} completed - {summary}")
    return summary
```

## Iteration Summary Format

The iteration MUST return a compact summary (one line, ~50-100 chars):

```
ready=5 building=2 shepherds=2/3 +shepherd=#123 +architect
```

**Summary components:**
- `ready=N` - Issues with loom:issue label
- `building=N` - Issues with loom:building label
- `shepherds=N/M` - Active/max shepherds
- `+shepherd=#N` - Spawned shepherd for issue (if any)
- `+architect` - Triggered Architect (if triggered)
- `+hermit` - Triggered Hermit (if triggered)
- `+guide` - Respawned Guide (if respawned)
- `+champion` - Respawned Champion (if respawned, interval-based)
- `+champion(demand)` - Spawned Champion on-demand (PRs ready to merge)
- `+doctor` - Respawned Doctor (if respawned, interval-based)
- `+doctor(demand)` - Spawned Doctor on-demand (PRs need fixes)
- `+auditor` - Respawned Auditor (if respawned)
- `+judge` - Respawned Judge (if respawned, interval-based)
- `+judge(demand)` - Spawned Judge on-demand (PRs need review)
- `promoted=N` - Proposals auto-promoted to loom:issue in force mode (if any)
- `stuck=N` - Stuck agents detected (if any)
- `completed=#N` - Issue completed this iteration (if any)
- `recovered=N` - Stale building issues recovered (if any)
- `spawn-fail=N` - Task spawns that failed verification (if any)
- `WARN:code` - Health warnings from snapshot (if any, appended at end)

**Health warning codes** (from `computed.health_warnings` in snapshot):
- `WARN:pipeline_stalled` - 0 ready, 0 building, >0 blocked
- `WARN:proposal_backlog` - Proposals exist but pipeline empty
- `WARN:no_work_available` - Pipeline completely empty
- `WARN:stale_heartbeats` - Shepherd(s) with stale heartbeats
- `WARN:orphaned_issues` - Orphaned shepherds detected
- `WARN:session_budget_low` - Session usage nearing limit
- `WARN:systematic_failure` - Multiple shepherds failed with same cause (LLM-detected)

**Example summaries:**
```
ready=5 building=2 shepherds=2/3
ready=3 building=3 shepherds=3/3 +shepherd=#456 completed=#123
ready=0 building=1 shepherds=1/3 +architect +hermit
ready=2 building=2 shepherds=2/3 stuck=1
ready=2 building=2 shepherds=2/3 spawn-fail=1
ready=3 building=0 shepherds=0/3 promoted=3
ready=0 building=0 shepherds=0/3 WARN:pipeline_stalled
ready=0 building=0 shepherds=0/3 WARN:pipeline_stalled WARN:proposal_backlog
ready=0 building=0 shepherds=0/3 WARN:no_work_available
SHUTDOWN_SIGNAL
```

## Using loom-tools snapshot for State Assessment

The `loom-tools snapshot` command consolidates all state queries into a single tool call, replacing 10+ individual `gh` commands:

```bash
# Get complete system state in one call
snapshot=$(python3 -m loom_tools.snapshot)

# Parse the JSON output
ready_count=$(echo "$snapshot" | jq '.computed.total_ready')
needs_work_gen=$(echo "$snapshot" | jq -r '.computed.needs_work_generation')
actions=$(echo "$snapshot" | jq -r '.computed.recommended_actions[]')

# Use pre-computed decisions
if [[ "$needs_work_gen" == "true" ]]; then
    # Trigger architect/hermit
fi

# Check recommended actions
if echo "$actions" | grep -q "spawn_shepherds"; then
    # Auto-spawn shepherds for ready issues
fi
```

**Benefits of loom-tools snapshot:**
- **Single tool call**: Replaces 10+ individual `gh` commands
- **Parallel queries**: Runs all `gh` queries concurrently for efficiency
- **Pre-computed decisions**: Includes `computed.recommended_actions` for immediate use
- **Token efficient**: Reduces context usage by ~50% per iteration
- **Deterministic**: Same output format every time, easy to parse

**Output structure:**
```json
{
  "timestamp": "2026-01-25T08:00:00Z",
  "pipeline": { "ready_issues": [...], "building_issues": [...], "blocked_issues": [...] },
  "proposals": { "architect": [...], "hermit": [...], "curated": [...] },
  "prs": { "review_requested": [...], "changes_requested": [...], "ready_to_merge": [...] },
  "usage": { "session_percent": 45, "healthy": true },
  "computed": {
    "total_ready": 3,
    "needs_work_generation": false,
    "available_shepherd_slots": 2,
    "recommended_actions": ["spawn_shepherds", "check_stuck"],
    "health_status": "healthy",
    "health_warnings": []
  },
  "config": { "issue_threshold": 3, "max_shepherds": 3 }
}
```

## Auto-Spawn Shepherds

Shepherds are spawned as ephemeral tmux sessions via `agent-spawn.sh`. Each shepherd runs in its own attachable session with fresh context.

```python
# Maximum spawn failures before marking an issue as blocked
MAX_SPAWN_FAILURES = 3

def auto_spawn_shepherds(state, snapshot_data, debug_mode=False):
    """Automatically spawn shepherds as ephemeral tmux workers.

    Tracks spawn failures per issue. After MAX_SPAWN_FAILURES consecutive failures,
    the issue is marked as loom:blocked to prevent infinite retry loops.
    """

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    # loom-tools snapshot returns issues pre-sorted by LOOM_ISSUE_STRATEGY:
    # - loom:urgent issues always first (regardless of strategy)
    # - Then sorted by: fifo (oldest first), lifo (newest first), or priority
    ready_issues = snapshot_data["pipeline"]["ready_issues"]
    active_count = count_active_shepherds(state)
    max_shepherds = snapshot_data["config"]["max_shepherds"]

    # Initialize retry queue in state if not present
    if "spawn_retry_queue" not in state:
        state["spawn_retry_queue"] = {}

    # Determine shepherd mode based on daemon's force_mode
    force_mode = state.get("force_mode", False)
    shepherd_flag = "--merge" if force_mode else ""  # default is force-pr behavior

    debug(f"Issue selection: {len(ready_issues)} ready issues, {active_count}/{max_shepherds} shepherds active")
    debug(f"Shepherd mode: {shepherd_flag} (force_mode={force_mode})")

    spawned = []
    spawn_failures = 0

    while active_count < max_shepherds and len(ready_issues) > 0:
        issue = ready_issues.pop(0)["number"]
        issue_key = str(issue)

        # Check retry queue for this issue
        retry_info = state["spawn_retry_queue"].get(issue_key, {"failures": 0, "last_attempt": None})

        # Skip issues that have exceeded max failures (they should already be blocked)
        if retry_info["failures"] >= MAX_SPAWN_FAILURES:
            debug(f"Issue #{issue} exceeded max spawn failures ({MAX_SPAWN_FAILURES}), should be blocked")
            continue

        debug(f"Issue selection: Claiming #{issue} (prior failures={retry_info['failures']})")

        # Claim immediately (atomic operation)
        run(f"gh issue edit {issue} --remove-label 'loom:issue' --add-label 'loom:building'")

        # Spawn shepherd as ephemeral tmux worker
        session_name = f"shepherd-issue-{issue}"
        result = dispatch_shepherd_tmux(issue, shepherd_flag, session_name, debug_mode)

        # Verify spawn succeeded
        if not result.get("success"):
            error_msg = result.get('error', 'unknown')
            debug(f"Spawn failed for #{issue}: {error_msg}")

            # Track failure in retry queue
            retry_info["failures"] += 1
            retry_info["last_attempt"] = now()
            retry_info["last_error"] = error_msg
            state["spawn_retry_queue"][issue_key] = retry_info

            if retry_info["failures"] >= MAX_SPAWN_FAILURES:
                # Mark as blocked after max failures
                debug(f"Issue #{issue} reached max spawn failures ({MAX_SPAWN_FAILURES}), marking as blocked")
                run(f"gh issue edit {issue} --remove-label 'loom:building' --add-label 'loom:blocked'")
                comment = f"**[daemon] Spawn Failed**\\n\\nThis issue has been marked as blocked after {MAX_SPAWN_FAILURES} consecutive spawn failures.\\n\\nLast error: `{error_msg}`\\n\\nA human or the Doctor role may need to investigate and unblock this issue."
                run(f"gh issue comment {issue} --body '{comment}'")
                print(f"  BLOCKED: #{issue} (spawn failed {MAX_SPAWN_FAILURES}x)")
            else:
                # Revert to loom:issue for retry
                debug(f"Reverting #{issue} to loom:issue for retry (failures={retry_info['failures']})")
                run(f"gh issue edit {issue} --remove-label 'loom:building' --add-label 'loom:issue'")

            spawn_failures += 1
            save_daemon_state(state)
            continue

        # Spawn succeeded - clear retry queue for this issue
        if issue_key in state["spawn_retry_queue"]:
            del state["spawn_retry_queue"][issue_key]
            save_daemon_state(state)

        debug(f"Spawning decision: shepherd assigned to #{issue} (verified)")
        debug(f"  tmux session: loom-{session_name}")

        # Record assignment
        record_shepherd_assignment(state, issue, session_name)

        active_count += 1
        spawned.append(issue)

        print(f"  AUTO-SPAWNED: shepherd for issue #{issue} ({shepherd_flag}, tmux:loom-{session_name})")

    if len(spawned) == 0 and len(ready_issues) == 0:
        debug("No shepherds spawned: no ready issues available")
    elif len(spawned) == 0:
        debug(f"No shepherds spawned: at capacity ({max_shepherds} max)")

    return {"spawned": spawned, "failures": spawn_failures}


def dispatch_shepherd_tmux(issue, shepherd_flag, session_name, debug_mode=False):
    """Spawn a shepherd as an ephemeral tmux worker.

    Uses either:
    - Shell-based shepherd (LOOM_SHELL_SHEPHERDS=true): spawn-shell-shepherd.sh
      - Deterministic shell script orchestration
      - No token accumulation across phases
      - ~80% token cost reduction vs LLM shepherd

    - LLM-based shepherd (default): agent-spawn.sh --role shepherd
      - LLM-interpreted orchestration via /shepherd slash command
      - Higher token cost but more flexible
    """

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    debug(f"Spawning tmux shepherd: {session_name} for issue #{issue}")

    # Check if shell-based shepherds are enabled
    use_shell_shepherd = os.environ.get("LOOM_SHELL_SHEPHERDS", "false").lower() == "true"

    if use_shell_shepherd:
        # Use deterministic shell-based shepherd
        debug("Using shell-based shepherd (LOOM_SHELL_SHEPHERDS=true)")
        result = run(
            f'./.loom/scripts/spawn-shell-shepherd.sh {issue} '
            f'{shepherd_flag} --name "{session_name}" --json'
        )
    else:
        # Use LLM-based shepherd via agent-spawn.sh
        debug("Using LLM-based shepherd (default)")
        result = run(
            f'./.loom/scripts/agent-spawn.sh --role shepherd --name "{session_name}" '
            f'--args "{issue} {shepherd_flag}" --on-demand --json'
        )

    try:
        spawn_result = json.loads(result)
        if spawn_result.get("status") in ("started", "spawned"):
            return {
                "success": True,
                "session_name": session_name
            }
        else:
            return {"success": False, "error": spawn_result.get("error", "spawn_failed")}
    except Exception as e:
        debug(f"Failed to parse shepherd spawn result: {e}")
        return {"success": False, "error": str(e)}


def record_shepherd_assignment(state, issue, session_name):
    """Record shepherd assignment in daemon state."""
    if "shepherds" not in state:
        state["shepherds"] = {}

    state["shepherds"][session_name] = {
        "status": "working",
        "issue": issue,
        "tmux_session": f"loom-{session_name}",
        "started": now()
    }
    save_daemon_state(state)
```

## Auto-Promote Proposals (Force Mode Only)

In force mode, the daemon AUTOMATICALLY promotes proposals to `loom:issue`:

```python
def auto_promote_proposals(promotable_issues, state, debug_mode=False):
    """Auto-promote proposals to loom:issue in force mode."""

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    promoted = 0

    for issue_num in promotable_issues:
        try:
            # Get current labels
            issue_data = run(f"gh issue view {issue_num} --json labels --jq '.labels[].name'")
            labels = issue_data.strip().split('\n')

            # Determine which proposal label to remove
            remove_label = None
            proposal_type = None
            if "loom:architect" in labels:
                remove_label = "loom:architect"
                proposal_type = "architect"
            elif "loom:hermit" in labels:
                remove_label = "loom:hermit"
                proposal_type = "hermit"
            elif "loom:curated" in labels:
                remove_label = "loom:curated"
                proposal_type = "curated"
            else:
                debug(f"Issue #{issue_num} has no promotable label, skipping")
                continue

            # Skip if blocked
            if "loom:blocked" in labels:
                debug(f"Issue #{issue_num} is blocked, skipping promotion")
                continue

            # Promote: remove proposal label, add loom:issue
            run(f"gh issue edit {issue_num} --remove-label '{remove_label}' --add-label 'loom:issue'")

            # Add audit trail comment
            timestamp = now()
            comment = f"""**[force-mode] Daemon Auto-Promotion**

This {proposal_type} proposal has been automatically promoted to `loom:issue` by the Loom daemon running in force mode.

**Ready for Builder** - A shepherd will claim this issue in the next iteration.

**Force mode enabled**: Champion evaluation bypassed for aggressive autonomous development.

---
*Automated by Loom daemon (force mode) at {timestamp}*"""
            run(f"gh issue comment {issue_num} --body '{comment}'")

            promoted += 1
            debug(f"Promoted #{issue_num} ({proposal_type} -> loom:issue)")

        except Exception as e:
            debug(f"Failed to promote #{issue_num}: {e}")
            continue

    return promoted
```

## Trigger Work Generation

**CRITICAL**: Work generation keeps the pipeline fed. When `loom-tools snapshot` includes `trigger_architect` or `trigger_hermit` in `recommended_actions`, the iteration MUST spawn these roles.

**Note**: `trigger_architect_role()` and `trigger_hermit_role()` have been removed. Architect and
hermit now use the unified `trigger_support_role()` function (same as guide, champion, doctor,
auditor, and judge). This eliminates separate state management paths and ensures all support roles
use `spawn-support-role.sh` for deterministic state management. The top-level
`last_architect_trigger` and `last_hermit_trigger` fields in daemon-state.json are no longer
written; cooldown is now tracked via `support_roles.architect.last_completed` and
`support_roles.hermit.last_completed`.

## Deterministic Support Role Spawning

Support role spawning uses the deterministic `spawn-support-role.sh` script for all
spawn decisions. The script is read-only (pure decision function) and does not modify
daemon-state.json. State management is handled in-memory by the iteration subagent,
which is the sole writer of daemon-state.json. This eliminates both LLM interpretation
variability and race conditions between concurrent writers.

### spawn-support-role.sh

The script is a **pure decision function** (read-only) that handles:
- **Interval checking**: Whether enough time has elapsed since last completion
- **Idempotency**: Never spawns if role already running with valid task_id
- **Demand mode**: Immediate spawn when `--demand` flag passed (skips interval)
- **Fabricated ID detection**: Detects roles stuck with invalid task IDs

**State management** (marking roles as running/completed) is handled entirely by the
iteration subagent in-memory. The iteration subagent is the sole writer of
daemon-state.json, which eliminates race conditions between concurrent writers.

```bash
# Check if a role should be spawned (interval-based)
./.loom/scripts/spawn-support-role.sh guide --json
# {"should_spawn":true,"reason":"interval_elapsed","role":"guide",...}

# Check if a role should be spawned (demand-based, skips interval)
./.loom/scripts/spawn-support-role.sh champion --demand --json
# {"should_spawn":true,"reason":"demand","role":"champion"}

# Check all roles at once
./.loom/scripts/spawn-support-role.sh --check-all --json
# {"roles":[...],"any_should_spawn":true}
```

## Workflow Demand (Demand-Based Spawning)

The daemon spawns Champion/Doctor/Judge immediately when work awaits them, providing faster response than interval-based spawning. Uses `spawn-support-role.sh --demand` to skip interval checks:

```python
def check_workflow_demand(state, snapshot_data, recommended_actions, debug_mode=False):
    """Spawn roles immediately when work awaits them.

    Uses spawn-support-role.sh with --demand flag for deterministic spawn decisions.
    The script checks idempotency (not already running) even in demand mode.
    """

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    demand_spawned = {"champion": False, "doctor": False, "judge": False}

    # Champion on-demand: PRs ready to merge
    if "spawn_champion_demand" in recommended_actions:
        prs_ready = snapshot_data.get("prs", {}).get("ready_to_merge", [])
        pr_count = len(prs_ready)
        debug(f"Champion demand detected: {pr_count} PRs ready to merge")

        # Use deterministic script to check if spawn is allowed
        check = run("./.loom/scripts/spawn-support-role.sh champion --demand --json")
        check_data = json.loads(check)
        if check_data["should_spawn"]:
            if trigger_support_role(state, "champion", f"Champion (on-demand, {pr_count} PRs)", debug_mode):
                demand_spawned["champion"] = True
                print(f"  AUTO-SPAWNED: Champion (on-demand, {pr_count} PRs ready to merge)")
        else:
            debug(f"Champion demand skipped: {check_data['reason']}")

    # Doctor on-demand: PRs need fixes
    if "spawn_doctor_demand" in recommended_actions:
        prs_needing_fixes = snapshot_data.get("prs", {}).get("changes_requested", [])
        pr_count = len(prs_needing_fixes)
        debug(f"Doctor demand detected: {pr_count} PRs need fixes")

        check = run("./.loom/scripts/spawn-support-role.sh doctor --demand --json")
        check_data = json.loads(check)
        if check_data["should_spawn"]:
            if trigger_support_role(state, "doctor", f"Doctor (on-demand, {pr_count} PRs)", debug_mode):
                demand_spawned["doctor"] = True
                print(f"  AUTO-SPAWNED: Doctor (on-demand, {pr_count} PRs need fixes)")
        else:
            debug(f"Doctor demand skipped: {check_data['reason']}")

    # Judge on-demand: PRs need review
    if "spawn_judge_demand" in recommended_actions:
        prs_needing_review = snapshot_data.get("prs", {}).get("review_requested", [])
        pr_count = len(prs_needing_review)
        debug(f"Judge demand detected: {pr_count} PRs need review")

        check = run("./.loom/scripts/spawn-support-role.sh judge --demand --json")
        check_data = json.loads(check)
        if check_data["should_spawn"]:
            if trigger_support_role(state, "judge", f"Judge (on-demand, {pr_count} PRs)", debug_mode):
                demand_spawned["judge"] = True
                print(f"  AUTO-SPAWNED: Judge (on-demand, {pr_count} PRs need review)")
        else:
            debug(f"Judge demand skipped: {check_data['reason']}")

    return demand_spawned
```

## Auto-Ensure Support Roles (Interval-Based)

Uses `spawn-support-role.sh` (without `--demand`) for interval-based spawn decisions:

```python
def auto_ensure_support_roles(state, snapshot_data, recommended_actions, debug_mode=False, demand_spawned=None):
    """Automatically keep Guide, Champion, Doctor, Auditor, Judge, Architect, and Hermit running.

    Uses spawn-support-role.sh for deterministic interval checking. The script
    handles all interval math, idempotency, and fabricated task_id detection.

    Architect and hermit are managed here alongside the other support roles,
    using the same spawn-support-role.sh infrastructure for unified state management.
    """

    if demand_spawned is None:
        demand_spawned = {"champion": False, "doctor": False, "judge": False}

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    ensured_roles = {"guide": False, "champion": False, "doctor": False, "auditor": False, "judge": False, "architect": False, "hermit": False}

    debug("Checking support roles via spawn-support-role.sh (interval-based)")
    debug(f"Recommended actions: {recommended_actions}")
    debug(f"Demand-spawned: {demand_spawned}")

    # Define which roles to check and their trigger actions
    # Architect and hermit use trigger_architect/trigger_hermit actions from loom-tools snapshot
    role_checks = [
        ("guide", "trigger_guide", "Guide backlog triage", False),
        ("champion", "trigger_champion", "Champion PR merge", demand_spawned.get("champion", False)),
        ("doctor", "trigger_doctor", "Doctor PR conflict resolution", demand_spawned.get("doctor", False)),
        ("auditor", "trigger_auditor", "Auditor main branch validation", False),
        ("judge", "trigger_judge", "Judge PR review", demand_spawned.get("judge", False)),
        ("architect", "trigger_architect", "Architect work generation", False),
        ("hermit", "trigger_hermit", "Hermit simplification proposals", False),
    ]

    for role_name, trigger_action, description, already_spawned in role_checks:
        # Skip if demand already spawned this role
        if already_spawned:
            debug(f"Skipping {role_name}: already demand-spawned this iteration")
            continue

        # Skip if not in recommended actions
        if trigger_action not in recommended_actions:
            continue

        # Use deterministic script to check if spawn is needed
        check = run(f"./.loom/scripts/spawn-support-role.sh {role_name} --json")
        try:
            check_data = json.loads(check)
        except Exception:
            debug(f"Failed to parse spawn-support-role.sh output for {role_name}")
            continue

        if check_data["should_spawn"]:
            debug(f"{role_name}: spawn needed ({check_data['reason']})")
            ensured_roles[role_name] = trigger_support_role(state, role_name, description, debug_mode)
        else:
            debug(f"{role_name}: spawn not needed ({check_data['reason']})")

    return ensured_roles


def trigger_support_role(state, role_name, description, debug_mode=False):
    """Spawn a support role as an ephemeral tmux worker via agent-spawn.sh.

    After successful spawn, updates in-memory state only. The iteration subagent
    is the sole writer of daemon-state.json - state is saved once at the end of
    the iteration via save_daemon_state(state).
    """

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    # Spawn as ephemeral tmux session
    session_name = role_name  # e.g., "guide", "champion"
    result = run(
        f'./.loom/scripts/agent-spawn.sh --role {role_name} --name "{session_name}" --on-demand --json'
    )

    try:
        spawn_result = json.loads(result)
        if spawn_result.get("status") != "started":
            print(f"  SPAWN FAILED: {role_name.capitalize()} - {spawn_result.get('error', 'unknown')}")
            return False
    except Exception as e:
        print(f"  SPAWN FAILED: {role_name.capitalize()} - parse error: {e}")
        return False

    # Update in-memory state only - iteration writes state file once at end
    if "support_roles" not in state:
        state["support_roles"] = {}
    state["support_roles"][role_name] = {
        "status": "running",
        "tmux_session": f"loom-{session_name}",
        "started_at": now()
    }

    debug(f"State update: {role_name} marked running in-memory (tmux session: loom-{session_name})")
    print(f"  AUTO-SPAWNED: {role_name.capitalize()} (tmux:loom-{session_name})")
    return True
```

## Check Support Role Completions

Uses `agent-wait.sh` with `--timeout 0` (non-blocking) to check if tmux worker sessions
have completed. Updates in-memory state when support roles finish.

```python
def check_support_role_completions(state, debug_mode=False):
    """Check if any support roles have completed and update their in-memory state.

    Uses agent-wait.sh with --timeout 0 for non-blocking completion checks.
    All state updates happen in-memory only. The iteration subagent writes
    daemon-state.json once at the end of the iteration.
    """

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    completed_roles = []

    if "support_roles" not in state:
        return completed_roles

    now_iso = now()

    for role_name, role_info in state["support_roles"].items():
        # Skip roles that aren't running
        if role_info.get("status") != "running":
            continue

        tmux_session = role_info.get("tmux_session")
        if not tmux_session:
            continue

        session_name = tmux_session.replace("loom-", "")

        # Non-blocking check: does the tmux session still exist and is claude running?
        result = run(f'./.loom/scripts/agent-wait.sh "{session_name}" --timeout 0 --json 2>/dev/null || true')

        try:
            wait_result = json.loads(result) if result.strip() else {}
        except Exception:
            wait_result = {}

        status = wait_result.get("status")

        if status == "completed":
            # Agent finished - update in-memory state
            role_info["status"] = "idle"
            role_info["last_completed"] = now_iso
            role_info["tmux_session"] = None

            completed_roles.append(role_name)
            debug(f"{role_name.capitalize()} completed (session {tmux_session})")

            # Clean up the tmux session
            run(f'./.loom/scripts/agent-destroy.sh "{session_name}" --force 2>/dev/null || true')

        elif status == "not_found":
            # Session no longer exists - mark as completed
            role_info["status"] = "idle"
            role_info["last_completed"] = now_iso
            role_info["tmux_session"] = None

            completed_roles.append(role_name)
            debug(f"{role_name.capitalize()} session not found (already cleaned up)")

    return completed_roles
```

## Check Shepherd Completions

Uses `agent-wait.sh` with `--timeout 0` (non-blocking) to check if shepherd tmux sessions
have completed. Additionally cross-references shepherd progress files from
`snapshot_data["shepherds"]["progress"]` to detect completions where the shepherd's work
is done (progress file shows `status: completed`) but the tmux session is still alive
(Claude CLI sitting at idle prompt).

```python
def check_shepherd_completions(state, snapshot_data, debug_mode=False):
    """Check if any shepherds have completed and update their in-memory state.

    Uses two completion signals:
    1. agent-wait.sh: Detects when Claude CLI exits or tmux session is destroyed
    2. Progress files: Detects when shepherd reports status=completed via
       report-milestone.sh, even if the tmux session remains alive

    Both signals trigger the same completion handling: mark shepherd idle,
    destroy tmux session, clean up progress file.
    """

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    completed = []

    if "shepherds" not in state:
        return completed

    now_iso = now()

    # Build lookup of completed progress files by issue number
    completed_progress = {}
    for progress in snapshot_data.get("shepherds", {}).get("progress", []):
        if progress.get("status") == "completed":
            issue = progress.get("issue")
            if issue is not None:
                completed_progress[issue] = progress

    for shepherd_name, shepherd_info in state["shepherds"].items():
        # Skip shepherds that aren't working
        if shepherd_info.get("status") != "working":
            continue

        tmux_session = shepherd_info.get("tmux_session")
        issue = shepherd_info.get("issue")

        if not tmux_session:
            continue

        session_name = tmux_session.replace("loom-", "")

        # Signal 1: Non-blocking process-tree check via agent-wait.sh
        result = run(f'./.loom/scripts/agent-wait.sh "{session_name}" --timeout 0 --json 2>/dev/null || true')

        try:
            wait_result = json.loads(result) if result.strip() else {}
        except Exception:
            wait_result = {}

        status = wait_result.get("status")
        reason = None

        if status == "completed":
            reason = "process_exited"
        elif status == "not_found":
            reason = "session_destroyed"
        elif status == "timeout" and issue in completed_progress:
            # Signal 2: Progress file shows completed but tmux session still alive
            reason = "progress_completed"
            debug(f"Shepherd {shepherd_name}: progress file shows completed for #{issue}, tmux still alive")

        if reason:
            # Mark shepherd as idle in state
            shepherd_info["status"] = "idle"
            shepherd_info["last_completed"] = now_iso
            shepherd_info["idle_since"] = now_iso
            shepherd_info["idle_reason"] = "completed_issue"
            shepherd_info["last_issue"] = issue
            shepherd_info["issue"] = None
            shepherd_info["tmux_session"] = None

            completed.append({
                "name": shepherd_name,
                "issue": issue,
                "reason": reason
            })

            debug(f"Shepherd {shepherd_name} completed: issue=#{issue} reason={reason}")

            # Clean up the tmux session
            run(f'./.loom/scripts/agent-destroy.sh "{session_name}" --force 2>/dev/null || true')

            # Clean up progress file for this shepherd
            if issue in completed_progress:
                task_id = completed_progress[issue].get("task_id", "")
                if task_id:
                    progress_file = f".loom/progress/shepherd-{task_id}.json"
                    run(f'rm -f "{progress_file}" 2>/dev/null || true')
                    debug(f"Cleaned up progress file: {progress_file}")

    return completed
```

## Error Handling

### Stuck Agent Detection

```python
def check_stuck_agents(state, debug_mode=False):
    """Auto-detect stuck agents and trigger appropriate interventions."""

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    result = run("loom-stuck-detection check --json")

    if result.exit_code == 2:  # Stuck agents found
        stuck_data = json.loads(result.stdout)

        for agent_result in stuck_data["results"]:
            if agent_result["stuck"]:
                severity = agent_result["severity"]
                intervention = agent_result["suggested_intervention"]
                issue = agent_result["issue"]
                indicators = agent_result["indicators"]

                print(f"  STUCK: {agent_result['agent_id']} on #{issue}")
                debug(f"    Severity: {severity}")
                debug(f"    Indicators: {', '.join(indicators)}")
                debug(f"    Intervention: {intervention}")

        return stuck_data.get("stuck_count", 0)

    return 0
```

### Stale Building Detection

```python
def check_stale_building(state, debug_mode=False):
    """Detect and recover orphaned building issues."""

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    result = run("./.loom/scripts/stale-building-check.sh --recover --json")

    if result.exit_code == 0:
        data = json.loads(result.stdout)
        recovered = [i for i in data.get("stale_issues", []) if i["reason"] == "no_pr"]

        for issue in recovered:
            print(f"  RECOVERED: #{issue['number']} (stale {issue['age_hours']}h, no PR)")

        return len(recovered)

    return 0
```

### Systematic Failure Detection (LLM-Side)

When the pipeline is stalled with blocked issues but no ready work, the iteration subagent
inspects recent shepherd errors and blocked issue details for shared patterns. This is complex
pattern matching that is natural for the LLM but hard to express in shell.

```python
def detect_systematic_failure(snapshot_data, state, debug_mode=False):
    """Detect systematic failure patterns across recent shepherds.

    Analyzes blocked issues and recent shepherd errors for shared root causes.
    Returns a warning dict if a systematic pattern is detected, None otherwise.
    """

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    blocked_issues = snapshot_data.get("pipeline", {}).get("blocked_issues", [])
    if len(blocked_issues) < 2:
        return None  # Need at least 2 blocked issues for a pattern

    # Check recent shepherd completions in state for shared error patterns
    shepherds = state.get("shepherds", {})
    recent_errors = []
    for name, info in shepherds.items():
        if info.get("last_error"):
            recent_errors.append(info["last_error"])

    # Also check spawn_retry_queue for repeated failures
    retry_queue = state.get("spawn_retry_queue", {})
    for issue_key, retry_info in retry_queue.items():
        if retry_info.get("last_error"):
            recent_errors.append(retry_info["last_error"])

    if len(recent_errors) < 2:
        return None

    # Look for common error substring patterns (basic deduplication)
    # The LLM should analyze whether the errors share a root cause
    # such as: same test failure, same build error, same dependency issue
    debug(f"Analyzing {len(recent_errors)} recent errors for systematic pattern")
    debug(f"Blocked issues: {[i.get('number') for i in blocked_issues]}")
    debug(f"Recent errors: {recent_errors[:3]}")

    # Count distinct error patterns by taking first 50 chars as signature
    error_signatures = {}
    for err in recent_errors:
        sig = err[:50] if len(err) > 50 else err
        error_signatures[sig] = error_signatures.get(sig, 0) + 1

    # If any single error pattern appears in majority of errors, it's systematic
    for sig, count in error_signatures.items():
        if count >= 2 and count >= len(recent_errors) * 0.5:
            return {
                "code": "systematic_failure",
                "message": f"{count} shepherds failed with similar cause: {sig}"
            }

    return None


def format_state_warnings(health_warnings):
    """Convert health_warnings list to daemon-state.json warnings format.

    The daemon-state.json warnings array uses a different schema than the
    snapshot health_warnings. This function maps between the two formats.
    """
    state_warnings = []
    now_iso = now()

    for w in health_warnings:
        state_warnings.append({
            "time": now_iso,
            "type": w["code"],
            "severity": w["level"],
            "message": w["message"],
            "context": {},
            "acknowledged": False
        })

    return state_warnings
```

### Orphan Recovery (In-Session)

```python
def check_orphaned_shepherds(state, debug_mode=False):
    """Detect and recover orphaned shepherds from crashes mid-session.

    This runs every 5 iterations (not just at startup) to catch:
    - Shepherds with stale task IDs (tasks that no longer exist)
    - loom:building issues without active shepherds
    - Progress files with stale heartbeats
    """

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    debug("Running in-session orphan recovery check")

    result = run("./.loom/scripts/recover-orphaned-shepherds.sh --recover --json")

    if result.exit_code == 0:
        try:
            data = json.loads(result.stdout)
            recovered_issues = data.get("recovered_issues", [])
            recovered_shepherds = data.get("recovered_shepherds", [])

            for issue in recovered_issues:
                print(f"  RECOVERED: #{issue} (orphaned - no active shepherd)")

            for shepherd_id in recovered_shepherds:
                debug(f"Reset orphaned shepherd: {shepherd_id}")

            return len(recovered_issues)
        except Exception as e:
            debug(f"Failed to parse orphan recovery result: {e}")
            return 0

    return 0
```

## Iteration State Handling

The iteration subagent reads and writes state atomically, with session ID validation:

```python
# Read state at start (capture session ID for later validation)
state = json.load(open(".loom/daemon-state.json"))
our_session_id = state.get("daemon_session_id")

# ... do all iteration work ...

# Validate session ownership before writing (dual-daemon prevention)
if our_session_id:
    current = json.load(open(".loom/daemon-state.json"))
    if current.get("daemon_session_id") != our_session_id:
        # Another daemon has taken over - do NOT write
        print(f"SESSION CONFLICT: refusing to write state (our={our_session_id}, file={current.get('daemon_session_id')})")
        return "SESSION_CONFLICT"

# Write state at end (atomic)
with open(".loom/daemon-state.json.tmp", "w") as f:
    json.dump(state, f, indent=2)
os.rename(".loom/daemon-state.json.tmp", ".loom/daemon-state.json")
```

**Important:** All context-heavy operations (gh commands, tmux worker management) happen ONLY in iteration mode.

## Empty Backlog Handling

When backlog is empty, the iteration triggers work generation:

```python
def handle_empty_backlog(state, debug_mode):
    """Automatic response to empty backlog."""

    if ready_issues == 0:
        print("  Backlog empty - checking work generation triggers...")

        # Work generation is handled by acting on recommended_actions
        # loom-tools snapshot includes trigger_architect/trigger_hermit when appropriate

        # Report what human can do (informational only)
        if curated_count > 0:
            print(f"  Human action available: Approve {curated_count} curated issues")
        if proposal_count > 0:
            print(f"  Human action available: Approve {proposal_count} proposals")
```

## Commands for Iteration Mode

| Command | Description |
|---------|-------------|
| `/loom iterate` | Execute single iteration (used by parent loop) |
| `/loom iterate --merge` | Single iteration with merge mode |
| `/loom iterate --debug` | Single iteration with verbose debug logging |

### Command Detection

```python
args = "$ARGUMENTS".strip().split()

if "iterate" in args:
    force_mode = "--merge" in args or "--force" in args  # --force is deprecated alias
    debug_mode = "--debug" in args
    summary = loom_iterate(force_mode, debug_mode)
    print(summary)  # This is what parent receives
```

## Debug Mode Output

When running with `--debug`, iteration produces verbose logging:

```
[DEBUG] Iteration 5 starting at 2026-01-25T10:30:00Z
[DEBUG] Pipeline state: ready=3 building=1 review_requested=2
[DEBUG] Shepherd pool: shepherd-issue-123=working shepherd-issue-200=working
[DEBUG] Issue selection: Claiming #456 (prior failures=0)
[DEBUG] Spawning tmux shepherd: shepherd-issue-456 for issue #456
[DEBUG] Spawning decision: shepherd assigned to #456 (verified)
[DEBUG]   tmux session: loom-shepherd-issue-456
[DEBUG] Shepherd mode: --merge (force_mode=true)
[DEBUG] Checking support roles via spawn-support-role.sh (interval-based)
[DEBUG] guide: spawn needed (interval_elapsed)
[DEBUG] State update: guide marked running in-memory (tmux session: loom-guide)
[DEBUG] Iteration 5 completed
```

**Observability**: Attach to any worker while running:
```bash
tmux -L loom attach -t loom-shepherd-issue-456
tmux -L loom attach -t loom-guide
```
