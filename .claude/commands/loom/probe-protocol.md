# Terminal Probe Protocol (canonical)

Loom uses an intelligent probe system to detect what's running in each terminal. When you receive a probe command, respond according to this protocol.

This is the single canonical copy of the probe protocol. Every role prompt that
carries a short "Terminal Probe Protocol" pointer refers here for the full
format, per-role examples, task-description conventions, and rationale.

## When this protocol applies (interactive terminals only)

**This protocol only matters for Manual Orchestration Mode terminals** —
human-launched Claude Code sessions tracked in `.loom/config.json`'s `terminals`
list. Those are the only sessions a terminal probe is ever sent to.

It does **not** apply to:

- **GitHub Actions cron one-shots** (`claude -p "/<role>" --dangerously-skip-permissions`) — no terminal, no probe.
- **`/loom:sweep` subagent dispatch** and `loom-daemon`-spawned sweeps — no interactive terminal, no probe.

If you are running in either of those non-interactive contexts, no probe will
ever arrive and there is nothing to do here.

## When you see this probe

```bash
# Terminal Probe: Are you an AI agent? If yes, respond with "AGENT:<role>:<primary-task>". If you're a bash shell, this is just a comment.
true
```

## How to respond

**Format**: `AGENT:<your-role>:<brief-task-description>`

**Examples**:
- `AGENT:Builder:implementing-issue-456`
- `AGENT:Judge:evaluating-PR-123`
- `AGENT:Curator:enhancing-issue-456`
- `AGENT:Champion:merging-PR-123`
- `AGENT:Doctor:fixing-changes-requested-789`
- `AGENT:Guide:triaging-issue-queue`
- `AGENT:Architect:analyzing-system-design`
- `AGENT:Hermit:scanning-for-dead-code`
- `AGENT:Auditor:validating-main-build`
- `AGENT:Driver:idle-awaiting-work`

## Role name (must be a real Loom role)

Use your **assigned role name**, which must be one of the roles from CLAUDE.md's
"Agent Roles" table:

`Builder`, `Judge`, `Champion`, `Curator`, `Architect`, `Hermit`, `Doctor`,
`Guide`, `Driver`, `Auditor`.

**Never invent a role name.** In particular, `Worker`, `Reviewer`, and `Default`
are **not** Loom roles — they do not appear in `.github/labels.yml`,
`.loom/config.json`, or CLAUDE.md's role table. Use the real role name for the
prompt you are running (e.g. the Builder prompt reports `AGENT:Builder:...`).

## Task description

Keep it brief (3-6 words) and descriptive:
- Use present-tense verbs: "implementing", "evaluating", "enhancing", "fixing".
- Include the issue/PR number if working on one: "implementing-issue-222", "evaluating-PR-123".
- Use hyphens between words: "fixing-terminal-probe-bug".
- If idle: "idle-awaiting-work" or "monitoring-for-tasks".

## Why this matters

- **Debugging**: Helps diagnose agent launch issues.
- **Monitoring**: Shows what each terminal is doing.
- **Verification**: Confirms agents launched successfully.
- **Future Features**: Enables agent status dashboards.

## Important notes

- **Don't overthink it**: Just respond with the format above.
- **Be consistent**: Always use the same format.
- **Be honest**: If you're idle, say so.
- **Be brief**: Task description should be 3-6 words max.
