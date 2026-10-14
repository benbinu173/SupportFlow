"""The abandoned-claim sweep, run for real: what it releases, and what it leaves alone.

**This is ADR-033 Finding 1's remedy, and it is the only file that can show it working.**
`run_analysis` and `run_ingestion` both claim a row — one conditional `UPDATE` to `processing`
— and commit before the slow part, which is what makes a redelivered message a no-op rather
than a second bill. The price is that the claim outlives the process that took it: a worker
killed by the out-of-memory killer, a container stopped mid-task, or the hard time limit firing
leaves the row `processing`, the message was acknowledged rather than redelivered, and nothing
else in this system looks at a `processing` row. `tests/integration/test_ai_analysis.py`'s
`test_an_unexpected_failure_keeps_the_work_already_paid_for` produces that state through the
real code path and asserts the row stays in it; this file is about the thing that gets it out.

**The sweep is invoked as a function, not awaited.** `reap_abandoned_claims` is a Celery task
whose body is `event_loop.run(_reap())`, so calling it from a synchronous test runs the
identical code path a worker would, on a loop the task owns (ADR-011) — the same argument
`tests/integration/test_sla_sweep.py` makes for its own task. Awaiting `_reap` directly would
run it inside `pytest-asyncio`'s loop instead, which is not the loop a worker uses.

**The stranded state is written with SQL, and there is no other way to write it.** *"A worker
ceased to exist holding this claim"* is not a state any route produces — the API can queue an
analysis and can upload a document, and neither can express a process that died. The two
`UPDATE`s below say exactly that and nothing more: status `processing`, and an `updated_at`
moved into the past, which is the column the sweep reads as the claim's clock.

**Time is moved by moving the row's `updated_at`, and the boundary is read from settings.**
The threshold is `ABANDONED_CLAIM_AFTER_SECONDS`, so the tests that need to sit either side of
it derive both offsets from `get_settings()` rather than hardcoding fifteen minutes — a
deployment that tunes the window must not fail the suite that describes it.
"""

from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.config import get_settings
from app.workers import maintenance_tasks
from tests.conftest import KNOWLEDGE, TICKETS, OrgSession

pytestmark = pytest.mark.integration

#: The two sentences the repository functions write, asserted as literals rather than imported
#: from the modules that write them. They are shown to staff — on the analysis row and on the
#: admin's document list — so what they say is part of the behaviour being tested, and a change
#: to the wording should have to be made deliberately in two places. `tests/integration/
#: test_knowledge_ingestion.py` takes the same position on its own user-visible strings.
ABANDONED_ANALYSIS = "The worker running this analysis stopped before it finished."
ABANDONED_DOCUMENT = "The worker ingesting this document stopped before it finished."

#: How far either side of the threshold the boundary test places its two claims. A minute is
#: far more than the microseconds between `datetime.now(UTC)` in the sweep and Postgres' `now()`
#: a moment earlier, and far less than any plausible threshold — so the test states which side
#: of the line a row is on rather than how close to it a clock can drift.
_BOUNDARY_MARGIN_SECONDS = 60


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every test in this file, as every database-touching file here does.

    `truncate_tables` is not autouse in the root conftest — requesting it pulls in a live
    Postgres, which is what keeps `tests/unit/` runnable with nothing started — and
    `test_celery_wiring.py` in this package deliberately contacts no service. So the opt-in is
    per file, as in `tests/security/`.
    """


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="Maintenance Co")


@pytest.fixture
def customer(org: OrgSession) -> str:
    return str(org.add_customer(name="Grace Hopper", email="grace@maintenance.co")["id"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def abandon_analysis(engine: Engine, *, analysis_id: str, seconds: int) -> None:
    """Leave one analysis row the way a worker that ceased to exist leaves it.

    `updated_at` and not `created_at`: that column is the claim's clock, because the claim is
    an `UPDATE` and `updated_at` is the one column an `UPDATE` is guaranteed to move. See
    `ai_repository.fail_abandoned_claims`.

    A raw statement rather than an ORM one, so `TimestampMixin`'s `onupdate` does not fire and
    silently re-stamp the row to now — which would make a row this test just aged look fresh
    and the sweep look broken.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE ai_analyses SET status = 'processing', "
                "updated_at = now() - make_interval(secs => :seconds) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": analysis_id, "seconds": seconds},
        )


