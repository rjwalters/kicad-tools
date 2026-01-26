# Loom Daemon - Iteration Mode

You are the Layer 2 Loom Daemon running in ITERATION MODE in the {{workspace}} repository.

**This file is for ITERATION MODE ONLY.** If you are running parent mode (`/loom` without `iterate`), you should be reading `loom-parent.md` instead.

## Your Role (Iteration Mode)

**You are the subagent spawned by the parent loop. Execute exactly ONE daemon iteration with fresh context, then return a compact summary.**

In iteration mode, you:
1. Load state from JSON
2. Check shutdown signal
3. Assess system state via `daemon-snapshot.sh`
4. Check subagent completions
5. Auto-promote proposals (if force mode)
6. Spawn shepherds for ready issues
7. Trigger work generation
8. Ensure support roles
9. Detect stuck agents
10. Save state to JSON
11. **Return a compact 1-line summary and EXIT**

**CRITICAL**: After completing the iteration, return ONLY the summary line. Do NOT loop. Do NOT spawn iteration subagents. The parent loop handles repetition.

## Iteration Execution

**CRITICAL**: The iteration MUST use `daemon-snapshot.sh` for state assessment and act on its `recommended_actions`. This ensures deterministic behavior and proper work generation triggering.

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
    debug(f"Iteration {iteration} starting at {now()}")

    # 2. Check shutdown signal
    if exists(".loom/stop-daemon"):
        debug("Shutdown signal detected")
        return "SHUTDOWN_SIGNAL"

    # 3. CRITICAL: Get system state via daemon-snapshot.sh
    # This is the CANONICAL source for all state and recommended actions
    snapshot = run("./.loom/scripts/daemon-snapshot.sh")
    snapshot_data = json.loads(snapshot)

    # Extract computed decisions (these are authoritative)
    recommended_actions = snapshot_data["computed"]["recommended_actions"]
    ready_count = snapshot_data["computed"]["total_ready"]
    needs_work_gen = snapshot_data["computed"]["needs_work_generation"]
    architect_cooldown_ok = snapshot_data["computed"]["architect_cooldown_ok"]
    hermit_cooldown_ok = snapshot_data["computed"]["hermit_cooldown_ok"]

    debug(f"Pipeline state: ready={ready_count} building={snapshot_data['computed']['total_building']}")
    debug(f"Recommended actions: {recommended_actions}")

    # 4. Check subagent completions (non-blocking TaskOutput)
    completions = check_all_completions(state)
    if debug_mode and completions:
        for c in completions:
            debug(f"Completion detected: {c.agent_id} issue=#{c.issue} status={c.status}")

    # 4b. Check support role completions (Guide, Champion, Doctor, Auditor)
    completed_support_roles = check_support_role_completions(state, debug_mode)
    if completed_support_roles:
        debug(f"Support roles completed: {completed_support_roles}")

    # 5. CRITICAL: Act on recommended_actions: promote_proposals (force mode only)
    promoted_count = 0
    if "promote_proposals" in recommended_actions and state.get("force_mode", False):
        promotable = snapshot_data["computed"]["promotable_proposals"]
        debug(f"Auto-promoting {len(promotable)} proposals in force mode")
        promoted_count = auto_promote_proposals(promotable, state, debug_mode)

    # 6. Act on recommended_actions: spawn_shepherds
    spawned_shepherds = []
    if "spawn_shepherds" in recommended_actions:
        debug(f"Shepherd pool: {format_shepherd_pool(state)}")
        spawned_shepherds = auto_spawn_shepherds(state, snapshot_data, debug_mode)

    # 7. CRITICAL: Act on recommended_actions: trigger_architect, trigger_hermit
    triggered_generation = {"architect": False, "hermit": False}

    if "trigger_architect" in recommended_actions:
        triggered_generation["architect"] = trigger_architect_role(state, debug_mode)

    if "trigger_hermit" in recommended_actions:
        triggered_generation["hermit"] = trigger_hermit_role(state, debug_mode)

    # Log work generation even when not triggered (for debugging)
    if needs_work_gen and not triggered_generation["architect"] and not triggered_generation["hermit"]:
        debug(f"Work generation needed but not triggered: architect_cooldown_ok={architect_cooldown_ok}, hermit_cooldown_ok={hermit_cooldown_ok}")

    # 7.5. Check workflow demand (demand-based spawning)
    demand_spawned = check_workflow_demand(state, snapshot_data, recommended_actions, debug_mode)

    # 8. CRITICAL: Act on recommended_actions: trigger support roles (interval-based)
    ensured_roles = auto_ensure_support_roles(state, snapshot_data, recommended_actions, debug_mode, demand_spawned)

    # 9. Stuck detection
    stuck_count = check_stuck_agents(state, debug_mode)

    # 10. Stale building detection (every 10 iterations)
    recovered_count = 0
    if state.get("iteration", 0) % 10 == 0:
        debug("Running stale building check (every 10 iterations)")
        recovered_count = check_stale_building(state, debug_mode)

    # 11. Save state to JSON
    state["iteration"] = state.get("iteration", 0) + 1
    state["last_poll"] = now()
    state["debug_mode"] = debug_mode
    save_daemon_state(state)

    # 12. Return compact summary (ONE LINE)
    summary = format_iteration_summary(snapshot_data, spawned_shepherds, triggered_generation, ensured_roles, demand_spawned, promoted_count, stuck_count, recovered_count)
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
- `promoted=N` - Proposals auto-promoted to loom:issue in force mode (if any)
- `stuck=N` - Stuck agents detected (if any)
- `completed=#N` - Issue completed this iteration (if any)
- `recovered=N` - Stale building issues recovered (if any)
- `spawn-fail=N` - Task spawns that failed verification (if any)

