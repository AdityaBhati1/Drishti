"""Initial alerts schema with spatial PostGIS support

Revision ID: 0001_initial_alerts_schema
Revises: 
Create Date: 2026-09-05 08:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0001_initial_alerts_schema'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    # 1. Create alerts table if it does not already exist
    if "alerts" not in tables:
        op.create_table(
            "alerts",
            sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
            sa.Column("event_id", sa.String(length=36), nullable=False),
            sa.Column("node_id", sa.String(length=128), nullable=False),
            sa.Column("event_type", sa.String(length=100), nullable=False),
            sa.Column("severity", sa.String(length=20), nullable=False),
            sa.Column("details", sa.String(length=1024), nullable=True),
            sa.Column("status", sa.String(length=50), nullable=False, server_default="PENDING"),
            sa.Column("lat", sa.Float(), nullable=False),
            sa.Column("lng", sa.Float(), nullable=False),
            sa.Column("extra_info", sa.Text(), nullable=True),
            sa.Column("snapshot_path", sa.String(length=512), nullable=True),
            sa.Column("clip_path", sa.String(length=512), nullable=True),
            sa.Column("timestamp", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        )

    # 2. Ensure indexes exist
    existing_indexes = [idx["name"] for idx in inspector.get_indexes("alerts")] if "alerts" in tables else []
    if "ix_alerts_id" not in existing_indexes:
        op.create_index("ix_alerts_id", "alerts", ["id"], unique=False)
    if "ix_alerts_event_id" not in existing_indexes:
        op.create_index("ix_alerts_event_id", "alerts", ["event_id"], unique=True)

    # 3. PostgreSQL-specific PostGIS spatial enhancements
    if dialect == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        
        # Check columns
        cols = [c["name"] for c in inspector.get_columns("alerts")]
        if "geom" not in cols:
            op.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS geom GEOMETRY(Point, 4326);")
        
        op.execute("CREATE INDEX IF NOT EXISTS alerts_geom_idx ON alerts USING GIST (geom);")
        
        # Geometry automatic update trigger
        op.execute("""
            CREATE OR REPLACE FUNCTION update_alert_geom()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW.lng IS NOT NULL AND NEW.lat IS NOT NULL THEN
                    NEW.geom := ST_SetSRID(ST_MakePoint(NEW.lng, NEW.lat), 4326);
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)
        op.execute("""
            DROP TRIGGER IF EXISTS trg_update_alert_geom ON alerts;
            CREATE TRIGGER trg_update_alert_geom
            BEFORE INSERT ON alerts
            FOR EACH ROW
            EXECUTE FUNCTION update_alert_geom();
        """)


def downgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_update_alert_geom ON alerts;")
        op.execute("DROP FUNCTION IF EXISTS update_alert_geom();")
        op.execute("DROP INDEX IF EXISTS alerts_geom_idx;")
    op.drop_table("alerts")
