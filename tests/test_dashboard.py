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


if __name__ == "__main__":
    unittest.main()
