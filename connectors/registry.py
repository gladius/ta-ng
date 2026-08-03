"""Connector registry — auto-discovers adapters, symmetric with auditor.strategy_registry.

Every connector is a SUBPACKAGE of connectors/ that exports `ADAPTER = <Adapter subclass>()` from its
__init__.py (e.g. connectors/langsmith/). Dropping a folder here registers a platform; no dispatch to
edit. Plain modules (base, schema, __main__, inspect_runs) are skipped — only packages are adapters.
"""

import importlib
import pkgutil

import connectors as _pkg
from connectors.base import Adapter


def load_adapters():
    """name -> Adapter instance, for every connector subpackage exporting a valid ADAPTER."""
    out = {}
    for _, name, ispkg in pkgutil.iter_modules(_pkg.__path__):
        if not ispkg or name.startswith("_"):
            continue
        mod = importlib.import_module("connectors.%s" % name)
        ad = getattr(mod, "ADAPTER", None)
        if isinstance(ad, Adapter) and ad.name and ad.name != "base":
            out[ad.name] = ad
        elif ad is not None:
            print("[connectors] skip %s: bad ADAPTER (needs an Adapter subclass with a name)" % name)
    return out


def list_adapters():
    return sorted(load_adapters())


def get_adapter(name):
    ads = load_adapters()
    key = str(name).lower()
    if key in ads:
        return ads[key]
    raise ValueError("unknown connector %r (available: %s)" % (name, ", ".join(sorted(ads)) or "none"))
