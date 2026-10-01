"""Control persistence: SQLAlchemy connection policy and explicit Alembic upgrades.

Runtime checks the revision and never performs DDL. URLs are private configuration,
not API input. SQLite is a local embedded store; PostgreSQL is the server backend.
"""

import argparse
import os
from pathlib import Path
import sys

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url, URL

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from observability import OperationError
from runner.runtime_boundary import private_directory

HERE = Path(__file__).resolve().parent


def configuration():
    return Config(str(HERE / "alembic.ini"))


def engine_for(database):
    value = str(database)
    url = (
        make_url(value)
        if "://" in value
        else URL.create("sqlite", database=str(Path(value).absolute()))
    )
    if url.get_backend_name() not in ("sqlite", "postgresql"):
        raise ValueError("unsupported control database backend")
    if (
        url.get_backend_name() == "postgresql"
        and url.drivername != "postgresql+psycopg"
    ):
        raise ValueError("use the pinned postgresql+psycopg driver")
    options = {"pool_pre_ping": True, "hide_parameters": True}
    if url.get_backend_name() == "sqlite":
        if not url.database or url.database == ":memory:":
            raise ValueError("durable SQLite file required")
        path = Path(url.database).absolute()
        private_directory(path.parent)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_uid != os.getuid():
                raise ValueError("database owner mismatch")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        url = url.set(database=str(path))
        options["connect_args"] = {"timeout": 5}
    else:
        options["connect_args"] = {"connect_timeout": 5}
    engine = create_engine(url, **options)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def sqlite_settings(connection, _):
            connection.isolation_level = None
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")

    return engine


def begin_write(connection):
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        connection.begin()
        # ponytail: short control transactions share one row lock; use per-account
        # locks only when measured queue contention warrants the added lock ordering.
        connection.execute(
            text("SELECT id FROM control_lock WHERE id=1 FOR UPDATE")
        ).scalar_one()


def require_head(engine):
    with engine.connect() as connection:
        actual = set(MigrationContext.configure(connection).get_current_heads())
    if actual != set(ScriptDirectory.from_config(configuration()).get_heads()):
        raise OperationError(
            "CONTROL_CONFIG_INVALID",
            component="control",
            phase="database.revision",
            retry_policy="after_configuration",
        )


def upgrade(engine):
    config = configuration()
    with engine.connect() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.begin()
            # Serializes schema upgrades, independent of runtime account locks.
            connection.execute(text("SELECT pg_advisory_xact_lock(1380010316)"))
        try:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    require_head(engine)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["upgrade", "check"])
    parser.add_argument(
        "--database-file", type=Path, help="SQLite file; use --url-file for PostgreSQL"
    )
    parser.add_argument(
        "--url-file", type=Path, help="private file containing a SQLAlchemy URL"
    )
    args = parser.parse_args()
    if bool(args.database_file) == bool(args.url_file):
        parser.error("select one database input")
    try:
        value = args.database_file
        if args.url_file:
            if args.url_file.is_symlink() or args.url_file.stat().st_mode & 0o077:
                raise ValueError("URL file must be private")
            value = args.url_file.read_text().strip()
        engine = engine_for(value)
        try:
            upgrade(engine) if args.action == "upgrade" else require_head(engine)
        finally:
            engine.dispose()
        print("control database revision verified")
    except Exception as exc:
        from observability import event_record

        error = (
            exc
            if isinstance(exc, OperationError)
            else OperationError(
                "STATE_STORAGE_FAILED",
                component="control",
                phase="database.migrate",
                retry_policy="after_reconcile",
                cause=exc,
            )
        )
        import json

        print(
            json.dumps(
                event_record(
                    "control.database.failed",
                    component="control",
                    phase="database.migrate",
                    outcome=error.outcome,
                    error=error,
                )
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
