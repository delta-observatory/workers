"""End-to-end worker flow on a real, temporary PostgreSQL server (pgserver).

The test applies the vendored migrations, queues an index job, executes it,
queues a tess-bls job for WASP-18, executes it, and reads the records.
"""
from pathlib import Path
import shutil
import uuid

import pgserver
import psycopg
from psycopg.types.json import Jsonb
import pytest

from delta_worker import config, db
from delta_worker.__main__ import process
from delta_worker.contracts import Contracts

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
CONTRACTS = Contracts(ROOT / 'vendor' / 'contracts')
WASP18 = 'tess2018234235059-s0002-0000000100100827-0121-s_lc.fits'


@pytest.fixture(scope='module')
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


@pytest.fixture
def settings(database_url, tmp_path):
    source = WORKSPACE / 'raw' / 'tess-spoc-light-curves' / WASP18
    if not source.exists():
        pytest.skip('WASP-18 light curve is not available')
    raw = tmp_path / 'raw' / 'tess-spoc-light-curves'
    raw.mkdir(parents=True)
    shutil.copy(source, raw / WASP18)
    catalog = tmp_path / 'catalog' / 'datasets' / 'tess-spoc-light-curves'
    catalog.mkdir(parents=True)
    (tmp_path / 'artifacts').mkdir()
    return config.Settings(
        database_url=database_url, raw_root=tmp_path / 'raw', artifact_root=tmp_path / 'artifacts',
        catalog_root=tmp_path / 'catalog', contracts_dir=ROOT / 'vendor' / 'contracts',
        worker_id='test-worker', worker_version='test', datasets=('tess-spoc-light-curves',),
        lease_seconds=60, poll_seconds=1)


def run_next(conn, settings):
    row = db.claim(conn, settings.worker_id, settings.lease_seconds)
    assert row is not None
    process(conn, CONTRACTS, settings, row)
    return conn.execute('SELECT * FROM job WHERE id = %s', (row['id'],)).fetchone()


@pytest.fixture
def conn(settings):
    with db.connect(settings.database_url) as connection:
        connection.execute('TRUNCATE measurement, artifact, job, run_input, run, observation, dataset CASCADE')
        yield connection


def test_index_then_search(settings, conn):
    job_id = db.ensure_index_job(conn, CONTRACTS, 'tess-spoc-light-curves', 'test')
    assert job_id
    assert db.ensure_index_job(conn, CONTRACTS, 'tess-spoc-light-curves', 'test') is None

    job = run_next(conn, settings)
    assert job['state'] == 'complete', job['result']
    observation = conn.execute('SELECT * FROM observation').fetchone()
    assert observation['target_id'] == 'TIC:100100827' and observation['state'] == 'ready'
    assert observation['time_scale'] == 'tdb' and observation['sector'] == 2
    dataset = conn.execute('SELECT * FROM dataset').fetchone()
    assert dataset['state'] == 'ready'
    assert any(f['kind'] == 'unlabeled' for f in dataset['findings'])
    light_curve = conn.execute("SELECT * FROM artifact WHERE kind = 'light_curve'").fetchone()
    assert (settings.artifact_root / light_curve['path']).stat().st_size == light_curve['bytes']

    # A second index finds the same file unchanged.
    db.ensure_index_job(conn, CONTRACTS, 'tess-spoc-light-curves', 'test')
    job = run_next(conn, settings)
    assert job['result']['diagnostics']['unchanged'] == 1
    assert conn.execute('SELECT count(*) AS n FROM observation').fetchone()['n'] == 1

    run_id, bls_id = str(uuid.uuid4()), str(uuid.uuid4())
    parameters = CONTRACTS.defaults('tess-bls', 1)
    document = {'contract': 'job/1', 'job_id': bls_id, 'run_id': run_id,
                'method': {'id': 'tess-bls', 'version': 1}, 'parameters': parameters,
                'inputs': [{'observation_id': str(observation['id'])}],
                'limits': {'maximum_seconds': 600, 'maximum_memory_mib': 2048}}
    with conn.transaction():
        conn.execute("""INSERT INTO run (id, method_id, method_version, parameters, parameters_sha256, requested_by)
                        VALUES (%s, 'tess-bls', 1, %s, %s, 'test')""",
                     (run_id, Jsonb(parameters), db.parameters_sha256(parameters)))
        conn.execute('INSERT INTO run_input VALUES (%s, %s)', (run_id, observation['id']))
        conn.execute('INSERT INTO job (id, run_id, observation_id, document) VALUES (%s, %s, %s, %s)',
                     (bls_id, run_id, observation['id'], Jsonb(document)))
    job = run_next(conn, settings)
    assert job['state'] == 'complete', job['result'].get('error')
    values = {r['name']: r['value_number'] for r in
              conn.execute('SELECT * FROM measurement WHERE job_id = %s', (bls_id,)).fetchall()}
    assert values['period'] == 0.9413849094310086
    kinds = {r['kind'] for r in conn.execute('SELECT kind FROM artifact WHERE job_id = %s', (bls_id,)).fetchall()}
    assert kinds == {'input_checks', 'prepared_series', 'periodogram', 'search_summary'}
    assert job['result']['resources']['peak_rss_mib'] > 0


def test_memory_limit_stops_job(settings, conn):
    db.ensure_index_job(conn, CONTRACTS, 'tess-spoc-light-curves', 'test')
    row = db.claim(conn, settings.worker_id, settings.lease_seconds)
    document = dict(row['document'])
    document['limits'] = {'maximum_seconds': 600, 'maximum_memory_mib': 64}
    with conn.transaction():
        conn.execute('UPDATE job SET document = %s WHERE id = %s', (Jsonb(document), row['id']))
    row = conn.execute('SELECT * FROM job WHERE id = %s', (row['id'],)).fetchone()
    process(conn, CONTRACTS, settings, row)
    job = conn.execute('SELECT * FROM job WHERE id = %s', (row['id'],)).fetchone()
    assert job['state'] == 'failed'
    assert job['result']['error']['kind'] == 'memory_limit'
    assert conn.execute('SELECT state FROM dataset').fetchone()['state'] == 'invalid'
