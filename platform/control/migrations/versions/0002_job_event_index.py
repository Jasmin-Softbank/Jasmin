"""Index immutable event ownership for scoped run history; no event rewrite."""
from alembic import op
import sqlalchemy as sa

revision = '0002_job_event_index'
down_revision = '0001_control'
branch_labels = None
depends_on = None


def upgrade():
    job = "json_extract(body, '$.attributes.job_id')" if op.get_bind().dialect.name == 'sqlite' else "(body::jsonb #>> '{attributes,job_id}')"
    op.execute(sa.text(f'CREATE INDEX event_workspace_job ON events(tenant_id, workspace_id, ({job}), seq)'))


def downgrade():
    op.drop_index('event_workspace_job', table_name='events')
