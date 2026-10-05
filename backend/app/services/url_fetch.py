"""Fetching a document from a URL — §22's `url` source, and the guard that makes it safe.

A feature that fetches a URL on behalf of a user is a server-side request forgery primitive by
default. The API can reach addresses the person asking cannot: `http://localhost:8000/…` is the
deployment's own admin surface, `http://169.254.169.254/` is a cloud metadata endpoint that
hands out credentials, and `http://10.0.0.5/` is whatever is inside the private network the
service runs in. §22 asks for *"product documentation"* and *"support policies"*, which are
pages on the public internet — so the guard is not an extra, it is the difference between a
document ingest and an open redirect for anyone who can reach the API.

**The guard is the address, not the name.** A blocklist of hostnames is defeated by a name that
resolves to `127.0.0.1`, and so is a check applied only to the URL the client sent — a public
page can redirect to `http://169.254.169.254/`. So every hop is resolved with `getaddrinfo` and
**every address it resolves to** must be a globally routable one; a name that resolves to both a
public and a private address is refused rather than gambled on. Refusal happens on the hop that
offends, not on the one that started it.

**What this guard does not close, stated plainly.** `getaddrinfo` and the connection that
follows it are two lookups, so a name whose answer changes between them — DNS rebinding — can
still reach a private address. Closing that needs the connection pinned to the address that was
checked, which `httpx` does not expose; it would mean hand-rolling the socket. The guard
therefore raises the cost of the attack from "spell a hostname" to "control a DNS server and win
a race", and the honest thing is to say so here rather than to describe it as complete.

**Redirects are followed by hand**, because a redirect is exactly where a checked URL becomes an
unchecked one — `follow_redirects=True` would move the request before the guard could look at
where. The hop count is capped, the body is read with a cap, and the readable media types are an
allowlist for the reason `app/core/file_validation.py` gives about uploads: the type is the
server's claim, and everything downstream of it assumes text.
"""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import anyio.to_thread
import httpx
import structlog

from app.core.event_loop import current_loop
from app.core.file_validation import normalize_content_type
from app.services.document_text import READABLE_MEDIA_TYPES

logger = structlog.get_logger(__name__)

#: How many redirects are followed. Five is what browsers default to and what `httpx` uses;
#: the number matters less than the fact that it is bounded, because each hop is a fresh
#: request from inside the network that the guard then has to check.
MAX_REDIRECTS = 5

#: The largest body this will read, in bytes. Five mebibytes is a long policy page and a modest
#: PDF, and it is deliberately smaller than `MAX_KNOWLEDGE_DOCUMENT_BYTES`: an upload is a file
#: a person chose deliberately, and a URL is a request a person typed that a server answers.
MAX_URL_BYTES = 5 * 1024 * 1024

#: The longest URL that can be stored. `knowledge_documents.source_reference` is `String(1000)`,
#: and a redirect can lengthen a URL a client sent, so the bound is applied to the final one.
MAX_URL_CHARS = 1000

#: The schemes this will follow. `HttpUrl` in `app/schemas/knowledge.py` already refuses anything
#: else in the request body, and this refuses it again because a *redirect* is not validated by
#: that schema — `Location: file:///etc/passwd` is a real thing to try.
_SCHEMES = frozenset({"http", "https"})

#: Sends a name, because a request from a service with no `User-Agent` is a request that looks
#: like a scanner, and some sites answer those with a 403.
_USER_AGENT = "SupportFlow-KnowledgeIngestion/1.0"

#: How long a single hop may take, end to end. Bounded for the reason every timeout here is:
#: the caller is a Celery task holding a database transaction, and a server that accepts the
#: connection and then sends nothing must not hold it open indefinitely.
_TIMEOUT_SECONDS = 15.0

_client: "httpx.AsyncClient | None" = None

#: The loop `_client` was built on, or `None` when this process did not build it — `groq.py`'s
#: field and its reasoning: a pool belongs to the loop that opened it, and a loop built for one
#: Celery task is closed before the next one runs.
_client_loop: "asyncio.AbstractEventLoop | None" = None