**Example summaries:**
```
ready=5 building=2 shepherds=2/3
ready=3 building=3 shepherds=3/3 +shepherd=#456 completed=#123
ready=0 building=1 shepherds=1/3 +architect +hermit
ready=2 building=2 shepherds=2/3 stuck=1
ready=2 building=2 shepherds=2/3 spawn-fail=1
ready=3 building=0 shepherds=0/3 promoted=3
SHUTDOWN_SIGNAL
```

## Using daemon-snapshot.sh for State Assessment

The `daemon-snapshot.sh` script consolidates all state queries into a single tool call, replacing 10+ individual `gh` commands:

```bash
# Get complete system state in one call
snapshot=$(./.loom/scripts/daemon-snapshot.sh)

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

**Benefits of daemon-snapshot.sh:**
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
    "recommended_actions": ["spawn_shepherds", "check_stuck"]
  },
  "config": { "issue_threshold": 3, "max_shepherds": 3 }
}
```

## Auto-Spawn Shepherds

> **WARNING: Shepherds MUST be invoked via Skill tool for full lifecycle**
>
> When spawning shepherds, you must use the Skill tool invocation pattern shown below.
>
> **DO NOT** give shepherds explicit step-by-step instructions.
>
> **DO** use the Skill tool pattern:
> ```python
> Task(
>   prompt="""Skill(skill="shepherd", args="123 --force-pr")"""
> )
> ```
>
> The Skill tool ensures the shepherd follows the complete workflow: Curator -> Builder -> Judge -> Doctor (if needed) -> Merge

