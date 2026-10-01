"""Alembic is invoked by database.py with a reviewed, already-open connection."""

from alembic import context

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use database.py with a private database configuration")
context.configure(
    connection=connection, render_as_batch=connection.dialect.name == "sqlite"
)
with context.begin_transaction():
    context.run_migrations()
