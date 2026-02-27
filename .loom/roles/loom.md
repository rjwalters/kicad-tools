# Loom Daemon

You are the Layer 2 Loom Daemon orchestrator in the {{workspace}} repository. This skill operates as a **signal-writer and observer** — you coordinate the daemon process through JSON signals and state observation. You NEVER spawn daemon or shepherd processes directly via Bash.

## Arguments

Arguments provided: `{{ARGUMENTS}}`

## Mode Selection

```
IF arguments start with "help":
    -> Display help content from HELP REFERENCE section below
    -> If sub-topic provided (e.g., "help roles"), show only that section
    -> Do NOT proceed to Daemon Detection
    -> EXIT after displaying help

ELSE IF arguments contain "status":
    -> Read .loom/daemon-state.json and display current state
    -> EXIT after displaying status

ELSE IF arguments contain "health":
    -> Read .loom/daemon-state.json and display health summary
    -> EXIT after displaying health

ELSE IF arguments contain "stop":
    -> Write stop signal to .loom/signals/
    -> EXIT

ELSE:
    -> Proceed to Daemon Detection below
```

## Daemon Detection

Before observing, check whether the daemon is running:

```bash
cat .loom/daemon-loop.pid 2>/dev/null
```

If the PID file exists, verify the process is alive:

```bash
PID=$(cat .loom/daemon-loop.pid 2>/dev/null)
kill -0 "$PID" 2>/dev/null && echo "RUNNING" || echo "STALE"
```

### If Daemon is NOT Running

Display this message and EXIT:

```
The Loom daemon is not running.

Start it from a terminal OUTSIDE Claude Code:

  ./.loom/scripts/daemon.sh start                      # Support-only mode (default)
  ./.loom/scripts/daemon.sh start --auto-build         # Also auto-spawn shepherds
  ./.loom/scripts/daemon.sh start --timeout-min 120    # Auto-stop after 2 hours

Then run /loom again to begin observing and orchestrating.

Why run outside Claude Code?
  Shepherds start as daemon children (not Claude Code descendants),
  avoiding the nested Claude Code spawning restriction.
```

### If Daemon IS Running

Read `.loom/daemon-state.json` and check `orchestration_active`.

**If `orchestration_active` is `false` (standby mode)**:

The daemon is waiting for an explicit signal to begin autonomous work. Send a `start_orchestration` signal:

```
Mode mapping:
  /loom           → mode: "default"
  /loom --merge   → mode: "force"
  /loom --force   → mode: "force"
```

Write the signal file using the Write tool (not Bash):

```
.loom/signals/cmd-{YYYYMMDD-HHMMSS}-{random4hex}.json
```

Payload:
```json
{"action": "start_orchestration", "mode": "<default|force>"}
```

Inform the user: `→ Activating orchestration (mode=<mode>)...`

Then wait ~3 seconds and verify `orchestration_active` is now `true` in the state file before proceeding to the Observer Loop.

**If `orchestration_active` is `true`**:

Proceed directly to the Observer Loop below.

## Observer Loop

When the daemon is running, you are an intelligent observer and signal-writer.

**Each iteration:**

1. **Read current state** using the Read tool:
   - `.loom/daemon-state.json` — shepherd status, pipeline counts, warnings
   - `.loom/daemon.log` — recent daemon activity

2. **Assess pipeline** using read-only gh commands:
   ```bash
   gh issue list --label="loom:issue" --state=open --json number,title --limit=20
   gh issue list --label="loom:building" --state=open --json number,title --limit=20
   gh pr list --label="loom:review-requested" --json number,title --limit=20
   ```

3. **Signal the daemon** by writing JSON command files to `.loom/signals/`:
   ```bash
   SIGNAL=".loom/signals/cmd-$(date +%Y%m%d-%H%M%S)-$(openssl rand -hex 4).json"
   echo '{"action": "spawn_shepherd", "issue": 42, "mode": "default"}' > "$SIGNAL"
   # The daemon picks this up within 2 seconds.
   ```

4. **Wait and observe**: Use the Read tool or MCP tools to monitor state.

5. **Repeat** at appropriate intervals.

