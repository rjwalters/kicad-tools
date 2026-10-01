---
name: "handoff"
description: "Roll the Claude session safely — file follow-ups, reset to baseline, check for a CLI update, and write a handoff note the next session reads first"
domain: repo
type: command
user-invocable: true
---

# /repo:handoff — Roll the Session Safely

Rolling a Claude Code session — quitting the CLI and starting fresh — is the
moment a session's most valuable output is most likely to be lost, because what
is worth carrying forward is exactly what exists *only* in the session's
context: in-flight state, settled decisions, and empirically-discovered traps.
This command makes the roll a repeatable ritual instead of an ad-hoc scramble.

It **composes** the existing commands rather than reimplementing them —
[[followups]] to capture deferred work, [[reset]] to reach a clean git baseline
— and adds only what does not exist yet: the CLI version check, the handoff
note, and exact restart instructions.

**Relationship to `/compact`:** compaction handles the *soft* boundary —
context pressure within a continuous session. `/repo:handoff` handles the
*hard* boundary: process restart, CLI upgrade, or a deliberate fresh start. If
the version check below finds no update and the only pressure is context size,
say so — a `/compact` may be all that's needed.

## Usage

```
/repo:handoff                  # Run the ritual end-to-end, confirmations as usual
/repo:handoff --dry-run        # Preview the note and proposed actions; file, prune, and write nothing
/repo:handoff --prune          # Pass --prune through to the reset stage
/repo:handoff --force          # Proceed even when the step-0 preflight fails
```

## Steps — ordering is load-bearing

Run the stages in exactly this order. Each later stage depends on the earlier
ones having actually happened.

### 0. Preflight — confirm something will actually read the note

Run before followups and reset, and stop on a blocking failure. Everything
below produces a note; this step proves the note has a reader first. That
reader is a `session-start-handoff.sh` **SessionStart hook** in two halves — a
script, and its wiring in `.claude/settings.json` — which `install.sh` writes
to paths with different git dispositions, so neither check substitutes for the
other.

Why each rule is shaped the way it is lives in the header of
`commands/repo/tests/test-handoff-preflight.sh` — **read it before changing
anything here.** That file pins much of this section by mutation-verified
regex, and a rewrite that drops a pin silently reopens the branch it guards.

```bash
HOOK='${CLAUDE_PROJECT_DIR}/.claude/skills/repo/hooks/session-start-handoff.sh'

# 1. Which of three states is settings.json in? They do NOT share a repair.
test -f .claude/settings.json              # absent, or present?
jq -e . .claude/settings.json >/dev/null   # malformed, or genuinely unwired?

# 2a. Our exact command, under every matcher we manage.
jq -e --arg c "$HOOK" '
      (.hooks.SessionStart // []) as $ss |
      (["startup","resume"] | all(. as $m | $ss | any(.[]?;
        (.matcher == $m) and ((.hooks // []) | any(.[]?; .command == $c)))))
    ' .claude/settings.json

# 2b. Only if 2a fails: is a DIFFERENT session-start-handoff.sh already wired?
jq -e --arg c "$HOOK" '
      (.hooks.SessionStart // []) | any(.[]?;
        (.hooks // []) | any(.[]?;
          ((.command // "") | test("session-start-handoff\\.sh")) and (.command != $c)))
    ' .claude/settings.json

# 3. Is the script that will actually run present and executable?
test -x .claude/skills/repo/hooks/session-start-handoff.sh

# 4. Is the note safe from being committed?
git check-ignore -q .claude/handoff.md
```

The rules that make those checks correct — each guards a branch, so keep its
sentence here even when compressing:

- **Run check 1 first.** 2a and 2b are both `jq -e`, so an absent
  `.claude/settings.json` and a malformed `.claude/settings.json` both fail
  them exactly the way a valid-but-unwired one does. Three states, three
  repairs.
- **Absent is fully repaired by `install.sh`** — never send it to a by-hand
  fix. `merge_settings_sessionstart_hook` creates an empty `{}` settings file
  *before* its invalid-JSON guard, so one re-install creates the file, wires
  both matchers and copies the script. Reachable on a fresh clone, not
  theoretical — the test header has the worked example.
- **Malformed is not repairable by `install.sh`.** That same guard refuses to
  touch invalid JSON and returns having wired nothing.