def abandon_document(engine: Engine, *, document_id: str, seconds: int) -> None:
    """The document half of `abandon_analysis`, and it says the same thing."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE knowledge_documents SET status = 'processing', "
                "updated_at = now() - make_interval(secs => :seconds) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": document_id, "seconds": seconds},
        )


def reap() -> dict[str, int]:
    """One sweep, the way beat runs it — the task, not `_reap`, so it owns its own loop."""
    return maintenance_tasks.reap_abandoned_claims()


def threshold() -> int:
    """The configured window, which every offset in this file is measured against."""
    return get_settings().ABANDONED_CLAIM_AFTER_SECONDS


def queue_an_analysis(
    queued: list[tuple[str, str, list[str]]], org: OrgSession, customer: str
) -> tuple[str, list[str]]:
    """Create a ticket — which queues its two operations — and return what the broker was told.

    Through the API, so the rows under test were written by the code that writes them:
    `request_analysis` writes two `pending` rows, commits, and hands their ids to a task. The
    pair comes back off the recorder (`tests/conftest.py`'s `queued_analyses`, autouse) rather
    than being constructed here, so what this file moves to `processing` is a real row a real
    worker would have been handed.

    `(ticket_id, analysis_ids)`, in `ANALYSIS_OPERATIONS` order — classify first, which is the
    order `load_analyses` reads them back in and the order the task runs them.
    """
    org.add_ticket(customer)
    ticket_id, _, analysis_ids = queued[-1]
    return ticket_id, analysis_ids


def analysis_rows(org: OrgSession, ticket_id: str) -> dict[str, dict]:
    """The ticket's latest analysis per operation, keyed by operation."""
    response = org.get(f"{TICKETS}/{ticket_id}/ai/analyses")
    assert response.status_code == 200, response.text
    return {row["operation"]: row for row in response.json()}


def document_rows(org: OrgSession) -> dict[str, dict]:
    """Every document this tenant has, keyed by id — the administrator's own view.

    Read through the route rather than from the table because half of what the sweep's
    docstring claims is that a stranded document ends up *on a screen an admin reads*. A row
    that is `failed` in Postgres and invisible through `GET /knowledge` would satisfy every
    other assertion in this file and not that one.
    """
    response = org.get(KNOWLEDGE)
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()}


def upload_document(org: OrgSession, *, title: str) -> str:
    """Register a manual document through the API and return its id."""
    response = org.post(KNOWLEDGE, json={"title": title, "content": "Refunds take five days."})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


# ---------------------------------------------------------------------------
# The analysis half
# ---------------------------------------------------------------------------


def test_an_abandoned_analysis_is_failed_and_the_rest_of_the_ticket_is_untouched(
    org: OrgSession,
    customer: str,
    queued_analyses: list[tuple[str, str, list[str]]],
    sync_engine: Engine,
) -> None:
    """One row stranded, one still waiting, and exactly one of them changes.

    The classification is left `pending` on purpose, and it is the control: the sweep's
    predicate is `status = 'processing'` **and** an old `updated_at`, so a row that has not
    been claimed yet must survive it. A sweep that failed every non-terminal row would pass
    every assertion about the stranded one and destroy the queue — which is the failure this
    file's second test is about from the other side.

    **`failed` and not `pending`**, which is the decision the module docstring argues for at
    length: nothing re-queues a `pending` row, so putting it back would leave it stuck in a
    different column, and `failed` is what restores the two things that matter — the operation
    can be asked for again, and the ticket shows a row that says what happened.
    """
    ticket_id, analysis_ids = queue_an_analysis(queued_analyses, org, customer)
    abandon_analysis(sync_engine, analysis_id=analysis_ids[1], seconds=threshold() + 60)

    counts = reap()

    assert counts == {"analyses": 1, "documents": 0}
    rows = analysis_rows(org, ticket_id)
    assert rows["sentiment"]["status"] == "failed"
    assert rows["sentiment"]["error_message"] == ABANDONED_ANALYSIS
    # Untouched, and not merely un-failed: the row this run never claimed is exactly as
    # `request_analysis` left it.
    assert rows["classify"]["status"] == "pending"
    assert rows["classify"]["error_message"] is None


def test_a_claim_inside_the_window_is_left_alone_however_long_the_sweep_runs(
    org: OrgSession,
    customer: str,
    queued_analyses: list[tuple[str, str, list[str]]],
    sync_engine: Engine,
) -> None:
    """Either side of the threshold, by a minute, in one pass.

    **This is the assertion that keeps the sweep from being a catastrophe rather than a
    repair.** The task it is releasing runs for up to `CELERY_TASK_TIME_LIMIT_SECONDS`; a sweep
    that reaped by status alone would fail every analysis that was currently being worked on,
    every ten minutes, forever — and it would look correct in every test that only ever
    produced abandoned rows.

    The two claims are one minute apart and sit on opposite sides of the configured window, so
    the assertion is about the *boundary* rather than about age in general. Both offsets come
    from `ABANDONED_CLAIM_AFTER_SECONDS` rather than from a literal, so tuning the window does
    not turn this into a statement about fifteen minutes.
    """
    young_ticket, young_ids = queue_an_analysis(queued_analyses, org, customer)
    abandon_analysis(
        sync_engine, analysis_id=young_ids[1], seconds=threshold() - _BOUNDARY_MARGIN_SECONDS
    )

    old_ticket, old_ids = queue_an_analysis(queued_analyses, org, customer)
    abandon_analysis(
        sync_engine, analysis_id=old_ids[1], seconds=threshold() + _BOUNDARY_MARGIN_SECONDS
    )

    counts = reap()

    assert counts == {"analyses": 1, "documents": 0}
    assert analysis_rows(org, young_ticket)["sentiment"]["status"] == "processing"
    assert analysis_rows(org, old_ticket)["sentiment"]["status"] == "failed"


