---
name: loom-comment-body-literal-path
description: "This is the single canonical copy of this warning. Every role prompt that carries a short \"`--body @path` Does NOT Expand\" pointer refers here for the full pitfall, incident citation, and fixes."
---
<!-- loom-managed-skill -->
<!-- GENERATED FILE — DO NOT EDIT DIRECTLY.
     Produced by `loom-daemon generate-agent-skills` from
     defaults/.claude/commands/loom/comment-body-literal-path.md (the same source Claude Code
     reads as /loom:comment-body-literal-path via .claude/commands/loom/). This is the
     cross-vendor skill-discovery surface (.agents/skills/<name>/SKILL.md)
     read natively by Codex, Kimi Code, Mistral Vibe, and Grok — see
     runtime-adapters.md §5. To change this file, edit the source above
     and re-run the generator; CI (`loom-daemon generate-agent-skills
     --check`) fails if this file is stale. -->

# `--body @path` Does NOT Expand — It Posts the Literal String (canonical)

This is the single canonical copy of this warning. Every role prompt that
carries a short "`--body @path` Does NOT Expand" pointer refers here for the
full pitfall, incident citation, and fixes.

**If a comment/review body you're posting (via `gh issue comment`, `gh pr
comment`, or `gh api ... comments`) lives in a scratch/scratchpad file, do not
pass it as `--body @path`.** Unlike some shells' `@file` conventions, `gh pr
comment --body @path` and `gh issue comment --body @path` do **not** read the
file — they post the literal text `@path` as the comment. A real incident (PR
#4457) lost an entire changes-requested review this way: the comment body was
the string `@/private/tmp/.../scratchpad/review.md`, not the review prose, and
the scratchpad file was later overwritten by an unrelated PR's review before
anyone caught it. It recurred again later via `gh api ... -f body=@path`
(`-f`/`--raw-field` never expands `@path` either) — see #5252.

```
❌ POSTS THE LITERAL STRING "@path" — NOT THE FILE CONTENTS
   gh pr comment 123 --body @/tmp/review-123.md
   gh pr comment 123 --body "@/tmp/review-123.md"
   gh issue comment 123 --body @/tmp/comment-123.md

❌ ALSO POSTS THE LITERAL STRING — a variable does NOT change what the flag does
   REVIEW_FILE="@/tmp/review-123.md"; gh pr comment 123 --body "$REVIEW_FILE"

❌ ALSO POSTS THE LITERAL STRING — on `gh api`, only -F/--field expands @path
   gh api repos/{owner}/{repo}/issues/123/comments -f body=@/tmp/review-123.md

✅ USE ONE OF THESE INSTEAD
   gh pr comment 123 --body "$(cat <<'EOF'
   ... review prose ...
   EOF
   )"
   gh pr comment 123 --body-file /tmp/review-123.md
   gh api repos/{owner}/{repo}/issues/123/comments -F body=@/tmp/review-123.md
```

Prefer the inline heredoc pattern above when the body is short/dynamic; use
`-F/--body-file <path>` when the body genuinely lives in a file (e.g. a
scratchpad review draft) — it is the one flag on `gh pr comment`/`gh issue
comment` that actually reads file contents (`gh api ... -F body=@path` also
works — but `-f`/`--raw-field` does **not**). **Never** pass the file path as
the value of `--body`/`-b` with an `@` prefix — that flag takes literal text
only. **After posting, re-fetch the comment** (`gh pr view <number>
--comments` / `gh issue view <number> --comments`) to confirm it renders your
prose, not a path string.

### Name every staged body file after its issue/PR number — never a fixed name (#6381)

`/tmp/review-123.md` above is not a stylistic choice — **always suffix a staged
body file with the issue or PR number it belongs to** (`pr-body-<N>.md`,
`review-<N>.md`, `fix-comment-<N>.md`), never a fixed constant like
`pr-body.md` or `review.md`. Wave subagents dispatched by `/loom:sweep` are
one level deep from a single orchestrator session and **share one scratchpad
directory** — there is no per-subagent scratch namespace. Two concurrent
agents each writing to the same fixed path race on it: one's `create-pr.sh
--body-file <path>` / `gh pr comment --body-file <path>` can read the *other*
agent's body between its write and your read, silently publishing a PR or
comment with the wrong title, wrong `Closes #N`, or wrong content — with
nothing failing and no error anywhere (#6381, near-miss in a consumer repo,
a private-repo PR). The same collision applies outside wave dispatch
too: two independent `/loom:sweep` runs (different terminals, same host) can
just as easily race on an unnamespaced `/tmp` path.

A namespaced path like `/tmp/pr-body-123.md` is still a **literal, non-
interpolated-at-guard-time path argument**, so it satisfies the
destructive-write guard's literal-path requirement exactly the same as the
fixed name did (#4921/#4178) — namespacing the filename is not a guard
workaround, it only removes the cross-agent collision.

**A guard denial is not an invitation to re-shape the same value.** The
`--body @path` shape is hard-denied by `guard-destructive-generic.sh`. If you
hit that denial, the only correct response is to switch to `--body-file` or
the heredoc — **never** to route the identical `@path` value through a shell
variable, a `--raw-field`, or any other wrapper. That exact evasion is how the
anti-pattern recurred on PR #4600 after the guard was already live (#4601), and
it is now denied too.