- **Mirror both predicates.** `install.sh`'s `merge_settings_sessionstart_hook`
  has **two** predicates, not one: 2a is its idempotency test and 2b its
  coexistence branch. `test-handoff-preflight.sh` extracts both jq programs
  from this file and from `install.sh` and asserts they are equal after
  normalization, so keep them copy-paste identical.
- **2b is not optional.** When a foreign-pathed hook is wired, `install.sh`
  defers to it and will never wire the command 2a tests for, so a 2a-only
  preflight produces a false block and an operator loop. A 2b match is a
  satisfied reader — report the foreign path as information and continue.
- **Check 3 follows the hook that will actually run**, which is the foreign
  path when 2b matched, not the installed path. When *that* script is missing,
  `./install.sh` is the wrong repair: it only ever copies to
  `.claude/skills/repo/hooks/`, and 2b makes it defer on the wiring, so the
  dangling entry survives. Fix or delete the foreign entry by hand first.
- **Check 4 auto-fixes** rather than blocks — adding the entry is the
  archetypal safe fix and step 4 would have done it anyway. Say that it was
  added. Under `--dry-run`, report it and add nothing.

A **partial** wiring — one matcher but not the other, with no foreign hook — is
a failure, exactly as the installer treats it as incomplete and finishes it.

**On a blocking failure, stop and report which check failed.** They have
different repairs, and one merged "the hook is broken" sends the operator to
the wrong one:

| Failing check | Repair |
|---|---|
| No script at the installed path, or not executable | `install.sh` re-copies it |
| 2b matched, but the *foreign* script is missing or not executable | By hand (above) |
| No `.claude/settings.json` at all | `install.sh` creates and wires one |
| No wiring at all (2a and 2b both fail) | `install.sh` merges it |
| Partial wiring, no foreign hook | `install.sh` completes it |
| `.claude/settings.json` is not valid JSON | By hand — `install.sh` wires nothing |

`install.sh` in that table means re-running the Repo Skills installer against
this repo — `./install.sh <repo>` from a Repo Skills checkout.

It has one further terminal state no check above can see in advance: if its
rewrite `jq` fails (a full disk, a `mktemp` failure), it warns `Failed to
update .claude/settings.json — left unchanged` and wires nothing. So an
`install.sh` row that leaves this preflight *still* failing, with that warning
in the installer's output, is not the wrong row — it is that state. It is
self-diagnosing but not self-repairing: clear the cause and re-run, or wire
the two matchers by hand.

Offer to run the repair, then re-run the preflight. `--force` proceeds anyway
and must say plainly, in the step-5 restart block, that the note will **not**
be announced on restart and has to be read by hand.

Three advisories that never block:

- If `REPO_HANDOFF_SIBLING_ROOT` is unset, say so once — it is the opt-in
  fallback that reports a pending note in a *sibling* checkout, and the only
  thing distinguishing "no note here" from "no note anywhere".
- Step 0 **does not check** whether the installed script is *stale* against
  the Repo Skills source: `test -x` cannot see staleness and the source
  checkout is not reachable from every consumer, so there is no probe here to
  fail — which is why staleness is not a row in the table. A wired, executable
  hook that predates a fix still runs and still announces the note, so mention
  `/repo:update-tools` as a follow-up if the version matters and move on.
- Under `--dry-run`, run every check for real and report the verdict, but
  apply no repair, not even the gitignore one: that flag writes nothing.

### 1. File follow-ups first (see [[followups]])

Run the full [[followups]] flow — mine the session, propose, confirm, file.
This must precede reset because [[reset]] prunes branches, worktrees, and
stashes that a follow-up may need to reference, and filing wants the git state
reset is about to remove. Record the issue URLs actually filed (and anything
proposed-but-declined) — the note needs them.

Under `--dry-run`, run followups in its own `--dry-run` mode: propose, file
nothing.

### 2. Reset to baseline (see [[reset]])

Run the full [[reset]] ritual: working-tree safety check, stash review, branch
& worktree review, remote sync, land on the default branch. Pass `--prune`
through if given. All of reset's gates apply unchanged — nothing irreversible
happens without explicit approval, and a dirty working tree stops the ritual
until the user decides (commit / stash / abort). Record what reset actually
did and what it intentionally left behind.

Under `--dry-run`, report what reset *would* do without acting.

### 3. Check the CLI version — before recommending a restart

Best-effort, never blocking:

