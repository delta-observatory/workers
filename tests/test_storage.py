import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from delta_worker import storage
from delta_worker.artifacts import ArtifactWriter


def test_workspace_limit_blocks_output_and_preserves_existing_file(tmp_path,monkeypatch):
    root=tmp_path/'workspace';root.mkdir();artifact=root/'artifacts';artifact.mkdir()
    config=root/'storage.json';config.write_text(json.dumps({'contract':'desktop-storage/1','reserve_bytes':536870912,'maximum_bytes':1073741824}))
    monkeypatch.setenv('DELTA_STORAGE_CONFIG',str(config))
    monkeypatch.setenv('DELTA_CONTRACTS_DIR',str(Path(__file__).parents[1]/'vendor/contracts'))
    # A sparse file reaches the configured logical limit without consuming that disk space.
    with (root/'existing').open('wb') as stream:stream.truncate(1073741824)
    writer=ArtifactWriter(artifact,'job')
    with pytest.raises(storage.StorageLimit,match='workspace storage limit'):
        writer.json('result.json',{'value':1},observation_id=None,kind='search_summary')
    assert not (artifact/'job/result.json').exists()
    assert (root/'existing').stat().st_size==1073741824


def test_free_space_and_invalid_settings_fail_closed(tmp_path,monkeypatch):
    monkeypatch.delenv('DELTA_STORAGE_CONFIG',raising=False)
    monkeypatch.setattr(storage.shutil,'disk_usage',lambda path:SimpleNamespace(free=536870912))
    with pytest.raises(storage.StorageLimit,match='reserve'):storage.check(tmp_path,1)
    config=tmp_path/'storage.json';config.write_text('{}');monkeypatch.setenv('DELTA_STORAGE_CONFIG',str(config))
    with pytest.raises(storage.StorageLimit,match='unavailable or invalid'):storage.check(tmp_path)