def test_a_second_sweep_reaps_nothing(
    org: OrgSession,
    customer: str,
    queued_analyses: list[tuple[str, str, list[str]]],
    sync_engine: Engine,
) -> None:
    """The row the sweep wrote is what stops it writing again.

    Beat runs this every `ABANDONED_CLAIM_SWEEP_INTERVAL_SECONDS` forever, so a predicate that
    kept matching would rewrite the same sentence onto the same row every ten minutes — the
    row's own `completed_at` moving each time, and the log filling with a warning about a
    claim that was already released. The closure is the status: `failed` is not `processing`,
    so the second pass finds nothing.

    Asserted as counts and not only as status, because "the row is still failed" would be true
    of a sweep that rewrote it. The message is asserted too, so a second pass that overwrote
    the sentence with different wording would fail here rather than pass by still being a
    sentence.
    """
    ticket_id, analysis_ids = queue_an_analysis(queued_analyses, org, customer)
    abandon_analysis(sync_engine, analysis_id=analysis_ids[1], seconds=threshold() + 60)

    first = reap()
    second = reap()

    assert first == {"analyses": 1, "documents": 0}
    assert second == {"analyses": 0, "documents": 0}
    rows = analysis_rows(org, ticket_id)
    assert rows["sentiment"]["status"] == "failed"
    assert rows["sentiment"]["error_message"] == ABANDONED_ANALYSIS


def test_one_pass_covers_every_tenant(
    org: OrgSession,
    customer: str,
    register_org: Callable[..., OrgSession],
    queued_analyses: list[tuple[str, str, list[str]]],
    sync_engine: Engine,
) -> None:
    """No organization is named anywhere in the sweep, and this is what that buys.

    `check_sla_deadlines` fans out one task per tenant because the work inside a tenant is
    unbounded; this sweep is two `UPDATE`s with an indexed predicate, and the number of rows
    it finds is however many workers died since the last pass — normally none. So it takes no
    organization and covers all of them, which is a property that would be invisible if every
    test here used one tenant: a reaper that had been given an `organization_id` filter would
    pass all of them and leave every other tenant's rows wedged.

    Two tenants, one stranded analysis each, one pass, both released.
    """
    other = register_org(organization_name="Maintenance Two Ltd")
    other_customer = str(other.add_customer(name="Ada Byron", email="ada@maintenance2.co")["id"])

    first_ticket, first_ids = queue_an_analysis(queued_analyses, org, customer)
    abandon_analysis(sync_engine, analysis_id=first_ids[1], seconds=threshold() + 60)

    second_ticket, second_ids = queue_an_analysis(queued_analyses, other, other_customer)
    abandon_analysis(sync_engine, analysis_id=second_ids[1], seconds=threshold() + 60)

    counts = reap()

    assert counts == {"analyses": 2, "documents": 0}
    # Read through each tenant's own session, which is also the check that the release did
    # not move anything across a boundary: `analysis_rows` takes the ticket id from the
    # caller and the token decides what is visible, so a row answered by the wrong tenant
    # would be a 404 here rather than a passing assertion.
    assert analysis_rows(org, first_ticket)["sentiment"]["status"] == "failed"
    assert analysis_rows(other, second_ticket)["sentiment"]["status"] == "failed"


# ---------------------------------------------------------------------------
# The document half
# ---------------------------------------------------------------------------


def test_an_abandoned_ingestion_is_failed_and_appears_on_the_admins_list(
    org: OrgSession,
    sync_engine: Engine,
) -> None:
    """A stranded document stops being invisible, which is the whole of what it costs.

    Retrieval requires `status = 'completed'` — `has_published_chunks` and `search_chunks` both
    say so — and no route re-queues an ingestion, so a document left `processing` answers
    nothing and is never tried again. It is quieter than the analysis case and just as final,
    and the only thing that puts it back in front of a person is the status change.

    **`chunk_count` is zeroed and `is_published` forced false**, following
    `knowledge_service._fail`. Neither can be set on a `processing` row today — nothing
    publishes a document before ingestion succeeds — and writing them makes the invariant read
    off the row rather than depend on the order of two statements somewhere else.

    The fresh claim is the control, and it is the same argument the analysis half makes: an
    ingestion that started a moment ago is still somebody's work in progress.
    """
    stranded = upload_document(org, title="Refund policy")
    abandon_document(sync_engine, document_id=stranded, seconds=threshold() + 60)
    running = upload_document(org, title="Shipping policy")
    abandon_document(
        sync_engine, document_id=running, seconds=threshold() - _BOUNDARY_MARGIN_SECONDS
    )

    counts = reap()

    assert counts == {"analyses": 0, "documents": 1}
    documents = document_rows(org)
    assert documents[stranded]["status"] == "failed"
    assert documents[stranded]["error_message"] == ABANDONED_DOCUMENT
    assert documents[stranded]["chunk_count"] == 0
    assert documents[stranded]["is_published"] is False
    # A document claimed a moment ago is still being worked on, whatever the sweep thinks
    # about the one beside it.
    assert documents[running]["status"] == "processing"
    assert documents[running]["error_message"] is None
