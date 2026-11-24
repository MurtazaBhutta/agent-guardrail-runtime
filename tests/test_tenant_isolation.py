"""Cross-tenant isolation, proven against a real PostgreSQL instance.

These tests do not check that the application remembers to filter by tenant. They check that
**it does not matter whether it remembers** - that the database refuses, on reads and on
writes, for a connection scoped to the wrong tenant.

Every query in `tenancy/store.py` deliberately omits a tenant predicate. If row-level
security were missing, misconfigured, or bypassed because the app owns the tables, these
tests fail loudly instead of the leak being discovered by a customer.

`test_row_level_security_is_actually_in_force` comes first for a reason: it is the test that
gives the others meaning. RLS is silently bypassed by a table's owner, so an isolation suite
run as the owner passes while proving nothing at all.

Skipped unless a database is configured:
    GUARDRAIL_ADMIN_DATABASE_URL  owner role - runs migrations and seeds fixtures
    GUARDRAIL_DATABASE_URL        application role - must NOT own the tables
Run `make db-up` (or see docker-compose.yml) to get both.
"""

from __future__ import annotations

import os

import pytest

from guardrail_runtime.ports import StepRecord
from guardrail_runtime.types import HaltReason, RunStatus, Usage

psycopg = pytest.importorskip("psycopg", reason="requires the 'postgres' extra")

ADMIN_DSN = os.environ.get("GUARDRAIL_ADMIN_DATABASE_URL")
APP_DSN = os.environ.get("GUARDRAIL_DATABASE_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not (ADMIN_DSN and APP_DSN),
        reason="set GUARDRAIL_ADMIN_DATABASE_URL and GUARDRAIL_DATABASE_URL",
    ),
]

TENANT_TABLES = ["tenant_config", "run_steps", "spend_ledger", "pending_approvals"]


@pytest.fixture(scope="module")
def migrated() -> None:
    from guardrail_runtime.tenancy import schema_sql

    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(schema_sql())


@pytest.fixture
def seeded(migrated: None):
    """Two tenants with deliberately different policy, seeded as the owner.

    Provisioning is an owner operation - the application role cannot create tenants, which
    is itself part of the isolation story.
    """
    from guardrail_runtime.tenancy import PostgresStores

    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(
            "truncate run_steps, spend_ledger, pending_approvals, tenant_config, tenants "
            "restart identity cascade"
        )
        conn.execute("insert into tenants (id, name) values ('tenant-a', 'A'), ('tenant-b', 'B')")
        conn.execute(
            """
            insert into tenant_config
                (tenant_id, kill_switch_enabled, daily_budget_cents, hitl_required,
                 blocked_input_patterns, max_attempts)
            values
                ('tenant-a', false, 500,  false, '{}',        3),
                ('tenant-b', true,  9999, true,  '{secret}',  1)
            """
        )
        conn.execute(
            "insert into spend_ledger (tenant_id, run_id, cents) values "
            "('tenant-a', 'a-run', 100), ('tenant-b', 'b-run', 4000)"
        )
        conn.execute(
            """
            insert into run_steps (tenant_id, run_id, step_index, layer, status, reason)
            values ('tenant-a', 'a-run', 0, 'cost_guard', 'COMPLETED', 'NONE'),
                   ('tenant-b', 'b-run', 0, 'kill_switch', 'BLOCKED', 'KILL_SWITCH')
            """
        )
        conn.execute(
            "insert into pending_approvals (token, tenant_id, run_id, prompt) values "
            "('token-for-b', 'tenant-b', 'b-run', 'post this')"
        )

    return PostgresStores(dsn=APP_DSN)


# ---------------------------------------------------------------------------------------
# The test that gives the rest meaning
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_row_level_security_is_actually_in_force(migrated: None, table: str) -> None:
    """RLS enabled, and the app role is not the owner that would bypass it.

    Without the ownership half of this assertion, every other test in this file could pass
    on a database where isolation is doing nothing - the owner sees all rows regardless of
    policy. This is the difference between an isolation guarantee and a comment claiming one.
    """
    with psycopg.connect(APP_DSN) as conn:
        row = conn.execute(
            """
            select c.relrowsecurity,
                   pg_get_userbyid(c.relowner) as owner,
                   current_user
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public' and c.relname = %s
            """,
            (table,),
        ).fetchone()

    assert row is not None, f"{table} does not exist"
    rls_enabled, owner, connected_role = row
    assert rls_enabled is True, f"row-level security is not enabled on {table}"
    assert owner != connected_role, (
        f"the application connects as {connected_role!r}, which owns {table} and therefore "
        "bypasses row-level security - isolation here is decorative"
    )


