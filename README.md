# agent-guardrail-runtime

[![ci](https://github.com/MurtazaBhutta/agent-guardrail-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/MurtazaBhutta/agent-guardrail-runtime/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Seven composable layers around an agent's model call — kill switch, guardrails, human
approval, spend cap, audit trail, tracing, retries — with per-tenant policy enforced by
PostgreSQL row-level security rather than by remembering to filter.**

Its sibling repo, [llm-release-gate](https://github.com/MurtazaBhutta/llm-release-gate),
stops a bad agent from *shipping*. This one stops a running agent from *doing damage*.

---

## The problem

An agent in production needs answers to questions the prompt cannot address:

- Someone needs this tenant's agent stopped **now**, at 2am, without a deploy.
- This tenant's spend must not exceed a ceiling, and retries count.
- This tenant requires a human to approve before anything is posted.
- Every one of those decisions must be explainable months later, per tenant.
- And no tenant may ever see another tenant's data — not their config, spend, audit trail,
  or approval tokens.

Scattering that across decorators and `if` statements at call sites means the platform's
safety posture cannot be read anywhere. Here it is one list, top to bottom, in
[`runtime.py`](src/guardrail_runtime/runtime.py).

## See it fire

```bash
pip install -e ".[dev]"
python -m examples.walkthrough
```

No database, no API key, no network. Each scenario changes exactly one thing:

```
4. Output guardrail: refused AFTER generation, so still billed
  status      BLOCKED
  reason      OUTPUT_GUARDRAIL
  attempts    1
  billed      2c
  trail       kill_switch -> guardrail -> hitl -> cost_guard -> model_retry -> run

5. Human in the loop: pauses, and a pause is never billed
  status      AWAITING_APPROVAL
  attempts    0
  billed      0c
  trail       kill_switch -> guardrail -> hitl -> run

7. Two transient failures then success: all three attempts billed
  status      COMPLETED
  attempts    3
  billed      3c
```

Read those three together and the design shows: **the ledger reflects spend, not delivery.**

### Claude or Azure OpenAI at the centre

The layers wrap a two-method `ModelClient` port, so the provider is a constructor choice and
nothing else changes. Both live clients map errors the same way: connection errors, rate
limits and 5xx become the retryable `ModelUnavailable`; a refusal or content-filter stop is
never reported as an empty answer; a 4xx is raised, not retried.

```python
from guardrail_runtime import AzureOpenAIModel, ClaudeModel

model = ClaudeModel()          # pip install -e ".[claude]"; ANTHROPIC_API_KEY
model = AzureOpenAIModel()     # pip install -e ".[azure]"; see below
```

`AzureOpenAIModel` uses the Azure OpenAI v1 API through the `openai` SDK and reads
`AZURE_OPENAI_ENDPOINT` (`https://<resource>.openai.azure.com`), `AZURE_OPENAI_API_KEY` and
`AZURE_OPENAI_DEPLOYMENT` (the deployment name, which Azure uses as the model id).

## The layer order is the design

Outermost first. Each position encodes a decision, and
[ADR 0001](docs/adr/0001-layer-order.md) records why.

| # | Layer | Why here |
|---|---|---|
| 1 | `trace` | Must see every outcome, halts and escaped exceptions included |
| 2 | `step_recorder` | Audit trail must contain blocked and paused runs, not just successes |
| 3 | `kill_switch` | One boolean, before any regex, ledger read, or model call |
| 4 | `guardrail` | Input screened before spend; output screened **after**, so a refused output is still billed |
| 5 | `hitl` | Outside the cost guard, so a pause is never charged |
| 6 | `cost_guard` | Outside the retry layer, so it debits every attempt, not one nominal call |
| 7 | `model_retry` | Innermost — a retry re-runs only the model call |

Three consequences worth stating plainly:

- **A blocked output is still billed.** The tokens were generated, so the money was spent.
  Refusing to deliver the text does not refund it, and skipping the debit would under-report
  spend for exactly the tenants generating the most disallowed output.
- **A pause is never billed.** Charging for approval pauses would make the safest
  configuration the most expensive one.
- **Retries do not re-run the outer layers.** If they wrapped `hitl`, a flapping model would
  ask a human to approve the same run once per attempt.

## Isolation is enforced by the database

Filtering by tenant in application code works until one query is written without the filter.
Then it fails **silently, in production, in front of a customer** — no error to alert on.
Application-level filtering fails *open*.

So every query in [`tenancy/store.py`](src/guardrail_runtime/tenancy/store.py) **deliberately
omits a tenant predicate**, and the policy adds it. If RLS were missing or bypassed, those
queries would return every tenant's rows.

Four details decide whether that is a real guarantee or a decorative one
([ADR 0002](docs/adr/0002-isolation-in-the-database.md)):

1. **The app must not own the tables.** A table owner *bypasses* RLS. "We enabled RLS" while
   connecting as owner is the most common way an isolation guarantee turns out to be inert —
   and it is invisible, because the schema looks perfect.
2. **Policies need `WITH CHECK`, not just `USING`.** `USING` hides other tenants' rows on
   read; without `WITH CHECK` a tenant can still *insert* rows labelled as someone else —
   forging spend against another tenant's budget.
3. **`set_config(..., true)`, never `SET`.** A plain `SET` is session-scoped, so on a pooled
   connection the tenant id outlives the request and the next borrower inherits it. Invisible
   under low load; appears under concurrency.
4. **`set_config` also avoids an injection point,** since `SET` takes no bind parameters.

### The test that gives the others meaning

`test_row_level_security_is_actually_in_force` asserts at run time that RLS is enabled **and
that the connected role is not the table owner.** Without that second half, every other
isolation test could pass against a database where isolation does nothing.

**19 isolation tests** run against real PostgreSQL, covering reads, writes, updates, deletes,
cross-tenant approval tokens, the pooling trap, and fail-closed behaviour for an unscoped
connection.

## Test layout

| Suite | Count | Needs |
|---|---|---|
| Middleware policy | **91** | nothing — no database, no credentials, no network |
| Cross-tenant isolation | **19** | real PostgreSQL (`make db-up`) |

```bash
make test        # 91 middleware tests
make db-up       # PostgreSQL + schema
make test-all    # all 110
```

Two CI jobs mirror that split. The isolation job additionally **fails if fewer than 15
isolation tests are collected** — otherwise a missing service container would turn 19
security assertions into 19 silent skips and a green tick.

## Honest limits

- **The spend cap admits a bounded overshoot.** Token counts are not knowable before the
  call, so the pre-check refuses only once the ledger has already reached the cap. A run that
  starts under it finishes and is billed in full. The overshoot is bounded by one run and
  cannot repeat — pinned by a test, explained in [ADR 0003](docs/adr/0003-spend-cap-bounded-overshoot.md).
  It is a rate limiter with one run of slack, not an accounting guarantee.
- **The in-memory stores enforce isolation only by convention.** They filter on the tenant id
  they were handed. They are test doubles for the middleware suite, not a second
  implementation of the guarantee. Only PostgreSQL enforces isolation here.
- **Guardrails are regex, not classifiers.** Deliberate: regex is deterministic, auditable,
  and free. Real semantic screening needs a model, which is a different repo with different
  latency and cost properties.
- **Retries widen the overshoot** in proportion to `max_attempts`, since all attempts bill.
- **The live backends are not called in CI.** `AzureOpenAIModel`'s request shape and error
  mapping are tested against a fake client that raises the SDK's real exception types;
  `ClaudeModel` is exercised by hand. The `ScriptedModel` double is what makes the retry,
  budget and guardrail behaviour exhaustively testable without a credential.
- **No async.** The chain is synchronous, which keeps the composition legible. A
  throughput-sensitive deployment would want an async variant of the same shape.

## Layout

```
src/guardrail_runtime/
  types.py         RunStatus, HaltReason, Usage, Pricing - a halt is a return value
  config.py        per-tenant policy; validates patterns when the tenant row loads
  chain.py         middleware protocol and onion composition
  context.py       per-run state; injected clock so budget windows are testable
  ports.py         storage ports + in-memory doubles
  model.py         ModelClient: ScriptedModel (tests) | ClaudeModel | AzureOpenAIModel (live)
  middleware/      the seven layers, one file each
  runtime.py       default_layers() - the safety posture in one expression
  tenancy/
    schema.sql     tables, RLS policies, and the non-owner application role
    store.py       Postgres ports; every query omits a tenant predicate on purpose
examples/walkthrough.py   all seven layers firing, no dependencies
docs/adr/                 why it is built this way
```

## Design decisions

- [ADR 0001 — The layer order is the design](docs/adr/0001-layer-order.md)
- [ADR 0002 — Tenant isolation belongs in the database](docs/adr/0002-isolation-in-the-database.md)
- [ADR 0003 — The spend cap admits a bounded overshoot](docs/adr/0003-spend-cap-bounded-overshoot.md)
- [ADR 0004 — A halt is a return value](docs/adr/0004-halts-are-return-values.md)

## Provenance

Written from scratch as a public demonstration of a production agent-safety runtime. The
*patterns* are ones I've shipped on a multi-tenant agent platform; none of the code, schemas,
prompts, or data here comes from any employer's system. All tenants, budgets and data are
synthetic. The credentials in `schema.sql` and `docker-compose.yml` are local-development
only.

MIT licensed.
