"""Per-tenant policy.

Every field here is a knob one tenant can turn without affecting another. That is the whole
reason this is a config object loaded per run rather than module-level settings: a platform
serving many tenants has no single correct budget, no single correct guardrail list, and no
single answer to "does a human approve this".

The authoritative copy of these values lives in the database, one row per tenant, behind a
row-level security policy (`tenancy/schema.sql`). This dataclass is what a loaded row turns
into. It is deliberately inert - no I/O, no defaults fetched at import time - so middleware
logic can be tested exhaustively without a database anywhere near it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .types import Pricing


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str

    # Absolute stop. Set when a tenant is offboarded, in incident response, or when their
    # agent is behaving badly and someone needs it to stop *now* - without a deploy.
    kill_switch_enabled: bool = False

    # Spend ceiling per rolling window. Checked before the model call and debited after.
    daily_budget_cents: int = 10_000

    # When true, no run reaches the model without a human approval token.
    hitl_required: bool = False

    # Regex patterns. Input patterns refuse the request; output patterns refuse the
    # response. Both are needed: screening only the input is half a guardrail, because the
    # model can produce something disallowed from a perfectly innocuous prompt.
    blocked_input_patterns: tuple[str, ...] = ()
    blocked_output_patterns: tuple[str, ...] = ()

    # Total model calls per run, including the first. 1 means no retries.
    max_attempts: int = 3

    pricing: Pricing = field(default_factory=Pricing)

    def __post_init__(self) -> None:
        # Everything below silently disables a safety control if wrong, so it is rejected at
        # construction - when the tenant row is loaded - rather than misbehaving per request.
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1; 0 would never call the model")
        if self.daily_budget_cents < 0:
            raise ValueError("daily_budget_cents cannot be negative")

        # An invalid regex must fail loudly here. If it were left to match time, the pattern
        # would raise on some requests and not others, or worse, be caught and treated as
        # "no match" - a guardrail that has quietly stopped guarding while still appearing
        # in the config as protection.
        for field_name in ("blocked_input_patterns", "blocked_output_patterns"):
            for pattern in getattr(self, field_name):
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(
                        f"{field_name} contains an invalid regex {pattern!r}: {exc}"
                    ) from exc