def test_an_unscoped_connection_sees_nothing(seeded) -> None:
    """Fail closed.

    With no `app.tenant_id` set, `current_setting(..., true)` is NULL, the policy predicate
    is NULL, and no row qualifies. A runtime bug that forgets to scope a connection leaks
    nothing rather than everything.
    """
    with psycopg.connect(APP_DSN) as conn:
        for table in TENANT_TABLES:
            count = conn.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608
            assert count == 0, f"{table} exposed {count} row(s) to an unscoped connection"


# ---------------------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------------------


def test_a_tenant_reads_only_its_own_config(seeded) -> None:
    config = seeded.load_config("tenant-a")
    assert config.tenant_id == "tenant-a"
    assert config.daily_budget_cents == 500
    assert config.kill_switch_enabled is False
    # tenant-b's row exists with a 9999 budget, a kill switch and a guardrail pattern. None
    # of it is visible here, and the query in load_config has no tenant predicate.
    assert config.blocked_input_patterns == ()


def test_each_tenant_gets_its_own_policy(seeded) -> None:
    assert seeded.load_config("tenant-b").kill_switch_enabled is True
    assert seeded.load_config("tenant-b").max_attempts == 1
    assert seeded.load_config("tenant-a").max_attempts == 3


def test_spend_totals_do_not_include_another_tenant(seeded) -> None:
    from datetime import datetime, timedelta

    since = datetime.now() - timedelta(hours=24)
    # 100c seeded for A, 4000c for B. A sum without isolation would be 4100.
    assert seeded.spend_cents_since("tenant-a", since) == 100
    assert seeded.spend_cents_since("tenant-b", since) == 4000


def test_step_trails_do_not_include_another_tenant(seeded) -> None:
    assert [r[0] for r in seeded.steps_for("tenant-a", "a-run")] == ["cost_guard"]
    # Querying B's run id under A's scope returns nothing, even though the row exists.
    assert seeded.steps_for("tenant-a", "b-run") == []


def test_a_tenant_cannot_see_another_tenants_existence(seeded) -> None:
    with psycopg.connect(APP_DSN) as conn, conn.transaction():
        conn.execute("select set_config('app.tenant_id', 'tenant-a', true)")
        rows = conn.execute("select id from tenants").fetchall()
    assert [r[0] for r in rows] == ["tenant-a"]


# ---------------------------------------------------------------------------------------
# Writes - the half that USING alone does not cover
# ---------------------------------------------------------------------------------------


def test_a_tenant_cannot_insert_a_row_labelled_as_another(seeded) -> None:
    """This is what `WITH CHECK` buys.

    With only a `USING` clause the policy hides other tenants' rows on read while happily
    accepting an insert that attributes spend, or an audit record, to someone else.
    """
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(APP_DSN) as conn, conn.transaction():
            conn.execute("select set_config('app.tenant_id', 'tenant-a', true)")
            conn.execute(
                "insert into spend_ledger (tenant_id, run_id, cents) "
                "values ('tenant-b', 'forged', 5000)"
            )


