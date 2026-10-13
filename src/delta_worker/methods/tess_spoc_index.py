"""tess-spoc-index v1: index TESS SPOC light-curve files in raw/<dataset_id>/.

The method reads headers and checksums. It writes one light-curve artifact
for each new file content, with the original flux values. It makes no
scientific calculation. Labels come from the dataset label manifest
(format tess-spoc-labels/1) in datasets/<dataset_id>/.
"""
import hashlib
import json
from pathlib import Path
import uuid

from astropy.io import fits
import numpy as np

from . import MethodContext, MethodOutput
from ..fits_checks import ChecksumError, verify as verify_checksums

PATTERN = '*_lc.fits'
LABEL_FORMAT = 'tess-spoc-labels/1'
HEADER_KEYS = {
    0: ['OBJECT', 'TICID', 'SECTOR', 'CAMERA', 'CCD', 'RA_OBJ', 'DEC_OBJ', 'TESSMAG', 'TEFF', 'LOGG',
        'RADIUS', 'MH', 'PROCVER', 'DATA_REL', 'DATE'],
    1: ['TIMESYS', 'TIMEREF', 'TIMEUNIT', 'BJDREFI', 'BJDREFF', 'TSTART', 'TSTOP', 'TIMEDEL',
        'CROWDSAP', 'FLFRCSAP', 'PDCVAR', 'PDCMETHD', 'CDPP0_5', 'CDPP1_0', 'CDPP2_0'],
}


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def finite(value):
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    return str(value)


def load_labels(catalog_root, dataset_id):
    """Return {sha256: label entry} from every label manifest of the dataset."""
    labels, sources = {}, []
    for path in sorted(Path(catalog_root, 'datasets', dataset_id).rglob('manifest.json')):
        document = json.loads(path.read_text())
        if document.get('format') != LABEL_FORMAT:
            continue
        relative = str(path.relative_to(catalog_root))
        sources.append(relative)
        for entry in document['files']:
            labels[entry['sha256']] = {
                'class': entry.get('class'), 'planets': entry.get('planets', []),
                'eclipsing_binary': entry.get('eclipsing_binary'),
                'mast_product_id': entry.get('mast_product_id'), 'source': relative,
                'reference_snapshot': document.get('reference_snapshot')}
    return labels, sources


def inspect(path):
    """Read one file. Return (fields, findings, columns) or raise for an unreadable file."""
    findings = []
    try:
        checksums = verify_checksums(path)
    except ChecksumError as error:
        return None, [{'kind': 'fits_checksum', 'message': str(error)}], None
    with fits.open(path, checksum=False) as hdus:
        hdus.verify('exception')
        headers = {i: hdus[i].header for i in (0, 1)}
        data = hdus[1].data
        header = {key: finite(headers[i][key]) for i, keys in HEADER_KEYS.items()
                  for key in keys if key in headers[i]}
        columns = {name: np.asarray(data[name.upper()], dtype=float if name != 'quality' else int)
                   for name in ['time', 'pdcsap_flux', 'pdcsap_flux_err', 'sap_flux', 'sap_flux_err', 'quality']}
    header['fits_checksums'] = checksums
    for key, value in {'TIMESYS': 'TDB', 'TIMEREF': 'SOLARSYSTEM', 'TIMEUNIT': 'd'}.items():
        if header.get(key) != value:
            findings.append({'kind': 'time_convention', 'message': f'{key} is {header.get(key)!r}, expected {value!r}.'})
    bjdref = (header.get('BJDREFI') or 0) + (header.get('BJDREFF') or 0)
    if bjdref != 2457000:
        findings.append({'kind': 'time_convention', 'message': f'BJD reference is {bjdref}, expected 2457000.'})
    t = columns['time']
    valid_t = t[np.isfinite(t)]
    fields = {
        'target_id': f"TIC:{header['TICID']}", 'sector': header.get('SECTOR'),
        'cadence_seconds': header['TIMEDEL'] * 86400 if header.get('TIMEDEL') else None,
        'time_start': float(valid_t.min()) if len(valid_t) else None,
        'time_stop': float(valid_t.max()) if len(valid_t) else None,
        'time_scale': 'tdb' if header.get('TIMESYS') == 'TDB' else None,
        'time_reference': 'BJD 2457000.0' if bjdref == 2457000 else None,
        'row_count': len(t), 'header': header,
    }
    if fields['time_scale'] is None or fields['time_reference'] is None:
        fields['time_scale'] = fields['time_reference'] = None
    if not len(valid_t):
        findings.append({'kind': 'no_valid_time', 'message': 'The file has no finite TIME values.'})
    return fields, findings, columns


