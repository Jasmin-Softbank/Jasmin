"""Initial control state. Immutable historical schema; do not import runtime metadata."""

from pathlib import Path
from alembic import op
import sqlalchemy as sa

revision = "0001_control"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "control_lock",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.CheckConstraint("id=1", name="ck_control_lock_singleton"),
    )
    op.execute(sa.text("INSERT INTO control_lock(id) VALUES (1)"))
    op.create_table(
        "accounts",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("capacity", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Integer, nullable=False),
        sa.CheckConstraint("capacity>0", name="ck_accounts_capacity"),
    )
    op.create_table(
        "workspaces",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("account_id", sa.Text, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False, server_default="1"),
        sa.Column("provider", sa.Text),
        sa.Column("resource_id", sa.Text),
        sa.Column("ready", sa.Integer, nullable=False, server_default="0"),
        sa.Column("observed_at", sa.Float),
        sa.Column("evidence_ref", sa.Text),
        sa.CheckConstraint("state IN ('ACTIVE','DRAINING')", name="ck_workspace_state"),
        sa.UniqueConstraint("provider", "resource_id", name="uq_workspace_resource"),
    )
    op.create_table(
        "allows",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("operation", sa.Text, nullable=False),
        sa.Column("plan_hash", sa.Text, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("approved_by", sa.Text),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"], ["workspaces.tenant_id", "workspaces.id"]
        ),
        sa.CheckConstraint(
            "state IN ('PENDING','APPROVED','DENIED','CONSUMED')", name="ck_allow_state"
        ),
    )
    op.create_table(
        "operations",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "allow_id", sa.Text, sa.ForeignKey("allows.id"), nullable=False, unique=True
        ),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("operation", sa.Text, nullable=False),
        sa.Column("plan_hash", sa.Text, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_table(
        "leases",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"], ["workspaces.tenant_id", "workspaces.id"]
        ),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("account_id", sa.Text, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("request_hash", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column("operation_id", sa.Text, sa.ForeignKey("operations.id"), unique=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False, server_default="0"),
        sa.Column("worker_id", sa.Text),
        sa.Column("lease_until", sa.Float),
        sa.Column("cancel_requested", sa.Integer, nullable=False, server_default="0"),
        sa.Column("requires_ready", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("error_json", sa.Text),
        sa.Column("input_lease_id", sa.Text),
        sa.Column("queue_seq", sa.Integer, nullable=False, unique=True),
        sa.UniqueConstraint(
            "tenant_id", "workspace_id", "idempotency_key", name="uq_job_idempotency"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"], ["workspaces.tenant_id", "workspaces.id"]
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED','DISPATCHED','PASS','FAIL','BLOCKED','UNKNOWN','CANCELLED')",
            name="ck_job_status",
        ),
    )
    op.create_index("job_capacity", "jobs", ["account_id", "status"])
    op.create_table(
        "events",
        sa.Column("seq", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text),
        sa.Column("workspace_id", sa.Text),
        sa.Column("principal_id", sa.Text),
        sa.Column("body", sa.Text, nullable=False),
    )
    op.create_index("event_tenant_cursor", "events", ["tenant_id", "seq"])
    op.create_table(
        "diagnostics",
        sa.Column(
            "event_seq", sa.Integer, sa.ForeignKey("events.seq"), primary_key=True
        ),
        sa.Column("text", sa.Text, nullable=False),
    )
    sql = (
        Path(__file__).parents[1]
        / "sql"
        / f"{op.get_bind().dialect.name}-events-append-only.sql"
    ).read_text()
    # SQLite DBAPI accepts one statement, PostgreSQL accepts the function body as-is.
    if op.get_bind().dialect.name == "sqlite":
        for statement in sql.strip().splitlines():
            op.execute(sa.text(statement))
    else:
        op.get_bind().exec_driver_sql(sql)


def downgrade():
    raise RuntimeError(
        "Control history is append-only; restore a reviewed backup instead of destructive downgrade"
    )
