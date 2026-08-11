---
name: refactor
description: Restructure code while preserving observable behavior and compatibility. Use for refactors, decomposition, consolidation, and architecture cleanup.
---

# Refactor

1. Establish tests or observable behavior before changing structure.
2. Preserve public APIs, persistence formats, platform behavior, and ownership.
3. Move in small coherent steps and keep each step verifiable.
4. Avoid mixing unrelated feature changes into the refactor.
5. Run compatibility and regression checks after the final structure is in place.
