# MVP Decisions

Interface version: `BTC_DAILY_RANGE_MVP_V1`. Decisions are Manager-owned; a
change requires a dated superseding entry and, for interface changes, a new
version.

1. **MVP_EXPRESS:** first functional slice is Strict Favorite A -> current
   executable input -> local paper state -> existing REST/WS extension ->
   functional dashboard.
2. **Strict A first:** `YES_STRICT_A_OPERATIONAL`; T60 primary, T30 fallback only
   without a logical execution/intent/position for that market date.
3. **Historical Data Completion removed from critical path:** do not repeat the
   7.27M-row import; receipt-less external SQLite/WAL/SHM are research only.
4. **PF1 deferred:** missing/unproved production bundle is
   `BLOCKED_MISSING_PRODUCTION_MODEL_BUNDLE`; never reconstruct or guess it.
5. **One logical Strict A execution per date:** idempotency boundary is
   `strict-a:<market_date>` across intent and position.
6. **Fixed baseline:** exactly five shares; default paper bankroll USD 1000.
7. **Recovered execution forbidden:** recovered evaluation may exist, but cannot
   create intent, fill, or position.
8. **Existing API reused:** extend aiohttp bootstrap/read seams and the existing
   transactional outbox/resumable WebSocket; do not build another framework.
9. **Simple dashboard:** server-attached HTML/CSS/JS is sufficient; no frontend
   framework, router, design system, or multi-page app for MVP.
10. **Review budget:** one worker RED/GREEN cycle, one self-review, focused
    verification, one Manager boundary review. One normal P0/P1 remediation
    iteration before reassessment; P2/P3 go to backlog.
11. **Real money prohibited:** `trading_approval=false`, `real_orders=false`,
    `wallet=false`, `signing=false`, `authenticated_CLOB_writes=false`.
12. **Shared ownership:** only the Manager changes shared models, storage/API
    wiring, runtime orchestration, and config. Workers return shared requests.
13. **Verification cadence:** focused worker tests; one fresh full Windows suite
    only after current input, C7, API extension, and dashboard are integrated.
