"""The profiler: one model read per call-site that says what it is (a name, what it does, its task and output
shape, what stays fixed and what changes per call), so every module calls a call-site by a name a person
recognises. Profiles live in the app's database, keyed by the call-site id; a site is profiled once."""
import datetime
import json

from sqlalchemy import Column, DateTime, Integer, String, Table, Text, delete, func, or_, select

from app.services import db, llm_client, store

MODEL_KEY = "AUDIT_PROFILE_MODEL"
DEFAULT_MODEL = "claude-sonnet-5"
TASKS = ("classify", "extract", "answer", "draft", "summarise", "converse", "orchestrate", "other")
OUTPUTS = ("json", "text", "tool_calls", "label")

profiles = Table(
    "profiles", db.metadata,
    Column("site", String(32), primary_key=True),
    Column("usecase", Text),
    Column("name", Text, nullable=False),
    Column("does", Text, nullable=False),
    Column("task", String(16), nullable=False),
    Column("output", String(16), nullable=False),
    Column("fixed", Text),
    Column("per_call", Text),
    Column("model", Text, nullable=False),          # the model that wrote the profile
    Column("tokens", Integer),
    Column("created_at", DateTime, nullable=False),
)
FIELDS = ("site", "usecase", "name", "does", "task", "output", "fixed", "per_call", "model", "tokens", "created_at")

_PROMPT = """You are naming a call-site: one place in a production system that sends a prompt to a model. Below is one
request as sent (the system prompt, which tools it could call, and the first user turn). Describe it for people who
will see hundreds of these in a dashboard. Return one json object:
- "name": 2 to 4 words a person would call this call-site (e.g. "Bill explainer", "Claim eligibility check").
- "does": one sentence, plain words, what it does for whom.
- "task": one of %s.
- "output": one of %s.
- "fixed": one short phrase: what stays the same every call (instructions, policies, examples, tool list...).
- "per_call": one short phrase: what changes per call (the question, customer details, retrieved documents...), or "".
Only the json object.

SYSTEM PROMPT:
%s

TOOLS: %s

FIRST USER TURN:
%s"""


def model():
    import credentials
    return credentials.get_config(MODEL_KEY) or DEFAULT_MODEL


def _row(r):
    d = dict(zip(FIELDS, r))
    d["created_at"] = d["created_at"].isoformat(timespec="seconds") if d["created_at"] else None
    return d


def get(site):
    """The stored profile of a call-site, or None."""
    eng = db.ready()
    with eng.connect() as c:
        r = c.execute(select(*[profiles.c[f] for f in FIELDS]).where(profiles.c.site == str(site))).first()
    if r:
        return _row(r)
    old = store.peek(("profile", site))                # profiles made before the table existed
    if old:
        return put(site, None, old)
    return None


def put(site, usecase, p):
    eng = db.ready()
    row = {"site": str(site), "usecase": usecase, "name": p["name"], "does": p["does"], "task": p["task"],
           "output": p["output"], "fixed": p.get("fixed"), "per_call": p.get("per_call"), "model": p["model"],
           "tokens": p.get("tokens"), "created_at": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)}
    with eng.begin() as c:
        c.execute(delete(profiles).where(profiles.c.site == str(site)))
        c.execute(profiles.insert().values(**row))
    return get(site)


def listing(usecase=None, q=None, page=1, per_page=50):
    """Profiled call-sites, newest first; `q` matches the name or what it does."""
    eng = db.ready()
    stmt = select(*[profiles.c[f] for f in FIELDS])
    if usecase:
        stmt = stmt.where(profiles.c.usecase == usecase)
    if q:
        like = "%%%s%%" % q
        stmt = stmt.where(or_(profiles.c.name.ilike(like), profiles.c.does.ilike(like), profiles.c.usecase.ilike(like)))
    with eng.connect() as c:
        total = c.execute(select(func.count()).select_from(stmt.subquery())).scalar()
        rows = c.execute(stmt.order_by(profiles.c.created_at.desc()).offset((page - 1) * per_page).limit(per_page)).all()
    return {"page": page, "per_page": per_page, "total": total, "profiles": [_row(r) for r in rows]}


def profile(site, usecase, system_prompt, tools, first_user=""):
    """The profile of `site`, made now if missing (one model call, the request read whole)."""
    p = get(site)
    if p:
        return p
    r = llm_client.complete(model=model(), max_tokens=1200, response_format={"type": "json_object"},
                            messages=[{"role": "user", "content": _PROMPT % (", ".join(TASKS), ", ".join(OUTPUTS),
                                                                             system_prompt, ", ".join(tools) or "none",
                                                                             first_user or "(not sampled)")}])
    txt = llm_client.text(r).strip()
    txt = txt[txt.find("{"):txt.rfind("}") + 1]
    d = json.loads(txt, strict=False)
    u = llm_client.usage(r)
    return put(site, usecase, {"name": str(d.get("name") or "")[:60], "does": str(d.get("does") or "")[:300],
                               "task": d.get("task") if d.get("task") in TASKS else "other",
                               "output": d.get("output") if d.get("output") in OUTPUTS else "text",
                               "fixed": str(d.get("fixed") or "")[:200], "per_call": str(d.get("per_call") or "")[:200],
                               "model": model(), "tokens": u["input"] + u["output"]})
