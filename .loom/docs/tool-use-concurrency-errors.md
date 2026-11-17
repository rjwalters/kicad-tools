# Claude Code Tool Use Concurrency Errors

This document explains the "API Error: 400 due to tool use concurrency issues" error that may occur when using Claude Code, including its causes, impact on Loom workflows, and mitigation strategies.

## Overview

The error message typically appears as:

```
API Error: 400 due to tool use concurrency issues. Run /rewind to recover the conversation.
```

This is a known issue affecting Claude Code users that stems from how parallel tool calls are handled by the Anthropic API.

## Root Cause

The underlying API error is:

```json
{
  "type": "error",
  "error": {
    "type": "invalid_request_error",
    "message": "messages.N: `tool_use` ids were found without `tool_result` blocks immediately after: toolu_XXX, toolu_YYY. Each `tool_use` block must have a corresponding `tool_result` block in the next message."
  }
}
```

### Technical Explanation

The Anthropic API has strict message structure requirements:

1. **Every `tool_use` block must have a corresponding `tool_result` block** in the immediately following user message
2. **The ordering is critical** - tool results must appear before any text content in the response
3. **All parallel tool results must be in a single user message** - sending separate messages for each result breaks the expected structure

When Claude Code attempts to execute multiple tools in parallel, if the response structure becomes malformed (e.g., due to hooks, interruptions, or internal errors), the API rejects the request with a 400 error.

## When This Error Occurs

### Common Scenarios

| Scenario | Description |
|----------|-------------|
| **Parallel file operations** | Reading, writing, or editing multiple files simultaneously |
| **Multiple agent spawning** | Requesting Claude to spawn multiple Task agents in parallel |
| **Print mode (`-p` flag)** | Non-interactive mode is more susceptible than interactive mode |
| **PostToolUse hooks** | Hooks that modify or inject content can corrupt the message structure |
| **File being viewed** | Editing files that are simultaneously open in an IDE may trigger syntax detection |
| **Large batch operations** | Processing many items that trigger multiple concurrent tool calls |

### Platforms Affected

- macOS (Platform: darwin)
- Windows (platform: windows)
- Various IDE integrations (VS Code, Cursor)

## Impact on Loom Workflows

Loom's multi-agent architecture can be affected in several ways:

### High-Risk Operations

1. **Builder agents** performing multiple file reads/writes
2. **Shepherd orchestration** spawning parallel sub-tasks
3. **Judge reviews** reading multiple files for context
4. **Daemon polling** that triggers concurrent operations

### Recovery Implications

When this error occurs:
- The current conversation may become corrupted
- In-progress tool operations may be partially completed
- Agent state may become inconsistent

## Mitigation Strategies

### Immediate Recovery

When the error occurs, use the `/rewind` command to recover the conversation state.

### Preventive Configuration

Add the following to your global Claude Code configuration (`~/.claude/CLAUDE.md`) to enforce sequential tool execution:

```markdown
# Force Sequential Tool Execution

## System Constraint
Execute tools sequentially, never in parallel.
This is mandatory due to API message structure requirements where each tool_use must have an immediate tool_result block.

## Implementation
- Process one tool call at a time
- Wait for tool_result before initiating next tool execution
- Do not batch or parallelize tool operations
```

**Note**: This workaround reduces parallelism but prevents the error from occurring. Users have reported zero recurrence after implementing this fix.

### Loom-Specific Mitigations

For Loom workflows, consider:

1. **Avoid aggressive parallelism in role definitions** - Role files should not instruct agents to perform many concurrent file operations

2. **Use heartbeats during long operations** - Shepherds should report heartbeats to prevent stuck detection from interfering

3. **Implement retry logic** - When orchestrating sub-tasks, be prepared to retry on 400 errors

4. **Monitor for the error pattern** - Add to stuck detection indicators if this error recurs frequently

### Best Practices for Tool Calls

Following the official Anthropic documentation:

1. **Tool result formatting**:
   ```json
   // Correct - all results in single message
   {"role": "user", "content": [
     {"type": "tool_result", "tool_use_id": "toolu_01", ...},
     {"type": "tool_result", "tool_use_id": "toolu_02", ...}
   ]}

   // Incorrect - will cause 400 error
   {"role": "user", "content": [
     {"type": "text", "text": "Results:"},  // Text before tool_result
     {"type": "tool_result", ...}
   ]}
   ```

2. **Message ordering**:
   - Tool result blocks must come FIRST in the content array
   - Any text must come AFTER all tool results
   - Never include messages between assistant's tool use and user's tool result

## Version History and Status

### Timeline

| Date | Event |
|------|-------|
| October 2025 | Issue first reported widely (GitHub Issue #8763) |
| October 7, 2025 | Anthropic identified 3 root causes, 1 mitigated |
| November 27, 2025 | Issue #8763 marked as COMPLETED/CLOSED |
| January 2026 | New reports continue (Issues #20592, #20598) |

### Current Status

The issue was partially fixed but continues to occur in certain scenarios. Anthropic has acknowledged the problem and implemented mitigations, but complete resolution may depend on:

- Claude Code version updates
- Upstream API changes
- User configuration adjustments

### Version-Specific Notes

- **Claude Code 2.15**: Reported as working
- **Claude Code 2.16, 2.17, 2.1.19**: May exhibit the issue more frequently
- Check for updates and consider version pinning if stability is critical

## Related Resources

### Official Documentation

- [Anthropic API Errors](https://platform.claude.com/docs/en/api/errors) - 400 error is `invalid_request_error`
- [How to Implement Tool Use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/implement-tool-use) - Message formatting requirements
- [Programmatic Tool Calling](https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling) - Advanced tool use patterns

### GitHub Issues

- [Issue #8763](https://github.com/anthropics/claude-code/issues/8763) - Main tracking issue (closed)
- [Issue #20598](https://github.com/anthropics/claude-code/issues/20598) - Recent report (January 2026)
- [Issue #9002](https://github.com/anthropics/claude-code/issues/9002) - Tool use concurrency limitation
- [Issue #18130](https://github.com/anthropics/claude-code/issues/18130) - Print mode specific issue

## Troubleshooting Checklist

When encountering this error:

- [ ] Run `/rewind` to recover the conversation
- [ ] Check if you're using print mode (`-p`) - try interactive mode instead
- [ ] Review any PostToolUse hooks that may be interfering
- [ ] Verify Claude Code version and check for updates
- [ ] Consider adding sequential execution instructions to CLAUDE.md
- [ ] For Loom: Check if the error correlates with parallel file operations in role definitions

## Appendix: API Rate Limits vs. Concurrency Errors

This error is **not** the same as rate limiting (HTTP 429). Key differences:

| Aspect | Tool Concurrency Error (400) | Rate Limit Error (429) |
|--------|------------------------------|------------------------|
| Cause | Malformed message structure | Too many requests |
| Recovery | `/rewind`, fix message format | Wait, implement backoff |
| Prevention | Sequential execution, proper formatting | Request throttling |

Rate limits are handled by exponential backoff and request throttling. Tool concurrency errors require structural fixes to how tool calls are formatted.
