# C9-C11 Release Decisions

The following decisions are frozen for this sprint. Their authority is the
approved bootstrap contract and `docs/release/HANDOFF_MVP_TO_C9_C11.md`.

1. Sprint weights are C9 EXPRESS 30%, C10 EXPRESS 40%, C11 PREP 20%, and
   FINAL COMBINED ACCEPTANCE 10%; the sum is 100%.
2. The real 48-hour C11 observation is excluded. C11 PREP begins only after
   C9 and C10 are integrated.
3. C9 uses user-level Task Scheduler unless objective focused evidence shows
   it cannot meet the contract. A Windows Service is not part of C9 EXPRESS.
4. C9 reuses the existing Windows named mutex in `src/single_instance.py` and
   `src/app.py`; it creates no second locking subsystem.
5. Backend logs use simple bounded rotation. C9 does not build telemetry or a
   log-management platform.
6. Backups use the SQLite online-backup API. Acceptance includes one restore
   drill in a temporary/copy location and refusal of a corrupted backup.
7. Startup uses cheap bounded `quick_check` plus schema/version validation;
   it does not run full `integrity_check` on every start.
8. C10 is coverage-first. It reuses sufficient C2/C3/C6/C7/C8 evidence and
   adds tests only for load-bearing gaps named in
   `docs/release/C10_COVERAGE_INDEX.md`.
9. C10 does not create a Chaos Framework, Fault DSL, scenario platform, or a
   kill-at-every-line matrix.
10. Workers run focused verification and one self-review. The full native
    Windows suite runs once, at the final integrated boundary.
11. Real-money functionality is prohibited: `trading_approval=false`,
    `real_orders=false`, `wallet=false`, `signing=false`, and
    `authenticated_CLOB_writes=false`.
12. Accepted MVP behavior is frozen unless a new reproducible P0/P1 is found.
    Historical Data Completion and PF1 are outside this sprint.
13. The C9 startup adapter is accepted because it returns the exact
    `SqliteStore` type, preserves migration/runtime initialization, and replaces
    only the startup integrity report with bounded `quick_check(1)` plus exact
    migration/table validation.
14. C9 Task Scheduler actions must run without `-ExecutionPolicy Bypass`, as
    required by the frozen C0/C1 contract. Integration removed that flag and
    added native registered-action verification; no shared MVP file changed.
