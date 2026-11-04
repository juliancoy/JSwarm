from scripts.swarm_pressure_guard import PressureWindow


def test_pressure_requires_persistence_and_recovery_hysteresis():
    guard=PressureWindow();hot={'iowait_percent':60,'io_pressure':70,'memory_pressure':20}
    cool={'iowait_percent':1,'io_pressure':2,'memory_pressure':0}
    assert guard.observe(hot)=='hold'
    assert guard.observe(hot)=='drain'
    assert guard.observe(cool)=='hold'
    assert guard.observe(cool)=='hold'
    assert guard.observe(cool)=='restore'


def test_cpu_busy_without_io_pressure_does_not_drain():
    guard=PressureWindow()
    sample={'iowait_percent':0,'io_pressure':0,'memory_pressure':0}
    assert all(guard.observe(sample)!='drain' for _ in range(5))


def test_guard_preserves_owner_maintenance_and_only_removes_its_own(tmp_path,monkeypatch):
    import hashlib,json,os,subprocess,sys
    from pathlib import Path
    from scripts.swarm_pressure_guard import UPDATE
    monkeypatch.setenv('HOME',str(tmp_path))
    path=tmp_path/'.config/lifetech-launcher/maintenance.json';path.parent.mkdir(parents=True)
    machine=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()
    def apply(decision):
        subprocess.run([sys.executable,'-c',UPDATE],input=json.dumps({'machine':machine,'decision':decision,'sample':{}}),text=True,check=True,capture_output=True)
    path.write_text(json.dumps({'managed_by':'owner','reason':'audio session'}))
    apply('drain');assert json.loads(path.read_text())['managed_by']=='owner'
    apply('restore');assert path.exists()
    path.unlink();apply('drain');assert json.loads(path.read_text())['managed_by']=='swarm-pressure-guard'
    apply('restore');assert not path.exists()
