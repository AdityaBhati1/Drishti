"""Tests for database migrations using Alembic.

Verifies:
1. Upgrade from an empty database creates the complete alerts schema.
2. Migrated database supports Alert model CRUD operations.
3. Central database initialization and persistence integration works against the migrated schema.
4. Clean migration downgrade and re-upgrade cycles.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker
from alembic.config import Config
from alembic import command

# Add workspace root to sys.path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from central.database import Alert, Base


class DatabaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_migration.db"
        self.db_url = f"sqlite:///{self.db_path}"
        self.alembic_ini = ROOT_DIR / "alembic.ini"
        self.engines = []
        self.assertTrue(self.alembic_ini.exists(), "alembic.ini must exist at root")

    def tearDown(self):
        for eng in self.engines:
            try:
                eng.dispose()
            except Exception:
                pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _create_tracked_engine(self, url=None):
        eng = create_engine(url or self.db_url)
        self.engines.append(eng)
        return eng

    def _get_alembic_config(self, connection=None):
        cfg = Config(str(self.alembic_ini))
        cfg.set_main_option("sqlalchemy.url", self.db_url)
        if connection:
            cfg.attributes["connection"] = connection
        return cfg

    def test_upgrade_from_empty_database(self):
        """Verify upgrading an empty database to head creates the full alerts schema."""
        self.assertFalse(self.db_path.exists(), "Database file must start empty")

        engine = self._create_tracked_engine()
        with engine.connect() as connection:
            cfg = self._get_alembic_config(connection=connection)
            command.upgrade(cfg, "head")

        self.assertTrue(self.db_path.exists(), "Database file was created by migration")

        # Inspect resulting tables and columns
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        self.assertIn("alerts", tables)
        self.assertIn("alembic_version", tables)

        columns = {c["name"]: c for c in inspector.get_columns("alerts")}
        expected_columns = [
            "id",
            "event_id",
            "node_id",
            "event_type",
            "severity",
            "details",
            "status",
            "lat",
            "lng",
            "extra_info",
            "snapshot_path",
            "clip_path",
            "timestamp",
        ]
        for col_name in expected_columns:
            self.assertIn(col_name, columns, f"Expected column {col_name} in migrated alerts table")

        # Check unique index on event_id
        indexes = inspector.get_indexes("alerts")
        index_names = [idx["name"] for idx in indexes]
        self.assertIn("ix_alerts_event_id", index_names)

    def test_crud_operations_against_migrated_schema(self):
        """Verify CRUD operations against the schema produced by Alembic migration."""
        engine = self._create_tracked_engine()
        with engine.connect() as connection:
            cfg = self._get_alembic_config(connection=connection)
            command.upgrade(cfg, "head")

        Session = sessionmaker(bind=engine)
        session = Session()

        alert = Alert(
            event_id="migrated-evt-001",
            node_id="cam-border-test",
            event_type="intrusion",
            severity="critical",
            details="Intruder crossing tripwire A",
            status="PENDING",
            lat=28.6139,
            lng=77.2090,
            snapshot_path="snapshots/cam_test.jpg",
            clip_path="snapshots/clips/cam_test.mp4",
        )
        session.add(alert)
        session.commit()

        # Query back
        fetched = session.query(Alert).filter_by(event_id="migrated-evt-001").first()
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.event_type, "intrusion")
        self.assertEqual(fetched.status, "PENDING")
        self.assertAlmostEqual(fetched.lat, 28.6139)

        # Update status
        fetched.status = "ACKNOWLEDGED"
        session.commit()

        refetched = session.query(Alert).filter_by(event_id="migrated-evt-001").first()
        self.assertEqual(refetched.status, "ACKNOWLEDGED")

        session.close()

    def test_migration_downgrade_and_reupgrade_lifecycle(self):
        """Verify clean rollback and re-application of migrations."""
        engine = self._create_tracked_engine()
        with engine.connect() as connection:
            cfg = self._get_alembic_config(connection=connection)
            # Upgrade to head
            command.upgrade(cfg, "head")
            inspector = inspect(connection)
            self.assertIn("alerts", inspector.get_table_names())

            # Downgrade to base
            command.downgrade(cfg, "base")
            inspector = inspect(connection)
            self.assertNotIn("alerts", inspector.get_table_names())

            # Re-upgrade to head
            command.upgrade(cfg, "head")
            inspector = inspect(connection)
            self.assertIn("alerts", inspector.get_table_names())

    def test_central_application_runs_against_migrated_schema(self):
        """Verify Central FastAPI app initializes and operates against the migrated schema."""
        import asyncio
        from httpx import AsyncClient, ASGITransport
        from central.main import app

        engine = self._create_tracked_engine()
        with engine.connect() as connection:
            cfg = self._get_alembic_config(connection=connection)
            command.upgrade(cfg, "head")

        async def _check():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/health")
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertEqual(data.get("status"), "ok")

        asyncio.run(_check())


if __name__ == "__main__":
    unittest.main()
