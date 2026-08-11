---
name: browser
description: Navigate and inspect interactive websites through a configured Browser MCP server. Use for opening URLs, clicking page controls, reading rendered page state, browser screenshots, and local web UI validation.
---

# Browser

1. Use only the namespaced tools supplied by the configured Browser MCP server.
2. Inspect current page state before clicking or typing; use stable visible labels or semantic targets.
3. Treat login, purchase, publishing, deletion, and message sending as external mutations and require an `action` task with the applicable policy.
4. For local web testing, start the approved local server first and keep navigation within the task scope.
5. If no Browser MCP tools are available, report that the plugin needs MCP configuration. Do not claim that a plain HTTP fetch is interactive browser control.