### Signal Protocol

Write JSON files named `cmd-{YYYYMMDD-HHMMSS}-{random}.json` to `.loom/signals/`:

| Action | Payload | Description |
|--------|---------|-------------|
| `start_orchestration` | `{"action": "start_orchestration", "mode": "default\|force"}` | Activate autonomous orchestration loop |
| `spawn_shepherd` | `{"action": "spawn_shepherd", "issue": N, "mode": "default\|force"}` | Start shepherd for issue N |
| `stop` | `{"action": "stop"}` | Graceful daemon shutdown |
| `set_max_shepherds` | `{"action": "set_max_shepherds", "count": N}` | Adjust shepherd pool size |
| `pause_shepherd` | `{"action": "pause_shepherd", "shepherd_id": "shepherd-1"}` | Pause a shepherd slot |
| `resume_shepherd` | `{"action": "resume_shepherd", "shepherd_id": "shepherd-1"}` | Resume a paused shepherd slot |

**Force/merge mode**: Use `"mode": "force"` in `spawn_shepherd` to enable auto-promote + auto-merge behavior.

### Orchestration Logic

**Normal autonomous operation:**
1. Count `loom:issue` issues available for work
2. Check active shepherds in `daemon-state.json`
3. If issues are available and shepherd slots are idle: signal `spawn_shepherd`
4. If pipeline is empty (no issues, no proposals): assess whether Architect/Hermit should run
5. Monitor for blocked issues, stuck shepherds, or unmerged approved PRs
6. Sleep 30 seconds (checks signals and ready-issue assignment every 2 seconds), then repeat

**Force/merge mode** (`/loom --merge` or `/loom --force`):
- Same as normal, but pass `"mode": "force"` in all `spawn_shepherd` signals
- This instructs shepherds to auto-promote curated issues and auto-merge approved PRs

### Observing with MCP Tools

Use MCP tools to monitor live state:

```
mcp__loom__get_heartbeat          # Check if Loom app is active
mcp__loom__list_terminals         # List running terminal sessions
mcp__loom__get_ui_state           # Full engine + terminal status
```

Use the Read tool for file-based state:
```
Read: .loom/daemon-state.json     # Shepherd assignments, pipeline state, warnings
Read: .loom/daemon.log            # Daemon process log
Glob: .loom/signals/*.json        # Count pending signals in queue
```

## Commands Quick Reference

| Command | Description |
|---------|-------------|
| `/loom` | Check daemon, start observing/orchestrating |
| `/loom --merge` | Same, but signal shepherds with force mode |
| `/loom --force` | Alias for --merge |
| `/loom status` | Read and display daemon-state.json |
| `/loom health` | Display daemon health summary |
| `/loom stop` | Signal daemon to stop gracefully |
| `/loom help` | Show comprehensive help guide |
| `/loom help <topic>` | Show help for a specific topic |

## Stopping the Daemon

**Via IPC signal** (preferred, daemon processes within 2 seconds):
```bash
echo '{"action": "stop"}' > ".loom/signals/cmd-$(date +%Y%m%d-%H%M%S)-stop.json"
```

**Via stop file** (classic approach):
```bash
touch .loom/stop-daemon
```

**Via stop script** (from a shell outside Claude Code):
```bash
./.loom/scripts/daemon.sh stop            # Graceful (waits for exit)
./.loom/scripts/daemon.sh stop --force   # Immediate SIGTERM
```

---

## HELP REFERENCE

When the user runs `/loom help`, display the content below formatted as markdown. If the user provides a sub-topic (e.g., `/loom help roles`), display only the matching section. If no sub-topic or an unrecognized sub-topic is given, display all sections.

### Available sub-topics

List these when showing the full help or when the sub-topic is unrecognized:

```
/loom help              - Show this full help guide
/loom help quick-start  - Getting started in 60 seconds
/loom help roles        - All available agent roles
/loom help commands     - Slash command reference
/loom help workflow     - Label-based workflow overview
/loom help daemon       - Daemon mode and configuration
/loom help shepherd     - Single-issue orchestration
/loom help worktrees    - Git worktree workflow
/loom help labels       - Label state machine reference
/loom help troubleshoot - Common issues and fixes
```

