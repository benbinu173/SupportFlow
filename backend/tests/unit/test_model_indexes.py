"""Index invariants over the model metadata.

No database needed — these read `Base.metadata` directly, so they run as unit tests.
They guard the tenancy index guarantee, which the `__org_index__` opt-out could
otherwise erode one table at a time without any test noticing.

The database-level equivalent (querying `pg_index`) is asserted separately in
tests/integration/test_models.py, so that a metadata-only guarantee is never mistaken
for one PostgreSQL actually implements.
"""

import pytest
from sqlalchemy import Table, UniqueConstraint

from app.models import Base

pytestmark = pytest.mark.unit

# Tables that opt out of the mixin's standalone organization_id index, because
# another index or unique constraint already leads with organization_id. Kept as an
# explicit list so adding an opt-out is a deliberate, reviewed change.
TABLES_WITHOUT_STANDALONE_ORG_INDEX = {
    "ai_analyses",
    "ai_usage",
    "audit_logs",
    "customers",
    "knowledge_chunks",
    "knowledge_documents",
    "sla_policies",
    "tickets",
    "users",
}


def tenant_tables() -> list[Table]:
    """Every table that carries organization_id. `organizations` is the scope itself."""
    return [
        table
        for table in Base.metadata.tables.values()
        if table.name != "organizations" and "organization_id" in table.columns
    ]


def org_leading_indexes(table: Table) -> set[str]:
    """Names of indexes whose first key column is organization_id."""
    names: set[str] = set()
    for index in table.indexes:
        columns = list(index.columns)
        if columns and columns[0].name == "organization_id" and index.name is not None:
            names.add(str(index.name))
    return names


def org_leading_constraints(table: Table) -> set[str]:
    """UNIQUE constraints leading with organization_id.

    A UNIQUE constraint is backed by a real unique B-tree in PostgreSQL, so it serves
    an org-only predicate exactly as a plain index does. Excluding these would make
    `users`, `customers`, and `sla_policies` look unindexed when they are not.
    """
    names: set[str] = set()
    for constraint in table.constraints:
        if not isinstance(constraint, UniqueConstraint) or not constraint.columns:
            continue
        if next(iter(constraint.columns)).name == "organization_id" and constraint.name:
            names.add(str(constraint.name))
    return names


@pytest.mark.parametrize("table", tenant_tables(), ids=lambda t: t.name)
def test_every_tenant_table_has_an_org_leading_index(table: Table) -> None:
    """The core guarantee: tenant filtering is always index-backed.

    Without this, a query filtering only on organization_id would sequentially scan,
    which is the performance half of the isolation story.
    """
    leading = org_leading_indexes(table) | org_leading_constraints(table)
    assert leading, f"{table.name} has no index whose first column is organization_id"


@pytest.mark.parametrize("table", tenant_tables(), ids=lambda t: t.name)
def test_opted_out_tables_have_no_standalone_org_index(table: Table) -> None:
    """An opt-out must actually remove the index, not merely be declared."""
    standalone = f"ix_{table.name}_organization_id"
    if table.name in TABLES_WITHOUT_STANDALONE_ORG_INDEX:
        assert standalone not in org_leading_indexes(table)
    else:
        assert standalone in org_leading_indexes(table)


def test_opt_out_is_justified_by_a_composite() -> None:
    """No table opts out without something else leading with organization_id.

    This is what makes `__org_index__ = False` safe rather than a claim. If someone
    adds the flag to a table with no org-leading composite, that table loses its index
    entirely — so the flag and the justification are checked together.
    """
    unjustified = []
    for table in tenant_tables():
        if table.name not in TABLES_WITHOUT_STANDALONE_ORG_INDEX:
            continue
        standalone = f"ix_{table.name}_organization_id"
        replacements = (org_leading_indexes(table) | org_leading_constraints(table)) - {standalone}
        if not replacements:
            unjustified.append(table.name)

    assert not unjustified, f"opted out with nothing leading with organization_id: {unjustified}"


def test_opt_out_list_matches_the_schema() -> None:
    """The declared list and the schema agree, in both directions.

    Guards against the list drifting: a stale entry would silently stop asserting
    anything, and a missing one would go unnoticed until a table lost its index.
    """
    actually_opted_out = {
        table.name
        for table in tenant_tables()
        if f"ix_{table.name}_organization_id" not in org_leading_indexes(table)
    }
    assert actually_opted_out == TABLES_WITHOUT_STANDALONE_ORG_INDEX
