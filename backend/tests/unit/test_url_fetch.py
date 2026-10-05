"""§22's `url` source — and the SSRF guard that makes a server-side fetch safe to offer.

`app/services/url_fetch.py` is the only code in this project that issues a request to an address
a **user** chose, and its whole difficulty is that the API can reach addresses the person who
typed the URL cannot. So most of this file is about refusal: the scheme, the resolved address,
a redirect that moves the request somewhere unchecked, a body with no end, a type nothing can
read. Each refusal is asserted twice where it matters — once that it raises, and once that *no
request was made*, because a guard that refuses after connecting has already done the thing it
exists to prevent.

`httpx.MockTransport` is the socket, and `_resolve` is replaced rather than the DNS: the guard's
decision is about addresses, so scripting them is what makes each case a decision rather than an
accident of the machine the suite runs on — `localhost` is not universally `127.0.0.1`, and a
test that depended on it would be a test of the developer's resolver.
"""

from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from app.services import url_fetch
from app.services.url_fetch import MAX_REDIRECTS, UrlFetchError

pytestmark = pytest.mark.unit

#: A public address, used as the answer for any host a test does not script. `93.184.216.34` is
#: `example.com`'s, chosen because it is a real globally routable address rather than a made-up
#: one — `is_global` is the property under test and a fictional address is a guess about it.
PUBLIC = "93.184.216.34"

PAGE = b"<html><body><p>Refunds take 5 working days.</p></body></html>"


class Wire:
    """A stand-in socket: the responses a test scripted, and the URLs it was asked for.

    A queue rather than a single answer, because this module genuinely makes several requests
    where `app/ai/groq.py`'s provider makes one — a redirect chain is the case the guard is
    built around, and a test of it has to be able to answer twice.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._responses: list[httpx.Response] = []

    def answers(
        self,
        status_code: int = 200,
        body: bytes = PAGE,
        *,
        content_type: str = "text/html",
        location: str | None = None,
    ) -> None:
        headers = {"content-type": content_type} if content_type else {}
        if location is not None:
            headers["location"] = location
        self._responses.append(httpx.Response(status_code, headers=headers, content=body))

    def fails(self, exc: Exception) -> None:
        """The next request raises rather than answering — a refused connection, a TLS error."""
        self._responses.append(exc)  # type: ignore[arg-type]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert self._responses, f"the test scripted no response for {request.url}"
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        outcome.request = request
        return outcome


def _resolves_to(*addresses: str) -> Callable[[str], object]:
    """An async stand-in for `_resolve` that always answers with `addresses`.

    Async because the real one is — it runs `getaddrinfo` in a worker thread — and a synchronous
    double would make the guard's `await` a lie the test never exercises.
    """

    async def resolve(_host: str) -> list[str]:
        return list(addresses)

    return resolve


def _resolves_by_host(
    mapping: dict[str, list[str]], default: str = PUBLIC
) -> Callable[[str], object]:
    """A resolver that answers per host, for the tests where the two hops differ.

    `_resolves_to` answers the same way for every name, which is exactly wrong for a redirect
    test: the *point* of that case is that the first hop is public and the second is not, and a
    resolver that refused both would pass the test for the wrong reason — it would be asserting
    that nothing is ever fetched.
    """

    async def resolve(host: str) -> list[str]:
        return mapping.get(host, [default])

    return resolve


@pytest.fixture
async def wire(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Wire]:
    """Point the module at a `MockTransport` whose every host resolves to a public address.

    `_shared_client` is replaced rather than `_client` — unlike `test_ai_groq.py`, the module
    reaches its client through a function that owns a per-loop cache, and replacing the function
    is the seam that exists. Everything else about the request is the module's own.
    """
    socket = Wire()
    client = httpx.AsyncClient(transport=httpx.MockTransport(socket.handle))
    monkeypatch.setattr(url_fetch, "_shared_client", lambda: client)
    monkeypatch.setattr(url_fetch, "_resolve", _resolves_to(PUBLIC))
    try:
        yield socket
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_a_public_page_comes_back_with_its_bytes_and_its_type(wire: Wire) -> None:
    """§22's `url` source, end to end: one request, and the body handed to the extractor."""
    wire.answers(body=PAGE)

    page = await url_fetch.fetch("https://example.com/refunds")

    assert page.body == PAGE
    assert page.content_type == "text/html"
    assert page.url == "https://example.com/refunds"
    assert [str(request.url) for request in wire.requests] == ["https://example.com/refunds"]


async def test_the_content_type_is_normalised_before_it_is_checked(wire: Wire) -> None:
    """A real server sends `text/html; charset=utf-8`, and passing that through unnormalised
    would refuse a page this pipeline can read perfectly well."""
    wire.answers(content_type="text/html; charset=utf-8")

    assert (await url_fetch.fetch("https://example.com/")).content_type == "text/html"


