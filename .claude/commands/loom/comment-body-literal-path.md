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
   gh pr comment 123 --body @/tmp/review.md
   gh pr comment 123 --body "@/tmp/review.md"
   gh issue comment 123 --body @/tmp/comment.md

❌ ALSO POSTS THE LITERAL STRING — a variable does NOT change what the flag does
   REVIEW_FILE="@/tmp/review.md"; gh pr comment 123 --body "$REVIEW_FILE"

❌ ALSO POSTS THE LITERAL STRING — on `gh api`, only -F/--field expands @path
   gh api repos/{owner}/{repo}/issues/123/comments -f body=@/tmp/review.md

✅ USE ONE OF THESE INSTEAD
   gh pr comment 123 --body "$(cat <<'EOF'
   ... review prose ...
   EOF
   )"
   gh pr comment 123 --body-file /tmp/review.md
   gh api repos/{owner}/{repo}/issues/123/comments -F body=@/tmp/review.md
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

**A guard denial is not an invitation to re-shape the same value.** The
`--body @path` shape is hard-denied by `guard-destructive-generic.sh`. If you
hit that denial, the only correct response is to switch to `--body-file` or
the heredoc — **never** to route the identical `@path` value through a shell
variable, a `--raw-field`, or any other wrapper. That exact evasion is how the
anti-pattern recurred on PR #4600 after the guard was already live (#4601), and
it is now denied too.
