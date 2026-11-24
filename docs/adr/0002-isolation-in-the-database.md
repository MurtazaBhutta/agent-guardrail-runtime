# ADR 0002: Tenant isolation belongs in the database, not in application code

## Status
Accepted.

## Context
The obvious way to isolate tenants is to filter by tenant id in every query. It works right
up until one query is written without the filter - and then it fails silently, in production,
in front of a customer, with no error to alert on. Nothing in a code review reliably catches
a missing `WHERE` clause among hundreds of queries, and nothing in a test suite catches it
unless a test happens to cover that exact query with more than one tenant's data present.

The failure mode is the problem: application-level filtering fails *open*.

## Decision
Isolation is a database policy. Every tenant-scoped table has row-level security enabled with
a policy keyed on a transaction-local setting, and **every query in `tenancy/store.py`
deliberately omits a tenant predicate**. If the policy were missing, those queries would
return every tenant's rows - which is what the isolation suite checks.

Four details decide whether this is a real guarantee or a decorative one.

### 1. The application must not own the tables
A table owner **bypasses row-level security**. "We enabled RLS" while connecting as the owner
is the single most common way an isolation guarantee turns out to do nothing whatsoever - and
it is invisible, because the schema looks correct and every policy is present.

So the app connects as `guardrail_app`, which owns nothing, and
`test_row_level_security_is_actually_in_force` asserts at run time that the connected role is
not the table owner. That test is what gives every other isolation test meaning: without it,
the whole suite could pass against a database where isolation is inert.

### 2. Every policy needs `WITH CHECK`, not just `USING`
`USING` governs which rows are *visible* to select, update and delete. `WITH CHECK` governs
which rows may be *written*. With `USING` alone, a tenant cannot read another tenant's rows
but can happily insert rows labelled with someone else's id - forging spend against another
tenant's budget, or planting an audit record in their trail.

### 3. `set_config(..., true)`, never `SET`
A plain `SET` is session-scoped. On a pooled connection a session-scoped tenant id outlives
the request, and the next borrower of that connection inherits it - reading another tenant's
data. That bug is invisible under low load and appears under concurrency, which is the worst
possible combination for discovering it.

The third argument to `set_config` makes the setting transaction-local, so scope cannot
outlive the transaction that set it.

### 4. `set_config` also avoids an injection point
`SET` does not accept bind parameters, so the tempting workaround is to format the tenant id
into the SQL string. That puts an injection point on the one value that decides what data is
visible. `set_config` takes it as a parameter.

## Consequences
- Isolation holds even for a query written without a tenant filter, which is the whole point.
- The policy **fails closed**: with no `app.tenant_id` set, `current_setting(..., true)`
  returns NULL, the predicate is NULL, and nothing is visible or writable. A runtime bug that
  forgets to scope a connection leaks nothing rather than everything.
- Tenant provisioning is an owner operation performed by migrations, because the application
  role deliberately cannot create tenants.
- The in-memory stores used by the middleware tests enforce isolation only by convention -
  they filter on the tenant id they were handed. That is a test double, not a second
  implementation of the guarantee, and the README says so.
- The isolation tests need a real PostgreSQL instance. They cannot be faked with SQLite,
  which has no row-level security, and a mock would only ever assert that the mock behaves
  like the mock.
