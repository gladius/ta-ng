"""The Token Auditor trace CONTRACT — the one published, versioned definition of a trace.

Both sides depend on this and nothing else for the trace shape:
  - connectors/*  (ingest adapters)  build traces that conform to it (build_trace + validate_trace);
  - auditor/*     (the core engine)  consumes traces that conform to it (callsite_key + the field spec).

Keeping the contract here — not inside auditor or inside a connector — is what makes platform support
first-class: a new connector is "correct" iff its traces pass validate_trace / carry TRACE_FIELDS.
"""

from contract.trace import (
    TRACE_VERSION, TRACE_FIELDS, ROLES,
    norm_role, norm_content, build_trace, validate_trace, callsite_key, eligible,
)

__all__ = ["TRACE_VERSION", "TRACE_FIELDS", "ROLES",
           "norm_role", "norm_content", "build_trace", "validate_trace", "callsite_key", "eligible"]
