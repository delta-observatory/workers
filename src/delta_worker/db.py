"""PostgreSQL access for the worker: queue claims, leases, inputs, and results."""
import hashlib
import json
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

INDEX_METHOD = ('tess-spoc-index', 1)
INDEX_LIMITS = {'maximum_seconds': 1800, 'maximum_memory_mib': 1536}


def connect(url):
    """Autocommit connection. Each transaction() block is one real transaction,
    and a plain read does not leave a transaction open while a job runs."""
    return psycopg.connect(url, row_factory=dict_row, autocommit=True)


def parameters_sha256(parameters):
    return hashlib.sha256(json.dumps(parameters, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def ensure_index_job(conn, contracts, dataset_id, requested_by):
    """Queue an index job for the dataset unless one waits or runs. Return the job ID or None."""
    parameters = {'dataset_id': dataset_id}
    with conn.transaction():
        conn.execute('INSERT INTO dataset (id) VALUES (%s) ON CONFLICT (id) DO NOTHING', (dataset_id,))
        active = conn.execute(
            """SELECT 1 FROM job WHERE state IN ('queued', 'running')
               AND document -> 'method' ->> 'id' = %s AND document -> 'parameters' ->> 'dataset_id' = %s""",
            (INDEX_METHOD[0], dataset_id)).fetchone()
        if active:
            return None
        run_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
        document = {'contract': 'job/1', 'job_id': job_id, 'run_id': run_id,
                    'method': {'id': INDEX_METHOD[0], 'version': INDEX_METHOD[1]},
                    'parameters': parameters, 'inputs': [], 'limits': INDEX_LIMITS}
        contracts.check_job(document)
        conn.execute(
            """INSERT INTO run (id, method_id, method_version, parameters, parameters_sha256, label, requested_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (run_id, *INDEX_METHOD, Jsonb(parameters), parameters_sha256(parameters),
             f'Index {dataset_id}', requested_by))
        inserted = conn.execute(
            'INSERT INTO job (id, run_id, document) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING id',
            (job_id, run_id, Jsonb(document))).fetchone()
        if not inserted:
            raise psycopg.errors.UniqueViolation('index job already active')
        conn.execute("UPDATE dataset SET state = 'indexing' WHERE id = %s", (dataset_id,))
        return job_id


def expire_exhausted(conn, software):
    """Fail running jobs whose lease expired after their last permitted attempt."""
    with conn.transaction():
        rows = conn.execute(
            """SELECT id, attempts FROM job WHERE state = 'running' AND lease_expires_at < now()
               AND attempts >= max_attempts FOR UPDATE SKIP LOCKED""").fetchall()
        for row in rows:
            result = {'contract': 'result/1', 'job_id': str(row['id']), 'outcome': 'failed',
                      'measurements': [], 'artifacts': [], 'diagnostics': {'attempts': row['attempts']},
                      'error': {'kind': 'lease_expired',
                                'message': 'The worker stopped without a result on every permitted attempt.'},
                      'software': software}
            conn.execute(
                """UPDATE job SET state = 'failed', result = %s, finished_at = now(),
                   lease_expires_at = NULL WHERE id = %s""", (Jsonb(result), row['id']))
    return len(rows)


def claim(conn, worker_id, lease_seconds):
    """Claim the oldest available job, or a job with an expired lease. Return the row or None."""
    with conn.transaction():
        return conn.execute(
            """UPDATE job SET state = 'running', attempts = attempts + 1, claimed_by = %(worker)s,
                   started_at = now(), lease_expires_at = now() + make_interval(secs => %(lease)s)
               WHERE id = (
                   SELECT id FROM job
                   WHERE (state = 'queued' AND available_at <= now())
                      OR (state = 'running' AND lease_expires_at < now() AND attempts < max_attempts)
                   ORDER BY created_at
                   FOR UPDATE SKIP LOCKED
                   LIMIT 1)
               RETURNING *""", {'worker': worker_id, 'lease': lease_seconds}).fetchone()


def extend_lease(conn, job, worker_id, lease_seconds):
    """Return False when another worker took the job or it left the running state."""
    with conn.transaction():
        row = conn.execute(
            """UPDATE job SET lease_expires_at = now() + make_interval(secs => %s)
               WHERE id = %s AND claimed_by = %s AND attempts = %s AND state = 'running' RETURNING id""",
            (lease_seconds, job['id'], worker_id, job['attempts'])).fetchone()
    return row is not None


def release(conn, job, worker_id):
    """Return a claimed job to the queue, for example at shutdown."""
    with conn.transaction():
        conn.execute(
            """UPDATE job SET state = 'queued', claimed_by = NULL, lease_expires_at = NULL,
                   attempts = GREATEST(attempts - 1, 0)
               WHERE id = %s AND claimed_by = %s AND attempts = %s AND state = 'running'""",
            (job['id'], worker_id, job['attempts']))


def observations(conn, ids):
    rows = conn.execute('SELECT * FROM observation WHERE id = ANY(%s)', (list(ids),)).fetchall()
    found = {str(r['id']): r for r in rows}
    return [plain(found[str(i)]) for i in ids if str(i) in found]


def dataset_observations(conn, dataset_id):
    rows = conn.execute(
        'SELECT id, relative_path, sha256, state, provenance FROM observation WHERE dataset_id = %s',
        (dataset_id,)).fetchall()
    return [plain(r) for r in rows]


def plain(row):
    return {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in row.items()}


def _apply_index(conn, index):
    dataset_id = index['dataset_id']
    for record in index['new']:
        conn.execute(
            """INSERT INTO observation (id, dataset_id, product_type, relative_path, bytes, sha256, target_id,
                   sector, cadence_seconds, time_start, time_stop, time_scale, time_reference, row_count,
                   header, labels, state, findings, provenance)
               VALUES (%(id)s, %(dataset_id)s, 'tess_spoc_light_curve', %(relative_path)s, %(bytes)s, %(sha256)s,
                   %(target_id)s, %(sector)s, %(cadence_seconds)s, %(time_start)s, %(time_stop)s, %(time_scale)s,
                   %(time_reference)s, %(row_count)s, %(header)s, %(labels)s, %(state)s, %(findings)s,
                   %(provenance)s)""",
            {**record, 'dataset_id': dataset_id, 'header': Jsonb(record['header']),
             'labels': Jsonb(record['labels']), 'findings': Jsonb(record['findings']),
             'provenance': Jsonb(record.get('provenance', {}))})
    if index['missing']:
        conn.execute("UPDATE observation SET state = 'missing' WHERE id = ANY(%s)", (index['missing'],))
    for seen in index['seen']:
        conn.execute(
            """UPDATE observation SET last_seen_at = now(), relative_path = %s, labels = %s,
                   state = CASE WHEN state = 'missing' THEN (CASE WHEN findings = '[]'::jsonb
                                THEN 'ready' ELSE 'invalid' END) ELSE state END
               WHERE id = %s""",
            (seen['relative_path'], Jsonb(index['labels'].get(seen['id'], {})), seen['id']))
        if 'provenance' in seen:
            # A listed file of a selection manifest. Root files keep their provenance.
            conn.execute('UPDATE observation SET provenance = %s WHERE id = %s',
                         (Jsonb(seen['provenance']), seen['id']))
    conn.execute(
        'UPDATE dataset SET state = %s, findings = %s, last_indexed_at = now() WHERE id = %s',
        (index['state'], Jsonb(index['findings']), dataset_id))


def state(conn, job_id):
    row = conn.execute('SELECT state FROM job WHERE id = %s', (job_id,)).fetchone()
    return row and row['state']


def finish(conn, job, worker_id, result, index=None):
    """Write measurements, artifacts, index changes, and the result in one transaction.
    Return False when the lease was lost, and write nothing."""
    with conn.transaction():
        owned = conn.execute(
            """SELECT id FROM job WHERE id = %s AND claimed_by = %s AND attempts = %s AND state = 'running'
               FOR UPDATE""", (job['id'], worker_id, job['attempts'])).fetchone()
        if not owned:
            return False
        if index is not None and result['outcome'] == 'complete':
            _apply_index(conn, index)
        elif index is not None:
            conn.execute("UPDATE dataset SET state = 'invalid', findings = %s WHERE id = %s",
                         (Jsonb([{'kind': 'index_failed', 'message': result['error']['message']}]),
                          index['dataset_id']))
        for m in result['measurements']:
            value = m['value']
            u = m['uncertainty']
            conn.execute(
                """INSERT INTO measurement (job_id, observation_id, role, name, value_number, value_boolean, unit,
                       time_scale, time_reference, uncertainty_kind, uncertainty_value, uncertainty_reason,
                       quality_flags)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (job['id'], m['observation_id'], m['role'], m['name'],
                 None if isinstance(value, bool) else value, value if isinstance(value, bool) else None,
                 m['unit'], m.get('time_scale'), m.get('time_reference'), u['kind'], u.get('value'),
                 u.get('reason'), m['quality_flags']))
        for a in result['artifacts']:
            conn.execute(
                """INSERT INTO artifact (job_id, observation_id, kind, media_type, content_encoding, path, bytes,
                       sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (job['id'], a['observation_id'], a['kind'], a['media_type'], a['content_encoding'], a['path'],
                 a['bytes'], a['sha256']))
        conn.execute(
            """UPDATE job SET state = %s, result = %s, finished_at = now(), lease_expires_at = NULL
               WHERE id = %s""", (result['outcome'], Jsonb(result), job['id']))
    return True
