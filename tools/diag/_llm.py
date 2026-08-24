"""Shared helpers for the LLM/gateway diag probes — kept tiny + transport-neutral so each probe
(litellm_catalog, cache_probe, and future ones) stays single-purpose, the way _common.py serves the
LangSmith diag tools.

ONE config path: base_url + key resolve via `credentials` (.env) using the SAME keys llm_client reads, so the
probes and the app never disagree on where/what the gateway is."""
import os
import re

import credentials
credentials.load()                                          # the ONE .env loader — same as the app's connector
from app.catalog import canonical_model

# base_url + key: the LiteLLM gateway's PROD names (LLM_GATEWAY_URL / LLM_GATEWAY_KEY — what the Verizon deployment
# actually sets) take precedence, then the llm_client-style ANTHROPIC_BASE_URL / *_BASE_URL fallbacks for local
# testing. So the probes hit the same gateway prod does, without a per-env code change.
BASE = credentials.get_config("LLM_GATEWAY_URL", None,
                              aliases=("ANTHROPIC_BASE_URL", "LITELLM_BASE_URL", "LLM_BASE_URL"))
KEY = credentials.get_secret("LLM_GATEWAY_KEY",
                             aliases=("ANTHROPIC_API_KEY", "ANTHROPIC_KEY", "CLAUDE_API_KEY", "LITELLM_API_KEY"))


def md_write(lines, out, append=False):
    """Write (or append) a report to `out`, creating parent dirs. Prints where it went."""
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out, "a" if append else "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("%s %s (%d lines)" % ("appended ->" if append else "wrote ->", out, len(lines)))


# A Bedrock/Vertex underlying id nests route + vendor namespace + inference-profile version, e.g.
#   bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0   vertex_ai/google/gemma-4-26b-a4b-it-maas
# so the naive one-prefix strip missed nearly every Bedrock-Claude. Peel ALL route segments, the region.vendor
# namespace, and the trailing bedrock version, then let canonical_model finish (dated/alias/prefix matching).
_VENDOR_RX = re.compile(r"^(?:(?:us|eu|apac|global)\.)?"
                        r"(?:anthropic|google|openai|meta|amazon|mistral|cohere|ai21|deepseek)\.")
_BEDROCK_VER_RX = re.compile(r"[-:]v\d+(?::\d+)?$|:\d+$")    # trailing -v1:0 / v1 / :0


def resolve_catalog(underlying):
    """Underlying litellm model id -> our catalog name, or None. Handles plain (`gemini/gemini-2.5-flash`),
    Bedrock (`bedrock/us.anthropic.claude-opus-4-8`), and Vertex (`vertex_ai/google/gemma-…`) shapes."""
    if not underlying:
        return None
    s = underlying
    while "/" in s:                                          # peel every route segment (bedrock/, vertex_ai/google/, …)
        s = s.split("/", 1)[1]
    s = _VENDOR_RX.sub("", s)                                # drop region.vendor namespace (us.anthropic. / google. …)
    s = _BEDROCK_VER_RX.sub("", s)                           # drop bedrock inference-profile version (-v1:0 / :0)
    return canonical_model(s) or canonical_model(underlying)
