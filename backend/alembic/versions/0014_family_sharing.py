import sqlalchemy as sa
from alembic import op

revision = "0014_family_sharing"
down_revision = "0013_user_signup_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("marketing_opt_in", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_table(
        "family_shares",
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("viewer_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("report", sa.Boolean(), nullable=False),
        sa.Column("timeline", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_family_shares_viewer_id", "family_shares", ["viewer_id"])


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
