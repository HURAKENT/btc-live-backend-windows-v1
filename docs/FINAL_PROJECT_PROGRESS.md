# Final Project Progress

## Current state

- Goal status: `ACTIVE`.
- Current train: `C5_ACTIVATION_CLASSIFICATION`.
- Verified baseline: `579289b550cff3793a74bbafba0d8e78670f0c2a`.
- Integration branch: `codex/final-project-completion`.
- Branch published: yes.
- Last verified integration commit: `b435e0e` (`test: record accepted Phase 0 baseline`).
- Manual action required: no.
- Trading approval: `false`.

## Package verification

- ZIP SHA-256: `0497121f93bbd549b3e9f6b8a9e76c8c1d3d411bdc9c7c71abfe98c6aa7c1855`.
- Outer ZIP CRC: PASS.
- Outer SHA256SUMS: 58 checked, 0 missing, 0 mismatched.
- Manifest: 57 checked, 0 size/hash mismatches.
- All 14 embedded evidence ZIPs passed CRC.
- Embedded checksum gaps are tracked in the risk register and do not authorize
  inferred rule math.

## Gate checklist

- [x] Phase 0 baseline offline gate and hypothesis reproduction.
- [x] C4 executable rules and parity.
- [ ] C5 activation classification.
- [ ] C6 persistent scheduler/replay.
- [ ] C7 paper execution.
- [ ] C8 stable API.
- [ ] Stage D dashboard.
- [ ] C9 Windows operations.
- [ ] C10 failure injection.
- [ ] C11 actual 48-hour observation.
- [ ] Final acceptance pack and terminal closure.

## C4 source-discovery preparation

- Bounded local search completed read-only while Phase 0 corrections were
  isolated in a linked worktree.
- V1 exact source/ledger status: `34/34 SOURCE_VERIFIED`, including Strict A
  and PF1 source modules and primary ledger hashes.
- V2 exact composed source status: `13/13 SOURCE_VERIFIED`; activation remains
  `DISABLED_RESEARCH_ONLY` and no evaluator/parity claim has been made.
- Primary ledger hashes independently rechecked: `1deb95da...d4aa1`,
  `4765ae6f...a950`, `6d6d71d0...5361`, `74e0c479...1440`.
- C4 implementation and acceptance are now complete; source discovery alone
  was not used as acceptance evidence.

## C4 accepted strategy gate

- Gate: `C4_STRATEGY_47_ACCEPTANCE_PASS`.
- Exact immutable rule/spec sources: `47/47`; trusted rule-pack SHA-256:
  `9d402e9a1e2e0dd2fdae3443e3641dfb777efdc29b88ae14b529689972e8d727`.
- Trusted dispatcher: `47/47` ordered registry identities (`34 V1`, `13 V2`),
  with fail-closed evaluator/input-schema/checkpoint binding.
- V1 full-decision historical parity: `34/34`, split into non-overlapping
  Early Horizon (`17`), Early Confidence (`11`) and Confirmation/Basket (`6`)
  source populations. The gate evaluates `5,135` identity decisions across
  `1,647` sanitized fixture records, including accepted and rejected rows.
- V2 volatility-overlay full-decision parity: `13/13`, `694` rows; outcome,
  five-share turnover and PnL are independently recomputed.
- C4 does not activate strategies or perform paper execution. Depth-backed
  executable eligibility and accounting remain C7 responsibilities.
- Acceptance report SHA-256:
  `641daca9fc68689f60baaf857a3a8711006802d9d1e28c8b5b09062f73180e5b`.
- Strategy status matrix SHA-256:
  `7ab18b6782f359177d7dde78ed4caa51cd7166eb9055d75755fd8919466ff7fc`.
- Provider requests, backend runs and Task 14 runs for C4: `0`.
- `trading_approval=false`.

## Phase 0 accepted baseline

- Gate: `BTC_DAILY_RANGE_WINDOWS_V1_PHASE0_PASS`.
- Source/harness commit under test:
  `33467ccf4cc3465e34acf2c5770c5ab14db34a0e`.
- Evidence commit: `b435e0e`.
- Focused tests: `101/101`; full offline suite: `647/647`.
- Receipt SHA-256:
  `4e1ceb2a1276f46abf5a8d2cdb3e60d0cdf6095a770ffc54e4704a4c2954155d`.
- Report SHA-256:
  `c7172837fc337a5d991c50d8c95833b081b5a5e006c2733693ef98100102574b`.
- Pack SHA-256:
  `b6bd4965c1bd0039de82c252b489e0c3e2dc4717ec78fd5b0653ab3faf6dee39`.
- Reproduced and corrected: recurring A→B→C re-arm, durable committed-cutover
  restart identity, pre/post-commit stream liveness race, receipt binding and
  sanitized cross-platform command evidence.
- Pack CRC, internal SHA256SUMS, path traversal, duplicate-entry and stored
  path/security scans passed. Historical C1/C2/C3 hashes match the receipt.
- Task 14 and public-provider requests were not performed by the Phase 0
  harness. `trading_approval=false`.

## Locked next step

Classify activation for every one of the 47 identities using the frozen C5
policy. C4 implementation/parity status must not be used as automatic
authorization for paper activation.
