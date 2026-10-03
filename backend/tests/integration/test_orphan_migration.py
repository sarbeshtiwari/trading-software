"""SQLite migration preserves values and refuses every unavailable accounting field."""

from decimal import Decimal

import pytest
import sqlalchemy as sa

from tests.integration.test_orphan_postgres import migration


def test_sqlite_orphan_migration_preserves_accounting_and_refuses_lossy_downgrade(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'orphan-migration.db'}")
    fields = ("realised_pnl", "unrealised_pnl", "total_charges")
    metadata = sa.MetaData()
    positions = sa.Table(
        "positions", metadata,
        sa.Column("id", sa.String(40), primary_key=True),
        *(sa.Column(field, sa.Numeric(20, 2), nullable=False) for field in fields),
    )
    sa.Index("ix_fixture_realised", positions.c.realised_pnl)
    known = dict(zip(fields, map(Decimal, ("123.45", "-67.89", "9.10")), strict=True))
    try:
        with engine.begin() as connection:
            metadata.create_all(connection)
            connection.execute(positions.insert().values(id="fixture-position", **known))
            migration(connection, "upgrade")
            assert all(column["nullable"] for column in sa.inspect(connection).get_columns(
                "positions"
            ) if column["name"] in fields)
            assert dict(connection.execute(sa.select(positions)).mappings().one()) == {
                "id": "fixture-position", **known,
            }
            for field in fields:
                connection.execute(positions.update().values({field: None}))
                with pytest.raises(RuntimeError, match="accounting is unavailable"):
                    migration(connection, "downgrade")
                assert connection.scalar(sa.select(positions.c[field])) is None
                connection.execute(positions.update().values({field: known[field]}))
            migration(connection, "downgrade")
            assert all(not column["nullable"] for column in sa.inspect(connection).get_columns(
                "positions"
            ) if column["name"] in fields)
            assert dict(connection.execute(sa.select(positions)).mappings().one()) == {
                "id": "fixture-position", **known,
            }
            assert "ix_fixture_realised" in {
                index["name"] for index in sa.inspect(connection).get_indexes("positions")
            }
    finally:
        engine.dispose()
