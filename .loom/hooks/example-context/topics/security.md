# Security Guidelines

This file is injected when a prompt mentions security-related keywords.
The default pattern matches the filename ("security"). For custom patterns,
create a sidecar file (e.g., topics/security.pattern) containing a regex.

## Example Content

- Never log secrets or tokens
- Use parameterized queries for all database access
- Validate and sanitize all user input
