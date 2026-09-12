"""Isolation for HTTP tests.

Every test in this package acts on a real database through the real application, and
those writes commit — the app owns its own engine and its own transaction, which is
exactly the point. So the schema has to be emptied between tests, and this is where
that happens.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every API test.

    Autouse here rather than in the root conftest because requesting it pulls in a
    live database, and the unit tests must run without one. Declaring it in this
    package scopes the requirement to the tests that genuinely have it.

    Nothing to do in the body: `truncate_tables` already yields, so *depending* on it
    is what places the truncation on the far side of the test.
    """
