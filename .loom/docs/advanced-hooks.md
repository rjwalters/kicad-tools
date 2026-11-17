# Advanced Hooks

Opt-in `UserPromptSubmit` context injection (the Methodology Injection Framework)
and durable session-transcript archival — reference-tier detail an agent looks up
on demand.

## Methodology Injection Framework

Loom provides an opt-in methodology injection hook that automatically injects project-specific context into every agent session. This is useful for domain knowledge, coding conventions, design rules, or any context that agents need to do their job well.

### Quick Start

1. Create the context directory in your repository:
   ```bash
   mkdir -p .loom/context/roles .loom/context/topics
   ```

2. Add a universal context file (injected once per session by default):
   ```bash
   cat > .loom/context/universal.md << 'EOF'
   # Project Rules
   - Use TypeScript strict mode
   - All functions must have JSDoc comments
   - Run tests before creating PRs
   EOF
   ```

3. The hook is already registered in `.claude/settings.json`. It activates automatically when `.loom/context/` exists and silently does nothing when the directory is absent.

### Context File Structure

```
.loom/context/
├── config.json              # Optional configuration
├── universal.md             # Injected once per session by default
├── roles/
│   ├── builder.md           # Injected when LOOM_ROLE=builder
│   ├── judge.md             # Injected when LOOM_ROLE=judge
│   └── ...
└── topics/
    ├── security.md          # Injected when prompt matches "security"
    ├── security.pattern     # Optional: custom regex pattern for matching
    ├── database.md          # Injected when prompt matches "database"
    └── ...
```

**Universal context** (`universal.md`): Injected when the context directory exists — **once per session by default** (`universal_frequency: "session"`), so the project-wide rules ride along on the first prompt of a session and are deduped on subsequent turns. Set `universal_frequency: "always"` to restore per-prompt injection. Use for project-wide rules and conventions.

**Role context** (`roles/<role>.md`): Injected when the `LOOM_ROLE` environment variable matches the filename, or when a slash command (e.g., `/builder`) is detected in the prompt. Role names are case-insensitive.

**Topic context** (`topics/<name>.md`): Injected when the prompt matches the topic keyword. By default the filename is matched as an **anchored** token — the topic name must appear either as a slash command (`/loom:<name>` or `/repo:<name>`) or as a standalone word that is not part of a flag or path segment. So `security.md` injects on "check the security model" or `/loom:security`, but a "release" topic does **not** inject on `cargo build --release` or `target/release`. For custom matching, create a sidecar `.pattern` file with a regex (e.g., `security.pattern` containing `security|auth|token|credential`); the sidecar overrides the filename fallback entirely.

### Configuration

Create `.loom/context/config.json` to customize behavior:

```json
{
  "max_context_chars": 8000,
  "enabled": true,
  "inject_universal": true,
  "universal_frequency": "session",
  "inject_role": true,
  "inject_topics": true
}
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_context_chars` | 8000 | Maximum total characters injected (prevents overwhelming the context window) |
| `enabled` | true | Set to false to disable injection without removing files |
| `inject_universal` | true | Whether to inject `universal.md` at all (on/off master switch) |
| `universal_frequency` | `"session"` | How often `universal.md` is injected: `"session"` (once per session — default) or `"always"` (every matching prompt, legacy behavior). Any missing/malformed value falls back to `"session"`. |
| `inject_role` | true | Whether to inject role-specific context |
| `inject_topics` | true | Whether to inject topic-matched context |

