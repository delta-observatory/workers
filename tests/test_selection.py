"""Selection manifests (tess-spoc-selection/1) in tess-spoc-index, on a temporary PostgreSQL server.

The tests copy registered development light curves into temporary raw/ and catalog directories.
They check that only listed subdirectory files enter the inventory, with their provenance, and that
changed, missing, refused, duplicate, and mismatched files give findings without duplicate observations.
"""
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from delta_worker import config, db
from delta_worker.__main__ import process
from delta_worker.contracts import Contracts
from delta_worker.methods.tess_spoc_index import selected_path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT.parent / 'raw' / 'tess-spoc-light-curves'
CONTRACTS = Contracts(ROOT / 'vendor' / 'contracts')
DATASET = 'tess-spoc-light-curves'
WASP18 = 'tess2018234235059-s0002-0000000100100827-0121-s_lc.fits'
OTHER = 'tess2021284114741-s0044-0000000172518755-0215-s_lc.fits'
THIRD = 'tess2021284114741-s0044-0000000243244680-0215-s_lc.fits'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def setup(database_url, tmp_path):
    for name in (WASP18, OTHER, THIRD):
        if not (RAW / name).exists():
            pytest.skip(f'{name} is not available')
    raw = tmp_path / 'raw' / DATASET
    raw.mkdir(parents=True)
    shutil.copy(RAW / WASP18, raw / WASP18)
    catalog = tmp_path / 'catalog'
    (catalog / 'datasets' / DATASET / 'selections').mkdir(parents=True)
    (tmp_path / 'artifacts').mkdir()
    settings = config.Settings(
        database_url=database_url, raw_root=tmp_path / 'raw', artifact_root=tmp_path / 'artifacts',
        catalog_root=catalog, contracts_dir=ROOT / 'vendor' / 'contracts', worker_id='test-worker',
        worker_version='test', datasets=(DATASET,), lease_seconds=60, poll_seconds=1)
    with db.connect(database_url) as conn:
        conn.execute('TRUNCATE measurement, artifact, job, run_input, run, observation, dataset CASCADE')
        yield settings, conn, raw, catalog / 'datasets' / DATASET / 'selections'


def entry(raw, relative, target_id, source=None):
    path = raw / relative
    return {'relative_path': relative, 'sha256': digest(path), 'bytes': path.stat().st_size,
            'source_id': f'mast:TESS/product/{source or Path(relative).name}', 'target_id': target_id}


def manifest(directory, name, files, dataset_id=DATASET):
    document = {'format': 'tess-spoc-selection/1', 'dataset_id': dataset_id, 'owner': 'agent-search-test',
                'registration': 'test-registration', 'reason': 'Selection test.', 'files': files}
    (directory / name).write_text(json.dumps(document))
    return f'datasets/{DATASET}/selections/{name}'


def index(settings, conn):
    assert db.ensure_index_job(conn, CONTRACTS, DATASET, 'test')
    row = db.claim(conn, settings.worker_id, settings.lease_seconds)
    process(conn, CONTRACTS, settings, row)
    job = conn.execute('SELECT * FROM job WHERE id = %s', (row['id'],)).fetchone()
    assert job['state'] == 'complete', job['result']
    return job['result']['diagnostics'], conn.execute('SELECT * FROM dataset').fetchone()


def observations(conn):
    return {o['relative_path']: o for o in conn.execute('SELECT * FROM observation').fetchall()}


