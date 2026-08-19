"""Shared plumbing for the read-only diag tools, so they authenticate EXACTLY like the app's connector: API key,
endpoint/host, and the workspace/tenant id all come from .env.

The subtlety that used to 403 in prod: the app never audits the .env workspace blindly — the user PICKS a workspace
from the key's authorized list (source.workspaces()), so the tenant is always one the key can access AND one that
holds the agent. A diag tool that just trusts .env's LANGSMITH_WORKSPACE_ID sends the wrong X-Tenant-Id -> 403 (or a
NotFound if the key can reach that ws but the agent lives elsewhere). So for a project-scoped command we RESOLVE the
workspace the same way the picker does: reuse the connector's SOURCE, and use the workspace that actually contains
the agent. --ws still wins (skips the lookup); .env is only the first guess."""
import os

import credentials
credentials.load()                                           # the ONE .env loader — same as the app's connector

_KEY_ALIASES = ("LANGCHAIN_API_KEY", "LANGSMITH_KEY")


def key():
    return credentials.get_secret("LANGSMITH_API_KEY", aliases=_KEY_ALIASES) or ""


def endpoint():
    return credentials.get_config("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com",
                                  aliases=("LANGCHAIN_ENDPOINT",)).rstrip("/")


def _env_ws():
    return (credentials.get_config("LANGSMITH_WORKSPACE_ID", None, aliases=("LANGCHAIN_WORKSPACE_ID",))
            or os.environ.get("LANGSMITH_WORKSPACE_ID"))


def ws_id(arg=None):
    """The workspace / tenant id when NO project is in play: an explicit --ws wins, else LANGSMITH_WORKSPACE_ID from
    .env. Exports it so the langsmith SDK sends it as X-Tenant-Id (org-scoped keys need it), like the connector.
    For a project-scoped command prefer ws_for_project() — it doesn't blindly trust the .env default."""
    w = arg or _env_ws()
    if not w:
        raise SystemExit("No workspace id — set LANGSMITH_WORKSPACE_ID in .env, or pass --ws <id>.")
    os.environ["LANGSMITH_WORKSPACE_ID"] = w
    return w


def _source():
    from connectors.langsmith.source import SOURCE          # reuse the connector's auth (x-api-key + X-Tenant-Id)
    return SOURCE


def workspaces():
    """The workspaces this key can access — the SAME list the app's picker shows (GET /workspaces, key only)."""
    return _source().workspaces()


def _has_project(ws, project):
    try:
        return any(a["name"] == project for a in _source().agents(ws))
    except Exception:
        return False                                          # forbidden / unreachable ws -> just not a match


def ws_for_project(project, arg=None):
    """Resolve the workspace that actually CONTAINS `project`, the way the app's picker does — so a stale/forbidden
    .env default can't 403 the tool. --ws (arg) wins outright. Otherwise: try the .env default first (one cheap
    /sessions call); if the agent isn't there, scan the key's authorized workspaces and use the one that has it,
    printing which (so next time you can pass --ws to skip the scan). Exports X-Tenant-Id via env either way."""
    if arg:
        os.environ["LANGSMITH_WORKSPACE_ID"] = arg
        return arg
    default = _env_ws()
    if default and _has_project(default, project):
        os.environ["LANGSMITH_WORKSPACE_ID"] = default
        return default
    tried = []
    for w in workspaces():
        if w["id"] == default:
            continue
        tried.append(w["id"])
        if _has_project(w["id"], project):
            os.environ["LANGSMITH_WORKSPACE_ID"] = w["id"]
            print("[diag] %r is in workspace %s (%s), not the .env default%s — using it "
                  "(pass --ws %s next time to skip this scan)"
                  % (project, w.get("name"), w["id"], " (%s)" % default if default else "", w["id"]))
            return w["id"]
    raise SystemExit("Project %r not found in any workspace this key can access.\n  .env default: %s\n  also scanned: %s\n"
                     "Check the agent name, or pass --ws <id> explicitly."
                     % (project, default or "(none)", ", ".join(tried) or "(none)"))


def client():
    """A langsmith Client from the SAME .env credentials the connector uses (key + endpoint). Call a ws resolver
    (ws_for_project / ws_id) FIRST so LANGSMITH_WORKSPACE_ID is exported and the client sends X-Tenant-Id."""
    from langsmith import Client
    return Client(api_key=key(), api_url=credentials.get_config("LANGSMITH_ENDPOINT", None, aliases=("LANGCHAIN_ENDPOINT",)))
