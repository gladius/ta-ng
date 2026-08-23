"""Shared helpers for the LLM/gateway diag probes — kept tiny + transport-neutral so each probe
(litellm_catalog, cache_probe, and future ones) stays single-purpose, the way _common.py serves the
LangSmith diag tools.

ONE config path: base_url + key resolve via `credentials` (.env) using the SAME keys llm_client reads, so the
probes and the app never disagree on where/what the gateway is."""
import os

import credentials
credentials.load()                                          # the ONE .env loader — same as the app's connector
from auditor.util import canonical_model

# base_url + key resolved EXACTLY like llm_client.client(): direct Anthropic by default, or a LiteLLM gateway.
BASE = credentials.get_config("ANTHROPIC_BASE_URL", None, aliases=("LITELLM_BASE_URL", "LLM_BASE_URL"))
KEY = credentials.get_secret("ANTHROPIC_API_KEY", aliases=("ANTHROPIC_KEY", "CLAUDE_API_KEY", "LITELLM_API_KEY"))


def md_write(lines, out, append=False):
    """Write (or append) a report to `out`, creating parent dirs. Prints where it went."""
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out, "a" if append else "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("%s %s (%d lines)" % ("appended ->" if append else "wrote ->", out, len(lines)))


def resolve_catalog(underlying):
    """Underlying litellm model id (e.g. 'gemini/gemini-2.5-flash', 'anthropic/claude-sonnet-4-5-20250929')
    -> our catalog name, or None. Strip the provider prefix, then let canonical_model do dated/alias matching."""
    if not underlying:
        return None
    base = underlying.split("/", 1)[1] if "/" in underlying else underlying
    return canonical_model(base) or canonical_model(underlying)