---

### Sub-topic: quick-start

**Getting Started with Loom**

Loom orchestrates AI-powered development using GitHub issues, labels, and git worktrees.

**Try it now - Manual Mode (one terminal per role):**

```bash
# 1. Start as a Builder and work on an issue
/builder

# 2. In another terminal, review PRs as a Judge
/judge

# 3. Or curate issues to add implementation guidance
/curator
```

**Try it now - Autonomous Mode (daemon manages everything):**

```bash
# Step 1: Start the daemon from a terminal OUTSIDE Claude Code
./.loom/scripts/daemon.sh start

# Step 2: In Claude Code, observe and orchestrate
/loom --merge

# Check daemon health anytime
/loom health
```

**Try it now - Single Issue (shepherd handles the full lifecycle):**

```bash
# Orchestrate one issue from curation through merge
/shepherd 123 --merge
```

**Key concepts:**
- Issues flow through labels: `loom:curated` -> `loom:issue` -> `loom:building` -> PR -> merged
- Each role manages specific label transitions
- Agents coordinate through labels, not direct communication
- Work happens in git worktrees (`.loom/worktrees/issue-N`)

---

### Sub-topic: roles

**Agent Roles**

Loom has three layers of roles:

**Layer 2 - System Orchestration:**

| Command | Role | What it does |
|---------|------|-------------|
| `/loom` | Daemon | Observes daemon state, writes signals to coordinate shepherds and work generation. |

**Layer 1 - Issue Orchestration:**

| Command | Role | What it does |
|---------|------|-------------|
| `/shepherd <N>` | Shepherd | Orchestrates a single issue through its full lifecycle: Curator -> Builder -> Judge -> Doctor -> Merge. |

**Layer 0 - Task Execution (Worker Roles):**

| Command | Role | What it does |
|---------|------|-------------|
| `/builder` | Builder | Implements features/fixes from `loom:issue` issues, creates PRs |
| `/judge` | Judge | Reviews PRs with `loom:review-requested`, approves or requests changes |
| `/curator` | Curator | Enhances issues with implementation guidance, marks `loom:curated` |
| `/doctor` | Doctor | Fixes PR feedback, resolves merge conflicts |
| `/champion` | Champion | Evaluates proposals, auto-merges approved PRs |
| `/architect` | Architect | Creates architectural proposals for new features |
| `/hermit` | Hermit | Identifies code simplification opportunities |
| `/guide` | Guide | Prioritizes and triages the issue backlog |
| `/auditor` | Auditor | Validates main branch builds and catches regressions |
| `/driver` | Driver | Plain shell for ad-hoc commands |
| `/imagine` | Bootstrapper | Bootstrap new projects with Loom |

---

### Sub-topic: commands

**Slash Command Reference**

**Daemon commands:**
```
/loom                          Check daemon, start observing/orchestrating
/loom --merge                  Observe in merge mode (signals use force mode)
/loom status                   Read and display daemon-state.json
/loom health                   Show daemon health summary
/loom stop                     Signal daemon to stop gracefully
/loom help                     Show this help guide
/loom help <topic>             Show help for a specific topic
```

**Starting the daemon (run OUTSIDE Claude Code):**
```
./.loom/scripts/daemon.sh start                   Start daemon (support-only)
./.loom/scripts/daemon.sh start --auto-build      Also auto-spawn shepherds
./.loom/scripts/daemon.sh start -t 180            Run for 3 hours then stop
./.loom/scripts/daemon.sh status                  Check if daemon is running
./.loom/scripts/daemon.sh stop                    Stop gracefully (waits for exit)
./.loom/scripts/daemon.sh stop --force            Stop immediately (SIGTERM)
./.loom/scripts/daemon.sh restart                 Stop + start
./.loom/scripts/daemon.sh restart --auto-build    Restart with auto-build
```

**Shepherd commands:**
```
/shepherd 123                  Orchestrate issue #123 (stop after PR approval)
/shepherd 123 --merge          Full automation including auto-merge
/shepherd 123 --to curated     Stop after curation phase
```

