"""Write artifact files under the artifact root and describe them for result/1."""
import gzip
import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np
from . import storage

COLUMNS = 'application/vnd.delta.columns+json'


def clean(value):
    """Convert numpy values to JSON values. Nonfinite floats become null."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [clean(v) for v in value]
    if isinstance(value, np.generic):
        return clean(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class ArtifactWriter:
    """Writes files to <root>/<prefix>/ and returns result/1 artifact entries."""

    def __init__(self, root: Path, prefix: str):
        self.root = root
        self.prefix = prefix.strip('/')
        (root / self.prefix).mkdir(parents=True, exist_ok=True)

    def _store(self, name, payload: bytes, observation_id, kind, media_type, encoding):
        storage.check(self.root, len(payload))
        relative = f'{self.prefix}/{name}'
        target = self.root / relative
        partial = target.with_name(target.name + '.part')
        partial.write_bytes(payload)
        os.replace(partial, target)
        return {'observation_id': observation_id, 'kind': kind, 'media_type': media_type,
                'content_encoding': encoding, 'path': relative, 'bytes': len(payload),
                'sha256': hashlib.sha256(payload).hexdigest()}

    def json(self, name, value, *, observation_id, kind):
        payload = (json.dumps(clean(value), indent=1, allow_nan=False) + '\n').encode()
        return self._store(name, payload, observation_id, kind, 'application/json', 'identity')

    def columns(self, name, columns, *, observation_id, kind, description=None):
        """columns: list of dicts with name, unit, values, and optional time_scale,
        time_reference, description. dtype comes from the values."""
        lengths = {len(c['values']) for c in columns}
        if len(lengths) != 1:
            raise ValueError(f'column lengths differ: {lengths}')
        encoded = []
        for column in columns:
            values = np.asarray(column['values'])
            dtype = 'bool' if values.dtype == bool else 'int64' if values.dtype.kind in 'iu' else 'float64'
            entry = {k: v for k, v in column.items() if k != 'values'}
            entry['dtype'] = dtype
            entry['values'] = clean(values)
            encoded.append(entry)
        document = {'format': 'columns/1', 'length': lengths.pop(), 'columns': encoded}
        if description:
            document['description'] = description
        raw = json.dumps(document, separators=(',', ':'), allow_nan=False).encode()
        payload = gzip.compress(raw, compresslevel=6, mtime=0)
        return self._store(name, payload, observation_id, kind, COLUMNS, 'gzip')
