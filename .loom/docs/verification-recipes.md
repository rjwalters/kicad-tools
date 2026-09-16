# Verification Recipes

How to check that a thing you just did actually happened. Five recipes, each
one a *command you run*, not a habit you cultivate.

These come from a root-cause analysis of 17 agent errors made across two
independent sessions in this repo (#7793). They cluster into five shapes, and
each shape has the same fix every time: **assert the postcondition, with a tool
that can see what you cannot.**

Two of the five are this repo's own most-paid-for defect classes. The pattern is
not that agents are careless — it is that the cheapest thing that looks like it
works (a regex, an exit status, a `grep` filter) fails silently on the minority
case.

## 1. A mechanical text move: assert an invariant, not the diff

**When**: extracting a test module, splitting a file, dedenting, bulk-renaming,
rewriting an issue body by string substitution — any transform that moves text
rather than changing behavior.

**The check**: name something that must be *byte-identical* after the move, and
verify it with a tool that parses the language. Not by reading the diff, and not
by counting matches.

| Kind of text | Invariant | Tool that can see it |
|---|---|---|
| Rust | every string-literal body unchanged | move verbatim + `cargo fmt` (real lexer) |
| Shell | every quoted heredoc/payload unchanged | `shfmt -d` + `shellcheck` |
| Markdown | heading depth and heading list unchanged | dump all headings before/after, diff the two lists |

**Why the obvious checks do not work**:

- **The diff cannot see it.** A dedent implemented as "strip four spaces from
  every line" rewrote the interiors of raw string literals — embedded shell
  scripts and JSON fixtures — in 4 of 8 files during #7718. Invisible in the
  diff, invisible to the compiler, and invisible to 5,123 passing tests, because
  the corrupted fixtures were whitespace-insensitive.
- **Counting matches does not work either.** An issue-body edit anchored on
  `## Cost 3, minor` guarded itself with `assert body.count(old) == 1`. The
  assertion *passed* — there genuinely was exactly one match, inside a `###`
  heading, because `##` is a substring of `###`. It ate one `#`, silently
  demoting a section. `count(old) == 1` is not a guard; `parse(result)` is.

**The generalization**: a line-oriented regex cannot tell code from the inside of
a string literal, from a comment, or from a markdown heading's depth. When you
need a count for a report, **exclude comment lines first and say "code-only"** —
a bash-4 survey inflated its at-risk list from 8 files to 13 by matching comments
that said `bash 3.2: no mapfile`, and a review reported "the fix didn't remove
them" after matching the new comment's own prose.

See also `.loom/docs/file-size-policy.md` → "Mechanical refactors" (repo-local to
the loom source repo) for the same rule stated as policy for Loom's own source.

## 2. A push or other mutation: re-read the ref

**When**: after `git push`, a label edit, a comment post, a file write from a
long-running script — anything whose success you are about to report.

**The check**: read the remote state back and compare. `&&`-chain it so a
non-zero exit can never be stepped over.

```bash
git push -u origin "$BRANCH" &&
  git fetch origin "$BRANCH" &&
  [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$BRANCH")" ] &&
  echo "PUSH LANDED: $(git rev-parse --short HEAD)"
```

**Two ways this goes wrong, both observed**:

- **`;` instead of `&&`.** A rejected push was reported as "pushed" because a
  `;` made the success message unconditional and a `grep` display filter swallowed
  the error (#7744). **Never let a filter consume the exit status** — filter a
  copy, check `$?` on the original.
- **Tailing a long report.** `resync-installed.sh` was reported successful on the
  strength of its last line (`1 file(s) updated … 0 removed`) and exit 0. It had
  printed `declare: -A: invalid option` **twice**, near the top of a ~430-line
  report, and continued because the script is `set -uo pipefail` with no `-e`
  (#7749). Tailing is the natural way to read a 430-line report, which is exactly
  why the error survived.

So for any command with long output, **grep the whole output for error markers**
rather than reading its end:

```bash
CMD=(gh pr checks "$PR")
RC=0
"${CMD[@]}" >"$LOG" 2>&1 || RC=$?
grep -nEi 'error|invalid option|not found|failed|denied' "$LOG" || true
echo "rc=$RC"
```

An exit status of 0 from a script without `set -e` means "it reached the end",
not "it worked".

## 3. CI and monitor state: enumerate every terminal state

**When**: reporting that checks are green, that a run finished, or that a set of
PRs is "resolved".

**The check**: your filter must name every state it will accept as done, and
every state that means *not yet*. `pending` is a state, not an absence of
failure.

```bash
# Require nonempty structured checks with only recognized terminal buckets.
rc=0
out="$(gh pr checks "$PR" --json bucket 2>/dev/null)" || rc=$?
if { [ "$rc" -eq 0 ] || [ "$rc" -eq 1 ]; } &&
  printf '%s\n' "$out" | jq -e '
    type == "array" and length > 0 and
    all(.[]; .bucket | IN("pass", "fail", "skipping", "cancel"))
  ' >/dev/null 2>&1; then
  echo "SETTLED"
else
  echo "NOT SETTLED: pending, empty, unknown, or unavailable checks — retry"
fi
```

`SETTLED` means terminal, not passing: failed and cancelled checks still need
attention. Exit 1 may carry failed-check results; pending checks return 8.

A monitor that modelled only `FAILURE` and `RUN` reported "all four resolved /
COMPLETE" while all four PRs still had `pending` checks — *absence of failure*
read as *done*. Zero rows is the same trap in a different costume: `gh pr checks`
is GraphQL-backed and returns an empty list on a transient forge error, which is
indistinguishable from "nothing pending" unless you assert a minimum row count
(#6169).

**The self-test**: *if this process crashed right now, would my filter emit
anything?* If the answer is "it would emit success", the filter is wrong.

## 4. Posting text to the forge: `--body-file`, always

**When**: any `gh issue comment` / `gh pr comment` / `gh api … comments` body
containing a backtick, `$`, or `!`.

**The check**: write the body to a file, post the file.

```bash
# The body is data. Do not let the shell evaluate it.
gh pr comment "$PR" --body-file /tmp/review-"$PR".md
```

Inline `--body "…"` is **silently lossy**: backticks inside an inline body were
command-substituted before ever reaching GitHub, deleting filenames and figures
from a posted review (#7714). Nothing errors; the comment simply arrives with
holes, and a review comment is a durable record. The only way to detect it after
the fact is to read the comment back.

This is the sibling failure of the one already documented in
`.claude/commands/loom/comment-body-literal-path.md`: `--body @path` posts the
literal string `@path`. Same lesson, opposite direction — **the body is data;
`--body-file` is the only flag that treats it that way.**

## 5. A diagnosis: name the falsifying fact and check it first

**When**: about to report a cause, a hazard, or an absence ("X is broken", "this
would have clobbered Y", "no policy covers this").

**The check**: before reporting, name the *one* fact that would make you wrong,
and go look at it. In every observed case it was a single field or a single
command away:

| Claim | The one check | What was actually true |
|---|---|---|
| "main is red" | `gh run list --branch main` | main was green; the failure was macOS-only |
| "pushing would clobber PR #N" | `gh pr view N --json isCrossRepository` | cross-repo PR; its head lives on the fork |
| "the acceptance grep is false" | `git rev-parse --abbrev-ref HEAD` | was grepping `main`, not the PR branch |
| "the gate false-passed" | compare against the run's echoed `--head` | different head; the branch had been rebased |
| "rebasing will fix it" | run the check *after* rebasing | genuine content bug |

**The absence case is separate, and more expensive.** "No prior art exists" and
"this policy isn't covered anywhere" are claims about *this repo's issue
history*, which is its institutional memory. One search settles them:

```bash
gh issue list --search "<the concept> in:title,body" --state all --limit 20
```

Two confident assertions — "guard telemetry has been off since it was added" and
"set `LOOM_GUARD_STASH_SCOPE=0` to reduce prompts" — were both already answered
in the issue history (a standing review policy with two completed reviews; and
#4821/#5754, where parallel builders actually raced on each other's stashes and
the telemetry review ruled "keep flagged — do not weaken"). An agent that does
not query the history will confidently re-derive conclusions that were already
tested and rejected.

## The shape all five share

Each failure was a *plausible* story that fit the first two observations,
reported before checking the third. The cost is asymmetric: a wrong result
delivered confidently sends the next reader somewhere the work isn't. All five
recipes are the same move — **spend one command to make the silent failure
loud.**
