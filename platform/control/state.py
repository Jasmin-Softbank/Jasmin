"""Authoritative control state using a versioned SQLite/PostgreSQL schema; authenticated API/worker adapters call this kernel.

No cloud creation, credential resolution, provider dispatch or billing calls occur here.
Principal comes only from server authentication. Worker and observer methods must never
be exposed as user-controlled HTTP mutations. UNKNOWN jobs reserve their account slot
until explicit reconciliation; a lease expiry is not proof the external work stopped.
"""

from contextlib import closing, contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
from sqlalchemy import text as sql_text
from sqlalchemy.exc import SQLAlchemyError
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from observability import OperationError, event_record
from control.database import engine_for, begin_write, require_head


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    principal_id: str
    is_admin: bool = False


def fail(code, phase, **fields):
    return OperationError(code, component="control", phase=phase, **fields)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}", value
    ):
        raise fail(
            "CONTROL_CONFIG_INVALID", "validate", retry_policy="after_configuration"
        )
    return value


def sha256(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise fail(
            "CONTROL_CONFIG_INVALID", "validate", retry_policy="after_configuration"
        )
    return value


def ttl(value, maximum=600):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= maximum
    ):
        raise fail(
            "CONTROL_CONFIG_INVALID", "validate", retry_policy="after_configuration"
        )
    return value


