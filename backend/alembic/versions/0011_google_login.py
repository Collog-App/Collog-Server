import sqlalchemy as sa
from alembic import op

revision = "0011_google_login"
down_revision = "0010_processing_retries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("google_subject", sa.String(255), nullable=True))
        batch.create_index("ix_users_google_subject", ["google_subject"], unique=True)
    op.create_table(
        "google_login_challenges",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("nonce_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_google_login_challenges_expires_at", "google_login_challenges", ["expires_at"]
    )


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
