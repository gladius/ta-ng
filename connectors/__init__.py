"""Observability connectors — pluggable adapters that turn recorded platform traces into Token
Auditor traces (contract-conformant). The live dashboard (web/lsource.py) pulls through these.

Each connector is a SUBPACKAGE exporting ADAPTER; connectors.registry auto-discovers them (symmetric
with auditor's strategy registry). Add a platform = drop a folder; nothing central to edit.
"""

from connectors.base import Adapter, pull, pull_graph
from connectors.registry import get_adapter, list_adapters

__all__ = ["Adapter", "pull", "pull_graph", "get_adapter", "list_adapters"]
