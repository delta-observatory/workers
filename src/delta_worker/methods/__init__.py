"""Method registry. Each method module defines execute(context) -> MethodOutput."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..artifacts import ArtifactWriter


class Inconclusive(Exception):
    """An input or quality condition prevents a usable result. No retry."""

    def __init__(self, kind, message, diagnostics=None):
        super().__init__(message)
        self.kind = kind
        self.diagnostics = diagnostics or {}


@dataclass
class MethodContext:
    job: dict
    parameters: dict
    inputs: list[dict]
    raw_root: Path
    catalog_root: Path
    artifacts: ArtifactWriter
    existing: list[dict] = field(default_factory=list)
    # Vendored contract schemas, for the input manifests that a method reads.
    contracts_dir: Path | None = None


@dataclass
class MethodOutput:
    measurements: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # Index methods only: observation changes and the dataset state.
    index: dict[str, Any] | None = None


def get(method_id):
    if method_id == 'tess-bls':
        from . import tess_bls
        return tess_bls.execute
    if method_id == 'tess-spoc-index':
        from . import tess_spoc_index
        return tess_spoc_index.execute
    raise KeyError(method_id)
