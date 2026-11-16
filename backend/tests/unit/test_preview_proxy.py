"""Render proxy hops must preserve client identity without trusting forged prefixes."""

import pytest
from scripts.run_preview import PREVIEW_TRUSTED_PROXIES
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("peer", "forwarded", "expected"),
    [
        (("127.0.0.1", 8000), "203.0.113.44, 10.29.232.5", ("203.0.113.44", 0)),
        (
            ("127.0.0.1", 8000),
            "198.51.100.222, 203.0.113.44, 10.24.120.78, 10.29.232.5",
            ("203.0.113.44", 0),
        ),
        (
            ("127.0.0.1", 8000),
            "198.51.100.222, 2001:db8::44, 10.29.232.5",
            ("2001:db8::44", 0),
        ),
        (("127.0.0.1", 8000), "198.51.100.222, 203.0.113.44", ("203.0.113.44", 0)),
        (("127.0.0.1", 8000), "203.0.113.44", ("203.0.113.44", 0)),
        (("198.51.100.99", 8000), "203.0.113.44, 10.29.232.5", ("198.51.100.99", 8000)),
    ],
)
async def test_forwarding_preserves_the_rightmost_untrusted_client(peer, forwarded, expected):
    observed = {}

    async def app(scope, receive, send):
        observed["client"] = scope["client"]

    middleware = ProxyHeadersMiddleware(app, trusted_hosts=PREVIEW_TRUSTED_PROXIES)
    scope = {
        "type": "http",
        "client": peer,
        "scheme": "http",
        "headers": [(b"x-forwarded-for", forwarded.encode())],
    }
    await middleware(scope, None, None)

    assert observed["client"] == expected
