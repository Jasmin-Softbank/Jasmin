"""SQLite control integration; synthetic identities/observations, no external providers."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
import uuid
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from state import ControlState, Principal
from database import engine_for, upgrade
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from observability import OperationError


CONFIG = """version: 1
runtime: codex-sdk
accounts:
  primary:
    credential_ref: synthetic-do-not-export
    max_concurrent_runs: 1
  secondary:
    credential_ref: another-synthetic-private-ref
    max_concurrent_runs: 1
allocation:
  strategy: explicit-sticky
  default_account: primary
  on_busy: queue
  copy_credentials_to_workspace: false
bindings:
  legacy-admin:
    account: secondary
"""


class ControlStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = self.root / "accounts.yaml"
        self.config.write_text(CONFIG)
        self.now = 1000.0
        self.path = self.root / "private/state.sqlite3"
        self.database = self.path
        if getattr(self, "postgres", False):
            admin = engine_for(os.environ["RAILSHOT_TEST_POSTGRES_URL"])
            schema = "test_" + uuid.uuid4().hex
            with admin.begin() as db:
                db.execute(text("CREATE SCHEMA " + schema))
            def drop_schema():
                with admin.begin() as db:
                    db.execute(text("DROP SCHEMA " + schema + " CASCADE"))
                admin.dispose()
            self.addCleanup(drop_schema)
            self.database = (make_url(os.environ["RAILSHOT_TEST_POSTGRES_URL"])
                .update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False))
        engine = engine_for(self.database)
        upgrade(engine)
        engine.dispose()
        self.store = ControlState(self.database, self.config, clock=lambda: self.now)
        self.addCleanup(self.store.engine.dispose)
        self.alice = Principal("tenant-a", "alice")
        self.bob = Principal("tenant-b", "bob")
        self.admin = Principal("tenant-a", "operator", True)
        self.store.create_workspace(self.alice, "workspace")

    def ready(self, ctx=None, name="workspace", resource="vm-a"):
        ctx = ctx or self.alice
        admin = Principal(ctx.tenant_id, "observer", True)
        self.store.register_resource(admin, name, provider="aws", resource_id=resource)
        return self.store.record_readiness(
            admin,
            name,
            resource_id=resource,
            ready=True,
            observed_at=self.now,
            evidence_ref="synthetic-observation",
        )

    def submit(self, key="request", ctx=None, **fields):
        return self.store.submit(
            ctx or self.alice,
            "workspace",
            kind="ci",
            request_hash="a" * 64,
            idempotency_key=key,
            **fields,
        )

    def assert_code(self, code, function):
        with self.assertRaises(OperationError) as caught:
            function()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_real_accounts_configuration_is_read_but_credentials_never_persist(self):
        actual = Path(__file__).resolve().parents[1] / "runner/accounts.yaml"
        other_database = self.database if getattr(self, "postgres", False) else self.root / "actual/state.sqlite3"
        engine = engine_for(other_database); upgrade(engine); engine.dispose()
        store = ControlState(other_database, actual, clock=lambda: self.now)
        self.addCleanup(store.engine.dispose)
        record = store.create_workspace(self.admin, "operator-default")
        self.assertEqual(record["account_id"], "operator-primary")
        self.assertIsNone(record["observed_at"])
        self.assertFalse(record["ready"])
        self.assertNotIn("credential", json.dumps(record))
        with self.store.engine.connect() as db:
            persisted = json.dumps([dict(row) for row in db.execute(text("SELECT * FROM accounts")).mappings()])
        self.assertNotIn("synthetic-do-not-export", persisted)
        self.assertNotIn("credential", persisted)
        if not getattr(self, "postgres", False):
            self.assertNotIn("synthetic-do-not-export", self.path.read_bytes().decode("latin1"))
            self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_two_principals_are_isolated_in_workspace_job_and_events(self):
        self.store.create_workspace(self.bob, "workspace")
        self.assert_code(
            "CONTROL_ACCESS_DENIED",
            lambda: self.store.get_workspace(
                Principal("tenant-a", "stranger"), "workspace"
            ),
        )
        job = self.submit()
        self.assert_code(
            "CONTROL_ACCESS_DENIED", lambda: self.store.get_job(self.bob, job["id"])
        )
        self.assert_code(
            "CONTROL_ACCESS_DENIED", lambda: self.store.cancel(self.bob, job["id"])
        )
        serialized = json.dumps(self.store.events(self.bob))
        self.assertNotIn(job["id"], serialized)
        self.assertNotIn("synthetic-do-not-export", serialized)

    def test_concurrent_idempotent_submit_and_conflicting_hash(self):
        barrier = threading.Barrier(2)

        def submit():
            barrier.wait()
            return self.submit()

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assert_code(
            "CONTROL_CONFLICT",
            lambda: self.store.submit(
                self.alice,
                "workspace",
                kind="ci",
                request_hash="b" * 64,
                idempotency_key="request",
            ),
        )
        events = self.store.events(self.alice)
        self.assertEqual(
            sum(e["event_name"] == "control.job.queued" for e in events), 1
        )

    def test_two_workers_share_one_global_account_slot_across_tenants(self):
        self.store.create_workspace(self.bob, "workspace")
        self.ready()
        self.ready(self.bob, resource="vm-b")
        jobs = [self.submit(), self.submit(ctx=self.bob)]
        barrier = threading.Barrier(2)

        def claim(worker):
            barrier.wait()
            return self.store.claim(worker)

        with ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(claim, ["worker-a", "worker-b"]))
        live = [job for job in claims if job]
        self.assertEqual(len(live), 1)
        first = live[0]
        self.store.complete(
            first["id"], first["worker_id"], first["generation"], outcome="PASS"
        )
        second = self.store.claim("worker-c")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual({j["id"] for j in jobs}, {first["id"], second["id"]})

    def test_restart_preserves_sticky_binding_and_pending_job(self):
        self.ready()
        job = self.submit()
        self.config.write_text(
            CONFIG.replace("default_account: primary", "default_account: secondary")
        )
        restarted = ControlState(self.database, self.config, clock=lambda: self.now)
        self.addCleanup(restarted.engine.dispose)
        self.assertEqual(
            restarted.get_workspace(self.alice, "workspace")["account_id"], "primary"
        )
        self.assertEqual(restarted.claim("worker")["id"], job["id"])
        self.assertEqual(
            restarted.create_workspace(self.alice, "new")["account_id"], "secondary"
        )

    def test_expired_dispatch_is_unknown_holds_capacity_and_rejects_late_completion(
        self,
    ):
        self.ready()
        first = self.submit("one")
        second = self.submit("two")
        claim = self.store.claim("worker", lease_seconds=10)
        self.now += 11
        self.assert_code(
            "CONTROL_LEASE_EXPIRED",
            lambda: self.store.complete(
                claim["id"], "worker", claim["generation"], outcome="PASS"
            ),
        )
        self.assertEqual(
            self.store.get_job(self.alice, claim["id"])["status"], "UNKNOWN"
        )
        self.assertIsNone(self.store.claim("replacement"))
        self.assertEqual(
            self.store.get_job(self.alice, second["id"])["status"], "QUEUED"
        )
        events = self.store.events(self.alice)
        unknown = [e for e in events if e["event_name"] == "control.job.unknown"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["error"]["side_effect"], "unknown")
        late = [
            e
            for e in events
            if e["error"] and e["error"]["code"] == "CONTROL_LEASE_EXPIRED"
        ]
        self.assertEqual(late[-1]["attributes"]["job_id"], first["id"])

    def test_wrong_worker_or_generation_cannot_heartbeat_or_complete(self):
        self.ready()
        self.submit()
        job = self.store.claim("worker")
        self.assert_code(
            "CONTROL_LEASE_EXPIRED",
            lambda: self.store.heartbeat(job["id"], "other", job["generation"]),
        )
        self.assert_code(
            "CONTROL_LEASE_EXPIRED",
            lambda: self.store.complete(
                job["id"], "worker", job["generation"] + 1, outcome="PASS"
            ),
        )
        self.assertEqual(
            self.store.get_job(self.alice, job["id"])["status"], "DISPATCHED"
        )
        self.store.heartbeat(job["id"], "worker", job["generation"])

    def test_registration_never_means_ready_and_observations_bind_resource(self):
        self.submit()
        self.assertIsNone(self.store.claim("worker"))
        registered = self.store.register_resource(
            self.admin, "workspace", provider="aws", resource_id="vm-a"
        )
        self.assertFalse(registered["ready"])
        self.assertIsNone(registered["observed_at"])
        self.assertIsNone(self.store.claim("worker"))
        self.assert_code(
            "CONTROL_ACCESS_DENIED",
            lambda: self.store.record_readiness(
                self.alice,
                "workspace",
                resource_id="vm-a",
                ready=True,
                observed_at=self.now,
                evidence_ref="fake",
            ),
        )
        self.assert_code(
            "CONTROL_NOT_READY",
            lambda: self.store.record_readiness(
                self.admin,
                "workspace",
                resource_id="other",
                ready=True,
                observed_at=self.now,
                evidence_ref="fake",
            ),
        )
        self.ready()
        self.now += 301
        self.assertIsNone(
            self.store.claim("worker"), "stale readiness must not authorize dispatch"
        )
        self.assert_code(
            "CONTROL_ACCESS_DENIED",
            lambda: self.submit("forced", ctx=self.admin, requires_ready=False),
        )

    def test_queued_cancel_is_final_but_running_cancel_does_not_free_slot(self):
        queued = self.submit()
        self.assertEqual(
            self.store.cancel(self.alice, queued["id"])["status"], "CANCELLED"
        )
        self.ready()
        self.submit("second")
        job = self.store.claim("worker")
        pending = self.store.cancel(self.alice, job["id"])
        self.assertEqual(pending["status"], "DISPATCHED")
        self.assertEqual(pending["cancel_requested"], 1)
        self.submit("third")
        self.assertIsNone(self.store.claim("other"))

    def allow(self, operation="deploy", **fields):
        record = self.store.create_allow(
            self.alice, "workspace", operation=operation, plan_hash="a" * 64, **fields
        )
        self.store.decide_allow(self.alice, record["id"], approve=True)
        return record

    def test_allow_expiry_generation_and_plan_binding(self):
        expired = self.allow(ttl_seconds=5)
        self.now += 6
        self.assert_code(
            "CONTROL_ALLOW_INVALID",
            lambda: self.store.consume_allow(
                self.alice,
                expired["id"],
                operation="deploy",
                plan_hash="a" * 64,
                generation=expired["generation"],
            ),
        )
        current = self.allow()
        self.assert_code(
            "CONTROL_ALLOW_INVALID",
            lambda: self.store.consume_allow(
                self.alice,
                current["id"],
                operation="deploy",
                plan_hash="b" * 64,
                generation=current["generation"],
            ),
        )
        self.store.register_resource(
            self.admin, "workspace", provider="aws", resource_id="vm-new"
        )
        self.assert_code(
            "CONTROL_ALLOW_INVALID",
            lambda: self.store.consume_allow(
                self.alice,
                current["id"],
                operation="deploy",
                plan_hash="a" * 64,
                generation=current["generation"],
            ),
        )

    def test_allow_concurrent_consume_creates_one_durable_operation(self):
        allow = self.allow()
        barrier = threading.Barrier(2)

        def consume():
            barrier.wait()
            try:
                return self.store.consume_allow(
                    self.alice,
                    allow["id"],
                    operation="deploy",
                    plan_hash="a" * 64,
                    generation=allow["generation"],
                )
            except OperationError as exc:
                return exc.code

        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(lambda _: consume(), range(2)))
        operations = [x for x in outcomes if isinstance(x, dict)]
        self.assertEqual(len(operations), 1)
        self.assertIn("CONTROL_ALLOW_INVALID", outcomes)
        restarted = ControlState(self.database, self.config, clock=lambda: self.now)
        self.addCleanup(restarted.engine.dispose)
        self.assertEqual(
            restarted.get_operation(self.alice, operations[0]["id"]), operations[0]
        )
        self.assert_code(
            "CONTROL_ACCESS_DENIED",
            lambda: restarted.get_operation(self.bob, operations[0]["id"]),
        )

    def test_approved_operation_can_be_bound_to_only_one_exact_request_job(self):
        allow = self.allow()
        operation = self.store.consume_allow(
            self.alice,
            allow["id"],
            operation="deploy",
            plan_hash="a" * 64,
            generation=allow["generation"],
        )
        self.assert_code(
            "CONTROL_ALLOW_INVALID",
            lambda: self.store.submit(
                self.alice,
                "workspace",
                kind="deploy",
                request_hash="a" * 64,
                idempotency_key="no-allow",
            ),
        )
        self.assert_code(
            "CONTROL_ALLOW_INVALID",
            lambda: self.store.submit(
                self.alice,
                "workspace",
                kind="deploy",
                request_hash="b" * 64,
                idempotency_key="changed",
                operation_id=operation["id"],
            ),
        )
        job = self.store.submit(
            self.alice,
            "workspace",
            kind="deploy",
            request_hash="a" * 64,
            idempotency_key="exact",
            operation_id=operation["id"],
        )
        self.assertEqual(job["operation_id"], operation["id"])
        self.assert_code(
            "CONTROL_CONFLICT",
            lambda: self.store.submit(
                self.alice,
                "workspace",
                kind="deploy",
                request_hash="a" * 64,
                idempotency_key="duplicate",
                operation_id=operation["id"],
            ),
        )

    def test_changed_target_generation_blocks_already_queued_authorized_operation(self):
        self.ready()
        allow = self.allow()
        operation = self.store.consume_allow(
            self.alice,
            allow["id"],
            operation="deploy",
            plan_hash="a" * 64,
            generation=allow["generation"],
        )
        job = self.store.submit(
            self.alice,
            "workspace",
            kind="deploy",
            request_hash="a" * 64,
            idempotency_key="release",
            operation_id=operation["id"],
        )
        self.store.register_resource(
            self.admin, "workspace", provider="aws", resource_id="replacement"
        )
        self.assertIsNone(self.store.claim("worker"))
        actual = self.store.get_job(self.alice, job["id"])
        self.assertEqual(actual["status"], "BLOCKED")
        self.assertEqual(actual["error"]["code"], "CONTROL_ALLOW_INVALID")

    def test_malformed_completion_is_typed_and_does_not_change_job(self):
        self.ready()
        self.submit()
        job = self.store.claim("worker")
        self.assert_code(
            "CONTROL_CONFIG_INVALID",
            lambda: self.store.complete(
                job["id"],
                "worker",
                job["generation"],
                outcome="FAIL",
                error={"code": "bad"},
            ),
        )
        self.assertEqual(
            self.store.get_job(self.alice, job["id"])["status"], "DISPATCHED"
        )

    def test_principal_event_pagination_filters_before_limit_and_safe_list_views(self):
        other = Principal("tenant-a", "other")
        self.store.create_workspace(other, "other-workspace")
        for number in range(205):
            self.store.acquire_lease(other, "other-workspace")
        own = self.submit("visible-after-other-events")
        events = self.store.events(self.alice)
        self.assertTrue(any(e["attributes"].get("job_id") == own["id"] for e in events))
        self.assertEqual(
            [w["id"] for w in self.store.list_workspaces(self.alice)], ["workspace"]
        )
        self.assertEqual(
            [j["id"] for j in self.store.jobs(self.alice, "workspace")], [own["id"]]
        )

    def test_drain_blocks_stop_until_actual_jobs_and_input_leases_are_clear(self):
        self.ready()
        self.submit()
        job = self.store.claim("worker")
        self.assert_code("CONTROL_CONFLICT", lambda: self.store.acquire_lease(self.alice, "workspace"))
        self.store.begin_drain(self.alice, "workspace")
        self.assert_code(
            "CONTROL_CONFLICT",
            lambda: self.store.assert_drained(self.alice, "workspace"),
        )
        self.assert_code(
            "CONTROL_CONFLICT",
            lambda: self.store.acquire_lease(self.alice, "workspace"),
        )
        allow = self.allow(operation="vm.stop")
        consume = lambda: self.store.consume_allow(
            self.alice,
            allow["id"],
            operation="vm.stop",
            plan_hash="a" * 64,
            generation=allow["generation"],
        )
        self.assert_code("CONTROL_CONFLICT", consume)
        self.store.complete(job["id"], "worker", job["generation"], outcome="PASS")
        drained = self.store.assert_drained(self.alice, "workspace")
        self.assertEqual((drained["active_jobs"], drained["active_leases"]), (0, 0))
        operation = consume()
        self.assertEqual(operation["operation"], "vm.stop")

    def test_event_write_failure_rolls_back_job_submit_and_is_typed(self):
        with patch.object(
            self.store,
            "_event",
            side_effect=SQLAlchemyError(
                "private synthetic storage diagnostic"
            ),
        ):
            error = self.assert_code("STATE_STORAGE_FAILED", lambda: self.submit())
        self.assertEqual(error.outcome, "UNKNOWN")
        self.assertNotIn("private synthetic", json.dumps(error.as_dict()))
        with self.store.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM jobs")).scalar_one(), 0)
        with self.assertRaises(SQLAlchemyError):
            with self.store.engine.begin() as db:
                db.execute(text("DELETE FROM events"))


    def test_input_lease_excludes_job_claim_and_blocks_drain_until_released(self):
        self.ready()
        lease = self.store.acquire_lease(self.alice, "workspace")
        queued = self.submit()
        self.assertIsNone(self.store.claim("worker"))
        self.store.begin_drain(self.alice, "workspace")
        self.assertEqual(self.store.get_job(self.alice, queued["id"])["status"], "CANCELLED")
        self.assert_code("CONTROL_CONFLICT", lambda: self.store.assert_drained(self.alice, "workspace"))
        self.store.release_lease(self.alice, lease["id"])
        self.assertEqual(self.store.assert_drained(self.alice, "workspace")["active_leases"], 0)

    def test_diagnostic_text_and_worker_phase_are_stored_without_argument_shadowing(self):
        seq = self.store.append_observation(self.admin, "workspace", event="control.worker.log",
            text="synthetic lint diagnostic\n", attributes={"phase": "lint", "stream": "stdout"})
        event = self.store.events(self.admin, workspace_id="workspace")[-1]
        self.assertEqual(event["phase"], "lint")
        self.assertNotIn("synthetic lint", json.dumps(event))
        self.assertEqual(self.store.diagnostics(self.admin, "workspace"),
            [{"sequence": seq, "text": "synthetic lint diagnostic\n"}])
        self.assertEqual(self.store.snapshot_events(self.admin, "workspace")["cursor"], seq)

    def test_scoped_admin_events_ignore_over_200_other_workspace_events(self):
        self.store.create_workspace(self.admin, "other-workspace")
        for number in range(205):
            self.store.append_observation(self.admin, "other-workspace", event="control.worker.log", text=f"other {number}\n")
        own = self.submit("visible-after-other-events")
        events = self.store.events(self.admin, workspace_id="workspace")
        self.assertTrue(any(e["attributes"].get("job_id") == own["id"] for e in events))
        self.assertTrue(all(e["attributes"].get("workspace_id") == "workspace" for e in events))
        snapshot = self.store.snapshot_events(self.admin, "workspace")
        self.assertEqual(snapshot["logs"], [])
        self.assertEqual(snapshot["cursor"], events[-1]["sequence"])


    def test_job_observations_are_scoped_and_separate_stages_from_logs(self):
        self.ready()
        queued=self.submit("observed-ci")
        job=self.store.claim("worker")
        common={"job_id":job["id"],"worker_id":"worker","generation":job["generation"]}
        self.store.append_observation(self.admin,"workspace",event="control.worker.stage",outcome="RUNNING",attributes={"phase":"Q"},**common)
        self.store.append_observation(self.admin,"workspace",event="control.worker.log",text="test log\n",attributes={"phase":"Q"},**common)
        stages=self.store.job_observations(self.admin,queued["id"])
        logs=self.store.job_observations(self.admin,queued["id"],logs=True)
        self.assertEqual([r["phase"] for r in stages["records"]],["Q"])
        self.assertEqual([r["text"] for r in logs["records"]],["test log\n"])
        self.assertFalse(stages["truncated"])
        self.assert_code("CONTROL_ACCESS_DENIED",lambda:self.store.job_observations(self.bob,queued["id"]))


@unittest.skipUnless(os.environ.get("RAILSHOT_TEST_POSTGRES_URL"), "explicit native PostgreSQL URL required")
class PostgresControlStateTest(ControlStateTest):
    postgres = True


if __name__ == "__main__":
    unittest.main()
