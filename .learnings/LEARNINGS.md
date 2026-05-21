## [LRN-20260521-001] correction

**Logged**: 2026-05-21T16:06:22+08:00
**Priority**: high
**Status**: pending
**Area**: infra

### Summary
Codex UserPromptSubmit hook output must not include Claude-style `decision: "allow"`.

### Details
The user reported that LiMem recall notices still were not visible after adding top-level `systemMessage`. Investigation of the Codex hook implementation showed that `UserPromptSubmit` maps `systemMessage` to a visible warning entry, but its schema only accepts `decision: "block"`. Emitting `decision: "allow"` makes the JSON invalid for Codex, so the notice is not rendered as intended.

### Suggested Action
For prompt-time visible hook notices, emit `systemMessage` and `suppressOutput` without `decision: "allow"`; reserve event-specific decisions for protocols that explicitly allow them.

### Metadata
- Source: user_feedback
- Related Files: limem-cli/limem/hooks.py
- Tags: codex, hooks, limem

---
