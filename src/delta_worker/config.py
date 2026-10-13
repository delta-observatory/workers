"""Worker settings from environment variables."""
from dataclasses import dataclass
import os
from pathlib import Path
import socket


@dataclass(frozen=True)
class Settings:
    database_url: str
    raw_root: Path
    artifact_root: Path
    catalog_root: Path
    contracts_dir: Path
    worker_id: str
    worker_version: str
    datasets: tuple[str, ...]
    lease_seconds: int
    poll_seconds: float


def load():
    here = Path(__file__).resolve().parents[2]
    return Settings(
        database_url=os.environ['DATABASE_URL'],
        raw_root=Path(os.environ.get('DELTA_RAW_ROOT', '/data/raw')),
        artifact_root=Path(os.environ.get('DELTA_ARTIFACT_ROOT', '/data/artifacts')),
        catalog_root=Path(os.environ.get('DELTA_CATALOG_ROOT', '/data/catalog')),
        contracts_dir=Path(os.environ.get('DELTA_CONTRACTS_DIR', here / 'vendor' / 'contracts')),
        worker_id=os.environ.get('DELTA_WORKER_ID', socket.gethostname()),
        worker_version=os.environ.get('DELTA_WORKER_VERSION', 'development'),
        datasets=tuple(d for d in os.environ.get('DELTA_DATASETS', 'tess-spoc-light-curves').split(',') if d),
        lease_seconds=int(os.environ.get('DELTA_LEASE_SECONDS', '60')),
        poll_seconds=float(os.environ.get('DELTA_POLL_SECONDS', '30')),
    )
