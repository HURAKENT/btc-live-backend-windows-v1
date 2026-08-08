# Final Project Progress

## Current state

- Goal status: `ACTIVE`.
- Current train: `C4_EXECUTABLE_RULES_AND_PARITY`.
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
- [ ] C4 executable rules and parity.
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
- C4 implementation status remains `NOT_STARTED`: verified discovery is not a
  frozen backend spec, evaluator or parity result.

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

Freeze the verified minimum C4 rule sources/specifications, then implement and
replay all 47 evaluators through strict RED → minimal GREEN → parity evidence.
Activation classification remains a separate C5 decision.