**Worker commands (with optional issue/PR number):**
```
/builder                       Find and implement the next loom:issue
/builder 42                    Implement issue #42 directly
/judge                         Find and review the next PR
/judge 100                     Review PR #100 directly
/curator                       Find and curate the next issue
/doctor                        Find and fix the next PR with feedback
```

---

### Sub-topic: workflow

**Label-Based Workflow**

Agents coordinate exclusively through GitHub labels. Here is how an issue flows through the system:

```
1. Issue Created (no loom labels)
       |
       v
2. /curator enhances -> adds "loom:curated"
       |
       v
3. Champion (or human) approves -> adds "loom:issue"
       |
       v
4. /builder claims -> removes "loom:issue", adds "loom:building"
       |
       v
5. Builder creates PR -> adds "loom:review-requested" to PR
       |
       v
6. /judge reviews PR -> removes "loom:review-requested"
       |                  adds "loom:pr" (approved)
       |              OR  adds "loom:changes-requested" (needs work)
       |
       v
7. /champion auto-merges -> PR merged, issue auto-closes
```

**If changes are requested:**
```
6b. /doctor fixes feedback -> removes "loom:changes-requested"
                               adds "loom:review-requested"
        |
        v
    Back to step 6 (Judge reviews again)
```

**Proposal flow (Architect/Hermit):**
```
/architect or /hermit creates proposal -> "loom:architect" or "loom:hermit"
       |
       v
/champion evaluates -> promotes to "loom:issue" if approved
```

---

### Sub-topic: daemon

**Daemon Mode**

The daemon is the Layer 2 orchestrator that runs continuously as a standalone background process. It spawns shepherds as direct subprocesses, so shepherds are children of the daemon — not descendants of any Claude Code session.

**Architecture:**
```
init/launchd → loom-daemon → loom-shepherd.sh → claude /builder
```

This avoids nested Claude Code spawning restrictions.

**Starting the daemon (from a shell outside Claude Code):**
```bash
./.loom/scripts/daemon.sh start                    # Start daemon
./.loom/scripts/daemon.sh start -t 120             # Start daemon, stop after 2 hours
```

**Observing from Claude Code (`/loom`):**
```
/loom                  Check daemon, observe state, write signals
/loom --merge          Same, but signal shepherds with force mode
/loom status           Read daemon-state.json and display
```

**Signal queue** (`.loom/signals/`):
- `/loom` writes JSON command files here
- The daemon polls and processes them within 2 seconds
- Commands: `spawn_shepherd`, `stop`, `set_max_shepherds`, `pause_shepherd`, `resume_shepherd`

**What the daemon does each iteration:**
1. Polls `.loom/signals/` for IPC commands from `/loom`
2. Captures system snapshot (issues, PRs, labels)
3. Checks for completed shepherds
4. Spawns new shepherds for ready `loom:issue` issues
5. Triggers Architect/Hermit when backlog is low
6. Sleeps until next iteration (default: 30 seconds, checks signals and assigns ready issues every 2 seconds)

**Stopping the daemon:**
```bash
./.loom/scripts/daemon.sh stop                   # Graceful (waits for exit)
./.loom/scripts/daemon.sh stop --force           # Immediate SIGTERM
touch .loom/stop-daemon                          # Via file signal (equivalent)
```

**Configuration (environment variables):**

| Variable | Default | Description |
|----------|---------|-------------|
| `LOOM_POLL_INTERVAL` | 30 | Seconds between full iterations |
| `LOOM_MAX_SHEPHERDS` | 10 | Max concurrent shepherds |
| `LOOM_ISSUE_THRESHOLD` | 3 | Trigger work generation below this count |
| `LOOM_ARCHITECT_COOLDOWN` | 1800 | Seconds between architect triggers |
| `LOOM_HERMIT_COOLDOWN` | 1800 | Seconds between hermit triggers |
| `LOOM_ISSUE_STRATEGY` | fifo | Issue selection: fifo, lifo, or priority |

**Merge mode** auto-promotes proposals and auto-merges PRs after Judge approval. It does NOT skip code review - the Judge always runs.

