import sqlalchemy as sa
from alembic import op

revision = "0006_apple_login"
down_revision = "0005_device_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.alter_column("phone", existing_type=sa.String(20), nullable=True)
        batch.add_column(sa.Column("apple_subject", sa.String(255), nullable=True))
        batch.create_index("ix_users_apple_subject", ["apple_subject"], unique=True)
    op.create_table(
        "apple_login_challenges",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("nonce_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_apple_login_challenges_expires_at", "apple_login_challenges", ["expires_at"]
    )


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
