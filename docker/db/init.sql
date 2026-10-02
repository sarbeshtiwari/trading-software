-- Executed once when the database volume is first created.
-- The TimescaleDB image ships the extension; the migration also creates it
-- defensively, so a plain PostgreSQL image still works (without hypertables).
CREATE EXTENSION IF NOT EXISTS timescaledb;