def execute(context: MethodContext) -> MethodOutput:
    dataset_id = context.parameters['dataset_id']
    root = Path(context.raw_root, dataset_id)
    labels, label_sources = load_labels(context.catalog_root, dataset_id)
    existing = {(o['relative_path'], o['sha256']): o for o in context.existing if o['state'] != 'missing'}
    by_sha = {o['sha256']: o for o in context.existing}
    output = MethodOutput()
    new, seen, label_updates, dataset_findings = [], [], {}, []
    files = sorted(root.glob(PATTERN)) if root.is_dir() else []
    if not root.is_dir():
        dataset_findings.append({'kind': 'missing_directory', 'message': f'raw/{dataset_id}/ does not exist.'})
    for path in files:
        relative = path.name
        digest = sha256(path)
        label = labels.get(digest, {})
        if (relative, digest) in existing or digest in by_sha:
            row = existing.get((relative, digest)) or by_sha[digest]
            seen.append({'id': row['id'], 'relative_path': relative})
            label_updates[row['id']] = label
            continue
        try:
            fields, findings, columns = inspect(path)
        except Exception as error:  # An unreadable file is an input finding, not a job failure.
            fields, findings, columns = None, [{'kind': 'unreadable', 'message': repr(error)}], None
        observation_id = str(uuid.uuid4())
        if fields is None:
            dataset_findings.append({'kind': 'invalid_file', 'path': relative, 'findings': findings})
            continue
        record = {'id': observation_id, 'relative_path': relative, 'bytes': path.stat().st_size,
                  'sha256': digest, **fields, 'labels': label,
                  'state': 'invalid' if findings else 'ready', 'findings': findings}
        new.append(record)
        output.artifacts.append(context.artifacts.columns(f'light-curve-{observation_id}.json.gz', [
            {'name': 'time', 'unit': 'd', 'time_scale': 'tdb', 'time_reference': 'BJD 2457000.0',
             'values': columns['time']},
            {'name': 'pdcsap_flux', 'unit': 'electron / s', 'values': columns['pdcsap_flux']},
            {'name': 'pdcsap_flux_err', 'unit': 'electron / s', 'values': columns['pdcsap_flux_err']},
            {'name': 'sap_flux', 'unit': 'electron / s', 'values': columns['sap_flux']},
            {'name': 'sap_flux_err', 'unit': 'electron / s', 'values': columns['sap_flux_err']},
            {'name': 'quality', 'unit': '1', 'values': columns['quality'],
             'description': 'SPOC QUALITY bit flags. 0 means no flag.'},
        ], observation_id=observation_id, kind='light_curve',
            description='Original SPOC light-curve columns, all rows.'))
    present = {(s['relative_path']) for s in seen} | {r['relative_path'] for r in new}
    missing = [o['id'] for o in context.existing if o['state'] != 'missing'
               and o['id'] not in {s['id'] for s in seen}]
    unlabeled = [r['relative_path'] for r in new if not r['labels']]
    if unlabeled:
        dataset_findings.append({'kind': 'unlabeled', 'paths': unlabeled,
                                 'message': 'No label manifest entry has this file checksum.'})
    ready = len(seen) + sum(r['state'] == 'ready' for r in new)
    invalid = sum(r['state'] == 'invalid' for r in new) + sum(f['kind'] == 'invalid_file' for f in dataset_findings)
    state = 'unavailable' if not files else 'incomplete' if invalid else 'ready' if ready else 'invalid'
    output.index = {'dataset_id': dataset_id, 'state': state, 'findings': dataset_findings,
                    'new': new, 'seen': seen, 'missing': missing, 'labels': label_updates}
    output.diagnostics = {'files': len(files), 'new': len(new), 'unchanged': len(seen), 'missing': len(missing),
                          'invalid': invalid, 'label_sources': label_sources, 'present': len(present),
                          'dataset_state': state}
    return output
