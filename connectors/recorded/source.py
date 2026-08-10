"""Recorded-traces data source — reads frozen contract traces from local JSON, no cloud.

An out-of-path auditor consumes RECORDED history; it does not need a live platform connection. This source
makes that literal: each `fixtures/<agent>.json` is a captured export the auditor reads deterministically. It
exists so the demo is STABLE (identical numbers on every reload — a live pull returns different runs each time)
and so the auditor works offline. It implements the exact same DataSource interface as LangSmith/Galileo, so the
web layer treats it identically — one more platform in the picker, nothing special-cased.

A fixture file: {"agent": name, "workspace": "recorded", "traces": [ <contract trace>, ... ]}.
Regenerate the shipped fixtures with `python -m connectors.recorded.build_fixture`.
"""
import os
import json
import glob
from collections import OrderedDict

import credentials
from connectors.datasource import DataSource

_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


class RecordedSource(DataSource):
    name = "recorded"

    def configured(self):
        # DEV/DEMO source: fixtures on disk AND not running in production. Set APP_ENV=prod (in .env) to hide it
        # from the datasource picker on a production deployment; it defaults to dev so local/demo shows it.
        if credentials.get_config("APP_ENV", "dev").strip().lower() == "prod":
            return False
        return os.path.isdir(_DIR) and bool(glob.glob(os.path.join(_DIR, "*.json")))

    def _agents(self):
        out = OrderedDict()
        for fp in sorted(glob.glob(os.path.join(_DIR, "*.json"))):
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
            out[d["agent"]] = d
        return out

    def workspaces(self):
        return [{"id": "recorded", "name": "Recorded traces"}]

    def agents(self, ws_id):
        return [{"id": a, "name": a, "runs": len(d.get("traces", []))} for a, d in self._agents().items()]

    def pull(self, ws_id, project, limit=None, since_hours=None):
        """RAW contract traces for one agent, grouped into call-site buckets (one per node_id). Deterministic."""
        d = self._agents().get(project)
        if not d:
            return OrderedDict(), []
        buckets = OrderedDict()
        for t in d.get("traces", [])[: (limit or None)]:
            key = "%s/%s" % (project, t.get("node_id") or "node")
            buckets.setdefault(key, []).append(t)
        return buckets, []


SOURCE = RecordedSource()
