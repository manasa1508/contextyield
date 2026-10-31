"""Idempotent schema initialization for local and controlled deployments."""

from pathlib import Path

import psycopg

from .config import load_settings


def main() -> None:
    migrations = sorted((Path(__file__).resolve().parent.parent / "migrations").glob("*.sql"))
    with psycopg.connect(load_settings().database_url) as conn:
        for migration in migrations:
            conn.execute(migration.read_text())
    print("ContextYield schema ready")


if __name__ == "__main__":
    main()
