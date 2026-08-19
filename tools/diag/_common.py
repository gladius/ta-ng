"""Shared plumbing for the read-only diag tools, so they pull EVERYTHING from .env the same way the app's connector
does: API key, endpoint/host, and the workspace/tenant id. --ws becomes optional (defaults to LANGSMITH_WORKSPACE_ID
from .env); pass it only to override."""
import os

import credentials
credentials.load()                                           # the ONE .env loader — same as the app's connector

_KEY_ALIASES = ("LANGCHAIN_API_KEY", "LANGSMITH_KEY")


def key():
    return credentials.get_secret("LANGSMITH_API_KEY", aliases=_KEY_ALIASES) or ""


def endpoint():
    return credentials.get_config("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com",
                                  aliases=("LANGCHAIN_ENDPOINT",)).rstrip("/")


def ws_id(arg=None):
    """The workspace / tenant id: an explicit --ws wins, else LANGSMITH_WORKSPACE_ID from .env (what the app uses).
    Also exports it so the langsmith SDK sends it as X-Tenant-Id (org-scoped keys need it), exactly like the connector."""
    w = (arg or credentials.get_config("LANGSMITH_WORKSPACE_ID", None, aliases=("LANGCHAIN_WORKSPACE_ID",))
         or os.environ.get("LANGSMITH_WORKSPACE_ID"))
    if not w:
        raise SystemExit("No workspace id — set LANGSMITH_WORKSPACE_ID in .env, or pass --ws <id>.")
    os.environ["LANGSMITH_WORKSPACE_ID"] = w
    return w


def client():
    """A langsmith Client from the SAME .env credentials the connector uses (key + endpoint)."""
    from langsmith import Client
    return Client(api_key=key(), api_url=credentials.get_config("LANGSMITH_ENDPOINT", None, aliases=("LANGCHAIN_ENDPOINT",)))
