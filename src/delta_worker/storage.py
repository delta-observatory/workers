"""Enforce desktop storage settings at worker write boundaries."""
import json
import os
from pathlib import Path
import shutil
from jsonschema import Draft202012Validator

class StorageLimit(ValueError):
    pass


def _check(path,additional=0):
    path=Path(path)
    while not path.exists():path=path.parent
    configuration=os.environ.get('DELTA_STORAGE_CONFIG')
    reserve=512*1024**2
    if configuration:
        config_path=Path(configuration)
        document=json.loads(config_path.read_text())
        contracts=Path(os.environ.get('DELTA_CONTRACTS_DIR',Path(__file__).resolve().parents[2]/'vendor/contracts'))
        schema=json.loads((contracts/'records/desktop-storage/v1.schema.json').read_text())
        Draft202012Validator(schema).validate(document)
        reserve=document['reserve_bytes'];root=config_path.parent
        used=sum(p.stat().st_size for p in root.rglob('*') if p.is_file() and not p.is_symlink())
        if used+additional>document['maximum_bytes']:raise StorageLimit('The operation exceeds the workspace storage limit.')
    if shutil.disk_usage(path).free-additional<reserve:raise StorageLimit('The operation exceeds the free-space reserve.')


def check(path,additional=0):
    try:_check(path,additional)
    except StorageLimit:raise
    except Exception as error:raise StorageLimit("The storage settings are unavailable or invalid.") from error
