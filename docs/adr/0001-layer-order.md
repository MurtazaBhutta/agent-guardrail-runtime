# ADR 0001: The layer order is the design

## Status
Accepted.

## Context
Seven layers wrap the model call. Which one is outermost is not a stylistic choice - each
position encodes a decision about what gets observed, what gets charged, and what gets
re-run. Get the order wrong and the controls still *look* present while behaving wrongly.

## Decision
Outermost first:

| # | Layer | Why here |
|---|---|---|
| 1 | `trace` | Must see every outcome, including halts and escaped exceptions. A trace covering only successful runs hides the runs someone needs to explain. |
| 2 | `step_recorder` | Same reason, one level in: the audit trail must contain blocked and paused runs. |
| 3 | `kill_switch` | Cheapest and most absolute. Reads one boolean before any regex is compiled, any ledger read, any model call. |
| 4 | `guardrail` | Screens input before spend. Screens output on the way back - **outside** the cost guard, so a refused output is still billed. |
| 5 | `hitl` | Outside the cost guard, so a pause is never charged. Inside the guardrail, so disallowed content is refused rather than queued for a human. |
| 6 | `cost_guard` | Outside the retry layer, so the usage it debits is the total across every attempt rather than one nominal call. |
| 7 | `model_retry` | Innermost. A retry re-runs only the model call. |

Three of those positions carry consequences worth stating plainly.

**A blocked output is still billed.** The guardrail sits outside the cost guard, so by the
time the output is screened the debit has happened. This is correct: the tokens were
generated, so the money was spent. Refusing to deliver the text does not refund it, and a
runtime that skipped the debit would under-report spend for exactly the tenants generating
the most disallowed output.

**A pause is never billed.** `hitl` sits outside the cost guard. Charging for approval pauses
would make the safest configuration the most expensive one, which is precisely backwards.

**Retries do not re-run the outer layers.** If retries wrapped `hitl`, a flapping model would
ask a human to approve the same run once per attempt. If they wrapped `guardrail`, every
retry would re-screen an input already screened. Innermost is the only position where a
retry means "call the model again" and nothing else.

## Consequences
- `test_chain.py` asserts the exact order, so a reshuffle is a deliberate change with this
  ADR updated rather than a silent behavioural shift.
- Adding a layer means deciding its position against the table above, not appending it.
- The order is declared once, in `default_layers()`, and is readable top to bottom. The
  platform's safety posture is one expression rather than decorators spread over call sites.