> **Behavior-change note (#3758)**: `universal_frequency` defaults to `"session"`,
> a deliberate flip from the historical always-inject behavior. Any repo that
> already opted into `.loom/context/` **without** setting this key now gets
> `universal.md` **once per session** instead of on every prompt. This mirrors the
> precedent set by #3609 for `skill-router.sh` (which likewise dropped per-prompt
> injection with no back-compat shim). To keep the old every-prompt behavior, set
> `"universal_frequency": "always"`. The once-per-session dedup uses a session-keyed
> marker at `.loom/logs/methodology-inject-seen/<sanitized-session-id>` (its own
> namespace, parallel to `skill-router.sh`'s `skill-router-seen/`); a missing/empty
> `session_id` on stdin degrades gracefully to per-turn injection. Role and topic
> injection are unaffected — they still fire on every matching turn.

### How It Works

The `methodology-inject.sh` hook runs as a `UserPromptSubmit` hook alongside `skill-router.sh`. On each prompt:

1. Checks for `.loom/context/` directory -- exits silently if absent
2. Reads `universal.md` if present, **once per session** by default (`universal_frequency`) — deduped via a session-keyed marker, exactly like `skill-router.sh`'s #3609 routing-table dedup
3. Detects the active role via `LOOM_ROLE` env var or prompt slash command
4. Scans `topics/` files, matching prompt against filename or sidecar `.pattern` regex
5. Concatenates matching content, capped at `max_context_chars`
6. Returns the collected context as `additionalContext`

The hook follows the same error-handling patterns as other Loom hooks: it never exits non-zero, logs errors to `.loom/logs/hook-errors.log`, and fails silently on any unexpected error.

### UserPromptSubmit Hooks: Opt-In Triggers and Disabling

Both `UserPromptSubmit` hooks are **opt-in by config presence** and do nothing until you add their config. Each has a one-line off switch:

| Hook | Opt-in trigger | One-line disable |
|------|----------------|------------------|
| `methodology-inject.sh` | Presence of the `.loom/context/` directory | Delete/rename `.loom/context/`, or set `"enabled": false` in `.loom/context/config.json`, or remove the hook's entry from the `UserPromptSubmit` array in `.claude/settings.json` |
| `skill-router.sh` | Presence of `.loom/config/skill-routes.json` | Delete `.loom/config/skill-routes.json`, or remove the hook's entry from the `UserPromptSubmit` array in `.claude/settings.json` |

`skill-router.sh` is already conservative (issue #3609): it emits nothing on non-matching turns, and appends its agent routing table at most once per session (session-keyed dedup, degrading gracefully when `session_id` is missing). `methodology-inject.sh` now mirrors that once-per-session discipline for `universal.md` (see `universal_frequency` above). Neither hook blocks a prompt or exits non-zero; both fail silently on any error.

### Example Context Files

Example context files are provided in `defaults/hooks/example-context/` to guide setup. Copy them to your `.loom/context/` directory and customize:

```bash
cp -r defaults/hooks/example-context/* .loom/context/
```

### Session Transcript Archival (opt-in, #3726)

Claude Code writes a full JSONL transcript for every session — and a per-subagent
transcript for every Builder / Judge / Doctor Task — under
`${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/<cwd-slug>/`. These are the
ground-truth record of what each agent did and what it cost (per-message `usage`
+ `model`), but they live only on the local box and are subject to Claude Code's
own pruning. `archive-transcripts.sh` copies them to a durable location so a
multi-day canary run can be audited / cost-harvested after the fact (serves the
#3725 per-role cost harvest — the archived index is its `agent-<id>` join key).

**Off by default. Zero behavior change unless you turn it on.** Enable via env or
config (env-over-config precedence, matching the `guards.rmScope` string pattern):

```bash
# env wins over config; a path enables, ""/off/0/no/disabled forces off:
LOOM_TRANSCRIPT_ARCHIVE=/Volumes/scratch/loom-transcripts \
  ./.loom/scripts/archive-transcripts.sh
```

```json
// .loom/config.json — new top-level "loom" block:
{ "loom": { "transcriptArchive": { "enabled": true, "dir": "/Volumes/scratch/loom-transcripts" } } }
```

**Layout at destination** — `<dir>/<repo>/<date>/<session-uuid>/`:

```
<session-uuid>.jsonl                       the session's own transcript
<session-uuid>/subagents/agent-*.jsonl     per-subagent transcripts
<session-uuid>/subagents/agent-*.meta.json role+issue sidecars (copied verbatim)
<session-uuid>/tool-results/…              large tool outputs
index.json                                 agent-id-keyed join index (schema loom.transcript-index/v1)
```

The `index.json` is keyed by `agent-<id>` with one row per subagent; **role and
issue are read from the existing `agent-*.meta.json` sidecars** (not re-derived),
and the archiver adds only the loom-side context the sidecar lacks (repo, sweep
issue, model, start/end ts, and `arm`/`attempt` when a model experiment is active).

**Base path is `CLAUDE_CONFIG_DIR`-aware** — the archiver never hard-codes
`~/.claude`. Per-agent isolated config dirs (`.loom/claude-config/<agent>/`) get a
fresh `projects/`, so the copier resolves its source through
`${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects` (the same base path
`loom-daemon/src/terminal.rs`'s `claude_config` module uses when it provisions
those isolated dirs).

**When it runs**: cron-friendly periodic sync (the durability backstop — the
session's own top-level `<uuid>.jsonl` is still being appended while the session
runs, so only a periodic + at-exit sync reliably captures the tail), plus a
completion-time invocation from `/loom:sweep`. Idempotent (size + mtime skip), so
re-runs copy nothing new. Cron example:

```cron
*/15 * * * * cd /path/to/repo && ./.loom/scripts/archive-transcripts.sh >> .loom/logs/archive-transcripts.log 2>&1
```

> **Guardrails — transcripts can contain secrets.** A transcript is full tool I/O
> and may include `.env` contents or token values that scrolled through a shell.
> The archiver treats the destination as sensitive, exactly like `.loom/tokens/`
> and `accounts.env`: created **mode `0700`**, files **`0600`**; if the destination
> is **inside a git repo it MUST be gitignored** or the archiver **refuses** (it
> will not copy secret-bearing transcripts into a tracked tree); and it prints a
> **loud one-line banner naming the destination** whenever archival is enabled.
> As with the token pool, **you (the operator) own the security of the archive
> location** — put it outside any repo, or gitignore it.

**Out of scope (v1)**: remote / object-storage backends (`s3://`, `gcs://`,
rsync-to-remote) are an explicit follow-on — v1 is a local filesystem destination
only.
