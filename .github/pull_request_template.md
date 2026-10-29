## Summary

## Testing
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `mypy` passes
- [ ] `python -m pytest --cov` passes
- [ ] New data paths have a test proving secrets do not reach the database or reports
- [ ] No new network calls (or they are opt-in and covered in `docs/threat-model.md`)
- [ ] Golden files regenerated and reviewed (if report output changed)
