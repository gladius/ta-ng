"""The ONE credential-resolution path — read-only, env-first, never logged.

Every secret the auditor needs (LLM keys, connector tokens) resolves through here, so there is exactly
one auditable place that touches credentials. Precedence:

    1. process environment  (the production path — inject from a secrets manager, never a file)
    2. a gitignored file at repo root  (DEV CONVENIENCE ONLY — see SECURITY.md)

A value is returned to the caller for the API call and is NEVER logged, printed, or written back to
disk. `redact()` masks token-shaped strings for any defensive logging. See docs/SECURITY.md for the
production model (least-privilege, read-only, per-workspace-scoped tokens, rotation).
"""

import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_SEARCH_DIRS = (_HERE,)                                     # repo root — .env here (dev) or the process env (prod)

# token shapes we mask in logs (Anthropic sk-ant-, LangSmith lsv2_/ls__, generic gl_/api key-ish)
_SECRET_RX = re.compile(r"\b(sk-ant-[A-Za-z0-9_\-]{6,}|lsv2_[A-Za-z0-9_]{6,}|ls__[A-Za-z0-9]{6,}|"
                        r"gl_[A-Za-z0-9_]{6,}|sk-[A-Za-z0-9]{16,})\b")

# --- the ONE .env loader for the whole app -----------------------------------------------------------
_ENV_FILES = (".env",)
# real-world env-var aliases: mirror them across the group so every consumer (langsmith SDK, LangChain
# tracer, connectors) agrees on one value regardless of which name the user saved it under.
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
    """THE single config loader: read the gitignored .env (repo root) into the process env ONCE, then mirror
    known aliases so every consumer agrees. The real process env wins
    (override=False). Idempotent + auto-invoked by get_secret/get_config — so ANY config access loads the
    whole app's .env, and there is exactly one loader. No python-dotenv dependency."""
    global _loaded
    if _loaded and not override:
        return
    for d in _SEARCH_DIRS:                                    # repo root
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
    """Resolve a secret by env var `name` (or any `aliases`) — auto-loading .env once — else the given
    gitignored `files`. Returns the value or None. A file may hold `NAME=value` (dotenv style) or the bare
    token. utf-8-sig tolerates a Windows BOM. Never logs or persists the value.
    """
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
            if txt and "=" not in txt:                       # a file holding only the bare token
                return txt.strip().strip('"').strip("'")
    return None


def present(name, *, aliases=(), files=()):
    """True if the secret resolves — WITHOUT exposing it (safe for status/health output)."""
    return bool(get_secret(name, aliases=aliases, files=files))


def get_config(name, default=None, *, aliases=(), files=(".env",), search_dirs=_SEARCH_DIRS):
    """Non-secret config (host, endpoint, header name, project id …) with the SAME resolution as a secret:
    env var `name` (or `aliases`) — auto-loading .env once — else `default`. So host + key + config all come
    from one place — set them in `.env` (or the process env) and everything picks them up."""
    v = get_secret(name, aliases=aliases, files=files, search_dirs=search_dirs)
    return v if v is not None else default


def require_secret(name, *, aliases=(), files=(), hint=""):
    """Resolve or raise a message that names WHERE to set it — never the value itself."""
    v = get_secret(name, aliases=aliases, files=files)
    if v:
        return v
    where = "env var %s" % name
    if files:
        where += " (or a gitignored %s at repo root)" % " / ".join(files)
    raise RuntimeError("%s not set — provide it via %s%s" % (name, where, (". " + hint) if hint else ""))
