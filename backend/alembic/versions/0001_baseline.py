from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from alembic.migration import MigrationContext
from sqlalchemy.engine import Connection

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def baseline_metadata() -> sa.MetaData:
    spec = spec_from_file_location("collog_baseline", Path(__file__).parents[1] / "baseline.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Migration baseline is unavailable")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.baseline_metadata()


def validate_existing(connection: Connection, metadata: sa.MetaData) -> None:
    inspector = sa.inspect(connection)
    migration = MigrationContext.configure(connection)
    existing = set(inspector.get_table_names())
    errors = []
    for table in metadata.sorted_tables:
        if table.name not in existing:
            continue
        columns = {column["name"]: column for column in inspector.get_columns(table.name)}
        for column in table.columns:
            actual = columns.get(column.name)
            label = f"{table.name}.{column.name}"
            if actual is None:
                errors.append(f"missing column {label}")
                continue
            expected_type = column.type.compile(dialect=connection.dialect)
            actual_type = actual["type"].compile(dialect=connection.dialect)
            if migration.impl.compare_type(sa.Column(column.name, actual["type"]), column):
                errors.append(f"type mismatch {label} ({actual_type} != {expected_type})")
            if not column.primary_key and actual["nullable"] != column.nullable:
                errors.append(f"nullable mismatch {label}")
        actual_pk = set(inspector.get_pk_constraint(table.name)["constrained_columns"])
        if actual_pk != set(table.primary_key.columns.keys()):
            errors.append(f"primary key mismatch {table.name}")
        actual_unique = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints(table.name)
        }
        actual_unique.update(
            tuple(item["column_names"])
            for item in inspector.get_indexes(table.name)
            if item["unique"]
        )
        for index in table.indexes:
            if index.unique and tuple(index.columns.keys()) not in actual_unique:
                errors.append(f"missing unique constraint {table.name}.{index.name}")
        actual_fk = {
            (
                tuple(item["constrained_columns"]),
                item["referred_table"],
                tuple(item["referred_columns"]),
            )
            for item in inspector.get_foreign_keys(table.name)
        }
        for constraint in table.foreign_key_constraints:
            expected_fk = (
                tuple(element.parent.name for element in constraint.elements),
                constraint.referred_table.name,
                tuple(element.column.name for element in constraint.elements),
            )
            if expected_fk not in actual_fk:
                errors.append(f"foreign key mismatch {table.name}")
    if errors:
        raise RuntimeError(
            "Existing schema is incompatible. No data was changed. " + ", ".join(errors)
        )


def upgrade() -> None:
    connection = op.get_bind()
    metadata = baseline_metadata()
    validate_existing(connection, metadata)
    metadata.create_all(connection, checkfirst=True)


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
