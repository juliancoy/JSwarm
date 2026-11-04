"""Durable, bounded test request inbox for the always-listening master."""
import copy
import json
from pathlib import Path
import threading
import time
import uuid
from datetime import datetime, timezone
from test_scope import normalize


def now():
    return datetime.now(timezone.utc).isoformat()


class TestRequests:
    def __init__(self, tests):
        self.tests = tests
        self.folder = tests.runner.folder / 'requests'
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.worker = None

    def save(self, record):
        path = self.folder / (record['id'] + '.json')
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(record)); tmp.chmod(0o600); tmp.replace(path)

    def records(self):
        result = []
        for path in self.folder.glob('*.json'):
            try: result.append(json.loads(path.read_text()))
            except (OSError, ValueError): continue
        return sorted(result, key=lambda r: r['submitted_at'])

    def submit(self, paths=None, suites=None, force=False):
        if type(force) is not bool:raise ValueError('force must be a boolean')
        selection = normalize(paths, suites)
        # Resolve against the current checkout before accepting a request. The
        # immutable published catalog is resolved again when execution starts.
        from work_queue import catalog
        from test_scope import select
        from distributed_worker import SUITES
        root = self.tests.runner.root.resolve()
        for path in selection['paths']:
            target = (root / path).resolve()
            if not target.is_relative_to(root) or not target.exists():
                raise ValueError('Code path does not exist in this workspace: ' + path)
        specs = copy.deepcopy(SUITES)
        select(catalog(self.tests.runner.root, specs), specs, selection)
        with self.lock:
            if sum(r['status'] in ('queued', 'running') for r in self.records()) >= 100:
                raise ValueError('Test request queue is full')
            record = {'id': str(uuid.uuid4()), 'submitted_at': now(), 'status': 'queued',
                      'selection': selection, 'force':force, 'run_id': None, 'finished_at': None}
            self.save(record)
        self.start()
        self.wake.set()
        return record

    def submit_research(self,profile,request_id):
        from research_jobs import profiles
        if profile not in profiles():raise ValueError('Unknown configured research profile')
        if str(uuid.UUID(request_id))!=request_id:raise ValueError('Invalid request ID')
        with self.lock:
            path=self.folder/(request_id+'.json')
            if path.exists():
                existing=json.loads(path.read_text())
                if existing.get('selection')!={'research':profile}:raise ValueError('Request ID already used')
                return existing
            if sum(r['status'] in ('queued','running') for r in self.records())>=100:raise ValueError('Job queue full')
            record={'id':request_id,'kind':'research','submitted_at':now(),'status':'queued',
                    'selection':{'research':profile},'run_id':None,'finished_at':None}
            self.save(record)
        self.start();self.wake.set();return record

    def cancel(self,ident):
        with self.tests.lock,self.lock:
            record=self.view(ident)
            if record['status']=='queued':
                record.update(status='stopped',finished_at=now());self.save(record);return True
            if record['status']=='running':return self.tests.stop_run(record['run_id'])
            return False

    def view(self, ident=None):
        with self.lock:
            if ident:
                if str(uuid.UUID(ident)) != ident: raise ValueError('Invalid request ID')
                path = self.folder / (ident + '.json')
                if not path.exists(): raise ValueError('Test request not found')
                return json.loads(path.read_text())
            return {'requests': self.records()[-100:][::-1]}

    def start(self):
        with self.lock:
            if self.worker and self.worker.is_alive(): return
            self.worker = threading.Thread(target=self.listen, daemon=True, name='test-request-inbox')
            self.worker.start()

    def tick(self):
        with self.tests.lock, self.lock:
            draining=(self.folder.parent/'DRAIN').exists()
            for record in self.records():
                if record['status'] == 'running':
                    try:
                        run = self.tests.view(record['run_id']).get('run') or {}
                    except ValueError:
                        record.update(status='interrupted', finished_at=now(), error='Associated run record is unavailable')
                        self.save(record)
                        continue
                    if not run.get('finished'): continue
                    record.update(status=run['status'], finished_at=run['finished'],
                                  duration_seconds=max(0, datetime.fromisoformat(run['finished']).timestamp() - datetime.fromisoformat(run['started']).timestamp()))
                    from runtime_stats import seconds
                    record['queue_seconds']=seconds(record['submitted_at'],record.get('started_at'))
                    record['total_seconds']=seconds(record['submitted_at'],record['finished_at'])
                    self.save(record)
                if record['status'] != 'queued' or draining: continue
                runner=self.tests.create_run(force=record.get('force',False),selection=record['selection'])
                if runner is None:continue
                # Persist the run ID before dispatch so restart cannot duplicate it.
                record.update(status='running', run_id=runner.run['id'], started_at=now())
                self.save(record)
                threading.Thread(target=runner.execute, daemon=True).start()

    def listen(self):
        import logging
        while True:
            try: self.tick()
            except Exception:
                logging.exception('Test request dispatcher failed')
            self.wake.wait(2)
            self.wake.clear()
