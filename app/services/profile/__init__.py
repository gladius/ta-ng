"""Profile layer — per-node comprehension.

Pipeline per node: `select` (deterministic $0 — sample + full-bucket facts + bounded digests) -> `comprehend`
(one PROFILE_MODEL read -> op / summary / coherence, reconciled against `distinct_system`) -> the report.

Descriptive ONLY: the profile proposes and points at lever candidates; the levers prove downstream.
"""
from app.services.profile.select import select

__all__ = ["select"]
