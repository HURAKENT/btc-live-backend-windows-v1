# Final Project Risk Register

| ID | Risk | Current evidence | Control | Status |
|---|---|---|---|---|
| R-001 | C3 may not re-arm after one rollover | V2 review hypothesis only | Reproduce A→B→C with real orchestrator/storage path before patching | OPEN |
| R-002 | Mid-cutover crash recovery may be incomplete | V2 review hypothesis only | Inject crashes at durable cutover boundaries and same-DB restart | OPEN |
| R-003 | C2/C3 PASS reports may have weak receipt linkage | Static report concern only | Compare executable logs/commits/pack contents; classify evidence | OPEN |
| R-004 | Exact Strict A/PF1 selector source may be absent | Bounded read-only search found 34/34 V1 sources; primary ledgers and Strict A/PF1 source hashes independently match | Freeze only the verified minimum source/spec and prove trade-level parity; never infer | SOURCE_DISCOVERY_VERIFIED |
| R-005 | Embedded analysis packs omit some files referenced by their internal checksum lists | CRC passes, but EARLY_HORIZON references five absent files; EARLY_CONFIDENCE references `FINAL_REPORT.json`; NO_FADE pack references omitted cache files | Treat outer hashes as package integrity only; locate authoritative local artifacts by hash before C4 | OPEN |
| R-006 | Existing schema may not support C7/import ledger | Current schema v2 has nine C1–C3 tables | Require RED persistence/restart evidence and versioned migration decision before schema change | OPEN |
| R-007 | C11 may be interrupted by sleep/session loss | Real 48h requirement | Durable heartbeat, monotonic validity, single observer and resumable state | OPEN |
| R-008 | Forbidden real-money surface could enter through paper abstractions | C7 adds execution/accounting concepts | Static/behavioral forbidden-surface audit; no auth/write adapter; trading_approval=false | OPEN |
| R-009 | Source workspaces contain ancillary manifest variance | Early Horizon launcher differs from its manifest; volatility `src/sources.py` differs while `sources.py.before_numeric_tolerance_hotfix` matches the locked hash | Do not freeze the mismatched files; record exact candidate hashes and use only independently locked evaluator/contract inputs | OPEN |
