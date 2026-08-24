"""The transport-agnostic usage contract (llm_client.usage): the same cache read must be recovered whether the
response is Anthropic-shaped (dev, /v1/messages) or OpenAI/LiteLLM-shaped (prod, /chat/completions). $0 — pure
shape mapping, no API. Guards against LiteLLM #27763 (cache read under different keys per shape)."""
import types
from app.services.llm_client import usage


def _resp(usage_obj):
    return types.SimpleNamespace(usage=usage_obj)


def test_anthropic_cold_write():
    u = usage(_resp(types.SimpleNamespace(input_tokens=10, output_tokens=1,
                                          cache_creation_input_tokens=8108, cache_read_input_tokens=0)))
    assert u == {"input": 10, "output": 1, "cache_read": 0, "cache_write": 8108}


def test_anthropic_warm_read():
    u = usage(_resp(types.SimpleNamespace(input_tokens=10, output_tokens=1,
                                          cache_creation_input_tokens=0, cache_read_input_tokens=8108)))
    assert u["cache_read"] == 8108 and u["cache_write"] == 0


def test_openai_litellm_gemini_cached_tokens():
    # OpenAI/LiteLLM shape: cache read lives in prompt_tokens_details.cached_tokens (and top-level here too).
    resp = {"usage": {"prompt_tokens": 7146, "completion_tokens": 0,
                      "prompt_tokens_details": {"cached_tokens": 4074, "text_tokens": 3072},
                      "cache_read_input_tokens": 4074}}
    u = usage(resp)
    assert u == {"input": 7146, "output": 0, "cache_read": 4074, "cache_write": 0}


def test_openai_only_details_no_toplevel():
    # the #27763 case inverted: only the nested field is present — must still be read.
    resp = {"usage": {"prompt_tokens": 5000, "completion_tokens": 2,
                      "prompt_tokens_details": {"cached_tokens": 1234}}}
    assert usage(resp)["cache_read"] == 1234


def test_bare_usage_dict_and_anthropic_passthrough_zero():
    assert usage({"input_tokens": 100, "output_tokens": 5})["input"] == 100        # bare usage, no wrapper
    # #27763 trap: /v1/messages passthrough drops cache fields -> everything reads 0 (transport issue, not ours).
    assert usage(_resp(types.SimpleNamespace(input_tokens=7146, output_tokens=0))) == \
        {"input": 7146, "output": 0, "cache_read": 0, "cache_write": 0}


def test_none_is_safe():
    assert usage(None) == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    assert usage(types.SimpleNamespace(usage=None))["input"] == 0
