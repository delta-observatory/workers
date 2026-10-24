"""Bounded public MAST acquisition on the existing worker.

A PostgreSQL session lock prevents two workers from handling the same transfer.
Completed files are immutable. A repeated attempt checks them before reuse.
An incomplete file is restarted. No partial file reaches the observation index.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import urllib.parse
import urllib.request

from psycopg.types.json import Jsonb
from . import db
from .fits_checks import verify


class Cancelled(Exception):
    pass


def canonical_sha(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def query(request):
    from astroquery.mast import Observations
    Observations.TIMEOUT = 45
    selected = []
    for sector in request['sectors']:
        observations = Observations.query_criteria(obs_collection='TESS', target_name=str(request['tic_id']),
                                                   provenance_name='SPOC', sequence_number=sector, t_exptime=[119, 121],
                                                   dataproduct_type='timeseries', dataRights='PUBLIC')
        if len(observations) > 64:
            raise ValueError('The archive query returned too many observations. Select fewer sectors.')
        if not len(observations):
            continue
        products = Observations.get_product_list(observations)
        for product in products:
            filename = str(product['productFilename'])
            if str(product['productSubGroupDescription']) != 'LC' or not filename.endswith('_lc.fits'):
                continue
            selected.append({'uri': str(product['dataURI']), 'filename': filename, 'bytes': int(product['size']),
                             'observation_id': str(product['obsID']), 'sector': sector, 'tic_id': request['tic_id'],
                             'processing_version': str(product['description'])[:200]})
    products = list({p['uri']: p for p in selected}.values())
    products.sort(key=lambda p: (p['sector'], p['filename']))
    if not products:
        raise ValueError('No compatible public SPOC 120-second light curve matches this selection.')
    if len(products) > request['maximum_files'] or sum(p['bytes'] for p in products) > request['maximum_bytes']:
        raise ValueError('The archive selection exceeds the file or byte limit. Reduce the sectors or increase the limit.')
    return {'contract': 'acquisition-manifest/1', 'source': 'MAST', 'dataset_id': 'tess-spoc-light-curves',
            'request': request, 'products': products}


def check_product(path, product):
    """Require archive length, all HDU checksums, target, sector, and 120-second cadence."""
    from astropy.io import fits
    if path.is_symlink() or not path.is_file() or path.stat().st_size != product['bytes']:
        raise ValueError('The file size differs from the approved archive product.')
    verify(path)
    with fits.open(path, memmap=False) as hdus:
        if int(hdus[0].header.get('TICID', -1)) != product['tic_id'] or int(hdus[0].header.get('SECTOR', -1)) != product['sector']:
            raise ValueError('The file target or sector differs from the approved product.')
        if abs(float(hdus[1].header.get('TIMEDEL', 0)) * 86400 - 120) > 0.1:
            raise ValueError('The acquired product does not have the supported 120-second cadence.')
        if not {'TIME', 'PDCSAP_FLUX', 'PDCSAP_FLUX_ERR', 'QUALITY'} <= set(hdus[1].columns.names):
            raise ValueError('The acquired product lacks required light curve columns.')
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def download(product, root, reserve, check_active, opener=urllib.request.urlopen, checker=check_product):
    """Read at most the exact approved bytes, then validate before atomic installation."""
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError('The acquisition directory cannot be a symbolic link.')
    name = product['filename']
    if Path(name).name != name or not name.endswith('_lc.fits'):
        raise ValueError('The approved filename is invalid.')
    target = root / name
    if target.exists() or target.is_symlink():
        return checker(target, product)
    temporary = root / ('.' + name + '.partial')
    if temporary.is_symlink():
        raise ValueError('The partial download cannot be a symbolic link.')
    if shutil.disk_usage(root).free - product['bytes'] < reserve:
        raise ValueError('The download would exceed the free-space reserve.')
    url = 'https://mast.stsci.edu/api/v0.1/Download/file?' + urllib.parse.urlencode({'uri': product['uri']})
    deadline, size = time.monotonic() + 600, 0
    try:
        with opener(url, timeout=30) as source, temporary.open('wb') as out:
            while True:
                check_active()
                if time.monotonic() > deadline:
                    raise TimeoutError('The file transfer exceeded 600 seconds.')
                if shutil.disk_usage(root).free < reserve:
                    raise ValueError('The available storage reached the free-space reserve.')
                chunk = source.read(min(1024 * 1024, product['bytes'] - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > product['bytes']:
                    raise ValueError('The archive sent more bytes than the approved product size.')
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        digest = checker(temporary, product)
        try:
            os.link(temporary, target)
        except FileExistsError:
            if checker(target, product) != digest:
                raise ValueError('A different file already uses this archive filename.')
        return digest
    finally:
        temporary.unlink(missing_ok=True)


def process(conn, row, contracts, settings, query_fn=query, download_fn=download):
    aid = row['id']
    def active():
        state = conn.execute('SELECT state FROM acquisition WHERE id=%s', [aid]).fetchone()['state']
        if state == 'cancelled':
            raise Cancelled('The owner cancelled this acquisition.')
    files = []
    try:
        if row['state'] == 'querying':
            manifest = query_fn(row['request'])
            contracts.check('records/acquisition-manifest/v1.schema.json', manifest)
            active()
            conn.execute("UPDATE acquisition SET state='preview',manifest=%s,manifest_sha256=%s,updated_at=now() "
                         "WHERE id=%s AND state='querying'", [Jsonb(manifest), canonical_sha(manifest), aid])
            return
        manifest = row['manifest']
        contracts.check('records/acquisition-manifest/v1.schema.json', manifest)
        if not row['approved_at'] or canonical_sha(manifest) != row['manifest_sha256']:
            raise ValueError('This acquisition has no valid manifest approval.')
        request = manifest['request']
        if sum(p['bytes'] for p in manifest['products']) > request['maximum_bytes'] or len(manifest['products']) > request['maximum_files']:
            raise ValueError('The approved manifest exceeds its limits.')
        conn.execute("UPDATE acquisition SET state='downloading',updated_at=now() WHERE id=%s AND state IN ('queued','downloading')", [aid])
        active()
        root = settings.raw_root / manifest['dataset_id']
        for product in manifest['products']:
            active()
            digest = download_fn(product, root, request['reserve_bytes'], active)
            files.append({'filename': product['filename'], 'bytes': product['bytes'], 'sha256': digest, 'state': 'complete', 'message': ''})
        active()
        result = {'contract': 'acquisition-result/1', 'files': files, 'outcome': 'complete', 'message': 'The acquired files passed integrity checks.'}
        contracts.check('records/acquisition-result/v1.schema.json', result)
        with conn.transaction():
            changed = conn.execute("UPDATE acquisition SET state='complete',result=%s,error=NULL,updated_at=now() WHERE id=%s "
                                   "AND state='downloading' RETURNING id", [Jsonb(result), aid]).fetchone()
            if changed:
                db.ensure_index_job(conn, contracts, manifest['dataset_id'], f'acquisition:{aid}')
    except (Exception, KeyboardInterrupt) as error:
        cancelled = isinstance(error, (Cancelled, KeyboardInterrupt))
        message = 'The acquisition was interrupted.' if cancelled else str(error)[:1000]
        result = {'contract': 'acquisition-result/1', 'files': files, 'outcome': 'cancelled' if cancelled else 'failed', 'message': message}
        contracts.check('records/acquisition-result/v1.schema.json', result)
        conn.execute("UPDATE acquisition SET state=CASE WHEN state='cancelled' THEN state ELSE %s END,result=%s,error=%s,updated_at=now() "
                     'WHERE id=%s', [result['outcome'], Jsonb(result), message, aid])
        if isinstance(error, KeyboardInterrupt):
            raise


def next_request(conn, contracts, settings):
    rows = conn.execute("SELECT id FROM acquisition WHERE state IN ('querying','queued','downloading') ORDER BY created_at LIMIT 32").fetchall()
    for candidate in rows:
        key = str(candidate['id'])
        if not conn.execute('SELECT pg_try_advisory_lock(hashtext(%s)) AS locked', [key]).fetchone()['locked']:
            continue
        try:
            row = conn.execute('SELECT * FROM acquisition WHERE id=%s', [candidate['id']]).fetchone()
            if row['state'] in ('querying', 'queued', 'downloading'):
                process(conn, row, contracts, settings)
                return True
        finally:
            conn.execute('SELECT pg_advisory_unlock(hashtext(%s))', [key])
    return False
