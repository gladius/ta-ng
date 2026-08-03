"""End-to-end token auditor (walking skeleton).

Stages, one real trace, real Anthropic calls, no mocks:
  (1) ingest  (2) segment  (3) discover skeleton  (4) detect
  (5) optimize  (6) re-execute + judge vs baseline  (7) audit-trace record

See docs/MVP_CONCEPT.md for the design and its verified boundaries.
"""
