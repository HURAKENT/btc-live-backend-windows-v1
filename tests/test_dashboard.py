from __future__ import annotations

import unittest
from importlib import import_module

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            dashboard = import_module("src.dashboard")
        except ModuleNotFoundError:
            self.fail("src.dashboard is not implemented")
        app = web.Application()
        dashboard.attach_dashboard_routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_attachable_dashboard_serves_complete_same_origin_ui(self):
        response = await self.client.get("/dashboard")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/html")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            response.headers["Content-Security-Policy"],
            "default-src 'self'; connect-src 'self' ws: wss:; "
            "script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        html = await response.text()
        for element_id in (
            "connection-status",
            "market-id",
            "market-date",
            "system-health",
            "source-health",
            "strict-a-signal",
            "execution-readiness",
            "block-reason",
            "paper-position",
            "paper-fills",
            "bankroll",
            "realized-pnl",
            "unrealized-pnl",
            "incidents",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', html)
        self.assertIn('href="/dashboard/styles.css"', html)
        self.assertIn('src="/dashboard/app.js"', html)

        css_response = await self.client.get("/dashboard/styles.css")
        self.assertEqual(css_response.status, 200)
        self.assertEqual(css_response.content_type, "text/css")
        self.assertIn("color-scheme: dark", await css_response.text())

        script_response = await self.client.get("/dashboard/app.js")
        self.assertEqual(script_response.status, 200)
        self.assertEqual(script_response.content_type, "text/javascript")
        script = await script_response.text()
        self.assertNotIn("binance", script.lower())
        self.assertNotIn("polymarket", script.lower())

    async def test_browser_consumer_uses_frozen_bootstrap_and_resumable_envelope(self):
        script = await (await self.client.get("/dashboard/app.js")).text()
        for frozen_field in (
            "interface_version",
            "current_market_identity",
            "health",
            "sources",
            "strict_a_signal",
            "execution_readiness",
            "paper_account",
            "paper_positions",
            "paper_fills",
            "incidents",
            "last_event_id",
        ):
            with self.subTest(frozen_field=frozen_field):
                self.assertIn(frozen_field, script)
        for envelope_field in (
            "event_id",
            "topic",
            "event_type",
            "payload",
            "created_at_ms",
        ):
            with self.subTest(envelope_field=envelope_field):
                self.assertIn(envelope_field, script)
        for topic in (
            "paper.fill",
            "paper.position",
            "paper.account",
            "paper.readiness",
        ):
            with self.subTest(topic=topic):
                self.assertIn(topic, script)
        self.assertIn('fetch("/api/v1/bootstrap"', script)
        self.assertIn('after_event_id=${state.last_event_id}', script)
        self.assertIn("scheduleReconnect", script)
        self.assertIn("BACKEND_UNAVAILABLE", script)
        self.assertIn("NO_DATA", script)

    async def test_dashboard_exposes_active_backend_computed_performance_workspace(self):
        html = await (await self.client.get("/dashboard")).text()
        for element_id in (
            "performance-transport-status",
            "performance-health-status",
            "performance-blocking-reason",
            "performance-freshness",
            "performance-catchup",
            "performance-view-selector",
            "strategies-body",
            "strategy-detail",
            "strategy-detail-title",
            "strategy-detail-labels",
            "strategy-metrics",
            "performance-provenance",
            "cumulative-series",
            "monthly-series",
            "rolling-series",
            "recent-resolutions",
            "recent-decisions",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', html)

        for source_view in ("HISTORICAL", "FORWARD", "COMBINED"):
            with self.subTest(source_view=source_view):
                self.assertIn(f'data-source-view="{source_view}"', html)

        script = await (await self.client.get("/dashboard/app.js")).text()
        for endpoint in (
            "/api/v1/performance/status",
            "/api/v1/performance/strategies",
            "/api/v1/performance/strategies/${encodeURIComponent(strategyId)}",
            "/timeseries?view=${encodeURIComponent(state.performanceView)}",
            "/observations?limit=25",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, script)
        for backend_metric in (
            "opportunity_count",
            "accepted_signal_count",
            "resolved_signal_count",
            "win_rate_numerator",
            "win_rate_denominator",
            "win_rate_ratio",
            "pnl_usd_micros",
            "roi_numerator_usd_micros",
            "roi_denominator_usd_micros",
            "roi_ratio",
            "annualized_return_ratio",
            "annualized_return_reason_code",
            "max_drawdown_usd_micros",
            "longest_win_streak",
            "longest_loss_streak",
        ):
            with self.subTest(backend_metric=backend_metric):
                self.assertIn(backend_metric, script)
        self.assertNotIn("calculateWinRate", script)
        self.assertNotIn("calculateRoi", script)
        self.assertNotIn("calculateCombined", script)
        self.assertIn('setPerformanceTransport("REFRESHING", "warn")', script)
        self.assertIn('setPerformanceTransport("BACKEND_UNAVAILABLE", "bad")', script)
        self.assertIn("database_health", script)
        self.assertIn("activation_status", script)
        self.assertIn("detail.version", script)
        self.assertIn("parent_strategy_id", script)
        self.assertIn("scoring_status", script)
        self.assertIn("source_layer", script)
        self.assertIn("window.setInterval", script)
        self.assertIn("30_000", script)

    async def test_performance_ui_defaults_combined_and_has_explicit_series_ordering(self):
        html = await (await self.client.get("/dashboard")).text()
        script = await (await self.client.get("/dashboard/app.js")).text()
        self.assertIn('data-source-view="COMBINED" aria-pressed="true"', html)
        self.assertIn('performanceView: "COMBINED"', script)
        self.assertIn("chronologicalSeriesRows", script)
        self.assertIn("recentFirstSeriesRows", script)
        self.assertIn("window_kind", script)
        self.assertIn("as_of_date", script)
        self.assertIn("original_raw_observation_count", script)
        self.assertIn("recovered_raw_observation_count", script)
        self.assertIn("forward_raw_observation_count", script)


if __name__ == "__main__":
    unittest.main()
