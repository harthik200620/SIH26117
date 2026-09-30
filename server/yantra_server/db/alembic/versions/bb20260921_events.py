"""Durable run activity. Existing runs and files are preserved."""
from alembic import op
import sqlalchemy as sa

revision = "bb20260921_events"
down_revision = "9ec23a44877b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_events",
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("frame", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "seq"),
    )


def downgrade() -> None:
    op.drop_table("run_events")
