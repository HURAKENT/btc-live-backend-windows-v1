# External DATA_ROOT Production Cutover Implementation Plan

> **Execution authority:** `/mnt/c/Users/gegos/Downloads/BTC_DAILY_RANGE_CODEX_GOAL_COMPLETE_EXTERNAL_DATA_TRANSITION_v1.md` is the approved end-to-end specification. Execute this plan autonomously through final verification, integration, and non-force push; do not request intermediate design approval.

**Goal:** Move the active Windows backend from repo-local mutable runtime state to the existing Stage A external `DATA_ROOT` contract while preserving complete durable production continuity and rollback evidence.

**Architecture:** Keep Python `src/runtime_paths.py` as the single path resolver. Remove only the transitional explicit database/log arguments from both Windows PowerShell launchers. Use the existing SQLite online-backup/restore tooling, Windows Task Scheduler lifecycle, immutable ORIGINAL seed, and cursor-driven recovery mechanisms; add no subsystem and change no domain/schema/security semantics.

**Stack:** Python 3.12, SQLite WAL, `unittest`, PowerShell, Windows Task Scheduler, Git worktrees.

---

### Task 1: Freeze fresh initial evidence

- Verify canonical/local/remote commits, worktrees, dirty state, exact Scheduled Task action/settings, backend process/listener/API state, old runtime files, and external target availability.
- With no writer, run `windows_database.py validate` and bounded SQL probes for migrations, representative counts, provenance, paper state, cursors, and latest identities.
- Record that full source hashes are authoritative only after quiesced backup creation.

### Task 2: End the transitional launcher pin using TDD

**Files:**
- Modify: `tests/test_external_runtime_paths.py`
- Modify: `tests/test_live_contract_smoke.py`
- Modify: `scripts/C9_RUN_BACKEND.ps1`
- Modify: `scripts/RUN_BACKEND_SAFE.ps1`
- Modify: `README.md`
- Modify: `AGENTS.md`

- First make launcher contract tests require an argument-free `run_windows_backend.py` invocation and reject repo-local DB/log arguments; run them and observe failure caused by the existing pin.
- Remove only `$DatabasePath`, `$LogPath`, `--database-path`, and `--log-path` from both launchers.
- Update current operational wording from pre-cutover pin to external default.
- Run path, launcher, security, compile, and diff checks.

### Task 3: Prove absent-DB bootstrap safely

- Use a fresh temporary external data root with no initial DB.
- Run the normal entrypoint/startup seam and existing ORIGINAL seed acceptance/parity tests.
- Verify directories/migrations, exact ORIGINAL seed counts/hashes, no dependency on nine external AHR datasets, no RECOVERED-to-FORWARD drift, and no fabricated historical signal/paper state.
- Exercise existing public-provider recovery seams and, where needed for a live claim, bounded unauthenticated provider reads.

### Task 4: Prove partial-DB recovery and idempotency

- Reuse the closest existing validated recovery fixture/older snapshot contract; do not mutate production.
- Demonstrate cursor-based gap detection, preservation of valid state, RECOVERED classification, no synthetic FORWARD, materialization convergence, and repeat-run idempotency.
- If a directly blocking defect appears, reproduce RED, implement one minimal fix, and rerun focused regressions.

### Task 5: Pre-cutover release gate and isolated commit

- Run the combined targeted matrix, security guard check, compile, and `git diff --check`.
- Verify no strategy, resolution, schema, or security change.
- Commit the clean tested isolated branch and record its SHA.
- Recheck canonical and remote branch state before maintenance.

### Task 6: Quiesce the exact production runtime

- Export the exact Scheduled Task XML/settings/state outside the repository.
- Disable only `BTC Daily Range Backend V1`.
- Gracefully stop only its process chain if running and verify no BTC process/listener/writer remains.

### Task 7: Create and validate rollback backup

- Create timestamped external directories under `C:\Users\gegos\Documents\BTC Daily Range`.
- Use `windows_database.py backup` on the quiesced repo-local DB.
- Record source/backup size, SHA-256, UTC time, migration/quick-check, complete table logical hashes, representative counts, provenance, paper state, and cursors.
- Run a restore drill to a separate copy and compare complete durable logical hashes.

### Task 8: Restore the active external database

- Use `windows_database.py restore-drill` to create `runtime\btc_daily_range.sqlite3` from the validated backup.
- Verify local non-cloud/non-reparse placement, quick/schema integrity, complete logical hashes, counts, provenance, signals/paper, and cursors against the quiesced source.

### Task 9: Integrate exact tested cutover commit

- Confirm canonical tracked tree is clean apart from the known untracked legacy runtime directory and canonical HEAD remains the reviewed ancestor.
- Fast-forward `codex/final-project-completion` to the exact tested cutover commit; do not push yet.
- Rerun the launcher/path smoke checks from canonical.

### Task 10: Start production via Task Scheduler

- Enable and start the exact task through its unchanged canonical Task Scheduler action.
- Verify one logical launcher/backend chain, canonical checkout command, listener/API health, active external DB/log paths, and no old repo-local DB handle.
- Allow bounded cursor-driven catch-up and collect current market/source evidence.

### Task 11: Post-cutover continuity/security acceptance

- Revalidate target DB health, migrations, pre-cutover durable logical-hash subset/monotonic append-only continuity, ORIGINAL/RECOVERED/FORWARD categories, signals/paper, performance, and five false security guards.
- Distinguish legitimate new prospective writes from pre-cutover state; fail closed on any provenance ambiguity.

### Task 12: Archive legacy repo-local mutable runtime state

- Classify bounded `data/` contents, preserve every DB/log/backup/unknown item under timestamped external `archive\legacy_repo_runtime_*`, and verify important hashes/sizes.
- Confirm no active process references repo-local paths, then remove repo-local mutable copies from the checkout without deleting any only copy.

### Task 13: Final acceptance, footprint, and publication

- Run the smallest sufficient combined code/ORIGINAL/recovery/storage/runtime/security matrix and `git diff --check`.
- Report tracked bytes, external `DATA_ROOT` bytes, and physical checkout split into tracked, `.git`, `.venv`, `.worktrees`, and other developer artifacts.
- Commit any required final tracked documentation change, verify canonical clean, push normally to `origin/codex/final-project-completion`, fetch/ls-remote fresh, and prove exact SHA with divergence `0 0`.
- Retain rollback backup/task export/archive and return the exact 24-section final report beginning with `GOAL_COMPLETE` only if every definition item is proven.