```bash
claude --version                 # what this session is running
npm view @anthropic-ai/claude-code version 2>/dev/null   # latest, if npm is available
```

Report one of: **update available** (restart is worth it — note both versions),
**current** (a restart gains nothing; if the motive was context pressure,
suggest `/compact` instead), or **unknown** (say so plainly — do not guess).

### 4. Write the handoff note last

Written last because it must record what followups actually filed and what
reset actually did — any earlier and it is speculative.

**Where it lives (both halves required):**

1. `.claude/handoff.md` in this repo — repo-scoped and discoverable. Ensure
   it is gitignored (step 0 already checked this; add a `.claude/handoff.md`
   entry if not already covered) — the note is session state, never a commit.
2. A pointer in the agent's auto-memory index (`MEMORY.md` in the memory
   directory, when one exists): a single line —
   `- Handoff note at .claude/handoff.md — READ FIRST, then delete note + this line.`
   The memory index is read automatically at session start, but it arrives as
   passive background context, not an instruction — so the pointer is a
   *best-effort backup*, not a guarantee the note is read (a live handoff has
   been observed to slip past it). The mechanism that actively announces the
   note is the `session-start-handoff.sh` **SessionStart hook** — whose
   presence and wiring step 0 has already verified rather than assumed —
   which surfaces the note as session context on startup and resume — the
   full body inlined when the note is small, a header outline plus an
   oversize warning when it is large.

**Both halves are repo-scoped, and that is the point** — a note belongs to the
repo it was written in, and is invisible from any other one. The cost is that
"no note in this repo" and "no note anywhere" look identical at session start,
which has already lost a real handoff (a note in a sibling checkout, found only
after minutes of searching). The optional remedy is an environment variable read
by the same hook:

```bash
export REPO_HANDOFF_SIBLING_ROOT="$HOME/GitHub"   # where your checkouts live
```

Unset (the default) nothing changes. Set, and **only when the current repo has
no note of its own**, the hook additionally lists which repos directly under
that root do have one — **path and age only, never the body**, because a note is
one-shot for the repo it belongs to. Absorb it by starting a session there; the
scan is read-only, single-level, and capped at 64 directories.

**What goes in — only what is not recoverable from the repo:**

- **In-flight state** — open PRs and what they await, running background work,
  anything mid-flight.
- **Decisions and their rationale** — settled questions the next session must
  not relitigate.
- **Empirically-discovered traps** — "this command hangs", "this flag silently
  no-ops": findings that cost real time and are invisible in the code.
- **The precise next action** — one concrete step, not a roadmap.

Deliberately **exclude** anything readable from git history, the issue
tracker, or `CLAUDE.md` — the exclusion discipline is what keeps the note
short enough to be read.

**Honesty constraint:** every item carries a verification status —
`[verified]` (done and checked), `[believed-done]` (done, not re-checked), or
`[attempted]` (tried, outcome uncertain). A handoff that overstates completion
is worse than none, because the next session builds on it.

**One-shot contract:** the note describes a single moment. The next session
reads it, absorbs it, then deletes both the note and the memory pointer —
promoting anything durable into real memory files or issues. A stale handoff
lying around is a trap of its own.

Under `--dry-run`, print the note to the conversation instead of writing it.

### 5. Emit the restart block — and stop

The agent cannot quit, upgrade, or relaunch its own process. Do not pretend
to. End by printing an exact, copy-pasteable block for the human, e.g.:

```
# In this terminal:
#   1. Quit this session (Ctrl+C or /exit)
#   2. If an update was available:
claude update
#   3. Relaunch in this repo:
cd <repo-root> && claude
# The SessionStart hook announces the handoff note to the new session
# (the memory-index pointer is a best-effort backup).
```

Under `--force` past a failed step-0 preflight, replace those last two
comment lines with the truth — the note will not be announced, and the
operator has to open it by hand:

```
# NO SessionStart hook is wired in this repo: the note will NOT be announced.
# After relaunching, read it yourself:  cat .claude/handoff.md
```

Then stop. The ritual is complete when the note is durable and the
instructions are on screen — the restart itself belongs to the human.

## Principles

Same as every hygiene command: **apply safe fixes, gate destructive ones** —
this command adds no gates of its own but inherits every gate of the commands
it composes ([[followups]] always confirms before filing; [[reset]] never
destroys without opt-in). **Don't be noisy**: the note's value comes from what
it excludes.
