"""Remove unsafe time-based outbox deletion; pending events must survive outages."""

from alembic import op

revision = "0007_outbox_retention"
down_revision = "0006_qa_bounty_deadline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
            IF EXISTS (
              SELECT 1 FROM timescaledb_information.hypertables
              WHERE hypertable_schema = current_schema()
                AND hypertable_name = 'outbox_events'
            ) THEN
              PERFORM remove_retention_policy('outbox_events', if_exists => TRUE);
            END IF;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    raise RuntimeError("自动恢复 outbox 时间保留策略会丢失待投递事件")
