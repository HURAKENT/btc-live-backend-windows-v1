from __future__ import annotations

from pathlib import Path

from aiohttp import web


_STATIC_ROOT = Path(__file__).with_name("dashboard_static")
_CSP = (
    "default-src 'self'; connect-src 'self' ws: wss:; "
    "script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def attach_dashboard_routes(app: web.Application) -> None:
    """Attach the read-only MVP dashboard routes to an aiohttp application."""
    if not isinstance(app, web.Application):
        raise ValueError("INVALID_DASHBOARD_APP")
    app.router.add_get("/dashboard", _dashboard)
    app.router.add_get("/dashboard/app.js", _javascript)
    app.router.add_get("/dashboard/styles.css", _stylesheet)


async def _dashboard(_request: web.Request) -> web.Response:
    return _asset_response(
        "index.html",
        content_type="text/html",
        headers={"Content-Security-Policy": _CSP},
    )


async def _javascript(_request: web.Request) -> web.Response:
    return _asset_response("app.js", content_type="text/javascript")


async def _stylesheet(_request: web.Request) -> web.Response:
    return _asset_response("styles.css", content_type="text/css")


def _asset_response(
    filename: str,
    *,
    content_type: str,
    headers: dict[str, str] | None = None,
) -> web.Response:
    response_headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        **(headers or {}),
    }
    return web.Response(
        body=_STATIC_ROOT.joinpath(filename).read_bytes(),
        content_type=content_type,
        headers=response_headers,
    )
