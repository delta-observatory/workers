"""tess-spoc-index v1: index TESS SPOC light-curve files in raw/<dataset_id>/.

The method reads headers and checksums. It writes one light-curve artifact
for each new file content, with the original flux values. It makes no
scientific calculation. Labels come from the dataset label manifest
(format tess-spoc-labels/1) in datasets/<dataset_id>/.

The method scans only the dataset directory itself. Files in subdirectories
enter the inventory only through a selection manifest (format
tess-spoc-selection/1) in datasets/<dataset_id>/selections/. Each listed file
must have its listed size and SHA-256. The observation records the manifest,
owner, source identifier, and registration in its provenance.
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
SELECTION_FORMAT = 'tess-spoc-selection/1'
SELECTION_SCHEMA = 'inputs/tess-spoc-selection/v1.schema.json'
# Selection findings that make the dataset incomplete. A duplicate content is not a defect.
SELECTION_DEFECTS = {'invalid_selection', 'selected_path_refused', 'selected_file_missing', 'selected_file_changed'}
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


def load_selections(catalog_root, dataset_id, contracts_dir):
    """Return ([(manifest path relative to the catalog root, document)], findings) for the dataset."""
    from jsonschema import Draft202012Validator
    directory = Path(catalog_root, 'datasets', dataset_id, 'selections')
    if not directory.is_dir():
        return [], []
    validator = Draft202012Validator(json.loads(Path(contracts_dir, SELECTION_SCHEMA).read_text()))
    selections, findings = [], []
    for path in sorted(directory.glob('*.json')):
        relative = str(path.relative_to(catalog_root))
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            findings.append({'kind': 'invalid_selection', 'selection': relative, 'message': repr(error)})
            continue
        errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
        if errors:
            where = '/'.join(str(p) for p in errors[0].absolute_path) or '(root)'
            findings.append({'kind': 'invalid_selection', 'selection': relative,
                             'message': f'{where}: {errors[0].message}'})
        elif document['dataset_id'] != dataset_id:
            findings.append({'kind': 'invalid_selection', 'selection': relative,
                             'message': f"dataset_id is {document['dataset_id']!r}, expected {dataset_id!r}."})
        else:
            selections.append((relative, document))
    return selections, findings


def selected_path(root, relative):
    """The listed file, or the reason that the path is refused. No component may be a symbolic link."""
    path = root / relative
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            return None, 'The path contains a symbolic link.'
    if not path.resolve().is_relative_to(root.resolve()):
        return None, 'The path resolves outside the dataset directory.'
    return path, None


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
    new_by_sha = {}

    def add_new(path, relative, digest, label, entry=None):
        """Inspect a file with new content and queue its observation and light-curve artifact."""
        try:
            fields, findings, columns = inspect(path)
        except Exception as error:  # An unreadable file is an input finding, not a job failure.
            fields, findings, columns = None, [{'kind': 'unreadable', 'message': repr(error)}], None
        observation_id = str(uuid.uuid4())
        if fields is None:
            dataset_findings.append({'kind': 'invalid_file', 'path': relative, 'findings': findings})
            return None
        provenance = {}
        if entry is not None:
            provenance = entry['provenance']
            if fields['target_id'] != entry['target_id']:
                findings.append({'kind': 'selected_target_mismatch',
                                 'message': f"The header gives {fields['target_id']}. "
                                            f"The selection manifest gives {entry['target_id']}."})
        record = {'id': observation_id, 'relative_path': relative, 'bytes': path.stat().st_size,
                  'sha256': digest, **fields, 'labels': label, 'provenance': provenance,
                  'state': 'invalid' if findings else 'ready', 'findings': findings}
        new.append(record)
        new_by_sha[digest] = record
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
        return record

    for path in files:
        relative = path.name
        digest = sha256(path)
        label = labels.get(digest, {})
        if (relative, digest) in existing or digest in by_sha:
            row = existing.get((relative, digest)) or by_sha[digest]
            seen.append({'id': row['id'], 'relative_path': relative})
            label_updates[row['id']] = label
            continue
        add_new(path, relative, digest, label)

    # Files in subdirectories enter only through a reviewed selection manifest. The scan is not recursive.
    selections, selection_findings = load_selections(context.catalog_root, dataset_id, context.contracts_dir) \
        if root.is_dir() else ([], [])
    dataset_findings += selection_findings
    selected = {'listed': 0, 'new': 0, 'unchanged': 0, 'duplicate': 0, 'refused': 0}
    seen_ids = {s['id'] for s in seen}
    for manifest, document in selections:
        for item in document['files']:
            selected['listed'] += 1
            relative = item['relative_path']
            where = {'selection': manifest, 'path': relative}
            path, refusal = selected_path(root, relative)
            if path is None:
                dataset_findings.append({'kind': 'selected_path_refused', **where, 'message': refusal})
                selected['refused'] += 1
                continue
            if not path.is_file():
                dataset_findings.append({'kind': 'selected_file_missing', **where,
                                         'message': 'The listed file does not exist.'})
                selected['refused'] += 1
                continue
            size = path.stat().st_size
            digest = sha256(path) if size == item['bytes'] else None
            if digest != item['sha256']:
                dataset_findings.append({'kind': 'selected_file_changed', **where,
                                         'message': f"The file has {size} bytes and SHA-256 {digest or 'unchecked'}. "
                                                    f"The manifest gives {item['bytes']} bytes and {item['sha256']}."})
                selected['refused'] += 1
                continue
            provenance = {'selection': manifest, 'owner': document['owner'], 'source_id': item['source_id'],
                          'registration': document['registration']}
            row = by_sha.get(digest)
            if row is not None and row['relative_path'] == relative:
                if row['id'] not in seen_ids:
                    seen.append({'id': row['id'], 'relative_path': relative, 'provenance': provenance})
                    seen_ids.add(row['id'])
                    label_updates[row['id']] = labels.get(digest, {})
                selected['unchanged'] += 1
                continue
            if row is not None or digest in new_by_sha:
                other = row['id'] if row is not None else new_by_sha[digest]['id']
                dataset_findings.append({'kind': 'duplicate_content', **where, 'observation_id': other,
                                         'message': 'An indexed observation has the same content. '
                                                    'The file is not indexed again.'})
                selected['duplicate'] += 1
                continue
            if add_new(path, relative, digest, labels.get(digest, {}),
                       {'provenance': provenance, 'target_id': item['target_id']}) is not None:
                selected['new'] += 1
    present = {(s['relative_path']) for s in seen} | {r['relative_path'] for r in new}
    defects = sum(f['kind'] in SELECTION_DEFECTS for f in dataset_findings)
    missing = [o['id'] for o in context.existing if o['state'] != 'missing'
               and o['id'] not in {s['id'] for s in seen}]
    unlabeled = [r['relative_path'] for r in new if not r['labels']]
    if unlabeled:
        dataset_findings.append({'kind': 'unlabeled', 'paths': unlabeled,
                                 'message': 'No label manifest entry has this file checksum.'})
    ready = len(seen) + sum(r['state'] == 'ready' for r in new)
    invalid = sum(r['state'] == 'invalid' for r in new) + sum(f['kind'] == 'invalid_file' for f in dataset_findings)
    state = 'unavailable' if not files and not selected['listed'] \
        else 'incomplete' if invalid or (defects and ready) else 'ready' if ready else 'invalid'
    output.index = {'dataset_id': dataset_id, 'state': state, 'findings': dataset_findings,
                    'new': new, 'seen': seen, 'missing': missing, 'labels': label_updates}
    output.diagnostics = {'files': len(files), 'new': len(new), 'unchanged': len(seen), 'missing': len(missing),
                          'invalid': invalid, 'label_sources': label_sources, 'present': len(present),
                          'selections': [m for m, _ in selections], 'selected': selected,
                          'dataset_state': state}
    return output