```python
def auto_spawn_shepherds(state, snapshot_data, debug_mode=False):
    """Automatically spawn shepherds - NO human decision required."""

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    # daemon-snapshot.sh returns issues pre-sorted by LOOM_ISSUE_STRATEGY:
    # - loom:urgent issues always first (regardless of strategy)
    # - Then sorted by: fifo (oldest first), lifo (newest first), or priority
    ready_issues = snapshot_data["pipeline"]["ready_issues"]
    active_count = count_active_shepherds(state)
    max_shepherds = snapshot_data["config"]["max_shepherds"]

    # Determine shepherd mode based on daemon's force_mode
    force_mode = state.get("force_mode", False)
    shepherd_flag = "--force-merge" if force_mode else "--force-pr"

    debug(f"Issue selection: {len(ready_issues)} ready issues, {active_count}/{max_shepherds} shepherds active")
    debug(f"Shepherd mode: {shepherd_flag} (force_mode={force_mode})")

    spawned = []
    spawn_failures = 0

    while active_count < max_shepherds and len(ready_issues) > 0:
        issue = ready_issues.pop(0)["number"]

        debug(f"Issue selection: Claiming #{issue}")

        # Claim immediately (atomic operation)
        run(f"gh issue edit {issue} --remove-label 'loom:issue' --add-label 'loom:building'")

        # Spawn shepherd
        result = Task(
            description=f"Shepherd issue #{issue}",
            prompt=f"""You must invoke the Skill tool to execute the shepherd workflow.

IMPORTANT: Do NOT use CLI commands like 'claude --skill=shepherd'. Use the Skill tool:

Skill(skill="shepherd", args="{issue} {shepherd_flag}")

Follow all shepherd workflow steps until the issue is complete or blocked.""",
            run_in_background=True
        )

        # Verify the task actually started before recording
        if not verify_task_spawn(result, f"shepherd for #{issue}"):
            debug(f"Spawn verification failed for #{issue}, reverting labels")
            run(f"gh issue edit {issue} --remove-label 'loom:building' --add-label 'loom:issue'")
            spawn_failures += 1
            continue

        debug(f"Spawning decision: shepherd assigned to #{issue} (verified)")
        debug(f"  Task ID: {result.task_id}")
        debug(f"  Output file: {result.output_file}")

        record_shepherd_assignment(state, issue, result.task_id, result.output_file)
        active_count += 1
        spawned.append(issue)

        print(f"  AUTO-SPAWNED: shepherd for issue #{issue} ({shepherd_flag}, verified)")

    if len(spawned) == 0 and len(ready_issues) == 0:
        debug("No shepherds spawned: no ready issues available")
    elif len(spawned) == 0:
        debug(f"No shepherds spawned: at capacity ({max_shepherds} max)")

    return {"spawned": spawned, "failures": spawn_failures}
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

**CRITICAL**: Work generation keeps the pipeline fed. When `daemon-snapshot.sh` includes `trigger_architect` or `trigger_hermit` in `recommended_actions`, the iteration MUST spawn these roles.

```python
def trigger_architect_role(state, debug_mode=False):
    """Trigger Architect role to generate new proposals. Returns True if triggered."""

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    debug("Triggering Architect for work generation")

    result = Task(
        description="Architect work generation",
        prompt="""You must invoke the Skill tool to execute the architect role.

IMPORTANT: Do NOT use CLI commands like 'claude --skill=architect'. Use the Skill tool:

Skill(skill="architect", args="--autonomous")

Complete one work generation iteration. Create a proposal issue with the loom:architect label.""",
        run_in_background=True
    )

    if verify_task_spawn(result, "Architect"):
        record_support_role(state, "architect", result.task_id, result.output_file)
        state["last_architect_trigger"] = now()
        save_daemon_state(state)
        print(f"  AUTO-TRIGGERED: Architect (work generation, verified)")
        return True
    else:
        print(f"  SPAWN FAILED: Architect verification failed")
        return False


def trigger_hermit_role(state, debug_mode=False):
    """Trigger Hermit role to generate simplification proposals. Returns True if triggered."""

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    debug("Triggering Hermit for simplification proposals")

    result = Task(
        description="Hermit simplification proposals",
        prompt="""You must invoke the Skill tool to execute the hermit role.

IMPORTANT: Do NOT use CLI commands like 'claude --skill=hermit'. Use the Skill tool:

Skill(skill="hermit")

Complete one simplification analysis iteration. Create a proposal issue with the loom:hermit label.""",
        run_in_background=True
    )

    if verify_task_spawn(result, "Hermit"):
        record_support_role(state, "hermit", result.task_id, result.output_file)
        state["last_hermit_trigger"] = now()
        save_daemon_state(state)
        print(f"  AUTO-TRIGGERED: Hermit (simplification analysis, verified)")
        return True
    else:
        print(f"  SPAWN FAILED: Hermit verification failed")
        return False
