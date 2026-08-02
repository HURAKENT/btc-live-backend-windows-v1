# Windows V1 Final Project Autonomous Operating Contract

## Role and terminal objective

Codex is the autonomous engineering manager and implementer for the remaining
BTC Daily Range Windows V1 scope on branch `codex/final-project-completion`.
The authoritative specification is the SHA-verified
`BTC_PROJECT_MASTER_CONTEXT_FOR_CODEX_V2.zip`; durable state lives in:

- `docs/FINAL_PROJECT_AUTONOMOUS_GOAL.md`;
- `docs/FINAL_PROJECT_PROGRESS.md`;
- `docs/FINAL_PROJECT_DECISION_LOG.md`;
- `docs/FINAL_PROJECT_RISK_REGISTER.md`;
- `reports/FINAL_PROJECT_ACCEPTANCE_MATRIX.md`;
- `reports/STRATEGY_47_STATUS_MATRIX.json`;
- `reports/DATA_COMPLETENESS_STATUS.json`.

Continue without a routine prompt until the exact terminal state:

`BTC_DAILY_RANGE_WINDOWS_V1_PROJECT_COMPLETE`.

## Historical boundary

C1 remains accepted and its canonical JSON/ZIP evidence is immutable. Historical
C2/C3 reports remain immutable; reproduced hardening findings are corrected and
accepted through new Phase 0 evidence rather than rewriting history.

## Engineering method

- Identify and reproduce the earliest load-bearing boundary before production
  changes.
- Use strict root cause -> RED -> minimal GREEN -> regression -> independent
  review.
- Preserve one SQLite writer and append-only/idempotent provenance.
- Prefer deterministic synthetic and loopback acceptance. Public read-only
  provider access is bounded and used only when local evidence cannot establish
  a required provider contract.
- Keep implementation and acceptance/evidence commits narrow and receipt-backed.
- Record every checkpoint, decision, risk and locked next step in durable files.
- Use subagents and isolated worktrees for independent tasks; never let parallel
  implementers edit the same checkout.
- After three material failed fixes at one boundary, require architecture review.

## Remaining scope

- Phase 0: verified baseline, recurring rollover and crash-safe cutover hardening,
  receipt-linked evidence.
- C4: exact rule sources/specs/evaluators/parity for all 47 identities.
- C5: 47/47 activation classification, independent of implementation status.
- C6: persistent exactly-once scheduling and recovered replay.
- C7: depth-evidenced paper execution and restart-safe accounting, minimum five
  shares.
- C8 and Stage D: versioned API, resumable events and backend-only dashboard.
- C9: reversible Windows operations, logs and backup/restore.
- C10: fail-closed failure injection.
- C11: 48 actual valid Windows hours under the durable observation protocol.

Strict A/PF1 rules must never be inferred from names or aggregate metrics. A
disabled activation never waives exact source, evaluator or parity work.

## Forbidden scope

- C12/Linux migration;
- real-money orders or live-money execution;
- wallet, private keys, signing or authenticated provider writes;
- unverified/fabricated strategy rules;
- force-push, changes to `main` or Git configuration.

`trading_approval=false` is invariant.

## Environment and manual boundary

- Codex runs through WSL2 with the repository under `/mnt/c` and may use the
  established Windows process wrapper for offline gates.
- Computer Use is not used for terminal automation.
- C11 elapsed time cannot be simulated. Its observer must be durable across UI
  closure, context compression and backend restart.
- Before a genuine manual checkpoint, finish safe code/tests/commit/push, prove
  clean `HEAD==origin/codex/final-project-completion`, persist the locked
  continuation, and provide one exact action and allowed run count.

## Completion

Completion requires C0-C11 and Stage D PASS, 47/47 exact rule/evaluator status,
zero unknown/unreviewed/unimplemented identities, paper accounting PASS,
real-money surfaces absent, `trading_approval=false`, C12 not started, verified
acceptance pack, clean worktree and `HEAD==origin`.

Stop only for a genuine manual/credential/destructive boundary, a frozen
contract conflict, two bounded non-progress cycles at one boundary, or complete
achievement of the final Goal.
