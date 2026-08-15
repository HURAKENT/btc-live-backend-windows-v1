### Task 5 report: read-only performance/system API

**Status:** DONE

**Scope implemented**

- Added `src/performance_query.py` with `PerformanceQueryService`.
- Registered five versioned resources:
  - `GET /api/v1/performance/status`
  - `GET /api/v1/performance/strategies`
  - `GET /api/v1/performance/strategies/{strategy_id}`
  - `GET /api/v1/performance/strategies/{strategy_id}/timeseries`
  - `GET /api/v1/performance/strategies/{strategy_id}/observations`
- Extended `/api/v1/health` with a compact performance system summary.
- Added focused API coverage in `tests/test_performance_api.py`.
- Extended `tests/test_mvp_api_dashboard.py` to cover route availability through the existing aiohttp app.

**Read-only boundaries**

- Query layer accepts only `SqliteReadStore`.
- API reads current complete materializations through `PerformanceRepository.read_current_materialization`.
- Metrics are returned from stored aggregate payloads verbatim; handlers do not calculate WR/ROI/PnL/drawdown/economics.
- Strategy identity/status metadata is read from canonical `reports/STRATEGY_47_STATUS_MATRIX.json`.
- `ALL` strategy detail is rejected with `PERFORMANCE_PORTFOLIO_TOTAL_FORBIDDEN`.

**Verification**

- RED observed:
  - `../../.venv/Scripts/python.exe -m unittest tests.test_performance_api -v`
  - Failed with 404s before route/service implementation.
  - `../../.venv/Scripts/python.exe -m unittest tests.test_mvp_api_dashboard.MvpApiDashboardTests.test_real_paper_state_is_exposed_to_attached_dashboard -v`
  - Failed because `/api/v1/health` lacked `performance`.
- GREEN:
  - `../../.venv/Scripts/python.exe -m unittest tests.test_performance_api tests.test_mvp_api_dashboard -v`
  - 15 tests passed.
  - `../../.venv/Scripts/python.exe -m unittest tests.test_runtime_core_integration -v`
  - 5 tests passed.
  - `git diff --check -- src/api.py src/performance_query.py tests/test_performance_api.py tests/test_mvp_api_dashboard.py`
  - Exit 0.

**Shared worktree note**

- Untracked Task 3/4 files were present and intentionally not staged by Task 5:
  - `src/performance_engine.py`
  - `src/performance_historical.py`
  - `tests/test_performance_engine.py`
  - `tests/test_performance_historical.py`
