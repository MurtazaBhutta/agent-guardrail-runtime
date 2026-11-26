"""A composable safety, spend and observability runtime around agent calls.

Seven layers wrap the model call. Policy is per tenant and lives in the database behind
row-level security, so isolation is enforced by the database rather than by remembering to
filter.
"""

from .config import TenantConfig
from .context import RunContext
from .model import AzureOpenAIModel, ClaudeModel, ScriptedModel
from .runtime import Runtime, default_layers
from .types import HaltReason, Pricing, Request, Response, RunStatus, Usage

__version__ = "0.1.0"

__all__ = [
    "AzureOpenAIModel",
    "ClaudeModel",
    "HaltReason",
    "Pricing",
    "Request",
    "Response",
    "RunContext",
    "RunStatus",
    "Runtime",
    "ScriptedModel",
    "TenantConfig",
    "Usage",
    "default_layers",
]
