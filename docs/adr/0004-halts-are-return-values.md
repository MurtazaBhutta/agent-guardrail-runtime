# ADR 0004: A halt is a return value; an exception is for what has no outcome

## Status
Accepted.

## Context
Five things can stop a run: the kill switch, an input guardrail, an output guardrail, a spend
cap, and a pending human approval. Each could reasonably be an exception - they are all
"the normal path did not happen".

But every one of them is an **expected, operationally normal outcome** of running agents in
production, and each has to be recorded, attributed to a tenant, counted, and returned to the
caller with a reason it can act on. Raising turns each into a stack trace at the call site and
pushes the classification work onto every caller - who then reconstructs, from an exception
type, the thing the runtime already knew.

There is a second problem. If halts raise, the layers that halt cannot report usage. An output
blocked by a guardrail consumed tokens; an exception carrying no usage would silently
under-report spend for exactly the tenants generating the most disallowed output.

## Decision
Halting is a return. Every layer returns a `Response` carrying a `RunStatus`, a `HaltReason`
from a closed set, a human-readable detail, and **the usage incurred so far**.

Exceptions are reserved for conditions the runtime cannot describe as an outcome:

- `ModelUnavailable` - the model could not be reached. Transient by assumption, so
  `model_retry` catches it; exhausted retries become a `FAILED` *response*, which is an
  outcome.
- Anything else (a `ValueError`, a database error) escapes the chain - but only after the
  tracer and step recorder have written the run, so an escape is still observable.

Two supporting rules follow:

- `HaltReason` is a closed enum, not free text. A reason nobody can count is a reason nobody
  acts on, and free-text reasons cannot be alerted on or reported by frequency.
- One mapping from reason to status, shared by both construction paths, so a condition cannot
  report different statuses depending on which layer produced it.

## Consequences
- Callers switch on `response.status` instead of catching five exception types.
- `Response.halted()` preserves usage, so spend survives a block. This is asserted, because
  it is the kind of thing a refactor loses quietly.
- `reached_model` is derived from `usage.attempts` rather than from status, which is what
  lets the cost guard bill a `FAILED` run that burned tokens across three attempts.
- The same philosophy appears in the sibling repo `llm-release-gate`, where a model's
  contract violation is *scored* rather than raised. A traceback is not a metric; here, a
  traceback is not an outcome.
