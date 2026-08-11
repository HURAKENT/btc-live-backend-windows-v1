# MVP Worker Handoff Template

Return one compact block with every field populated. Use `NONE` when verified
empty; do not use placeholders.

```text
WORKER:
BASE_COMMIT:
HEAD_COMMIT:
FILES_CHANGED:
INTERFACES_CONSUMED:
INTERFACES_PROVIDED:
TEST_COMMANDS:
TEST_RESULTS:
KNOWN_GAPS:
P0_P1_FINDINGS:
P2_P3_BACKLOG:
ASSUMPTIONS:
SHARED_CHANGE_REQUESTS:
INTEGRATION_ORDER:
DO_NOT_MERGE_IF:
```

Required merge guards: exact HUB base, changes limited to worker ownership,
focused tests GREEN, frozen interface unchanged, no recovered execution, and
`trading_approval=false`, `real_orders=false`, `wallet=false`, `signing=false`,
`authenticated_CLOB_writes=false`.