class UrlFetchError(Exception):
    """This URL cannot be fetched as a document, and the message says why.

    `DocumentTextError`'s shape and for the same reason: a pure-ish module refuses in its own
    vocabulary and the service decides what that means for a row. The messages here are written
    for the admin who reads `knowledge_documents.error_message`, so they name the condition —
    a refused address, a refused type, an oversized page — rather than an internal step.
    """


@dataclass(frozen=True)
class FetchedPage:
    """What a URL returned, before extraction: the bytes, what they claim to be, and where they
    came from.

    `url` is the **final** URL after redirects rather than the one that was asked for, and it is
    what `source_reference` stores: the document came from where the request ended, and a later
    re-fetch should go to the same place.
    """

    url: str
    content_type: str
    body: bytes


def _shared_client() -> "httpx.AsyncClient":
    """The client for the running loop, built on first use on that loop.

    `app/ai/groq.py`'s pattern, for the same reason: the pool belongs to the loop that opened
    it, and `app/core/event_loop.py` builds and closes a loop per Celery task, so a client
    cached across two tasks points at a loop that no longer exists.

    `follow_redirects` is **off**, and that is the guard rather than a preference — see the
    module docstring. The client is built without it so that nothing in this module can
    accidentally hand a redirect to `httpx`.
    """
    global _client, _client_loop
    running = current_loop()
    if (
        running is not None
        and _client is not None
        and _client_loop is not None
        and _client_loop is not running
    ):
        _client = None
    if _client is None:
        _client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=_TIMEOUT_SECONDS,
            headers={"User-Agent": _USER_AGENT},
        )
        _client_loop = running
    return _client


def reset_client() -> None:
    """Drop the cached client. Called by tests that repoint the transport."""
    global _client, _client_loop
    _client = None
    _client_loop = None