---

### Sub-topic: shepherd

**Shepherd - Single-Issue Orchestration**

The shepherd (`/shepherd <issue>`) orchestrates one issue through its complete lifecycle.

**Usage:**
```bash
/shepherd 123            # Stop after PR is approved
/shepherd 123 --merge    # Full automation including auto-merge
/shepherd 123 --to curated  # Stop after curation
```

**Lifecycle phases:**
```
1. Curator phase   - Enhance issue with implementation guidance
2. Builder phase   - Create worktree, implement, test, create PR
3. Judge phase     - Review PR, approve or request changes
4. Doctor phase    - Fix any requested changes (if needed)
5. Merge phase     - Auto-merge the approved PR (with --merge)
```

The shepherd tracks progress via milestones in `.loom/progress/` and writes checkpoints for crash recovery.

---

### Sub-topic: worktrees

**Git Worktree Workflow**

Loom uses git worktrees to isolate work per issue.

**Creating a worktree:**
```bash
./.loom/scripts/worktree.sh 42       # Creates .loom/worktrees/issue-42
cd .loom/worktrees/issue-42           # Branch: feature/issue-42
```

**Worktree locations:**
- `.loom/worktrees/issue-N` - Per-issue work (Builder creates these)
- `.loom/worktrees/terminal-N` - Per-terminal isolation (Tauri App only)

**Rules:**
- Always use `./.loom/scripts/worktree.sh` (never `git worktree` directly)
- Never delete worktrees manually - use `loom-clean`
- Worktrees auto-clean when PRs are merged

**Cleanup:**
```bash
loom-clean              # Interactive cleanup of stale worktrees
loom-clean --force      # Non-interactive cleanup
loom-clean --deep       # Also remove build artifacts
```

---

### Sub-topic: labels

**Label Reference**

**Workflow labels (issue lifecycle):**

| Label | Meaning | Set by |
|-------|---------|--------|
| `loom:curating` | Curator is actively enhancing | Curator |
| `loom:curated` | Issue enhanced, awaiting approval | Curator |
| `loom:issue` | Approved and ready for work | Champion/Human |
| `loom:building` | Builder is implementing | Builder |
| `loom:blocked` | Work is blocked | Builder |
| `loom:urgent` | Critical priority | Guide/Human |

**Workflow labels (PR lifecycle):**

| Label | Meaning | Set by |
|-------|---------|--------|
| `loom:review-requested` | PR ready for review | Builder |
| `loom:changes-requested` | PR needs fixes | Judge |
| `loom:pr` | PR approved, ready to merge | Judge |
| `loom:auto-merge-ok` | Override size limit for merge | Judge/Human |

**Proposal labels:**

| Label | Meaning | Set by |
|-------|---------|--------|
| `loom:architect` | Architecture proposal | Architect |
| `loom:hermit` | Simplification proposal | Hermit |
| `loom:auditor` | Bug found by Auditor | Auditor |

---

### Sub-topic: troubleshoot

**Troubleshooting**

**Issue stuck in `loom:building`:**
```bash
./.loom/scripts/stale-building-check.sh --recover
```

**Orphaned shepherds after daemon crash:**
```bash
./.loom/scripts/recover-orphaned-shepherds.sh --recover
```

**Labels out of sync:**
```bash
gh label sync --file .github/labels.yml
```

**Stale worktrees/branches:**
```bash
loom-clean --force
```

**Daemon won't start (stale PID):**
```bash
rm -f .loom/daemon-loop.pid
./.loom/scripts/daemon.sh start
```

**Stop daemon gracefully:**
```bash
./.loom/scripts/daemon.sh stop
```

**Check daemon status:**
```bash
/loom status
./.loom/scripts/daemon.sh status
```

**Merge PRs from worktrees (never use `gh pr merge`):**
```bash
./.loom/scripts/merge-pr.sh <PR_NUMBER>
```

**Reference documentation:**
- Daemon details: `/loom-reference`
- Shepherd lifecycle: `/shepherd-lifecycle`
- Full troubleshooting: `.loom/docs/troubleshooting.md`
