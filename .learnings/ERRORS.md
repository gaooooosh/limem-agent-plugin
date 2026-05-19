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