async def _resolve(host: str) -> list[str]:
    """Every address `host` resolves to, as strings.

    In a worker thread because `getaddrinfo` is a blocking call to a resolver, and the caller
    is an event loop that has a Celery task's other work to do. Sorted and de-duplicated so that
    a host resolving to the same address twice is checked once — the set of addresses is what
    matters, not how many records produced it.
    """
    infos = await anyio.to_thread.run_sync(socket.getaddrinfo, host, None, 0, socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


async def _assert_routable(host: str) -> None:
    """Refuse `host` unless every address it resolves to is globally routable.

    `is_global` is the check, and it is one property rather than five because the five it
    implies are the ones an attacker would otherwise try in turn: a loopback address (`127.0.0.1`,
    `::1`), a private one (`10.0.0.0/8`, `192.168.0.0/16`, `fc00::/7`), a link-local one
    (`169.254.0.0/16` — the cloud metadata endpoint — and `fe80::/10`), and a reserved or
    unspecified one (`0.0.0.0`, `240.0.0.0/4`). `is_global` is false for all of them and true for
    the public addresses a document actually lives at.

    **`is_multicast` is checked beside it, and that is a correction rather than a tidy-up.**
    This function's first version said `is_global` covered multicast too, and on CPython 3.14
    `ipaddress.ip_address("224.0.0.1").is_global` is **`True`** — as is `ff02::1`'s. The claim
    was written from the documentation's intent and the test `test_url_fetch.py` asserts it
    against the interpreter that runs this, which is how the gap was found. A multicast address
    is a group rather than a host — `224.0.0.1` is every host on the local segment — so it is
    not an SSRF into one private service, but it is certainly not a document on the public
    internet, and refusing it costs one already-parsed property.

    **All the addresses, not the first.** A host with an A record pointing at a public address
    and another at `127.0.0.1` is a host that resolves to a private address, and which one the
    connect() picks is not something this code gets to decide.
    """
    addresses = await _resolve(host)
    if not addresses:
        raise UrlFetchError(f"the host {host!r} did not resolve to any address")
    for address in addresses:
        parsed = ipaddress.ip_address(address)
        if parsed.is_multicast or not parsed.is_global:
            raise UrlFetchError(
                f"the host {host!r} resolves to {address}, which is not a publicly routable "
                "address; this service will not fetch addresses inside its own network"
            )


def _parse(url: str) -> tuple[str, str]:
    """`url` as `(scheme, host)`, or `UrlFetchError`.

    A missing host is refused here rather than reaching `getaddrinfo`, which raises a `socket`
    error this module would then have to translate anyway.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _SCHEMES:
        raise UrlFetchError(f"only http and https URLs are fetched, not {parsed.scheme!r}")
    if not parsed.hostname:
        raise UrlFetchError("the URL has no host")
    return parsed.scheme.lower(), parsed.hostname


async def _read_capped(response: httpx.Response) -> bytes:
    """The response body, refused past `MAX_URL_BYTES` rather than truncated.

    **Refused, not truncated.** A truncated page is a document that ingested successfully and
    silently says less than it does, and nothing downstream could tell. The cap is checked as
    the body streams, so a server sending gigabytes is stopped after five mebibytes rather than
    after the whole thing has been buffered.
    """
    chunks: list[bytes] = []
    total = 0
    async for piece in response.aiter_bytes():
        total += len(piece)
        if total > MAX_URL_BYTES:
            raise UrlFetchError(
                f"the page is larger than the {MAX_URL_BYTES // (1024 * 1024)} MiB this will "
                "fetch; upload the file instead"
            )
        chunks.append(piece)
    return b"".join(chunks)


async def fetch(url: str) -> FetchedPage:
    """Fetch `url` as a document, following redirects one guarded hop at a time.

    The loop is the guard's shape: parse, resolve, check, request — and then, if that was a
    redirect, do all four again for the address the redirect names. A URL that was checked is
    only ever connected to once, and a redirect can never skip a check because there is no path
    through this function that does not begin with `_assert_routable`.

    Every failure is a `UrlFetchError` with a reason written for an admin: a refused scheme, a
    private address, a non-2xx status, a media type this cannot extract, an oversized page, too
    many hops. A transport failure — a DNS timeout, a refused connection, a TLS error — becomes
    one too, because from the caller's position the document is equally unobtainable.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _scheme, host = _parse(current)
        await _assert_routable(host)

        try:
            async with _shared_client().stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise UrlFetchError("the URL redirected without giving a location")
                    # Relative `Location` values are ordinary, so the redirect target is
                    # resolved against the URL that produced it rather than used raw.
                    current = urljoin(current, location)
                    if len(current) > MAX_URL_CHARS:
                        raise UrlFetchError("the redirect chain produced an unusably long URL")
                    continue

                if response.status_code >= 400:
                    raise UrlFetchError(
                        f"the URL returned HTTP {response.status_code}, so there is no document "
                        "to ingest"
                    )

                content_type = normalize_content_type(response.headers.get("content-type"))
                # `READABLE_MEDIA_TYPES` is the extraction pipeline's own set, imported rather
                # than restated: two copies of "what can be read" is how a page comes to be
                # fetched as a text document and refused as an unreadable one a second later.
                if content_type not in READABLE_MEDIA_TYPES:
                    raise UrlFetchError(
                        f"the URL returned {content_type or 'no content type'}, which this "
                        "pipeline cannot extract text from"
                    )

                body = await _read_capped(response)
        except httpx.HTTPError as exc:
            # Translated rather than propagated, and the reason is the type name only: an
            # `httpx` message can quote the URL and the transport's own error text, and this
            # string is stored on a row an admin reads.
            logger.warning(
                "knowledge_url_fetch_failed",
                error_type=type(exc).__name__,
                reason="the URL could not be reached",
            )
            raise UrlFetchError("the URL could not be reached") from exc

        logger.info("knowledge_url_fetched", url=current, content_type=content_type)
        return FetchedPage(url=current, content_type=content_type, body=body)

    raise UrlFetchError(
        f"the URL redirected more than {MAX_REDIRECTS} times, which is as far as this will follow"
    )
