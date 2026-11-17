# Squad coordination

Squad provides the repository's local chatroom and shared goal board. Loom
continues to manage issue ownership, worktrees, CI review, and merges. A chat
message or Squad claim does not replace a Loom verdict or GitHub claim.

## Local setup

The checked-in workflows come from [rjwalters/squad](https://github.com/rjwalters/squad)
0.15.2. Build a local Squad checkout following its README, then run its installer
from this repository's primary clone:

```sh
bash ../squad/install.sh --no-link "$PWD"
```

If Codex already has a working global Squad MCP registration, add `--no-codex`
to preserve it. This still installs both repository skill bundles. The installer
preserves customized files and reports conflicts for explicit resolution.

The MCP configuration and installation ownership receipt are machine-local;
room history lives in ignored `.squad/`. Neither belongs in Git. Other machines
must run the installer themselves.

### Linked worktrees

Loom shares the primary clone's `.mcp.json` with its worktrees. Pin Squad's local
runtime and room paths so launching Claude from a worktree reaches the same
room. Run this from the primary clone after installing, adjusting the sibling
Squad path if necessary:

```sh
python3 - <<'PYTHON'
import json
from pathlib import Path

repo = Path.cwd().resolve()
config_path = repo / ".mcp.json"
config = json.loads(config_path.read_text())
squad = config["mcpServers"]["squad"]
squad["args"] = [str((repo.parent / "squad" / "dist" / "index.js").resolve())]
squad.setdefault("env", {})["SQUAD_DIR"] = str(repo / ".squad")
config_path.write_text(json.dumps(config, indent=2) + "\n")
PYTHON
```

The installer accepts these absolute paths as equivalent to its defaults.
Refresh them after moving the checkouts. Without an explicit `SQUAD_DIR`, the
Squad CLI and global Codex MCP registration resolve linked worktrees back to
the primary clone automatically.

## Use the room

- Codex: invoke `$squad`, or ask to join the room.
- Claude: invoke `/squad:join`.
- Human terminal: `squad tail` watches chat; `squad send "message"` posts;
  `squad goals` shows the shared objective.

Use distinct identities for independent agents. Subagents use the CLI with their
own `SQUAD_PERSONA`, because inherited MCP connections share their parent's
identity. Check messages before taking work, announce claims, and post evidence
and handoffs when finished. See the installed Squad skill for details.

## Verify

```sh
bash ../squad/install.sh --no-codex --no-link --check "$PWD"
squad doctor
squad path
```

Verify MCP joins from both the primary clone and a linked worktree return the
primary clone's `.squad/squad.db`. CLI path checks alone do not exercise the
project MCP launcher's working directory.
