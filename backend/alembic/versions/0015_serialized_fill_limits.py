"""Serialize cross-row fill limits and guard order quantity reductions."""

import sqlalchemy as sa
from alembic import op

revision = "0015_serialized_fill_limits"
down_revision = "0014_broker_auth_budget"
branch_labels = None
depends_on = None

FILL_LIMIT = """
CREATE OR REPLACE FUNCTION ats_check_fill_sum() RETURNS trigger AS $$
DECLARE
    other_fills bigint;
    ordered_quantity integer;
BEGIN
    UPDATE orders SET quantity = quantity WHERE id = NEW.order_id
        RETURNING quantity INTO ordered_quantity;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'fill parent order is unavailable'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    SELECT COALESCE(SUM(quantity), 0) INTO other_fills
        FROM trades WHERE order_id = NEW.order_id AND id <> NEW.id;
    IF other_fills + NEW.quantity > ordered_quantity THEN
        RAISE EXCEPTION 'fills exceed ordered quantity'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

ORDER_LIMIT = """
CREATE OR REPLACE FUNCTION ats_check_order_fill_limit() RETURNS trigger AS $$
BEGIN
    IF (SELECT COALESCE(SUM(quantity), 0) FROM trades WHERE order_id = NEW.id)
       > NEW.quantity THEN
        RAISE EXCEPTION 'order quantity cannot be lower than recorded fills'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("LOCK TABLE orders, trades IN SHARE ROW EXCLUSIVE MODE")
    invalid = bind.scalar(
        sa.text(
            "SELECT count(*) FROM (SELECT orders.id FROM orders JOIN trades "
            "ON trades.order_id=orders.id GROUP BY orders.id, orders.quantity "
            "HAVING SUM(trades.quantity)>orders.quantity) AS overfilled"
        )
    )
    if invalid:
        raise RuntimeError("Existing overfilled orders require reconciliation before migration")
    op.execute(FILL_LIMIT)
    op.execute(ORDER_LIMIT)
    op.execute("""
        CREATE TRIGGER orders_fill_limit BEFORE UPDATE OF quantity ON orders
        FOR EACH ROW WHEN (NEW.quantity IS DISTINCT FROM OLD.quantity)
        EXECUTE FUNCTION ats_check_order_fill_limit()
    """)


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        raise RuntimeError("Refusing to remove serialized fill safety; use reviewed recovery")
