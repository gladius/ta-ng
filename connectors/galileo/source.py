"""Galileo dashboard data source — navigation over Galileo's project API + the galileo Adapter.

ALL Galileo-specific API calls live here (agents = GET /v2/projects; pull = the galileo adapter's
spans/search). Galileo has no "workspace" layer, so workspaces() returns one default scope and agents()
lists Galileo PROJECTS. Docs-based, like the adapter — the offline + mock paths are exercised
(connectors/galileo/test_live_mock.py); the project-listing shape should be confirmed against a real
instance (it degrades to an empty list on error, never crashes the dashboard).
"""

import credentials
from connectors import get_adapter, pull
from connectors.datasource import DataSource

_DEFAULT_WS = {"id": "galileo", "name": "Galileo"}   # Galileo has no workspace concept -> one default scope


class GalileoSource(DataSource):
    name = "galileo"

    def configured(self):
        return credentials.present("GALILEO_API_KEY", files=(".galileo_key", ".env"))

    def workspaces(self):
        return [_DEFAULT_WS]

    def agents(self, ws_id):
        """Galileo projects = the auditable agents. Reuses the adapter's authed HTTP (retry + redaction)."""
        ad = get_adapter("galileo")
        base = credentials.get_config("GALILEO_API_URL", "https://api.galileo.ai").rstrip("/")
        key = credentials.get_secret("GALILEO_API_KEY", files=(".galileo_key", ".env"))
        if not key:
            return []
        try:
            data = ad._request("GET", "%s/v2/projects" % base, key)
        except Exception:
            return []                                            # a nav call must never break the page
        items = data if isinstance(data, list) else (data.get("projects") or data.get("records") or [])
        out = [{"id": p.get("id") or p.get("project_id"), "name": p.get("name") or p.get("id"),
                "runs": p.get("run_count")} for p in items if isinstance(p, dict)]
        return sorted(out, key=lambda a: (a["name"] or "").lower())

    def pull(self, ws_id, project, limit=500, since_hours=None):
        """RAW contract traces via the galileo adapter (`project` = the Galileo project id or name)."""
        return pull(get_adapter("galileo"), project_id=project, limit=limit)


SOURCE = GalileoSource()
