---
name: code-review
description: Review final code and tests independently for correctness, omissions, security, and compatibility. Use for read-only review and acceptance decisions.
---

# Code Review

1. Compare final behavior with the user request and acceptance criteria.
2. Inspect complete final files when a diff boundary is ambiguous.
3. Report only actionable findings with file, location, impact, and severity.
4. Check error paths, cleanup, persistence, Windows compatibility, and tests.
5. Treat deterministic validation as evidence, not as proof of semantic completeness.
