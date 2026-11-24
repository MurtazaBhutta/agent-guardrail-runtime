"""PostgreSQL implementations of the storage ports.

Every statement runs inside a transaction that first scopes itself to one tenant. Two
details in `_tenant_tx` are the whole design:

**`set_config(..., true)`, not `SET`.** The third argument makes the setting *transaction*
local. A plain `SET` is session-scoped, and on a pooled connection a session-scoped tenant
id outlives the request - the next borrower of that connection inherits it and reads someone
else's data. That bug is invisible under low load and appears under concurrency, which is
the worst possible combination.

**`set_config` rather than string interpolation.** `SET` does not accept bind parameters, so
the tempting workaround is to format the tenant id into the SQL. That is an injection point
on the one value that decides what data is visible. `set_config` takes it as a parameter.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ..config import TenantConfig
from ..ports import StepRecord
from ..types import Pricing

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def schema_sql() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


@dataclass
class PostgresStores:
    """All four ports over one connection string.

    Kept as one object because they share the tenant-scoping rule, and that rule is the part
    that must not be reimplemented per store.
    """

    dsn: str

    @contextmanager
    def _tenant_tx(self, tenant_id: str) -> Iterator[object]:
        import psycopg

        with psycopg.connect(self.dsn) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    # Transaction-local, parameterised. See the module docstring.
                    cur.execute("select set_config('app.tenant_id', %s, true)", (tenant_id,))
                    yield cur

    # -- SpendLedger ---------------------------------------------------------------------

    def spend_cents_since(self, tenant_id: str, since: datetime) -> int:
        with self._tenant_tx(tenant_id) as cur:
            # No tenant predicate in the WHERE clause. That is the point: the policy adds
            # it. If the policy were missing or the role owned the table, this query would
            # return every tenant's spend - which is exactly what the isolation tests check.
            cur.execute(
                "select coalesce(sum(cents), 0) from spend_ledger where created_at >= %s",
                (since,),
            )
            return int(cur.fetchone()[0])

    def debit(self, tenant_id: str, run_id: str, cents: int) -> None:
        with self._tenant_tx(tenant_id) as cur:
            cur.execute(
                "insert into spend_ledger (tenant_id, run_id, cents) values (%s, %s, %s)",
                (tenant_id, run_id, cents),
            )

    # -- StepStore ----------------------------------------------------------------------

    def record(self, step: StepRecord) -> None:
        self.record_many([step])

    def record_many(self, steps: Sequence[StepRecord]) -> None:
        if not steps:
            return
        tenant_ids = {s.tenant_id for s in steps}
        if len(tenant_ids) != 1:
            # A batch spanning tenants would have to be written under one tenant's scope,
            # which means writing another tenant's rows. Refuse rather than pick one.
            raise ValueError(f"a step batch must belong to one tenant, got {sorted(tenant_ids)}")

        with self._tenant_tx(steps[0].tenant_id) as cur:
            cur.executemany(
                """
                insert into run_steps
                    (tenant_id, run_id, step_index, layer, status, reason, detail,
                     input_tokens, output_tokens, attempts)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        s.tenant_id,
                        s.run_id,
                        s.step_index,
                        s.layer,
                        str(s.status),
                        str(s.reason),
                        s.detail,
                        s.usage.input_tokens,
                        s.usage.output_tokens,
                        s.usage.attempts,
                    )
                    for s in steps
                ],
            )

    def steps_for(self, tenant_id: str, run_id: str) -> list[tuple]:
        with self._tenant_tx(tenant_id) as cur:
            cur.execute(
                "select layer, status, reason from run_steps where run_id = %s "
                "order by step_index",
                (run_id,),
            )
            return list(cur.fetchall())

    # -- ApprovalStore -------------------------------------------------------------------

    def create_pending(self, tenant_id: str, run_id: str, prompt: str) -> str:
        import secrets

        token = secrets.token_urlsafe(24)
        with self._tenant_tx(tenant_id) as cur:
            cur.execute(
                "insert into pending_approvals (token, tenant_id, run_id, prompt) "
                "values (%s, %s, %s, %s)",
                (token, tenant_id, run_id, prompt),
            )
        return token

    def consume(self, tenant_id: str, token: str) -> bool:
        with self._tenant_tx(tenant_id) as cur:
            # DELETE ... RETURNING makes consumption atomic: two concurrent presentations of
            # the same token cannot both succeed, because only one delete can match a row.
            # A select-then-delete would let both through under concurrency, turning a
            # single-use approval into a reusable one exactly when it is being abused.
            cur.execute("delete from pending_approvals where token = %s returning token", (token,))
            return cur.fetchone() is not None

    # -- Config loading ------------------------------------------------------------------

    def load_config(self, tenant_id: str) -> TenantConfig:
        with self._tenant_tx(tenant_id) as cur:
            cur.execute(
                """
                select tenant_id, kill_switch_enabled, daily_budget_cents, hitl_required,
                       blocked_input_patterns, blocked_output_patterns, max_attempts,
                       cents_per_1k_input, cents_per_1k_output
                from tenant_config
                """
            )
            row = cur.fetchone()

        if row is None:
            # Either the tenant does not exist, or it exists and the policy hid it. Both are
            # "no policy for this tenant", and the runtime must not invent a permissive
            # default - an unknown tenant that silently gets the default budget and no
            # guardrails is a provisioning bug that bills someone.
            raise LookupError(f"no tenant_config visible for tenant {tenant_id!r}")

        return TenantConfig(
            tenant_id=row[0],
            kill_switch_enabled=row[1],
            daily_budget_cents=row[2],
            hitl_required=row[3],
            blocked_input_patterns=tuple(row[4]),
            blocked_output_patterns=tuple(row[5]),
            max_attempts=row[6],
            pricing=Pricing(
                cents_per_1k_input=Decimal(str(row[7])),
                cents_per_1k_output=Decimal(str(row[8])),
            ),
        )
