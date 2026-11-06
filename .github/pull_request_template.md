## Problem and scope
- Issue / observed behavior:
- Changed interfaces, data format or CLI behavior:

## Safety, failures and recovery
- Security/privacy changes:
- Failure behavior and rollback/recovery:
- Telemetry/logging impacts:

## Automated verification (exact commands, commit SHA, results)
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `mypy` passes
- [ ] `python -m pytest --cov --cov-fail-under=90` passes
- [ ] New sensitive-data paths prove secrets cannot enter storage or reports
- [ ] New network calls are opt-in and covered by `docs/threat-model.md`
- [ ] Golden files regenerated/reviewed if report output changed
- Tests **not run** and reason:
- CI run link:

## Manual acceptance
- Exact OS/environment and steps performed:
- Observed result and evidence link:
- Still unverified (do not claim operational acceptance):

## BES-1 lifecycle
- [ ] Built
- [ ] Verified
- [ ] Released (only if artifact exists)
- [ ] Operational (only if real runtime accepted)

AI assistance:  
Human verification:
