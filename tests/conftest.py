"""A temporary PostgreSQL server (pgserver) with the vendored migrations, shared by the flow tests."""
from pathlib import Path

import pgserver
import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='session')
def database_url(tmp_path_factory):
    server = pgserver.get_server(tmp_path_factory.mktemp('pg'), cleanup_mode='stop')
    base = server.get_uri()
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute('CREATE DATABASE delta')
    url = base.replace('/postgres?', '/delta?')
    with psycopg.connect(url, autocommit=True) as conn:
        for path in sorted((ROOT / 'vendor' / 'migrations').glob('[0-9]*.sql')):
            conn.execute(path.read_text())
    yield url
    server.cleanup()