def test_a_tenant_cannot_forge_an_audit_record_for_another(seeded) -> None:
    forged = StepRecord(
        tenant_id="tenant-b",
        run_id="forged-run",
        step_index=0,
        layer="kill_switch",
        status=RunStatus.COMPLETED,
        reason=HaltReason.NONE,
        detail="looks fine",
        usage=Usage(),
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        # Scoped to A, writing a record attributed to B.
        with psycopg.connect(APP_DSN) as conn, conn.transaction():
            conn.execute("select set_config('app.tenant_id', 'tenant-a', true)")
            conn.execute(
                "insert into run_steps (tenant_id, run_id, step_index, layer, status, reason)"
                " values (%s, %s, %s, %s, %s, %s)",
                (
                    forged.tenant_id,
                    forged.run_id,
                    forged.step_index,
                    forged.layer,
                    str(forged.status),
                    str(forged.reason),
                ),
            )


def test_a_mixed_tenant_batch_is_refused_before_it_reaches_the_database(seeded) -> None:
    # A batch spanning tenants would have to be written under one tenant's scope, which
    # means writing another tenant's rows. The store refuses rather than picking one.
    with pytest.raises(ValueError, match="one tenant"):
        ok = RunStatus.COMPLETED
        none = HaltReason.NONE
        seeded.record_many(
            [
                StepRecord("tenant-a", "r", 0, "run", ok, none, "", Usage()),
                StepRecord("tenant-b", "r", 1, "run", ok, none, "", Usage()),
            ]
        )


def test_a_tenant_cannot_raise_its_own_budget_by_updating_anothers_row(seeded) -> None:
    with psycopg.connect(APP_DSN) as conn, conn.transaction():
        conn.execute("select set_config('app.tenant_id', 'tenant-a', true)")
        cur = conn.execute("update tenant_config set daily_budget_cents = 1000000")
        # The update is scoped by the policy, so it touches A's row only - not both.
        assert cur.rowcount == 1

    assert seeded.load_config("tenant-b").daily_budget_cents == 9999


def test_a_tenant_cannot_delete_anothers_records(seeded) -> None:
    with psycopg.connect(APP_DSN) as conn, conn.transaction():
        conn.execute("select set_config('app.tenant_id', 'tenant-a', true)")
        cur = conn.execute("delete from spend_ledger")
        assert cur.rowcount == 1  # A's single row, not B's

    from datetime import datetime, timedelta

    assert seeded.spend_cents_since("tenant-b", datetime.now() - timedelta(hours=24)) == 4000


def test_an_approval_token_cannot_be_consumed_by_another_tenant(seeded) -> None:
    # A capability check enforced by the database rather than by an application `if`.
    assert seeded.consume("tenant-a", "token-for-b") is False
    assert seeded.consume("tenant-b", "token-for-b") is True
    assert seeded.consume("tenant-b", "token-for-b") is False  # single use


# ---------------------------------------------------------------------------------------
# The pooling trap
# ---------------------------------------------------------------------------------------


def test_tenant_scope_does_not_survive_the_transaction(seeded) -> None:
    """Why `set_config(..., true)` and not `SET`.

    A session-scoped tenant id outlives the request. On a pooled connection the next
    borrower inherits it and reads someone else's data - a bug that is invisible until
    there is enough concurrency to reuse connections, which is exactly when it matters.
    """
    with psycopg.connect(APP_DSN) as conn:
        with conn.transaction():
            conn.execute("select set_config('app.tenant_id', 'tenant-a', true)")
            assert conn.execute("select count(*) from tenant_config").fetchone()[0] == 1

        # Same connection, new transaction, no scope set. The previous tenant must be gone.
        with conn.transaction():
            assert conn.execute("select current_setting('app.tenant_id', true)").fetchone()[0] in (
                None,
                "",
            )
            assert conn.execute("select count(*) from tenant_config").fetchone()[0] == 0


def test_an_unknown_tenant_raises_rather_than_getting_permissive_defaults(seeded) -> None:
    # An unknown tenant that silently receives the default budget and no guardrails is a
    # provisioning bug that bills someone and protects nobody.
    with pytest.raises(LookupError):
        seeded.load_config("tenant-does-not-exist")


def test_the_runtime_end_to_end_uses_only_its_own_tenants_policy(seeded) -> None:
    """Full stack: real database, real policy, real chain.

    tenant-b has the kill switch on; tenant-a does not. Same runtime, same stores, one
    connection string - the only difference is which tenant's config was loaded.
    """
    from guardrail_runtime.model import ScriptedModel
    from guardrail_runtime.ports import RecordingTraceSink
    from guardrail_runtime.runtime import Runtime
    from guardrail_runtime.types import Request

    runtime = Runtime(
        model=ScriptedModel(script=["ok", "ok"]),
        ledger=seeded,
        steps=seeded,
        approvals=seeded,
        traces=RecordingTraceSink(),
    )

    assert runtime.run(seeded.load_config("tenant-a"), Request("hello")).status is (
        RunStatus.COMPLETED
    )
    blocked = runtime.run(seeded.load_config("tenant-b"), Request("hello"))
    assert blocked.status is RunStatus.BLOCKED
    assert blocked.reason is HaltReason.KILL_SWITCH
