"""The worker must reproduce the closed T01 search results on the same files."""
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from delta_worker.artifacts import ArtifactWriter
from delta_worker.contracts import Contracts
from delta_worker.methods import MethodContext, tess_bls

WORKSPACE = Path(__file__).resolve().parents[2]
T01 = WORKSPACE / 'experiments' / 'tess-known-planet-recovery'
RAW = WORKSPACE / 'raw'
CONTRACTS = Contracts(Path(__file__).resolve().parents[1] / 'vendor' / 'contracts')
FILES = {100100827: 'tess2018234235059-s0002-0000000100100827-0121-s_lc.fits',
         25155310: 'tess2018234235059-s0002-0000000025155310-0121-s_lc.fits'}


def observation(tic):
    path = RAW / 'tess-spoc-light-curves' / FILES[tic]
    if not path.exists():
        pytest.skip(f'{path} is not available')
    reference = json.loads((T01 / 'reference' / 'selection.json').read_text())
    row = next(r['row'] for r in reference['records'] if r['row']['tic_id'] == f'TIC {tic}')
    return {'id': str(uuid.uuid4()), 'dataset_id': 'tess-spoc-light-curves', 'relative_path': path.name,
            'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'target_id': f'TIC:{tic}',
            'labels': {'planets': [{'name': row['pl_name'], 'period_days': row['pl_orbper'],
                                    'transit_midpoint_btjd_tdb': row['pl_tranmid'] - 2457000}]}}


@pytest.mark.parametrize('tic', sorted(FILES))
def test_reproduces_t01(tic, tmp_path):
    expected = json.loads((T01 / 'results' / str(tic) / 'search.json').read_text())
    comparison = json.loads((T01 / 'results' / str(tic) / 'comparison.json').read_text())
    obs = observation(tic)
    job_id = str(uuid.uuid4())
    context = MethodContext(job={'job_id': job_id}, parameters=CONTRACTS.defaults('tess-bls', 1),
                            inputs=[obs], raw_root=RAW, catalog_root=WORKSPACE,
                            artifacts=ArtifactWriter(tmp_path, f'jobs/{job_id}/1'))
    output = tess_bls.execute(context)
    values = {m['name']: m['value'] for m in output.measurements}
    for key in ['period', 'transit_time', 'duration', 'depth', 'depth_snr', 'log_likelihood']:
        assert values[key] == expected['best'][key], key
    assert output.diagnostics['grid_trials'] == expected['grid_trials']
    assert values['relative_period_error'] == pytest.approx(comparison['relative_period_error'], rel=1e-12)
    assert values['phase_error'] == pytest.approx(comparison['phase_error_days'], rel=1e-9)
    result = {'contract': 'result/1', 'job_id': job_id, 'outcome': 'complete',
              'measurements': output.measurements, 'artifacts': output.artifacts,
              'diagnostics': json.loads(json.dumps(output.diagnostics, default=float)),
              'software': {'worker_version': 'test', 'python': '3.12', 'packages': {}}}
    CONTRACTS.check_result(result)
    for artifact in output.artifacts:
        if artifact['media_type'].endswith('columns+json'):
            import gzip
            CONTRACTS.check('artifacts/columns/v1.schema.json',
                            json.loads(gzip.decompress((tmp_path / artifact['path']).read_bytes())))