def test_selection_manifest(setup, tmp_path):
    settings, conn, raw, selections = setup
    sub = raw / 'agent-search-test' / 'B001'
    sub.mkdir(parents=True)
    shutil.copy(RAW / WASP18, sub / WASP18)            # same content as the root file
    shutil.copy(RAW / OTHER, sub / OTHER)              # new content
    (raw / 'agent-search-test' / 'B002').mkdir()
    shutil.copy(RAW / THIRD, raw / 'agent-search-test' / 'B002' / THIRD)   # header TICID differs from the manifest
    changed = raw / 'agent-search-test' / 'B003'
    changed.mkdir()
    shutil.copy(RAW / OTHER, changed / OTHER)
    outside = tmp_path / 'outside'
    outside.mkdir()
    shutil.copy(RAW / THIRD, outside / THIRD)
    (raw / 'agent-search-test' / 'link').symlink_to(outside)

    files = [entry(raw, f'agent-search-test/B001/{WASP18}', 'TIC:100100827'),
             entry(raw, f'agent-search-test/B001/{OTHER}', 'TIC:172518755'),
             entry(raw, f'agent-search-test/B002/{THIRD}', 'TIC:1'),
             entry(raw, f'agent-search-test/B003/{OTHER}', 'TIC:172518755'),
             entry(raw, f'agent-search-test/link/{THIRD}', 'TIC:243244680'),
             {**entry(raw, f'agent-search-test/B001/{OTHER}', 'TIC:172518755'),
              'relative_path': 'agent-search-test/B009/gone_lc.fits'}]
    with (changed / OTHER).open('ab') as stream:   # changed after the manifest
        stream.write(b'\0' * 2880)
    listed = manifest(selections, 'research.json', files)
    manifest(selections, 'other-dataset.json', files[:1], dataset_id='another-dataset')
    (selections / 'broken.json').write_text('{"format": "tess-spoc-selection/1"}')

    diagnostics, dataset = index(settings, conn)
    rows = observations(conn)
    assert set(rows) == {WASP18, f'agent-search-test/B001/{OTHER}', f'agent-search-test/B002/{THIRD}'}
    assert rows[WASP18]['provenance'] == {} and rows[WASP18]['state'] == 'ready'
    selected = rows[f'agent-search-test/B001/{OTHER}']
    assert selected['state'] == 'ready' and selected['target_id'] == 'TIC:172518755'
    assert selected['provenance'] == {'selection': listed, 'owner': 'agent-search-test',
                                      'source_id': f'mast:TESS/product/{OTHER}', 'registration': 'test-registration'}
    mismatch = rows[f'agent-search-test/B002/{THIRD}']
    assert mismatch['state'] == 'invalid'
    assert [f['kind'] for f in mismatch['findings']] == ['selected_target_mismatch']
    kinds = sorted(f['kind'] for f in dataset['findings'] if f['kind'] != 'unlabeled')
    assert kinds == ['duplicate_content', 'invalid_selection', 'invalid_selection', 'selected_file_changed',
                     'selected_file_missing', 'selected_path_refused']
    duplicate = next(f for f in dataset['findings'] if f['kind'] == 'duplicate_content')
    assert duplicate['observation_id'] == str(rows[WASP18]['id'])
    assert dataset['state'] == 'incomplete'
    assert diagnostics['selections'] == [listed]
    assert diagnostics['selected'] == {'listed': 6, 'new': 2, 'unchanged': 0, 'duplicate': 1, 'refused': 3}
    light_curves = conn.execute("SELECT count(*) AS n FROM artifact WHERE kind = 'light_curve'").fetchone()['n']
    assert light_curves == 3

    # A repeated index adds nothing. The listed files stay as they are.
    diagnostics, _ = index(settings, conn)
    again = observations(conn)
    assert {k: (o['id'], o['state'], o['provenance']) for k, o in again.items()} == \
        {k: (o['id'], o['state'], o['provenance']) for k, o in rows.items()}
    assert diagnostics['new'] == 0 and diagnostics['missing'] == 0
    assert diagnostics['selected'] == {'listed': 6, 'new': 0, 'unchanged': 2, 'duplicate': 1, 'refused': 3}
    assert conn.execute("SELECT count(*) AS n FROM artifact WHERE kind = 'light_curve'").fetchone()['n'] == 3

    # Without the manifest, the listed files leave the inventory. The scan does not find them by itself.
    (selections / 'research.json').unlink()
    diagnostics, _ = index(settings, conn)
    states = {k: o['state'] for k, o in observations(conn).items()}
    assert states == {WASP18: 'ready', f'agent-search-test/B001/{OTHER}': 'missing',
                      f'agent-search-test/B002/{THIRD}': 'missing'}
    assert diagnostics['missing'] == 2


def test_no_recursive_scan(setup):
    settings, conn, raw, _ = setup
    (raw / 'agent-search-test').mkdir()
    shutil.copy(RAW / OTHER, raw / 'agent-search-test' / OTHER)
    diagnostics, dataset = index(settings, conn)
    assert set(observations(conn)) == {WASP18}
    assert diagnostics['selections'] == [] and diagnostics['selected']['listed'] == 0
    assert dataset['state'] == 'ready'


def test_selected_path_refuses_outside_paths(tmp_path):
    root = tmp_path / 'raw' / DATASET
    (root / 'agent').mkdir(parents=True)
    assert selected_path(root, 'agent/x_lc.fits') == (root / 'agent' / 'x_lc.fits', None)
    assert selected_path(root, '../outside/x_lc.fits')[1] == 'The path resolves outside the dataset directory.'
    (root / 'agent' / 'link').symlink_to(tmp_path)
    assert selected_path(root, 'agent/link/x_lc.fits')[1] == 'The path contains a symbolic link.'
