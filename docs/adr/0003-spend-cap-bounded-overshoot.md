# ADR 0003: The spend cap admits a bounded overshoot rather than pretending to be exact

## Status
Accepted.

## Context
A spend cap wants to guarantee "this tenant never spends more than N". It cannot, because the
cost of a call is not knowable until the call has happened. Token counts depend on what the
model generates.

Three options:

1. **Pre-check only against the current ledger.** Refuse when spend already meets the cap.
   Simple, but a run that starts just under the cap is allowed to finish and is billed in
   full, so the cap can be exceeded.
2. **Estimate before calling** and refuse if the estimate would exceed. The estimate is a
   guess about output length; too conservative and legitimate work is refused, too generous
   and the overshoot returns anyway.
3. **Stream and abort mid-generation** when spend crosses the line. Closest to exact, and it
   still bills the partial output, and it makes every call stateful and cancellable.

## Decision
Option 1, with the limitation stated rather than hidden.

The pre-check refuses only when the ledger has already reached the cap. Actual usage is
debited after the call, including every retry attempt. A single run can therefore overshoot
the cap by up to one run's cost.

The overshoot is **bounded by one run and cannot repeat**: once the debit lands, the ledger
is at or over the cap, and the next run is refused. `test_the_cap_can_be_overshot_by_at_most_one_run`
pins both halves of that - the overshoot happens, and the run after it is stopped.

## Consequences
- The cap is a rate limiter with one run of slack, not an accounting guarantee. Anyone
  relying on it for hard financial limits needs option 3 and should know that before reading
  the code, so the README says it too.
- The behaviour is honest and testable, which a guessed estimate would not be: with option 2
  the effective cap depends on how good the estimate is, and nobody can state the bound.
- Retries widen the overshoot in proportion to `max_attempts`, since all attempts are billed.
  A tenant with `max_attempts=10` has a correspondingly larger single-run bound.
- The window is a rolling 24 hours computed from an injected clock, so the boundary is
  testable - and the boundary is the only interesting part of a spend window.
