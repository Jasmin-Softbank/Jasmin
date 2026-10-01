"""Native Alembic/SQLAlchemy storage boundary checks; no fake database adapter."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from database import engine_for, upgrade, require_head
from state import ControlState, Principal
from observability import OperationError
from sqlalchemy import inspect, text


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "private/control.sqlite3"
        self.engine = engine_for(self.path)
        self.addCleanup(self.engine.dispose)

    def test_runtime_refuses_missing_revision_without_creating_schema(self):
        with self.assertRaises(OperationError):
            ControlState(self.path, Path(__file__).parents[1] / "runner/accounts.yaml")
        self.assertEqual(inspect(self.engine).get_table_names(), [])

    def test_upgrade_is_repeatable_preserves_data_and_checks_unknown_revision(self):
        upgrade(self.engine)
        store = ControlState(
            self.path, Path(__file__).parents[1] / "runner/accounts.yaml"
        )
        self.addCleanup(store.engine.dispose)
        principal = Principal("operator", "operator", True)
        before = store.create_workspace(principal, "one")
        upgrade(self.engine)
        self.assertEqual(before, store.get_workspace(principal, "one"))
        with self.engine.begin() as db:
            db.execute(
                text("UPDATE alembic_version SET version_num='unknown_revision'")
            )
        with self.assertRaises(OperationError):
            require_head(self.engine)
        with self.assertRaises(Exception):
            upgrade(self.engine)
        self.assertEqual(before, store.get_workspace(principal, "one"))

    def test_failed_migration_rolls_back_schema_and_has_no_revision_claim(self):
        def fail(config, _):
            config.attributes["connection"].execute(
                text("CREATE TABLE interrupted(id INTEGER)")
            )
            raise RuntimeError("injected failure")

        with patch("database.command.upgrade", side_effect=fail), self.assertRaises(
            RuntimeError
        ):
            upgrade(self.engine)
        self.assertNotIn("interrupted", inspect(self.engine).get_table_names())
        with self.assertRaises(OperationError):
            require_head(self.engine)

    def test_unversioned_existing_data_is_not_stamped_or_destroyed(self):
        with self.engine.begin() as db:
            db.execute(text("CREATE TABLE accounts(id TEXT PRIMARY KEY)"))
            db.execute(text("INSERT INTO accounts VALUES ('retained')"))
        with self.assertRaises(Exception):
            upgrade(self.engine)
        with self.engine.connect() as db:
            self.assertEqual(
                db.execute(text("SELECT id FROM accounts")).scalar_one(), "retained"
            )
        with self.assertRaises(OperationError):
            require_head(self.engine)

    def test_symlink_and_unregistered_dialect_are_rejected(self):
        symlink = self.path.with_name("link.sqlite3")
        symlink.symlink_to(self.path)
        with self.assertRaises(OSError):
            engine_for(symlink)
        with self.assertRaises(ValueError):
            engine_for("mysql://localhost/control")


if __name__ == "__main__":
    unittest.main()
