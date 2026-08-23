from __future__ import annotations

import json
import unittest
from copy import deepcopy

from src.performance_catchup_source import (
    fetch_gamma_daily_range_inventory,
    project_gamma_daily_range_inventory,
)


def _event(market_date: str, *, closed: bool, winner: int = 3) -> dict[str, object]:
    outcomes = [
        "<52,000", "52,000-54,000", "54,000-56,000", "56,000-58,000",
        "58,000-60,000", "60,000-62,000", "62,000-64,000",
        "64,000-66,000", "66,000-68,000", "68,000-70,000", ">70,000",
    ]
    markets = []
    for index, title in enumerate(outcomes):
        prices = ["1", "0"] if index == winner else ["0", "1"]
        markets.append(
            {
                "id": f"market-{market_date}-{index}",
                "conditionId": f"condition-{market_date}-{index}",
                "groupItemTitle": title,
                "outcomes": json.dumps(["Yes", "No"]),
                "outcomePrices": json.dumps(prices),
                "clobTokenIds": json.dumps(
                    [f"yes-{market_date}-{index}", f"no-{market_date}-{index}"]
                ),
                "active": True,
                "closed": closed,
            }
        )
    return {
        "id": f"event-{market_date}",
        "ticker": f"bitcoin-price-on-{market_date}",
        "slug": f"bitcoin-price-on-{market_date}",
        "active": True,
        "closed": closed,
        "endDate": f"{market_date}T16:00:00Z",
        "markets": markets,
    }


class GammaDailyRangeCatchupSourceTests(unittest.TestCase):
    def test_projects_resolved_pending_and_complete_absence_without_guessing(self) -> None:
        projection = project_gamma_daily_range_inventory(
            events=[
                _event("2026-07-08", closed=True, winner=5),
                _event("2026-07-09", closed=False),
            ],
            start_date="2026-07-08",
            end_date="2026-07-10",
            observed_at_ms=10_000,
            inventory_complete=True,
        )

        self.assertEqual(len(projection.markets), 2)
        payloads = [json.loads(event.payload_json) for event in projection.events]
        self.assertEqual(
            [event.event_type for event in projection.events],
            [
                "POLYMARKET_DAILY_RANGE_MARKET_RESOLVED",
                "POLYMARKET_DAILY_RANGE_MARKET_ABSENT",
            ],
        )
        self.assertEqual(payloads[0]["winning_bucket_identity"], "60,000-62,000")
        self.assertEqual(payloads[0]["winning_bucket_count"], 1)
        self.assertEqual(payloads[1]["market_date"], "2026-07-10")

    def test_incomplete_inventory_never_fabricates_absence(self) -> None:
        projection = project_gamma_daily_range_inventory(
            events=[],
            start_date="2026-07-08",
            end_date="2026-07-08",
            observed_at_ms=10_000,
            inventory_complete=False,
        )
        self.assertEqual(projection.events, ())

    def test_unparseable_in_range_event_never_fabricates_absence(self) -> None:
        malformed = _event("2026-07-08", closed=True)
        malformed["ticker"] = "unexpected-public-identifier"
        malformed["slug"] = "unexpected-public-identifier"

        with self.assertRaisesRegex(
            ValueError,
            "GAMMA_DAILY_RANGE_MARKET_DATE_UNPARSEABLE",
        ):
            project_gamma_daily_range_inventory(
                events=[malformed],
                start_date="2026-07-08",
                end_date="2026-07-08",
                observed_at_ms=10_000,
                inventory_complete=True,
            )

    def test_contradictory_event_date_never_fabricates_absence(self) -> None:
        contradictory = _event("2026-07-07", closed=True)
        contradictory["endDate"] = "2026-07-08T16:00:00Z"

        with self.assertRaisesRegex(
            ValueError,
            "GAMMA_DAILY_RANGE_MARKET_DATE_CONFLICT",
        ):
            project_gamma_daily_range_inventory(
                events=[contradictory],
                start_date="2026-07-08",
                end_date="2026-07-08",
                observed_at_ms=10_000,
                inventory_complete=True,
            )

    def test_ambiguous_winner_and_duplicate_date_fail_closed(self) -> None:
        ambiguous = _event("2026-07-08", closed=True, winner=5)
        ambiguous["markets"][6]["outcomePrices"] = json.dumps(["1", "0"])
        with self.assertRaisesRegex(ValueError, "GAMMA_DAILY_RANGE_WINNER_AMBIGUOUS"):
            project_gamma_daily_range_inventory(
                events=[ambiguous], start_date="2026-07-08",
                end_date="2026-07-08", observed_at_ms=10_000,
                inventory_complete=True,
            )


class _Response:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return self.payload


class _Session:
    def __init__(self, payloads: list[object]) -> None:
        self.payloads = list(payloads)
        self.requests: list[dict[str, object]] = []

    def get(self, _url: str, *, params: dict[str, object], allow_redirects: bool):
        self.requests.append(dict(params))
        return _Response(self.payloads.pop(0))


class GammaInventoryFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_closed_and_open_keysets_to_complete_cursor_end(self) -> None:
        session = _Session(
            [
                {"events": [{"id": "closed-1"}], "next_cursor": "next"},
                {"events": [{"id": "closed-2"}], "next_cursor": ""},
                {"events": [{"id": "open-1"}], "next_cursor": ""},
            ]
        )

        inventory = await fetch_gamma_daily_range_inventory(
            session=session,
            gamma_events_url="http://provider/events/keyset",
            start_date="2026-07-08",
            end_date="2026-07-10",
            max_pages_per_state=3,
        )

        self.assertTrue(inventory.complete)
        self.assertEqual([row["id"] for row in inventory.events], ["closed-1", "closed-2", "open-1"])
        self.assertEqual([row["closed"] for row in session.requests], ["true", "true", "false"])
        self.assertEqual(session.requests[1]["after_cursor"], "next")

    async def test_cursor_loop_or_page_bound_fails_closed(self) -> None:
        session = _Session(
            [
                {"events": [], "next_cursor": "again"},
                {"events": [], "next_cursor": "again"},
            ]
        )
        with self.assertRaisesRegex(ValueError, "GAMMA_CATCHUP_CURSOR_LOOP"):
            await fetch_gamma_daily_range_inventory(
                session=session,
                gamma_events_url="http://provider/events/keyset",
                start_date="2026-07-08",
                end_date="2026-07-10",
                max_pages_per_state=3,
            )

        duplicate = deepcopy(_event("2026-07-08", closed=True))
        duplicate["id"] = "different-event"
        with self.assertRaisesRegex(ValueError, "GAMMA_DAILY_RANGE_DATE_AMBIGUOUS"):
            project_gamma_daily_range_inventory(
                events=[_event("2026-07-08", closed=True), duplicate],
                start_date="2026-07-08", end_date="2026-07-08",
                observed_at_ms=10_000, inventory_complete=True,
            )


if __name__ == "__main__":
    unittest.main()
