from __future__ import annotations

import argparse
import asyncio
from urllib.parse import urlencode

from aiohttp import ClientSession


async def watch(base_url: str, after_event_id: int) -> None:
    last_seen = after_event_id
    async with ClientSession() as session:
        query = urlencode({"after_event_id": last_seen})
        url = f"{base_url.rstrip('/')}/ws/v1/events?{query}"
        async with session.ws_connect(url) as websocket:
            async for message in websocket:
                payload = message.json()
                event_id = payload["event_id"]
                event_type = payload["event_type"]
                if type(event_id) is not int or event_id <= last_seen:
                    raise ValueError("NON_MONOTONIC_EVENT_ID")
                print(f"{event_id}\t{event_type}", flush=True)
                last_seen = event_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Watch committed backend outbox events.",
    )
    parser.add_argument(
        "base_url",
        help="Loopback API base URL, for example http://127.0.0.1:8767",
    )
    parser.add_argument(
        "--after-event-id",
        type=int,
        default=0,
    )
    arguments = parser.parse_args()
    if arguments.after_event_id < 0:
        parser.error("--after-event-id must be non-negative")
    asyncio.run(watch(arguments.base_url, arguments.after_event_id))


if __name__ == "__main__":
    main()
