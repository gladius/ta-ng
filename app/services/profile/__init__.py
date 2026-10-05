"""Profile layer — per-node comprehension.

Pipeline per node: `select` (deterministic $0 — sample + full-bucket facts + bounded digests) -> `comprehend`
(one PROFILE_MODEL read -> op / summary / coherence, reconciled against `distinct_system`) -> the report.

Descriptive ONLY: the profile proposes and points at downgrade candidates; the downgrade proof runs downstream.
"""