import json
import time
from task_registry import registry_snapshot


def test_live_projection_supersedes_historical_record_and_exposes_expiry(tmp_path):
    for name in ('tasks','task-projections'):(tmp_path/name).mkdir()
    old={'id':'work','status':'running','children':[{'id':'step','status':'running'}]}
    (tmp_path/'tasks/work.json').write_text(json.dumps(old))
    projected={**old,'updated':time.time(),'children':[{'id':'step','status':'completed'}]}
    path=tmp_path/'task-projections/work.json';path.write_text(json.dumps(projected))
    rows,*_=registry_snapshot(tmp_path)
    assert len(rows)==1 and rows[0]['status']=='completed'
    projected={**old,'updated':time.time()-120};path.write_text(json.dumps(projected))
    rows,*_=registry_snapshot(tmp_path)
    assert rows[0]['stale'] and rows[0]['children'][0]['status']=='unknown'
    assert json.loads((tmp_path/'tasks/work.json').read_text())==old
