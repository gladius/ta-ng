"""Runs the migrations: on the app's own connection (db.migrate), or on db.engine() from the alembic command line."""
from alembic import context

from app.services import db

meta = db.load_tables()


def run(c):
    context.configure(connection=c, target_metadata=meta, render_as_batch=c.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()


if context.config.attributes.get("connection") is not None:
    run(context.config.attributes["connection"])
else:
    with db.engine().begin() as c:
        run(c)
