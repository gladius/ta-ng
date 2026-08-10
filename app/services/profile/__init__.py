"""Profile layer — per-node comprehension. ONE place for select / analyze / report so it stays easy to manage.

Pipeline per node: `select` (deterministic $0 — the SAME 5 the proof uses, + full-bucket facts + bounded digests)
-> `analyze_node` (one PROFILE_MODEL call — consolidated node view + per-instance component maps) -> `render`.

Descriptive + routing ONLY: the profile proposes and points; every $ claim is proven downstream by prove_transform.
"""
from app.services.profile.select import select
from app.services.profile.analyze import analyze_node
from app.services.profile.report import render

__all__ = ["select", "analyze_node", "render"]
