import sqlalchemy as sa
from alembic import op

revision = "0013_user_signup_fields"
down_revision = "0012_profile_concerns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("nickname", sa.String(10), nullable=True))
        batch.add_column(sa.Column("gender", sa.String(8), nullable=True))
        batch.add_column(sa.Column("birth_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("email", sa.String(320), nullable=True))
        batch.create_index("ix_users_nickname", ["nickname"], unique=True)


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