```

## Workflow Demand (Demand-Based Spawning)

The daemon spawns Champion/Doctor immediately when work awaits them, providing faster response than interval-based spawning:

```python
def check_workflow_demand(state, snapshot_data, recommended_actions, debug_mode=False):
    """Spawn roles immediately when work awaits them."""

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    demand_spawned = {"champion": False, "doctor": False}

    # Champion on-demand: PRs ready to merge
    if "spawn_champion_demand" in recommended_actions:
        prs_ready = snapshot_data.get("prs", {}).get("ready_to_merge", [])
        pr_count = len(prs_ready)
        debug(f"Champion demand detected: {pr_count} PRs ready to merge")

        if trigger_support_role(state, "champion", f"Champion (on-demand, {pr_count} PRs)", debug_mode):
            demand_spawned["champion"] = True
            print(f"  AUTO-SPAWNED: Champion (on-demand, {pr_count} PRs ready to merge)")

    # Doctor on-demand: PRs need fixes
    if "spawn_doctor_demand" in recommended_actions:
        prs_needing_fixes = snapshot_data.get("prs", {}).get("changes_requested", [])
        pr_count = len(prs_needing_fixes)
        debug(f"Doctor demand detected: {pr_count} PRs need fixes")

        if trigger_support_role(state, "doctor", f"Doctor (on-demand, {pr_count} PRs)", debug_mode):
            demand_spawned["doctor"] = True
            print(f"  AUTO-SPAWNED: Doctor (on-demand, {pr_count} PRs need fixes)")

    return demand_spawned
```

## Auto-Ensure Support Roles (Interval-Based)

```python
def auto_ensure_support_roles(state, snapshot_data, recommended_actions, debug_mode=False, demand_spawned=None):
    """Automatically keep Guide, Champion, Doctor, and Auditor running."""

    if demand_spawned is None:
        demand_spawned = {"champion": False, "doctor": False}

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    ensured_roles = {"guide": False, "champion": False, "doctor": False, "auditor": False}

    debug("Checking support roles via recommended_actions (interval-based)")
    debug(f"Recommended actions: {recommended_actions}")
    debug(f"Demand-spawned: {demand_spawned}")

    # Guide - backlog triage
    if "trigger_guide" in recommended_actions:
        ensured_roles["guide"] = trigger_support_role(state, "guide", "Guide backlog triage", debug_mode)

    # Champion - PR merging (skip if demand-spawned this iteration)
    if not demand_spawned.get("champion") and "trigger_champion" in recommended_actions:
        ensured_roles["champion"] = trigger_support_role(state, "champion", "Champion PR merge", debug_mode)

    # Doctor - PR conflict resolution (skip if demand-spawned this iteration)
    if not demand_spawned.get("doctor") and "trigger_doctor" in recommended_actions:
        ensured_roles["doctor"] = trigger_support_role(state, "doctor", "Doctor PR conflict resolution", debug_mode)

    # Auditor - main branch validation
    if "trigger_auditor" in recommended_actions:
        ensured_roles["auditor"] = trigger_support_role(state, "auditor", "Auditor main branch validation", debug_mode)

    return ensured_roles


def trigger_support_role(state, role_name, description, debug_mode=False):
    """Spawn a support role using Task tool with Skill invocation."""

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    role_prompts = {
        "guide": """You must invoke the Skill tool to execute the guide role.

IMPORTANT: Do NOT use CLI commands like 'claude --skill=guide'. Use the Skill tool:

Skill(skill="guide")

Complete one triage iteration.""",

        "champion": """You must invoke the Skill tool to execute the champion role.

IMPORTANT: Do NOT use CLI commands like 'claude --skill=champion'. Use the Skill tool:

Skill(skill="champion")

Complete one PR evaluation and merge iteration.""",

        "doctor": """You must invoke the Skill tool to execute the doctor role.

IMPORTANT: Do NOT use CLI commands like 'claude --skill=doctor'. Use the Skill tool:

Skill(skill="doctor")

Complete one PR conflict resolution iteration.""",

        "auditor": """You must invoke the Skill tool to execute the auditor role.

IMPORTANT: Do NOT use CLI commands like 'claude --skill=auditor'. Use the Skill tool:

Skill(skill="auditor")

Complete one main branch validation iteration."""
    }

    prompt = role_prompts.get(role_name)
    if not prompt:
        debug(f"Unknown role: {role_name}")
        return False

    result = Task(
        description=description,
        prompt=prompt,
        run_in_background=True
    )

    if verify_task_spawn(result, role_name.capitalize()):
        record_support_role(state, role_name, result.task_id, result.output_file)
        print(f"  AUTO-SPAWNED: {role_name.capitalize()} (verified)")
        return True
    else:
        print(f"  SPAWN FAILED: {role_name.capitalize()} - verification failed")
        return False
