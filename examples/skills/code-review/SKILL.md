---
name: code-review
description: Use when asked to review code, a diff, or a pull request for bugs, security and maintainability.
---

# Code review procedure

1. Establish scope: `bash git diff --stat` (or list the files named by the user).
2. Read every changed file fully with `read_file` before commenting.
3. Check, in order: correctness and edge cases → error handling → security (injection, secrets, path handling) → tests → readability.
4. Run the project's tests and linters; quote failures verbatim.
5. Report findings grouped as **Blocker / Should fix / Nit**, each with `path:line` and a concrete fix.
6. Finish with a one-line verdict: approve, approve with nits, or request changes.

Never modify files during a review unless explicitly asked.
