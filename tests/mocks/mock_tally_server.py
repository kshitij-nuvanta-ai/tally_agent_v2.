"""
Lightweight HTTP server mimicking TallyPrime's behavior.
Delegates pattern-matching to tally_bridge.mock_handler
for a single canonical implementation.
"""

from aiohttp import web
from tally_bridge.mock_handler import mock_tally_request


async def handle_tally_request(request: web.Request) -> web.Response:
    body = await request.text()
    result = mock_tally_request(body)
    return web.Response(text=result, content_type="text/xml")


def create_mock_tally_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/", handle_tally_request)
    return app
