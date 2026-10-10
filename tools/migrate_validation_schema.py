"""Upgrade a Postgres validation database from bfa695b to the current schema, before new code takes traffic. Idempotent.

    DATABASE_URL=postgresql+psycopg://... .venv/bin/python tools/migrate_validation_schema.py

Makes max_budget_usd nullable (else jobs without a budget fail), judge_model TEXT, creates missing tables and backfills
validation_cells for finished requests. A new database needs none of this; for SQLite, delete .data/audit_store.db."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import exists, insert, select, text  # noqa: E402

from app.services import db  # noqa: E402
from validation import jobs  # noqa: E402

BATCH = 200                 # finished requests read per round: each result holds every run's output


def main():
    eng = db.ready()
    if eng.dialect.name != "postgresql":
        sys.exit("Postgres only (this is %s); see the docstring for SQLite." % eng.dialect.name)
    with eng.begin() as c:
        c.execute(text("ALTER TABLE validation_jobs ALTER COLUMN max_budget_usd DROP NOT NULL"))
        c.execute(text("ALTER TABLE validation_jobs ALTER COLUMN judge_model TYPE TEXT"))
    print("validation_jobs: max_budget_usd optional, judge_model TEXT")

    it, ce, done = jobs._items.c, jobs._cells.c, 0
    while True:
        with eng.begin() as c:
            rows = c.execute(select(it.job_id, it.seq, it.result).where(
                it.result.is_not(None), ~exists().where(ce.job_id == it.job_id, ce.seq == it.seq)).limit(BATCH)).all()
            if not rows:
                break
            c.execute(insert(jobs._cells), [cell for job_id, seq, r in rows
                                            for cell in jobs._cell_rows(job_id, seq, json.loads(r))])
        done += len(rows)
    print("validation_cells: %d finished requests backfilled" % done)


if __name__ == "__main__":
    main()
