"""The one credential path: process env first, then the gitignored .env at repo root (dev only).
Values are never logged, printed or written back; `redact()` masks token-shaped strings."""

import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_SEARCH_DIRS = (_HERE,)

_SECRET_RX = re.compile(r"\b(sk-ant-[A-Za-z0-9_\-]{6,}|lsv2_[A-Za-z0-9_]{6,}|ls__[A-Za-z0-9]{6,}|"
                        r"gl_[A-Za-z0-9_]{6,}|sk-[A-Za-z0-9]{16,})\b")

_ENV_FILES = (".env",)
# Aliases are mirrored so the langsmith SDK, LangChain tracer and connectors all see one value.
_ALIAS_GROUPS = (
    ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY", "LANGSMITH_KEY"),
    ("LANGSMITH_ENDPOINT", "LANGCHAIN_ENDPOINT"),
    ("LANGSMITH_WORKSPACE_ID", "LANGCHAIN_WORKSPACE_ID"),
)
_loaded = False


def _parse_env_file(path):
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def load(*, override=False):
    """Load the repo-root .env into the process env once (the real env wins unless `override`), then mirror
    aliases. Auto-invoked by get_secret/get_config."""
    global _loaded
    if _loaded and not override:
        return
    for d in _SEARCH_DIRS:
        for fn in _ENV_FILES:
            for k, v in _parse_env_file(os.path.join(d, fn)).items():
                if override or k not in os.environ:
                    os.environ[k] = v
    for group in _ALIAS_GROUPS:
        val = next((os.environ[k] for k in group if os.environ.get(k)), None)
        if val:
            for k in group:
                os.environ[k] = val
    _loaded = True


def _ensure_loaded():
    if not _loaded:
        load()


def redact(text):
    """Mask token-shaped substrings so a value can never reach a log line intact."""
    return _SECRET_RX.sub(lambda m: m.group(0)[:6] + "…REDACTED", str(text))


def get_secret(name, *, aliases=(), files=(), search_dirs=_SEARCH_DIRS):
    """Env var `name` (or `aliases`), else the gitignored `files` (`NAME=value` or a bare token), else None."""
    _ensure_loaded()
    for n in (name, *aliases):
        v = os.environ.get(n)
        if v:
            return v.strip()
    for d in search_dirs:
        for fn in files:
            p = os.path.join(d, fn)
            if not os.path.exists(p):
                continue
            txt = open(p, encoding="utf-8-sig").read().strip()
            m = re.search(re.escape(name) + r"\s*=\s*(\S+)", txt)
            if m:
                return m.group(1).strip().strip('"').strip("'")
            if txt and "=" not in txt:
                return txt.strip().strip('"').strip("'")
    return None


def present(name, *, aliases=(), files=()):
    """True if the secret resolves, without exposing it."""
    return bool(get_secret(name, aliases=aliases, files=files))


def get_config(name, default=None, *, aliases=(), files=(".env",), search_dirs=_SEARCH_DIRS):
    """Non-secret config, resolved like a secret (process env, then .env), else `default`."""
    v = get_secret(name, aliases=aliases, files=files, search_dirs=search_dirs)
    return v if v is not None else default
