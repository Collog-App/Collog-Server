import sqlalchemy as sa
from alembic import op

revision = "0012_profile_concerns"
down_revision = "0011_google_login"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("parent_profiles") as batch:
        batch.add_column(sa.Column("concerns", sa.Text(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
