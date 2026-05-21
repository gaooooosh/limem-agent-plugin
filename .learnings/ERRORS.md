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
