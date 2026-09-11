"""Event loop selection.

Exists because of one hard constraint: psycopg's async mode drives sockets with
`add_reader`/`add_writer`, which Windows' `ProactorEventLoop` does not implement
for sockets. Running against it raises

    psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' ...

on the first query, not at startup — so it surfaces as a broken endpoint rather
than a failed boot.

Uvicorn picks `ProactorEventLoop` on win32 whenever it is not spawning a
subprocess, which means `--reload` and `--workers N` happen to work while a plain
`uvicorn app.main:app` does not. Relying on that difference is not a plan, so the
loop is chosen explicitly here and passed to uvicorn via
`--loop app.core.event_loop:loop_factory`.

`asyncio.WindowsSelectorEventLoopPolicy` would be the conventional fix, but the
event loop policy API is deprecated in Python 3.14 and slated for removal in 3.16
(ADR-005 pins us to 3.14). A loop factory is the replacement and is what both
`asyncio.Runner` and uvicorn now accept.

Not a concern in production: the container is Linux, where the default selector
loop already supports the required calls. This is a local-development fix that
must not be allowed to diverge from how the app runs elsewhere.
"""

import asyncio
import selectors
import sys
from collections.abc import Coroutine
from typing import Any

# Held as a named constant so the platform is read once. Comparing `sys.platform`
# against a literal inside the function makes mypy narrow the branch as provably
# dead on the platform it is running on, and the non-Windows fallback then fails
# the unreachable-code check.
_IS_WINDOWS = sys.platform == "win32"


def loop_factory() -> asyncio.AbstractEventLoop:
    """Build an event loop psycopg can use on this platform.

    Referenced by import string, so the signature must stay zero-argument: uvicorn
    returns a custom factory as-is rather than calling it with `use_subprocess`.
    """
    if _IS_WINDOWS:
        # SelectSelector caps at 512 sockets. Ample for local development, and the
        # explicit choice avoids depending on which selector asyncio would pick.
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.new_event_loop()


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run` on a loop that supports the database driver.

    For entrypoints that own the loop themselves — Celery tasks under the solo
    pool, and management scripts. Anything reaching the database from synchronous
    code must go through here rather than calling `asyncio.run` directly.
    """
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        return runner.run(coro)
