"""Worker main loop: queue index jobs at startup, then claim and execute jobs."""
import logging
import select
import signal
import sys

from . import config, db, runner, acquisition
from .contracts import ContractError, Contracts

log = logging.getLogger('delta_worker')


class Stop(BaseException):
    pass


def build_result(job, record, resources, software):
    result = {'contract': 'result/1', 'job_id': job['job_id'], 'outcome': record['outcome'],
              'measurements': record.get('measurements', []), 'artifacts': record.get('artifacts', []),
              'diagnostics': record.get('diagnostics', {}), 'resources': resources, 'software': software}
    if record.get('error'):
        result['error'] = record['error']
    return result


def process(conn, contracts, settings, row):
    job = dict(row['document'])
    software = runner.software(settings.worker_version)
    try:
        contracts.check_job(job)
    except ContractError as error:
        record, resources = {'outcome': 'failed', 'error': {'kind': 'contract', 'message': str(error)}}, {}
    else:
        inputs = db.observations(conn, [i['observation_id'] for i in job['inputs']])
        existing = db.dataset_observations(conn, job['parameters']['dataset_id']) \
            if job['method']['id'] == 'tess-spoc-index' else []
        if len(inputs) != len(job['inputs']):
            record, resources = {'outcome': 'failed', 'error': {'kind': 'missing_input',
                                                                'message': 'An input observation does not exist.'}}, {}
        else:
            job['attempt'] = row['attempts']
            record, resources = runner.run(
                job, inputs, existing, settings,
                keep_lease=lambda: db.extend_lease(conn, row, settings.worker_id, settings.lease_seconds))
    result = build_result(job, record, resources, software)
    try:
        contracts.check_result(result)
    except ContractError as error:
        log.error('job %s produced an invalid result: %s', job['job_id'], error)
        result = build_result(job, {'outcome': 'failed', 'diagnostics': {},
                                    'error': {'kind': 'contract', 'message': str(error)}}, resources, software)
    index = record.get('index')
    if job['method']['id'] == 'tess-spoc-index' and index is None:
        index = {'dataset_id': job['parameters']['dataset_id']}
    if db.finish(conn, row, settings.worker_id, result, index):
        log.info('job %s %s %s in %.1f s', job['job_id'], job['method']['id'], result['outcome'],
                 resources.get('elapsed_seconds', 0))
    elif db.state(conn, row['id']) == 'cancelled':
        # The API cancelled the running job. The files of the attempt stay without artifact rows.
        log.info('job %s cancelled; output discarded', job['job_id'])
    else:
        log.warning('job %s lease lost; result discarded', job['job_id'])


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    settings = config.load()
    contracts = Contracts(settings.contracts_dir)
    conn = db.connect(settings.database_url)
    listener = db.connect(settings.database_url)
    listener.execute('LISTEN delta_jobs')

    def on_term(signum, frame):
        raise Stop()
    signal.signal(signal.SIGTERM, on_term)

    for dataset_id in settings.datasets:
        if db.ensure_index_job(conn, contracts, dataset_id, f'worker:{settings.worker_id}'):
            log.info('queued index job for %s', dataset_id)
    log.info('worker %s %s ready', settings.worker_id, settings.worker_version)
    current = None
    try:
        while True:
            db.expire_exhausted(conn, runner.software(settings.worker_version))
            current = db.claim(conn, settings.worker_id, settings.lease_seconds)
            if current:
                process(conn, contracts, settings, current)
                current = None
                continue
            if acquisition.next_request(conn, contracts, settings):
                continue
            select.select([listener.fileno()], [], [], settings.poll_seconds)
            for _ in listener.notifies(timeout=0):
                pass
    except (Stop, KeyboardInterrupt):
        log.info('stopping')
        if current:
            db.release(conn, current, settings.worker_id)
    return 0


if __name__ == '__main__':
    sys.exit(main())
