"""LangSmith DataSource: workspaces and agents from the LangSmith REST API, traces through the langsmith adapter.
Every LangSmith-specific call lives here; the web layer sees only the neutral DataSource interface.
"""

import os
import json
import urllib.request

import credentials
from connectors import get_adapter, pull, pull_graph
from connectors.datasource import DataSource

credentials.load()
_KEY_ALIASES = ("LANGCHAIN_API_KEY", "LANGSMITH_KEY")


def _endpoint():
    return credentials.get_config("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com",
                                  aliases=("LANGCHAIN_ENDPOINT",)).rstrip("/")


def _get(path, tenant=None):
    headers = {"x-api-key": credentials.get_secret("LANGSMITH_API_KEY", aliases=_KEY_ALIASES) or ""}
    if tenant:
        headers["X-Tenant-Id"] = tenant                          # org-scoped keys need the workspace id
    req = urllib.request.Request(_endpoint() + path, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


class LangSmithSource(DataSource):
    name = "langsmith"

    def configured(self):
        return credentials.present("LANGSMITH_API_KEY", aliases=_KEY_ALIASES)

    def workspaces(self):
        data = _get("/workspaces")
        items = data if isinstance(data, list) else data.get("workspaces", [])
        return [{"id": w["id"], "name": w.get("display_name") or w.get("name") or w["id"]} for w in items]

    def agents(self, ws_id):
        """LangSmith projects in the workspace = the auditable agents/apps."""
        data = _get("/sessions?limit=100", tenant=ws_id)
        items = data if isinstance(data, list) else data.get("sessions", [])
        return sorted(
            [{"id": s["id"], "name": s.get("name") or s["id"], "runs": s.get("run_count")} for s in items],
            key=lambda a: (a["name"] or "").lower())

    def pull(self, ws_id, project, limit=500, since_hours=None):
        """Raw contract traces of one agent's recent whole run trees (the web layer caches and prices them)."""
        os.environ["LANGSMITH_WORKSPACE_ID"] = ws_id             # scope the SDK client to this workspace
        return pull(get_adapter("langsmith"), project=project, limit=limit, since_hours=since_hours)

    def pull_graph(self, ws_id, project, limit=500, since_hours=None):
        """The execution graph folded from the run tree (topology and node identity)."""
        os.environ["LANGSMITH_WORKSPACE_ID"] = ws_id
        return pull_graph(get_adapter("langsmith"), project=project, limit=limit, since_hours=since_hours)


SOURCE = LangSmithSource()
