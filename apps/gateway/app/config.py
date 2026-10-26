import os

from sqlalchemy import URL


def database_url() -> URL:
    password = os.getenv("PGPASSWORD", "")
    if not password:
        raise RuntimeError("PGPASSWORD must contain the gateway database password.")

    return URL.create(
        "postgresql+psycopg",
        username=os.getenv("PGUSER", "gateway_app"),
        password=password,
        host=os.getenv("PGHOST", "postgres"),
        port=int(os.getenv("PGPORT", "5432")),
        database=os.getenv("PGDATABASE", "gateway"),
    )