class ControlState:
    def __init__(self, db_path, accounts_path, *, clock=time.time):
        self.clock = clock
        self.engine = None
        try:
            self.engine = engine_for(db_path)
            self.path = (
                Path(self.engine.url.database)
                if self.engine.dialect.name == "sqlite"
                else None
            )
            import yaml

            config = yaml.safe_load(Path(accounts_path).read_text())
            if (
                config["version"] != 1
                or config["runtime"] != "codex-sdk"
                or config["allocation"]["strategy"] != "explicit-sticky"
                or config["allocation"]["on_busy"] != "queue"
                or config["allocation"]["copy_credentials_to_workspace"] is not False
            ):
                raise ValueError("unsupported account policy")
            # Deliberately retain no credential_ref/location/auth material in state or API records.
            self.capacities = {
                identifier(key): value["max_concurrent_runs"]
                for key, value in config["accounts"].items()
            }
            if not self.capacities or any(
                type(value) is not int or not 1 <= value <= 32
                for value in self.capacities.values()
            ):
                raise ValueError("invalid account capacity")
            self.default_account = config["allocation"]["default_account"]
            self.bindings = {
                identifier(key): value["account"]
                for key, value in config.get("bindings", {}).items()
            }
            if any(
                value not in self.capacities
                for value in [self.default_account, *self.bindings.values()]
            ):
                raise ValueError("unknown sticky account")
            require_head(self.engine)
            with closing(self._connect()) as db:
                begin_write(db)
                db.execute(sql_text("UPDATE accounts SET enabled=0"))
                for account, capacity in self.capacities.items():
                    db.execute(
                        sql_text(
                            "INSERT INTO accounts VALUES (:account,:capacity,1) ON CONFLICT(id) DO UPDATE SET capacity=excluded.capacity,enabled=1"
                        ),
                        {"account": account, "capacity": capacity},
                    )
                db.commit()
                # Existing workspace bindings are intentionally not reassigned on config changes.
        except OperationError:
            if self.engine is not None:
                self.engine.dispose()
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if self.engine is not None:
                self.engine.dispose()
            raise fail(
                "CONTROL_CONFIG_INVALID",
                "open",
                retry_policy="after_configuration",
                cause=exc,
            ) from exc
        except SQLAlchemyError as exc:
            if self.engine is not None:
                self.engine.dispose()
            raise fail(
                "STATE_STORAGE_FAILED",
                "open",
                retry_policy="after_reconcile",
                cause=exc,
            ) from exc

    def _connect(self):
        return self.engine.connect()

    def _event(
        self,
        db,
        name,
        *,
        ctx=None,
        tenant_id=None,
        phase="state",
        outcome="RUNNING",
        error=None,
        **attributes,
    ):
        if ctx:
            attributes["principal_id"] = ctx.principal_id
        record = event_record(
            name,
            component="control",
            phase=phase,
            outcome=outcome,
            error=error,
            attributes=attributes,
        )
        return db.execute(
            sql_text(
                "INSERT INTO events(tenant_id,workspace_id,principal_id,body) VALUES (:value,:value_1,:value_2,:value_3) RETURNING seq"
            ),
            {
                "value": ctx.tenant_id if ctx else tenant_id,
                "value_1": attributes.get("workspace_id"),
                "value_2": attributes.get("principal_id"),
                "value_3": json.dumps(record),
            },
        ).scalar_one()

    @contextmanager
    def _tx(self, ctx=None, phase="state", job_id=None):
        db = None
        try:
            if ctx is not None:
                identifier(ctx.tenant_id)
                identifier(ctx.principal_id)
                if type(ctx.is_admin) is not bool:
                    raise fail("CONTROL_ACCESS_DENIED", phase)
            db = self._connect()
            begin_write(db)
            yield db
            db.commit()
        except OperationError as exc:
            if db is not None:
                db.rollback()
                try:
                    begin_write(db)
                    job = (
                        db.execute(
                            sql_text(
                                "SELECT tenant_id,workspace_id FROM jobs WHERE id=:job_id"
                            ),
                            {"job_id": job_id},
                        )
                        .mappings()
                        .fetchone()
                        if job_id
                        else None
                    )
                    self._event(
                        db,
                        "control.rejected",
                        ctx=ctx,
                        tenant_id=job["tenant_id"] if job else None,
                        phase=phase,
                        outcome=exc.outcome,
                        error=exc,
                        **(
                            {"job_id": job_id, "workspace_id": job["workspace_id"]}
                            if job
                            else {}
                        ),
                    )
                    db.commit()
                except SQLAlchemyError as storage:
                    db.rollback()
                    raise fail(
                        "STATE_STORAGE_FAILED",
                        phase,
                        outcome="UNKNOWN",
                        retry_policy="after_reconcile",
                        side_effect="unknown",
                        cause=storage,
                    ) from storage
            raise
        except SQLAlchemyError as exc:
            if db is not None:
                db.rollback()
            raise fail(
                "STATE_STORAGE_FAILED",
                phase,
                outcome="UNKNOWN",
                retry_policy="after_reconcile",
                side_effect="unknown",
                cause=exc,
            ) from exc
        finally:
            if db is not None:
                db.close()

    def _workspace(self, db, ctx, workspace_id):
        row = (
            db.execute(
                sql_text(
                    "SELECT * FROM workspaces WHERE tenant_id=:tenant_id AND id=:workspace_id"
                ),
                {"tenant_id": ctx.tenant_id, "workspace_id": identifier(workspace_id)},
            )
            .mappings()
            .fetchone()
        )
        if row is None or (
            row["principal_id"] != ctx.principal_id and not ctx.is_admin
        ):
            raise fail("CONTROL_ACCESS_DENIED", "workspace")
        return row

    def _job(self, db, job_id):
        row = (
            db.execute(
                sql_text("SELECT * FROM jobs WHERE id=:job_id"),
                {"job_id": identifier(job_id)},
            )
            .mappings()
            .fetchone()
        )
        if row is None:
            raise fail("CONTROL_ACCESS_DENIED", "job")
        return row

    @staticmethod
    def _public_job(row):
        value = dict(row)
        value.pop("idempotency_key", None)
        value["error"] = (
            json.loads(value.pop("error_json")) if value.get("error_json") else None
        )
        value.pop("error_json", None)
        return value

    def create_workspace(self, ctx, workspace_id):
        with self._tx(ctx, "workspace.create") as db:
            identifier(workspace_id)
            existing = (
                db.execute(
                    sql_text(
                        "SELECT * FROM workspaces WHERE tenant_id=:tenant_id AND id=:workspace_id"
                    ),
                    {"tenant_id": ctx.tenant_id, "workspace_id": workspace_id},
                )
                .mappings()
                .fetchone()
            )
            if existing is not None:
                return dict(self._workspace(db, ctx, workspace_id))
            account = self.bindings.get(
                ctx.tenant_id + ":" + workspace_id,
                self.bindings.get(workspace_id, self.default_account)
                if ctx.is_admin
                else self.default_account,
            )
            db.execute(
                sql_text(
                    "INSERT INTO workspaces(tenant_id,id,principal_id,account_id,state) VALUES (:tenant_id,:workspace_id,:principal_id,:account,:value)"
                ),
                {
                    "tenant_id": ctx.tenant_id,
                    "workspace_id": workspace_id,
                    "principal_id": ctx.principal_id,
                    "account": account,
                    "value": "ACTIVE",
                },
            )
            self._event(
                db,
                "control.workspace.created",
                ctx=ctx,
                workspace_id=workspace_id,
                readiness="NOT_OBSERVED",
            )
            return dict(self._workspace(db, ctx, workspace_id))

    def get_workspace(self, ctx, workspace_id):
        with self._tx(ctx, "workspace.read") as db:
            return dict(self._workspace(db, ctx, workspace_id))

    def list_workspaces(self, ctx):
        with self._tx(ctx, "workspace.list") as db:
            return [
                dict(row)
                for row in db.execute(
                    sql_text(
                        "SELECT * FROM workspaces WHERE tenant_id=:tenant_id AND (principal_id=:principal_id OR :is_admin=1) ORDER BY id"
                    ),
                    {
                        "tenant_id": ctx.tenant_id,
                        "principal_id": ctx.principal_id,
                        "is_admin": int(ctx.is_admin),
                    },
                ).mappings()
            ]

    def register_resource(self, ctx, workspace_id, *, provider, resource_id):
        """Trusted administrator registration, not proof of readiness or VM creation."""
        with self._tx(ctx, "workspace.register") as db:
            ws = self._workspace(db, ctx, workspace_id)
            if not ctx.is_admin:
                raise fail("CONTROL_ACCESS_DENIED", "workspace.register")
            if (
                provider not in ("aws", "gcp", "azure")
                or not isinstance(resource_id, str)
                or not re.fullmatch(r"[A-Za-z0-9/._:@+-]{1,2048}", resource_id)
            ):
                raise fail("CONTROL_CONFIG_INVALID", "workspace.register")
            if (
                self._active(db, ws)["active_jobs"]
                or self._active(db, ws)["active_leases"]
            ):
                raise fail(
                    "CONTROL_CONFLICT",
                    "workspace.register",
                    retry_policy="after_reconcile",
                )
            db.execute(
                sql_text(
                    "UPDATE workspaces SET provider=:provider,resource_id=:resource_id,ready=0,observed_at=NULL,evidence_ref=NULL,generation=generation+1 WHERE tenant_id=:tenant_id AND id=:workspace_id"
                ),
                {
                    "provider": provider,
                    "resource_id": resource_id,
                    "tenant_id": ctx.tenant_id,
                    "workspace_id": workspace_id,
                },
            )
            self._event(
                db,
                "control.resource.registered",
                ctx=ctx,
                workspace_id=workspace_id,
                readiness="NOT_OBSERVED",
            )
            return dict(self._workspace(db, ctx, workspace_id))

    def record_readiness(
        self, ctx, workspace_id, *, resource_id, ready, observed_at, evidence_ref
    ):
        """Observer-only adapter method. API must not accept this from a user PATCH.

        The provider adapter is responsible for real resource/runner observations and
        private evidence. This kernel binds the trusted observation to its registration.
        """
        with self._tx(ctx, "workspace.observe") as db:
            ws = self._workspace(db, ctx, workspace_id)
            if not ctx.is_admin:
                raise fail("CONTROL_ACCESS_DENIED", "workspace.observe")
            if (
                not ws["resource_id"]
                or ws["resource_id"] != resource_id
                or type(ready) is not bool
                or not isinstance(observed_at, (int, float))
                or isinstance(observed_at, bool)
                or not math.isfinite(observed_at)
                or not self.clock() - 300 <= observed_at <= self.clock()
                or (ws["observed_at"] is not None and observed_at < ws["observed_at"])
            ):
                raise fail(
                    "CONTROL_NOT_READY",
                    "workspace.observe",
                    retry_policy="after_reconcile",
                )
            identifier(evidence_ref)
            db.execute(
                sql_text(
                    "UPDATE workspaces SET ready=:ready,observed_at=:observed_at,evidence_ref=:evidence_ref WHERE tenant_id=:tenant_id AND id=:workspace_id"
                ),
                {
                    "ready": int(ready),
                    "observed_at": observed_at,
                    "evidence_ref": evidence_ref,
                    "tenant_id": ctx.tenant_id,
                    "workspace_id": workspace_id,
                },
            )
            self._event(
                db,
                "control.resource.observed",
                ctx=ctx,
                workspace_id=workspace_id,
                ready=ready,
                observed_at=observed_at,
                evidence_ref=evidence_ref,
            )
            return dict(self._workspace(db, ctx, workspace_id))

    def submit(
        self,
        ctx,
        workspace_id,
        *,
        kind,
        request_hash,
        idempotency_key,
        requires_ready=True,
        operation_id=None,
        input_lease_id=None,
    ):
        with self._tx(ctx, "job.submit") as db:
            ws = self._workspace(db, ctx, workspace_id)
            identifier(kind)
            identifier(idempotency_key)
            sha256(request_hash)
            if kind not in (
                "ci",
                "agent",
                "prepare",
                "terminal",
                "deploy",
                "vm.stop",
                "vm.delete",
                "vm.restart",
                "vm.create",
                "scale",
            ):
                raise fail("CONTROL_CONFIG_INVALID", "job.submit")
            if (
                kind not in ("ci", "agent", "prepare", "terminal")
                and operation_id is None
            ):
                raise fail("CONTROL_ALLOW_INVALID", "job.submit")
            if type(requires_ready) is not bool or (
                not requires_ready
                and (
                    not ctx.is_admin
                    or kind
                    not in ("prepare", "terminal", "vm.create", "vm.stop", "vm.delete")
                )
            ):
                raise fail("CONTROL_ACCESS_DENIED", "job.submit")
            if kind == "terminal":
                lease = (
                    db.execute(
                        sql_text("SELECT * FROM leases WHERE id=:input_lease_id"),
                        {"input_lease_id": input_lease_id},
                    )
                    .mappings()
                    .fetchone()
                )
                if (
                    not ctx.is_admin
                    or lease is None
                    or (
                        lease["tenant_id"],
                        lease["workspace_id"],
                        lease["principal_id"],
                    )
                    != (ctx.tenant_id, workspace_id, ctx.principal_id)
                    or lease["expires_at"] <= self.clock()
                    or not ws["resource_id"]
                ):
                    raise fail("CONTROL_LEASE_EXPIRED", "job.submit")
            elif input_lease_id is not None:
                raise fail("CONTROL_CONFIG_INVALID", "job.submit")
            old = (
                db.execute(
                    sql_text(
                        "SELECT * FROM jobs WHERE tenant_id=:tenant_id AND workspace_id=:workspace_id AND idempotency_key=:idempotency_key"
                    ),
                    {
                        "tenant_id": ctx.tenant_id,
                        "workspace_id": workspace_id,
                        "idempotency_key": idempotency_key,
                    },
                )
                .mappings()
                .fetchone()
            )
            if old is not None:
                if (
                    old["request_hash"],
                    old["kind"],
                    old["requires_ready"],
                    old["operation_id"],
                    old["input_lease_id"],
                ) != (
                    request_hash,
                    kind,
                    int(requires_ready),
                    operation_id,
                    input_lease_id,
                ):
                    raise fail("CONTROL_CONFLICT", "job.submit")
                return self._public_job(old)
            if ws["state"] != "ACTIVE" and kind not in ("vm.stop", "vm.delete"):
                raise fail("CONTROL_CONFLICT", "job.submit")
            if operation_id is not None:
                operation = (
                    db.execute(
                        sql_text("SELECT * FROM operations WHERE id=:operation_id"),
                        {"operation_id": identifier(operation_id)},
                    )
                    .mappings()
                    .fetchone()
                )
                if (
                    operation is None
                    or (
                        operation["tenant_id"],
                        operation["workspace_id"],
                        operation["generation"],
                    )
                    != (ctx.tenant_id, workspace_id, ws["generation"])
                    or operation["operation"] != kind
                    or operation["plan_hash"] != request_hash
                ):
                    raise fail("CONTROL_ALLOW_INVALID", "job.submit")
                if (
                    db.execute(
                        sql_text("SELECT 1 FROM jobs WHERE operation_id=:operation_id"),
                        {"operation_id": operation_id},
                    )
                    .mappings()
                    .fetchone()
                ):
                    raise fail("CONTROL_CONFLICT", "job.submit")
            job_id = str(uuid.uuid4())
            sequence = self._event(
                db,
                "control.job.queued",
                ctx=ctx,
                workspace_id=workspace_id,
                job_id=job_id,
            )
            db.execute(
                sql_text(
                    "INSERT INTO jobs(id,tenant_id,workspace_id,account_id,kind,request_hash,idempotency_key,operation_id,status,requires_ready,created_at,input_lease_id,queue_seq) VALUES (:job_id,:tenant_id,:workspace_id,:account_id,:kind,:request_hash,:idempotency_key,:operation_id,:value,:requires_ready,:observed_now,:input_lease_id,:sequence)"
                ),
                {
                    "job_id": job_id,
                    "tenant_id": ctx.tenant_id,
                    "workspace_id": workspace_id,
                    "account_id": ws["account_id"],
                    "kind": kind,
                    "request_hash": request_hash,
                    "idempotency_key": idempotency_key,
                    "operation_id": operation_id,
                    "value": "QUEUED",
                    "requires_ready": int(requires_ready),
                    "observed_now": self.clock(),
                    "input_lease_id": input_lease_id,
                    "sequence": sequence,
                },
            )
            return self._public_job(self._job(db, job_id))

    def reconcile_expired(self):
        """Lease loss after claim is uncertain; never put this job back in the queue."""
        with self._tx(phase="job.expire") as db:
            rows = (
                db.execute(
                    sql_text(
                        "SELECT * FROM jobs WHERE status='DISPATCHED' AND lease_until<=:observed_now"
                    ),
                    {"observed_now": self.clock()},
                )
                .mappings()
                .fetchall()
            )
            for job in rows:
                error = fail(
                    "STATE_INFLIGHT_UNCERTAIN",
                    "job.expire",
                    outcome="UNKNOWN",
                    retry_policy="after_reconcile",
                    side_effect="unknown",
                )
                db.execute(
                    sql_text(
                        "UPDATE jobs SET status='UNKNOWN',error_json=:value WHERE id=:id"
                    ),
                    {"value": json.dumps(error.as_dict()), "id": job["id"]},
                )
                self._event(
                    db,
                    "control.job.unknown",
                    tenant_id=job["tenant_id"],
                    phase="job.expire",
                    outcome="UNKNOWN",
                    error=error,
                    job_id=job["id"],
                    workspace_id=job["workspace_id"],
                    generation=job["generation"],
                )
            return len(rows)

    def claim(self, worker_id, *, lease_seconds=60, tenant_id=None, workspace_id=None):
        self.reconcile_expired()
        with self._tx(phase="job.claim") as db:
            identifier(worker_id)
            ttl(lease_seconds)
            stale = (
                db.execute(
                    sql_text(
                        "SELECT j.* FROM jobs j JOIN operations o ON j.operation_id=o.id\n              JOIN workspaces w ON w.tenant_id=j.tenant_id AND w.id=j.workspace_id\n              WHERE j.status='QUEUED' AND o.generation!=w.generation"
                    )
                )
                .mappings()
                .fetchall()
            )
            for job in stale:
                error = fail(
                    "CONTROL_ALLOW_INVALID",
                    "job.claim",
                    retry_policy="after_configuration",
                )
                db.execute(
                    sql_text(
                        "UPDATE jobs SET status='BLOCKED',error_json=:value WHERE id=:id"
                    ),
                    {"value": json.dumps(error.as_dict()), "id": job["id"]},
                )
                self._event(
                    db,
                    "control.job.blocked",
                    tenant_id=job["tenant_id"],
                    phase="job.claim",
                    outcome="BLOCKED",
                    error=error,
                    job_id=job["id"],
                    workspace_id=job["workspace_id"],
                )
            job = (
                db.execute(
                    sql_text(
                        "SELECT j.* FROM jobs j JOIN accounts a ON j.account_id=a.id\n              JOIN workspaces w ON w.tenant_id=j.tenant_id AND w.id=j.workspace_id\n              WHERE j.status='QUEUED' AND a.enabled=1 AND (w.state='ACTIVE' OR j.kind IN ('vm.stop','vm.delete'))\n              AND (CAST(:tenant_id AS TEXT) IS NULL OR j.tenant_id=:tenant_id_1) AND (CAST(:workspace_id AS TEXT) IS NULL OR j.workspace_id=:workspace_id_3)\n              AND (j.requires_ready=0 OR (w.ready=1 AND w.observed_at>=:value))\n              AND ((j.kind='terminal' AND EXISTS (SELECT 1 FROM leases l WHERE l.id=j.input_lease_id AND l.expires_at>:observed_now))\n                OR (j.kind!='terminal' AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.tenant_id=j.tenant_id AND l.workspace_id=j.workspace_id AND l.expires_at>:observed_now_6)))\n              AND (SELECT count(*) FROM jobs busy WHERE busy.account_id=j.account_id AND busy.status IN ('DISPATCHED','UNKNOWN'))<a.capacity\n              ORDER BY j.queue_seq LIMIT 1"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "tenant_id_1": tenant_id,
                        "workspace_id": workspace_id,
                        "workspace_id_3": workspace_id,
                        "value": self.clock() - 300,
                        "observed_now": self.clock(),
                        "observed_now_6": self.clock(),
                    },
                )
                .mappings()
                .fetchone()
            )
            if job is None:
                return None
            db.execute(
                sql_text(
                    "UPDATE jobs SET status='DISPATCHED',generation=generation+1,worker_id=:worker_id,lease_until=:value WHERE id=:id"
                ),
                {
                    "worker_id": worker_id,
                    "value": self.clock() + lease_seconds,
                    "id": job["id"],
                },
            )
            value = self._job(db, job["id"])
            self._event(
                db,
                "control.job.dispatched",
                tenant_id=job["tenant_id"],
                job_id=job["id"],
                workspace_id=job["workspace_id"],
                generation=value["generation"],
            )
            return self._public_job(value)

    def _fence(self, db, job_id, worker_id, generation):
        job = self._job(db, job_id)
        if (
            job["status"] != "DISPATCHED"
            or job["worker_id"] != worker_id
            or type(generation) is not int
            or job["generation"] != generation
            or job["lease_until"] <= self.clock()
        ):
            raise fail(
                "CONTROL_LEASE_EXPIRED", "job.fence", retry_policy="after_reconcile"
            )
        return job

    def heartbeat(self, job_id, worker_id, generation, *, lease_seconds=60):
        self.reconcile_expired()
        with self._tx(phase="job.heartbeat", job_id=job_id) as db:
            job = self._fence(db, job_id, worker_id, generation)
            ttl(lease_seconds)
            db.execute(
                sql_text("UPDATE jobs SET lease_until=:value WHERE id=:job_id"),
                {"value": self.clock() + lease_seconds, "job_id": job_id},
            )
            self._event(
                db,
                "control.job.heartbeat",
                tenant_id=job["tenant_id"],
                job_id=job_id,
                generation=generation,
            )
            return self._public_job(self._job(db, job_id))

    def complete(self, job_id, worker_id, generation, *, outcome, error=None):
        self.reconcile_expired()
        with self._tx(phase="job.complete", job_id=job_id) as db:
            job = self._fence(db, job_id, worker_id, generation)
            try:
                detail = (
                    OperationError.from_dict(
                        error.as_dict() if isinstance(error, OperationError) else error
                    ).as_dict()
                    if error
                    else None
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise fail("CONTROL_CONFIG_INVALID", "job.complete", cause=exc) from exc
            if (
                outcome not in ("PASS", "FAIL", "BLOCKED", "UNKNOWN")
                or (outcome != "PASS" and detail is None)
                or (detail and detail["outcome"] != outcome)
            ):
                raise fail("CONTROL_CONFIG_INVALID", "job.complete")
            db.execute(
                sql_text(
                    "UPDATE jobs SET status=:outcome,error_json=:value WHERE id=:job_id"
                ),
                {
                    "outcome": outcome,
                    "value": json.dumps(detail) if detail else None,
                    "job_id": job_id,
                },
            )
            self._event(
                db,
                "control.job.completed",
                tenant_id=job["tenant_id"],
                phase="job.complete",
                outcome=outcome,
                error=detail,
                job_id=job_id,
                workspace_id=job["workspace_id"],
                generation=generation,
            )
            return self._public_job(self._job(db, job_id))

    def get_job(self, ctx, job_id):
        with self._tx(ctx, "job.read") as db:
            job = self._job(db, job_id)
            self._workspace(db, ctx, job["workspace_id"])
            if job["tenant_id"] != ctx.tenant_id:
                raise fail("CONTROL_ACCESS_DENIED", "job.read")
            return self._public_job(job)

    def job_observations(self, ctx, job_id, *, logs=False):
        """Bounded job-owned history; native JSON expression shares the migration index."""
        with self._tx(ctx, "job.observations") as db:
            job = self._job(db, identifier(job_id))
            self._workspace(db, ctx, job["workspace_id"])
            if job["tenant_id"] != ctx.tenant_id:
                raise fail("CONTROL_ACCESS_DENIED", "job.observations")
            expression = "json_extract(e.body, '$.attributes.job_id')" if db.dialect.name == "sqlite" else "(e.body::jsonb #>> '{attributes,job_id}')"
            query = ("SELECT e.seq,e.body" + (",d.text" if logs else "") + " FROM events e "
                     + ("JOIN diagnostics d ON d.event_seq=e.seq " if logs else "")
                     + "WHERE e.tenant_id=:tenant AND e.workspace_id=:workspace AND " + expression
                     + "=:job AND " + ("1=1" if logs else "e.body LIKE :stage") + " ORDER BY e.seq DESC LIMIT 1001")
            rows = db.execute(sql_text(query), {"tenant":ctx.tenant_id,"workspace":job["workspace_id"],"job":job_id,
                "stage":'%"event_name": "control.worker.stage"%'}).mappings().all()
            result=[]; size=0
            for row in rows[:1000]:
                size += len(row["body"].encode()) + (len(row["text"].encode()) if logs else 0)
                if size > 262144: break
                item={**json.loads(row["body"]),"sequence":row["seq"]}
                if logs: item["text"]=row["text"]
                result.append(item)
            return {"records":list(reversed(result)),"truncated":len(result)<len(rows)}

    def jobs(self, ctx, workspace_id):
        with self._tx(ctx, "job.list") as db:
            self._workspace(db, ctx, workspace_id)
            return [
                self._public_job(row)
                for row in db.execute(
                    sql_text(
                        "SELECT * FROM jobs WHERE tenant_id=:tenant_id AND workspace_id=:workspace_id ORDER BY queue_seq DESC LIMIT 100"
                    ),
                    {"tenant_id": ctx.tenant_id, "workspace_id": workspace_id},
                ).mappings()
            ]

    def cancel(self, ctx, job_id):
        with self._tx(ctx, "job.cancel") as db:
            job = self._job(db, job_id)
            self._workspace(db, ctx, job["workspace_id"])
            if job["tenant_id"] != ctx.tenant_id:
                raise fail("CONTROL_ACCESS_DENIED", "job.cancel")
            if job["status"] not in ("QUEUED", "DISPATCHED", "UNKNOWN"):
                return self._public_job(job)
            status = "CANCELLED" if job["status"] == "QUEUED" else job["status"]
            db.execute(
                sql_text(
                    "UPDATE jobs SET status=:status,cancel_requested=1 WHERE id=:job_id"
                ),
                {"status": status, "job_id": job_id},
            )
            self._event(
                db,
                "control.job.cancel_requested",
                ctx=ctx,
                job_id=job_id,
                status=status,
            )
            return self._public_job(self._job(db, job_id))

    def _active(self, db, ws):
        jobs = db.execute(
            sql_text(
                "SELECT count(*) FROM jobs WHERE tenant_id=:tenant_id AND workspace_id=:id AND status IN ('DISPATCHED','UNKNOWN')"
            ),
            {"tenant_id": ws["tenant_id"], "id": ws["id"]},
        ).scalar_one()
        leases = db.execute(
            sql_text(
                "SELECT count(*) FROM leases WHERE tenant_id=:tenant_id AND workspace_id=:id AND expires_at>:observed_now"
            ),
            {
                "tenant_id": ws["tenant_id"],
                "id": ws["id"],
                "observed_now": self.clock(),
            },
        ).scalar_one()
        return {"active_jobs": jobs, "active_leases": leases}

    def begin_drain(self, ctx, workspace_id):
        with self._tx(ctx, "workspace.drain") as db:
            ws = self._workspace(db, ctx, workspace_id)
            if ws["state"] != "DRAINING":
                db.execute(
                    sql_text(
                        "UPDATE workspaces SET state='DRAINING',generation=generation+1 WHERE tenant_id=:tenant_id AND id=:workspace_id"
                    ),
                    {"tenant_id": ctx.tenant_id, "workspace_id": workspace_id},
                )
                db.execute(
                    sql_text(
                        "UPDATE jobs SET status='CANCELLED',cancel_requested=1 WHERE tenant_id=:tenant_id AND workspace_id=:workspace_id AND status='QUEUED'"
                    ),
                    {"tenant_id": ctx.tenant_id, "workspace_id": workspace_id},
                )
                self._event(
                    db,
                    "control.workspace.draining",
                    ctx=ctx,
                    workspace_id=workspace_id,
                    **self._active(db, ws),
                )
            return dict(self._workspace(db, ctx, workspace_id))

    def _drained(self, db, ws):
        counts = self._active(db, ws)
        if ws["state"] != "DRAINING" or any(counts.values()):
            raise fail(
                "CONTROL_CONFLICT", "workspace.drain", retry_policy="after_reconcile"
            )
        return {
            "workspace_id": ws["id"],
            "generation": ws["generation"],
            "observed_at": self.clock(),
            **counts,
        }

    def assert_drained(self, ctx, workspace_id):
        with self._tx(ctx, "workspace.stop_check") as db:
            return self._drained(db, self._workspace(db, ctx, workspace_id))

    def acquire_lease(self, ctx, workspace_id, *, ttl_seconds=60):
        with self._tx(ctx, "lease.acquire") as db:
            ws = self._workspace(db, ctx, workspace_id)
            ttl(ttl_seconds)
            if ws["state"] != "ACTIVE":
                raise fail("CONTROL_CONFLICT", "lease.acquire")
            if self._active(db, ws)["active_jobs"]:
                raise fail("CONTROL_CONFLICT", "lease.acquire")
            old = (
                db.execute(
                    sql_text(
                        "SELECT * FROM leases WHERE tenant_id=:tenant_id AND workspace_id=:workspace_id AND expires_at>:observed_now"
                    ),
                    {
                        "tenant_id": ctx.tenant_id,
                        "workspace_id": workspace_id,
                        "observed_now": self.clock(),
                    },
                )
                .mappings()
                .fetchone()
            )
            if old is not None:
                if old["principal_id"] != ctx.principal_id:
                    raise fail("CONTROL_CONFLICT", "lease.acquire")
                return {
                    "id": old["id"],
                    "workspace_id": workspace_id,
                    "expires_at": old["expires_at"],
                    "generation": ws["generation"],
                }
            lease_id = str(uuid.uuid4())
            expires = self.clock() + ttl_seconds
            db.execute(
                sql_text(
                    "INSERT INTO leases VALUES (:lease_id,:tenant_id,:workspace_id,:principal_id,:expires)"
                ),
                {
                    "lease_id": lease_id,
                    "tenant_id": ctx.tenant_id,
                    "workspace_id": workspace_id,
                    "principal_id": ctx.principal_id,
                    "expires": expires,
                },
            )
            self._event(
                db,
                "control.lease.acquired",
                ctx=ctx,
                workspace_id=workspace_id,
                lease_id=lease_id,
                expires_at=expires,
            )
            return {
                "id": lease_id,
                "workspace_id": workspace_id,
                "expires_at": expires,
                "generation": ws["generation"],
            }

    def release_lease(self, ctx, lease_id):
        with self._tx(ctx, "lease.release") as db:
            lease = (
                db.execute(
                    sql_text("SELECT * FROM leases WHERE id=:lease_id"),
                    {"lease_id": identifier(lease_id)},
                )
                .mappings()
                .fetchone()
            )
            if (
                lease is None
                or lease["tenant_id"] != ctx.tenant_id
                or lease["principal_id"] != ctx.principal_id
            ):
                raise fail("CONTROL_ACCESS_DENIED", "lease.release")
            db.execute(
                sql_text("DELETE FROM leases WHERE id=:lease_id"),
                {"lease_id": lease_id},
            )
            self._event(
                db,
                "control.lease.released",
                ctx=ctx,
                workspace_id=lease["workspace_id"],
                lease_id=lease_id,
            )

    def create_allow(self, ctx, workspace_id, *, operation, plan_hash, ttl_seconds=300):
        with self._tx(ctx, "allow.create") as db:
            ws = self._workspace(db, ctx, workspace_id)
            identifier(operation)
            sha256(plan_hash)
            ttl(ttl_seconds, 3600)
            allow_id = str(uuid.uuid4())
            expires = self.clock() + ttl_seconds
            db.execute(
                sql_text(
                    "INSERT INTO allows VALUES (:allow_id,:tenant_id,:workspace_id,:operation,:plan_hash,:generation,:expires,:value,NULL)"
                ),
                {
                    "allow_id": allow_id,
                    "tenant_id": ctx.tenant_id,
                    "workspace_id": workspace_id,
                    "operation": operation,
                    "plan_hash": plan_hash,
                    "generation": ws["generation"],
                    "expires": expires,
                    "value": "PENDING",
                },
            )
            self._event(
                db,
                "control.allow.created",
                ctx=ctx,
                workspace_id=workspace_id,
                allow_id=allow_id,
                generation=ws["generation"],
            )
            return dict(
                db.execute(
                    sql_text("SELECT * FROM allows WHERE id=:allow_id"),
                    {"allow_id": allow_id},
                )
                .mappings()
                .fetchone()
            )

    def _allow(self, db, ctx, allow_id):
        row = (
            db.execute(
                sql_text("SELECT * FROM allows WHERE id=:allow_id"),
                {"allow_id": identifier(allow_id)},
            )
            .mappings()
            .fetchone()
        )
        if row is None or row["tenant_id"] != ctx.tenant_id:
            raise fail("CONTROL_ACCESS_DENIED", "allow")
        ws = self._workspace(db, ctx, row["workspace_id"])
        if row["expires_at"] <= self.clock() or row["generation"] != ws["generation"]:
            raise fail(
                "CONTROL_ALLOW_INVALID", "allow", retry_policy="after_configuration"
            )
        return row, ws

    def decide_allow(self, ctx, allow_id, *, approve):
        with self._tx(ctx, "allow.decide") as db:
            row, ws = self._allow(db, ctx, allow_id)
            if type(approve) is not bool or row["state"] != "PENDING":
                raise fail("CONTROL_ALLOW_INVALID", "allow.decide")
            db.execute(
                sql_text(
                    "UPDATE allows SET state=:value,approved_by=:principal_id WHERE id=:allow_id"
                ),
                {
                    "value": "APPROVED" if approve else "DENIED",
                    "principal_id": ctx.principal_id,
                    "allow_id": allow_id,
                },
            )
            self._event(
                db,
                "control.allow.decided",
                ctx=ctx,
                workspace_id=ws["id"],
                allow_id=allow_id,
                approved=approve,
            )
            return dict(
                db.execute(
                    sql_text("SELECT * FROM allows WHERE id=:allow_id"),
                    {"allow_id": allow_id},
                )
                .mappings()
                .fetchone()
            )

    def consume_allow(self, ctx, allow_id, *, operation, plan_hash, generation):
        """One-shot authorization plus durable operation intent in the same transaction.

        The dispatcher must reserve budget using this operation id before any billable
        action. This state kernel never treats an Allow as a billing reservation.
        """
        with self._tx(ctx, "allow.consume") as db:
            row, ws = self._allow(db, ctx, allow_id)
            if (
                row["state"] != "APPROVED"
                or (row["operation"], row["plan_hash"], row["generation"])
                != (operation, plan_hash, generation)
                or type(generation) is not int
            ):
                raise fail("CONTROL_ALLOW_INVALID", "allow.consume")
            if operation in ("vm.stop", "vm.delete"):
                self._drained(db, ws)
            operation_id = str(uuid.uuid4())
            db.execute(
                sql_text(
                    "INSERT INTO operations VALUES (:operation_id,:allow_id,:tenant_id,:id,:operation,:plan_hash,:generation,:observed_now)"
                ),
                {
                    "operation_id": operation_id,
                    "allow_id": allow_id,
                    "tenant_id": ctx.tenant_id,
                    "id": ws["id"],
                    "operation": operation,
                    "plan_hash": plan_hash,
                    "generation": generation,
                    "observed_now": self.clock(),
                },
            )
            db.execute(
                sql_text("UPDATE allows SET state='CONSUMED' WHERE id=:allow_id"),
                {"allow_id": allow_id},
            )
            self._event(
                db,
                "control.allow.consumed",
                ctx=ctx,
                workspace_id=ws["id"],
                allow_id=allow_id,
                operation_id=operation_id,
                generation=generation,
            )
            return dict(
                db.execute(
                    sql_text("SELECT * FROM operations WHERE id=:operation_id"),
                    {"operation_id": operation_id},
                )
                .mappings()
                .fetchone()
            )

    def get_operation(self, ctx, operation_id):
        with self._tx(ctx, "operation.read") as db:
            row = (
                db.execute(
                    sql_text("SELECT * FROM operations WHERE id=:operation_id"),
                    {"operation_id": identifier(operation_id)},
                )
                .mappings()
                .fetchone()
            )
            if row is None or row["tenant_id"] != ctx.tenant_id:
                raise fail("CONTROL_ACCESS_DENIED", "operation.read")
            self._workspace(db, ctx, row["workspace_id"])
            return dict(row)

    def events(self, ctx, *, after=0, workspace_id=None):
        with self._tx(ctx, "events.read") as db:
            if type(after) is not int or after < 0:
                raise fail("CONTROL_CONFIG_INVALID", "events.read")
            if workspace_id is not None:
                self._workspace(db, ctx, workspace_id)
            # Tenant events are admin-only; other users get only owned workspace/job events.
            rows = (
                db.execute(
                    sql_text(
                        "SELECT seq,body FROM events WHERE tenant_id=:tenant_id AND seq>:after AND\n              (CAST(:workspace_filter AS TEXT) IS NULL OR workspace_id=:workspace_filter) AND\n              (:is_admin=1 OR principal_id=:principal_id OR workspace_id IN\n                (SELECT id FROM workspaces WHERE tenant_id=:tenant_id_4 AND principal_id=:principal_id_5)) ORDER BY seq LIMIT 200"
                    ),
                    {
                        "tenant_id": ctx.tenant_id,
                        "after": after,
                        "workspace_filter": workspace_id,
                        "is_admin": int(ctx.is_admin),
                        "principal_id": ctx.principal_id,
                        "tenant_id_4": ctx.tenant_id,
                        "principal_id_5": ctx.principal_id,
                    },
                )
                .mappings()
                .fetchall()
            )
            return [{"sequence": row["seq"], **json.loads(row["body"])} for row in rows]

    def control(self, ctx, workspace_id):
        with self._tx(ctx, "lease.read") as db:
            ws = self._workspace(db, ctx, workspace_id)
            lease = (
                db.execute(
                    sql_text(
                        "SELECT * FROM leases WHERE tenant_id=:tenant_id AND workspace_id=:workspace_id AND expires_at>:observed_now ORDER BY expires_at DESC LIMIT 1"
                    ),
                    {
                        "tenant_id": ctx.tenant_id,
                        "workspace_id": workspace_id,
                        "observed_now": self.clock(),
                    },
                )
                .mappings()
                .fetchone()
            )
            return {
                "owner": "human"
                if lease
                else "bot"
                if self._active(db, ws)["active_jobs"]
                else "none",
                "lease_id": lease["id"] if lease else None,
                "expires_at": lease["expires_at"] if lease else None,
            }

    def allows(self, ctx, workspace_id):
        with self._tx(ctx, "allow.list") as db:
            ws = self._workspace(db, ctx, workspace_id)
            return [
                dict(row)
                for row in db.execute(
                    sql_text(
                        "SELECT * FROM allows WHERE tenant_id=:tenant_id AND workspace_id=:workspace_id AND generation=:generation AND expires_at>:observed_now AND state IN ('PENDING','APPROVED') ORDER BY expires_at,id"
                    ),
                    {
                        "tenant_id": ctx.tenant_id,
                        "workspace_id": workspace_id,
                        "generation": ws["generation"],
                        "observed_now": self.clock(),
                    },
                ).mappings()
            ]

    def append_observation(
        self,
        ctx,
        workspace_id,
        *,
        event,
        outcome="RUNNING",
        error=None,
        attributes=None,
        text=None,
        job_id=None,
        worker_id=None,
        generation=None,
    ):
        """Internal observer: diagnostic text stays out of canonical event attributes."""
        with self._tx(ctx, "observe", job_id=job_id) as db:
            self._workspace(db, ctx, workspace_id)
            if not ctx.is_admin:
                raise fail("CONTROL_ACCESS_DENIED", "observe")
            if event not in (
                "control.connection.observed",
                "control.worker.log",
                "control.worker.stage",
                "control.worker.result",
                "control.upload.saved",
                "control.chat.queued",
                "control.chat.started",
                "control.chat.result",
            ):
                raise fail("CONTROL_CONFIG_INVALID", "observe")
            if job_id:
                job = self._fence(db, job_id, worker_id, generation)
                if (job["tenant_id"], job["workspace_id"]) != (
                    ctx.tenant_id,
                    workspace_id,
                ):
                    raise fail("CONTROL_ACCESS_DENIED", "observe")
            attrs = dict(attributes or {})
            if set(attrs) - {
                "status",
                "hostname",
                "phase",
                "upload_id",
                "file_count",
                "stream",
                "receipt_sha256",
                "message_id", "provider", "model", "session_id", "thread_id", "turn_id",
                "step", "started_at", "duration_s", "attempt_id", "check", "project",
            }:
                raise fail("CONTROL_CONFIG_INVALID", "observe")
            if text is not None and (
                not isinstance(text, str) or len(text.encode()) > 65536
            ):
                raise fail("CONTROL_CONFIG_INVALID", "observe")
            seq = self._event(
                db,
                event,
                ctx=ctx,
                phase=attrs.pop("phase", "observe"),
                outcome=outcome,
                error=error,
                workspace_id=workspace_id,
                job_id=job_id,
                generation=generation,
                diagnostic_available=text is not None,
                **attrs,
            )
            if text is not None:
                db.execute(
                    sql_text("INSERT INTO diagnostics VALUES (:seq,:text)"),
                    {"seq": seq, "text": text},
                )
            return seq

    def diagnostics(self, ctx, workspace_id, *, after=0):
        with self._tx(ctx, "diagnostics.read") as db:
            self._workspace(db, ctx, workspace_id)
            if type(after) is not int or after < 0:
                raise fail("CONTROL_CONFIG_INVALID", "diagnostics.read")
            return [
                {"sequence": row["event_seq"], "text": row["text"]}
                for row in db.execute(
                    sql_text(
                        "SELECT d.event_seq,d.text FROM diagnostics d\n              JOIN events e ON d.event_seq=e.seq WHERE e.tenant_id=:tenant_id AND e.workspace_id=:workspace_id\n              AND d.event_seq>:after ORDER BY d.event_seq LIMIT 200"
                    ),
                    {
                        "tenant_id": ctx.tenant_id,
                        "workspace_id": workspace_id,
                        "after": after,
                    },
                ).mappings()
            ]

    def snapshot_events(self, ctx, workspace_id, *, limit=200):
        """Atomic high-water cursor plus a bounded recent diagnostic window."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise fail("CONTROL_CONFIG_INVALID", "events.snapshot")
        with self._tx(ctx, "events.snapshot") as db:
            self._workspace(db, ctx, workspace_id)
            cursor = db.execute(
                sql_text(
                    "SELECT COALESCE(MAX(seq),0) FROM events WHERE tenant_id=:tenant AND workspace_id=:workspace"
                ),
                {"tenant": ctx.tenant_id, "workspace": workspace_id},
            ).scalar_one()
            rows = (
                db.execute(
                    sql_text(
                        "SELECT d.event_seq,d.text FROM diagnostics d JOIN events e ON d.event_seq=e.seq WHERE e.tenant_id=:tenant AND e.workspace_id=:workspace AND e.seq<=:cursor ORDER BY d.event_seq DESC LIMIT :limit"
                    ),
                    {
                        "tenant": ctx.tenant_id,
                        "workspace": workspace_id,
                        "cursor": cursor,
                        "limit": limit + 1,
                    },
                )
                .mappings()
                .all()
            )
            size = 0
            logs = []
            for row in rows[:limit]:
                size += len(row["text"].encode())
                if size > 262144:
                    break
                logs.append({"sequence": row["event_seq"], "text": row["text"]})
            return {
                "cursor": cursor,
                "logs": list(reversed(logs)),
                "logs_truncated": len(logs) < len(rows),
            }
