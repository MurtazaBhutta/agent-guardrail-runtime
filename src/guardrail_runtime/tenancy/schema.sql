-- Multi-tenant storage with isolation enforced by the database.
--
-- The premise: filtering by tenant in application code is one forgotten WHERE clause away
-- from a cross-tenant leak, and that leak is silent, in production, in front of a customer.
-- Row-level security moves the rule into the database, where forgetting is not an option.
--
-- Two details decide whether that is a real guarantee or a decorative one:
--
--   1. THE APPLICATION MUST NOT OWN THESE TABLES. A table owner bypasses row-level
--      security. "We enabled RLS" while connecting as the owner is the most common way an
--      isolation guarantee turns out to do nothing at all. Hence the separate
--      `guardrail_app` role below, and hence the test that asserts at run time that the
--      connected role is not the owner.
--
--   2. EVERY POLICY NEEDS BOTH `USING` AND `WITH CHECK`. `USING` governs which rows are
--      visible to select/update/delete. `WITH CHECK` governs which rows may be written.
--      With `USING` alone, a tenant cannot *read* another tenant's rows but can happily
--      *insert* rows labelled with someone else's tenant id.
--
-- Provisioning a new tenant is an owner operation, performed by migrations rather than by
-- the application role - which is why `tenants` is readable but not writable by the app.

create table if not exists tenants (
    id   text primary key,
    name text not null
);

create table if not exists tenant_config (
    tenant_id              text primary key references tenants (id) on delete cascade,
    kill_switch_enabled    boolean       not null default false,
    daily_budget_cents     integer       not null default 10000 check (daily_budget_cents >= 0),
    hitl_required          boolean       not null default false,
    blocked_input_patterns text[]        not null default '{}',
    blocked_output_patterns text[]       not null default '{}',
    max_attempts           integer       not null default 3 check (max_attempts >= 1),
    -- Money as numeric, never float. A spend cap built on accumulated binary float error
    -- is a spend cap that drifts.
    cents_per_1k_input     numeric(10, 4) not null default 0.50,
    cents_per_1k_output    numeric(10, 4) not null default 2.50
);

create table if not exists run_steps (
    id            bigserial primary key,
    tenant_id     text        not null references tenants (id) on delete cascade,
    run_id        text        not null,
    step_index    integer     not null,
    layer         text        not null,
    status        text        not null,
    reason        text        not null,
    detail        text        not null default '',
    input_tokens  integer     not null default 0,
    output_tokens integer     not null default 0,
    attempts      integer     not null default 0,
    created_at    timestamptz not null default now(),
    unique (run_id, step_index)
);

create index if not exists run_steps_tenant_run_idx on run_steps (tenant_id, run_id);

create table if not exists spend_ledger (
    id         bigserial primary key,
    tenant_id  text        not null references tenants (id) on delete cascade,
    run_id     text        not null,
    cents      integer     not null check (cents >= 0),
    created_at timestamptz not null default now()
);

-- The cost guard's only read is "spend for this tenant since a timestamp", so the index
-- matches that exactly.
create index if not exists spend_ledger_tenant_time_idx
    on spend_ledger (tenant_id, created_at desc);

create table if not exists pending_approvals (
    token      text primary key,
    tenant_id  text        not null references tenants (id) on delete cascade,
    run_id     text        not null,
    prompt     text        not null,
    created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------------------
-- `current_setting('app.tenant_id', true)` returns NULL rather than raising when the
-- setting is absent. That matters: with NULL, `tenant_id = NULL` evaluates to NULL, which
-- the policy treats as false, so an unscoped connection sees nothing and can write nothing.
-- The runtime fails closed. Using the two-argument form with `false` would raise instead,
-- which is louder but turns a missing scope into a 500 rather than an empty result.

alter table tenants          enable row level security;
alter table tenant_config    enable row level security;
alter table run_steps        enable row level security;
alter table spend_ledger     enable row level security;
alter table pending_approvals enable row level security;

drop policy if exists tenant_self_read on tenants;
create policy tenant_self_read on tenants
    for select
    using (id = current_setting('app.tenant_id', true));

drop policy if exists tenant_isolation on tenant_config;
create policy tenant_isolation on tenant_config
    using (tenant_id = current_setting('app.tenant_id', true))
    with check (tenant_id = current_setting('app.tenant_id', true));

drop policy if exists tenant_isolation on run_steps;
create policy tenant_isolation on run_steps
    using (tenant_id = current_setting('app.tenant_id', true))
    with check (tenant_id = current_setting('app.tenant_id', true));

drop policy if exists tenant_isolation on spend_ledger;
create policy tenant_isolation on spend_ledger
    using (tenant_id = current_setting('app.tenant_id', true))
    with check (tenant_id = current_setting('app.tenant_id', true));

drop policy if exists tenant_isolation on pending_approvals;
create policy tenant_isolation on pending_approvals
    using (tenant_id = current_setting('app.tenant_id', true))
    with check (tenant_id = current_setting('app.tenant_id', true));

-- ---------------------------------------------------------------------------------------
-- The application role
-- ---------------------------------------------------------------------------------------
-- Deliberately not the owner of any table above, so row-level security applies to it.
-- The password here is for local development and CI only; a real deployment injects a
-- secret and never keeps one in a migration.

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'guardrail_app') then
        create role guardrail_app login password 'guardrail_app_dev_only';
    end if;
end
$$;

grant usage on schema public to guardrail_app;
grant select on tenants to guardrail_app;
grant select, insert, update, delete
    on tenant_config, run_steps, spend_ledger, pending_approvals
    to guardrail_app;
grant usage, select on all sequences in schema public to guardrail_app;
