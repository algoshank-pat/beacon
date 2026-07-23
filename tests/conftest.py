import pytest

from app.db import get_connection
from app.migrate import run_migrations


@pytest.fixture
def db_conn(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    run_migrations(conn)
    yield conn
    conn.close()
