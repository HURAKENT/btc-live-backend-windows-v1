# C2-C3 Autonomous Operating Contract

## Role and terminal objective

Codex is the autonomous engineering manager and implementer for C2 Recovery
Hardening and C3 Market Rollover on branch `codex/c2-c3`. Work continues
without a new prompt until all three gates are established:

- `C2_RECOVERY_HARDENING_PASS`;
- `C3_MARKET_ROLLOVER_PASS`;
- `BTC_LIVE_BACKEND_WINDOWS_V1_C2_C3_PASS`.

The final Goal state is `C2_C3_AUTONOMOUS_GOAL_COMPLETE`.

## Historical C1 boundary

C1 remains accepted and immutable. Canonical history is recorded in:

- `docs/C1_AUTONOMOUS_GOAL.md`;
- `docs/C1_GOAL_PROGRESS.md`;
- `reports/C1_DOWNTIME_ACCEPTANCE.json`;
- `reports/C1_FINAL_ACCEPTANCE.json`;
- `artifacts/C1_ACCEPTANCE_PACK.zip`.

C2-C3 work must not rewrite those acceptance artifacts or reinterpret C1 as
trading approval.

## Engineering method

- Identify the earliest load-bearing boundary before changing production.
- Use strict RED -> minimal GREEN -> regression verification.
- Preserve one SQLite writer and append-only/idempotent evidence semantics.
- Prefer deterministic synthetic and loopback acceptance over wall-clock waits.
- Record decisions and checkpoints in the canonical C2-C3 documents.
- Keep production fixes and acceptance/evidence commits narrow.
- A third production correction at the same release boundary requires
  `ARCHITECTURE_REVIEW_REQUIRED`.

## Scope and safety

Allowed scope is persistent recovery completeness, cutover buffering,
reconciliation, incident evidence, recovered evaluations, active/next market
lifecycle, subscription migration, and deterministic daily-market rollover.

Forbidden scope:

- C4 or later milestones;
- Registry 47 execution;
- paper execution;
- real orders or live-money;
- wallet, private keys, signing, authentication, or private provider APIs;
- dashboard expansion;
- migration without a proven schema necessity, RED restart/backup evidence,
  and an explicit decision-log entry.

`trading_approval` remains `false` for every C2-C3 report and result.

## Environment and manual boundary

- Codex runs through WSL2 with the repository under `/mnt/c`.
- Windows offline commands may use the established bounded process wrapper.
- Computer Use is not used for terminal automation.
- A public-provider run is allowed only when deterministic/loopback evidence
  cannot establish a required provider boundary. At most one bounded public
  read-only recovery acceptance may be requested.
- Real daily rollover waiting is forbidden; C3 uses deterministic replay.

Before any manual Windows checkpoint, Codex must finish code/tests/commit/push,
prove `HEAD==origin/codex/c2-c3` and a clean tree, update
`docs/C2_C3_GOAL_PROGRESS.md`, provide one command and one allowed run count,
and stop without asking the user to choose the next engineering step.

## Git and completion

- Never change `main`, force-push, or alter Git configuration.
- Completion claims require fresh focused, full offline, compile, dependency,
  scope, security, report, and pack verification.
- Final state requires `HEAD==origin/codex/c2-c3`, clean tracked/index/untracked
  state, C1 artifacts unchanged, C2 and C3 reports PASS, and C4 not started.

Codex stops only for a true manual/credential/destructive boundary, a frozen
contract conflict, two bounded non-progress cycles at one boundary, or complete
achievement of the C2-C3 Goal.
