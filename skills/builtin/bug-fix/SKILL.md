---
name: bug-fix
description: Diagnose and repair a concrete failure with regression protection. Use for errors, exceptions, broken behavior, failed tests, and repeated workflow failures.
---

# Bug Fix

1. Reproduce or identify the failing path from logs, tests, and current code.
2. Separate the root cause from the final symptom and provider error wording.
3. Patch the narrowest shared cause, including recovery and platform variants.
4. Add a regression test that fails before the fix and passes after it.
5. Run the focused test first, then the relevant suite; report any residual risk.
