## [ERR-20260519-001] source_install_script_functions

**Logged**: 2026-05-19T00:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: tests

### Summary
Sourcing `install.sh` for function-level checks accidentally executed the installer.

### Error
```text
PEP 668 stopped pipx installation during an unintended installer run.
```

### Context
- Attempted to source `install.sh` with `sed "$d"` to call `parse_args`.
- The expression did not remove the final `main "$@"` line, so the script started normally.

### Suggested Fix
Use `head -n -1 install.sh` or a dedicated test harness when sourcing shell scripts that call `main` at the bottom.

### Metadata
- Reproducible: yes
- Related Files: install.sh

---
## [ERR-20260521-001] python_entrypoint_missing

**Logged**: 2026-05-21T16:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: tests

### Summary
The repo shell does not provide a `python` command; use `.venv/bin/pytest`, `python3`, or `uv` for local verification.

### Error
```text
zsh:1: command not found: python
```

### Context
- Attempted `python -m pytest ...` in `/home/gaooooosh/limem-agent-plugin/limem-cli`.
- `python3` and `uv` exist, and older project notes already prefer the project virtualenv test runner.

### Suggested Fix
Use `.venv/bin/pytest ...` for this repository when available.

### Metadata
- Reproducible: yes
- Related Files: limem-cli/pyproject.toml

---
## [ERR-20260520-001] pytest_entrypoint_missing

**Logged**: 2026-05-20T13:42:11+08:00
**Priority**: low
**Status**: pending
**Area**: tests

### Summary
The repo root shell may not have a global `pytest`; use the project virtualenv test runner.

### Error
```text
zsh:1: command not found: pytest
/usr/bin/python3: No module named pytest
```

### Context
- Attempted `pytest limem-cli/tests/...` from `/home/gaooooosh/limem-agent-plugin`.
- System Python lacks pytest, while `.venv/bin/pytest` exists and runs the suite.

### Suggested Fix
Use `.venv/bin/pytest ...` for this repository unless the environment is refreshed.

### Metadata
- Reproducible: yes
- Related Files: limem-cli/pyproject.toml

---
## [ERR-20260521-003] zsh_url_glob_in_curl

**Logged**: 2026-05-21T16:06:22+08:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
Unquoted GitHub API URLs with `?` fail in zsh because the shell treats them as glob patterns.

### Error
```text
zsh:1: no matches found: https://api.github.com/repos/openai/codex/git/trees/main?recursive=1
```

### Context
- Tried to inspect Codex hook source through the GitHub tree API.
- The URL was not quoted, so zsh expanded `?` before `curl` ran.

### Suggested Fix
Quote URLs containing `?`, `&`, or glob-like characters in zsh commands.

### Metadata
- Reproducible: yes
- Related Files: limem-cli/limem/hooks.py

---
## [ERR-20260521-002] limem_mcp_write_feedback_500

**Logged**: 2026-05-21T15:51:15+08:00
**Priority**: medium
**Status**: pending
**Area**: infra

### Summary
`limem_write` failed with a LiMem 500 while trying to persist user feedback about recall visibility.

### Error
```text
{"error": "LiMem 500: Internal Server Error"}
```

### Context
- The user corrected the design: recall usage should appear as a separate hook/tool-style notice, not inside the agent's final prose.
- Attempted to persist that project feedback through the LiMem MCP `limem_write` tool.
- The code fix continued locally despite the backend failure.

### Suggested Fix
Diagnose the LiMem service ingest/write path before assuming feedback persistence succeeded; retry once the backend is healthy.

### Metadata
- Reproducible: unknown
- Related Files: limem-cli/limem/hooks.py

---
## [ERR-20260522-001] codex_desktop_hidden_hook_systemmessage

**Logged**: 2026-05-22T10:19:43+08:00
**Priority**: high
**Status**: pending
**Area**: integration

### Summary
Codex desktop can execute LiMem hooks and inject memory while still hiding hook `systemMessage` notices from the user.

### Error
```text
UserPromptSubmit stdout contained {"systemMessage": "..."} and recent_recalls recorded memory hits, but the Codex desktop UI showed no citation notice.
```

### Context
- The user's current Codex desktop session recalled project memories and wrote `recall_emitted` records with short IDs.
- The prior implementation assumed top-level hook `systemMessage` and Stop-hook stderr would be user-visible.
- Local evidence showed Codex desktop did not surface prompt-hook stdout JSON or Stop-hook stderr as a reliable visible channel.

### Suggested Fix
For Codex, keep hook `systemMessage` for hosts that support it, but also inject a small `additionalContext` fallback instructing the assistant to print the LiMem citation line in the final response. Keep Stop-hook recall notice ahead of passive session flushing so backend timeouts cannot block it.

### Metadata
- Reproducible: yes
- Related Files: limem-cli/limem/hooks.py

---
## [ERR-20260522-002] limem_write_feedback_timeout

**Logged**: 2026-05-22T10:49:02+08:00
**Priority**: medium
**Status**: pending
**Area**: integration

### Summary
Persisting user feedback about LiMem citation styling timed out through `limem_write`.

### Error
```text
LiMem 0: network error: The read operation timed out
```

### Context
- User said the visible memory citation looked ugly and exposed too much internal log content.
- Attempted to persist the feedback with `mem_type=feedback`.
- Local implementation continued despite the backend timeout.

### Suggested Fix
Retry persistence when backend connectivity is healthy; keep local tests covering the citation UI style.

### Metadata
- Reproducible: unknown
- Related Files: limem-cli/limem/hooks.py

---
