import time
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.config import settings

DATABASE_URL = settings.database_url

# Bound initial connection attempts so an unavailable database does not leave
# API startup/request workers hanging indefinitely.
_engine_options = {"pool_pre_ping": True}
if DATABASE_URL.startswith("postgresql"):
    _engine_options["connect_args"] = {"connect_timeout": 3}
elif DATABASE_URL.startswith("sqlite"):
    _engine_options["connect_args"] = {"check_same_thread": False}
    if ":memory:" in DATABASE_URL:
        _engine_options["poolclass"] = StaticPool
engine = create_engine(DATABASE_URL, **_engine_options)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String(36), unique=True, nullable=False, index=True)
    node_id = Column(String(128), nullable=False)
    event_type = Column(String(100), nullable=False)
    severity = Column(String(20), nullable=False)
    details = Column(String(1024), nullable=True)
    status = Column(String(50), default="PENDING", nullable=False)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    extra_info = Column(Text, nullable=True)
    snapshot_path = Column(String(512), nullable=True)
    clip_path = Column(String(512), nullable=True)
    timestamp = Column(DateTime, server_default=text("NOW()"))

def apply_migrations() -> bool:
    """Apply Alembic migrations to current database if available."""
    try:
        from alembic.config import Config
        from alembic import command
        from pathlib import Path
        alembic_ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
        if alembic_ini_path.exists():
            alembic_cfg = Config(str(alembic_ini_path))
            alembic_cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
            with engine.connect() as connection:
                alembic_cfg.attributes["connection"] = connection
                command.upgrade(alembic_cfg, "head")
            print("[Central DB] Applied Alembic migrations successfully.")
            return True
    except Exception as exc:
        print(f"[Central DB] Alembic migration note ({exc}); proceeding with metadata sync.")
    return False


def init_db():
    dialect = engine.dialect.name
    print(f"[Central DB] Initializing database (dialect: {dialect})...")
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1;"))
    except Exception as e:
        print(f"[Central DB] Database is unavailable ({e}). Persistence is unavailable.")
        return False

    # 1. Attempt schema migration via Alembic
    apply_migrations()

    if dialect == "postgresql":
        for i in range(3):
            try:
                with engine.connect() as conn:
                    # Enable PostGIS extension
                    conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
                    conn.commit()
                    print("[Central DB] PostGIS extension initialized.")
                
                    # Ensure SQLAlchemy tables exist
                    Base.metadata.create_all(bind=engine)
                
                # Add spatial column, index, and trigger to automatically populate geom Point from lat/lng
                with engine.connect() as conn:
                    conn.execute(text("""
                        ALTER TABLE alerts ADD COLUMN IF NOT EXISTS event_id VARCHAR(36);
                    """))
                    conn.execute(text("""
                        ALTER TABLE alerts ADD COLUMN IF NOT EXISTS snapshot_path VARCHAR(512);
                    """))
                    conn.execute(text("""
                        ALTER TABLE alerts ADD COLUMN IF NOT EXISTS clip_path VARCHAR(512);
                    """))
                    conn.execute(text("""
                        CREATE UNIQUE INDEX IF NOT EXISTS alerts_event_id_idx ON alerts (event_id);
                    """))
                    conn.execute(text("""
                        ALTER TABLE alerts ADD COLUMN IF NOT EXISTS geom GEOMETRY(Point, 4326);
                    """))
                    conn.execute(text("""
                        CREATE INDEX IF NOT EXISTS alerts_geom_idx ON alerts USING GIST (geom);
                    """))
                    
                    # Trigger setup for automatic geom point creation
                    conn.execute(text("""
                        CREATE OR REPLACE FUNCTION update_alert_geom()
                        RETURNS TRIGGER AS $$
                        BEGIN
                            IF NEW.lng IS NOT NULL AND NEW.lat IS NOT NULL THEN
                                NEW.geom := ST_SetSRID(ST_MakePoint(NEW.lng, NEW.lat), 4326);
                            END IF;
                            RETURN NEW;
                        END;
                        $$ LANGUAGE plpgsql;
                    """))
                    
                    # Check and create trigger
                    conn.execute(text("""
                        DROP TRIGGER IF EXISTS trg_update_alert_geom ON alerts;
                        CREATE TRIGGER trg_update_alert_geom
                        BEFORE INSERT ON alerts
                        FOR EACH ROW
                        EXECUTE FUNCTION update_alert_geom();
                    """))
                    conn.commit()
                    
                print("[Central DB] Database tables, spatial columns, and triggers configured successfully.")
                return True
            except Exception as e:
                print(f"[Central DB] Connection retry {i+1}/3 failed. Database might still be starting. Error: {e}")
                time.sleep(2)
        return False
    else:
        # SQLite / local standalone engine
        try:
            Base.metadata.create_all(bind=engine)
            print(f"[Central DB] Tables created successfully for {dialect}.")
            return True
        except Exception as e:
            print(f"[Central DB] Table creation failed for {dialect}: {e}")
            return False


def is_database_available() -> bool:
    """Perform a short, side-effect-free persistence readiness check."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1;"))
        return True
    except Exception:
        return False

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
