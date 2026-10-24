"""Transfer limits and recovery use a controlled byte source."""
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from psycopg.types.json import Jsonb
from delta_worker import acquisition, db
from delta_worker.contracts import Contracts

ROOT = Path(__file__).parents[1] / 'vendor/contracts'

def product(size=12):
    return {'filename':'tess-test_lc.fits','uri':'mast:TESS/product/tess-test_lc.fits','bytes':size}

def check(path, selected):
    assert path.stat().st_size == selected['bytes']
    return hashlib.sha256(path.read_bytes()).hexdigest()

def test_restart_atomic_download_and_no_duplicate(tmp_path):
    calls=[]
    def source(*args,**kwargs):
        calls.append(1)
        return io.BytesIO(b'123456789012')
    target=tmp_path/'raw'
    target.mkdir()
    (target/'.tess-test_lc.fits.partial').write_bytes(b'123')
    first=acquisition.download(product(),target,0,lambda:None,source,check)
    second=acquisition.download(product(),target,0,lambda:None,source,check)
    assert first==second and len(calls)==1
    assert list(target.iterdir())==[target/'tess-test_lc.fits']

def test_oversize_cancellation_and_reserve_preserve_destination(tmp_path):
    with pytest.raises(ValueError,match='more bytes'):
        acquisition.download(product(3),tmp_path,0,lambda:None,lambda *a,**k:io.BytesIO(b'1234'),check)
    assert not list(tmp_path.iterdir())
    def cancel():raise acquisition.Cancelled('cancel')
    with pytest.raises(acquisition.Cancelled):
        acquisition.download(product(),tmp_path,0,cancel,lambda *a,**k:io.BytesIO(b'1234'),check)
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError,match='reserve'):
        acquisition.download(product(),tmp_path,2**63,lambda:None,lambda *a,**k:io.BytesIO(b'1'),check)

def test_query_approval_download_and_index_queue(database_url,tmp_path):
    contracts=Contracts(ROOT)
    with db.connect(database_url) as conn:
        person=conn.execute("INSERT INTO client(name,kind,permissions,token_sha256,created_by) VALUES('archive-owner','person','{acquire}',repeat('e',64),'test') RETURNING id").fetchone()['id']
        request=json.loads((ROOT/'examples/records/acquisition-request-bounded.valid.json').read_text())
        manifest=json.loads((ROOT/'examples/records/acquisition-manifest-bounded.valid.json').read_text())
        row=conn.execute('INSERT INTO acquisition(request,created_by) VALUES(%s,%s) RETURNING *',[Jsonb(request),person]).fetchone()
        settings=SimpleNamespace(raw_root=tmp_path)
        acquisition.process(conn,row,contracts,settings,query_fn=lambda request:manifest)
        row=conn.execute('SELECT * FROM acquisition WHERE id=%s',[row['id']]).fetchone()
        assert row['state']=='preview' and row['approved_at'] is None
        row=conn.execute("UPDATE acquisition SET state='queued',approved_by=%s,approved_at=now() WHERE id=%s RETURNING *",[person,row['id']]).fetchone()
        acquisition.process(conn,row,contracts,settings,download_fn=lambda *args:'a'*64)
        done=conn.execute('SELECT * FROM acquisition WHERE id=%s',[row['id']]).fetchone()
        assert done['state']=='complete' and done['result']['files'][0]['sha256']=='a'*64
        assert conn.execute("SELECT count(*) n FROM job WHERE document->'method'->>'id'='tess-spoc-index'").fetchone()['n']>=1
