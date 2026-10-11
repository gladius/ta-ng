"""The ledger's time series in the app's database, filled by each refresh: usecase x site x model x day. Everything
else about a call-site lives on its record (core.identity.store)."""
import datetime

from sqlalchemy import Column, Date, Float, Integer, String, Table, Text, delete, select

from app.services import db

ledger_daily = Table(
    "ledger_daily", db.metadata,
    Column("day", Date, primary_key=True),
    Column("usecase", Text, primary_key=True, default=""),
    Column("site", String(32), primary_key=True),
    Column("model", Text, primary_key=True, default=""),
    Column("calls", Integer, nullable=False),
    Column("prompt_tokens", Integer), Column("cached_tokens", Integer), Column("written_tokens", Integer),
    Column("completion_tokens", Integer), Column("calls_cached", Integer),
    Column("cost", Float), Column("cost_nocache", Float),
    Column("avg_ms", Float), Column("avg_messages", Float), Column("calls_with_tools", Integer),
)
NUMS = ("calls", "prompt_tokens", "cached_tokens", "written_tokens", "completion_tokens", "calls_cached", "cost",
        "cost_nocache", "avg_ms", "avg_messages", "calls_with_tools")


def store_ledger(rows):
    """Replace the window's rows."""
    eng = db.ready()
    out = [{"day": r["day"], "usecase": r["usecase"] or "", "site": str(r["site"]), "model": r["model"] or "",
            **{k: r.get(k) for k in NUMS}} for r in rows]
    with eng.begin() as c:
        c.execute(delete(ledger_daily))
        for i in range(0, len(out), 500):
            c.execute(ledger_daily.insert(), out[i:i + 500])
    return len(out)


def ledger(where=None):
    eng = db.ready()
    stmt = select(*ledger_daily.columns)
    if where is not None:
        stmt = stmt.where(where)
    with eng.connect() as c:
        rows = [dict(zip([col.name for col in ledger_daily.columns], r)) for r in c.execute(stmt).all()]
    for r in rows:
        if isinstance(r["day"], (datetime.date, datetime.datetime)):
            r["day"] = str(r["day"])[:10]
    return rows
