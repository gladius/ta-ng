"""Upgrade an existing validation database to the current schema. Idempotent: run it as often as you like.

    DATABASE_URL=postgresql+psycopg://... .venv/bin/python tools/migrate_validation_schema.py

Needed once on a Postgres database that ran an earlier version (bfa695b), before the new code takes traffic:
  - validation_jobs.max_budget_usd may be NULL (a job without a budget runs to the end): without this, every job
    submitted without max_budget_usd fails
  - validation_jobs.judge_model becomes TEXT (it was VARCHAR(120))
  - validation_cells / validation_callbacks are created if missing (the app also creates missing tables itself)
  - requests finished before validation_cells existed get their grid rows, so old jobs' pages, filters and
    summaries show them
A new database needs none of this: the app creates every table on first use. Postgres only; a dev SQLite database
is simplest to delete (.data/audit_store.db) and let the app recreate."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import exists, insert, select, text  # noqa: E402

from app.services import db  # noqa: E402
from validation import jobs  # noqa: E402

BATCH = 200                 # finished requests read per round: each result holds every run's output


def main():
    eng = db.ready()                                   # creates the tables that are missing
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
