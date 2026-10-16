# Why the heredoc delimiter must be quoted (`<<'EOF'`, not `<<EOF`)

Companion to the "USE ONE OF THESE INSTEAD" block in
`defaults/.claude/commands/loom/comment-body-literal-path.md`. The `<<'EOF'`
in that block is load-bearing.

An **unquoted** `<<EOF` expands `$var`, `$(...)` **and backticks**, so prose
like `` `.loom/` `` is executed and replaced by its empty output. The error goes
to stderr while the mangled body is posted, so the post succeeds with the path
silently deleted (seen curating #9123).

```
❌ cat <<EOF                      → "Sha abc123. The path  is materialized."
   Sha $SHA. The path `.loom/` is materialized.
   EOF
```

- **Rule:** always quote the delimiter for prose bodies; a quoted body is fully
  literal.
- **The trap:** you want `$SHA` in the body. Do not drop the quotes; write a
  placeholder and substitute afterward, then post with `--body-file`:

  ```
  ✅ cat <<'EOF' | sed "s/@SHA@/$SHA/g" > /tmp/body-123.md
     Sha @SHA@. The path `.loom/` is materialized.
     EOF
  ```
- **Escaping is not the answer:** one missed `` \` `` fails silently; the quoted
  delimiter is all-or-nothing.
- **Re-read after posting:** this damage is visible only on the forge (see the
  "After posting, re-fetch the comment" rule in the command file).
