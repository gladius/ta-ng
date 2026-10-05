"""Observability connectors — pluggable adapters that turn recorded platform traces into Token
Auditor traces (contract-conformant). The live dashboard pulls through these.

Each connector is a SUBPACKAGE exporting ADAPTER; connectors.registry auto-discovers them.
Add a platform = drop a folder; nothing central to edit.
"""

from connectors.base import Adapter, pull, pull_graph
from connectors.registry import get_adapter

__all__ = ["Adapter", "pull", "pull_graph", "get_adapter"]