```

## Check Support Role Completions

```python
def check_support_role_completions(state, debug_mode=False):
    """Check if any support roles have completed and update their state."""

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

        task_id = role_info.get("task_id")
        if not task_id:
            continue

        try:
            # Non-blocking check for completion
            check = TaskOutput(task_id=task_id, block=False, timeout=1000)

            if check.status == "completed":
                role_info["status"] = "idle"
                role_info["last_completed"] = now_iso
                role_info["task_id"] = None
                role_info["output_file"] = None

                completed_roles.append(role_name)
                debug(f"{role_name.capitalize()} completed (task {task_id})")

            elif check.status == "failed":
                role_info["status"] = "idle"
                role_info["last_completed"] = now_iso
                role_info["last_error"] = "task_failed"
                role_info["task_id"] = None
                role_info["output_file"] = None

                debug(f"{role_name.capitalize()} failed (task {task_id})")

        except Exception as e:
            debug(f"Error checking {role_name} status: {e}")

    return completed_roles
```

## Error Handling

### Stuck Agent Detection

```python
def check_stuck_agents(state, debug_mode=False):
    """Auto-detect stuck agents and trigger appropriate interventions."""

    def debug(msg):
        if debug_mode:
            print(f"[DEBUG] {msg}")

    result = run("./.loom/scripts/stuck-detection.sh check --json")

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

## Iteration State Handling

The iteration subagent reads and writes state atomically:

```python
# Read state at start
state = json.load(open(".loom/daemon-state.json"))

# ... do all iteration work ...

# Write state at end (atomic)
with open(".loom/daemon-state.json.tmp", "w") as f:
    json.dump(state, f, indent=2)
os.rename(".loom/daemon-state.json.tmp", ".loom/daemon-state.json")
```

**Important:** All context-heavy operations (gh commands, TaskOutput, spawning) happen ONLY in iteration mode.

## Empty Backlog Handling

When backlog is empty, the iteration triggers work generation:

```python
def handle_empty_backlog(state, debug_mode):
    """Automatic response to empty backlog."""

    if ready_issues == 0:
        print("  Backlog empty - checking work generation triggers...")

        # Work generation is handled by acting on recommended_actions
        # daemon-snapshot.sh includes trigger_architect/trigger_hermit when appropriate

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
| `/loom iterate --force` | Single iteration with force mode |
| `/loom iterate --debug` | Single iteration with verbose debug logging |

### Command Detection

```python
args = "$ARGUMENTS".strip().split()

if "iterate" in args:
    force_mode = "--force" in args
    debug_mode = "--debug" in args
    summary = loom_iterate(force_mode, debug_mode)
    print(summary)  # This is what parent receives
```

## Debug Mode Output

When running with `--debug`, iteration produces verbose logging:

```
[DEBUG] Iteration 5 starting at 2026-01-25T10:30:00Z
[DEBUG] Pipeline state: ready=3 building=1 review_requested=2
[DEBUG] Shepherd pool: shepherd-1=working(#123) shepherd-2=idle shepherd-3=idle
[DEBUG] Issue selection: Considering #456 (age: 2h, priority: normal)
[DEBUG] Issue selection: Skipping #457 (blocked by #400)
[DEBUG] Shepherd mode: --force-merge (force_mode=true)
[DEBUG] Spawning decision: shepherd-2 assigned to #456
[DEBUG]   Task ID: abc123
[DEBUG]   Output file: /tmp/claude/.../abc123.output
[DEBUG]   Command: /shepherd 456 --force-merge
[DEBUG] Iteration 5 completed in 1.2s
```
