"""Read-only connection helper for the service layer.

Deliberately separate from crawler/db.py: that module owns schema and
writes (upserts, backfills) for ingestion. This one only ever reads, on
behalf of whatever calls into service/queries.py (FastAPI later, MCP
later) — never two independent query implementations, per the plan.
"""
import psycopg

from crawler import config


def connect() -> psycopg.Connection:
    return psycopg.connect(config.DATABASE_URL)