async def test_a_pdf_is_fetched_like_any_other_page(wire: Wire) -> None:
    """§22 names PDFs first, and a PDF served over HTTP is the ordinary way one arrives."""
    wire.answers(body=b"%PDF-1.4 ...", content_type="application/pdf")

    assert (await url_fetch.fetch("https://example.com/policy.pdf")).content_type == (
        "application/pdf"
    )


# ---------------------------------------------------------------------------
# The scheme, and the host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "gopher://x/"])
async def test_only_http_and_https_are_fetched(url: str) -> None:
    """`file:///etc/passwd` is the one to think about, and it is not hypothetical.

    `HttpUrl` already refuses these in the request body — but a *redirect* is not validated by
    that schema, so this check is the one that covers `Location: file:///…` and it is here rather
    than only there for exactly that reason.
    """
    with pytest.raises(UrlFetchError, match="only http and https"):
        await url_fetch.fetch(url)


async def test_a_url_with_no_host_is_refused_before_dns(wire: Wire) -> None:
    """`getaddrinfo` raises on an empty name, and this module would have to translate it anyway."""
    with pytest.raises(UrlFetchError, match="no host"):
        await url_fetch.fetch("http:///refunds")

    assert wire.requests == []


# ---------------------------------------------------------------------------
# The address guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback: the deployment's own admin surface
        "::1",  # the same, over IPv6
        "10.0.0.5",  # a private network the service runs inside
        "192.168.1.1",
        "172.16.0.9",
        "169.254.169.254",  # link-local, and the cloud metadata endpoint
        "fe80::1",
        "0.0.0.0",  # noqa: S104 — an address to refuse, not a socket to bind
        "224.0.0.1",  # multicast — and `is_global` is True for it on CPython 3.14
        "ff02::1",  # the same group over IPv6, and likewise `is_global`
    ],
)
async def test_an_address_inside_the_network_is_refused_and_never_contacted(
    wire: Wire, monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    """**The whole feature, in one assertion.** Ten ways of naming an address the API can reach
    and the person asking cannot, each refused — and `wire.requests == []` is the half that
    matters: a guard that checks after connecting has already made the request.

    The list is not the implementation, and it is here because a property is easier to weaken
    than a list is to forget: a future change that dropped the check would fail ten tests naming
    ten distinct attacks. **The two multicast rows earn their place differently from the other
    eight** — they are the two `is_global` alone does not refuse, which is how the missing
    `is_multicast` clause in `_assert_routable` was found rather than argued about.
    """
    monkeypatch.setattr(url_fetch, "_resolve", _resolves_to(address))
    wire.answers()

    with pytest.raises(UrlFetchError, match="not a publicly routable address"):
        await url_fetch.fetch("https://metadata.internal/")

    assert wire.requests == []


async def test_a_host_resolving_to_a_public_and_a_private_address_is_refused(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All the addresses, not the first — and this is the case a first-address check misses.

    A name with one A record pointing at a public address and another at `127.0.0.1` is a name
    that resolves to a loopback address. Which one `connect()` picks is not something this code
    decides, so the only safe reading of that answer is "refuse".
    """
    monkeypatch.setattr(url_fetch, "_resolve", _resolves_to(PUBLIC, "127.0.0.1"))
    wire.answers()

    with pytest.raises(UrlFetchError, match=r"127\.0\.0\.1"):
        await url_fetch.fetch("https://half-public.example/")

    assert wire.requests == []


async def test_a_host_that_does_not_resolve_is_refused(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty answer is not "nothing to check" — it is a name with no document behind it."""
    monkeypatch.setattr(url_fetch, "_resolve", _resolves_to())
    wire.answers()

    with pytest.raises(UrlFetchError, match="did not resolve"):
        await url_fetch.fetch("https://nowhere.example/")

    assert wire.requests == []


# ---------------------------------------------------------------------------
# Redirects — where a checked URL becomes an unchecked one
# ---------------------------------------------------------------------------


async def test_a_redirect_is_followed_and_the_final_url_is_what_comes_back(wire: Wire) -> None:
    """`source_reference` stores where the request *ended*, and a later re-fetch goes there.

    The redirect is followed by hand rather than by `httpx`, because `follow_redirects=True` would
    move the request before the guard could look at where — so the hop count here is a fact about
    this code rather than about a client setting.
    """
    wire.answers(302, location="https://example.com/refunds/policy")
    wire.answers(body=PAGE)

    page = await url_fetch.fetch("https://example.com/refunds")

    assert page.url == "https://example.com/refunds/policy"
    assert [str(request.url) for request in wire.requests] == [
        "https://example.com/refunds",
        "https://example.com/refunds/policy",
    ]


async def test_a_relative_redirect_is_resolved_against_the_url_that_produced_it(
    wire: Wire,
) -> None:
    """`Location: /policy` is ordinary, and used raw it is not a URL at all."""
    wire.answers(302, location="/policy")
    wire.answers(body=PAGE)

    page = await url_fetch.fetch("https://example.com/refunds/")

    assert page.url == "https://example.com/policy"


async def test_a_redirect_to_a_private_address_is_refused_on_the_hop(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The attack the guard is shaped around**, and it defeats any check applied only to the
    URL the client sent.

    A public page — one this service is entitled to fetch — answers `302` with a metadata
    endpoint as its target. The second hop is resolved and refused before it is contacted, which
    is why the redirect is followed by hand: the check has to happen on the address the request
    is about to be made to, not on the one it started from.

    The request log is the assertion that carries the weight, and the resolver is per host for
    that reason: `public.example` resolves publicly and really is fetched, and
    `169.254.169.254` is the only name that is refused. A resolver that answered the same way
    for both would leave the log empty and the test passing for a reason that is not this one.
    """
    wire.answers(302, location="http://169.254.169.254/latest/meta-data/")
    monkeypatch.setattr(
        url_fetch, "_resolve", _resolves_by_host({"169.254.169.254": ["169.254.169.254"]})
    )

    with pytest.raises(UrlFetchError, match="not a publicly routable address"):
        await url_fetch.fetch("https://public.example/redirect")

    assert [str(request.url) for request in wire.requests] == ["https://public.example/redirect"]


async def test_a_redirect_without_a_location_is_refused(wire: Wire) -> None:
    """A `3xx` with nothing to follow is a dead end, and inventing a target is not an option."""
    wire.answers(302)

    with pytest.raises(UrlFetchError, match="without giving a location"):
        await url_fetch.fetch("https://example.com/")


async def test_a_redirect_chain_longer_than_the_cap_is_refused(wire: Wire) -> None:
    """Each hop is a fresh request from inside the network, so the chain has to be bounded.

    Every hop here is to a public address, so nothing but the cap stops it — which is the point:
    this is the failure mode of a bounded guard rather than of a refusing one, and the message
    says which.
    """
    for number in range(MAX_REDIRECTS + 2):
        wire.answers(302, location=f"https://example.com/hop{number}")

    with pytest.raises(UrlFetchError, match=f"more than {MAX_REDIRECTS} times"):
        await url_fetch.fetch("https://example.com/start")


# ---------------------------------------------------------------------------
# The response
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [400, 403, 404, 500, 503])
async def test_an_error_status_is_a_refusal_with_the_code_in_it(
    wire: Wire, status_code: int
) -> None:
    """`4xx` and `5xx` alike: from the caller's position there is no document either way.

    The code is in the message because an admin reading `error_message` needs to be able to tell
    "the page moved" from "their site is down".
    """
    wire.answers(status_code, body=b"nope")

    with pytest.raises(UrlFetchError, match=f"HTTP {status_code}"):
        await url_fetch.fetch("https://example.com/")


@pytest.mark.parametrize("content_type", ["image/png", "application/octet-stream", ""])
async def test_a_type_the_pipeline_cannot_read_is_refused_before_the_body_is_read(
    wire: Wire, content_type: str
) -> None:
    """The type is the server's claim, and everything downstream of it assumes text.

    The set is `document_text.READABLE_MEDIA_TYPES`, imported rather than restated — two copies of
    "what can be read" is how a page comes to be fetched as a text document and refused as an
    unreadable one a second later. An absent `Content-Type` is refused with its own wording,
    because "no content type" is not a media type a reader can act on.
    """
    wire.answers(content_type=content_type)

    with pytest.raises(UrlFetchError, match="cannot extract text from"):
        await url_fetch.fetch("https://example.com/")


async def test_an_oversized_page_is_refused_rather_than_truncated(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncated page is a document that ingested successfully and silently says less.

    The cap is patched down to two mebibytes rather than a five-mebibyte body being generated:
    the branch is one comparison and the loop that feeds it, and the size in the message is the
    cap's own — asserted, so a message that stopped naming it would fail here.
    """
    cap = 2 * 1024 * 1024
    monkeypatch.setattr(url_fetch, "MAX_URL_BYTES", cap)
    wire.answers(body=b"x" * (cap + 1))

    with pytest.raises(UrlFetchError, match="larger than the 2 MiB"):
        await url_fetch.fetch("https://example.com/huge")


async def test_a_transport_failure_becomes_a_refusal_that_quotes_nothing(
    wire: Wire,
) -> None:
    """A DNS timeout, a refused connection, and a TLS error are one outcome for the caller.

    The message is fixed rather than taken from the exception, because `httpx`'s own text can
    quote the URL and the transport's internals — and this string is stored on a row an admin
    reads. The `from exc` on the raise keeps the original for a traceback; what is not kept is
    its text in the value a person sees.
    """
    wire.fails(httpx.ConnectError("connection refused to 10.0.0.5 while fetching the policy"))

    with pytest.raises(UrlFetchError, match="could not be reached") as caught:
        await url_fetch.fetch("https://example.com/")

    assert "10.0.0.5" not in str(caught.value)
    assert "example.com" not in str(caught.value)
